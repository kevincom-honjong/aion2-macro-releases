# -*- coding: utf-8 -*-
"""[대시보드] 사고 667 (2026-09-29 PC-09) — 「본인 확인 필요」 계정은 사람 몫: 서버 알람 두 통을 조용히.

  매크로(lc loot._cmd_idverify)는 [본인 확인] 을 본 계정으로 오는 ★자동★ 명령을
  ack {"status":"cancelled","why":"계정 b 본인 확인 필요(사고 667) — …"} 로 접고 알람은 스스로 한 번 낸다.
  서버는 몰라서 (a) «[순환] ⛔ 순환 정지 — ▶시작 뒤 7분째 사냥이 안 잡힙니다» (b) «🔭 스카우터: PC-09 — 버그스샷 …» 을 또 냈다.
  → 표식 ack = 그 물리 PC 보류: 순환은 routine 한 줄로 멈춤 · 스카우터 제외 · 사람 ▶시작/전환이 푼다 · 재배포해도 남는다.

    cd updater/server && python -X utf8 tests/test_idverify_667.py
"""
import json
from datetime import datetime, timedelta, timezone

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402
import scout_alarm as S

MIN_CHECKS = 16
M = main
SAID = []          # (text, routine)
TAG_WHY = "계정 b 본인 확인 필요(사고 667) — 런처 [본인 확인] 안내 · 자동 start 접음"


async def _f_say(t, pc, text, routine=False):
    SAID.append((text, routine))


async def _f_save(force=False):
    pass


async def _f_logs(pc, limit=1000):
    return []


_PATCH = {"_rot_say": _f_say, "_rot_save": _f_save}
_ORIG = {}


def _iso(ago_s):
    return (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).strftime("%Y-%m-%dT%H:%M:%S")


def card(pid, status, ago=5):
    return {"pc_id": pid, "status": status, "last_active": _iso(ago), "_updated_at": _iso(ago),
            "acct_ids": {"1": "a@x", "2": "b@x"}, "acct_names": {"1": ["A"], "2": ["B"]}}


def _starting(ago):
    now = M._rot_now()
    return {"stage": "starting", "since": now - ago, "armed_at": now - 9999, "day": M._kst_today_key(),
            "target": "b", "full": False, "hops": 1, "visits": {}, "expect_restart": False}


async def reset():
    SAID.clear()
    M._ROT.clear()
    M._IDV_HOLD.clear()
    M._IDV_LOADED[0] = True
    await db.set_setting("idverify_hold", "{}")


async def step(st, pcs, base="PC-09"):
    key = M.ns("main", base)
    M._ROT[key] = st
    M._ROT_STEP_CUR.update(key=key, st=st)
    try:
        await M._rot_step_pc("main", base, st, pcs)
    finally:
        M._ROT_STEP_CUR.update(key=None, st=None)
    return M._ROT.get(key) is st


def _hard():
    return [t for t, r in SAID if not r]


async def t_ack_stops_rotation_quietly():
    await reset()
    M._ROT[M.ns("main", "PC-09")] = _starting(60)
    cid = await db.insert_command("PC-09b", "start", {})
    await M.ack_cmd("PC-09b", cid, Req({"status": "cancelled", "why": TAG_WHY}))
    ok("I667-1 ★표식 ack → 그 물리 PC 순환이 멈춘다★", M.ns("main", "PC-09") not in M._ROT, str(M._ROT))
    ok("I667-2 ★텔레그램 없이 routine 한 줄(«계정2 본인 확인 필요 — 순환 멈춤»)★",
       _hard() == [] and any("계정2 본인 확인 필요 — 순환 멈춤" in t and r for t, r in SAID), str(SAID))
    held = json.loads(await db.get_setting("idverify_hold") or "{}")
    ok("I667-3 보류가 설정에 남는다(PC 는 물리 PC 키, 카드는 PC-09b)",
       held.get("PC-09", {}).get("pc") == "PC-09b" and "사고 667" in held["PC-09"].get("why", ""), str(held))
    rows = await db.get_recent_commands(20)
    ok("I667-4 명령 이력은 예전처럼 cancelled", next(r for r in rows if r["id"] == cid)["status"] == "cancelled")


async def t_other_cancel_untouched():
    await reset()
    st = _starting(60)
    M._ROT[M.ns("main", "PC-10")] = st
    cid = await db.insert_command("PC-10b", "start", {})
    await M.ack_cmd("PC-10b", cid, Req({"status": "cancelled", "why": "이전 명령 처리 중"}))
    ok("I667-5 표식 없는 cancelled 는 순환·보류를 안 건드린다",
       M._ROT.get(M.ns("main", "PC-10")) is st and not M._IDV_HOLD and SAID == [], str(SAID))
    cid2 = await db.insert_command("PC-10b", "start", {})
    await M.ack_cmd("PC-10b", cid2, Req({"status": "acked", "why": TAG_WHY}))
    ok("I667-6 acked 는 why 에 표식이 있어도 보류가 아니다(접힌 것만)", not M._IDV_HOLD, str(M._IDV_HOLD))


async def t_rotation_guard():
    await reset()
    pcs = [card("PC-09", "other_account", 3000), card("PC-09b", "idle")]
    alive = await step(_starting(M.ROT_START_MAX + 60), pcs)
    ok("I667-7 대조: 보류가 없으면 예전처럼 ⛔ «▶시작 뒤 N분째 사냥이 안 잡힙니다»",
       not alive and any("사냥이 안 잡힙니다" in t for t in _hard()), str(SAID))
    await reset()
    M._IDV_HOLD[M.ns("main", "PC-09")] = {"pc": "PC-09b", "why": TAG_WHY, "at": "x"}
    alive = await step(_starting(M.ROT_START_MAX + 60), pcs)
    ok("I667-8 ★보류가 살아 있으면(재배포로 되살아난 무장) ⛔ 대신 조용히 멈춘다★",
       not alive and _hard() == [] and any("본인 확인 필요" in t for t, _r in SAID), str(SAID))


async def t_scout_skips():
    await reset()
    M._IDV_HOLD[M.ns("main", "PC-09")] = {"pc": "PC-09b", "why": TAG_WHY, "at": "x"}
    ok("I667-9 스카우터 음소거: 그 물리 PC 의 모든 카드", M._scout_muted("main", "PC-09")
       and M._scout_muted("main", "PC-09b") and not M._scout_muted("main", "PC-10"))
    names = ["PC-09_20260929_043800_PC-09_20260929_043801_lc2-rescue-aion2-fail.png",
             "PC-10_20260929_043800_captcha.png"]

    async def run_scout():
        sc, t0, out = S.Scout(), 1_790_000_000.0, []
        rows = [card("PC-09", "idle"), card("PC-09b", "idle"), card("PC-10", "idle")]

        async def logs(p):
            return []
        mute = lambda p: M._scout_muted("main", p)       # noqa: E731
        for t, ns_ in ((t0, []), (t0 + 60, names), (t0 + 60 + S.BUG_GRACE_S + 5, names)):
            o = await sc.step(rows, t, logs, ns_, mute)
            for p, k, _x in o:
                sc.mark(p, k, t)
            out += [(p, k) for p, k, _x in o]
        return out
    held = await run_scout()
    M._IDV_HOLD.clear()
    free = await run_scout()
    ok("I667-10 ★보류 PC 의 버그스샷(lcN-rescue-aion2-fail)은 안 운다 · 다른 PC 는 그대로 · 대조: 보류가 없으면 운다★",
       held == [("PC-10", "bug:captcha")] and ("PC-09", "bug:lcN-rescue-aion2-fail") in free, str([held, free]))


async def t_human_clears():
    await reset()
    M.macro_ws_connections.clear()
    M._IDV_HOLD[M.ns("main", "PC-09")] = {"pc": "PC-09b", "why": TAG_WHY, "at": "x"}
    await db.set_setting("idverify_hold", json.dumps(M._IDV_HOLD, ensure_ascii=False))
    await M._dispatch_macro_command("main", "PC-09b", "stop", {})
    ok("I667-11 사람 ■정지는 보류를 안 푼다", M.ns("main", "PC-09") in M._IDV_HOLD)
    await M._dispatch_macro_command("main", "PC-09b", "start", {})
    ok("I667-12 ★사람 ▶시작이 보류를 푼다(설정까지)★", not M._IDV_HOLD
       and json.loads(await db.get_setting("idverify_hold") or "{}") == {}, str(M._IDV_HOLD))
    logs = await db.get_logs("PC-09b", 20)
    ok("I667-13 푼 것을 카드 로그에 남긴다", any("보류(사고 667) 풀림" in str(x.get("message")) for x in logs))
    M._IDV_HOLD[M.ns("main", "PC-09")] = {"pc": "PC-09b", "why": TAG_WHY, "at": "x"}
    await M._dispatch_macro_command("main", "PC-09", "switch_launcher", {"acct_no": 3, "chrome_label": "c"})
    ok("I667-14 ★사람 전환(다른 카드여도 같은 물리 PC)이 보류를 푼다★", not M._IDV_HOLD, str(M._IDV_HOLD))


async def t_persist():
    await reset()
    M._IDV_LOADED[0] = False
    await db.set_setting("idverify_hold", json.dumps({"PC-09": {"pc": "PC-09b", "why": TAG_WHY, "at": "x"}},
                                                     ensure_ascii=False))
    await M._idv_load()
    ok("I667-15 ★재배포 뒤에도 보류가 살아난다(설정 idverify_hold)★", M._idv_held("main", "PC-09c") is not None)
    ok("I667-16 idverify_hold 는 서버 관리 설정(일반 POST 금지) · 표식 문자열 = 매크로와 같은 글자",
       "idverify_hold" in M.SERVER_MANAGED_SETTINGS and M.IDVERIFY_HOLD_TAG == "본인 확인 필요(사고 667)")
    await reset()


def test_all():
    for k, v in _PATCH.items():
        _ORIG[k] = getattr(M, k)
        setattr(M, k, v)
    try:
        run_all([t_ack_stops_rotation_quietly, t_other_cancel_untouched, t_rotation_guard, t_scout_skips,
                 t_human_clears, t_persist])
    finally:
        for k, v in _ORIG.items():
            setattr(M, k, v)
    finish("test_idverify_667", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
