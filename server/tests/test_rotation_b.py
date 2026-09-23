# -*- coding: utf-8 -*-
"""[대시보드] 계정 순환 엔진 — 반증 B-ROT1~8 (2026-09-23) ★실제 _rot_step_pc/_rot_arm 호출★.

반증 에이전트 프로브(형제 카드 증거·은퇴 계정·status 뒤집힘·거짓 ✅·옛 로그 줄·await 경합·
브로드캐스트 무장)를 시험으로 옮겼다. 각 검사는 ★고치기 전 main.py 에서 빨간불★ 임을 확인했다.
B-ROT5(05:00 경계)·PageUp full=True 는 주인님 결정 대기라 여기서 다루지 않는다.

  python -X utf8 tests/test_rotation_b.py      (server/ 에서)
"""
import json
from datetime import datetime, timedelta, timezone

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 21     # 실측 22검사

M = main
SENT, SAID, LOGS = [], [], {"lines": [], "hook": None}


async def _f_send(t, pc, cmd, args=None):
    SENT.append((pc, cmd, dict(args or {})))
    return True


async def _f_say(t, pc, text, routine=False):
    SAID.append(text)


async def _f_save(force=False):
    pass


async def _f_logs(pc, limit=1000):
    if LOGS.get("hook"):
        await LOGS["hook"]()
    return list(LOGS["lines"])[-limit:]


async def _f_pm(t):
    return {"20": "peer"}


async def _f_allow(t="main"):
    return {"*"}


async def _f_nm(t):
    return {}


_PATCH = {"_rot_send": _f_send, "_rot_say": _f_say, "_rot_save": _f_save, "get_logs": _f_logs,
          "_get_parsec_map": _f_pm, "_rot_allow": _f_allow, "_rot_nm_clears": _f_nm}
_ORIG = {}


def _patch():
    for k, v in _PATCH.items():
        _ORIG[k] = getattr(M, k)
        setattr(M, k, v)


def _unpatch():
    for k, v in _ORIG.items():
        setattr(M, k, v)


def _iso(ago_s):
    return (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).strftime("%Y-%m-%dT%H:%M:%S")


def _log(msg, ago_s):
    return {"level": "info", "message": msg, "created_at": _iso(ago_s)}


def _today():
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")


DP_TODAY = [{"slot": 1, "completed": True, "completed_time": _today()}]
IDS = {"1": "a@x", "2": "b@x", "3": "c@x"}
NAMES = {"1": ["A"], "2": ["B"], "3": ["C"]}
KEY = M.ns("main", "PC-20")


def card(pid, status, ago, **kw):
    d = {"pc_id": pid, "status": status, "last_active": _iso(ago), "acct_ids": IDS, "acct_names": NAMES}
    d.update(kw)
    return d


def reset():
    SENT.clear(); SAID.clear(); LOGS.update(lines=[], hook=None); M._ROT.clear()


async def step(st, pcs, n=1):
    """엔진과 같이 _ROT_STEP_CUR 를 걸고 한 걸음. 예외는 삼켜 문자열로 돌려준다(기존 판에서도 검사가 다 돈다)."""
    M._ROT[KEY] = st
    err = ""
    for _ in range(n):
        M._ROT_STEP_CUR.update(key=KEY, st=st)
        try:
            await M._rot_step_pc("main", "PC-20", st, pcs)
        except Exception as e:                       # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, e)
        finally:
            M._ROT_STEP_CUR.update(key=None, st=None)
    return err


def _collecting(**kw):
    now = M._rot_now()
    st = {"stage": "collecting", "since": now - 60, "armed_at": now - 9999, "day": M._kst_today_key(),
          "full": False, "hops": 0, "visits": {}}
    st.update(kw)
    return st


# ─────────────────────────────────────────────────────────────── B-ROT1
async def t_rot1():
    reset()
    now = M._rot_now()
    # 수집 카드는 5분+ 무보고(active=None), 형제 카드엔 ★며칠 전★ 수집값 — 3시간째
    st = _collecting(since=now - 3 * 3600, char_before="2026-09-22 10:00:00", tvisit=["1"])
    pcs = [card("PC-20", "collecting", 900, _char_collected_at="2026-09-22 10:00:00", daily_progress=DP_TODAY),
           card("PC-20b", "other_account", 99999, _char_collected_at="2026-09-19 08:00:00", daily_progress=[])]
    err = await step(st, pcs)
    ok("B-ROT1-a 형제 카드의 옛 수집값을 증거로 안 읽는다 — 예외 없이", not err, err)
    ok("B-ROT1-b 수집 중 무응답 상한이 울려 ⛔ 로 세운다", KEY not in M._ROT and any("응답이 없습니다" in s for s in SAID),
       str(SAID)[:120])

    # 수집 증거는 새로 왔는데 active=None(아직 idle 보고 전) → 다음 계정 고르기로 내려가지 않는다
    reset()
    st = _collecting(since=now - 120, char_before="c0", collect_pc="PC-20")
    pcs = [card("PC-20", "collecting", 900, _char_collected_at="c1", daily_progress=DP_TODAY),
           card("PC-20b", "other_account", 99999, _char_collected_at="old", daily_progress=[])]
    err = await step(st, pcs)
    ok("B-ROT1-c 증거는 있고 active=None 이면 기다린다(예외·송신 없음, 무장 유지)",
       not err and not SENT and M._ROT.get(KEY) is st, "%s sent=%s" % (err, SENT))

    # skip_collect 경로(char_before="") + active=None + 형제 증거 → 예외 없이 기다린다
    reset()
    st = _collecting(since=now - 30, char_before="", skip_collect=True, collect_pc="PC-20b", target="b")
    pcs = [card("PC-20", "other_account", 99999, _char_collected_at="x", daily_progress=DP_TODAY),
           card("PC-20b", "offline", 99999, daily_progress=[])]
    err = await step(st, pcs)
    ok("B-ROT1-d skip_collect + active=None 에서도 AttributeError 가 안 난다", not err and not SENT, err)

    # hunting 에서 수집을 보낼 때 ★어느 카드★ 인지 남긴다
    reset()
    st = {"stage": "hunting", "since": now - 600, "armed_at": now - 9999, "day": M._kst_today_key(),
          "full": False, "hops": 0, "visits": {}}
    pcs = [card("PC-20", "idle", 5, _char_collected_at="c0", daily_progress=DP_TODAY)]
    err = await step(st, pcs)
    ok("B-ROT1-e 수집 명령과 함께 collect_pc 를 기록한다",
       not err and st.get("stage") == "collecting" and st.get("collect_pc") == "PC-20", str(st.get("collect_pc")))


# ─────────────────────────────────────────────────────────────── B-ROT2
async def t_rot2():
    now = M._rot_now()
    for mode in ("retired", "no_account_set"):
        reset()
        M.RETIRED_PCS.discard(M.ns("main", "PC-20c")); M.NO_ACCOUNT_PCS.discard(M.ns("main", "PC-20c"))
        (M.RETIRED_PCS if mode == "retired" else M.NO_ACCOUNT_PCS).add(M.ns("main", "PC-20c"))
        st = _collecting(since=now - 10, char_before="old", collect_pc="PC-20")
        pcs = [card("PC-20", "idle", 10, _char_collected_at="new", daily_progress=DP_TODAY),
               card("PC-20b", "other_account", 5000, daily_progress=DP_TODAY)]
        try:
            err = await step(st, pcs)
        finally:
            M.RETIRED_PCS.discard(M.ns("main", "PC-20c")); M.NO_ACCOUNT_PCS.discard(M.ns("main", "PC-20c"))
        ok("B-ROT2-%s 계정3(%s)으로 전환하지 않는다" % ("a" if mode == "retired" else "b", mode),
           not err and not any(c == "switch_launcher" for _, c, _a in SENT), "%s sent=%s" % (err, SENT))
    # 작업 순환(_rot_next_acct_task)도 같은 규칙
    reset()
    M.RETIRED_PCS.add(M.ns("main", "PC-20b"))
    try:
        st = {"stage": "tasking", "task": "corridor", "since": now - 999, "sent_at": now - 999,
              "armed_at": now - 9999, "busy": True, "other": "", "retask": False, "day": M._kst_today_key(),
              "tvisit": ["1"], "queue": [], "plan": ["corridor"], "hops": 0, "visits": {}}
        pcs = [card("PC-20", "idle", 5, daily_progress=DP_TODAY)]
        err = await step(st, pcs)
    finally:
        M.RETIRED_PCS.discard(M.ns("main", "PC-20b"))
    tgt = [a.get("acct_no") for _, c, a in SENT if c == "switch_launcher"]
    ok("B-ROT2-c 작업 순환도 은퇴 계정2 를 건너뛰고 계정3 으로 간다", not err and tgt == [3], "%s %s" % (err, tgt))


# ─────────────────────────────────────────────────────────────── B-ROT3
async def t_rot3():
    reset()
    real_now = M._rot_now
    clock = {"t": real_now()}
    M._rot_now = lambda: clock["t"]
    try:
        t0 = clock["t"]
        st = {"stage": "tasking", "task": "daily_dungeon", "since": t0, "sent_at": t0, "armed_at": t0,
              "busy": False, "other": "", "retask": False, "day": M._kst_today_key(), "tvisit": ["1"],
              "queue": [], "plan": ["daily_dungeon"], "hops": 0, "visits": {}}
        moved = False
        err = ""
        for i in range(int(40 * 60 / 30)):              # 40분, 사냥↔이동이 2분마다 뒤집힌다
            s = "hunting" if (i // 4) % 2 == 0 else "moving"
            pcs = [card("PC-20", s, 5, daily_progress=DP_TODAY), card("PC-20b", "other_account", 5000)]
            err = await step(st, pcs) or err
            clock["t"] += 30
            if any(c == "switch_launcher" for _, c, _a in SENT):
                moved = True
                break
        ok("B-ROT3-a status 가 뒤집혀도 ROT_OTHER_MAX(20분) 뒤 다음 계정으로 넘어간다", moved and not err,
           "%s stage=%s" % (err, st.get("stage")))
        ok("B-ROT3-b 넘어간 사유가 「못 했다」로 남는다", any("못 했습니다" in s for s in SAID), str(SAID)[:100])
    finally:
        M._rot_now = real_now


# ─────────────────────────────────────────────────────────────── B-ROT4
async def t_rot4():
    reset()
    now = M._rot_now()
    st = {"stage": "tasking", "task": "corridor", "since": now - 999, "sent_at": now - 999, "armed_at": now - 9999,
          "busy": False, "other": "", "retask": True, "day": M._kst_today_key(), "tvisit": ["1", "2", "3"],
          "queue": [], "plan": ["corridor"], "hops": 2, "visits": {}}
    pcs = [card("PC-20", "idle", 5, daily_progress=DP_TODAY)]
    err = await step(st, pcs)
    fin = SAID[-1] if SAID else ""
    ok("B-ROT4-a 한 번도 안 바빠진 작업이 있으면 끝 알림이 ✅ 가 아니다",
       not err and KEY not in M._ROT and fin.startswith("⚠") and "✅" not in fin, fin[:80])
    ok("B-ROT4-b 못 한 계정·작업을 끝 알림에 적는다", "계정1" in fin and "회랑" in fin, fin[:120])
    # 대조: 전부 바빠졌다 끝났으면 ✅ 그대로
    reset()
    st = dict(st, busy=True, retask=False)
    st.pop("undone", None)
    err = await step(st, pcs)
    ok("B-ROT4-c 대조 — 다 한 순환은 여전히 ✅", not err and SAID and SAID[-1].startswith("✅"), str(SAID)[:80])


# ─────────────────────────────────────────────────────────────── B-ROT6
async def t_rot6():
    reset()
    now = M._rot_now()
    st = _collecting(since=now - 60, char_before="x", collect_pc="PC-20")
    pcs = [card("PC-20", "collecting", 5, _char_collected_at="x", daily_progress=DP_TODAY)]
    LOGS["lines"] = [_log("[웹플레이] 아직 호스트 없음", 600)] * 3 + [_log("filler", 5)] * 5
    err = await step(st, pcs)
    ok("B-ROT6-a 수집 전에 찍힌 옛 무호스트 줄로 ⛔ 하지 않는다", not err and M._ROT.get(KEY) is st, str(SAID)[:80])
    reset()
    st = _collecting(since=now - 60, char_before="x", collect_pc="PC-20")
    LOGS["lines"] = [_log("[웹플레이] 아직 호스트 없음", 30)] * 3
    await step(st, pcs)
    ok("B-ROT6-b 대조 — 수집 뒤의 무호스트 3줄이면 여전히 ⛔", KEY not in M._ROT and any("nohost" in s for s in SAID), str(SAID)[:120])  # 문구는 B-TG6 관측형(nohost)
    # 작업 재전송 — 보낸 뒤가 아니라 ★전에★ 찍힌 거부 줄
    reset()
    st = {"stage": "tasking", "task": "corridor", "since": now - 400, "sent_at": now - 400,
          "armed_at": now - 9999, "busy": False, "other": "", "retask": False, "day": M._kst_today_key(),
          "tvisit": ["1", "2", "3"], "queue": [], "plan": ["corridor"], "hops": 2, "visits": {}}
    LOGS["lines"] = [_log("'corridor'를 거부 (이전 명령 처리 중)", 3000)]
    await step(st, [card("PC-20", "idle", 5, daily_progress=DP_TODAY)])
    ok("B-ROT6-c 옛 거부 줄로 작업을 재전송하지 않는다", not any(c == "corridor" for _, c, _a in SENT), str(SENT))
    reset()
    st = {"stage": "tasking", "task": "corridor", "since": now - 400, "sent_at": now - 400,
          "armed_at": now - 9999, "busy": False, "other": "", "retask": False, "day": M._kst_today_key(),
          "tvisit": ["1", "2", "3"], "queue": [], "plan": ["corridor"], "hops": 2, "visits": {}}
    LOGS["lines"] = [_log("'corridor'를 거부 (이전 명령 처리 중)", 100)]
    await step(st, [card("PC-20", "idle", 5, daily_progress=DP_TODAY)])
    ok("B-ROT6-d 대조 — 보낸 뒤의 거부 줄이면 재전송한다", any(c == "corridor" for _, c, _a in SENT), str(SENT))


# ─────────────────────────────────────────────────────────────── B-ROT7
async def t_rot7():
    reset()
    now = M._rot_now()
    st = _collecting(since=now - 60, char_before="x", recollect=False, collect_pc="PC-20")
    pcs = [card("PC-20", "collecting", 5, _char_collected_at="x", daily_progress=DP_TODAY)]
    LOGS["lines"] = [_log("'collect_info'를 거부 (이전 명령 처리 중)", 10)]

    async def owner_stops():
        M._rot_disarm("main", "PC-20", "수동 해제")
    LOGS["hook"] = owner_stops
    err = await step(st, pcs)
    ok("B-ROT7 로그를 읽는 사이 정지되면 collect_info 를 다시 보내지 않는다",
       not err and not SENT and KEY not in M._ROT, "%s sent=%s" % (err, SENT))


# ─────────────────────────────────────────────────────────────── B-ROT8
async def t_rot8():
    reset()
    sess = main.new_session("main")

    async def _rotate(pc):
        r = await M.rotate_set(pc, Req({"on": True}, api_key=None, session=sess))
        return json.loads(bytes(r.body))
    r_all = await M._rot_arm("main", "all")
    ok("B-ROT8-a _rot_arm('all') 은 무장하지 않는다", r_all[0] is False and "all" not in M._ROT, str(r_all))
    j = await _rotate("all")
    ok("B-ROT8-b POST /rotate/all 도 무장하지 않는다", j.get("on") is False and "all" not in M._ROT, str(j))
    j = await _rotate("PC-NOCARD")
    ok("B-ROT8-c 카드가 없는 id 는 /rotate 로 무장되지 않는다",
       j.get("on") is False and M.ns("main", "PC-NOCARD") not in M._ROT, str(j))
    await db.upsert_status("PC-ROT8b", {"status": "other_account"})
    j = await _rotate("PC-ROT8")
    ok("B-ROT8-d 대조 — 계정 카드(PC-ROT8b)만 있어도 본 PC 는 무장된다", j.get("on") is True, str(j))
    M._ROT.pop(M.ns("main", "PC-ROT8"), None)
    M._ROT_BOOT.pop(M.ns("main", "PC-ROT8"), None)
    M._ROT.pop(M.ns("main", "PC-NOCARD"), None)
    M._ROT.pop("all", None)


async def t_all():
    _patch()
    try:
        for t in (t_rot1, t_rot2, t_rot3, t_rot4, t_rot6, t_rot7, t_rot8):
            await t()
    finally:
        _unpatch()
        M._ROT.clear()


def test_all():
    run_all([t_all])
    finish("test_rotation_b", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
