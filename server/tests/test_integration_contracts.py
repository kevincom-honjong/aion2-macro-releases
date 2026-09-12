# -*- coding: utf-8 -*-
"""[통합 2026-09-12] 영역 간 연결 지점 — 서버 쪽 시험 (통합 세션이 만든 계약 6건).

각 영역 시험이 다 초록이어도 ★연결★ 이 깨지면 여기가 먼저 빨간불.
  I1 매크로 ack 본문 {status:"cancelled"} → 이력이 cancelled (CONTRACTS_대시보드 #1 · 매크로 report_module.notify_dropped)
  I2 FV silent_s · updater.age_s 는 항상 정수, 모름 = 10**9      (CONTRACTS_팜뷰 · 팜뷰 fvdash.group 대표 카드)
  I3 FV raw 는 ?raw=1 일 때만                                       (CONTRACTS_대시보드 #4 · 팜뷰는 raw 를 버린다)
  I4 FV pc:"all" 은 400 · 8대 이상은 confirm_fleet 필요             (CONTRACTS_대시보드 #3 · 팜뷰 fvdash.send_cmd)
  I5 /parsec/map 쓰기는 세션 로그인으로만(API 키·토큰 401)          (CONTRACTS_대시보드 #2 · 주인님 결정 2026-09-13)
  I6 순환 정보수집 상한이 6캐릭 28.5분(PC-07 실측)을 넘긴다          (SHARED_ISSUES_아이온2 #1)
"""
import os
import sys

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 16


async def t_ack_cancelled():
    cid = await db.insert_command("PC-I1", "start", {})
    r = await main.ack_cmd("PC-I1", cid, Req({"status": "cancelled", "why": "부팅 드레인(시험)"}))
    ok("I1 ack 본문 cancelled 를 200 으로 받는다", getattr(r, "status_code", 200) == 200)
    rows = await db.get_recent_commands(50)
    row = next((x for x in rows if x.get("id") == cid), None)
    ok("I1 이력 status 가 cancelled 다", row is not None and row.get("status") == "cancelled", str(row))
    # 본문 없는 옛 ack 는 그대로 acked
    cid2 = await db.insert_command("PC-I1", "stop", {})
    r2 = await main.ack_cmd("PC-I1", cid2, Req({}))
    rows = await db.get_recent_commands(50)
    row2 = next((x for x in rows if x.get("id") == cid2), None)
    ok("I1 본문 없는 ack 는 예전처럼 acked", row2 is not None and row2.get("status") == "acked", str(row2))
    # 이미 acked 인 것을 뒤늦게 cancelled 로 못 뒤집는다(§A12 — cancel_command 와 같은 규칙)
    r3 = await main.ack_cmd("PC-I1", cid2, Req({"status": "cancelled", "why": "늦은 취소"}))
    rows = await db.get_recent_commands(50)
    row3 = next((x for x in rows if x.get("id") == cid2), None)
    ok("I1 acked 뒤의 cancelled 는 무시된다(pending 일 때만)", row3 is not None and row3.get("status") == "acked", str(row3))


def t_fv_ints():
    v = main._fv_pc_view({"pc_id": "PC-I2", "status": "idle", "_macro_silent_s": -1, "daily_progress": []})
    ok("I2 silent_s 가 -1 이면 10**9 로 나간다", v["silent_s"] == main.FV_UNKNOWN_S, str(v["silent_s"]))
    ok("I2 updater.age_s 가 없으면 10**9", v["updater"]["age_s"] == main.FV_UNKNOWN_S, str(v["updater"]))
    v2 = main._fv_pc_view({"pc_id": "PC-I2", "status": "idle", "_macro_silent_s": 7, "_updater_age_s": 3, "daily_progress": []})
    ok("I2 정상 값은 그대로 정수", v2["silent_s"] == 7 and v2["updater"]["age_s"] == 3)
    ok("I2 둘 다 int 타입", isinstance(v["silent_s"], int) and isinstance(v2["updater"]["age_s"], int))


def t_fv_raw():
    body = {"pcs": {"PC-A": {"pc_id": "PC-A", "raw": {"secret": 1}, "status": "idle"}}, "global": {}}
    r = Req(api_key=None)
    stripped = main._fv_maybe_raw(r, body)
    ok("I3 기본은 raw 가 빠진다", "raw" not in stripped["pcs"]["PC-A"], str(stripped["pcs"]["PC-A"].keys()))
    ok("I3 원본(캐시)은 안 건드린다", "raw" in body["pcs"]["PC-A"])
    r.query_params = {"raw": "1"}
    kept = main._fv_maybe_raw(r, body)
    ok("I3 ?raw=1 이면 그대로", "raw" in kept["pcs"]["PC-A"])


async def t_fv_all():
    main.FV_TOKEN = "fvsecret"
    H = {"X-FV-Token": "fvsecret"}
    r = await main.fv_command_send(Req({"cmd": "start", "pc": "all"}, api_key=None, headers=H))
    ok("I4 맨몸 pc:'all' 은 400", getattr(r, "status_code", 0) == 400, str(getattr(r, "status_code", None)))
    body = r.body.decode("utf-8", "replace") if hasattr(r, "body") else ""
    ok("I4 400 문구가 confirm_fleet 를 안내한다", "confirm_fleet" in body, body[:120])
    big = ["PC-%02d" % i for i in range(1, 9)]
    r2 = await main.fv_command_send(Req({"cmd": "start", "pc": big}, api_key=None, headers=H))
    b2 = r2.body.decode("utf-8", "replace") if hasattr(r2, "body") else ""
    ok("I4 8대 목록에 confirm_fleet 없으면 400", getattr(r2, "status_code", 0) == 400 and "confirm_fleet" in b2, b2[:120])
    r3 = await main.fv_command_send(Req({"cmd": "start", "pc": ["PC-01"]}, api_key=None, headers=H))
    b3 = r3.body.decode("utf-8", "replace") if hasattr(r3, "body") else ""
    ok("I4 1대는 confirm_fleet 없이도 400 이 아니다(모르는 PC 면 그 사유로 답한다)", "confirm_fleet" not in b3, b3[:120])


async def t_parsec_token():
    """주인님 결정 2026-09-13 — 파섹 주소록 ★쓰기★ 는 세션 로그인으로만. API 키·토큰 헤더로는 401."""
    from fastapi import HTTPException
    try:
        await main.set_parsec_map(Req({"map": {"8": "peer8"}}, api_key="testkey"))
        ok("I5 API 키로는 주소록을 못 덮는다(401)", False, "예외 없이 통과했다")
    except HTTPException as e:
        ok("I5 API 키로는 주소록을 못 덮는다(401)", e.status_code == 401, str(e.status_code))
    try:
        await main.set_parsec_map(Req({"map": {"8": "peer8"}}, api_key=None, headers={"X-Parsec-Token": "x"}))
        ok("I5 토큰 헤더 길은 없다(401)", False, "예외 없이 통과했다")
    except HTTPException as e:
        ok("I5 토큰 헤더 길은 없다(401)", e.status_code == 401, str(e.status_code))
    r = await main.set_parsec_map(Req({"map": {"8": "peer8"}}, api_key=None, session=main.new_session("main")))
    ok("I5 세션 로그인은 통과", getattr(r, "status_code", 200) == 200, str(getattr(r, "status_code", None)))
    ok("I5 서버에 PARSEC_MAP_TOKEN 길이 남아 있지 않다", not hasattr(main, "_parsec_map_token_tenant"))


def t_collect_cap():
    # _rot_collect_max 는 ★카드 목록★ 을 받고 daily_progress 길이로 캐릭 수를 센다
    cards6 = [{"pc_id": "PC-07", "daily_progress": [{"slot": i + 1} for i in range(6)]}]
    cap6 = main._rot_collect_max(cards6) if hasattr(main, "_rot_collect_max") else None
    ok("I6 6캐릭 상한이 PC-07 실측 28.5분(1710초)을 넘긴다", cap6 is not None and cap6 >= 1710, str(cap6))
    ok("I6 상한은 절대 최대(2400)를 넘지 않는다", cap6 is not None and cap6 <= main.ROT_COLLECT_HARD_MAX, str(cap6))


run_all([t_ack_cancelled, t_fv_ints, t_fv_raw, t_fv_all, t_parsec_token, t_collect_cap])
finish("test_integration_contracts", MIN_CHECKS)
