# -*- coding: utf-8 -*-
"""🔭 스카우터 알람을 서버 안으로 (주인님 #288) — 가상 시계 시험. 아이온2 반증(HIGH-1·MED-2~7·LOW) 각각을 한 줄 이상이 지킨다.

    cd updater/server && python -X utf8 tests/test_scout_alarm.py
"""
import json
from datetime import datetime, timedelta

from _harness import main, db, ok, run_all, finish   # noqa: E402
import scout_alarm as S

MIN_CHECKS = 51
T0 = 1_790_000_000.0


def iso(t):
    return (datetime(1970, 1, 1) + timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%S")


def card(pid, st="hunting", t=T0, **kw):
    """t = 서버가 마지막으로 받은 시각(_updated_at). last_active(매크로 시계)는 일부러 5분 어긋나게."""
    r = {"pc_id": pid, "status": st, "_updated_at": iso(t), "last_active": iso(t + 300), "_ws_live": True,
         "errors": [], "daily_progress": [{"slot": 1, "completed": False}], "slot": 1}
    r.update(kw)
    return r


class World:
    def __init__(self):
        self.logs, self.bugs, self.muted, self.fail = {}, [], set(), False

    async def logs_fn(self, pid):
        return self.logs.get(pid, [])


async def run(sc, w, rows, t):
    """한 틱 — 후보를 ★보낸 것으로★ 장부에 적는다(w.fail 이면 전송 실패 = 안 적음)."""
    out = await sc.step(rows, t, w.logs_fn, list(w.bugs), lambda p: p in w.muted or S.base_pc(p) in w.muted)
    if not w.fail:
        for p, k, _t in out:
            sc.mark(p, k, t)
    return out


def keys(out):
    return [(p, k) for p, k, _t in out]


async def t_offline():
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-01")], T0)
    o = await run(sc, w, [card("PC-01", "offline", T0)], T0 + 60)
    o += await run(sc, w, [card("PC-01", "offline", T0)], T0 + 230)
    ok("SC-1 offline 3분 전엔 안 울린다", o == [], str(o))
    o = await run(sc, w, [card("PC-01", "offline", T0)], T0 + 241)
    ok("SC-2 ★offline 3분 → 🔭 한 통★ · 시간은 서버 수신 정체(4분)", keys(o) == [("PC-01", "dead")] and "4분째" in o[0][2], str(o))
    o = await run(sc, w, [card("PC-01", "offline", T0)], T0 + 900)
    ok("SC-3 같은 사고는 다시 안 울린다", o == [], str(o))
    o = await run(sc, w, [card("PC-01", "offline", T0)], T0 + 241 + S.RENOTIFY + 1)
    ok("SC-4 ★6시간 뒤 그대로면 «재알림» · 기간은 시간 단위로 참말★", keys(o) == [("PC-01", "dead")] and "재알림" in o[0][2]
       and "6.1시간째" in o[0][2], str(o))
    t = T0 + 30000
    await run(sc, w, [card("PC-01", "hunting", t)], t)
    await run(sc, w, [card("PC-01", "offline", t)], t + 60)
    o = await run(sc, w, [card("PC-01", "offline", t)], t + 241)
    ok("SC-5 풀렸다가 다시 죽으면 곧바로 다시(재무장)", keys(o) == [("PC-01", "dead")] and "재알림" not in o[0][2], str(o))


async def t_alive_exit_park():
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-02")], T0)
    w.logs["PC-02"] = [(iso(T0 + 250), "[맵] 도착")]
    await run(sc, w, [card("PC-02", "offline")], T0 + 60)
    o = await run(sc, w, [card("PC-02", "offline")], T0 + 300)
    ok("SC-6 offline 이라도 매크로 로그가 흐르면 안 울린다", o == [], str(o))
    w.logs["PC-02"] = [(iso(T0 + 100), "[맵] 도착"), (iso(T0 + 590), "[순환] 전환 대기"), (iso(T0 + 595), "[스카우터] x")]
    await run(sc, w, [card("PC-02", "offline")], T0 + 400)
    o = await run(sc, w, [card("PC-02", "offline")], T0 + 600)
    ok("SC-7 ★서버가 쓴 줄([순환]·[스카우터])은 「흐른다」 로 안 친다★", keys(o) == [("PC-02", "dead")], str(o))
    sc2 = S.Scout()
    await run(sc2, w, [card("PC-03")], T0)
    w.logs["PC-03"] = [(iso(T0), "[BOOT] 핫키 대기"), (iso(T0 + 10), "[원격명령] exit 명령 — 매크로 종료")]
    await run(sc2, w, [card("PC-03", "offline")], T0 + 60)
    o = await run(sc2, w, [card("PC-03", "offline")], T0 + 400)
    ok("SC-8 사람이 exit 로 끈 PC 는 안 울린다", o == [], str(o))
    sc3 = S.Scout()
    await run(sc3, w, [card("PC-04")], T0)
    w.logs["PC-04"] = [(iso(T0), "Home 수동 일시정지"), (iso(T0 + 5), "[원격명령] 매크로 정지"), (iso(T0 + 8), "어비스에서 자동 퇴장")]
    await run(sc3, w, [card("PC-04", "offline")], T0 + 60)
    o = await run(sc3, w, [card("PC-04", "offline")], T0 + 400)
    ok("SC-9 ★일시정지·■정지·어비스 퇴장 뒤 진짜 죽음은 울린다(exit 표식만 면제, MED-7)★", keys(o) == [("PC-04", "dead")], str(o))
    o = await run(sc3, w, [card("PC-05", "offline", stopped_by="human")], T0 + 2000)
    ok("SC-10 stopped_by=human 카드는 감시 밖", o == [], str(o))


async def t_wsdead_and_merge():
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-06"), card("PC-07", "abyss")], T0)
    rows = lambda t: [card("PC-06", "reconnecting", t), card("PC-07", "abyss", T0, _ws_live=False)]
    await run(sc, w, rows(T0 + 10), T0 + 10)
    o = await run(sc, w, rows(T0 + 251), T0 + 251)
    ok("SC-11 reconnecting 4분 → 울림 · WS 끊김은 서버 수신 6분 전이라 아직", keys(o) == [("PC-06", "reconnecting")], str(o))
    o = await run(sc, w, rows(T0 + 361), T0 + 361)
    ok("SC-12 ★어비스 중 WS 끊김 + 서버 수신 정체 6분 → 울림(MED-3)★", keys(o) == [("PC-07", "dead")] and "abyss" in o[0][2], str(o))
    o = await run(sc, w, [card("PC-06", "reconnecting", T0 + 900), card("PC-07", "offline", T0, _ws_live=False)], T0 + 1100)
    ok("SC-13 ★900초 뒤 카드가 offline 으로 뒤집혀도 같은 사고 — 두 번 안 운다(MED-4)★", o == [], str(o))
    sc2 = S.Scout()
    await run(sc2, w, [card("PC-08")], T0)
    o = await run(sc2, w, [card("PC-08", "hunting", T0 + 400, _ws_live=False, last_active=iso(T0 - 300))], T0 + 400)
    ok("SC-14 WS 는 끊겼어도 서버가 방금 받았으면(매크로 시계가 늦어도) 안 울린다", o == [], str(o))


async def t_all_done_and_today():
    sc, w = S.Scout(), World()
    alld = [{"slot": 1, "completed": True, "today": True}]
    await run(sc, w, [card("PC-09", daily_progress=alld)], T0)
    await run(sc, w, [card("PC-09", "offline", daily_progress=alld)], T0 + 10)
    o = await run(sc, w, [card("PC-09", "offline", daily_progress=alld)], T0 + 200)
    ok("SC-15 ★오늘 할 일을 다 끝낸 PC 도 죽으면 운다(어비스 무한사냥, MED-3)★", keys(o) == [("PC-09", "dead")], str(o))
    dp = [{"slot": 1, "completed": True, "today": False}, {"slot": 2, "completed": False}]
    sc2 = S.Scout()
    await run(sc2, w, [card("PC-10")], T0)
    await run(sc2, w, [card("PC-10", daily_progress=dp, slot=1)], T0 + 10)
    o = await run(sc2, w, [card("PC-10", daily_progress=dp, slot=1)], T0 + 300)
    ok("SC-16 05:00 지나 today:false 인 완료는 「완료 슬롯 사냥」 이 아니다", o == [], str(o))


async def t_done_slot():
    sc, w = S.Scout(), World()
    dp = [{"slot": 1, "completed": True, "today": True}, {"slot": 2, "completed": False}]
    await run(sc, w, [card("PC-11", daily_progress=dp, slot=1), card("PC-12")], T0)
    await run(sc, w, [card("PC-11", daily_progress=dp, slot=1), card("PC-12", daily_progress=dp, slot=1)], T0 + 60)
    o = await run(sc, w, [card("PC-11", daily_progress=dp, slot=1), card("PC-12", daily_progress=dp, slot=1)], T0 + 190)
    ok("SC-17 ★완료 슬롯 사냥 2분 → 울림★ · 부팅 때 이미 그랬던 PC-11 은 안 울림", keys(o) == [("PC-12", "done_slot")], str(o))
    await run(sc, w, [card("PC-11", daily_progress=dp, slot=2)], T0 + 300)
    await run(sc, w, [card("PC-11", daily_progress=dp, slot=1)], T0 + 400)
    o = await run(sc, w, [card("PC-11", daily_progress=dp, slot=1)], T0 + 530)
    ok("SC-18 한 번 풀린 뒤 다시 그러면 PC-11 도 울림", keys(o) == [("PC-11", "done_slot")], str(o))


async def t_error_and_bad_rows():
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-13"), card("PC-14"), card("PC-15")], T0)
    w.logs["PC-15"] = [(iso(T0 + 50), "[알람] PC-15 | ⛔ 계정 전환 실패 — 사람 필요")]
    o = await run(sc, w, [card("PC-13", "error"), card("PC-14", errors=["E1"]), card("PC-15", "error")], T0 + 60)
    ok("SC-19 ★status=error 바로 · errors 새로 생김 바로 · 매크로가 방금 스스로 알린 error 는 안 겹침★",
       sorted(keys(o)) == [("PC-13", "error"), ("PC-14", "errors")], str(o))
    o = await run(sc, w, [card("PC-13", "error"), card("PC-14", errors=["E1", "E2"]), card("PC-15", "error")], T0 + 120)
    ok("SC-20 같은 PC 같은 이유는 창 안 한 번", o == [], str(o))
    bad = [card("PC-16", errors={"x": 1}), card("PC-17", "hunting", _updated_at="깨짐", _ws_live=False),
           {"pc_id": "PC-18", "status": "offline", "daily_progress": "x"}, card("PC-18b", _Boom()),
           card("PC-18c", "hunting", daily_progress=_BoomList([1])), card("PC-19", "error")]
    o = await run(sc, w, bad, T0 + 130)
    o += await run(sc, w, bad, T0 + 140)
    ok("SC-21 ★모양이 틀린 카드가 섞여도 나머지는 운다(LOW)★", ("PC-19", "error") in keys(o), str(o))


class _Boom:
    """str() 이 터지는 값 — 한 카드의 예외가 틱 전체를 죽이지 않는지."""
    def __str__(self):
        raise ValueError("boom")


class _BoomList(list):
    """거르기는 통과하고 판정 안에서 터지는 값 — 카드별 try 가 실제로 받는지."""
    def __iter__(self):
        raise ValueError("boom")


async def t_bugs():
    sc, w = S.Scout(), World()
    w.bugs = ["PC-20_20260927_090000_old_fail.png"]
    await run(sc, w, [card("PC-20"), card("PC-20b", "idle")], T0)
    w.bugs.append("PC-20b_20260927_101000_captcha.png")
    o = await run(sc, w, [card("PC-20"), card("PC-20b", "idle")], T0 + 60)
    ok("SC-22 ★새 캡차 이름 = 바로 · 물리 PC 한 번(카드 둘)★", keys(o) == [("PC-20", "bug:captcha")], str(o))
    w.bugs = w.bugs[1:] + ["PC-20_20260927_101100_switch_launcher-fail.png"]       # 40장 상한 교체 — 개수 그대로
    o = await run(sc, w, [card("PC-20"), card("PC-20b", "idle")], T0 + 120)
    ok("SC-23 ★개수가 안 늘어도(상한 교체) 새 이름이면 본다 · 전환 실패 = 바로(MED-5)★",
       keys(o) == [("PC-20", "bug:switch_launcher-fail")], str(o))
    w.bugs.append("PC-20_20260927_101200_ocr_learn_ok.png")
    o = await run(sc, w, [card("PC-20"), card("PC-20b", "idle")], T0 + 180)
    ok("SC-24 깨울 종류가 아닌 스샷은 조용", o == [] and not sc.bug_pending, str(o))
    ok("SC-25 실물 이름(시각 둘)의 종류 = 마지막 시각 뒤",
       S.bug_kind("PC-05_20260927_012442_PC-05_20260927_102408_cube_trash_open_fail.png") == "cube_trash_open_fail")
    # 회랑 입구 실패가 쏟아져도(48시간 67건) 같은 PC 는 6시간에 한 번
    sc2 = S.Scout()
    await run(sc2, w, [card("PC-21")], T0)
    sent = 0
    for i in range(8):
        t = T0 + 60 + i * 1300
        w.bugs.append(f"PC-21_20260927_{100000 + i:06d}_PC-21_20260927_{110000 + i:06d}_corridor_portal_enter_fail.png")
        await run(sc2, w, [card("PC-21", "idle", t)], t)
        sent += len(await run(sc2, w, [card("PC-21", "idle", t)], t + 700))
    ok("SC-26 ★corridor_portal_enter_fail 8번(3시간) → 1통(같은 PC·같은 종류 6시간)★", sent == 1, str(sent))
    sc3 = S.Scout()
    w.bugs = []
    await run(sc3, w, [card("PC-22")], T0)
    w.bugs = ["PC-22_20260927_101000_unknown_screen.png"]
    o = await run(sc3, w, [card("PC-22", "idle")], T0 + 60)
    o += await run(sc3, w, [card("PC-22", "hunting", T0 + 700)], T0 + 700)
    ok("SC-27 유예 10분 → 다시 일하면 자가복구(안 울림)", o == [] and not sc3.bug_pending, str(o))
    sc4 = S.Scout()
    await run(sc4, w, [card("PC-23")], T0)
    w.bugs = ["PC-23_20260927_101000_stuck_map.png"]
    await run(sc4, w, [card("PC-23", "idle")], T0 + 60)
    o = await run(sc4, w, [card("PC-23", "idle")], T0 + 600)
    ok("SC-28 유예 10분 전엔 안 울림", o == [], str(o))
    o = await run(sc4, w, [card("PC-23", "idle")], T0 + 661)
    ok("SC-29 ★10분 뒤에도 멈춰 있으면 울림★", len(o) == 1 and "자가복구 안 됨" in o[0][2], str(o))
    sc5 = S.Scout()
    await run(sc5, w, [card("PC-24")], T0)
    w.bugs = ["PC-24_20260927_101000_warehouse_enter_fail.png"]
    o = await run(sc5, w, [card("PC-24", "idle")], T0 + 60)
    ok("SC-30 과제 음소거(#81)는 조용", o == [] and not sc5.bug_pending, str(o))


async def t_mass():
    sc, w = S.Scout(), World()
    pcs = ["PC-31", "PC-32", "PC-33", "PC-34"]
    await run(sc, w, [card(p) for p in pcs], T0)
    await run(sc, w, [card(p, "offline") for p in pcs[:3]] + [card("PC-34")], T0 + 10)
    o = await run(sc, w, [card(p, "offline") for p in pcs[:3]] + [card("PC-34")], T0 + 200)
    ok("SC-31 ★15분 안에 3대 → 「회선/전원 의심」 한 통, 함대 이름으로(MED-6)★",
       keys(o) == [(S.FLEET_ID, "mass")] and "회선/전원" in o[0][2], str(o))
    o = await run(sc, w, [card(p, "offline") for p in pcs], T0 + 500)
    o += await run(sc, w, [card(p, "offline") for p in pcs], T0 + 700)
    ok("SC-32 30분 안엔 집단 알람 다시 안 옴 · 회선 알람이 부른 3대는 한 대씩 또 안 부름(나중에 죽은 PC-34 만)",
       keys(o) == [("PC-34", "dead")], str(o))
    # 재배포: 이미 오래 죽어 있던 3대 + 이미 알린 장부
    sc2 = S.Scout(alerted={"PC-31:dead": T0})
    dead = [card(p, "offline", T0 - 86400) for p in pcs[:3]] + [card("PC-34", "offline", T0 - 86400)]
    await run(sc2, w, dead, T0 + 100)
    o = await run(sc2, w, dead, T0 + 400)
    ok("SC-33 ★재배포 때 이미 죽어 있던 PC 들로는 「회선/전원」 안 운다(MED-2)★", "mass" not in [k for _p, k in keys(o)], str(o))
    ok("SC-34 그중 장부에 없던 PC 는 한 대씩 운다 · 기간은 하루로 참말",
       sorted(keys(o)) == [("PC-32", "dead"), ("PC-33", "dead"), ("PC-34", "dead")] and "24.1시간째" in o[0][2], str(o))
    sc3 = S.Scout()
    w.muted = {"PC-41", "PC-42"}
    await run(sc3, w, [card(p) for p in ("PC-41", "PC-42", "PC-43")], T0)
    await run(sc3, w, [card(p, "offline") for p in ("PC-41", "PC-42", "PC-43")], T0 + 10)
    o = await run(sc3, w, [card(p, "offline") for p in ("PC-41", "PC-42", "PC-43")], T0 + 200)
    ok("SC-35 ★음소거 PC 는 집단 셈에 안 넣고 후보도 없다(MED-6)★", keys(o) == [("PC-43", "dead")], str(o))
    w.muted = set()


async def t_mark_only_on_send():
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-51")], T0)
    w.fail = True
    o1 = await run(sc, w, [card("PC-51", "error")], T0 + 60)
    o2 = await run(sc, w, [card("PC-51", "error")], T0 + 120)
    w.fail = False
    ok("SC-36 ★전송 실패면 장부에 안 적고 다음 틱에 다시(MED-6)★", keys(o1) == keys(o2) == [("PC-51", "error")]
       and "PC-51:error" not in sc.alerted, str(o2))
    w.muted = {"PC-52"}
    await run(sc, w, [card("PC-52")], T0 + 130)
    o = await run(sc, w, [card("PC-52", "error")], T0 + 140)
    w.muted = set()
    o2 = await run(sc, w, [card("PC-52", "error")], T0 + 200)
    ok("SC-37 음소거 중엔 후보 없음 → 풀리면 그때 운다", o == [] and keys(o2) == [("PC-52", "error")], "%s %s" % (o, o2))


async def t_wiring():
    """main._scout_tick 그대로 — 동기 _list_bug_files·음소거·장부 저장까지(HIGH-1 은 여기서만 보였다)."""
    sent, files, muted = [], [{"filename": "PC-61_20260927_090000_old_fail.png"}], set()

    async def fake_state(tenant):
        return [card("PC-61", "hunting", T0), card("PC-62", "error", T0)]

    async def fake_tg(chat, text):
        sent.append(text)
        return 1

    real = (main._build_full_state, main.tg_send_text, main.tg_enabled, main.tenant_chat_id, main._list_bug_files,
            main._tg_muted)
    main._build_full_state, main.tg_send_text = fake_state, fake_tg
    main.tg_enabled, main.tenant_chat_id = (lambda: True), (lambda t: "123")
    main._list_bug_files = lambda tenant, pc_id=None: list(files)        # 실물처럼 ★동기★
    main._tg_muted = lambda tenant, pc: 60.0 if pc in muted else 0.0
    try:
        main._SCOUT.__init__()
        main._SCOUT_LOADED[0] = False
        o1 = await main._scout_tick("main", now=T0)
        files.append({"filename": "PC-61_20260927_101000_PC-61_20260927_101001_captcha.png"})
        o2 = await main._scout_tick("main", now=T0 + 60)
        saved = json.loads(await db.get_setting("scout_alerted") or "{}")
        muted.add("PC-63")
        main._build_full_state = lambda tenant: _rows([card("PC-63", "error", T0)])
        o3 = await main._scout_tick("main", now=T0 + 120)
        muted.clear()

        async def fail_tg(chat, text):
            return None
        main.tg_send_text = fail_tg
        main._build_full_state = lambda tenant: _rows([card("PC-64", "error", T0)])
        o4 = await main._scout_tick("main", now=T0 + 180)
    finally:
        (main._build_full_state, main.tg_send_text, main.tg_enabled, main.tenant_chat_id, main._list_bug_files,
         main._tg_muted) = real
    ok("SC-38 배선: error → 🔭 스카우터 글로 텔레그램", keys(o1) == [("PC-62", "error")]
       and sent[0].startswith("PC-62 | 🔭 스카우터: PC-62 — 상태=error"), str(sent))
    ok("SC-39 ★배선 그대로 새 캡차 스샷 → 텔레그램(HIGH-1: 동기 목록이 죽이던 것)★", keys(o2) == [("PC-61", "bug:captcha")]
       and any("captcha" in s for s in sent), str(o2))
    ok("SC-40 장부는 ★보낸 것만★ 설정에 저장(재배포 뒤 도배 없음)", "PC-62:error" in saved and "PC-61:bug:captcha" in saved, str(saved))
    ok("SC-41 음소거 PC 는 텔레그램도 장부도 없음", o3 == [] and "PC-63:error" not in main._SCOUT.alerted, str(o3))
    ok("SC-45 ★텔레그램 전송 실패면 장부에 안 적는다 — 다음 틱에 다시(MED-6)★",
       o4 == [] and "PC-64:error" not in main._SCOUT.alerted, str(o4))
    ok("SC-42 함대 알람은 음소거를 안 탄다(FLEET_ID)", main._scout_muted("main", "PC-63") is False)   # muted 해제 뒤
    ok("SC-43 scout_alerted 는 서버 관리 설정(일반 POST 금지)", "scout_alerted" in main.SERVER_MANAGED_SETTINGS)
    ok("SC-44 서버가 쓰는 [스카우터] 줄은 「흐른다」 머리 목록에", "[스카우터]" in S.SERVER_LINE_HEADS)


async def t_round2():
    """아이온2 반증 2차(ad410e9) — HIGH 1 + LOW 3."""
    pcs = [f"PC-7{i}" for i in range(10)]
    sc, w = S.Scout(), World()
    await run(sc, w, [card(p) for p in pcs], T0)
    await run(sc, w, [card(p, "offline") for p in pcs], T0 + 10)
    o = await run(sc, w, [card(p, "offline") for p in pcs], T0 + 200)
    ok("SC-46 ★한 틱에 10대 사망 → 함대 알람 정확히 1통, 한 대씩은 0(2차 HIGH)★",
       keys(o) == [(S.FLEET_ID, "mass")] and all(p in o[0][2] for p in pcs), str(keys(o)))
    sc, w = S.Scout(), World()
    await run(sc, w, [card(p) for p in pcs], T0)
    await run(sc, w, [card(p, "offline") for p in pcs], T0 + 10)
    w.fail = True
    fails = [await run(sc, w, [card(p, "offline") for p in pcs], T0 + t) for t in (200, 260, 320)]
    w.fail = False
    sent = await run(sc, w, [card(p, "offline") for p in pcs], T0 + 380)
    sent += await run(sc, w, [card(p, "offline") for p in pcs], T0 + 440)
    ok("SC-47 ★텔레그램 3틱 실패 → 틱마다 후보 1개 · 복구 뒤 보낸 것 정확히 1통(쌓였다 쏟아지지 않음)★",
       [len(f) for f in fails] == [1, 1, 1] and keys(sent) == [(S.FLEET_ID, "mass")], str([keys(f) for f in fails] + [keys(sent)]))
    # LOW1 — 서버 줄은 시각·[텔레그램] 중계 머리에 싸여 온다
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-80")], T0)
    w.logs["PC-80"] = [(iso(T0 + 230), f"[2026-09-27 12:00:00] [텔레그램] 중계 전송: PC-80 | [순환] 다음 계정 대기")]
    await run(sc, w, [card("PC-80", "offline", T0)], T0 + 10)
    o = await run(sc, w, [card("PC-80", "offline", T0)], T0 + 241)
    ok("SC-48 ★시각·중계 머리에 싸인 서버 줄은 「로그 흐름」이 아니다 → 죽은 PC 는 운다(2차 LOW1)★",
       keys(o) == [("PC-80", "dead")], str(o))
    ok("SC-49 매크로가 쓴 중계 줄(머리 뒤가 서버 머리 아님)은 서버 줄이 아니다",
       not S.is_server_line("[2026-09-27 12:00:00] [텔레그램] 중계 전송: PC-80 | 🛑 사냥 정지")
       and S.is_server_line("[알람] PC-80 | x") and not S.is_server_line("사냥 중"))
    # LOW2 — 버그 알람은 보낸 뒤에만 「봤다」
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-81"), card("PC-82")], T0)
    w.bugs = ["PC-81_20260927_101000_captcha.png"]
    w.fail = True
    f1 = await run(sc, w, [card("PC-81"), card("PC-82")], T0 + 60)
    w.bugs.append("PC-82_20260927_101000_stuck_map.png")
    await run(sc, w, [card("PC-81"), card("PC-82", "idle")], T0 + 70)
    f2 = await run(sc, w, [card("PC-81"), card("PC-82", "idle")], T0 + 700)
    w.fail = False
    o = await run(sc, w, [card("PC-81"), card("PC-82", "idle")], T0 + 760)
    o2 = await run(sc, w, [card("PC-81"), card("PC-82", "idle")], T0 + 820)
    ok("SC-50 ★버그 알람 전송 실패 → 복구 뒤 다시 보낸다(즉시·유예 둘 다) · 그다음은 조용(2차 LOW2)★",
       keys(f1) == [("PC-81", "bug:captcha")] and ("PC-82", "bug:stuck_map") in keys(f2)
       and sorted(keys(o)) == [("PC-81", "bug:captcha"), ("PC-82", "bug:stuck_map")] and o2 == [] and not sc.bug_pending,
       str([keys(f1), keys(f2), keys(o), keys(o2)]))
    # LOW3 — 전환 실패 알람만 error 와 겹친다
    sc, w = S.Scout(), World()
    await run(sc, w, [card("PC-83"), card("PC-84")], T0)
    w.logs["PC-83"] = [(iso(T0 + 50), "[알람] PC-83 | 🧊 큐브 꽉참 5분 안 재발")]
    w.logs["PC-84"] = [(iso(T0 + 50), "[알람] PC-84 | ⚠️ 본컴 런처 전환 실패 — 4/12단계")]
    o = await run(sc, w, [card("PC-83", "error"), card("PC-84", "error")], T0 + 60)
    ok("SC-51 ★다른 매크로 알람은 error 를 삼키지 않는다 · 전환 실패 알람만 겹침 처리(2차 LOW3)★",
       keys(o) == [("PC-83", "error")], str(o))


async def _rows(x):
    return x


def test_all():
    run_all([t_offline, t_alive_exit_park, t_wsdead_and_merge, t_all_done_and_today, t_done_slot, t_error_and_bad_rows,
             t_bugs, t_mass, t_mark_only_on_send, t_wiring, t_round2])
    finish("test_scout_alarm", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
