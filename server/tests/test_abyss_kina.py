# -*- coding: utf-8 -*-
"""[대시보드] 어비스 수익 전광판 + 카드 줄 (2026-09-23 주인님 장부 #104) 회귀 가드 — 실제 호출.

  A  전광판 「어비스 수익 (오늘)」 — 서버가 PC(물리) 마다 구간(since)을 은행에 쌓는다
     A1 구간이 바뀌면 앞 구간의 마지막 gain 을 입금   A2 재전송(같은 since·더 작은 gain·옛 since)은 멱등
     A3 KST 00:00 에 오늘 합계 초기화(자정 전 몫은 어제)   A4 재배포(설정 KV 에서 복원) 뒤에도 그대로
     A5 옛 매크로 글자 칸 abyss_kina 폴백 · 쓸 것 없으면 「측정 대기」(null) — ★0 이 아니다★
     A6 「시간당」 = state ok·mins≥문턱 PC 합 · 대당 평균 · N대 측정 / M대 대기 · 빨강 문턱(설정)
     A7 가짜·은퇴·계정없음 제외(다른 합계와 같은 _fv_pc_excluded) · 계정 카드(PC-03b)는 물리 PC 하나로
     A8 /summary = 스냅샷 global.totals, 새 값은 totals.abyss 한 키에만
  B  카드 「💰 어비스」 줄(JS, node 로 실행 · 한국/베트남 두 시간대)
     ok → 「+N · 시간당 M (HH:MM부터)」 · 빨강은 rate<문턱 ★그리고★ mins≥문턱 · 그 전엔 중립 「(측정 N분)」
     waiting → mins<문턱이면 회색 「측정 중 (HH:MM부터)」, 문턱 넘으면 gain/mins 로 ok 처럼(R5) · HH:MM 은 KST(R7)
     숫자 칸 없으면 옛 글자 그대로 · 전광판 칸 「측정 대기」
  R  적대 검증 재현(refute_abyss) — R1 끝 글자 다음 날 재입금 · R2 미래 since · R3 상한 · R4 옛 0 수익 생존
     R5 waiting 10분 · R6 옛 시계 빠름 이중 집계 · R7 KST(B-1) · R8 자정 뒤 stale → 0 · R9 옛 gain 감소 · X 가짜·음수

    cd updater/server && python -X utf8 tests/test_abyss_kina.py
"""
import json
from datetime import datetime

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402
from test_js_b import _js_func, _run_node, _DOM, _need_node   # noqa: E402

MIN_CHECKS = 127    # 2026-09-24 주인님 #159 +13(L 옛 글자 빨강·음수 두 시간대 5×2 + 서버 3) · v4 반증 3차 +3(V3 깨진 저장본) · 2026-09-24 이식 +1(A8-e2) +4(S2 관리자 명세 2) · 2026-09-23 실측값(77 + 적대 검증 R1~R9·기타 29) — 검사가 조용히 빠지면 빨간불

KST = main._KST_TZ if hasattr(main, "_KST_TZ") else None


def _kst(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=KST).timestamp()


def _rep(since, gain, rate=0, mins=0, state="ok"):
    return {"abyss_kina_state": state, "abyss_kina_gain": gain, "abyss_kina_rate": rate,
            "abyss_kina_since": int(since), "abyss_kina_mins": mins}


def _today(tenant="main", now=None, red=1_000_000, mins=5):
    return main._abyss_billboard(tenant, now, red, mins)


def _need(*names):
    miss = [n for n in names if not hasattr(main, n)]
    if miss:
        ok("A-0 서버 함수가 있다: " + ", ".join(miss), False)
        return False
    return True


# ───────────────── A1·A2 — 구간 은행 · 재전송 멱등 ─────────────────

def t_bank_and_idempotent():
    if not _need("_abyss_ingest", "_abyss_billboard", "ABYSS_ACC"):
        return
    main.ABYSS_ACC.clear()
    t0 = _kst(2026, 9, 23, 10, 0)
    s1, s2, s3 = int(t0), int(t0 + 3600), int(t0 + 7200)
    ing = main._abyss_ingest
    ing("PC-01", _rep(s1, 100, 600_000, 1), now=t0 + 60)
    ing("PC-01", _rep(s1, 300, 1_800_000, 10), now=t0 + 600)
    ok("A1-a 같은 구간 안에서는 마지막 gain(300)이 오늘", _today(now=t0 + 601)["today"] == 300,
       str(_today(now=t0 + 601)))
    ing("PC-01", _rep(s2, 50, 0, 0, "waiting"), now=s2 + 5)
    ok("A1-b since 가 바뀌면 앞 구간 300 을 입금 + 새 구간 50 = 350", _today(now=s2 + 6)["today"] == 350,
       str(main.ABYSS_ACC.get("PC-01")))
    ing("PC-01", _rep(s2, 80, 480_000, 10), now=s2 + 600)
    ok("A1-c 새 구간이 자라면 380", _today(now=s2 + 601)["today"] == 380)
    for _ in range(3):
        ing("PC-01", _rep(s2, 80, 480_000, 10), now=s2 + 620)
    ok("A2-a 같은 보고 세 번 → 그대로 380", _today(now=s2 + 621)["today"] == 380)
    ing("PC-01", _rep(s2, 60, 360_000, 9), now=s2 + 630)
    rec = main.ABYSS_ACC.get("PC-01") or {}
    ok("A2-b 같은 since·더 작은 gain(늦은 재전송) → 줄지 않는다(380)", _today(now=s2 + 631)["today"] == 380, str(rec))
    ok("A2-c 그 늦은 보고가 표시 칸(rate·mins)도 되돌리지 않는다", rec.get("rate") == 480_000 and rec.get("mins") == 10,
       str(rec))
    how = ing("PC-01", _rep(s1, 999, 1, 50), now=s2 + 640)
    ok("A2-d 옛 since(앞 구간) 재전송 → 무시(stale), 재입금 없음(380)",
       how == "stale" and _today(now=s2 + 641)["today"] == 380, how)
    ing("PC-01", _rep(s3, 10, 0, 0, "waiting"), now=s3 + 5)
    ok("A2-e 다음 구간으로 넘어가면 앞 구간 80 을 ★한 번만★ 입금(300+80+10=390)",
       _today(now=s3 + 6)["today"] == 390, str(main.ABYSS_ACC.get("PC-01")))
    ing("PC-01", _rep(s2, 80, 480_000, 10), now=s3 + 30)
    ing("PC-01", _rep(s3, 10, 0, 0, "waiting"), now=s3 + 40)
    ok("A2-f 구간이 넘어간 뒤 옛 구간이 또 와도 390 그대로", _today(now=s3 + 41)["today"] == 390)
    ok("A2-g 매크로 0·음수·문자 gain 은 0 으로(구간 규칙 안 깨짐)",
       main._abyss_int(-5) is None and main._abyss_int(True) is None and main._abyss_int("x") is None)
    main.ABYSS_ACC.clear()


# ───────────────── A3 — KST 00:00 초기화 ─────────────────

def t_kst_rollover():
    if not _need("_abyss_ingest", "_abyss_billboard"):
        return
    main.ABYSS_ACC.clear()
    ing = main._abyss_ingest
    s = _kst(2026, 9, 23, 23, 30)
    ing("PC-02", _rep(s, 1000, 3_000_000, 20), now=_kst(2026, 9, 23, 23, 50))
    ing("PC-02", _rep(s, 1500, 3_000_000, 29), now=_kst(2026, 9, 23, 23, 59))
    b = _today(now=_kst(2026, 9, 23, 23, 59, 30))
    ok("A3-a 23:59 KST — 오늘(9/23) 1500", b["today"] == 1500 and b["day"] == "2026-09-23", str(b))
    b = _today(now=_kst(2026, 9, 24, 0, 0, 30))
    ok("A3-b 00:00 지나 아직 보고 없음 → 「측정 대기」(null) — 어제 1500 을 오늘로 안 센다",
       b["today"] is None and b["day"] == "2026-09-24", str(b))
    ing("PC-02", _rep(s, 1700, 3_000_000, 31), now=_kst(2026, 9, 24, 0, 1))
    b = _today(now=_kst(2026, 9, 24, 0, 1, 30))
    ok("A3-c 자정을 넘은 구간은 자정 뒤 몫(1700-1500=200)만 오늘", b["today"] == 200, str(b))
    ing("PC-02", _rep(_kst(2026, 9, 24, 0, 5), 40, 0, 0, "waiting"), now=_kst(2026, 9, 24, 0, 5, 5))
    b = _today(now=_kst(2026, 9, 24, 0, 6))
    ok("A3-d 00:05 새 구간 → 앞 구간 오늘 몫 200 입금 + 40 = 240", b["today"] == 240, str(b))
    ing("PC-02", _rep(_kst(2026, 9, 24, 0, 5), 90, 0, 1, "waiting"), now=_kst(2026, 9, 25, 0, 0, 10))
    b = _today(now=_kst(2026, 9, 25, 0, 0, 20))
    ok("A3-e 다음 날 00:00 — 어제 은행(240)이 비고 새 몫만(90-40=50)", b["today"] == 50 and b["day"] == "2026-09-25",
       str(b))
    # 처음 보는 구간이 어제 시작 — 어제 몫을 오늘로 세지 않는다(배포 첫날)
    main.ABYSS_ACC.clear()
    ing("PC-03", _rep(_kst(2026, 9, 23, 22, 0), 5000, 2_000_000, 150), now=_kst(2026, 9, 24, 0, 30))
    ing("PC-03", _rep(_kst(2026, 9, 23, 22, 0), 5600, 2_000_000, 151), now=_kst(2026, 9, 24, 0, 31))
    b = _today(now=_kst(2026, 9, 24, 0, 31, 30))
    ok("A3-f 어제 시작한 구간을 오늘 처음 보면 본 뒤 늘어난 몫(600)만", b["today"] == 600, str(b))
    main.ABYSS_ACC.clear()


# ───────────────── A4 — 재배포 뒤 복원 · /report 입구 ─────────────────

async def t_persist_and_report():
    if not _need("_abyss_ingest", "_abyss_persist", "_abyss_restore", "_abyss_billboard"):
        return
    import time as _t
    main.ABYSS_ACC.clear()
    now = _t.time()
    s = int(max(now - 1800, main._abyss_day_start(now) + 1))   # 00:00~00:30 KST 에 돌려도 「오늘 시작」 구간
    s2 = max(s + 1, min(s + 1500, int(now)))   # 둘째 구간 — 00:00~00:25 에 돌려도 미래 since(R2 가 버림)가 안 되게
    await main.receive_report("PC-04", Req(body=dict(_rep(s, 7000, 2_500_000, 30), status="abyss")))
    ok("A4-a /report 입구가 쌓는다(PC-04 gain 7000)", (main.ABYSS_ACC.get("PC-04") or {}).get("gain") == 7000,
       str(main.ABYSS_ACC.get("PC-04")))
    raw = await db.get_setting(main.ABYSS_ACC_KEY)
    ok("A4-b 새 구간(입금 사건)은 바로 볼륨 DB 설정에 저장", bool(raw) and "PC-04" in json.loads(raw), str(raw)[:120])
    await main.receive_report("PC-04", Req(body=dict(_rep(s2, 300, 0, 0, "waiting"), status="abyss")))
    before = _today(now=_t.time())
    await main._abyss_persist()
    main.ABYSS_ACC.clear()
    ok("A4-c (재배포 흉내) 메모리를 비우면 오늘 합계가 사라진다", _today(now=_t.time())["today"] is None)
    await main._abyss_restore()
    after = _today(now=_t.time())
    ok("A4-d 설정 KV 에서 복원 → 오늘 합계 그대로(7300)", after["today"] == before["today"] == 7300,
       "%s → %s" % (before["today"], after["today"]))
    await main.receive_report("PC-04", Req(body=dict(_rep(s2, 300, 0, 0, "waiting"), status="abyss")))
    await main.receive_report("PC-04", Req(body=dict(_rep(s, 7000, 2_500_000, 30), status="abyss")))
    ok("A4-e 복원 뒤 재전송(같은 구간·옛 구간)이 다시 입금하지 않는다(7300)", _today(now=_t.time())["today"] == 7300)
    # 볼륨이 없어도(설정이 깨진 JSON) 죽지 않는다
    await db.set_setting(main.ABYSS_ACC_KEY, "{깨짐")
    main.ABYSS_ACC.clear()
    try:
        await main._abyss_restore()
        ok("A4-f 깨진 저장본이어도 복원이 죽지 않고 비어서 시작", main.ABYSS_ACC == {})
    except Exception as e:
        ok("A4-f 깨진 저장본이어도 복원이 죽지 않고 비어서 시작", False, str(e))
    ok("A4-g lifespan 이 복원·저장 일꾼을 건다",
       "_abyss_restore()" in _src_of(main.lifespan) and "_abyss_saver()" in _src_of(main.lifespan))
    ok("A4-h WS status 입구도 같은 함수를 부른다(두 입구 §A12)", _src_count("await _abyss_note(nspc,") == 2,
       str(_src_count("await _abyss_note(nspc,")))
    main.ABYSS_ACC.clear()


def _src_of(fn):
    import inspect
    try:
        return inspect.getsource(fn)
    except Exception:
        return ""


def _src_count(s):
    return open(main.__file__, encoding="utf-8").read().count(s)


# ───────────────── A5 — 옛 매크로 글자 칸 · 측정 대기 ─────────────────

def t_legacy():
    if not _need("_abyss_ingest", "_abyss_billboard", "_abyss_parse"):
        return
    main.ABYSS_ACC.clear()
    now = _kst(2026, 9, 23, 22, 0)
    p = main._abyss_parse({"abyss_kina": "+1,234,567 키나 · 시간당 2,000,000 (21:30부터)"}, now)
    ok("A5-a 1.1.1003 실시간 글자 → ok·gain·rate·since(KST 21:30)·mins 30",
       p and p["state"] == "ok" and p["gain"] == 1234567 and p["rate"] == 2000000
       and p["since"] == int(_kst(2026, 9, 23, 21, 30)) and p["mins"] == 30, str(p))
    p = main._abyss_parse({"abyss_kina": "+5,000 키나 · 시간당 60,000 (21:55~)"}, now)
    ok("A5-b 옛 「(HH:MM~)」 꼴도 읽는다", p and p["since"] == int(_kst(2026, 9, 23, 21, 55)) and p["rate"] == 60000, str(p))
    p = main._abyss_parse({"abyss_kina": "-2,000 키나 · 40분 (21:00부터)"}, now)
    ok("A5-c 끝난 구간(음수) → gain 0 · mins 40", p and p["gain"] == 0 and p["mins"] == 40, str(p))
    p = main._abyss_parse({"abyss_kina": "정산 중 (21:58부터)"}, now)
    ok("A5-d 「정산 중」 → waiting", p and p["state"] == "waiting" and p["since"] == int(_kst(2026, 9, 23, 21, 58)), str(p))
    p = main._abyss_parse({"abyss_kina": "+10 키나 · 시간당 20 (23:50부터)"}, now)
    ok("A5-e 미래 시각(23:50)이면 어제 23:50", p and p["since"] == int(_kst(2026, 9, 22, 23, 50)), str(p))
    for txt in ("정산 대기 (시작 판독 재시도 중)", "정산 생략 (12분 · 판독 없음)", "측정 실패 (3분)", "측정 불가 (시작 판독 실패)"):
        main._abyss_ingest("PC-05", {"abyss_kina": txt}, now=now)
    b = _today(now=now)
    ok("A5-f 쓸 것 없는 글자만 온 함대 → 오늘·시간당 둘 다 null(「측정 대기」, 0 아님)",
       b["today"] is None and b["rate_sum"] is None and b["rate_avg"] is None, str(b))
    main._abyss_ingest("PC-06", {"abyss_kina": "+3,000,000 키나 · 시간당 1,500,000 (20:00부터)"}, now=now)
    b = _today(now=now + 1)
    ok("A5-g 옛 매크로 실시간 글자도 오늘·시간당에 들어간다(120분 ≥ 5분)",
       b["today"] == 3000000 and b["rate_sum"] == 1500000 and b["rate_n"] == 1, str(b))
    ok("A5-h 새 숫자 칸이 있으면 글자보다 숫자 칸이 이긴다",
       main._abyss_parse(dict(_rep(now - 600, 7, 8, 10), abyss_kina="+1 키나 · 시간당 1 (21:30부터)"), now)["gain"] == 7)
    main.ABYSS_ACC.clear()


# ───────────────── A6·A7 — 시간당 · 제외 · 문턱 설정 ─────────────────

async def t_rate_and_exclusion():
    if not _need("_abyss_ingest", "_abyss_billboard", "_abyss_thresholds"):
        return
    main.ABYSS_ACC.clear()
    now = _kst(2026, 9, 23, 15, 0)
    ing = main._abyss_ingest
    ing("PC-10", _rep(now - 3600, 1_000_000, 1_000_000, 60), now=now)
    ing("PC-11", _rep(now - 1800, 200_000, 400_000, 30), now=now)
    ing("PC-12", _rep(now - 180, 50_000, 1_000_000, 3), now=now)            # 3분 < 5분 → 대기
    ing("PC-13", _rep(now - 60, 0, 0, 0, "waiting"), now=now)
    ing("PC-14", _rep(now - 7200, 9_000_000, 9_000_000, 60), now=now - 900)  # 15분째 안 바뀜 → 끝난 구간
    ing("PC-15", _rep(now - 600, 777, 5_000_000, 10), now=now)              # 은퇴
    ing("PC-16", _rep(now - 600, 888, 5_000_000, 10), now=now)              # 계정없음
    ing("PC-TEST", _rep(now - 600, 999, 5_000_000, 10), now=now)            # 가짜
    ing("PC-17", _rep(now - 600, 100, 3_000_000, 10), now=now)
    ing("PC-17b", _rep(now - 600, 100, 3_000_000, 10), now=now)             # 같은 물리 PC · 같은 구간
    th0 = await main._abyss_thresholds("main")
    await db.set_setting(main.ns("main", "abyss_red_rate"), "2,000,000")
    await db.set_setting(main.ns("main", "abyss_min_mins"), "abc")
    th = await main._abyss_thresholds("main")
    await db.set_setting(main.ns("main", "abyss_red_rate"), "")
    old_r, old_n = set(main.RETIRED_PCS), set(main.NO_ACCOUNT_PCS)
    main.RETIRED_PCS.add("PC-15")
    main.NO_ACCOUNT_PCS.add("PC-16")
    try:
        b = _today(now=now + 1)
        b2 = main._abyss_billboard("main", now + 1, th[0], th[1])
        b3 = main._abyss_billboard("main", now + 1, 1_000_000, 40)
    finally:
        main.RETIRED_PCS.clear()
        main.RETIRED_PCS.update(old_r)
        main.NO_ACCOUNT_PCS.clear()
        main.NO_ACCOUNT_PCS.update(old_n)
    ok("A6-a 시간당 합 = ok·5분 이상·지금 재는 PC(10·11·17) = 1,000,000+400,000+3,000,000",
       b["rate_sum"] == 4_400_000 and b["rate_n"] == 3, str(b))
    ok("A6-b 대당 평균 = 합 / 3", b["rate_avg"] == 4_400_000 // 3, str(b["rate_avg"]))
    ok("A6-c 「M대 대기」 = 3분째(12) + waiting(13) = 2", b["wait_n"] == 2, str(b))
    ok("A6-d 끝난 구간(15분째 안 바뀜, 14)은 시간당·대수에서 빠진다(오늘 합계엔 남는다)",
       b["rate_n"] + b["wait_n"] == 5 and b["today"] >= 9_000_000, str(b))
    ok("A7-a 은퇴·계정없음·가짜 PC 는 오늘 합계에서 빠진다(777·888·999 없음)",
       b["today"] == 1_000_000 + 200_000 + 50_000 + 0 + 9_000_000 + 100, str(b["today"]))
    ok("A7-b 계정 카드 PC-17·PC-17b 같은 구간 → 물리 PC 한 대로 한 번만(100)",
       "PC-17b" not in main.ABYSS_ACC and (main.ABYSS_ACC.get("PC-17") or {}).get("gain") == 100)
    ok("A6-e 평균 1,466,666 ≥ 기본 문턱 100만 → 빨강 아님", b["red"] is False and b["red_rate"] == 1_000_000)
    ok("A6-f 기본 문턱 설정값(설정 없음) = 1,000,000 · 5분", tuple(th0) == (1_000_000, 5), str(th0))
    ok("A6-g 설정 abyss_red_rate(쉼표 허용)·abyss_min_mins(숫자 아님 → 기본 5)", tuple(th) == (2_000_000, 5), str(th))
    ok("A6-h 문턱 200만이면 평균 146만 → 빨강", b2["red"] is True, str(b2))
    ok("A6-i 최소 분 40 이면 30분(11)·10분(17)은 대기로 — 시간당 = 10 만", b3["rate_n"] == 1 and b3["wait_n"] == 4, str(b3))
    ok("A7-c 다른 테넌트 칸은 안 섞인다", main._abyss_billboard("other", now, 1, 5)["today"] is None)
    main.ABYSS_ACC.clear()


# ───────────────── A8 — /summary · 스냅샷 모양 ─────────────────

async def t_snapshot():
    if not _need("_abyss_ingest"):
        return
    import time as _t
    main.ABYSS_ACC.clear()

    async def _rows(tenant):
        return [{"pc_id": "PC-20", "status": "abyss", "_total_kina": 5, "_bug_count": 0,
                 "macro_version": "1.1.1004", "daily_progress": [],
                 **_rep(1790000000, 120_000, 480_000, 15)},
                {"pc_id": "PC-21", "status": "abyss", "_total_kina": 5, "_bug_count": 0,
                 "macro_version": "1.1.1003", "daily_progress": [], "abyss_kina": "+1 키나",
                 "abyss_kina_state": "bogus", "abyss_kina_gain": -5, "abyss_kina_rate": "x", "abyss_kina_mins": True}]
    o = (main._build_full_state, main._require_session)
    main._build_full_state = _rows
    main._require_session = lambda r: "main"
    try:
        empty = (await main._fv_build_snapshot("main"))["global"]["totals"]
        _n = _t.time()
        main._abyss_ingest("PC-20", _rep(max(_n - 900, main._abyss_day_start(_n) + 1), 120_000, 480_000, 15))
        snap = await main._fv_build_snapshot("main")
        body = json.loads((await main.dashboard_summary(Req())).body)
    finally:
        main._build_full_state, main._require_session = o
    t = snap["global"]["totals"]
    a = t.get("abyss") or {}
    ok("A8-a 데이터 없으면 totals.abyss.today·rate_sum = null(측정 대기)",
       (empty.get("abyss") or {}).get("today", 0) is None and (empty.get("abyss") or {}).get("rate_sum", 0) is None,
       str(empty.get("abyss")))
    ok("A8-b 스냅샷 totals.abyss — 오늘 120,000 · 시간당 480,000 · 1대 측정",
       a.get("today") == 120_000 and a.get("rate_sum") == 480_000 and a.get("rate_n") == 1, str(a))
    ok("A8-c /summary 에도 같은 abyss(대시보드·팜뷰 한 함수)", body.get("abyss") == json.loads(json.dumps(a)), str(body.get("abyss")))
    ok("A8-d 기존 totals 키는 그대로(total_kina·bugs·trade_kina·subscribed·corridor_remaining)",
       all(k in t for k in ("total_kina", "bugs", "trade_kina", "gakin_kina", "odd_energy", "awakening_ticket",
                            "subscribed", "corridor_remaining", "corridor_detail")))
    # ★A8-e 뒤집음(2026-09-24 이식, 관리자 요구)★ — 팜뷰 스냅샷 카드 progress 에 숫자 칸 5개를 ★더한다★(기존 키 그대로).
    _p20, _p21 = snap["pcs"]["PC-20"]["progress"], snap["pcs"]["PC-21"]["progress"]
    ok("A8-e 카드(pcs) progress 에 숫자 칸 5개가 그대로 실린다(state·gain·rate·since·mins)",
       {k: _p20.get(k) for k in ("abyss_kina_state", "abyss_kina_gain", "abyss_kina_rate", "abyss_kina_since",
                                 "abyss_kina_mins")}
       == {"abyss_kina_state": "ok", "abyss_kina_gain": 120_000, "abyss_kina_rate": 480_000,
           "abyss_kina_since": 1790000000, "abyss_kina_mins": 15}, str(_p20))
    ok("A8-e2 옛·이상 값은 null(state 모름·음수·글자·bool), 글자 칸 abyss_kina 는 그대로",
       all(_p21.get(k, 0) is None for k in ("abyss_kina_state", "abyss_kina_gain", "abyss_kina_rate",
                                            "abyss_kina_since", "abyss_kina_mins"))
       and _p21.get("abyss_kina") == "+1 키나", str(_p21))
    ok("A8-f 문턱이 같이 나간다(카드 JS 가 쓴다)", a.get("red_rate") == 1_000_000 and a.get("min_mins") == 5, str(a))
    main.ABYSS_ACC.clear()


async def t_feed_to_card():
    """숫자 칸이 상태 피드(_build_full_state → _card_key)로 카드까지 가는가 + 카드가 그것으로 그려지는가."""
    import time as _t
    since = int(_t.time() - 600)
    await main.receive_report("PC-21", Req(body=dict(_rep(since, 10, 60, 10), status="abyss")))
    rows = {r["pc_id"]: r for r in await main._build_full_state("main")}
    r = rows.get("PC-21") or {}
    k1 = main._card_key(r)
    k2 = main._card_key(dict(r, abyss_kina_gain=11))
    js_ok = False
    if "function abyssCardLine(" in main.HTML_DASHBOARD:
        src = main.HTML_DASHBOARD
        js = (_DOM + _js_line(src, "const KST_OFF_MS") + _js_line(src, "const ABYSS_TH_DEFAULT")
              + _js_line(src, "let ABYSS_TH =")
              + "\n".join(_js_func(src, n) for n in ("esc", "escAttr", "fmtTs", "fmtKstTs", "fmtKinaKor", "abyssTh",
                                                      "abyssCardLine"))
              + "\nconsole.log(JSON.stringify(abyssCardLine(" + json.dumps(r) + ")));")
        try:
            js_ok = "시간당" in (_run_node(js) or "")
        except Exception as e:
            print("   node:", str(e)[:200])
    ok("B-0 상태 피드가 숫자 칸을 싣고(카드 키가 gain 에 반응) 카드가 그 칸으로 그린다",
       r.get("abyss_kina_state") == "ok" and k1 != k2 and js_ok, str({k: r.get(k) for k in r if "abyss" in k}))
    ok("B-0b buildCard 가 옛 인라인 줄 대신 abyssCardLine 을 부른다",
       "${abyssCardLine(pc)}" in main.HTML_DASHBOARD
       and "💰 어비스 ${esc(pc.abyss_kina)}</div>`:''}" not in main.HTML_DASHBOARD)


def _js_line(src, prefix):
    for ln in src.splitlines():
        if ln.strip().startswith(prefix):
            return ln.strip() + "\n"
    raise ValueError("줄 없음: " + prefix)


# ───────────────── B — 카드 줄 · 전광판 칸 (node) ─────────────────

_CARD_SCEN = r"""
const out = {};
const S = 1790000000;   // 고정 epoch — 시간대마다 HH:MM 이 달라야 한다
const hm = fmtKstTs(new Date(S * 1000)).slice(11, 16);   // ★R7 — 카드 시각은 KST(매크로 글자·다른 KST 화면과 같게)★
out.hm = hm;
const P = (o) => Object.assign({pc_id:'PC-01'}, o);
out.redAfter = abyssCardLine(P({abyss_kina_state:'ok', abyss_kina_gain:123456789, abyss_kina_rate:500000, abyss_kina_since:S, abyss_kina_mins:12, abyss_kina:'+123,456,789 키나 · 시간당 500,000 (21:30부터)'}));
out.neutral = abyssCardLine(P({abyss_kina_state:'ok', abyss_kina_gain:30000, abyss_kina_rate:500000, abyss_kina_since:S, abyss_kina_mins:3}));
out.good = abyssCardLine(P({abyss_kina_state:'ok', abyss_kina_gain:30000000, abyss_kina_rate:2500000, abyss_kina_since:S, abyss_kina_mins:12}));
out.edge = abyssCardLine(P({abyss_kina_state:'ok', abyss_kina_gain:1, abyss_kina_rate:999999, abyss_kina_since:S, abyss_kina_mins:5}));
out.wait = abyssCardLine(P({abyss_kina_state:'waiting', abyss_kina_gain:0, abyss_kina_rate:0, abyss_kina_since:S, abyss_kina_mins:0}));
out.legacy = abyssCardLine(P({abyss_kina:'정산 대기 (21:30 시작)<b>'}));
// ★L — 옛 매크로(1.1.1003) 글자 칸: 빨강·음수 (2026-09-24 주인님 #159)★ — HH:MM 은 지금 KST 에서 N분 전
const hmAgo = (m) => fmtKstTs(new Date(Date.now() - m * 60000)).slice(11, 16);
out.lgRed = abyssCardLine(P({abyss_kina:`+123,456 키나 · 시간당 500,000 (${hmAgo(12)}~)`}));
out.lgGood = abyssCardLine(P({abyss_kina:`+30,000,000 키나 · 시간당 2,500,000 (${hmAgo(12)}부터)`}));
out.lgNew = abyssCardLine(P({abyss_kina:`+1,000 키나 · 시간당 500,000 (${hmAgo(2)}~)`}));
out.lgNeg = abyssCardLine(P({abyss_kina:'+-6,349,199 키나 · 시간당 -12,622,269 (04:40~)'}));
out.lgNeg2 = abyssCardLine(P({abyss_kina:'-5,000 키나 · 12분 (04:40부터)'}));
out.none = abyssCardLine(P({}));
renderAbyssTiles({abyss:{day:'2026-09-23', today:null, today_pcs:0, rate_sum:null, rate_avg:null, rate_n:0, wait_n:0, red:false, red_rate:3000000, min_mins:20}}, '');   // 문턱은 서버 요약에서 받는다
out.thRed = abyssCardLine(P({abyss_kina_state:'ok', abyss_kina_gain:1, abyss_kina_rate:2500000, abyss_kina_since:S, abyss_kina_mins:25}));
out.thNeutral = abyssCardLine(P({abyss_kina_state:'ok', abyss_kina_gain:1, abyss_kina_rate:500000, abyss_kina_since:S, abyss_kina_mins:12}));
// 전광판 칸
const tiles = () => ({today: els['cnt-abyss-today'].textContent, rate: els['cnt-abyss-rate'].textContent,
  sub: els['cnt-abyss-rate-sub'].textContent, red: els['tile-abyss-rate'].dataset.red, tt: els['cnt-abyss-today'].title,
  rt: els['cnt-abyss-rate'].title});
['cnt-abyss-today','cnt-abyss-rate','cnt-abyss-rate-sub','tile-abyss-rate'].forEach(mkEl);
renderAbyssTiles(null, '※ 서버 요약 대기 중');
out.bNull = tiles();
renderAbyssTiles({abyss:{day:'2026-09-23', today:null, today_pcs:0, rate_sum:null, rate_avg:null, rate_n:0, wait_n:2, red:false, red_rate:1000000, min_mins:5}}, 'n');
out.bWait = tiles();
renderAbyssTiles({abyss:{day:'2026-09-23', today:123456789, today_pcs:3, rate_sum:4400000, rate_avg:1466666, rate_n:3, wait_n:2, red:false, red_rate:1000000, min_mins:5}}, 'n');
out.bOk = tiles();
renderAbyssTiles({abyss:{day:'2026-09-23', today:0, today_pcs:1, rate_sum:900000, rate_avg:900000, rate_n:1, wait_n:0, red:true, red_rate:1000000, min_mins:5}}, 'n');
out.bRed = tiles();
console.log(JSON.stringify(out));
"""


def t_js_card_and_tiles():
    if not _need_node("B"):
        return
    src = main.HTML_DASHBOARD
    try:
        fns = "\n".join(_js_func(src, n) for n in ("esc", "escAttr", "fmtTs", "fmtKstTs", "fmtKinaKor", "abyssTh",
                                                  "abyssCardLine", "renderAbyssTiles"))
        consts = (_js_line(src, "const KST_OFF_MS") + _js_line(src, "const ABYSS_TH_DEFAULT")
                  + _js_line(src, "let ABYSS_TH ="))
    except Exception as e:
        ok("B-1 카드·전광판 함수를 잘라낸다(abyssCardLine·renderAbyssTiles)", False, str(e)[:200])
        return
    import subprocess as _sp
    for tz in ("Asia/Seoul", "Asia/Ho_Chi_Minh"):
        try:
            o = _run_node(_DOM + consts + fns + _CARD_SCEN, tz)
        except Exception as e:
            ok("B-1 node 실행 [%s]" % tz, False, str(e)[:300])
            continue
        want_hm = "23:13"     # 1790000000 = 2026-09-21 14:13:20 UTC = 23:13 KST — ★보는 기기 시간대와 무관(R7)★
        tag = "[%s]" % tz
        ok("B-1 %s 시작 시각은 ★KST★ HH:MM(%s) — 베트남 기기에서도 매크로 글자와 같은 시각(R7)" % (tag, want_hm),
           o["hm"] == want_hm and ("(%s부터)" % want_hm) in o["good"], o["hm"] + " | " + o["good"][-60:])
        ok("B-2 %s ok·12분·시간당 50만 < 100만 → 빨강, 「+1억 2,345만 · 시간당 50만 (HH:MM부터)」" % tag,
           "text-red-300" in o["redAfter"] and ("+1억 2,345만 · 시간당 50만 (%s부터)" % want_hm) in o["redAfter"],
           o["redAfter"][:200])
        ok("B-3 %s 3분(< 5분) → 빨강 아님·중립 「(측정 3분)」" % tag,
           "text-red" not in o["neutral"] and "(측정 3분)" in o["neutral"] and "text-gray-300" in o["neutral"],
           o["neutral"][:200])
        ok("B-4 %s 시간당 250만 → 호박색(빨강 아님)" % tag,
           "text-amber-300" in o["good"] and "text-red" not in o["good"] and "시간당 250만" in o["good"], o["good"][:200])
        ok("B-5 %s 경계 5분·999,999 → 빨강(5분부터 믿는다)" % tag, "text-red-300" in o["edge"], o["edge"][:160])
        ok("B-6 %s waiting → 회색 「측정 중 (HH:MM부터)」, 빨강 아님" % tag,
           ("측정 중 (%s부터)" % want_hm) in o["wait"] and "text-gray-400" in o["wait"] and "text-red" not in o["wait"],
           o["wait"][:200])
        ok("B-7 %s 숫자 칸 없는 옛 매크로 · 못 읽는 모양 → 예전 줄 그대로(esc 포함)" % tag,
           o["legacy"] == ('<div class="mt-1.5 text-xs text-amber-300 bg-amber-900/20 border border-amber-800/40 rounded '
                           'px-2 py-0.5 truncate" title="어비스(Delete) 세션 키나 정산 — 켤 때/끌 때 보유 키나 차액. '
                           '다음 세션 시작까지 유지">💰 어비스 정산 대기 (21:30 시작)&lt;b&gt;</div>'),
           o["legacy"][:220])
        ok("L-1 %s ★old: 옛 글자 「+X 키나 · 시간당 50만 (12분 전~)」 → 빨강(100만 미만) · 억/만 글자★" % tag,
           "text-red-300" in o["lgRed"] and "시간당 50만" in o["lgRed"] and "+12만 · 시간당 50만 (" in o["lgRed"], o["lgRed"][:200])
        ok("L-2 %s 옛 글자 시간당 250만 → 호박색(빨강 아님)" % tag,
           "text-amber-300" in o["lgGood"] and "text-red" not in o["lgGood"] and "시간당 250만" in o["lgGood"], o["lgGood"][:200])
        ok("L-3 %s 옛 글자 잰 지 2분(< 5분) → 중립 「(측정 N분)」, 빨강 아님" % tag,
           "text-red" not in o["lgNew"] and "(측정 " in o["lgNew"] and "text-gray-300" in o["lgNew"], o["lgNew"][:200])
        ok("L-4 %s ★old: PC-17b 「+-6,349,199 · 시간당 -12,622,269」 → 회색 «판독 오류 — 재측정 대기», 음수·숫자 안 나감★" % tag,
           "판독 오류 — 재측정 대기" in o["lgNeg"] and "text-gray-400" in o["lgNeg"] and "-" not in o["lgNeg"].split("💰")[1]
           and "6,349" not in o["lgNeg"] and "12,622" not in o["lgNeg"], o["lgNeg"][:220])
        ok("L-5 %s 끝난 세션 글자도 음수면 «판독 오류»(「-5,000 키나 · 12분」)" % tag,
           "판독 오류" in o["lgNeg2"] and "5,000" not in o["lgNeg2"], o["lgNeg2"][:200])
        ok("B-8 %s 아무 칸도 없으면 줄 없음" % tag, o["none"] == "")
        ok("B-9 %s 문턱은 서버 설정(/summary abyss.red_rate 300만·min 20분)을 따른다" % tag,
           "text-red-300" in o["thRed"] and "(측정 12분)" in o["thNeutral"] and "text-red" not in o["thNeutral"],
           o["thRed"][:120] + " | " + o["thNeutral"][:120])
        if tz != "Asia/Seoul":
            continue
        bn, bw, bo, br = o["bNull"], o["bWait"], o["bOk"], o["bRed"]
        ok("B-10 서버값 없음 → 두 칸 「측정 대기」(0 아님)", bn["today"] == "측정 대기" and bn["rate"] == "측정 대기", str(bn))
        ok("B-11 오늘·시간당 null → 「측정 대기」 · 「0대 측정 중 / 2대 대기」",
           bw["today"] == "측정 대기" and bw["rate"] == "측정 대기" and "0대 측정 중 / 2대 대기" in bw["sub"], str(bw))
        ok("B-12 값 있으면 억/만(1억 2,345만 · 440만) · 대당 146만 · 3대/2대",
           bo["today"] == "1억 2,345만" and bo["rate"] == "440만" and "대당 146만" in bo["sub"]
           and "3대 측정 중 / 2대 대기" in bo["sub"] and bo["red"] == "", str(bo))
        ok("B-13 대당 평균 < 문턱 → 시간당 칸 빨강(data-red=1)·툴팁에 문턱", br["red"] == "1" and "문턱" in br["rt"], str(br))
        ok("B-14 재서 0 이면 0(모름과 다르다)", br["today"] == "0", str(br))
        ok("B-15 툴팁에 KST 날짜·00:00 초기화", "KST 2026-09-23" in bo["tt"] and "00:00" in bo["tt"], bo["tt"])
    _ = _sp
    ok("B-16 전광판 HTML 에 두 칸(id) · refreshSummary 가 그린다",
       all(("id=\"%s\"" % i) in src for i in ("cnt-abyss-today", "cnt-abyss-rate", "tile-abyss-rate"))
       and "renderAbyssTiles(ss, ssNote)" in _js_func(src, "refreshSummary"))


# ───────────────── R — 적대 검증 재현(2026-09-23 refute_abyss R1~R9·기타) ─────────────────
#   tests/test_refute_abyss.py 의 재현을 옮겨 왔다. 각 줄은 ★기대 동작★ 을 단언한다.

def _card(pc, tz="Asia/Seoul"):
    """카드 줄 한 개를 node 로 그린다(시간대 지정)."""
    src = main.HTML_DASHBOARD
    js = (_DOM + _js_line(src, "const KST_OFF_MS") + _js_line(src, "const ABYSS_TH_DEFAULT")
          + _js_line(src, "let ABYSS_TH =")
          + "\n".join(_js_func(src, n) for n in ("esc", "escAttr", "fmtTs", "fmtKstTs", "fmtKinaKor", "abyssTh",
                                                  "abyssCardLine"))
          + "\nconsole.log(JSON.stringify(abyssCardLine(" + json.dumps(dict(pc, pc_id="PC-07")) + ")));")
    return _run_node(js, tz) or ""


def r1_end_text_other_day():
    """R1 — 끝난 구간 글자 「+N 키나 · M분 (HH:MM부터)」 가 다음 날 다시 오면 또 더하던 것."""
    main.ABYSS_ACC.clear()
    ing = main._abyss_ingest
    live = "+18,000,000 키나 · 시간당 9,000,000 (10:00부터)"
    end = "+20,000,000 키나 · 120분 (10:00부터)"
    ing("PC-01", {"abyss_kina": live}, now=_kst(2026, 9, 23, 12, 0))
    ing("PC-01", {"abyss_kina": end}, now=_kst(2026, 9, 23, 12, 1))
    d0 = _today(now=_kst(2026, 9, 23, 12, 2))["today"]
    for h, m, sec in ((0, 5, 0), (9, 58, 0), (9, 59, 30), (10, 5, 0), (15, 0, 0)):
        ing("PC-01", {"abyss_kina": end}, now=_kst(2026, 9, 24, h, m, sec))
    d1 = _today(now=_kst(2026, 9, 24, 15, 1))["today"]
    ok("R1-a 그날(D) 실시간→끝 글자 = 20,000,000", d0 == 20_000_000, str(d0))
    ok("R1-b 다음 날 어비스를 안 했는데 같은 끝 글자만 재전송 → 오늘 0(어제 몫 재입금 없음)",
       d1 == 0, "today=%s rec=%s" % (d1, main.ABYSS_ACC.get("PC-01")))
    main.ABYSS_ACC.clear()
    ing("PC-02", {"abyss_kina": end}, now=_kst(2026, 9, 24, 15, 0))
    t = _today(now=_kst(2026, 9, 24, 15, 1))["today"]
    ok("R1-c 끝 글자를 오늘 처음 봄(실시간으로 못 봄) → 오늘로 안 센다(0 또는 측정 대기)", t in (0, None), str(t))
    main.ABYSS_ACC.clear()


async def r2_future_since():
    """R2 — 미래 since(시계 틀림·쓰레기) 한 번에 그 PC 가 영영 얼던 것(저장까지 됨)."""
    main.ABYSS_ACC.clear()
    ing = main._abyss_ingest
    now = _kst(2026, 9, 23, 10, 0)
    how = ing("PC-03", _rep(now + 3600, 0, 0, 0, "waiting"), now=now)
    s = int(now + 60)
    for k in range(1, 40):
        ing("PC-03", _rep(s, 100_000 * k, 3_000_000, k), now=s + 60 * k)
    t = _today(now=s + 60 * 40)["today"]
    ok("R2-a 미래 since(+1시간)는 받지 않는다 → 뒤의 진짜 구간이 센다(3,900,000)",
       how == "" and t == 3_900_000, "how=%r today=%s" % (how, t))
    main.ABYSS_ACC.clear()
    ing("PC-04", _rep(10 ** 30, 0, 0, 0, "waiting"), now=now)
    nd = _kst(2026, 9, 30, 12, 0)
    ing("PC-04", _rep(nd - 600, 500_000, 3_000_000, 10), now=nd)
    t2 = _today(now=nd + 1)["today"]
    ok("R2-b since=1e30 한 번 → 일주일 뒤에도 그 PC 가 센다(500,000)", t2 == 500_000, str(t2))
    ok("R2-c 경계 — now+60초 이내 since 는 받는다(매크로 시계 조금 빠름)",
       ing("PC-05", _rep(now + 59, 1, 0, 0, "waiting"), now=now) == "bank")
    # 옛 판(이 수정 전)이 저장해 둔 미래 since — 복원·자정에서 비운다
    main.ABYSS_ACC.clear()
    key = main.ns("main", "PC-06")
    poison = {"day": "2026-09-23", "banked": 0, "since": 10 ** 12, "gain": 5, "base": 0, "card": "PC-06",
              "state": "ok", "rate": 1, "mins": 1, "ts": now, "sig": [], "sig_at": now}
    await db.set_setting(main.ABYSS_ACC_KEY, json.dumps({key: dict(poison)}))
    await main._abyss_restore()
    r = main.ABYSS_ACC.get(key) or {}
    ok("R2-d 복원할 때 미래 since 구간 상태를 비운다", r.get("since") is None, str(r))
    main.ABYSS_ACC.clear()
    main.ABYSS_ACC[key] = dict(poison)
    ing("PC-06", _rep(_kst(2026, 9, 24, 9, 0), 70_000, 700_000, 6), now=_kst(2026, 9, 24, 9, 6))
    t3 = _today(now=_kst(2026, 9, 24, 9, 7))["today"]
    ok("R2-e 메모리에 남은 미래 since 도 자정(날짜 넘김)에서 비워 다음 보고가 센다(70,000)",
       t3 == 70_000, "today=%s rec=%s" % (t3, main.ABYSS_ACC.get(key)))
    await db.set_setting(main.ABYSS_ACC_KEY, "{}")
    main.ABYSS_ACC.clear()


def r3_upper_bounds():
    """R3 — gain·rate·mins 상한이 없어 1e30 한 번이 오늘 합계로 굳던 것."""
    main.ABYSS_ACC.clear()
    ing = main._abyss_ingest
    now = _kst(2026, 9, 23, 10, 0)
    s = int(now - 600)
    ing("PC-05", _rep(s, 1e30, 1e30, 10), now=now)
    ing("PC-05", _rep(s, 700_000, 4_200_000, 11), now=now + 60)
    b = _today(now=now + 61)
    ok("R3-a gain 1e30 한 번이 오늘 합계가 되지 않는다(700,000)", b["today"] == 700_000, str(b))
    main.ABYSS_ACC.clear()
    how = ing("PC-06", _rep(s, 5_000, 10 ** 13, 10), now=now)
    ok("R3-b 시간당이 상한(ABYSS_RATE_MAX)을 넘는 보고는 버린다", how == "" and not main.ABYSS_ACC, "how=%r" % how)
    ing("PC-07", _rep(s, 5_000, 30_000, 10 ** 9), now=now)
    r = main.ABYSS_ACC.get(main.ns("main", "PC-07")) or {}
    ok("R3-c mins 는 상한(ABYSS_MINS_MAX)으로 자른다", r.get("mins") == getattr(main, "ABYSS_MINS_MAX", -1),
       str(r.get("mins")))
    ok("R3-d gain 상한 = 구간당 1조", getattr(main, "ABYSS_GAIN_MAX", None) == 10 ** 12)
    main.ABYSS_ACC.clear()


def r4_legacy_zero_earner():
    """R4 — 옛 글자 칸이 0 에서 안 바뀌면(한 푼도 못 벎) 300초 뒤 「끝난 구간」 으로 빠져 빨강이 안 뜨던 것."""
    main.ABYSS_ACC.clear()
    t0 = _kst(2026, 9, 23, 14, 0)
    for k in range(0, 20):
        main._abyss_ingest("PC-06", {"abyss_kina": "+0 키나 · 시간당 0 (14:00부터)"}, now=t0 + 60 * k)
    b = _today(now=t0 + 60 * 19 + 1)
    ok("R4-a 옛 매크로 19분째 시간당 0 → 재는 중(rate_n=1)·빨강", b["rate_n"] == 1 and b["red"] is True, str(b))
    main.ABYSS_ACC.clear()
    for sec in (0, 20, 40):      # 같은 분 안에서 글자·mins 가 그대로여도 어비스 상태 보고 = 살아 있다
        main._abyss_ingest("PC-06", {"abyss_kina": "+0 키나 · 시간당 0 (13:50부터)", "status": "abyss"},
                           now=t0 + sec)
    b = _today(now=t0 + 40 + 290)
    ok("R4-b 어비스 상태로 보고가 오는 PC 는 살아 있다(보고 도착 기준)", b["rate_n"] + b["wait_n"] == 1, str(b))
    main.ABYSS_ACC.clear()
    for k in range(0, 3):
        main._abyss_ingest("PC-08", {"abyss_kina": "+5,000 키나 · 40분 (13:00부터)"}, now=t0 + 60 * k)
    b = _today(now=t0 + 60 * 2 + 400)
    ok("R4-c 끝난 구간 글자(「· N분」)는 재전송이 계속 와도 시간당·대수에서 빠진다",
       b["rate_n"] + b["wait_n"] == 0, str(b))
    main.ABYSS_ACC.clear()


def r5_waiting_until_10min():
    """R5 — 매크로는 10분까지 state=waiting(lc/loot.py ABYSS_KINA_OK_MINS=10), 주인님 규칙은 5분부터 빨강."""
    main.ABYSS_ACC.clear()
    now = _kst(2026, 9, 23, 16, 0)
    s = int(now - 7 * 60)
    main._abyss_ingest("PC-07", _rep(s, 50_000, 428_571, 7, "waiting"), now=now)
    b = _today(now=now + 1)
    ok("R5-a waiting 이어도 7분 ≥ 문턱 5분·시간당 42만 < 100만 → 잰다(rate_n=1)·빨강",
       b["rate_n"] == 1 and b["red"] is True and b["rate_sum"] == 50_000 * 60 // 7, str(b))
    main.ABYSS_ACC.clear()
    main._abyss_ingest("PC-07", _rep(now - 180, 5_000, 100_000, 3, "waiting"), now=now)
    main._abyss_ingest("PC-08", _rep(now - 600, 0, 0, 0, "waiting"), now=now)
    b = _today(now=now + 1)
    ok("R5-b waiting 3분(< 5분)·숫자 없는 waiting(mins 0) → 둘 다 대기", b["rate_n"] == 0 and b["wait_n"] == 2, str(b))
    main.ABYSS_ACC.clear()
    if not _need_node("R5-c"):
        return
    o = _card(_rep(s, 50_000, 428_571, 7, "waiting"))
    ok("R5-c 카드 — waiting 7분·시간당 42만 → 빨강(회색 「측정 중」 아님)",
       "text-red-300" in o and "측정 중" not in o and "시간당 42만" in o, o[:220])
    o = _card(_rep(s, 50_000, 428_571, 3, "waiting"))
    ok("R5-d 카드 — waiting 3분(< 5분) → 회색 「측정 중 (HH:MM부터)」 그대로",
       "측정 중 (" in o and "text-gray-400" in o and "text-red" not in o, o[:220])
    o = _card(_rep(s, 3_000_000, 0, 7, "waiting"))
    ok("R5-e 카드 — waiting 7분·gain 300만 → 시간당은 gain/mins 로(2,571만) 호박색",
       "text-amber-300" in o and "시간당 2,571만" in o, o[:220])


def r6_legacy_clock_ahead():
    """R6 — 옛 매크로 시계가 60초 넘게 빠르면 HH:MM 이 어제→오늘로 뒤집혀 그 사이 몫을 두 번 셌다."""
    main.ABYSS_ACC.clear()
    for k in range(0, 31):
        main._abyss_ingest("PC-08", {"abyss_kina": "+%s 키나 · 시간당 6,000,000 (13:10부터)" % format(100_000 * k, ",")},
                           now=_kst(2026, 9, 23, 13, 0) + 60 * k + 30)
    t = _today(now=_kst(2026, 9, 23, 13, 31))["today"]
    ok("R6 30분 × 10만 = 최대 3,000,000 — 시계 틀림 구간을 두 번 세지 않는다", t is not None and t <= 3_000_000, str(t))
    main.ABYSS_ACC.clear()


def r8_stale_after_midnight():
    """R8 — 자정 뒤 버려진(옛 since) 보고가 「측정 대기」 를 0 으로 바꾸던 것."""
    main.ABYSS_ACC.clear()
    s = _kst(2026, 9, 23, 20, 0)
    main._abyss_ingest("PC-09", _rep(s + 600, 5, 0, 0, "waiting"), now=s + 610)
    how = main._abyss_ingest("PC-09", _rep(s, 999, 1, 50), now=_kst(2026, 9, 24, 0, 1))
    t = _today(now=_kst(2026, 9, 24, 0, 2))["today"]
    ok("R8 자정 뒤 옛 보고(stale)만 옴 → 오늘은 여전히 「측정 대기」(null)", how == "stale" and t is None,
       "how=%r today=%s" % (how, t))
    main.ABYSS_ACC.clear()


def r9_legacy_spend():
    """R9 — 옛 글자 칸 gain 이 같은 since 안에서 줄면(수리비) 조기 return 으로 표시·생존이 얼던 것."""
    main.ABYSS_ACC.clear()
    t0 = _kst(2026, 9, 23, 14, 0)
    main._abyss_ingest("PC-10", {"abyss_kina": "+2,000,000 키나 · 시간당 6,000,000 (14:00~)"}, now=t0 + 20 * 60)
    for k in range(1, 7):
        g = 1_200_000 + 100_000 * (k - 1)
        main._abyss_ingest("PC-10", {"abyss_kina": "+%s 키나 · 시간당 %s (14:00~)"
                                     % (format(g, ","), format(g * 60 // (20 + k), ","))}, now=t0 + (20 + k) * 60)
    b = _today(now=t0 + 26 * 60 + 1)
    r = main.ABYSS_ACC.get(main.ns("main", "PC-10")) or {}
    ok("R9-a 수리비로 줄어도 계속 사냥 중인 옛 PC 는 센다(rate_n + wait_n == 1)", b["rate_n"] + b["wait_n"] == 1, str(b))
    ok("R9-b 표시 칸(시간당)은 줄어든 새 값을 따른다", r.get("rate") == 1_700_000 * 60 // 26, str(r.get("rate")))
    ok("R9-c 은행은 max 그대로 — 오늘 2,000,000(줄었다 다시 오른 몫을 두 번 세지 않는다)", b["today"] == 2_000_000, str(b))
    main.ABYSS_ACC.clear()


def rx_fake_cap_and_clamp():
    """기타 — 가짜 PC 는 상한을 안 먹고 진짜를 안 밀어낸다 · 카드 글자에 음수·NaN 을 안 쓴다."""
    main.ABYSS_ACC.clear()
    now = _kst(2026, 9, 23, 12, 0)
    for i in range(main.ABYSS_TENANT_MAX):
        main._abyss_ingest("PC-R%03d" % i, _rep(now - 2000, 1, 6, 10), now=now - 1000 + i)
    for pid in ("PC-TEST", "PC-TESTb", "PC-DEMO"):
        main._abyss_ingest(pid, _rep(now - 600, 1, 6, 10), now=now)
    real = [k for k in main.ABYSS_ACC if main.split_ns(k)[1].startswith("PC-R")]
    ok("X-1 가짜 PC(PC-TEST·PC-DEMO) 는 쌓지 않는다", not any("TEST" in k or "DEMO" in k for k in main.ABYSS_ACC),
       str([k for k in main.ABYSS_ACC if "TEST" in k or "DEMO" in k]))
    ok("X-2 가짜 PC 가 진짜 %d대를 밀어내지 않는다" % main.ABYSS_TENANT_MAX, len(real) == main.ABYSS_TENANT_MAX, str(len(real)))
    main.ABYSS_ACC.clear()
    if not _need_node("X-3"):
        return
    o = _card({"abyss_kina_state": "ok", "abyss_kina_gain": -5, "abyss_kina_rate": "NaN",
               "abyss_kina_since": 1790000000, "abyss_kina_mins": -3})
    body = o.split("💰")[-1]
    ok("X-3 카드 — 음수·NaN mins/rate/gain 은 0 으로(「-」·NaN 글자 없음)",
       "NaN" not in o and "+-" not in body and "(측정 0분)" in body and "시간당 0 " in body, o[:220])
    o = _card({"abyss_kina_state": "waiting", "abyss_kina_gain": 1000, "abyss_kina_since": 1790000000,
               "abyss_kina_mins": "abc"})
    ok("X-4 카드 — mins 가 숫자가 아니면 waiting 은 회색 「측정 중」", "측정 중 (" in o and "NaN" not in o, o[:220])


# ───────────────── S2 — 관리자 명세 2 (2026-09-24 이식) ─────────────────
#   (a) 「오늘 누적은 리셋돼도 줄지 않게」 — 매크로 카운터가 리셋(since 바뀜·재시작·gain 감소)돼도 오늘 합계는 안 준다
#   (b) 「since 가 바뀌면 빨강을 푼다」 — 새 구간이 유효한 시간당(mins ≥ 문턱)을 갖기 전엔 빨강 아님(카드·전광판)

def s2_owner_item2():
    if not _need("_abyss_ingest", "_abyss_billboard"):
        return
    main.ABYSS_ACC.clear()
    t0 = _kst(2026, 9, 23, 10, 0)
    s1, s2 = int(t0), int(t0 + 1500)
    seq = [(_rep(s1, 300_000, 900_000, 20), t0 + 1200),            # 구간 1 — 시간당 90만(< 100만)
           (_rep(s1, 500_000, 900_000, 30), t0 + 1800 - 400),
           (_rep(s1, 400_000, 800_000, 31), t0 + 1800 - 340),      # 같은 since·gain 감소(재전송·수리비)
           (_rep(s2, 0, 0, 0, "waiting"), s2 + 5),                   # 자리 옮김·재개 → 새 since, 카운터 0
           (_rep(s2, 0, 0, 0, "waiting"), s2 + 65),
           ({}, s2 + 70),                                            # 매크로 재시작 — 숫자 칸 없음
           (_rep(s2, 200_000, 1_200_000, 10), s2 + 600)]
    todays, reds = [], []
    for rep, now in seq:
        main._abyss_ingest("PC-30", rep, now=now)
        b = _today(now=now + 1)
        todays.append(b["today"])
        reds.append(b["red"])
    ok("S2-a 오늘 누적은 리셋·재시작·gain 감소에도 한 번도 줄지 않는다(비감소)",
       all(x is not None for x in todays) and all(a <= b for a, b in zip(todays, todays[1:])), str(todays))
    ok("S2-a2 앞 구간 50만을 지키고 새 구간 20만을 더한다(= 70만)", todays[-1] == 700_000, str(todays))
    ok("S2-b 전광판 — 옛 구간 시간당 90만이면 빨강, since 가 바뀐 즉시 빨강이 풀린다(대기로)",
       reds[1] is True and reds[3] is False and reds[4] is False, str(reds))
    main.ABYSS_ACC.clear()
    if not _need_node("S2-b2"):
        return
    old = _card(_rep(s1, 500_000, 900_000, 30))
    new_wait = _card(_rep(s2, 0, 0, 0, "waiting"))
    new_short = _card(_rep(s2, 20_000, 400_000, 3))             # 새 구간 3분 — 시간당 40만이어도 아직 문턱 전
    ok("S2-b2 카드 — 옛 구간 90만/시간은 빨강, since 바뀐 뒤(waiting·mins 0 / ok 3분)는 빨강 아님",
       "text-red-300" in old and "text-red-300" not in new_wait and "text-red-300" not in new_short
       and "측정 중" in new_wait and "(측정 3분)" in new_short, "\n".join((old[:120], new_wait[:120], new_short[:120])))


async def l_legacy_negative_server():
    """주인님 #159 — PC-17b 옛 글자 「+-6,349,199 키나 · 시간당 -12,622,269 (04:40~)」 가 전광판 합계에 섞이나."""
    import time as _t
    neg = "+-6,349,199 키나 · 시간당 -12,622,269 (04:40~)"
    _n = _t.time()
    ok("L-6 서버 옛 글자 파서는 음수 글자를 못 읽는다(None = 측정 대기, 0·음수 아님)", main._abyss_parse({"abyss_kina": neg}, _n) is None,
       str(main._abyss_parse({"abyss_kina": neg}, _n)))
    main.ABYSS_ACC.clear()
    main._abyss_ingest("PC-20", _rep(max(_n - 900, main._abyss_day_start(_n) + 1), 120_000, 1_500_000, 15), _n)
    b1 = _today(now=_n)
    for _ in range(3):
        main._abyss_ingest("PC-17b", {"abyss_kina": neg}, _n)
    b2 = _today(now=_n)
    ok("L-7 ★음수 글자를 받아도 오늘 합계·시간당 합·대수가 그대로★",
       (b1["today"], b1["rate_sum"], b1["today_pcs"], b1["rate_n"]) == (b2["today"], b2["rate_sum"], b2["today_pcs"], b2["rate_n"]),
       "%s | %s" % (b1, b2))
    ok("L-8 합계에 음수가 없다", (b2["today"] or 0) >= 0 and (b2["rate_sum"] or 0) >= 0, str(b2))
    main.ABYSS_ACC.clear()


async def v3_corrupt_store():
    """v4 반증 3차(아이온2) — 저장본(abyss_acc_all)에 글자·NaN 칸이 있는 채로 재시작하면 _abyss_billboard 의 int() 가 터져
    /summary·/api/fv/snapshot 이 ★전 PC 500★. → 칸마다 정수 변환(_abyss_i) + 전광판 계산 try(실패면 today:null)."""
    if not _need("_abyss_ingest", "_abyss_restore"):
        return
    import time as _t
    main.ABYSS_ACC.clear()
    _n = _t.time()
    main._abyss_ingest("PC-20", _rep(max(_n - 900, main._abyss_day_start(_n) + 1), 120_000, 480_000, 15))
    rec = dict(main.ABYSS_ACC.get("PC-20") or {})
    rec.update({"banked": "700", "mins": "nan", "rate": "abc"})
    bad = {"PC-20": rec, "PC-22": ["깨진", "줄"], "PC-23": dict(rec, gain="x", base="inf"),
           "PC-24": dict(rec, banked="x")}                  # 적립 칸이 글자 — 그냥 int() 면 전광판째 today:null
    await db.set_setting(main.ABYSS_ACC_KEY, json.dumps(bad, ensure_ascii=False))
    main.ABYSS_ACC.clear()
    await main._abyss_restore()
    main.ABYSS_ACC.update({k: v for k, v in bad.items() if k not in main.ABYSS_ACC})   # 복원이 걸러도 메모리엔 깨진 줄이 있다고 치고
    main.ABYSS_ACC["PC-24"] = dict(rec, banked="x", gain="nan")   # ★복원이 숫자로 고친 칸도 메모리에서 깨졌다고 치고★
    #   (_abyss_restore 는 칸을 0 으로 고쳐 넣는다 — 그러면 전광판 가드가 시험에 안 닿는다. 복원을 안 거친 줄로 직접 찌른다)

    async def _rows(tenant):
        return [{"pc_id": "PC-20", "status": "abyss", "_total_kina": 5, "_bug_count": 0, "daily_progress": []}]
    o = (main._build_full_state, main._require_session)
    main._build_full_state = _rows
    main._require_session = lambda r: "main"
    err, snap = "", None
    try:
        snap = await main._fv_build_snapshot("main")
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    a = ((snap or {}).get("global") or {}).get("totals", {}).get("abyss") or {}
    ok("V3-a ★old: 깨진 저장본(글자·NaN·목록 줄)으로 재시작해도 스냅샷이 예외 없이 나온다(전 PC 500 아님)★", not err and bool(a), err or str(a))
    ok("V3-b 못 읽는 칸(글자·NaN)은 0 으로 센다 — 전광판이 today:null 로 죽지 않고 숫자를 낸다(오늘 ≥ 700)",
       isinstance(a.get("today"), int) and a.get("today") >= 700, str(a))
    ob = main._abyss_billboard

    def _boom(*x, **k):
        raise ValueError("테스트")
    main._abyss_billboard = _boom
    try:
        snap2 = await main._fv_build_snapshot("main")
        a2 = snap2["global"]["totals"]["abyss"]
        ok("V3-c ★old: 전광판 계산이 터져도 스냅샷은 나가고 abyss.today 는 null(측정 대기)★",
           a2.get("today") is None and a2.get("error") is True, str(a2))
    except Exception as e:
        ok("V3-c ★old: 전광판 계산이 터져도 스냅샷은 나가고 abyss.today 는 null(측정 대기)★", False, str(e))
    finally:
        main._abyss_billboard = ob
        main._build_full_state, main._require_session = o
        main.ABYSS_ACC.clear()


def test_all():
    run_all([t_bank_and_idempotent, t_kst_rollover, t_persist_and_report, t_legacy, t_rate_and_exclusion,
             t_snapshot, t_feed_to_card, t_js_card_and_tiles, s2_owner_item2,
             r1_end_text_other_day, r2_future_since, r3_upper_bounds, r4_legacy_zero_earner, r5_waiting_until_10min,
             r6_legacy_clock_ahead, r8_stale_after_midnight, r9_legacy_spend, rx_fake_cap_and_clamp, v3_corrupt_store,
             l_legacy_negative_server])
    finish("test_abyss_kina", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
