# -*- coding: utf-8 -*-
"""🔭 스카우터 알람을 서버 안으로 (주인님 #288) — 가상 시계 시험.

    cd updater/server && python -X utf8 tests/test_scout_alarm.py
"""
import asyncio
import json
from datetime import datetime, timedelta

from _harness import main, db, ok, run_all, finish   # noqa: E402
import scout_alarm as S

MIN_CHECKS = 30
T0 = 1_790_000_000.0


def iso(t):
    return (datetime(1970, 1, 1) + timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%S")


def card(pid, st="hunting", t=T0, **kw):
    r = {"pc_id": pid, "status": st, "last_active": iso(t), "_updated_at": iso(t), "_ws_live": True,
         "_bug_count": 0, "errors": [], "daily_progress": [{"slot": 1, "completed": False}], "slot": 1}
    r.update(kw)
    return r


class World:
    def __init__(self):
        self.logs = {}      # pid -> [(created_at, msg)]
        self.bugs = {}      # base -> [names]

    async def logs_fn(self, pid):
        return self.logs.get(pid, [])

    async def bugs_fn(self, bp):
        return self.bugs.get(bp, [])


async def run(sc, w, rows, t):
    return await sc.step(rows, t, w.logs_fn, w.bugs_fn)


def keys(out):
    return [(p, k) for p, k, _t in out]


async def t_offline_dwell_once_realarm():
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-01")], T0)                               # 기준선
    o = await run(sc, w, [card("PC-01", "offline", T0)], T0 + 60)
    o += await run(sc, w, [card("PC-01", "offline", T0)], T0 + 60 + 170)
    ok("SC-1 offline 3분 전엔 안 울린다", o == [], str(o))
    o = await run(sc, w, [card("PC-01", "offline", T0)], T0 + 60 + 181)
    ok("SC-2 ★offline 3분 → 🔭 한 통★", keys(o) == [("PC-01", "offline")] and "분째 지속" in o[0][2], str(o))
    o = await run(sc, w, [card("PC-01", "offline", T0)], T0 + 60 + 600)
    ok("SC-3 같은 고장은 다시 안 울린다(도배 없음)", o == [], str(o))
    o = await run(sc, w, [card("PC-01", "offline", T0)], T0 + 60 + 181 + S.RENOTIFY + 1)
    ok("SC-4 ★6시간 뒤에도 그대로면 «계속 고장 중 — 재알림»★", keys(o) == [("PC-01", "offline")] and "재알림" in o[0][2], str(o))
    t = T0 + 60 + 181 + S.RENOTIFY + 100
    await run(sc, w, [card("PC-01", "hunting", t)], t)
    o = await run(sc, w, [card("PC-01", "offline", t)], t + 60)
    o += await run(sc, w, [card("PC-01", "offline", t)], t + 60 + 181)
    ok("SC-5 풀렸다가 다시 죽으면 곧바로 다시 울린다(재무장)", keys(o) == [("PC-01", "offline")], str(o))


async def t_offline_alive_or_parked():
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-02")], T0)
    w.logs["PC-02"] = [(iso(T0 + 250), "[맵] 도착")]
    await run(sc, w, [card("PC-02", "offline")], T0 + 60)
    o = await run(sc, w, [card("PC-02", "offline")], T0 + 300)
    ok("SC-6 offline 이라도 로그가 흐르면 안 울린다", o == [], str(o))
    w.logs["PC-02"] = [(iso(T0 + 10), "[BOOT] 백그라운드 스레드 시작됨"), (iso(T0 + 20), "PageDown 수동 종료")]
    o = await run(sc, w, [card("PC-02", "offline")], T0 + 700)
    o += await run(sc, w, [card("PC-02", "offline")], T0 + 900)
    ok("SC-7 ★사람이 세웠으면(수동 종료) 안 울린다★", o == [], str(o))
    o = await run(sc, w, [card("PC-02", "offline", stopped_by="human")], T0 + 2000)
    ok("SC-8 stopped_by=human 카드는 감시 밖", o == [], str(o))
    w.logs["PC-02"] = [(iso(T0 + 20), "PageDown 수동 종료"), (iso(T0 + 30), "[BOOT] 서버 연결 시작"), (iso(T0 + 40), "x")]
    await run(sc, w, [card("PC-02", "offline")], T0 + 3000)
    o = await run(sc, w, [card("PC-02", "offline")], T0 + 3200)
    ok("SC-9 껐다가 다시 켠 뒤 죽으면 울린다(부팅 표식이 뒤)", keys(o) == [("PC-02", "offline")], str(o))


async def t_reconnecting_wsdead():
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-03"), card("PC-04")], T0)
    await run(sc, w, [card("PC-03", "reconnecting"), card("PC-04", "hunting", T0, _ws_live=False)], T0 + 10)
    o = await run(sc, w, [card("PC-03", "reconnecting"), card("PC-04", "hunting", T0, _ws_live=False)], T0 + 251)
    ok("SC-10 reconnecting 4분 → 울림 · WS 끊김은 6분 전이라 아직", keys(o) == [("PC-03", "reconnecting")], str(o))
    o = await run(sc, w, [card("PC-03", "reconnecting"), card("PC-04", "hunting", T0, _ws_live=False)], T0 + 371)
    ok("SC-11 ★사냥 중 WS 끊김 + 보고 정체 6분 → 울림★", keys(o) == [("PC-04", "wsdead")], str(o))
    sc2 = S.Scout()
    await run(sc2, w, [card("PC-05")], T0)
    await run(sc2, w, [card("PC-05", "hunting", T0 + 10, _ws_live=False)], T0 + 10)
    o = await run(sc2, w, [card("PC-05", "hunting", T0 + 400, _ws_live=False)], T0 + 400)
    ok("SC-12 WS 는 끊겼어도 상태 보고가 신선하면 안 울린다", o == [], str(o))


async def t_error_and_errors():
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-06"), card("PC-07")], T0)
    o = await run(sc, w, [card("PC-06", "error"), card("PC-07", errors=["E1"])], T0 + 60)
    ok("SC-13 ★status=error 바로 · errors 새로 생김 바로★", sorted(keys(o)) == [("PC-06", "error"), ("PC-07", "errors")], str(o))
    o = await run(sc, w, [card("PC-06", "error"), card("PC-07", errors=["E1", "E2"])], T0 + 120)
    ok("SC-14 같은 PC 같은 이유(errors)는 30분 안 한 번", o == [], str(o))
    o = await run(sc, w, [card("PC-06", "error"), card("PC-07", errors=["E1", "E2", "E3"])], T0 + 120 + S.EVENT_WINDOW)
    ok("SC-15 창이 지나면 새 errors 는 다시", keys(o) == [("PC-07", "errors")], str(o))
    w.logs["PC-08"] = [(iso(T0), "[원격명령] exit 명령 수신 — 매크로 종료")]
    await run(sc, w, [card("PC-08")], T0 + 200)
    o = await run(sc, w, [card("PC-08", "error")], T0 + 260)
    ok("SC-16 사람이 exit 로 끈 error 는 안 울린다", o == [], str(o))
    sc3 = S.Scout()
    o = await run(sc3, w, [card("PC-09", "error", errors=["x"])], T0)
    ok("SC-17 첫 틱(서버 부팅)의 errors 는 기준선 — 안 울림, error 상태는 울림", keys(o) == [("PC-09", "error")], str(o))


async def t_done_slot():
    sc, w = S.Scout(), World()
    dp = [{"slot": 1, "completed": True}, {"slot": 2, "completed": False}]
    await run(sc, w, [card("PC-10", daily_progress=dp, slot=1), card("PC-11")], T0)       # PC-10 은 부팅 때 이미
    o = await run(sc, w, [card("PC-10", daily_progress=dp, slot=1), card("PC-11", daily_progress=dp, slot=1)], T0 + 60)
    o += await run(sc, w, [card("PC-10", daily_progress=dp, slot=1), card("PC-11", daily_progress=dp, slot=1)], T0 + 190)
    ok("SC-18 ★완료 슬롯 사냥 2분 → 울림★ · 부팅 때 이미 그랬던 PC-10 은 안 울림",
       keys(o) == [("PC-11", "done_slot")], str(o))
    await run(sc, w, [card("PC-10", daily_progress=dp, slot=2)], T0 + 300)
    await run(sc, w, [card("PC-10", daily_progress=dp, slot=1)], T0 + 400)
    o = await run(sc, w, [card("PC-10", daily_progress=dp, slot=1)], T0 + 530)
    ok("SC-19 한 번 풀린 뒤 다시 그러면 PC-10 도 울림", keys(o) == [("PC-10", "done_slot")], str(o))
    alld = [{"slot": 1, "completed": True}]
    sc4 = S.Scout()
    await run(sc4, w, [card("PC-12")], T0)
    await run(sc4, w, [card("PC-12", "offline", daily_progress=alld)], T0 + 10)
    o = await run(sc4, w, [card("PC-12", "offline", daily_progress=alld)], T0 + 900)
    ok("SC-20 오늘 할 일을 다 끝낸 PC 의 offline 은 안 울린다", o == [], str(o))


async def t_bugs():
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-13"), card("PC-13b", "idle", _bug_count=0)], T0)
    w.bugs["PC-13"] = ["PC-13b_20260927_101000_captcha.png", "PC-13_20260927_090000_old_fail.png"]
    o = await run(sc, w, [card("PC-13", _bug_count=1), card("PC-13b", "idle", _bug_count=1)], T0 + 60)
    ok("SC-21 ★캡차 버그스샷 = 바로 · 물리 PC 한 번만(카드 둘)★", keys(o) == [("PC-13", "bug:captcha")], str(o))
    w.bugs["PC-13"].append("PC-13_20260927_101100_switch_launcher-fail.png")
    o = await run(sc, w, [card("PC-13", _bug_count=2), card("PC-13b", "idle", _bug_count=2)], T0 + 120)
    ok("SC-22 전환 실패 가족 = 유예 없이 바로", keys(o) == [("PC-13", "bug:acct_switch")], str(o))
    w.bugs["PC-13"].append("PC-13_20260927_101200_ocr_learn_ok.png")
    o = await run(sc, w, [card("PC-13", _bug_count=3), card("PC-13b", "idle", _bug_count=3)], T0 + 180)
    ok("SC-23 깨울 종류가 아닌 스샷(학습 크롭 등)은 조용", o == [] and not sc.bug_pending, str(o))
    # 유예: 자가복구
    sc2 = S.Scout()
    await run(sc2, w, [card("PC-14")], T0)
    w.bugs["PC-14"] = ["PC-14_20260927_101000_unknown_screen.png"]
    o = await run(sc2, w, [card("PC-14", "idle", _bug_count=1)], T0 + 60)
    o += await run(sc2, w, [card("PC-14", "hunting", T0 + 700, _bug_count=1)], T0 + 700)
    ok("SC-24 ★그 밖의 깨울 종류는 유예 10분 → 다시 일하면 자가복구(안 울림)★", o == [] and not sc2.bug_pending, str(o))
    # 유예: 멈춘 채
    sc3 = S.Scout()
    await run(sc3, w, [card("PC-15")], T0)
    w.bugs["PC-15"] = ["PC-15_20260927_101000_stuck_map.png"]
    await run(sc3, w, [card("PC-15", "idle", _bug_count=1)], T0 + 60)
    o = await run(sc3, w, [card("PC-15", "idle", _bug_count=1)], T0 + 600)
    ok("SC-25 유예 10분 전엔 안 울림", o == [], str(o))
    o = await run(sc3, w, [card("PC-15", "idle", _bug_count=1)], T0 + 661)
    ok("SC-26 ★10분 뒤에도 멈춰 있으면 울림(자가복구 안 됨)★", len(o) == 1 and "자가복구 안 됨" in o[0][2], str(o))
    sc5 = S.Scout()
    await run(sc5, w, [card("PC-16")], T0)
    w.bugs["PC-16"] = ["PC-16_20260927_101000_warehouse_enter_fail.png"]
    o = await run(sc5, w, [card("PC-16", "idle", _bug_count=1)], T0 + 60)
    ok("SC-27 과제 음소거(warehouse_enter_fail #81)는 조용", o == [] and not sc5.bug_pending, str(o))


async def t_mass():
    sc, w = S.Scout(), World()
    pcs = ["PC-17", "PC-18", "PC-19", "PC-20"]
    await run(sc, w, [card(p) for p in pcs], T0)
    await run(sc, w, [card(p, "offline") for p in pcs[:3]] + [card("PC-20")], T0 + 10)
    o = await run(sc, w, [card(p, "offline") for p in pcs[:3]] + [card("PC-20")], T0 + 200)
    ok("SC-28 ★15분 안에 3대 → 「회선/전원 의심」 한 통★", [k for _p, k in keys(o)].count("mass") == 1
       and any("회선/전원" in t for _p, _k, t in o), str(o))
    await run(sc, w, [card(p, "offline") for p in pcs], T0 + 260)
    o = await run(sc, w, [card(p, "offline") for p in pcs], T0 + 500)
    ok("SC-29 30분 안엔 집단 알람 다시 안 옴", "mass" not in [k for _p, k in keys(o)], str(o))


async def t_wiring():
    # main._scout_tick — 실제 /status 카드 한 벌을 흉내 · 장부는 설정에 저장 · 텔레그램 대신 기록
    sent = []

    async def fake_state(tenant):
        return [card("PC-21", "error")]

    async def fake_tg(chat, text):
        sent.append(text)
        return 1

    real = (main._build_full_state, main.tg_send_text, main.tg_enabled, main.tenant_chat_id)
    main._build_full_state, main.tg_send_text = fake_state, fake_tg
    main.tg_enabled, main.tenant_chat_id = (lambda: True), (lambda t: "123")
    try:
        main._SCOUT.__init__()
        main._SCOUT_LOADED[0] = False
        out = await main._scout_tick("main", now=T0)
        saved = json.loads(await db.get_setting("scout_alerted") or "{}")
    finally:
        main._build_full_state, main.tg_send_text, main.tg_enabled, main.tenant_chat_id = real
    ok("SC-30 ★배선: 🔭 스카우터 글로 텔레그램 · 장부 저장(재배포 뒤 도배 없음)★",
       out and sent and sent[0].startswith("PC-21 | 🔭 스카우터: PC-21 — 상태=error") and "PC-21:error" in saved, str(sent))


def test_all():
    run_all([t_offline_dwell_once_realarm, t_offline_alive_or_parked, t_reconnecting_wsdead, t_error_and_errors,
             t_done_slot, t_bugs, t_mass, t_wiring])
    finish("test_scout_alarm", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
