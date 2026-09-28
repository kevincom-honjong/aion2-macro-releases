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

MIN_CHECKS = 27
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


def _ic(pid, st, upd):
    """스카우터용 카드 — upd = 서버가 받은 시각(epoch)."""
    iso = datetime.fromtimestamp(upd, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return {"pc_id": pid, "status": st, "_updated_at": iso, "last_active": iso, "_ws_live": True,
            "errors": [], "daily_progress": [{"slot": 1, "completed": False}], "slot": 1}


class _Scene:
    """main._scout_tick 그대로(상태·버그 목록·텔레그램만 가짜) — 좁힌 범위를 배선째로 본다."""

    def __init__(self):
        self.rows, self.files, self.sent, self.fail = [], [], [], False

    async def state(self, tenant):
        return list(self.rows)

    async def tg(self, chat, text):
        if self.fail:
            return None
        self.sent.append(text)
        return 1

    def __enter__(self):
        self.real = (M._build_full_state, M.tg_send_text, M.tg_enabled, M.tenant_chat_id, M._list_bug_files,
                     M._tg_muted, M.get_logs)
        M._build_full_state, M.tg_send_text = self.state, self.tg
        M.tg_enabled, M.tenant_chat_id = (lambda: True), (lambda t: "123")
        M._list_bug_files = lambda tenant, pc_id=None: [{"filename": f} for f in self.files]
        M._tg_muted = lambda tenant, pc: 0.0
        M.get_logs = _f_logs
        M._SCOUT.__init__()
        M._SCOUT_LOADED[0] = True
        return self

    def __exit__(self, *e):
        (M._build_full_state, M.tg_send_text, M.tg_enabled, M.tenant_chat_id, M._list_bug_files,
         M._tg_muted, M.get_logs) = self.real
        M._SCOUT.__init__()


def _said(sent, *words):
    return [x for x in sent if all(w in x for w in words)]


async def t_scout_narrow():
    """① 보류는 버그스샷(구조 실패)만 뺀다 — 죽음·error·무보고는 운다(아이온2: #288 조용한 알람)."""
    await reset()
    M._IDV_HOLD[M.ns("main", "PC-09")] = {"pc": "PC-09b", "why": TAG_WHY, "at": "2099-01-01T00:00:00Z"}
    ok("I667-9 ★보류는 스카우터 음소거가 아니다(죽음·error 셈·전송 그대로)★", not M._scout_muted("main", "PC-09")
       and not M._scout_muted("main", "PC-09b"))
    shot = "PC-09_20260929_043800_PC-09_20260929_043801_lc2-rescue-aion2-fail.png"
    t0 = 1_790_000_000.0
    with _Scene() as sc:
        sc.rows = [_ic("PC-09", "idle", t0), _ic("PC-09b", "idle", t0), _ic("PC-10", "idle", t0)]
        await M._scout_tick("main", now=t0)
        sc.files = [shot, "PC-10_20260929_043800_captcha.png"]
        sc.rows = [_ic("PC-09", "idle", t0 + 60), _ic("PC-09b", "error", t0 + 60), _ic("PC-10", "idle", t0 + 60)]
        await M._scout_tick("main", now=t0 + 60)
        sc.rows = [_ic("PC-09", "idle", t0 + 60), _ic("PC-09b", "error", t0 + 60), _ic("PC-10", "idle", t0 + 700)]
        await M._scout_tick("main", now=t0 + 60 + M._scout_mod.BUG_GRACE_S + 5)
        bug_held = _said(sc.sent, "PC-09", "rescue")
        err = _said(sc.sent, "PC-09b", "상태=error")
        other = _said(sc.sent, "PC-10", "captcha")
    ok("I667-10 ★보류 PC 의 버그스샷(lcN-rescue-aion2-fail)은 안 운다 · 다른 PC 버그는 운다★",
       bug_held == [] and len(other) == 1, str([bug_held, other]))
    ok("I667-17 ★보류 중이어도 error 는 운다(매크로 크래시 = 주인님이 들어야 한다)★", len(err) == 1, str(err))
    with _Scene() as sc:
        sc.rows = [_ic("PC-09", "idle", t0), _ic("PC-09b", "idle", t0)]
        await M._scout_tick("main", now=t0)
        sc.rows = [_ic("PC-09", "offline", t0), _ic("PC-09b", "offline", t0)]
        await M._scout_tick("main", now=t0 + 60)
        await M._scout_tick("main", now=t0 + 60 + M._scout_mod.DWELL["offline"] + 5)
        dead = _said(sc.sent, "PC-09", "offline")
    ok("I667-18 ★보류 중 PC 가 죽으면(offline·무보고) 운다★", len(dead) >= 1, str(dead))
    # 실물 순서 — 구조 실패 스샷이 먼저(유예 10분 대기 중), 매크로의 표식 ack 가 그 뒤에 온다
    M._IDV_HOLD.clear()
    with _Scene() as sc:
        sc.rows = [_ic("PC-09", "idle", t0)]
        await M._scout_tick("main", now=t0)
        sc.files = ["PC-09_20260929_043700_stuck_map.png"]          # 유예로 가는 종류
        sc.rows = [_ic("PC-09", "idle", t0 + 60)]
        await M._scout_tick("main", now=t0 + 60)
        pend = bool(M._SCOUT.bug_pending.get("PC-09"))
        M._IDV_HOLD[M.ns("main", "PC-09")] = {"pc": "PC-09b", "why": TAG_WHY, "at": "2099-01-01T00:00:00Z"}
        await M._scout_tick("main", now=t0 + 60 + M._scout_mod.BUG_GRACE_S + 5)
        late = _said(sc.sent, "PC-09", "stuck_map")
    ok("I667-27 ★스샷이 유예 중일 때 보류가 걸려도(실물 순서) 유예 끝에 안 운다★", pend and late == [], str([pend, late]))
    # 대조 — 보류가 없으면 그 구조 실패 스샷은 운다(시험이 헛돌지 않게)
    M._IDV_HOLD.clear()
    with _Scene() as sc:
        sc.rows = [_ic("PC-09", "idle", t0)]
        await M._scout_tick("main", now=t0)
        sc.files = [shot]
        await M._scout_tick("main", now=t0 + 60)
        await M._scout_tick("main", now=t0 + 60 + M._scout_mod.BUG_GRACE_S + 5)
        free = _said(sc.sent, "PC-09", "rescue")
    ok("I667-19 대조: 보류가 없으면 같은 스샷이 운다", len(free) == 1, str(free))


async def t_card_and_chip():
    """② 보류가 /status 카드와 대시보드 칩에 보인다."""
    await reset()
    await db.upsert_status("PC-09b", {"pc_id": "PC-09b", "status": "idle", "character": "부캐"})
    await db.upsert_status("PC-09", {"pc_id": "PC-09", "status": "other_account", "character": "주캐"})
    await db.upsert_status("PC-10", {"pc_id": "PC-10", "status": "idle", "character": "c10"})
    M._IDV_HOLD[M.ns("main", "PC-09")] = {"pc": "PC-09b", "why": TAG_WHY, "at": "2026-09-29T04:38:00Z"}
    rows = {r["pc_id"]: r for r in await M._build_full_state("main")}
    h = (rows.get("PC-09b") or {}).get("idverify_hold")
    ok("I667-20 ★/status 카드에 idverify_hold:{since, acct}(그 물리 PC 카드 전부)★",
       h == {"since": "2026-09-29T04:38:00Z", "acct": 2, "pc": "PC-09b"}
       and (rows.get("PC-09") or {}).get("idverify_hold") == h and "idverify_hold" not in (rows.get("PC-10") or {}),
       str([h, rows.get("PC-10", {}).get("idverify_hold")]))
    html = M.HTML_DASHBOARD
    ok("I667-21 ★대시보드 카드에 «본인 확인 필요» 칩이 그려진다(카드 줄에 끼움)★",
       "const idvChip = pc.idverify_hold" in html and "본인 확인 필요 · 계정" in html
       and "${idvChip}${bugBadge}${doneBadges}" in html)
    M._IDV_HOLD.clear()
    rows = {r["pc_id"]: r for r in await M._build_full_state("main")}
    ok("I667-22 풀리면 카드에서 사라진다", "idverify_hold" not in (rows.get("PC-09b") or {}))


async def t_remind_12h():
    """③ 12시간째 ★한 번★ 텔레그램 «PC-XX 계정N 본인 확인 대기 12시간째»."""
    await reset()
    now = 1_790_000_000.0

    def at(ago):
        return datetime.fromtimestamp(now - ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    M._IDV_HOLD[M.ns("main", "PC-09")] = {"pc": "PC-09b", "why": TAG_WHY, "at": at(M.IDVERIFY_REMIND_S - 60)}
    with _Scene() as sc:
        await M._scout_tick("main", now=now)
        ok("I667-23 12시간 전엔 안 보낸다", _said(sc.sent, "본인 확인 대기") == [], str(sc.sent))
        M._IDV_HOLD[M.ns("main", "PC-09")]["at"] = at(M.IDVERIFY_REMIND_S + 30)
        sc.fail = True
        await M._scout_tick("main", now=now + 60)
        ok("I667-24 전송 실패면 reminded 를 안 적는다(다음 틱에 다시)",
           not M._IDV_HOLD[M.ns("main", "PC-09")].get("reminded"))
        sc.fail = False
        await M._scout_tick("main", now=now + 120)
        await M._scout_tick("main", now=now + 180)
        await M._scout_tick("main", now=now + 3 * 3600)
        r = _said(sc.sent, "PC-09 계정2 본인 확인 대기 12시간째")
    ok("I667-25 ★스카우터 틱이 12시간째 한 번 보낸다 · 다시 안 보낸다(3시간 뒤에도)★", len(r) == 1, str(sc.sent))
    held = json.loads(await db.get_setting("idverify_hold") or "{}")
    ok("I667-26 보냈다는 표시가 설정에 남는다(재배포 뒤 또 안 보냄)", bool(held.get("PC-09", {}).get("reminded")), str(held))


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
        run_all([t_ack_stops_rotation_quietly, t_other_cancel_untouched, t_rotation_guard, t_scout_narrow,
                 t_card_and_chip, t_remind_12h, t_human_clears, t_persist])
    finally:
        for k, v in _ORIG.items():
            setattr(M, k, v)
    finish("test_idverify_667", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
