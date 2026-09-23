# -*- coding: utf-8 -*-
"""[대시보드] 순환 전환 상한 — 새 전환 명령부터 다시 재고, 진행 중이면 늘린다 (2026-09-24 PC-21, 사고 522 부류).

  PC-21: 00:18:50 순환 switch_launcher(계정2) → 00:23 첫 시도 실패 → 00:36 사람이 한 대 재시도(switch_acct.py =
  /command switch_account) → 00:37 본컴 전환 진행 → 00:38:50 «계정2 전환이 20분째 안 끝났습니다» 로 ★전환 도중★ 순환 정지.
  → main._rot_switch_age: ① 단계 시작 뒤 이 PC 카드에 들어온 새 전환 명령 시각부터 ROT_SWITCH_MAX 를 다시 잰다(명령마다 한 번)
    ② 그래도 넘었는데 카드가 acct_switching 을 신선하게 보고 중이면 ROT_SWITCH_HARD 까지 멈추지 않는다.
  ★그물은 남는다★ — 새 명령도 진행 보고도 없으면 예전처럼 20분에 ⛔ (사고 522 의 «단계별 상한은 그대로», ops 게이트 38 rotttl_test [3]).

★«old:» 를 적은 줄은 고치기 전 main.py 에서 실패한다★ (실제 _rot_step_pc 호출).

    cd updater/server && python -X utf8 tests/test_rot_switch_clock.py
"""
from datetime import datetime, timedelta, timezone

from _harness import main, db, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 53
M = main
SENT, SAID = [], []
BASE = "PC-21"
KEY = M.ns("main", BASE)


async def _f_send(t, pc, cmd, args=None):
    SENT.append((pc, cmd))
    return True


async def _f_say(t, pc, text, routine=False):
    SAID.append(text)


async def _f_save(force=False):
    pass


async def _f_logs(pc, limit=1000):
    return []


class _AnyPeer(dict):
    def get(self, k, d=None):
        return "peer"


async def _f_pm(t):
    return _AnyPeer()


async def _f_allow(t="main"):
    return {"*"}


async def _f_nm(t):
    return {}


_PATCH = {"_rot_send": _f_send, "_rot_say": _f_say, "_rot_save": _f_save, "get_logs": _f_logs,
          "_get_parsec_map": _f_pm, "_rot_allow": _f_allow, "_rot_nm_clears": _f_nm}
_ORIG = {}


def _iso(ago_s):
    return (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).strftime("%Y-%m-%dT%H:%M:%S")


def card(pid, status, ago, recv=None, **kw):
    """ago = 매크로 시계(last_active), recv = 서버가 받은 나이(_updated_at, 기본은 ago 와 같다 · False 면 칸 없음)."""
    d = {"pc_id": pid, "status": status, "last_active": _iso(ago),
         "acct_ids": {"1": "a@x", "2": "b@x"}, "acct_names": {"1": ["A"], "2": ["B"]}}
    if recv is not False:
        d["_updated_at"] = _iso(ago if recv is None else recv)
    d.update(kw)
    return d


def _st(ago, **kw):
    now = M._rot_now()
    st = {"stage": "switching", "since": now - ago, "armed_at": now - 9999, "day": M._kst_today_key(),
          "target": "b", "full": False, "hops": 1, "visits": {}, "expect_restart": False}
    st.update(kw)
    return st


async def step(st, pcs, base=BASE):
    key = M.ns("main", base)
    M._ROT[key] = st
    M._ROT_STEP_CUR.update(key=key, st=st)
    try:
        await M._rot_step_pc("main", base, st, pcs)
    finally:
        M._ROT_STEP_CUR.update(key=None, st=None)
    return M._ROT.get(key) is st            # True = 아직 무장(안 꺼졌다)


def _stopped():
    return any("순환 정지" in s and "전환이" in s for s in SAID)


def reset():
    SENT.clear(); SAID.clear(); M._ROT.clear()


async def _insert(pc, cmd, ago, status=None):
    cid = await db.insert_command(M.ns("main", pc), cmd, {})
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE commands SET created_at=? WHERE id=?", (_iso(ago), cid))
        if status:
            await c.execute("UPDATE commands SET status=? WHERE id=?", (status, cid))
        await c.commit()
    return cid


# 계정1 카드(PC-21)가 아직 살아 있고(전환 안 끝남) 계정2 카드(PC-21b)는 다른 계정 — 전환이 20분을 넘긴 모양
def _pcs(status1="idle", ago1=900, base=BASE):
    return [card(base, status1, ago1), card(base + "b", "other_account", 3000)]


async def t_reclock_on_new_command():
    reset()
    st = _st(1210)                                            # 00:18:50 → 00:38:50
    cid = await _insert("PC-21b", "switch_account", 150)      # 00:36 사람이 한 대 재시도
    alive = await step(st, _pcs())
    ok("R-1 ★old: 단계 시작 뒤 새 전환 명령이 들어왔으면 20분째여도 순환을 끄지 않는다(PC-21 00:38:50)★",
       alive and not _stopped(), str(SAID))
    ok("R-1b 상한 시계가 그 명령 시각으로 옮겨졌다(나이 ≈ 150초)", 140 <= M._rot_now() - st["since"] <= 170
       and st.get("reclock_cmd") == cid, "%s %s" % (M._rot_now() - st["since"], st.get("reclock_cmd")))
    ok("R-1c 다시 잰다는 말을 남긴다(routine)", any("다시 잽니다" in s for s in SAID), str(SAID))
    # 같은 명령으로는 두 번 안 늘린다 — 그 명령에서도 20분이 지나면 멈춘다
    SAID.clear()
    st["since"] = M._rot_now() - 1210
    alive = await step(st, _pcs())
    ok("R-1d 같은 명령(id)으로는 한 번만 — 그 뒤 20분이 지나면 ⛔", not alive and _stopped(), str(SAID))


async def t_own_and_other_commands_ignored():
    reset()
    st = _st(1210)
    await _insert("PC-31", "switch_launcher", 1215)           # 순환이 단계를 열며 보낸 자기 명령(since 보다 앞)
    await _insert("PC-32", "switch_account", 60)              # 다른 PC 의 명령
    await _insert("PC-31", "start", 60)                       # 전환이 아닌 명령
    alive = await step(st, _pcs(base="PC-31"), base="PC-31")
    ok("R-2 자기 명령(단계 시작 전)·다른 PC·전환 아닌 명령으로는 안 늘린다 → 20분에 ⛔(그물 그대로)",
       not alive and _stopped(), str(SAID))


async def t_extend_while_switching():
    reset()
    st = _st(1300)
    alive = await step(st, _pcs("acct_switching", 20, base="PC-33"), base="PC-33")
    ok("R-3 ★old: 새 명령이 없어도 카드가 지금 acct_switching 을 보고 중이면 멈추지 않고 늘린다★",
       alive and not _stopped(), str(SAID))
    ok("R-3b 늘린다는 말은 한 번", sum("기다립니다" in s for s in SAID) == 1, str(SAID))
    await step(st, _pcs("acct_switching", 20, base="PC-33"), base="PC-33")
    ok("R-3c 다음 틱엔 다시 말하지 않는다", sum("기다립니다" in s for s in SAID) == 1, str(SAID))
    st["since"] = M._rot_now() - (M.ROT_SWITCH_HARD + 10)
    alive = await step(st, _pcs("acct_switching", 20, base="PC-33"), base="PC-33")
    ok("R-3d ★진행 중이어도 ROT_SWITCH_HARD(40분)를 넘으면 ⛔ — 무한 대기 없음★", not alive and _stopped(), str(SAID))


async def t_stale_progress_not_trusted():
    reset()
    st = _st(1300)
    alive = await step(st, _pcs("acct_switching", M.ROT_SWITCH_PROGRESS_FRESH + 60, base="PC-34"), base="PC-34")
    ok("R-4 acct_switching 이 박제(5분 넘게 무보고)면 진행 중으로 안 친다 → ⛔", not alive and _stopped(), str(SAID))
    reset()
    st = _st(1300)
    alive = await step(st, _pcs("idle", 900, base="PC-35"), base="PC-35")
    ok("R-4b 새 명령도 진행 보고도 없으면 예전처럼 20분에 ⛔(사고 522: 단계 상한은 유일한 그물)",
       not alive and _stopped(), str(SAID))


async def t_task_branch():
    reset()
    st = _st(1210, task="daily_dungeon", plan=["daily_dungeon"], queue=[], tvisit=["1"])
    await _insert("PC-36b", "switch_account", 100)
    alive = await step(st, _pcs(base="PC-36"), base="PC-36")
    ok("R-5 ★old: 작업 순환의 전환 단계도 새 전환 명령부터 다시 잰다★", alive and not _stopped(), str(SAID))
    reset()
    st = _st(1300, task="daily_dungeon", plan=["daily_dungeon"], queue=[], tvisit=["1"])
    alive = await step(st, _pcs("acct_switching", 20, base="PC-23"), base="PC-23")
    ok("R-5b 작업 순환도 진행 중이면 늘린다", alive and not _stopped(), str(SAID))


async def t_under_limit_no_db():
    reset()
    calls = []
    orig = M.latest_command_after

    async def spy(*a, **k):
        calls.append(a)
        return await orig(*a, **k)
    M.latest_command_after = spy
    try:
        st = _st(600)
        alive = await step(st, _pcs(base="PC-38"), base="PC-38")
    finally:
        M.latest_command_after = orig
    ok("R-6 상한 안(10분)이면 DB 를 안 읽고 그대로 기다린다(틱마다 쿼리 없음)", alive and calls == [], str(calls))


async def t_hunt_like_statuses():
    """재지시 #1 P0 + v4 반증 F1 — 사냥 확인은 hunting·moving·abyss 셋, 회랑·악몽·일일던전·각성전은 «세션 중 대기»."""
    got = {}
    for i, s in enumerate(M.ROT_HUNT_OK):
        reset()
        base = "PC-4%d" % i
        st = {"stage": "starting", "since": M._rot_now() - (M.ROT_START_MAX + 60), "armed_at": M._rot_now() - 9999,
              "day": M._kst_today_key(), "full": False, "hops": 0, "visits": {}, "target": "a"}
        alive = await step(st, [card(base, s, 10)], base=base)
        got[s] = (alive, st.get("stage"))
    ok("H-1 ★old: ▶시작 뒤 7분이 지나도 abyss 카드는 «사냥 확인» — ⛔ 아님★(hunting·moving 도)",
       all(v == (True, "hunting") for v in got.values()), str(got))
    got = {}
    for i, s in enumerate(M.ROT_SESSION):
        reset()
        base = "PC-6%d" % i
        st = {"stage": "starting", "since": M._rot_now() - (M.ROT_START_MAX + 60), "armed_at": M._rot_now() - 9999,
              "day": M._kst_today_key(), "full": False, "hops": 0, "visits": {}, "target": "a"}
        alive = await step(st, [card(base, s, 10)], base=base)
        got[s] = (alive, st.get("stage"), not SENT, any("중입니다" in x for x in SAID))
    ok("H-1b ★v4 반증 F1: 회랑·악몽·일일던전·각성전은 사냥 확인이 아니다 — starting 에 남아 기다린다(⛔·start 없음, 말 한 번)★",
       all(v == (True, "starting", True, True) for v in got.values()), str(got))
    reset()
    st = {"stage": "starting", "since": M._rot_now() - 9000, "armed_at": M._rot_now() - 9999, "day": M._kst_today_key(),
          "full": False, "hops": 0, "visits": {}, "target": "a", "sess_since": M._rot_now() - (M.ROT_SESSION_MAX + 30)}
    alive = await step(st, [card("PC-69", "dungeon", 10)], base="PC-69")
    ok("H-1c 세션 대기도 ROT_SESSION_MAX(90분)를 넘으면 ⛔ — 무한 대기 없음",
       not alive and any("세션 대기 상한" in x for x in SAID), str(SAID))
    reset()
    st = {"stage": "starting", "since": M._rot_now() - (M.ROT_START_MAX + 60), "armed_at": M._rot_now() - 9999,
          "day": M._kst_today_key(), "full": False, "hops": 0, "visits": {}}
    alive = await step(st, [card("PC-49", "idle", 10)], base="PC-49")
    ok("H-2 idle 로 7분이면 여전히 ⛔(그물은 그대로)", not alive and any("사냥이 안 잡힙니다" in x for x in SAID), str(SAID))
    reset()
    st = _st(60)
    alive = await step(st, [card("PC-50", "other_account", 3000), card("PC-50b", "corridor", 10)], base="PC-50")
    ok("H-3 ★v4 반증 F1: 전환 단계에서 목표 계정 카드가 corridor 면 사냥 확인이 아니라 세션 대기(starting)·start 안 보냄★",
       alive and st.get("stage") == "starting" and st.get("sess_since") and not SENT, "%s %s %s" % (st.get("stage"), SENT, SAID))
    reset()
    st = _st(60)
    alive = await step(st, [card("PC-50", "other_account", 3000), card("PC-50b", "abyss", 10)], base="PC-50")
    ok("H-3b 목표 카드가 abyss 면 사냥 확인", alive and st.get("stage") == "hunting", "%s %s" % (st.get("stage"), SAID))
    reset()
    st = _st(60, task="daily_dungeon", plan=["daily_dungeon"], queue=[], tvisit=["1"])
    alive = await step(st, [card("PC-51", "other_account", 3000), card("PC-51b", "abyss", 10)], base="PC-51")
    ok("H-4 작업 순환도 목표 카드가 abyss 면 작업을 보낸다(20분 헛돌지 않는다)",
       alive and st.get("stage") == "tasking" and ("PC-51b", "daily_dungeon") in SENT, "%s %s" % (st.get("stage"), SENT))


async def _run_vt(st, timeline, base, hours):
    """가상 시계로 30초마다 한 걸음(아이온2 v4 반증 하네스 h_hunt_like 와 같은 방식). → (무장 유지, [(초, 단계)], [(초, 명령)])"""
    real = M._rot_now
    t0 = real()
    vt = [0.0]
    M._rot_now = lambda: t0 + vt[0]
    st["since"] = t0 + float(st.get("since", 0))
    stages, sent_at = [], []
    key = M.ns("main", base)
    try:
        while vt[0] <= hours * 3600:
            n0 = len(SENT)
            if not await step(st, timeline(vt[0]), base=base):
                break
            sent_at += [(int(vt[0]), c) for _, c in SENT[n0:]]
            if not stages or stages[-1][1] != st.get("stage"):
                stages.append((int(vt[0]), st.get("stage")))
            vt[0] += 30
    finally:
        M._rot_now = real
    return M._ROT.get(key) is st, stages, sent_at, int(vt[0])


def _sst(stage, ago, **kw):
    d = {"stage": stage, "since": -ago, "armed_at": M._rot_now() - 9999, "day": M._kst_today_key(),
         "target": "b", "full": False, "hops": 1, "visits": {}, "expect_restart": False}
    d.update(kw)
    return d


async def t_session_scenarios():
    """v4 반증 F1·F3 — 아이온2 하네스 장면(h_hunt_like S1·S1b·S2, h_task T1)을 옮겼다. 옛 판(세션=사냥)은 12시간 침묵."""
    reset()
    tl = lambda t: [card("PC-70", "nightmare" if t < 2400 else "nightmare_wait", 5)]
    alive, stages, sent, t_end = await _run_vt(_sst("starting", 60), tl, "PC-70", 12)
    ok("S-1 ★old: 시작 대기 중 사람이 누른 악몽(40분) → 악몽 대기 — 사냥으로 안 넘어가고(12시간 침묵 아님) 악몽 대기도 세션 대기로 "
       "기다리다 첫 세션 목격부터 90분에 ⛔(v4 델타 반증 R9 — 옛 판은 악몽이 끝난 틱에 ⛔)★",
       not alive and M.ROT_SESSION_MAX <= t_end <= M.ROT_SESSION_MAX + 60 and all(sg != "hunting" for _, sg in stages)
       and any("세션 대기 상한" in x for x in SAID), "t=%s %s %s" % (t_end, stages, SAID[-1:]))
    reset()
    tl = lambda t: [card("PC-71", "dungeon" if t < 2400 else "idle", 5)]
    alive, stages, sent, t_end = await _run_vt(_sst("starting", 60), tl, "PC-71", 12)
    starts = [x for x in sent if x[1] == "start"]
    ok("S-1b ★old: 시작 대기 중 일일던전(40분) → idle(미완) — ▶start 를 ★한 번★ 다시 보내고, 그래도 안 잡히면 7분 뒤 ⛔★",
       not alive and len(starts) == 1 and 2400 <= starts[0][0] <= 2460 and t_end <= 2460 + M.ROT_START_MAX + 90,
       "t=%s starts=%s stages=%s" % (t_end, starts, stages))
    said = [x for x in SAID if "중입니다" in x]
    ok("S-1c 세션 대기 안내는 40분(80틱) 동안 ★한 번★만 — 30초마다 알림을 쌓지 않는다", len(said) == 1, str(len(said)))
    reset()
    tl = lambda t: [card("PC-72", "other_account", 3000), card("PC-72b", "dungeon" if t < 900 else "idle", 5)]
    alive, stages, sent, t_end = await _run_vt(_sst("switching", 60), tl, "PC-72", 12)
    starts = [x for x in sent if x[1] == "start"]
    ok("S-2 ★old: 전환 대기 중 목표 카드 일일던전(15분) → idle — 세션 동안 기다렸다가 끝나면 ▶start 한 번, 안 잡히면 ⛔★",
       not alive and len(starts) == 1 and 900 <= starts[0][0] <= 990 and ("starting" in [sg for _, sg in stages]),
       "t=%s starts=%s stages=%s" % (t_end, starts, stages))
    reset()
    tl = lambda t: [card("PC-73", "other_account", 3000), card("PC-73b", "nightmare" if t < 300 else "idle", 5)]
    st = _sst("switching", 60, task="daily_dungeon", plan=["daily_dungeon"], queue=[], tvisit=["1"])
    alive, stages, sent, t_end = await _run_vt(st, tl, "PC-73", 1)
    tasks = [x for x in sent if x[1] == "daily_dungeon"]
    ok("T-1 ★old: 작업 순환 전환 — 목표 카드가 악몽 세션(5분) 중엔 작업을 안 보내고, idle 이 되면 보낸다(버려지지 않는다)★",
       tasks and tasks[0][0] >= 300 and st.get("stage") == "tasking",
       "tasks=%s stages=%s undone=%s" % (tasks, stages, st.get("undone")))
    reset()
    tl = lambda t: [card("PC-74", "other_account", 3000), card("PC-74b", "nightmare" if t < 1800 else "idle", 5)]
    st = _sst("switching", 60, task="daily_dungeon", plan=["daily_dungeon"], queue=[], tvisit=["1"])
    alive, stages, sent, t_end = await _run_vt(st, tl, "PC-74", 1)
    tasks = [x for x in sent if x[1] == "daily_dungeon"]
    ok("T-2 작업 순환 전환 — 목표 카드 세션이 전환 상한(20분)보다 길어도(30분) ⛔ 아니라 기다렸다 작업을 보낸다",
       tasks and tasks[0][0] >= 1800 and not _stopped(), "tasks=%s stages=%s said=%s" % (tasks, stages, SAID[-1:]))


def _seq(pid, spans):
    """spans=[(끝 초, 상태[, 받은 나이])] — t < 끝 인 첫 구간(아이온2 델타 하네스 d1_sess.seq 와 같은 모양)."""
    def tl(t):
        for sp in spans:
            if t < sp[0]:
                break
        ago = sp[2] if len(sp) > 2 else 5
        return [card(pid, "other_account", 3000), card(pid + "b", sp[1], ago)]
    return tl


async def t_delta_scenarios():
    """v4 델타 반증(아이온2 d1_sess) — 세션 대기 중 한 틱 딴 상태·악몽 대기·idle 한 틱·오프라인 문구·작업 전환."""
    # R5·R6·R6b: 세션 도중 captcha·reconnecting·paused 한 번 → ⛔ 없이 세션 끝(1800)까지 기다리고, idle 2틱 뒤 start 한 번
    for name, pid, blip in (("R5 captcha", "PC-90", [(900, "dungeon"), (960, "captcha")]),
                            ("R6 reconnecting", "PC-91", [(720, "nightmare"), (810, "reconnecting")]),
                            ("R6b paused", "PC-92", [(600, "corridor"), (630, "paused")])):
        reset()
        sess = blip[0][1]
        alive, stages, sent, t_end = await _run_vt(_sst("switching", 60), _seq(pid, blip + [(1800, sess), (99999, "idle")]), pid, 1)
        starts = [x for x in sent if x[1] == "start"]
        ok("D-%s ★old: 세션 30분 중 %s 한 틱 — 7분 넘었다고 ⛔ 하지 않고 세션이 끝난 뒤 idle 2틱에 ▶start 한 번★" % (name, blip[1][1]),
           t_end >= 1800 + M.ROT_START_MAX and len(starts) == 1 and 1800 < starts[0][0] <= 1860,
           "t=%s starts=%s said=%s" % (t_end, starts, SAID[-1:]))
    # R9: 악몽 20분 → 악몽 대기 10분 → idle — 악몽 대기에서 ⛔ 하지 않는다
    reset()
    alive, stages, sent, t_end = await _run_vt(_sst("switching", 60),
                                               _seq("PC-93", [(1200, "nightmare"), (1800, "nightmare_wait"), (99999, "idle")]), "PC-93", 1)
    starts = [x for x in sent if x[1] == "start"]
    ok("D-R9 ★old: 악몽 → 악몽 최종보스 대기(사람) 10분 → idle — 대기 중 ⛔·start 없음, idle 2틱 뒤 start 한 번★",
       t_end >= 1800 + M.ROT_START_MAX and len(starts) == 1 and 1800 < starts[0][0] <= 1860, "t=%s starts=%s" % (t_end, starts))
    # R10: 세션 40분 중 20분에 idle 한 틱 — 한 번뿐인 재시작을 거기서 안 쓴다
    reset()
    alive, stages, sent, t_end = await _run_vt(_sst("switching", 60),
                                               _seq("PC-94", [(1200, "dungeon"), (1230, "idle"), (2400, "dungeon"), (99999, "idle")]),
                                               "PC-94", 1.5)
    starts = [x for x in sent if x[1] == "start"]
    ok("D-R10 ★old: 세션 도중 idle 한 틱은 재시작을 안 쓴다 — 세션이 진짜 끝난 뒤(2400+) start 한 번★",
       len(starts) == 1 and 2400 < starts[0][0] <= 2460, "starts=%s" % starts)
    reset()
    alive, stages, sent, t_end = await _run_vt(_sst("switching", 60),
                                               _seq("PC-89", [(1200, "dungeon"), (1230, "idle"), (1260, "captcha"), (1290, "idle"),
                                                              (2400, "dungeon"), (99999, "idle")]), "PC-89", 1.5)
    starts = [x for x in sent if x[1] == "start"]
    ok("D-R10b idle 은 ★연속★ 2틱이어야 한다 — idle·captcha·idle 로는 재시작을 안 쓴다", len(starts) == 1 and 2400 < starts[0][0] <= 2460,
       "starts=%s" % starts)
    # R1: start 가 거부돼 세션이 다시 돌면 — ⛔ 문구가 «▶시작 뒤» 가 아니라 실제 상태
    reset()
    alive, stages, sent, t_end = await _run_vt(_sst("switching", 60),
                                               _seq("PC-95", [(900, "dungeon"), (960, "idle"), (2000, "dungeon"), (99999, "idle")]),
                                               "PC-95", 1.5)
    stop = [x for x in SAID if x.startswith("⛔")]
    ok("D-R1 ★old: 세션 대기 뒤 ⛔ 문구는 «세션 대기가 끝나고 N분째 … ▶시작 한 번 다시 보냄» — «▶시작 뒤 18분» 이 아니다★",
       not alive and stop and "세션 대기가 끝나고" in stop[-1] and "▶시작 한 번 다시 보냄" in stop[-1] and "▶시작 뒤" not in stop[-1]
       and t_end >= 2000 + M.ROT_START_MAX, "t=%s %s" % (t_end, stop))
    # R3·R3b: 세션 뒤 오프라인·보고 끊김 → ⛔ 는 마지막 세션 틱부터 7분, 문구는 오프라인
    for name, pid, tail in (("R3 offline", "PC-96", (99999, "offline")), ("R3b 보고 끊김", "PC-97", (99999, "dungeon", 2000))):
        reset()
        alive, stages, sent, t_end = await _run_vt(_sst("switching", 60), _seq(pid, [(1200, "dungeon"), tail]), pid, 1)
        stop = [x for x in SAID if x.startswith("⛔")]
        ok("D-%s ★old: 세션 20분 뒤 %s — 곧바로가 아니라 7분 뒤 ⛔, 문구에 오프라인(«▶시작 뒤 20분째 사냥» 아님)★" % (name, tail[1]),
           not alive and 1200 + M.ROT_START_MAX <= t_end <= 1200 + M.ROT_START_MAX + 90 and stop and "오프라인" in stop[-1]
           and "▶시작 뒤" not in stop[-1], "t=%s %s" % (t_end, stop))
    # T7·T9: 작업 순환 전환 — 세션 25분 뒤 captcha 한 틱 / 악몽 대기
    reset()
    st = _sst("switching", 60, task="daily_dungeon", plan=["daily_dungeon"], queue=[], tvisit=["1"])
    alive, stages, sent, t_end = await _run_vt(st, _seq("PC-98", [(1500, "nightmare"), (1560, "captcha"), (1800, "nightmare"),
                                                                  (99999, "idle")]), "PC-98", 1)
    tasks = [x for x in sent if x[1] == "daily_dungeon"]
    ok("D-T7 ★old: 작업 전환 — 세션 25분 뒤 captcha 한 틱에 «전환 26분째» ⛔ 하지 않고, 세션이 끝나면 작업을 보낸다★",
       tasks and tasks[0][0] >= 1800 and not any(x.startswith("⛔") for x in SAID), "tasks=%s said=%s" % (tasks, SAID[-1:]))
    reset()
    st = _sst("switching", 60, task="daily_dungeon", plan=["daily_dungeon"], queue=[], tvisit=["1"])
    alive, stages, sent, t_end = await _run_vt(st, _seq("PC-99", [(1500, "nightmare"), (99999, "nightmare_wait")]), "PC-99", 1)
    ok("D-T9 ★old: 작업 전환 — 목표 카드가 악몽 대기면 20분 그물로 ⛔ 하지 않고 세션 대기(작업은 안 보낸다)★",
       alive and not any(x.startswith("⛔") for x in SAID) and not [x for x in sent if x[1] == "daily_dungeon"],
       "t=%s said=%s" % (t_end, SAID[-1:]))
    reset()
    st = _sst("switching", 60, task="daily_dungeon", plan=["daily_dungeon"], queue=[], tvisit=["1"])
    alive, stages, sent, t_end = await _run_vt(st, _seq("PC-88", [(600, "nightmare"), (99999, "captcha")]), "PC-88", 1)
    stop = [x for x in SAID if x.startswith("⛔")]
    ok("D-T10 작업 전환 — 세션 뒤 계속 captcha 면 마지막 세션 틱부터 20분에 ⛔, 문구는 «세션 대기가 끝나고 N분째 작업을 못 보냈습니다»",
       not alive and 600 + M.ROT_SWITCH_MAX <= t_end <= 600 + M.ROT_SWITCH_MAX + 60 and stop and "세션 대기가 끝나고" in stop[-1]
       and "전환이" not in stop[-1], "t=%s %s" % (t_end, stop))


async def t_active_and_reject():
    """v4 반증 F2(_rot_active 신선도) · F3(거부 문구 셋)."""
    old = {s: bool(M._rot_active([card("PC-80", s, 2000)])) for s in M.ROT_HUNT_LIKE}
    new = {s: bool(M._rot_active([card("PC-80", s, 10)])) for s in M.ROT_HUNT_LIKE}
    ok("F2 ★old: abyss·corridor·nightmare·dungeon·awakening 카드가 2000초 무보고면 박제 — 살아 있는 카드로 안 친다★",
       not any(old.values()) and all(new.values()), "stale=%s fresh=%s" % (old, new))
    forms = ["[원격명령] 악몽 진행 중 → 'daily_dungeon' 거부",
             "[텔레그램] 중계 전송: 악몽 진행 중이라 'daily_dungeon' 명령을 거부했습니다 (끝나고 다시 보내주세요)",
             "⚠️ 이전 명령(start)이 진행 중이라 'daily_dungeon'를 거부했습니다"]
    ok("F3-a ★old: 매크로가 실제로 남기는 거부 문구 셋을 다 잡는다(«→ 'X' 거부»·«'X' 명령을 거부»·«'X'를 거부»)★",
       all(M._rot_rejected(f, "daily_dungeon") for f in forms), str([M._rot_rejected(f, "daily_dungeon") for f in forms]))
    ok("F3-b 다른 명령의 거부·거부 아닌 줄은 안 잡는다",
       not M._rot_rejected("[원격명령] 악몽 진행 중 → 'start' 거부", "daily_dungeon")
       and not M._rot_rejected("'daily_dungeon' 시작", "daily_dungeon"), "")
    # 실제 작업 순환 재전송 경로(로그 줄 «→ 'X' 거부»)
    reset()
    now = M._rot_now()
    st = {"stage": "tasking", "task": "daily_dungeon", "since": now - (M.ROT_TASK_GRACE + 30),
          "sent_at": now - (M.ROT_TASK_GRACE + 30), "armed_at": now - 9999, "busy": False, "other": "", "retask": False,
          "day": M._kst_today_key(), "tvisit": ["1"], "queue": [], "plan": ["daily_dungeon"], "hops": 0, "visits": {}}
    orig = M.get_logs

    async def logs(pc, limit=1000):
        return [{"level": "info", "message": "[원격명령] 악몽 진행 중 → 'daily_dungeon' 거부", "created_at": _iso(5)}]
    M.get_logs = logs
    try:
        alive = await step(st, [card("PC-81", "idle", 5)], base="PC-81")
    finally:
        M.get_logs = orig
    ok("F3-c ★old: 로그 «→ 'daily_dungeon' 거부» 를 보고 작업을 한 번 다시 보낸다(버리지 않는다)★",
       alive and st.get("retask") and ("PC-81", "daily_dungeon") in SENT, "%s %s" % (SENT, SAID))
    # 정보수집 재전송 경로도 같은 문구 판정(로그 줄 «→ 'collect_info' 거부»)
    reset()
    now = M._rot_now()
    st = {"stage": "collecting", "since": now - 60, "armed_at": now - 9999, "day": M._kst_today_key(),
          "full": False, "hops": 0, "visits": {}, "char_before": "x", "recollect": False, "collect_pc": "PC-82"}

    async def logs2(pc, limit=1000):
        return [{"level": "info", "message": "[원격명령] 일일던전 진행 중 → 'collect_info' 거부", "created_at": _iso(5)}]
    M.get_logs = logs2
    try:
        alive = await step(st, [card("PC-82", "collecting", 5, _char_collected_at="x")], base="PC-82")
    finally:
        M.get_logs = orig
    ok("F3-d ★old: 로그 «→ 'collect_info' 거부» 를 보고 정보수집을 한 번 다시 보낸다★",
       alive and st.get("recollect") and ("PC-82", "collect_info") in SENT, "%s %s" % (SENT, SAID))


async def t_v4_breaker():
    """v4 반증 에이전트(2026-09-24)가 재현한 것 — BRK-1 매크로 시계 · BRK-3 버린 명령 · BRK-2 hop 넘어 남는 표시 · 죽은 순환에 말."""
    # BRK-1: 서버는 5초 전에 acct_switching 을 받았는데 PC 시계가 늦어 last_active 는 305초 전
    reset()
    st = _st(1300)
    alive = await step(st, [card("PC-52", "acct_switching", M.ROT_SWITCH_PROGRESS_FRESH + 5, recv=5),
                            card("PC-52b", "other_account", 3000)], base="PC-52")
    ok("R-7 ★old: 진행 신선도는 서버가 받은 시각으로 — PC 시계가 305초 늦어도 5초 전에 받았으면 늘린다(BRK-1)★",
       alive and not _stopped(), str(SAID))
    reset()
    st = _st(1300)
    alive = await step(st, [card("PC-53", "acct_switching", 5, recv=M.ROT_SWITCH_PROGRESS_FRESH + 100),
                            card("PC-53b", "other_account", 3000)], base="PC-53")
    ok("R-7b ★old: 시계가 앞선 PC — last_active 는 5초 전이어도 서버가 400초째 못 받았으면 박제 → ⛔★",
       not alive and _stopped(), str(SAID))
    reset()
    st = _st(1300)
    alive = await step(st, [card("PC-54", "acct_switching", 5, recv=False),
                            card("PC-54b", "other_account", 3000)], base="PC-54")
    ok("R-7c 받은 시각을 모르면 신선으로 안 친다(모름≠신선) → ⛔", not alive and _stopped(), str(SAID))
    # BRK-3: 매크로가 거부한 명령·배달 안 된 명령
    for i, stt in enumerate(("cancelled", "expired")):
        reset()
        st = _st(1210)
        await _insert("PC-5%db" % (5 + i), "switch_account", 150, status=stt)
        alive = await step(st, _pcs(base="PC-5%d" % (5 + i)), base="PC-5%d" % (5 + i))
        ok("R-8%s ★old: %s 명령(아무 전환도 안 시작함)으로는 다시 재지 않는다 → ⛔(BRK-3)★" % ("" if i == 0 else "b", stt),
           not alive and _stopped() and st.get("reclock_cmd") is None, "%s %s" % (st.get("reclock_cmd"), SAID))
    reset()
    st = _st(1210)
    await _insert("PC-57b", "switch_account", 150, status="acked")
    alive = await step(st, _pcs(base="PC-57"), base="PC-57")
    ok("R-8c 매크로가 받아 처리한(acked) 명령은 센다", alive and not _stopped(), str(SAID))
    # 죽은 순환에 «다시 잽니다» 를 말하지 않는다 — 조회 await 사이에 해제
    reset()
    st = _st(1210)
    await _insert("PC-58b", "switch_account", 150)
    orig = M.latest_command_after

    async def racing(*a, **k):
        M._ROT.pop(M.ns("main", "PC-58"), None)          # 그 사이 주인님이 순환을 껐다
        return await orig(*a, **k)
    M.latest_command_after = racing
    try:
        await step(st, _pcs(base="PC-58"), base="PC-58")
    finally:
        M.latest_command_after = orig
    ok("R-9 ★old: 조회 사이에 해제된 순환에는 «다시 잽니다» 를 안 보낸다★", not any("다시 잽니다" in x for x in SAID), str(SAID))
    # BRK-2: 새 전환 단계에 들어가면 지난 hop 의 연장 표시·재측정 id 를 지운다(두 곳)
    reset()
    now = M._rot_now()
    st = {"stage": "collecting", "since": now - 10, "armed_at": now - 9999, "day": M._kst_today_key(),
          "full": False, "hops": 1, "visits": {}, "char_before": "old", "collect_pc": "PC-59",
          "switch_ext": True, "reclock_cmd": 424242, "sess_since": 123.0, "sess_restarted": True, "sess_said": True}
    alive = await step(st, [card("PC-59", "idle", 10, _char_collected_at="new",
                                 daily_progress=[{"slot": 1, "completed": True, "completed_time": _kst_now()}]),
                            card("PC-59b", "other_account", 5000)], base="PC-59")
    ok("R-10 ★old: 완주 순환이 다음 계정 전환 단계에 들어가면 switch_ext·reclock_cmd·세션 표시를 지운다(BRK-2·F1)★",
       st.get("stage") == "switching" and not st.get("switch_ext") and st.get("reclock_cmd") is None
       and not st.get("sess_since") and not st.get("sess_restarted") and not st.get("sess_said"),
       "%s ext=%s rc=%s sent=%s said=%s" % (st.get("stage"), st.get("switch_ext"), st.get("reclock_cmd"), SENT, SAID[-2:]))
    reset()
    st = {"stage": "tasking", "task": "corridor", "since": now - 999, "sent_at": now - 999,
          "armed_at": now - 9999, "busy": True, "other": "", "retask": False, "day": M._kst_today_key(),
          "tvisit": ["1"], "queue": [], "plan": ["corridor"], "hops": 1, "visits": {},
          "switch_ext": True, "reclock_cmd": 424242, "sess_since": 123.0, "sess_restarted": True, "sess_said": True}
    alive = await step(st, [card("PC-60", "idle", 5)], base="PC-60")
    ok("R-10b ★old: 작업 순환도 같다★",
       st.get("stage") == "switching" and not st.get("switch_ext") and st.get("reclock_cmd") is None
       and not st.get("sess_since") and not st.get("sess_restarted") and not st.get("sess_said"),
       "%s ext=%s rc=%s sent=%s said=%s" % (st.get("stage"), st.get("switch_ext"), st.get("reclock_cmd"), SENT, SAID[-2:]))


def _kst_now():
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")


def test_all():
    for k, v in _PATCH.items():
        _ORIG[k] = getattr(M, k)
        setattr(M, k, v)
    try:
        run_all([t_reclock_on_new_command, t_own_and_other_commands_ignored, t_extend_while_switching,
                 t_stale_progress_not_trusted, t_task_branch, t_under_limit_no_db, t_hunt_like_statuses,
                 t_v4_breaker, t_session_scenarios, t_delta_scenarios, t_active_and_reject])
    finally:
        for k, v in _ORIG.items():
            setattr(M, k, v)
    finish("test_rot_switch_clock", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
