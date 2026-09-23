# -*- coding: utf-8 -*-
"""[대시보드] 텔레그램 중계·명령 큐 반증 확정분(B-TG*/B-CQ*/B-N*, 2026-09-23) 회귀 가드 — 실제 호출.

각 검사는 고치기 전 코드(base)에서 빨간불, 고친 코드에서 초록불이 되도록 짰다(반증 에이전트 probe_tgcq 재사용).
  TG1  답장인데 매핑 행이 없으면 「대기 PC 하나」 추론으로 ★다른 PC★ 에 코드가 갔다 · N2 edited_message 재라우팅
  TG2  효율 알람이 전송 전에 「중계 전송」 로그 + last 찍기 → 실패해도 3시간 침묵
  TG3  서버 텔레그램 제한시간이 매크로(15·40초)보다 길었다
  TG4  429 retry_after 를 버렸다
  TG5  /alert 에 은퇴 가드가 없었다
  TG6  nohost 알람이 원인 단정 + 폐기된 「퍼플 런처 재시작」 처방
  CQ1  set_info 가 15분 만료로 조용히 사라졌다
  CQ2  매크로가 ack 한 뒤 보내는 cancelled/rejected 가 no-op
  CQ7  스프레드행 「↺ 재시작」 이 update 를 보냈다
  CQ8  재시작·매크로만 update 가 3분 뒤 거짓 ✗
  CQ10 WS 로 이미 간 명령을 「취소됨」 이라 했다 · N1 200+ok:false 도 「취소됨」
  CQ11 재접속 드레인 전에 소켓 등록 → 새 명령이 밀린 것보다 먼저/두 번
  CQ12 업데이터 큐에 계정 접미사 id(PC-20b)가 그대로 들어가 아무도 안 집었다 · 은퇴 PC 에도 들어갔다
JS 는 main.HTML_DASHBOARD 에서 함수를 잘라 node 로 돌린다(test_summary 와 같은 방식).
"""
import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from _harness import (main, db, aiosqlite, ok, FakeWS, Req, run_all, finish,   # noqa: E402
                      HTTPException, TG_SENT)

MIN_CHECKS = 55     # 2026-09-23 실측 55 — 검사를 더하면 같이 올린다

CHAT = "999"
_NOOP_CALLS = []


async def _noop_alert(*a, **k):
    _NOOP_CALLS.append(a)
    return None

main.push_alert = _noop_alert
main.TENANTS.setdefault("main", {})


def _sess():
    return main.new_session("main")


async def _row(cid):
    async with aiosqlite.connect(db.DB_PATH) as c:
        c.row_factory = aiosqlite.Row
        async with c.execute("SELECT * FROM commands WHERE id=?", (cid,)) as cur:
            r = await cur.fetchone()
    return dict(r) if r else None


def _body(resp):
    try:
        return json.loads(resp.body)
    except Exception:
        return {}


# ───────────────────────── TG1 / N2 ─────────────────────────
async def t_tg1():
    main.TENANTS["main"]["chat_id"] = CHAT
    main.CHAT_TO_TENANT[CHAT] = "main"
    await db.tg_map_put(100, "PC-TA", CHAT)
    await db.tg_map_put(101, "PC-TB", CHAT)
    await main._tg_handle_update({"message": {"chat": {"id": CHAT}, "text": "abcd",
                                              "reply_to_message": {"message_id": 100}}})
    # A 의 행은 지워졌다 — 사람이 A 사진에 다시 답장(정정)
    await main._tg_handle_update({"message": {"chat": {"id": CHAT}, "text": "abce",
                                              "reply_to_message": {"message_id": 100}}})
    rows = await db.get_recent_commands(100)
    caps = [(r["pc_id"], str(r.get("args"))) for r in rows if r["command"] == "captcha_code"]
    ok("TG1-a 첫 답장은 A 로", any(p == "PC-TA" and "abcd" in a for p, a in caps), str(caps))
    ok("TG1-b 주인 없는 답장이 다른 PC(B)로 안 간다", not any(p == "PC-TB" for p, a in caps), str(caps))
    ok("TG1-c 「이미 처리됐거나 만료」 로 답한다", "이미 처리됐거나 만료" in (TG_SENT[-1][1] if TG_SENT else ""),
       str(TG_SENT[-1:]))
    # 답장이 아닌 코드는 예전처럼 「대기 하나」 추론(편의 기능 유지)
    await main._tg_handle_update({"message": {"chat": {"id": CHAT}, "text": "abcf"}})
    rows = await db.get_recent_commands(100)
    caps = [(r["pc_id"], str(r.get("args"))) for r in rows if r["command"] == "captcha_code"]
    ok("TG1-d 답장 아닌 코드는 대기 PC 하나(B)로", any(p == "PC-TB" and "abcf" in a for p, a in caps), str(caps))
    # N2 — 고친 메시지는 받지 않는다
    await db.tg_map_put(102, "PC-TC", CHAT)
    await main._tg_handle_update({"edited_message": {"chat": {"id": CHAT}, "text": "zzzz"}})
    rows = await db.get_recent_commands(100)
    caps = [(r["pc_id"], str(r.get("args"))) for r in rows if r["command"] == "captcha_code"]
    ok("TG1-e(N2) edited_message 는 라우팅 안 된다", not any("zzzz" in a for p, a in caps), str(caps))


# ───────────────────────── TG2 ─────────────────────────
async def _run_eff_ticks(n_ticks=1):
    orig_sleep = asyncio.sleep
    ticks = {"n": 0}

    async def fast_sleep(s, *a, **k):
        if s >= 90 or s == main.EFF_TICK:
            ticks["n"] += 1
            if ticks["n"] > n_ticks:
                raise asyncio.CancelledError
        return await orig_sleep(0)
    main.asyncio.sleep = fast_sleep
    try:
        await main._eff_watch()
    except asyncio.CancelledError:
        pass
    finally:
        main.asyncio.sleep = orig_sleep


async def t_tg2():
    main.TENANTS["main"]["chat_id"] = CHAT
    real_send, real_state = main.tg_send_text, main._build_full_state
    main.TELEGRAM_BOT_TOKEN = "x"
    calls = []

    async def _fail(chat, text, **kw):
        calls.append(text)
        return None

    async def _good(chat, text, **kw):
        calls.append(text)
        return 7
    try:
        # (a) _eff_say 직접 — 실패면 「중계 전송」 을 안 적는다
        main.tg_send_text = _fail
        r = await main._eff_say("main", "PC-E1", "시험")
        logs = [l["message"] for l in await db.get_logs(main.ns("main", "PC-E1"), limit=10)]
        ok("TG2-a 실패하면 로그에 「중계 전송」 이 없다", not any("중계 전송" in m for m in logs), str(logs))
        ok("TG2-b 실패는 「중계 실패」 로 적힌다", any("중계 실패" in m for m in logs), str(logs))
        ok("TG2-c 반환값이 실패(False)", r is False, repr(r))

        # (b) 감시 루프 — 실패면 last 를 3시간으로 안 찍는다(곧 재시도)
        key = main.ns("main", "PC-E2")

        async def _state(t):
            return [{"pc_id": "PC-E2", "status": "hunting", "efficiency": 1.0}]
        main._build_full_state = _state
        main._EFF[key] = {"since": time.time() - 3 * 3600, "last": 0.0, "worst": 1.0}
        await _run_eff_ticks(1)
        st = main._EFF.get(key) or {}
        due_in = main.EFF_RENOTIFY - (time.time() - st.get("last", 0))
        ok("TG2-d 전송 실패 뒤 재시도가 3시간이 아니라 ≤10분", due_in <= 601, "%.0fs" % due_in)
        # (c) 재시도 차례 — 텔레그램만(대시보드 말하기 알림을 또 안 울린다)
        st["last"] = time.time() - main.EFF_RENOTIFY - 1
        n_alert, n_send = len(_NOOP_CALLS), len(calls)
        main.tg_send_text = _good
        await _run_eff_ticks(1)
        ok("TG2-e 재시도는 텔레그램을 다시 보낸다", len(calls) == n_send + 1, str(len(calls) - n_send))
        ok("TG2-f 재시도는 대시보드 알림(말하기)을 또 안 울린다", len(_NOOP_CALLS) == n_alert,
           str(len(_NOOP_CALLS) - n_alert))
        st = main._EFF.get(key) or {}
        due_in = main.EFF_RENOTIFY - (time.time() - st.get("last", 0))
        ok("TG2-g 성공하면 3시간 재알람 간격", due_in > main.EFF_RENOTIFY - 60, "%.0fs" % due_in)
        logs = [l["message"] for l in await db.get_logs(key, limit=10)]
        ok("TG2-h 성공은 「중계 전송」 로 적힌다", any("중계 전송" in m for m in logs), str(logs[:2]))
    finally:
        main.tg_send_text, main._build_full_state = real_send, real_state
        main.TELEGRAM_BOT_TOKEN = ""
        main._EFF.clear()


# ───────────────────────── TG3 / TG4 (가짜 httpx) ─────────────────────────
class _FakeResp:
    def __init__(self, code, js):
        self.status_code, self._js, self.text = code, js, json.dumps(js)

    def json(self):
        return self._js


class _FakeClient:
    timeouts: list = []
    script: list = []
    posts: list = []

    def __init__(self, timeout=None, **k):
        _FakeClient.timeouts.append(timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def post(self, url, data=None, files=None):
        _FakeClient.posts.append(url.rsplit("/", 1)[-1])
        return _FakeClient.script.pop(0) if _FakeClient.script else _FakeResp(
            200, {"ok": True, "result": {"message_id": 1}})


def _real_tg_send_text():
    """하네스가 tg_send_text 를 기록기로 바꿔 둬서, 소스에서 진짜 함수를 다시 만든다."""
    src = open(main.__file__, encoding="utf-8").read()
    m = re.search(r"^async def tg_send_text\(.*?(?=\n\n)", src, re.S | re.M)
    g = dict(main.__dict__)
    exec(m.group(0), g)
    fn = g["tg_send_text"]
    fn.__globals__.update({})       # g 는 main 과 같은 객체(_tg_call 등)를 가리킨다
    return fn


async def t_tg34():
    import httpx
    real_client = httpx.AsyncClient
    orig_sleep = asyncio.sleep
    slept = []

    async def rec_sleep(s, *a, **k):
        slept.append(s)
        return await orig_sleep(0)
    httpx.AsyncClient = _FakeClient
    main.TELEGRAM_BOT_TOKEN = "x"
    try:
        # TG3 — 제한시간
        _FakeClient.timeouts[:] = []
        await _real_tg_send_text()(CHAT, "hi")
        t_text = sum(_FakeClient.timeouts)
        _FakeClient.timeouts[:] = []
        await main.tg_send_photo(CHAT, "cap", b"x")
        t_photo = sum(_FakeClient.timeouts)
        ok("TG3-a sendMessage 제한시간 ≤ 10초(매크로 15초보다 짧게)", t_text <= 10.0, str(t_text))
        ok("TG3-b sendPhoto 제한시간 ≤ 25초(매크로 40초보다 짧게)", t_photo <= 25.0, str(t_photo))
        # TG4 — 429 한 번 기다렸다 다시
        main.asyncio.sleep = rec_sleep
        _FakeClient.timeouts[:] = []
        _FakeClient.posts[:] = []
        _FakeClient.script[:] = [_FakeResp(429, {"ok": False, "parameters": {"retry_after": 3}}),
                                 _FakeResp(200, {"ok": True, "result": {"message_id": 9}})]
        res = await main._tg_call("sendMessage", {"chat_id": CHAT, "text": "x"}, timeout=10.0)
        ok("TG4-a 429 뒤 한 번 더 보내 성공", (res or {}).get("message_id") == 9, str(res))
        ok("TG4-b retry_after 만큼 기다렸다", slept[-1:] == [3.0], str(slept))
        _t = _FakeClient.timeouts
        ok("TG4-c 기다림+두 번째 제한시간이 남은 예산(10초) 안", len(_t) == 2 and _t[1] + slept[-1] <= 10.0 + 0.1,
           str(_t) + " wait=" + str(slept))
        slept[:] = []
        _FakeClient.script[:] = [_FakeResp(429, {"ok": False, "parameters": {"retry_after": 60}}),
                                 _FakeResp(200, {"ok": True, "result": {"message_id": 10}})]
        res = await main._tg_call("sendMessage", {"chat_id": CHAT, "text": "x"}, timeout=10.0)
        ok("TG4-d 긴 retry_after 는 5초로 자른다", slept[-1:] == [5.0] and (res or {}).get("message_id") == 10,
           str(slept) + str(res))
        _FakeClient.posts[:] = []
        _FakeClient.script[:] = [_FakeResp(429, {"ok": False, "parameters": {"retry_after": 1}}),
                                 _FakeResp(429, {"ok": False, "parameters": {"retry_after": 1}}),
                                 _FakeResp(200, {"ok": True, "result": {"message_id": 11}})]
        res = await main._tg_call("sendMessage", {"chat_id": CHAT, "text": "x"}, timeout=10.0)
        ok("TG4-e 재시도는 딱 한 번(두 번째 429 면 실패)", res is None and len(_FakeClient.posts) == 2,
           str(res) + str(_FakeClient.posts))
    finally:
        httpx.AsyncClient = real_client
        main.asyncio.sleep = orig_sleep
        main.TELEGRAM_BOT_TOKEN = ""
        _FakeClient.script[:] = []


# ───────────────────────── TG5 / TG6 ─────────────────────────
async def t_tg5():
    main.RETIRED_PCS.add(main.ns("main", "PC-R5"))
    try:
        r = await main.receive_alert("PC-R5", Req({"kind": "captcha", "message": "캡차 3회 실패"}))
        logs = await db.get_logs(main.ns("main", "PC-R5"), limit=5)
        ok("TG5-a 은퇴 id 의 /alert 는 로그를 안 만든다", len(logs) == 0, str(len(logs)))
        ok("TG5-b 응답은 {ok, retired}", _body(r).get("retired") is True, str(_body(r)))
    finally:
        main.RETIRED_PCS.discard(main.ns("main", "PC-R5"))
    r = await main.receive_alert("PC-A5", Req({"kind": "captcha", "message": "살아있는 PC"}))
    logs = await db.get_logs(main.ns("main", "PC-A5"), limit=5)
    ok("TG5-c 은퇴 아닌 PC 는 그대로 기록", len(logs) == 1 and _body(r).get("ok") is True, str(len(logs)))


def t_tg6():
    src = inspect.getsource(main._rot_step_pc)
    ok("TG6-a nohost 알람에 폐기된 「퍼플 런처 재시작」 처방이 없다", "'재시작' 을 누르거나" not in src)
    ok("TG6-b nohost 알람은 관측(3회+ nohost)만 적는다",
       "'퍼플온이 실행된 PC가 없습니다' 3회+ (nohost)" in src and "로그인 안 됐을 수 있음" in src)


# ───────────────────────── CQ1 / CQ2 ─────────────────────────
async def t_cq1():
    old = "2020-01-01T00:00:00"
    c_info = await db.insert_command("PC-Q1", "set_info", {"kv": {"계정1_서버": "x"}})
    c_start = await db.insert_command("PC-Q1", "start", {})
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE commands SET created_at=? WHERE id IN (?,?)", (old, c_info, c_start))
        await c.commit()
    got = await db.get_pending_commands("PC-Q1", all_key="all", limit=5)
    ids = [g["id"] for g in got]
    ok("CQ1-a 15분 넘은 set_info 도 배달된다(만료 면제)", c_info in ids, str(ids))
    ok("CQ1-b 15분 넘은 start 는 여전히 만료", c_start not in ids and (await _row(c_start))["status"] == "expired")


async def t_cq2():
    cid = await db.insert_command("PC-Q2", "collect_info", {})
    await main.ack_cmd("PC-Q2", cid, Req({}))                       # 받자마자 ack
    r = await main.ack_cmd("PC-Q2", cid, Req({"status": "cancelled", "why": "오버라이드"}))
    ok("CQ2-a acked 뒤 매크로 cancelled 통지 → cancelled", (await _row(cid))["status"] == "cancelled",
       str(await _row(cid)))
    ok("CQ2-b 응답 ok/status", _body(r).get("ok") is True and _body(r).get("status") == "cancelled", str(_body(r)))
    cid2 = await db.insert_command("PC-Q2", "start", {})
    await main.ack_cmd("PC-Q2", cid2, Req({}))
    await main.ack_cmd("PC-Q2", cid2, Req({"status": "rejected", "why": "이전 명령 처리 중"}))
    ok("CQ2-c acked 뒤 rejected 통지도 ⛔(cancelled)", (await _row(cid2))["status"] == "cancelled")
    # 대시보드 ✕ 는 그대로 pending 만
    cid3 = await db.insert_command("PC-Q2", "stop", {})
    await main.ack_cmd("PC-Q2", cid3, Req({}))
    r3 = await main.cancel_cmd(cid3, Req(api_key=None, session=_sess()))
    ok("CQ2-d 대시보드 ✕ 는 acked 를 못 뒤집는다", (await _row(cid3))["status"] == "acked"
       and _body(r3).get("ok") is False, str(_body(r3)))
    # 만료는 매크로 통지로도 안 뒤집는다
    cid4 = await db.insert_command("PC-Q2", "sell", {})
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE commands SET status='expired' WHERE id=?", (cid4,))
        await c.commit()
    await main.ack_cmd("PC-Q2", cid4, Req({"status": "cancelled"}))
    ok("CQ2-e expired 는 그대로", (await _row(cid4))["status"] == "expired")


# ───────────────────────── CQ10 ─────────────────────────
async def t_cq10():
    nspc = main.ns("main", "PC-Q10")
    ws = FakeWS()
    main.macro_ws_connections[nspc] = ws
    try:
        cid = await db.insert_command(nspc, "start", {})
        sent = await main.send_command_to_macro(nspc, "start", {}, cid)
        r = await main.cancel_cmd(cid, Req(api_key=None, session=_sess()))
        b = _body(r)
        ok("CQ10-a WS 로 간 명령 취소는 delivered:true", sent and b.get("delivered") is True, str(b))
        ok("CQ10-b 그 명령은 「취소됨」 이 아니다(ok false · 행 pending)",
           b.get("ok") is False and (await _row(cid))["status"] == "pending", str(b))
    finally:
        main.macro_ws_connections.pop(nspc, None)
    cid2 = await db.insert_command(nspc, "start", {})
    r2 = await main.cancel_cmd(cid2, Req(api_key=None, session=_sess()))
    b2 = _body(r2)
    ok("CQ10-c 안 간 명령은 그대로 취소(delivered:false)",
       b2.get("ok") is True and b2.get("delivered") is False and (await _row(cid2))["status"] == "cancelled", str(b2))


# ───────────────────────── CQ11 ─────────────────────────
async def _wait_reg(nspc, ws, n=100):
    for _ in range(n):
        await asyncio.sleep(0.01)
        if main.macro_ws_connections.get(nspc) is ws:
            return True
    return False


def _sent_ids(ws):
    out = []
    for m in ws.sent:
        try:
            j = json.loads(m)
        except Exception:
            continue
        if j.get("type") == "command":
            out.append(j["id"])
    return out


async def t_cq11():
    # (a) 상한(8)을 넘게 밀린 PC — 새 명령이 남은 옛 것을 새치기하지 않는다
    nspc = main.ns("main", "PC-W1")
    olds = [await db.insert_command(nspc, "go_home", {"n": i}) for i in range(10)]
    ws = FakeWS()
    task = asyncio.create_task(main.macro_websocket(ws, "PC-W1"))
    reg = await _wait_reg(nspc, ws)
    await asyncio.sleep(0.2)      # 고치기 전 코드는 드레인 ★전에★ 등록하므로 드레인이 끝나길 기다린다
    ids = _sent_ids(ws)
    ok("CQ11-a 재접속 드레인은 오래된 순 8건", reg and ids == olds[:main.WS_RECONNECT_DRAIN], str(ids))
    new = await db.insert_command(nspc, "stop", {})
    sent = await main.send_command_to_macro(nspc, "stop", {}, new)
    ok("CQ11-b 밀린 게 남았으면 새 명령은 WS 로 새치기 안 한다", sent is False, str(sent))
    for c in olds:
        await db.ack_command(c)
    new2 = await db.insert_command(nspc, "start", {})
    # new(stop) 가 아직 pending 이라 그보다 늦은 new2 도 새치기 안 한다 — new 를 치우면 다시 WS
    await db.ack_command(new)
    sent2 = await main.send_command_to_macro(nspc, "start", {}, new2)
    ok("CQ11-c 밀린 게 다 나가면 다시 WS 즉시 전송", sent2 is True, str(sent2))
    ws.die()
    await asyncio.wait_for(task, 5)

    # (b) 드레인 도중 들어온 명령 — 밀린 것 뒤에, 한 번만
    nspc2 = main.ns("main", "PC-W2")
    c1 = await db.insert_command(nspc2, "go_home", {})
    c2 = await db.insert_command(nspc2, "sell", {})
    real_enrich = main.enrich_cmd_args
    injected = {}

    async def _enrich(tenant, pc_id, command, args):
        if not injected and pc_id == "PC-W2":
            injected["id"] = await db.insert_command(nspc2, "stop", {})
            injected["ws"] = await main.send_command_to_macro(nspc2, "stop", {}, injected["id"])
        return await real_enrich(tenant, pc_id, command, args)
    main.enrich_cmd_args = _enrich
    ws2 = FakeWS()
    try:
        task2 = asyncio.create_task(main.macro_websocket(ws2, "PC-W2"))
        await _wait_reg(nspc2, ws2)
        await asyncio.sleep(0.2)
    finally:
        main.enrich_cmd_args = real_enrich
    ids2 = _sent_ids(ws2)
    ok("CQ11-d 드레인 중 들어온 명령은 밀린 것 ★뒤★ 에", ids2 == [c1, c2, injected.get("id")], str(ids2))
    ok("CQ11-e 두 번 안 간다", len(ids2) == len(set(ids2)), str(ids2))
    ws2.die()
    await asyncio.wait_for(task2, 5)


# ───────────────────────── CQ12 ─────────────────────────
async def t_cq12():
    s = _sess()
    await main.dashboard_send_updater_command("PC-40b", Req({"command": "update"}, api_key=None, session=s))
    r = await main.updater_poll_command("PC-40", Req())
    ok("CQ12-a 계정 접미사 id 로 보낸 업데이터 명령을 물리 PC 업데이터가 받는다",
       _body(r).get("command") == "update", str(_body(r)))
    main.RETIRED_PCS.add(main.ns("main", "PC-41"))
    try:
        code = None
        try:
            await main.dashboard_send_updater_command("PC-41c", Req({"command": "restart"}, api_key=None, session=s))
        except HTTPException as e:
            code = e.status_code
        ok("CQ12-b 은퇴한 물리 PC 에는 넣지 않고 410", code == 410, str(code))
        r = await main.updater_poll_command("PC-41", Req())
        ok("CQ12-c 은퇴 PC 큐는 비어 있다", _body(r).get("command") is None, str(_body(r)))
    finally:
        main.RETIRED_PCS.discard(main.ns("main", "PC-41"))


# ───────────────────────── JS (node) ─────────────────────────
def _js_func(src: str, name: str) -> str:
    """test_summary._js_func 와 같은 절단기 — `function name(` 부터 짝 맞는 `}` 까지."""
    i = src.index("function %s(" % name)
    j = src.index("{", i)
    if src[max(0, i - 6):i] == "async ":
        i -= 6
    depth, k, n = 0, j, len(src)
    stack = []
    while k < n:
        c = src[k]
        if stack and stack[-1] == "tpl":
            if c == "\\":
                k += 2
                continue
            if c == "`":
                stack.pop()
            elif c == "$" and src[k + 1:k + 2] == "{":
                stack.append(depth)
                depth += 1
                k += 2
                continue
            k += 1
            continue
        if c in "'\"":
            q = c
            k += 1
            while k < n and src[k] != q:
                k += 2 if src[k] == "\\" else 1
            k += 1
            continue
        if c == "`":
            stack.append("tpl")
            k += 1
            continue
        if src.startswith("//", k):
            k = src.index("\n", k)
            continue
        if src.startswith("/*", k):
            k = src.index("*/", k) + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if stack and stack[-1] == depth:
                stack.pop()
            elif depth == 0:
                return src[i:k + 1]
        k += 1
    raise ValueError("짝이 안 맞음: " + name)


def _run_node(js: str):
    node = shutil.which("node")
    if not node:
        return None
    d = tempfile.mkdtemp(prefix="tgcqjs_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])
    return json.loads(r.stdout.strip().splitlines()[-1])


_JS_CANCEL = r"""
let toasts = [];
function showToast(t){ toasts.push(t); }
let RESP = null;
async function fetch(){ if (RESP === 'throw') throw new Error('net'); return RESP; }
%s
(async () => {
  const out = {};
  RESP = {ok:true, json: async()=>({ok:false})};            await cancelCmd(1); out.n1 = toasts.pop();
  RESP = {ok:true, json: async()=>({ok:false, delivered:true})}; await cancelCmd(2); out.dl = toasts.pop();
  RESP = {ok:true, json: async()=>({ok:true})};             await cancelCmd(3); out.good = toasts.pop();
  RESP = {ok:false, json: async()=>({})};                   await cancelCmd(4); out.bad = toasts.pop();
  console.log(JSON.stringify(out));
})();
"""

_JS_UPD = r"""
let NOW = 1e12; Date.now = () => NOW;
let state = {};
let latestVersions = {macro:'2.1', updater:'3.0'};
%s
const out = {};
const B = 'PC-50';
function run(cmd, before, after, advance){
  state = {}; state[B] = before; UPD_RESULT = {};
  updResultStart(B, before._updater_version || '', cmd);
  state = {}; state[B] = after;
  NOW += 30000; updResultSweep();
  let r1 = UPD_RESULT[B].result;
  if (advance) { NOW += 200000; updResultSweep(); }
  return UPD_RESULT[B].result;
}
out.restart_ok   = run('restart', {_updater_version:'3.0', macro_version:'2.0', uptime_hours:5.0},
                                  {_updater_version:'3.0', macro_version:'2.0', uptime_hours:0.01}, true);
out.macro_upd    = run('update',  {_updater_version:'3.0', macro_version:'2.0', uptime_hours:5.0},
                                  {_updater_version:'3.0', macro_version:'2.1', uptime_hours:0.01}, true);
out.no_evidence  = run('restart', {_updater_version:'3.0', macro_version:'2.0'},
                                  {_updater_version:'3.0', macro_version:'2.0'}, true);
out.restart_fail = run('restart', {_updater_version:'3.0', macro_version:'2.0', uptime_hours:5.0},
                                  {_updater_version:'3.0', macro_version:'2.0', uptime_hours:5.06}, true);
out.upd_updater  = run('update',  {_updater_version:'2.9', macro_version:'2.1', uptime_hours:5.0},
                                  {_updater_version:'3.0', macro_version:'2.1', uptime_hours:5.0}, false);
out.upd_latest   = run('update',  {_updater_version:'3.0', macro_version:'2.1', uptime_hours:5.0},
                                  {_updater_version:'3.0', macro_version:'2.1', uptime_hours:0.01}, false);
out.upd_stale    = run('update',  {_updater_version:'3.0', macro_version:'1.9', uptime_hours:5.0},
                                  {_updater_version:'3.0', macro_version:'1.9', uptime_hours:0.01}, true);
console.log(JSON.stringify(out));
"""


def t_js():
    src = main.HTML_DASHBOARD
    # CQ7 — 「↺ 재시작」 버튼은 전부 restart 를 보낸다
    cmds = re.findall(r"sendUpdaterCmd\('\$\{pc\}','(\w+)'\)\"[^>]*>↺ 재시작<", src)
    ok("CQ7-a 스프레드행 「↺ 재시작」 은 restart", bool(cmds) and all(c == "restart" for c in cmds), str(cmds))
    if not shutil.which("node"):
        ok("JS node 가 있어야 한다(대시보드 JS 는 node 로만 시험된다)", False)
        return
    try:
        o = _run_node(_JS_CANCEL % _js_func(src, "cancelCmd"))
    except Exception as e:
        ok("JS cancelCmd 실행", False, str(e)[:400])
        o = {}
    ok("N1-a 200 + ok:false 는 「취소됨」 이 아니다", o.get("n1") not in (None, "✕ 명령 취소됨"), str(o.get("n1")))
    ok("CQ10-d delivered 면 「이미 전달됨 — 정지로 끊으세요」", "이미 전달됨" in str(o.get("dl")), str(o.get("dl")))
    ok("N1-b 진짜 취소는 그대로 「✕ 명령 취소됨」", o.get("good") == "✕ 명령 취소됨", str(o.get("good")))
    ok("N1-c HTTP 실패는 실패", "실패" in str(o.get("bad")), str(o.get("bad")))
    try:
        u = _run_node(_JS_UPD % "\n".join(
            [re.search(r"let UPD_RESULT = \{\};", src).group(0)]
            + [_js_func(src, n) for n in ("updResultStart", "updResultSweep")]))
    except Exception as e:
        ok("JS updResult 실행", False, str(e)[:400])
        u = {}
    ok("CQ8-a restart — 가동시간이 줄면 ✓", u.get("restart_ok") == "ok", str(u.get("restart_ok")))
    ok("CQ8-b 매크로만 바뀐 update 도 ✓", u.get("macro_upd") == "ok", str(u.get("macro_upd")))
    ok("CQ8-c 증거가 없으면 ✗ 가 아니라 ?(unknown)", u.get("no_evidence") == "unknown", str(u.get("no_evidence")))
    ok("CQ8-d restart 인데 가동시간이 계속 늘면 ✗", u.get("restart_fail") == "fail", str(u.get("restart_fail")))
    ok("CQ8-e 업데이터 버전이 바뀐 update 는 ✓", u.get("upd_updater") == "ok", str(u.get("upd_updater")))
    ok("CQ8-f 이미 최신이던 update 는 재기동으로 ✓", u.get("upd_latest") == "ok", str(u.get("upd_latest")))
    ok("CQ8-g 재기동됐는데 옛 버전 그대로면 ✗", u.get("upd_stale") == "fail", str(u.get("upd_stale")))


run_all([t_tg1, t_tg2, t_tg34, t_tg5, t_tg6, t_cq1, t_cq2, t_cq10, t_cq11, t_cq12, t_js])
finish("test_tgcq_b", MIN_CHECKS)
