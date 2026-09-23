# -*- coding: utf-8 -*-
"""[대시보드] B2 묶음(2026-09-23) — 메모리 상태·영속본 확정 버그 시험.

반증 에이전트 repro(hunt2/mem·time)가 잡은 것을 ★실제 호출★ 로 고정한다.
각 검사는 고치기 전 서버(r2base)에서는 FAIL, 고친 서버에서는 PASS 여야 한다.
  B2-6   회랑 영속본을 [:200000] 으로 잘라 깨진 JSON → 재배포 때 전 테넌트 소실 · 필드 무제한 · 상한 없음
  B2-7   내부망 캐시도 같은 자르기 · /report lan_url 길이·모양 검사 없음
  B2-10  한 PC 의 daily_progress [null] 이 /summary·FV 스냅샷 500 + 뒤 PC today 누락
  B2-11  mark_seen 4000 초과 시 clear() 가 남의 테넌트 생존 기록까지 지움
  B2-lo  _ROT_BOOT · _LOG_SINCE_PRUNE 무한 증식 · 수집 시각 +09:00 오프셋을 버림
"""
import asyncio
import json
import types
import uuid
from datetime import timedelta

from _harness import main, db, ok, Req, run_all, finish, HTTPException   # noqa: E402

MIN_CHECKS = 15
LEGIT = {"our": {"lower": 3, "middle": 2},
         "slots": {str(s): {"lower": 1, "middle": 0} for s in range(1, 9)},
         "remaining": 5, "total": 8, "reset_key": "2026-09-20"}
GOOD_LAN = "http://172.30.1.%d:8765/?k=abcDEF0123-_xyz"


async def _nob(*a, **k):
    return None


def _sess():
    return Req(api_key=None, session=main.new_session("main"))


async def t_corridor():
    main.manager.broadcast = _nob
    main.TENANTS.setdefault("friend", {"name": "friend"})
    main.KEY_TO_TENANT["friendkey"] = "friend"
    await main.save_corridor_progress("PC-01", Req(dict(LEGIT), api_key="friendkey"))
    for i in range(1, 25):
        await main.save_corridor_progress("PC-%02d" % i, Req(dict(LEGIT)))
    big = dict(LEGIT)
    big["junk"] = "A" * 300_000
    await main.save_corridor_progress("PC-BIG", Req(big))
    ok("B2-6 PC 가 보낸 모르는 필드(junk 30만 자)는 저장하지 않는다",
       "junk" not in (main.CORRIDOR_PROG.get("PC-BIG") or {}) and
       (main.CORRIDOR_PROG.get("PC-BIG") or {}).get("remaining") == 5)
    for i in range(260):                               # 같은 테넌트 매크로가 id 를 바꿔가며
        await main.save_corridor_progress("PC-X%d" % i, Req(dict(LEGIT)))
    n_main = sum(1 for k in main.CORRIDOR_PROG if main.ns_of(k) == "main")
    fk = main.ns("friend", "PC-01")
    ok("B2-6 테넌트별 상한(≤200) — 넘친 테넌트만 줄고 남의 테넌트 칸은 그대로",
       n_main <= 200 and fk in main.CORRIDOR_PROG, "main=%d friend=%s" % (n_main, fk in main.CORRIDOR_PROG))
    raw = await db.get_setting(main.CORRIDOR_KEY)
    try:
        json.loads(raw)
        good = True
    except Exception:
        good = False
    main.CORRIDOR_PROG.clear()
    await main._corridor_restore()
    ok("B2-6 영속본이 온전한 JSON 이고 재배포 뒤 최근 PC·남의 테넌트가 복원된다",
       good and "PC-X259" in main.CORRIDOR_PROG and fk in main.CORRIDOR_PROG,
       "json=%s n=%d" % (good, len(main.CORRIDOR_PROG)))
    try:
        await main.save_corridor_progress("PC-02", Req(["not", "a", "dict"]))
        code = 200
    except HTTPException as e:
        code = e.status_code
    except Exception as e:
        code = type(e).__name__
    ok("B2-6 객체가 아닌 본문은 500 이 아니라 400", code == 400, str(code))


async def _run_lan_saver_once():
    real = main.asyncio
    n = [0]

    async def fake_sleep(s):
        n[0] += 1
        if n[0] > 1:
            raise asyncio.CancelledError
    main.asyncio = types.SimpleNamespace(sleep=fake_sleep, CancelledError=asyncio.CancelledError)
    try:
        await main._lan_cache_saver()
    finally:
        main.asyncio = real


async def t_lan():
    main.push_state = _nob
    for i in range(1, 25):
        await main.receive_report("PC-%02d" % i, Req({"status": "hunting", "lan_url": GOOD_LAN % i}))
    bad = "http://1.2.3.4:8765/?k=" + "a" * 250_000
    await main.receive_report("PC-25", Req({"status": "hunting", "lan_url": bad}))
    rows = {r["pc_id"]: r for r in await main._build_full_state("main")}
    ok("B2-7 /report 의 이상한 lan_url(25만 자)은 카드·캐시에 안 들어가고 정상 주소는 그대로",
       not (rows.get("PC-25") or {}).get("lan_url") and main._lan_cache_last.get("PC-25") is None
       and main._lan_cache_last.get("PC-03") == GOOD_LAN % 3)
    await main.receive_report("PC-26", Req({"status": "hunting", "lan_url": "javascript:alert(1)//"}))
    await main._build_full_state("main")
    await _run_lan_saver_once()
    raw = await db.get_setting(main.LAN_CACHE_KEY)
    try:
        json.loads(raw)
        good = True
    except Exception:
        good = False
    main._lan_cache_last.clear()
    await main._lan_cache_restore()
    ok("B2-7 내부망 캐시 영속본이 온전하고 재기동 뒤 정상 24건이 복원된다(이상값 0)",
       good and len(main._lan_cache_last) == 24 and "PC-26" not in main._lan_cache_last,
       "json=%s n=%d" % (good, len(main._lan_cache_last)))


async def t_daily_progress():
    main.push_state = _nob
    old = "2020-01-01 10:00:00"
    await main.receive_report("PC-01", Req({"status": "hunting", "daily_progress": [None]}))
    await main.receive_report("PC-02", Req({"status": "hunting",
                                            "daily_progress": [{"slot": 1, "completed": True, "completed_time": old}]}))
    await main.receive_report("PC-03", Req({"status": "hunting", "daily_progress": "abc"}))
    rows = {r["pc_id"]: r for r in await main._build_full_state("main")}
    dp2 = (rows.get("PC-02") or {}).get("daily_progress") or [{}]
    ok("B2-10 앞 PC 가 [null] 이어도 뒤 PC 의 어제 완주에 today=False 가 붙는다",
       dp2[0].get("today") is False, str(dp2))
    ok("B2-10 모양이 틀린 daily_progress 는 객체 칸만 남긴 목록이 된다",
       rows["PC-01"].get("daily_progress") == [] and rows["PC-03"].get("daily_progress") == [],
       "%r %r" % (rows["PC-01"].get("daily_progress"), rows["PC-03"].get("daily_progress")))
    try:
        r = await main.dashboard_summary(_sess())
        code = r.status_code
    except Exception as e:
        code = type(e).__name__
    ok("B2-10 /summary 가 500 이 아니다", code == 200, str(code))
    try:
        snap = await main._fv_build_snapshot("main")
        good = isinstance(snap, dict) and "global" in snap
    except Exception as e:
        good = False
        print("   ", type(e).__name__, e)
    ok("B2-10 FV 스냅샷이 터지지 않는다", good)


async def t_last_seen():
    main.TENANTS.setdefault("friend", {"name": "friend"})
    main.KEY_TO_TENANT["friendkey"] = "friend"
    main._last_seen.clear()
    main.mark_seen("PC-07")
    for i in range(4000):                             # 주인 1 + 4000 = 4001칸 — 마지막 요청이 정리를 부른다
        main.mark_seen(main.ns("friend", "PC-R%d" % i))
    ok("B2-11 남의 테넌트 id 폭주가 주인 함대 PC-07 생존 기록을 안 지운다", main.seen_fresh("PC-07", 180))
    ok("B2-11 방금 온 요청(폭주 테넌트 마지막 id)도 살아 있다",
       main.seen_fresh(main.ns("friend", "PC-R3999"), 180))


def t_rot_boot():
    main._ROT_BOOT.clear()
    armed = main.ns("main", "PC-09")
    main._ROT[armed] = {"stage": "hunting"}
    main._ROT_BOOT[armed] = {"id": "", "at": 1.0}          # 가장 오래된 칸 — 그래도 지우면 안 된다
    try:
        for i in range(2600):
            main._rot_note_boot("PC-Z%d" % i, "[BOOT#%s] start" % uuid.uuid4().hex)
        ok("B2 _ROT_BOOT 상한(≤2000) · 무장된 PC 칸은 남는다",
           len(main._ROT_BOOT) <= 2000 and armed in main._ROT_BOOT, str(len(main._ROT_BOOT)))
    finally:
        main._ROT.pop(armed, None)


async def t_log_since_prune():
    db._LOG_SINCE_PRUNE.clear()
    for i in range(2100):
        await db.insert_log("PC-L%d" % i, "info", "x")
    ok("B2 _LOG_SINCE_PRUNE 칸이 pc_id 수만큼 무한히 늘지 않는다(≤2000)",
       len(db._LOG_SINCE_PRUNE) <= 2000, str(len(db._LOG_SINCE_PRUNE)))


def t_ticket_offset():
    R = main._fv_last_weekly_reset_utc()
    s = (R - timedelta(hours=1)).astimezone(main._KST_TZ).isoformat(timespec="seconds")   # 리셋 1시간 전(+09:00)
    ok("B2 수집 시각의 +09:00 오프셋을 버리지 않는다(리셋 전 수집 → 가득 참 3)",
       main._fv_ticket_reset_aware(s, 0, 3) == 3, s)


def test_all():
    run_all([t_corridor, t_lan, t_daily_progress, t_last_seen, t_rot_boot, t_log_since_prune, t_ticket_offset])
    finish("test_state_b2", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
