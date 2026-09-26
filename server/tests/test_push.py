# -*- coding: utf-8 -*-
"""대시보드 상태 방송(push_state · 상태 펌프 · ConnectionManager.broadcast) — 느린 대시보드가 남을 붙잡지 않고,
바뀐 카드만 보내도 화면이 서버와 ★같은 상태★ 로 끝난다.

2026-09-23 반응속도 실측: 운영 /diag/perf push_state 최대 11,734ms. 로컬 재현 — 3초 걸리는 대시보드
소켓 1개가 붙어 있으면 매크로 보고 1건이 push_state 에서 3,016ms 를 기다렸고, 멀쩡한 대시보드도
그만큼 늦게 받았다. 운영 WS 60초: 전량 111통 11.4MB 인데 화면에서 달라진 카드는 66장(163KB).
→ 보고는 기다리지 않고, 판 번호(ver)를 붙여 앞 판을 가진 화면엔 바뀐 카드만(state_diff) 보낸다.

    cd updater/server && python -X utf8 tests/test_push.py
"""
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import main, ok, FakeWS, run_all, finish   # noqa: E402

MIN_CHECKS = 44


class TimedWS(FakeWS):
    """보내는 데 delay 초 걸리는 대시보드. hang=True 면 영영 안 끝난다."""

    def __init__(self, delay=0.0, hang=False):
        super().__init__()
        self.delay, self.hang, self.at = delay, hang, []

    async def send_text(self, m):
        if self.hang:
            await asyncio.Event().wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(m)
        self.at.append(time.perf_counter())

    async def close(self, code=1000):
        self.closed = code


class _Env:
    """_build_full_state 를 바꿔 끼우고, 방송 상태(_PUSH·_FEED·manager)를 깨끗이 되돌린다.
    rows: 조립 때마다 돌려줄 카드 목록을 만드는 함수(n=조립 번호)."""

    def __init__(self, gap=None, rows=None):
        self.gap = gap
        self.n = 0
        self.fail = False
        self.rows = rows or (lambda n: [{"pc_id": "PC-01", "status": "hunting", "seq": n}])

    async def _rows(self, tenant="main"):
        self.n += 1
        if self.fail:
            raise RuntimeError("조립 실패(시험)")
        return [dict(r) for r in self.rows(self.n)]

    def _reset(self):
        main._PUSH.clear()
        main._FEED.clear()
        for d in (main.manager._locks, main.manager._backlog, main.manager._sent_ver, main.manager._pump):
            d.clear()
        main.manager.active = []

    def __enter__(self):
        self._o = (main._build_full_state, list(main.manager.active), main.PUSH_MIN_GAP_S,
                   dict(main._version_cache))
        main._build_full_state = self._rows
        main._version_cache["data"] = {"exe": {"version": "1.0"}, "updater": {"version": "1.0"}}
        main._version_cache["ts"] = time.time()
        if self.gap is not None:
            main.PUSH_MIN_GAP_S = self.gap
        self._reset()
        return self

    def __exit__(self, *e):
        self._reset()
        main._build_full_state = self._o[0]
        main.manager.active = self._o[1]
        main.PUSH_MIN_GAP_S = self._o[2]
        main._version_cache.clear()
        main._version_cache.update(self._o[3])


_REAL_SLEEP = asyncio.sleep


class _VClock:
    """★가상 시계(2026-09-23 test_push C-1 흔들림)★ — main 의 time.monotonic · asyncio.sleep 만 갈아끼운다.
    C-1 은 「보고가 이어지는 시간 ÷ PUSH_MIN_GAP_S」 로 방송 수가 정해지는데, 예전엔 그 시간을 진짜
    asyncio.sleep(0.01) × 20 으로 만들었다. 윈도 타이머는 한 칸이 15.6ms 이고 기계·부하에 따라 한 번에
    14~70ms 가 걸려(실측: 0.28s ~ 1.41s) 문턱 0.3s 를 넘나들었다 → 방송 2~6번(단독 실행 5/5 실패).
    → 시간은 시험이 advance 로만 민다. 잠든 일꾼은 마감 순서대로 깨운다. 그 밖의 time·asyncio 는 진짜."""

    def __init__(self, start=1000.0):
        self.now = start
        self._waits = []            # [(마감, future)]
        self._o = None

    def monotonic(self):
        return self.now

    async def sleep(self, d, result=None):
        if d <= 0:
            await _REAL_SLEEP(0)
            return result
        fut = asyncio.get_running_loop().create_future()
        self._waits.append((self.now + d, fut))
        await fut
        return result

    async def _settle(self):
        for _ in range(50):          # 깨어난 일꾼·펌프가 제 할 일을 끝낼 만큼 넘겨준다(진짜 시간은 안 흐른다)
            await _REAL_SLEEP(0)

    async def advance(self, dt):
        target = self.now + dt
        while True:
            await self._settle()
            due = [w for w in self._waits if w[0] <= target]
            if not due:
                break
            self.now = max(self.now, min(w[0] for w in due))
            for w in [w for w in self._waits if w[0] <= self.now]:
                self._waits.remove(w)
                if not w[1].done():
                    w[1].set_result(None)
        self.now = target
        await self._settle()

    def __enter__(self):
        clock = self

        class _T:
            def __getattr__(self, n):
                return getattr(time, n)
            monotonic = staticmethod(clock.monotonic)

        class _A:
            def __getattr__(self, n):
                return getattr(asyncio, n)
            sleep = staticmethod(clock.sleep)
        self._o = (main.time, main.asyncio)
        main.time, main.asyncio = _T(), _A()
        return self

    def __exit__(self, *e):
        main.time, main.asyncio = self._o
        for _d, f in self._waits:
            if not f.done():
                f.cancel()
        self._waits.clear()


async def _quiesce(timeout=10.0):
    """앞 시험이 뒤에 남긴 작업(보내던 중인 느린 소켓 등)이 다 끝날 때까지 — 다음 시험의 전역 기록을 안 흐리게."""
    me = asyncio.current_task()
    rest = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
    if rest:
        await asyncio.wait(rest, timeout=timeout)


def _client(ws):
    """화면(applyStateMsg)과 같은 규칙으로 받은 글을 적용 — [(종류, 그때 상태)] 와 최종 상태."""
    state, ver, hist, bad = {}, -1, [], 0
    for m in ws.sent:
        d = json.loads(m)
        if d.get("type") == "state":
            state = {p["pc_id"]: p for p in d.get("pcs") or []}
            ver = d.get("ver", -1)
        elif d.get("type") == "state_diff":
            if d.get("base") != ver:
                bad += 1
                ver = -1
                continue
            for p in d.get("upd") or []:
                state[p["pc_id"]] = p
            for i in d.get("del") or []:
                state.pop(i, None)
            ver = d["ver"]
        else:
            continue
        hist.append((d["type"], json.loads(json.dumps(state))))
    return hist, state, bad


def _strip(st):
    return {k: {kk: vv for kk, vv in v.items() if kk not in main.STATE_VOLATILE} for k, v in st.items()}


async def t_slow_dashboard_does_not_block():
    with _Env(gap=0.2):
        fast, slow = TimedWS(), TimedWS(delay=1.5)
        main.manager.active = [(slow, "main"), (fast, "main")]      # 느린 쪽이 앞 — 예전엔 이게 뒤를 막았다
        t0 = time.perf_counter()
        await main.push_state("main")
        waited = (time.perf_counter() - t0) * 1000
        ok("B-1 느린 대시보드가 붙어 있어도 보고(push_state)는 기다리지 않는다(<50ms)", waited < 50, "%.1fms" % waited)
        await asyncio.sleep(0.8)
        lag = (fast.at[0] - t0) * 1000 if fast.at else None
        ok("B-2 멀쩡한 대시보드는 느린 쪽을 안 기다리고 1초 안에 받는다", lag is not None and lag < 1000, str(lag))
        await main._push_drain("main")
        ok("B-3 느린 대시보드도 (끊기지 않았으면) 결국 받는다", len(slow.sent) >= 1, str(len(slow.sent)))


async def t_coalesce_keeps_last_state():
    # ★가상 시계로 돈다★ — 보고 20건을 가상 0.01초 간격(= 0.2초)에, 간격 0.3초. 방송은 첫 판 + 마지막 한 판 = 2번으로
    #   기계·부하와 상관없이 늘 같다. 묶음(PUSH_MIN_GAP_S)을 빼면 보고마다 한 판씩 → 20번 → C-1 이 깨진다.
    await _quiesce()
    gap = 0.3
    with _Env(gap=gap) as env, _VClock() as clock:
        fast = TimedWS()
        main.manager.active = [(fast, "main")]

        async def _drain():
            await clock.advance(gap * 2)                     # 잠든 일꾼을 가상 시간으로 깨운다
            await asyncio.wait_for(main._push_drain("main"), 5.0)   # 영영 안 끝나면 매달리지 말고 실패

        for _ in range(20):
            await main.push_state("main")
            await clock.advance(0.01)
        await clock.advance(0.05)
        await _drain()
        hist, st, bad = _client(fast)
        ok("C-1 보고 20건이 몰려도 방송은 몇 번으로 묶인다(1~3)", 1 <= len(hist) <= 3,
           "%s 조립=%d" % ([h[0] for h in hist], env.n))
        ok("C-2 ★화면의 마지막 상태 = 마지막 조립(유실 없음)★", st.get("PC-01", {}).get("seq") == env.n,
           "%s vs n=%d" % (st.get("PC-01"), env.n))
        before = len(fast.sent)
        await main.push_state("main")
        await _drain()
        ok("C-3 일꾼이 끝난 뒤 온 보고도 방송된다", len(fast.sent) == before + 1, "%d→%d" % (before, len(fast.sent)))


async def t_diff_protocol():
    cards = {"PC-01": {"pc_id": "PC-01", "status": "hunting", "kina": 1, "_macro_silent_s": 3, "_updater_age_s": 5},
             "PC-02": {"pc_id": "PC-02", "status": "idle", "kina": 7, "_macro_silent_s": 3, "_updater_age_s": 5}}
    with _Env(gap=0, rows=lambda n: list(cards.values())):
        ws = TimedWS()
        main.manager.active = [(ws, "main")]
        await main.push_state("main")
        await main._push_drain("main")
        d0 = json.loads(ws.sent[0])
        ok("P-1 새 화면의 첫 글은 전량(state) + 판 번호", d0.get("type") == "state" and isinstance(d0.get("ver"), int)
           and len(d0.get("pcs") or []) == 2, str(d0)[:120])
        cards["PC-01"] = dict(cards["PC-01"], kina=2)
        await main.push_state("main")
        await main._push_drain("main")
        d1 = json.loads(ws.sent[-1])
        ok("P-2 바뀐 카드 하나만 state_diff 로(base=앞 판)", d1.get("type") == "state_diff" and d1.get("base") == d0["ver"]
           and [p["pc_id"] for p in d1.get("upd") or []] == ["PC-01"] and d1.get("del") == [], str(d1)[:160])
        ok("P-2b 바뀌지 않은 retired·latest 는 조각에 안 싣는다", "retired" not in d1 and "latest" not in d1)
        n_before = len(ws.sent)
        # 매 조립마다 커지는 초 칸만 바뀜 → 안 보낸다
        for k in cards:
            cards[k] = dict(cards[k], _macro_silent_s=cards[k]["_macro_silent_s"] + 30, _updater_age_s=100)
        await main.push_state("main")
        await main._push_drain("main")
        ok("P-3 초 단위 칸(_macro_silent_s·_updater_age_s ≤270)만 변하면 아무것도 안 보낸다",
           len(ws.sent) == n_before, "%d→%d" % (n_before, len(ws.sent)))
        # 업데이터가 270초를 넘기면 화면 표시(N분전)가 바뀌므로 보낸다
        cards["PC-02"] = dict(cards["PC-02"], _updater_age_s=main.UPDATER_STALE_S + 31)
        await main.push_state("main")
        await main._push_drain("main")
        d3 = json.loads(ws.sent[-1])
        ok("P-4 업데이터 270초 넘김은 보낸다(화면 「N분전」)", d3.get("type") == "state_diff"
           and [p["pc_id"] for p in d3.get("upd") or []] == ["PC-02"], str(d3)[:120])
        # 카드가 사라지면 del
        del cards["PC-02"]
        await main.push_state("main")
        await main._push_drain("main")
        d4 = json.loads(ws.sent[-1])
        ok("P-5 없어진 카드는 del", d4.get("type") == "state_diff" and d4.get("del") == ["PC-02"], str(d4)[:120])
        # resync → 다음은 전량
        main.manager.resync(ws, "main")
        await main.manager.publish_state("main")
        await main._push_drain("main")
        d5 = json.loads(ws.sent[-1])
        ok("P-6 resync 하면 다음 글은 전량", d5.get("type") == "state" and d5.get("ver") == d4.get("ver"), str(d5)[:80])
        hist, st, bad = _client(ws)
        ok("P-7 화면 재구성 = 서버 마지막 조립(조각 어긋남 0)", bad == 0 and _strip(st) == _strip({k: v for k, v in cards.items()}),
           "bad=%d st=%s" % (bad, list(st)))
        # 새로 붙은 화면: 바뀐 게 없어도 전량을 받는다
        ws2 = TimedWS()
        main.manager.active.append((ws2, "main"))
        await main.push_state("main")
        await main._push_drain("main")
        ok("P-8 바뀐 게 없어도 새로 붙은 화면은 전량을 받는다", ws2.sent and json.loads(ws2.sent[0]).get("type") == "state",
           str(len(ws2.sent)))


async def t_random_consistency():
    """무작위 변화 · 빠른/느린/아주 느린 화면 셋 — 모두 조각 어긋남 없이 서버 최종 상태에 도달한다."""
    rnd = random.Random(7)
    cards = {("PC-%02d" % i): {"pc_id": "PC-%02d" % i, "kina": 0, "status": "hunting"} for i in range(1, 9)}

    def rows(n):
        for _ in range(rnd.randint(0, 3)):
            k = "PC-%02d" % rnd.randint(1, 12)
            r = rnd.random()
            if r < 0.15 and k in cards:
                del cards[k]
            else:
                cards[k] = {"pc_id": k, "kina": rnd.randint(0, 99), "status": rnd.choice(["hunting", "idle"])}
        return list(cards.values())
    with _Env(gap=0.02, rows=rows):
        a, b, c = TimedWS(), TimedWS(delay=0.07), TimedWS(delay=0.3)
        main.manager.active = [(a, "main"), (b, "main"), (c, "main")]
        for _ in range(60):
            await main.push_state("main")
            await asyncio.sleep(rnd.random() * 0.03)
        await main._push_drain("main")
        await asyncio.sleep(0.05)
        await main._push_drain("main")
        want = _strip(cards)
        res = []
        for w in (a, b, c):
            _h, st, bad = _client(w)
            res.append((bad, _strip(st) == want, sum(1 for m in w.sent if '"state_diff"' in m[:30]),
                        sum(1 for m in w.sent if m.startswith('{"type":"state",'))))
        ok("R-1 세 화면 모두 조각 어긋남 0 · 최종 상태 = 서버", all(r[0] == 0 and r[1] for r in res), str(res))
        ok("R-2 아주 느린 화면은 중간 판을 건너뛴다(보낸 수 < 빠른 화면)", len(c.sent) < len(a.sent),
           "%d vs %d" % (len(c.sent), len(a.sent)))
        ok("R-3 느린 화면도 끊기지 않았다(밀림이 쌓이지 않음)", all(any(x is w for x, _t in main.manager.active) for w in (a, b, c)))


async def t_order_per_socket():
    with _Env(gap=0):
        ws = TimedWS(delay=0.05)
        main.manager.active = [(ws, "main")]
        for i in range(6):
            await main.manager.broadcast({"type": "log", "i": i}, "main")
        await asyncio.sleep(0.6)
        got = [json.loads(m)["i"] for m in ws.sent]
        ok("O-1 한 소켓엔 보낸 순서대로 도착한다(동시 쓰기 없음)", got == list(range(6)), str(got))


async def t_drop_stuck_socket():
    with _Env(gap=0):
        _o_to = main.BROADCAST_SEND_TIMEOUT_S
        main.BROADCAST_SEND_TIMEOUT_S = 0.2
        try:
            stuck, fast = TimedWS(hang=True), TimedWS()
            main.manager.active = [(stuck, "main"), (fast, "main")]
            d0 = (main._PERF.get("broadcast_dropped") or {}).get("n", 0)
            await main.manager.broadcast({"type": "log", "i": 0}, "main")
            await asyncio.sleep(0.4)
            gone = not any(c is stuck for c, _t in main.manager.active)
            d1 = (main._PERF.get("broadcast_dropped") or {}).get("n", 0)
            ok("D-1 한 통이 상한을 넘긴 소켓은 끊는다(1013)", gone and stuck.closed == 1013 and d1 == d0 + 1,
               "gone=%s closed=%s dropped %d→%d" % (gone, stuck.closed, d0, d1))
            ok("D-2 그동안 멀쩡한 대시보드는 받았다", len(fast.sent) == 1)
            ok("D-3 끊긴 소켓의 자물쇠·밀림 기록이 남지 않는다(누수 없음)",
               id(stuck) not in main.manager._locks and id(stuck) not in main.manager._backlog)
            # 상태 펌프도 같은 상한으로 끊는다
            stuck2 = TimedWS(hang=True)
            main.manager.active = [(stuck2, "main")]
            await main.push_state("main")
            await asyncio.sleep(0.5)
            ok("D-3b 상태 펌프도 멈춘 소켓을 끊는다", stuck2.closed == 1013 and not main.manager.active, str(stuck2.closed))
        finally:
            main.BROADCAST_SEND_TIMEOUT_S = _o_to
        _o_w = main.BROADCAST_WAIT_S
        main.BROADCAST_WAIT_S = 0.01           # 부른 쪽이 거의 안 기다리게 → 느린 소켓에 밀림이 쌓인다
        try:
            slow = TimedWS(delay=0.5)
            main.manager.active = [(slow, "main")]
            for i in range(main.BROADCAST_BACKLOG_MAX + 2):
                await asyncio.wait_for(main.manager.broadcast({"type": "log", "i": i}, "main"), 1.0)
        finally:
            main.BROADCAST_WAIT_S = _o_w
        ok("D-4 로그 방송이 BROADCAST_BACKLOG_MAX 만큼 밀린 소켓은 끊는다",
           not any(c is slow for c, _t in main.manager.active) and slow.closed == 1013, str(slow.closed))
        # 방송 도중 다른 소켓이 끊겨도 기록을 다시 만들지 않는다(반증 #3)
        gone_ws = TimedWS()
        main.manager.active = [(gone_ws, "main")]
        main.manager.disconnect(gone_ws)
        await main.manager._send_one(gone_ws, "{}")
        ok("D-5 끊긴 소켓에 _send_one 이 와도 자물쇠를 새로 안 만든다",
           id(gone_ws) not in main.manager._locks and id(gone_ws) not in main.manager._backlog)


async def t_worker_survives_failure():
    with _Env(gap=0) as env:
        ws = TimedWS()
        main.manager.active = [(ws, "main")]
        env.fail = True
        f0 = (main._PERF.get("push_state_failed") or {}).get("n", 0)
        await main.push_state("main")
        await main._push_drain("main")
        f1 = (main._PERF.get("push_state_failed") or {}).get("n", 0)
        env.fail = False
        await main.push_state("main")
        await main._push_drain("main")
        ok("W-1 조립이 한 번 실패해도(센다) 다음 보고는 방송된다", f1 == f0 + 1 and len(ws.sent) == 1,
           "failed %d→%d sent=%d" % (f0, f1, len(ws.sent)))
        # 한 판이 영영 안 끝나도 일꾼은 상한 뒤 산다(반증 #1)
        _o = main.PUSH_BUILD_TIMEOUT_S
        main.PUSH_BUILD_TIMEOUT_S = 0.2
        hang = [True]

        async def _slow(tenant="main"):
            if hang[0]:
                hang[0] = False
                await asyncio.Event().wait()
            return [{"pc_id": "PC-01", "seq": 999}]
        main._build_full_state = _slow
        try:
            await main.push_state("main")
            await asyncio.sleep(0.05)
            await main.push_state("main")
            await main._push_drain("main")
        finally:
            main.PUSH_BUILD_TIMEOUT_S = _o
        _h, st, _b = _client(ws)
        ok("W-2 멈춘 판 하나 뒤에도 다음 판이 나간다(상한)", st.get("PC-01", {}).get("seq") == 999, str(st))
        main.manager.active = []
        await main.push_state("main")
        ok("W-3 보는 사람이 없으면 일꾼을 만들지 않는다",
           (main._PUSH.get("main") or {}).get("task") is None or main._PUSH["main"]["task"].done())


async def t_version_refresh_single():
    _o = (main._load_version_json_async, dict(main._version_cache))
    calls = []

    async def _fake():
        calls.append(1)
        await asyncio.sleep(0.05)
        return {}
    main._load_version_json_async = _fake
    main._version_cache.clear()
    main._VERSION_REFRESH[0] = None
    try:
        for _ in range(5):
            main._version_latest_nowait()
        await asyncio.sleep(0.1)
    finally:
        main._load_version_json_async = _o[0]
        main._version_cache.clear()
        main._version_cache.update(_o[1])
    ok("V-1 캐시가 비어 같은 순간 5번 불려도 갱신 작업은 하나(반증 #5)", len(calls) == 1, str(len(calls)))


async def t_instrumentation():
    # ★앞 시험이 남긴 느린 전송(D-4 의 0.5초 소켓 등)이 문턱 50ms 로 낮춘 사이 끝나면 [-1] 을 빼앗고,
    #   링(40칸)이 차 있으면 「늘었나」 도 못 본다 → 남은 작업을 먼저 끝내고, 링을 비우고, 내 것은 브라우저로 찾는다.★
    await _quiesce()
    with _Env(gap=0):
        _o = main.SLOW_SEND_MS
        _o_ring = list(main._SLOW_SENDS)
        main.SLOW_SEND_MS = 50
        main._SLOW_SENDS.clear()
        try:
            slow = TimedWS(delay=0.1)
            slow.headers = {"user-agent": "시험브라우저/1.0"}
            main.manager.active = [(slow, "main")]
            await main.push_state("main")
            await main._push_drain("main")
        finally:
            main.SLOW_SEND_MS = _o
            mine = [r for r in main._SLOW_SENDS if r.get("ua") == "시험브라우저/1.0"]
            main._SLOW_SENDS[:] = (_o_ring + main._SLOW_SENDS)[-40:]
        last = mine[-1] if mine else {}
        ok("I-1 느린 대시보드 전송은 시각·ms·크기·종류·브라우저로 남는다",
           last.get("kind") == "full" and last.get("ms", 0) >= 50 and last.get("ua") == "시험브라우저/1.0" and last.get("at"),
           str(last))
        # 대시보드 /ws 끊김 — code 가 남는다
        main.manager.active = []
        tok = main.new_session("main")
        ws = FakeWS()
        ws.cookies = {"session": tok}
        task = asyncio.create_task(main.websocket_endpoint(ws))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if main.manager.active:
                break
        n0 = len(main._DASH_CLOSES)
        ws.die()
        await asyncio.wait_for(task, timeout=5)
        last = main._DASH_CLOSES[-1] if len(main._DASH_CLOSES) > n0 else {}
        ok("I-2 대시보드 /ws 끊김은 code 와 함께 따로 남는다(매크로 끊김 통계와 안 섞임)",
           last.get("why") == "disconnect(code=1005)" and last.get("tenant") == "main", str(last))
        main._perf_note("시험_최대", 5.0)
        ok("I-3 최대값이 난 시각을 남긴다(ms_max_at)", bool(main._PERF["시험_최대"].get("ms_max_at")))
        main._PERF.pop("시험_최대", None)


async def t_pump_after_drop():
    """B2-1: 펌프가 보내는 중에 소켓이 끊기면(_drop 은 자물쇠 없이 disconnect) 판 번호를 되살리지 않는다."""
    with _Env(gap=0):
        ws = TimedWS(delay=0.3)
        main.manager.active = [(ws, "main")]
        await main.push_state("main")
        await asyncio.sleep(0.1)                  # 펌프가 전량을 보내는 중
        main.manager.disconnect(ws)               # broadcast 의 _drop 과 같은 길
        await main._push_drain("main")
        await asyncio.sleep(0.4)
        ok("B2-1 끊긴 소켓의 판 번호는 되살아나지 않는다(샘·id 재사용 시 조각부터 받는 일 없음)",
           id(ws) not in main.manager._sent_ver, str(main.manager._sent_ver))


class QWS(TimedWS):
    """화면이 보내는 글을 차례로 넣어 주는 소켓."""

    def __init__(self):
        super().__init__()
        self.q = asyncio.Queue()
        self.cookies = {}

    async def receive_text(self):
        m = await self.q.get()
        if m is None:
            raise main.WebSocketDisconnect(code=1000)
        return m


async def t_resync_gap():
    """resync 연타는 2초에 한 번 — 버리지 않고 미룬다(버리면 화면은 다시 안 조른다)."""
    with _Env(gap=0):
        ws = QWS()
        ws.cookies = {"session": main.new_session("main")}
        task = asyncio.create_task(main.websocket_endpoint(ws))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if ws.sent:
                break
        n0 = len(ws.sent)
        t0 = time.perf_counter()
        for _ in range(3):                        # 0.3초 간격 연타 — 예전엔 조를 때마다 전량(~155KB)
            ws.q.put_nowait('{"type":"resync"}')
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.5)
        fast = len(ws.sent) - n0
        await asyncio.sleep(1.0)
        later = len(ws.sent) - n0
        ws.q.put_nowait(None)
        await asyncio.wait_for(task, timeout=5)
        kinds = [json.loads(m).get("type") for m in ws.sent[n0:]]
        ok("B2-6 resync 0.3초 간격 3연타 → 1.4초 안엔 전량 1통만", fast == 1, "%d %s" % (fast, kinds))
        ok("B2-6b 미룬 resync 도 2초 뒤 전량을 받는다(버리지 않음)", later >= 2 and set(kinds) == {"state"},
           "%d %s %.1fs" % (later, kinds, time.perf_counter() - t0))


def _fn(src, name):
    i = src.index("function " + name + "(")
    d, k = 0, src.index("{", i)
    while True:
        c = src[k]
        d += (c == "{") - (c == "}")
        k += 1
        if d == 0:
            return src[i:k]


def _node(js):
    node = shutil.which("node")
    if not node:
        return None, "node 없음"
    d = tempfile.mkdtemp(prefix="pushjs2_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), ""
    except Exception:
        return None, r.stderr[-400:]


def t_js_feed_b2():
    src = main.HTML_DASHBOARD
    # B2-2 재접속하면 판 번호·조르기 표시가 비워진다
    i = src.index("let STATE_VER")
    js = (src[i:src.index("\n", i)] + "\nlet state={}, RETIRED=new Set(), latestVersions={}, _ws=null, _wsLastMsg=0, _wsVisSent=false;\n"
          + "const innerWidth=800, innerHeight=600, window={};\n"      # #271 connectWS 가 숨김 여부(_wsHid)를 주소에 싣는다
          + _fn(src, "_wsHid") + "\n" + _fn(src, "applyStateMsg") + "\n" + _fn(src, "connectWS") + r"""
const location={protocol:'http:',host:'x'}; const document={getElementById:()=>({}), hidden:false};
function WebSocket(u){ this.readyState=1; this.send=()=>{}; }
const sock={readyState:1, send:()=>{}};
applyStateMsg({type:'state', ver:5, pcs:[], retired:[]}, sock);
applyStateMsg({type:'state_diff', ver:9, base:8, upd:[], del:[]}, sock);   // 어긋남 → 조름
const before=_resyncAsked;
connectWS();
console.log(JSON.stringify({before, after:_resyncAsked, ver:STATE_VER}));
""")
    o, err = _node(js)
    ok("B2-2 재접속하면 STATE_VER=-1·_resyncAsked=false(앞 소켓의 조르기가 안 남는다)",
       o is not None and o["before"] is True and o["after"] is False and o["ver"] == -1, str(o or err))
    # B2-3 핸들을 누르고 끌지 않고 놓으면 draggable·dragSrcId 가 되돌아간다
    js = r"""
function baseId(x){ return String(x).replace(/[bcd]$/,''); }
let dragSrcId=null, dragSection=null; let saved=[];
function saveCurrentOrder(g,k){ saved.push([...GRID.children].map(gridKeyOf)); }
class El { constructor(id){ this.id=id; this.attrs={}; this.ls={}; this.classList={s:new Set(),add:(c)=>this.classList.s.add(c),remove:(c)=>this.classList.s.delete(c),contains:(c)=>this.classList.s.has(c)}; this.handle=null; }
  setAttribute(k,v){this.attrs[k]=v;} querySelector(q){ return q==='.drag-handle'?this.handle:null; }
  addEventListener(t,f,o){ (this.ls[t]=this.ls[t]||[]).push([f,o]); }
  fire(t,e){ const l=this.ls[t]||[]; this.ls[t]=l.filter(x=>!(x[1]&&x[1].once)); l.forEach(x=>x[0](Object.assign({stopPropagation(){},preventDefault(){this.dp=true}},e||{}))); }
  getBoundingClientRect(){return {top:0,height:100};} after(x){ const a=GRID.children; a.splice(a.indexOf(x),1); a.splice(a.indexOf(this)+1,0,x);} before(x){const a=GRID.children; a.splice(a.indexOf(x),1); a.splice(a.indexOf(this),0,x);} }
function mk(base){ const c=new El('stack-'+base); c.handle=new El('h'); return c; }
const GRID={children:[mk('PC-01'),mk('PC-02'),mk('PC-03')], querySelectorAll(){return [];}};
const DOC=new El('doc'); const document={getElementById:()=>GRID, addEventListener:(t,f,o)=>DOC.addEventListener(t,f,o)};
""" + _fn(src, "gridKeyOf") + "\n" + _fn(src, "setupDrag") + r"""
const [A,B,C]=GRID.children;
setupDrag('grid-online','k');
A.handle.fire('mousedown'); DOC.fire('mouseup');
B.handle.fire('mousedown'); DOC.fire('mouseup');
setupDrag('grid-online','k'); setupDrag('grid-online','k');
const r1={a:A.attrs.draggable, b:B.attrs.draggable, src:dragSrcId};
// 진짜 끌기는 그대로 된다: C 핸들 → dragstart → A 위에 drop
C.handle.fire('mousedown');
const dt={d:null,setData(t,v){this.d=v},getData(){return this.d}};
C.fire('dragstart',{dataTransfer:dt}); A.fire('drop',{dataTransfer:dt, clientY:10}); C.fire('dragend');
console.log(JSON.stringify({r1, order:GRID.children.map(gridKeyOf), c:C.attrs.draggable, src2:dragSrcId}));
"""
    o, err = _node(js)
    ok("B2-3 핸들을 누르기만 하고 놓으면 draggable=false·dragSrcId=null 로 돌아간다",
       o is not None and o["r1"] == {"a": "false", "b": "false", "src": None}, str(o or err))
    ok("B2-3b 진짜 끌기는 그대로 된다(C 를 A 앞으로)",
       o is not None and o["order"] == ["PC-03", "PC-01", "PC-02"] and o["c"] == "false" and o["src2"] is None, str(o or err))
    # B2-5 초기 /status 가 WS 전량보다 늦게 와도 덮지 않는다
    ok("B2-5 초기 /status 재시도는 WS 전량이 먼저 왔으면 state 를 안 덮는다",
       "if (STATE_VER !== -1) return true;" in src[src.index("const _initStatus"):src.index("const _initStatus") + 1200])
    # B2-4 툴팁은 분 단위(카드는 분이 바뀔 때만 다시 온다)
    ok("B2-4 업데이터 나이 툴팁에 초(_uage}초째)를 쓰지 않는다", "${_uage}초째" not in src)


def t_js_contract():
    src = main.HTML_DASHBOARD
    m = re.search(r"_ustale = \(_uage !== null && _uage > (\d+)\)", src)
    ok("J-1 서버 UPDATER_STALE_S = 화면 buildCard 의 업데이터 문턱", m and int(m.group(1)) == main.UPDATER_STALE_S,
       str(m.group(1) if m else None))
    node = shutil.which("node")
    if not node:
        ok("J-2 node 필요(없으면 일부러 빨간불)", False)
        return
    i = src.index("let STATE_VER")
    j = src.index("function applyStateMsg")
    depth, k = 0, src.index("{", j)
    while True:
        c = src[k]
        depth += (c == "{") - (c == "}")
        k += 1
        if depth == 0:
            break
    js = ("let state={}, RETIRED=new Set(), latestVersions={};\n" + src[i:src.index("\n", i)] + "\n" + src[j:k] + r"""
const sent=[]; const sock={readyState:1, send:(x)=>sent.push(x)};
const out={};
out.a = applyStateMsg({type:'state', ver:5, pcs:[{pc_id:'A',k:1},{pc_id:'B',k:1}], retired:['Z']}, sock);
out.b = applyStateMsg({type:'state_diff', ver:6, base:5, upd:[{pc_id:'A',k:2}], del:['B']}, sock);
out.s1 = JSON.stringify(state);
out.c = applyStateMsg({type:'state_diff', ver:9, base:8, upd:[{pc_id:'A',k:9}], del:[]}, sock);
out.d = applyStateMsg({type:'state_diff', ver:10, base:9, upd:[{pc_id:'A',k:10}], del:[]}, sock);
out.s2 = JSON.stringify(state); out.sent = sent.slice();
out.e = applyStateMsg({type:'state', ver:10, pcs:[{pc_id:'A',k:10}], retired:[]}, sock);
out.f = applyStateMsg({type:'state_diff', ver:11, base:10, upd:[], del:['A']}, sock);
out.s3 = JSON.stringify(state); out.ret = [...RETIRED];
console.log(JSON.stringify(out));
""")
    d = tempfile.mkdtemp(prefix="pushjs_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        o = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        ok("J-2 applyStateMsg 실행", False, r.stderr[-300:])
        return
    ok("J-2 전량 → 조각 적용(A 갱신, B 삭제)", o["a"] and o["b"] and o["s1"] == '{"A":{"pc_id":"A","k":2}}', o["s1"])
    ok("J-3 판이 어긋난 조각은 버리고 resync 를 ★한 번만★ 조른다", o["c"] is False and o["d"] is False
       and o["s2"] == o["s1"] and o["sent"] == ['{"type":"resync"}'], str(o["sent"]))
    ok("J-4 전량이 오면 다시 조각을 받는다", o["e"] and o["f"] and o["s3"] == "{}" and o["ret"] == [], str(o))


def test_all():
    run_all([t_slow_dashboard_does_not_block, t_coalesce_keeps_last_state, t_diff_protocol, t_random_consistency,
             t_order_per_socket, t_drop_stuck_socket, t_worker_survives_failure, t_version_refresh_single,
             t_instrumentation, t_pump_after_drop, t_resync_gap, t_js_contract, t_js_feed_b2])
    finish("test_push", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
