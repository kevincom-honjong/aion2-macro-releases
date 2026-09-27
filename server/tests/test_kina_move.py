# -*- coding: utf-8 -*-
"""매니아 판매가 엉뚱한 카드에서 빠진 것 (2026-09-27 아이온2).

거래 #07963787(1억9천)의 글은 매니아 장부상 PC-21c 계정인데 팜뷰가 PC-02b 카드로 보냈다 →
PC-02b 215,319,919 → 25,319,919, PC-21c 는 그대로 190,184,667. 서버는 카드를 고르지 않는다(팜뷰가 준 pc_id).
  · 데이터: 부팅 때 main._kina_fix_moves 가 장부 줄을 PC-21c 로 옮기고 두 카드를 맞춘다(database.move_kina_adjust).
  · 앞으로: why.booked_pc(매니아가 적은 카드)가 pc_id 와 다르면 409 — 빼지 않고 그 카드 로그에 남긴다.

    cd updater/server && python -X utf8 tests/test_kina_move.py
"""
import json

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 34
TOK = "fvsecret-km"
H = {"X-FV-Token": TOK}


def _utc(delta_s):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%S")


def _epoch(delta_s):
    import time
    return int(time.time() + delta_s)


async def _read(pc, total, read_at, seq, resend=False):
    # 새 전송의 collected_at = 보내는 순간(PC 시계) — 서버는 그것으로 PC 시계를 고쳐 read_srv 를 만든다(test_kina_ledger L-28)
    body = {"characters": [{"slot": 1, "name": "러닝"}], "total_kina": total, "collected_at": _utc(0),
            "kina_read_at": read_at, "kina_seq": seq}
    if resend:
        body.update(resend=True, collected_at=read_at)   # 늦게 도착한 판독(매크로 재전송 — 옛 collected_at 그대로)
    r = await main.receive_char_info(pc, Req(body))
    assert r.status_code == 200, r.body
    return await _kina(pc)


async def _kina(pc):
    return (await db.get_char_info(main.ns("main", pc)) or {}).get("total_kina")


async def _sell(pc, delta, tid, **why):
    main.FV_TOKEN = TOK
    w = dict({"tid": tid, "server": "테스트", "man": -delta // 10000, "won": 1, "src": "itemmania"}, **why)
    r = await main.fv_kina_adjust(Req({"pc_id": pc, "delta_kina": delta, "why": w}, api_key=None, headers=H))
    return r.status_code, json.loads(bytes(r.body))


async def _row(tid):
    return await db.kina_adjust_row(tid)


async def t_prod_case():
    main.FV_TENANT = "main"
    # 운영 그대로: PC-02b 판독 12:34:50Z(판매 전) · PC-21c 판독 05:30Z(판매 전) · 판매 15:23 KST(인계 시각 = 방금)
    await _read("PC-02b", 215_319_919, _utc(-7200), 1)
    await _read("PC-21c", 190_184_667, _utc(-9000), 1)
    st, j = await _sell("PC-02b", -190_000_000, "07963787", hand_at=_epoch(-60))
    ok("KM-0 재현: 팜뷰가 PC-02b 로 보내면 PC-02b 가 25,319,919", st == 200 and await _kina("PC-02b") == 25_319_919, str(j))
    await _fix(FWD)
    ok("KM-1 ★PC-02b 215,319,919 로 되돌림★", await _kina("PC-02b") == 215_319_919, str(await _kina("PC-02b")))
    ok("KM-2 ★PC-21c 190,184,667 − 1억9천 = 184,667★", await _kina("PC-21c") == 184_667, str(await _kina("PC-21c")))
    r = await _row("07963787")
    ok("KM-3 장부 줄이 PC-21c 로(before/after 도 PC-21c 값) · 옮긴 흔적",
       r["pc_id"] == main.ns("main", "PC-21c") and r["before"] == 190_184_667 and r["after"] == 184_667
       and (r["why"].get("moved") or {}).get("from") == main.ns("main", "PC-02b"), str(r))
    l2 = [x.get("message") for x in await db.get_logs(main.ns("main", "PC-02b"), 5)]
    l21 = [x.get("message") for x in await db.get_logs(main.ns("main", "PC-21c"), 5)]
    ok("KM-4 두 카드 로그줄(A2 증거)", any("되돌림" in str(m) and "07963787" in str(m) for m in l2)
       and any("옮겨 옴" in str(m) and "07963787" in str(m) for m in l21), "%s %s" % (l2[:1], l21[:1]))
    await _fix(FWD)
    ok("KM-5 부팅마다 돌아도 멱등", await _kina("PC-02b") == 215_319_919 and await _kina("PC-21c") == 184_667)
    # 그 뒤 판독 사다리가 새 카드에서 옳게 돈다
    v = await _read("PC-02b", 215_319_919, _utc(-3600), 2, resend=True)
    ok("KM-6 PC-02b 판매 전 판독이 늦게 와도 다시 안 뺀다(장부 줄이 떠났다)", v == 215_319_919, str(v))
    v = await _read("PC-21c", 190_184_667, _utc(-3600), 2, resend=True)
    ok("KM-7 PC-21c 판매 전 판독이 늦게 오면 판매를 뺀 값", v == 184_667, str(v))
    v = await _read("PC-21c", 184_667, _utc(+2), 3)
    ok("KM-8 PC-21c 판매 뒤 판독은 그대로", v == 184_667, str(v))
    st, j = await _sell("PC-21c", -190_000_000, "07963787", hand_at=_epoch(-60))
    ok("KM-9 팜뷰가 PC-21c 로 다시 보내면 dup(두 번 안 뺀다)", st == 200 and j.get("dup") is True
       and await _kina("PC-21c") == 184_667, str(j))
    st, j = await _sell("PC-02b", -190_000_000, "07963787", hand_at=_epoch(-60))
    ok("KM-10 PC-02b 로 다시 보내면 409(다른 카드에서 뺐다)", st == 409 and await _kina("PC-02b") == 215_319_919, str(j))


async def t_undo():
    # 아이온2 2026-09-27 HOLD — PC-02b 가 주인님의 진짜 선택이었으면 되돌린다(같은 길로, 반대 방향). 옮긴 직후 상태에서.
    for pc, v, ra in (("PC-U02b", 215_319_919, -7200), ("PC-U21c", 190_184_667, -9000)):
        await _read(pc, v, _utc(ra), 1)
    await _sell("PC-U02b", -190_000_000, "u2026092707963787", hand_at=_epoch(-60))
    await _fix((("PC-U02b", -190_000_000, "07963787", "PC-U21c"),))
    ok("KM-23 (준비) 옮긴 상태 184,667 / 215,319,919",
       await _kina("PC-U21c") == 184_667 and await _kina("PC-U02b") == 215_319_919)
    await _fix((("PC-U21c", -190_000_000, "07963787", "PC-U02b"),))
    ok("KM-24 ★되돌리기: PC-02b 25,319,919 · PC-21c 190,184,667★",
       await _kina("PC-U02b") == 25_319_919 and await _kina("PC-U21c") == 190_184_667,
       "%s %s" % (await _kina("PC-U02b"), await _kina("PC-U21c")))
    r = await _row("u2026092707963787")
    ok("KM-25 장부 줄도 PC-02b 로 돌아온다(before 215,319,919 → after 25,319,919)",
       r["pc_id"] == main.ns("main", "PC-U02b") and r["before"] == 215_319_919 and r["after"] == 25_319_919, str(r))
    await _fix((("PC-U21c", -190_000_000, "07963787", "PC-U02b"),))
    ok("KM-26 되돌리기도 멱등", await _kina("PC-U02b") == 25_319_919 and await _kina("PC-U21c") == 190_184_667)


FWD = (("PC-02b", -190_000_000, "07963787", "PC-21c"),)


async def _fix(moves):
    real = main.KINA_MOVES_20260927
    main.KINA_MOVES_20260927 = moves
    try:
        await main._kina_fix_moves()
    finally:
        main.KINA_MOVES_20260927 = real


async def t_known_reads():
    # 판매 뒤에 새로 읽은 카드는 판독이 진실 — 옮길 때 더하지도 빼지도 않는다
    await _read("PC-KM1", 50_000_000, _utc(-7200), 1)
    await _read("PC-KM2", 300_000_000, _utc(-7200), 1)
    await _sell("PC-KM1", -10_000_000, "km-known", hand_at=_epoch(-600))
    await _read("PC-KM1", 50_000_000, _utc(-30), 2)          # 판매 뒤 판독(그 계정은 안 팔았으니 그대로 50)
    await _read("PC-KM2", 290_000_000, _utc(-30), 2)         # 판매 뒤 판독(이미 반영)
    r = await db.move_kina_adjust("km-known", main.ns("main", "PC-KM2"))
    ok("KM-11 옛 카드: 판매 뒤 판독이 왔으면 되돌리지 않는다", await _kina("PC-KM1") == 50_000_000, str(r))
    ok("KM-12 새 카드: 판매 뒤 판독이면 또 빼지 않는다", await _kina("PC-KM2") == 290_000_000, str(r))
    r = await db.move_kina_adjust("km-none", main.ns("main", "PC-KM2"))
    ok("KM-13 없는 tid → missing, 아무것도 안 바꿈", r.get("missing") is True and await _kina("PC-KM2") == 290_000_000)
    r = await db.move_kina_adjust("km-known", main.ns("main", "PC-KMX"))
    ok("KM-14 새 카드 행이 없으면 no_card, 장부 그대로", r.get("no_card") and
       (await _row("km-known"))["pc_id"] == main.ns("main", "PC-KM2"), str(r))


async def t_slow_pc_clock():
    # 반증 MED — PC 시계가 늦으면 read_at(PC 시계)이 판매보다 앞이어도 read_srv(서버 시계)는 판매 뒤다 → 또 빼면 안 된다
    import aiosqlite
    await _read("PC-KS1", 10_000_000, _utc(-7200), 1)
    await _read("PC-KS2", 190_184_667, _utc(-7200), 1)
    await _sell("PC-KS1", -190_000_000 // 100, "ks-slow")
    await _read("PC-KS2", 188_284_667, _utc(+2), 2)            # 판매 뒤 판독(이미 반영)
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE kina_read SET read_at=?, read_srv=? WHERE pc_id=?",
                        (_utc(-10800), _utc(+2), main.ns("main", "PC-KS2")))
        await c.commit()
    await db.move_kina_adjust("ks-slow", main.ns("main", "PC-KS2"))
    ok("KM-22 ★늦은 PC 시계: read_srv 가 판매 뒤면 새 카드에서 또 안 뺀다★", await _kina("PC-KS2") == 188_284_667,
       str(await _kina("PC-KS2")))


async def t_ambiguous_no_guess():
    await _read("PC-KA1", 100_000_000, _utc(-7200), 1)
    await _read("PC-KA2", 100_000_000, _utc(-7200), 1)
    await _sell("PC-KA1", -1_000_000, "a-555-1")
    await _sell("PC-KA1", -1_000_000, "b-555-2")
    real = main.KINA_MOVES_20260927
    main.KINA_MOVES_20260927 = (("PC-KA1", -1_000_000, "555", "PC-KA2"),)
    try:
        await main._kina_fix_moves()
    finally:
        main.KINA_MOVES_20260927 = real
    ok("KM-15 ★같은 번호·금액이 두 줄이면 짐작해 옮기지 않는다★",
       await _kina("PC-KA1") == 98_000_000 and await _kina("PC-KA2") == 100_000_000, str(await _kina("PC-KA2")))


async def t_booked_info_only():
    # 아이온2 2026-09-27 — 주인님이 고른 카드(pc_id)가 진실. booked_pc 가 달라도 막지 않고 pc_id 에서 뺀다, 로그는 info 한 줄.
    await _read("PC-KB1", 500_000_000, _utc(-7200), 1)
    st, j = await _sell("PC-KB1", -100_000_000, "kb-mis", booked_pc="PC-KB2")
    ok("KM-16 ★booked_pc 가 달라도 200 · pc_id 에서 뺀다★", st == 200 and j.get("ok") is True
       and await _kina("PC-KB1") == 400_000_000 and (await _row("kb-mis"))["pc_id"] == main.ns("main", "PC-KB1"), str(j))
    logs = [x for x in await db.get_logs(main.ns("main", "PC-KB1"), 5)]
    ok("KM-17 다름은 그 카드 로그에 info 로만", any("카드 다름" in str(x.get("message")) and "kb-mis" in str(x.get("message"))
       and x.get("level") == "info" for x in logs), str([(x.get("level"), x.get("message")) for x in logs][:2]))
    st, j = await _sell("PC-KB1", -100_000_000, "kb-ok", booked_pc="pc-kb1")
    ok("KM-18 같은 카드면 알림 없이 뺀다", st == 200 and await _kina("PC-KB1") == 300_000_000, str(j))
    st, j = await _sell("PC-KB1", -1, "kb-bad", booked_pc=7)
    ok("KM-19 booked_pc 가 글자가 아니어도 막지 않는다", st == 200 and await _kina("PC-KB1") == 299_999_999, str(j))
    st, j = await _sell("PC-KB1", -1_000_000, "kb-none")
    ok("KM-20 booked_pc 없음 → 그대로 뺀다", st == 200 and await _kina("PC-KB1") == 298_999_999, str(j))
    ok("KM-21 «모르면 막기» 스위치는 없다(영구 삭제)", not hasattr(main, "FV_KINA_REQUIRE_BOOKED"))


async def _tids(pc):
    ci = {r["pc_id"]: r for r in await db.get_all_char_info()}.get(main.ns("main", pc))
    card = {}
    main._attach_char_info(card, ci)
    return card.get("_kina_adj_tids"), card.get("_kina_read_at")


async def t_adj_tids():
    # 아이온2 2026-09-27 — /status 카드 _kina_adj_tids: 판독(_kina_read_at) 뒤에 이 카드에서 뺀 차감 tid(창구가 두 번 안 빼게)
    ra = _utc(-7200)
    await _read("PC-T1", 500_000_000, ra, 1)
    await _read("PC-T2", 300_000_000, _utc(-7200), 1)
    t, r0 = await _tids("PC-T1")
    ok("KM-27 판독만 있으면 빈 목록(늘 목록)", t == [], str(t))
    await _sell("PC-T1", -100_000_000, "tt-1", hand_at=_epoch(-60))
    await _sell("PC-T1", -1_000_000, "tt-2")
    t, r1 = await _tids("PC-T1")
    ok("KM-28 ★차감 → tid 가 판독 뒤 순서대로★ · _kina_read_at 은 그대로(판독 시각)", t == ["tt-1", "tt-2"] and r1 == r0, "%s %s %s" % (t, r0, r1))
    await db.move_kina_adjust("tt-1", main.ns("main", "PC-T2"))
    ok("KM-29 ★옮기면 tid 가 카드를 따라간다★", (await _tids("PC-T1"))[0] == ["tt-2"] and (await _tids("PC-T2"))[0] == ["tt-1"],
       "%s %s" % ((await _tids("PC-T1"))[0], (await _tids("PC-T2"))[0]))
    await db.move_kina_adjust("tt-1", main.ns("main", "PC-T1"))
    ok("KM-30 되돌리면 다시 원래 카드로", (await _tids("PC-T1"))[0] == ["tt-1", "tt-2"] and (await _tids("PC-T2"))[0] == [],
       str((await _tids("PC-T1"))[0]))
    await _read("PC-T1", 399_000_000, _utc(+2), 2)
    t, r2 = await _tids("PC-T1")
    ok("KM-31 ★새 판독이 오면 빈다★(판독 시각도 새것)", t == [] and r2 and r2 != r0, "%s %s" % (t, r2))
    # 판독 전에 인계된 판매가 판독 뒤에 기록되면 판독이 이미 안다 → 목록에 없다
    await _read("PC-T3", 100_000_000, _utc(-600), 1)
    await _sell("PC-T3", -1_000, "tt-3", hand_at=_epoch(-7300))     # 기록은 판독 뒤, 인계는 판독 전
    await _sell("PC-T3", -2_000, "tt-4", hand_at=_epoch(-60))       # 인계도 판독 뒤
    ok("KM-32 인계가 판독 전이면 목록에 없다(_kina_after_read 와 같은 규칙)", (await _tids("PC-T3"))[0] == ["tt-4"],
       str((await _tids("PC-T3"))[0]))
    card = {}
    main._attach_char_info(card, None)
    ok("KM-33 char_info 없는 카드도 빈 목록", card.get("_kina_adj_tids") == [], str(card))


def test_all():
    run_all([t_prod_case, t_undo, t_known_reads, t_slow_pc_clock, t_ambiguous_no_guess, t_booked_info_only, t_adj_tids])
    finish("test_kina_move", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
