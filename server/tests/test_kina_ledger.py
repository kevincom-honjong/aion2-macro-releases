# -*- coding: utf-8 -*-
"""창고키나 재전송이 팜뷰 차감을 되돌리던 것 (2026-09-23, 팜뷰방 진단 — 5건 전부 되돌려짐).

매크로는 부팅·WS 재연결마다 로컬 char_info.json 을 통째로 다시 보낸다(report_module._push_local_char_info).
서버가 그 total_kina 로 덮어 kina_adjust 로 뺀 만큼이 되살아났다. 서버는 이제 ★장부 사슬★ 로 가른다
(database._kina_ledger_replay): 받은 값이 어떤 차감의 before 와 같으면 재전송 → 그 뒤 사슬의 after.

    cd updater/server && python -X utf8 tests/test_kina_ledger.py
"""
import asyncio
import json

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 36
TOK = "fvsecret-kl"
H = {"X-FV-Token": TOK}
OLD_CA = "2026-09-22T20:00:00"      # 매크로 전체수집 시각(UTC) — 판매보다 앞


async def _collect(pc, total, merge=False, ca=OLD_CA):
    body = {"total_kina": total, "characters": [{"slot": 1, "name": "러닝"}], "merge": merge, "collected_at": ca}
    r = await main.receive_char_info(pc, Req(body))
    assert r.status_code == 200, r.body
    return (await db.get_char_info(main.ns("main", pc)) or {}).get("total_kina")


async def _sell(pc, delta, tid):
    main.FV_TOKEN = TOK
    r = await main.fv_kina_adjust(Req({"pc_id": pc, "delta_kina": delta, "why": {"tid": tid}}, api_key=None, headers=H))
    return json.loads(bytes(r.body))


async def _ledger_rows(pc):
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT COUNT(*) FROM kina_adjust WHERE pc_id=?", (main.ns("main", pc),)) as cur:
            return (await cur.fetchone())[0]


async def t_resend_keeps_deduction():
    main.FV_TENANT = "main"
    pc = "PC-L07"
    await _collect(pc, 690_980_731)
    a = await _sell(pc, -300_000_000, "tid-L07")
    ok("L-1 차감 690,980,731 → 390,980,731", a.get("before") == 690_980_731 and a.get("after") == 390_980_731, str(a))
    v = await _collect(pc, 690_980_731, merge=True)           # WS 재연결 재전송(옛 값·옛 시각, merge)
    ok("L-2 ★재전송(merge)이 차감을 되돌리지 않는다★", v == 390_980_731, str(v))
    v = await _collect(pc, 690_980_731, merge=False)          # 부팅 재전송이 merge 아닌 판
    ok("L-3 재전송(전체수집 모양)도 차감 유지", v == 390_980_731, str(v))
    v = await _collect(pc, 690_980_731, merge=True)
    ok("L-4 두 번·세 번 재전송해도 한 번만 뺀다(멱등)", v == 390_980_731 and await _ledger_rows(pc) == 1, str(v))
    logs = await db.get_logs(main.ns("main", pc), 10)
    ok("L-5 그 PC 로그줄에 「장부 다시 적용」 이 남는다(A2 증거)",
       any("장부 다시 적용" in str(x.get("message")) and "tid-L07" in str(x.get("message")) for x in logs),
       str([x.get("message") for x in logs][:2]))


async def t_fresh_read_not_double():
    pc = "PC-L11"
    await _collect(pc, 846_856_019)
    await _sell(pc, -500_000_000, "tid-L11")
    # 사고 569 창고 판독: ★새 total_kina + 옛 collected_at★ (merge) — 시각으로 가르면 여기서 두 번 뺀다
    v = await _collect(pc, 352_000_000, merge=True, ca=OLD_CA)
    ok("L-6 새 창고 판독(옛 collected_at)은 그대로 — 판매를 두 번 빼지 않는다", v == 352_000_000, str(v))
    v = await _collect(pc, 352_000_000, merge=True)
    ok("L-7 그 새 판독의 재전송도 그대로", v == 352_000_000, str(v))
    v = await _collect(pc, 360_500_000, merge=False, ca="2026-09-23T12:00:00")
    ok("L-8 새 전체수집은 진짜 값으로 덮는다(«맞춰 나가기» 유지)", v == 360_500_000, str(v))


async def t_chain():
    pc = "PC-L19"
    await _collect(pc, 406_846_332)
    await _sell(pc, -400_000_000, "tid-L19a")      # 406,846,332 → 6,846,332
    await _sell(pc, -1_000_000, "tid-L19b")        # 6,846,332 → 5,846,332
    v = await _collect(pc, 406_846_332, merge=True)
    ok("L-9 판매 두 건 사슬 — 첫 판매 전 값 재전송 → 마지막 after", v == 5_846_332, str(v))
    # 사슬 중간에 새 판독이 끼면 거기서 끊는다
    pc = "PC-L12b"
    await _collect(pc, 146_487_482)
    await _sell(pc, -140_000_000, "tid-L12a")      # → 6,487,482
    await _collect(pc, 9_000_000, merge=True)      # 새 판독(사냥 수익)
    await _sell(pc, -2_000_000, "tid-L12b")        # 9,000,000 → 7,000,000
    v = await _collect(pc, 9_000_000, merge=True)
    ok("L-10 새 판독 뒤 판매 — 그 판독값 재전송 → 7,000,000", v == 7_000_000, str(v))
    v = await _collect(pc, 146_487_482, merge=True)
    ok("L-11 끊긴 사슬 앞의 옛 값은 그 사슬까지만(모르는 만큼 더 빼지 않는다) → 6,487,482", v == 6_487_482, str(v))


async def t_already_reverted_heals():
    # 운영 지금 모양: 장부엔 before→after 가 있는데 저장값이 before 로 되돌아가 있다 → 다음 재전송이 고친다
    pc = "PC-L09"
    await _collect(pc, 261_773_318)
    await _sell(pc, -200_000_000, "tid-L09")
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as c:                 # 옛 서버가 덮어쓴 상태를 그대로 만든다
        await c.execute("UPDATE char_info SET total_kina=? WHERE pc_id=?", (261_773_318, main.ns("main", pc)))
        await c.commit()
    v = await _collect(pc, 261_773_318, merge=True)
    ok("L-12 ★이미 되돌려진 카드도 다음 재전송에 저절로 복구(손 보정 0)★ → 61,773,318", v == 61_773_318, str(v))
    ok("L-13 복구에 새 장부 행이 안 생긴다(두 번 빼기 없음)", await _ledger_rows(pc) == 1)


def _utc(delta_s):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%S")


def _kst(delta_s):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).astimezone(timezone(timedelta(hours=9))).isoformat()


async def _age_ledger(pc, hours):
    """그 카드 장부 행을 hours 시간 전으로 — 72시간 상한 시험용."""
    import aiosqlite
    from datetime import datetime, timezone, timedelta
    at = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE kina_adjust SET at=? WHERE pc_id=?", (at, main.ns("main", pc)))
        await c.commit()


async def _post(pc, body):
    r = await main.receive_char_info(pc, Req(dict({"characters": [{"slot": 1, "name": "러닝"}]}, **body)))
    assert r.status_code == 200, r.body
    return (await db.get_char_info(main.ns("main", pc)) or {}).get("total_kina")


async def t_clock_independent_and_isolation():
    pc = "PC-L20"
    await _collect(pc, 500_000_000)
    await _sell(pc, -100_000_000, "tid-L20")
    v1 = await _collect(pc, 500_000_000, merge=False, ca=_kst(+3600))                  # 판매 ★뒤★ 시각(15분 재수집 모양)
    v2 = await _collect(pc, 500_000_000, merge=False, ca="2020-01-01T00:00:00")         # 저장보다 옛 시각
    ok("L-14 ★표식 없는 옛 매크로는 시각과 무관하게 값으로 가른다 — 새 시각의 옛 값(_resend_last_full, 반증 v2 #1)도 장부 적용★",
       v1 == v2 == 400_000_000, "%s %s" % (v1, v2))
    v = await _collect("PC-L20b", 500_000_000)
    ok("L-15 형제 카드(PC-L20b)는 남의 장부를 안 탄다", v == 500_000_000, str(v))
    v = await _collect(pc, 0, merge=True)
    ok("L-16 total_kina=0(못 읽음)은 장부 반영값을 지킨다", v == 400_000_000, str(v))


async def t_refuter_2026_09_23():
    # F1·F2 — 값 규칙은 ★덜 보이는 쪽★ 으로 틀린다(없는 키나를 팔지 않게). 72시간이 지나면 새 판독이 그대로 들어간다.
    pc = "PC-LR1"
    await _collect(pc, 500_000_000)
    await _sell(pc, -100_000_000, "tid-LR1")
    v = await _collect(pc, 500_000_000, ca=_utc(+5))
    ok("L-18 72시간 안: 판매 전 값과 같은 판독은 재전송으로 본다(덜 보이는 쪽 — 오판매 없음)", v == 400_000_000, str(v))
    await _age_ledger(pc, 73)
    v = await _collect(pc, 500_000_000, ca=_utc(+6))
    ok("L-19 ★72시간 지난 판매는 안 탄다 — 새 판독이 그대로(반증 ② 상한)★", v == 500_000_000, str(v))
    # F3 — 사슬이 끊기면 멈춘다
    pc = "PC-LR3"
    await _collect(pc, 100_000_000)
    await _sell(pc, -10_000_000, "tid-LR3a")               # 100M → 90M
    await _collect(pc, 60_000_000, ca=_utc(+1))            # 새 판독
    await _sell(pc, -10_000_000, "tid-LR3b")               # 60M → 50M
    await _collect(pc, 90_000_000, ca=_utc(+2))            # 새 판독 90M(어떤 before 와도 다르다 → 그대로)
    await _sell(pc, -40_000_000, "tid-LR3c")               # 90M → 50M
    v = await _collect(pc, 100_000_000, merge=True)
    ok("L-20 끊긴 사슬 뒤의 우연한 같은 before 에 다시 잇지 않는다(돌연변이 «끊겨도 계속») → 90M", v == 90_000_000, str(v))
    # collected_at 없음도 값으로 가른다
    pc = "PC-LR4"
    await _collect(pc, 300_000_000)
    await _sell(pc, -100_000_000, "tid-LR4")
    v = await _post(pc, {"total_kina": 300_000_000, "merge": True})
    ok("L-21 collected_at 이 없는 재전송도 장부 적용", v == 200_000_000, str(v))


async def t_reverted_then_new_sale():
    # 반증 ① — 되돌려진 채인 카드에 새 판매 → 첫 판매가 사라지던 것
    import aiosqlite
    pc = "PC-LN1"
    await _collect(pc, 1_000_000_000)
    await _sell(pc, -200_000_000, "tid-LN1a")              # 1000 → 800
    async with aiosqlite.connect(db.DB_PATH) as c:          # 옛 서버가 재전송으로 되돌린 모양
        await c.execute("UPDATE char_info SET total_kina=? WHERE pc_id=?", (1_000_000_000, main.ns("main", pc)))
        await c.commit()
    a = await _sell(pc, -100_000_000, "tid-LN1b")
    ok("L-22 ★되돌려진 카드의 새 판매는 장부를 먼저 적용하고 뺀다 — before 800M → after 700M★",
       a.get("before") == 800_000_000 and a.get("after") == 700_000_000, str(a))
    logs = await db.get_logs(main.ns("main", pc), 10)
    ok("L-23 그 복구가 로그줄로 남는다(A2)", any("되돌림 복구" in str(x.get("message")) for x in logs),
       str([x.get("message") for x in logs][:3]))
    v = await _collect(pc, 1_000_000_000, merge=True)
    ok("L-24 그 뒤 옛 값 재전송 → 700M(두 판매 모두)", v == 700_000_000, str(v))
    # 옛 서버가 이미 남긴 모양: 같은 before(되돌려진 값)를 가진 행이 둘 — 둘 다 뺀다(돌연변이 «가장 늦은 것만» 을 잡는다)
    pc = "PC-LN2"
    await _collect(pc, 1_000_000_000)
    async with aiosqlite.connect(db.DB_PATH) as c:
        for tid, b, a2, at in (("tid-LN2a", 1_000_000_000, 800_000_000, _utc(-120)),
                               ("tid-LN2b", 1_000_000_000, 900_000_000, _utc(-60))):
            await c.execute("INSERT INTO kina_adjust(tid, pc_id, delta, before, after, why, at) VALUES(?,?,?,?,?,?,?)",
                            (tid, main.ns("main", pc), a2 - b, b, a2, "{}", at))
        await c.commit()
    v = await _collect(pc, 1_000_000_000, merge=True)
    ok("L-25 ★옛 서버가 되돌린 값에서 두 번 판 장부 — 재전송 1000M → 700M(둘 다)★", v == 700_000_000, str(v))


async def t_boot_heal():
    import aiosqlite
    pcs = {"PC-LH1": (690_980_731, -300_000_000), "PC-LH2": (261_773_318, -200_000_000)}
    for pc, (tot, d) in pcs.items():
        await _collect(pc, tot)
        await _sell(pc, d, "tid-" + pc)
        async with aiosqlite.connect(db.DB_PATH) as c:       # 운영 지금 모양(되돌려진 채)
            await c.execute("UPDATE char_info SET total_kina=? WHERE pc_id=?", (tot, main.ns("main", pc)))
            await c.commit()
    await _collect("PC-LH3", 123_456_789)                   # 장부 없는 카드
    healed = await db.heal_reverted_kina()
    got = {h["pc_id"]: h["after"] for h in healed}
    ok("L-26 ★부팅 복구: 되돌려진 카드만 사슬 끝으로 — 재전송을 안 기다린다★",
       got.get(main.ns("main", "PC-LH1")) == 390_980_731 and got.get(main.ns("main", "PC-LH2")) == 61_773_318
       and main.ns("main", "PC-LH3") not in got, str(got))
    v = (await db.get_char_info(main.ns("main", "PC-LH3")) or {}).get("total_kina")
    again = await db.heal_reverted_kina()
    ok("L-27 부팅 복구는 멱등(두 번째엔 0건) · 장부 없는 카드는 그대로", not [h for h in again if "PC-LH" in h["pc_id"]]
       and v == 123_456_789, "%s %s" % (again, v))


async def t_read_at_contract():
    # KF1 — 매크로가 kina_read_at(창고를 실제로 읽은 UTC 시각)을 보내는 판
    pc = "PC-LK1"
    # 새 전송의 collected_at = 보내는 순간(PC _now()) — 서버가 그걸 보낸 시각으로 쓴다(반증 v2 #1). 판독은 10분 전.
    # ★판독 시각은 한 번만 잰다★ — 두 번 따로 재면 그 사이 초가 넘어갈 때 «같은 판독» 이 1초 다른 새 판독이 돼
    #   가끔 1000 이 됐다(2026-09-24 verify 1/4 실패 — 코드가 아니라 시험의 초 경계)
    ra = _utc(-600)
    await _post(pc, {"total_kina": 1_000_000_000, "collected_at": _utc(0), "kina_read_at": ra, "kina_seq": 1})
    await _sell(pc, -200_000_000, "tid-LK1")               # 1000 → 800
    v = await _post(pc, {"total_kina": 1_000_000_000, "merge": True, "collected_at": ra,
                         "kina_read_at": ra, "kina_seq": 1})
    ok("L-28 같은 판독의 재전송(read_at·seq 같음)은 창고키나를 안 덮는다", v == 800_000_000, str(v))
    #   늦게 도착 = 매크로의 재전송(resend:true, 옛 collected_at 그대로 — 보낸 시각으로 안 쓴다)
    v = await _post(pc, {"total_kina": 1_200_000_000, "collected_at": _utc(-300), "kina_read_at": _utc(-300), "kina_seq": 2,
                         "resend": True})
    ok("L-29 ★판매 전에 읽었는데 늦게 도착한 새 판독(보내기 실패 뒤, 반증 ③) → 1200 − 판매 200 = 1000★",
       v == 1_000_000_000, str(v))
    v = await _post(pc, {"total_kina": 1_000_000_000, "collected_at": _utc(+5), "kina_read_at": _utc(+5), "kina_seq": 3})
    ok("L-30 판매 뒤에 읽은 새 판독은 그대로(옛 before 와 같아도 — F1 이 표식으로 닫힌다)", v == 1_000_000_000, str(v))
    v = await _post(pc, {"total_kina": 1_100_000_000, "collected_at": _utc(-300), "kina_read_at": _utc(-300), "kina_seq": 2})
    ok("L-31 마지막 판독보다 옛 판독은 무시(값이 달라도)", v == 1_000_000_000, str(v))
    v = await _post(pc, {"total_kina": 5, "kina_seq": 2})
    ok("L-32 read_at 없이 seq 만 와도 옛 순번은 무시", v == 1_000_000_000, str(v))
    for bad in ("0001-01-01T00:00:00+09:00", "9999-12-31T23:59:59-01:00", "x", 12345):
        v = await _post("PC-LK2", {"total_kina": 7, "collected_at": bad if isinstance(bad, str) else None,
                                   "kina_read_at": bad})
    ok("L-33 끝값·엉터리 시각은 500 이 아니다(판독 표식을 무시하고 저장, 반증 v2 #4)", v == 7, str(v))


async def t_kst_naive_is_not_sent():
    # 매크로는 collected_at 을 UTC 로 보낸다(lc report_module._now = datetime.now(timezone.utc)) — 운영 실측:
    #   PC-10c collected_at 07:46:28 ↔ 그 PC 로그 «로컬 저장 완료» 16:46:28 KST. KST 는 오프셋을 붙여야 옮긴다.
    ok("L-34 판독 시각: +09:00 는 9시간 빼서 UTC · 오프셋 없는 값은 UTC 그대로",
       db._ca_utc("2026-09-23T16:46:28+09:00") == "2026-09-23T07:46:28" and db._ca_utc("2026-09-23T07:46:28") == "2026-09-23T07:46:28",
       "%s %s" % (db._ca_utc("2026-09-23T16:46:28+09:00"), db._ca_utc("2026-09-23T07:46:28")))


async def t_ws_frame_kina0():
    # R-5 — 전체수집이 키나를 못 읽어 0 을 보내면 DB 는 기존값을 지키고, 화면 프레임도 그 값이어야 한다
    pc = "PC-LR5"
    await _collect(pc, 700_000_000)
    sent = []
    orig = main.manager.broadcast

    async def cap(msg, tenant=None, *a, **k):
        sent.append(msg)
    main.manager.broadcast = cap
    try:
        await _collect(pc, 0, merge=False, ca=_utc(+1))
    finally:
        main.manager.broadcast = orig
    fr = [m for m in sent if isinstance(m, dict) and m.get("type") == "char_info"]
    ok("L-35 키나 0(못 읽음) 전체수집의 화면 프레임도 기존값(반증 R-5)", fr and fr[-1].get("total_kina") == 700_000_000,
       str([m.get("total_kina") for m in fr]))


async def t_concurrent():
    pc = "PC-L30"
    await _collect(pc, 800_000_000)
    for i in range(10):
        await asyncio.gather(_collect(pc, 800_000_000, merge=True), _sell(pc, -1_000_000, "tid-L30-%d" % i))
    v = await _collect(pc, 800_000_000, merge=True)
    ok("L-17 재전송·차감이 동시에 몰려도 끝값 = 장부 사슬 끝(8억 − 10×100만)", v == 790_000_000, str(v))


async def t_forced_interleave():
    # 재전송 판정(읽기)과 저장(쓰기) 사이에 판매가 끼는 판을 ★일부러★ 만든다 — BEGIN IMMEDIATE 가 없으면 그 판매가 사라진다
    pc = "PC-LI1"
    await _collect(pc, 800_000_000)
    await _sell(pc, -100_000_000, "tid-LI1a")               # 800 → 700
    orig = db._kina_ledger_replay

    async def slow(*a, **k):
        r = await orig(*a, **k)
        await asyncio.sleep(0.3)
        return r
    db._kina_ledger_replay = slow
    try:
        async def later_sale():
            await asyncio.sleep(0.1)
            return await _sell(pc, -50_000_000, "tid-LI1b")
        await asyncio.gather(_collect(pc, 800_000_000, merge=True), later_sale())
    finally:
        db._kina_ledger_replay = orig
    v = (await db.get_char_info(main.ns("main", pc)) or {}).get("total_kina")
    ok("L-36 ★재전송 판정 도중 끼어든 판매도 안 사라진다(한 트랜잭션) → 650M★", v == 650_000_000, str(v))


def test_all():
    run_all([t_resend_keeps_deduction, t_fresh_read_not_double, t_chain, t_already_reverted_heals,
             t_clock_independent_and_isolation, t_refuter_2026_09_23, t_reverted_then_new_sale, t_boot_heal,
             t_read_at_contract, t_kst_naive_is_not_sent, t_ws_frame_kina0, t_concurrent, t_forced_interleave])
    finish("test_kina_ledger", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
