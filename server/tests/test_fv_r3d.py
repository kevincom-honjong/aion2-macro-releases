# -*- coding: utf-8 -*-
"""팜뷰 #201 r3d 요청 둘 (2026-09-24 아이온2 → 대시보드, SHARED_ISSUES_팜뷰 «r3d»).

  KR  FV 스냅샷 `progress.kina_read_age_s` — 마지막으로 ★받아들인★ 창고 판독(kina_read.read_srv) 뒤 몇 초. 정수, 모르면 10^9.
      merge 판독(사냥 끝 창고 읽기, 사고 569 — collected_at 을 안 바꾼다)에도 바뀐다. 재전송·판독 시각 없는 보고는 안 바꾼다.
  P6  kina_adjust why.hand_at(인계 시각) — 인계가 판독 이전이면 늦게 도착한 판독 값에서 그 판매를 다시 안 뺀다(r3e p6)
  N   POST /api/fv/notify {key, text, pc_id?} — 팜뷰 → 주인님 텔레그램. 같은 key 한 번만 · 분/시간 상한 429 · 감사 장부
      (fv_notify 표, GET /api/fv/notify) · 카드 로그 한 줄.

    cd updater/server && python -X utf8 tests/test_fv_r3d.py
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 56
TOK = "fvsecret-r3d"
H = {"X-FV-Token": TOK}
C1 = [{"slot": 1, "name": "러닝"}]
UNK = 10 ** 9


def U(ds):
    return (datetime.now(timezone.utc) + timedelta(seconds=ds)).strftime("%Y-%m-%dT%H:%M:%S")


def _body(r):
    return json.loads(bytes(r.body))


async def _post(pc, body):
    r = await main.receive_char_info(pc, Req(dict({"characters": C1}, **body)))
    assert r.status_code == 200, r.body


async def _prog(pc):
    main.FV_TOKEN = TOK
    r = await main.fv_snapshot(Req({}, api_key=None, headers=H))
    assert r.status_code == 200, r.body
    b = _body(r)
    p = ((b.get("pcs") or {}).get(pc) or {}).get("progress") or {}
    # _read_ts = 판독 시각(epoch) — 본문 만든 때(지금 − cache_age_s) − 나이. 느린 기계에서도 두 스냅샷 사이 흔들리지 않는다.
    if isinstance(p.get("kina_read_age_s"), int):
        p["_read_ts"] = main.time.time() - float(b.get("cache_age_s") or 0) - p["kina_read_age_s"]
    return p


async def t_kina_read_age():
    pc = "PC-KR1"
    await db.upsert_status(pc, {"pc_id": pc, "status": "idle"})
    # 전체수집 — 창고는 100초 전에 읽고 지금 보냄(sent_at), 전체수집 시각은 2시간 전(옛 값이라 치고)
    await _post(pc, {"total_kina": 900_000_000, "kina_read_at": U(-100), "kina_seq": 1, "sent_at": U(0),
                     "collected_at": U(-7200)})
    p = await _prog(pc)
    ok("KR-1 새 판독 뒤 kina_read_age_s ≈ 100 (정수)", isinstance(p.get("kina_read_age_s"), int) and 95 <= p["kina_read_age_s"] <= 130,
       str(p.get("kina_read_age_s")))
    ok("KR-1b 대조 — kina_age_s 는 전체수집 시각(≈7200) 그대로", 7190 <= int(p.get("kina_age_s") or 0) <= 7300, str(p.get("kina_age_s")))
    # ★사냥 끝 merge 판독(사고 569)★ — collected_at 을 안 보낸다(서버도 안 바꾼다). 3초 캐시 안이라도 새 값이어야(세대).
    await _post(pc, {"total_kina": 700_000_000, "merge": True, "kina_read_at": U(-10), "kina_seq": 2, "sent_at": U(0)})
    p = await _prog(pc)
    ok("KR-2 ★merge 판독(collected_at 안 바뀜)에도 kina_read_age_s 가 새로(≈10) — 3초 캐시를 건너★",
       isinstance(p.get("kina_read_age_s"), int) and 5 <= p["kina_read_age_s"] <= 40 and p.get("total_kina") == 700_000_000,
       f"{p.get('kina_read_age_s')} {p.get('total_kina')}")
    ok("KR-2b 대조 — merge 판독은 kina_age_s(전체수집)를 안 바꾼다", 7190 <= int(p.get("kina_age_s") or 0) <= 7300, str(p.get("kina_age_s")))
    # 같은 판독의 재전송(부팅) — 판독 시각 그대로 → 안 바뀐다. 옛 판독(seq 1) 재전송도 안 바뀐다.
    await _post(pc, {"total_kina": 900_000_000, "kina_read_at": U(-100), "kina_seq": 1, "resend": True,
                     "collected_at": U(-7200)})
    p2 = await _prog(pc)
    ok("KR-3 ★옛 판독 재전송은 kina_read_age_s 를 안 바꾼다(젊어지지도 늙지도)★",
       p2.get("total_kina") == 700_000_000 and abs(p2["_read_ts"] - p["_read_ts"]) <= 2,
       f"{p2.get('total_kina')} {p['kina_read_age_s']}→{p2.get('kina_read_age_s')}")
    # 판독 시각이 없는 옛 매크로 보고 — 판독 표식 카드에선 안 바꾼다
    await _post(pc, {"total_kina": 650_000_000, "merge": True})
    p3 = await _prog(pc)
    ok("KR-4 판독 시각 없는 보고는 kina_read_age_s 를 안 바꾼다", abs(p3["_read_ts"] - p["_read_ts"]) <= 2,
       f"{p['kina_read_age_s']}→{p3.get('kina_read_age_s')}")
    # ★PC 시계 어긋남 (아이온2 M9)★ — 나이는 PC 시계 판독 시각(raw)이 아니라 서버 시계로 옮긴 read_srv 기준이다
    pcs = "PC-KR4"
    await db.upsert_status(pcs, {"pc_id": pcs, "status": "idle"})
    await _post(pcs, {"total_kina": 800_000_000, "kina_read_at": U(-100 - 600), "kina_seq": 1, "sent_at": U(-600),
                      "collected_at": U(-600)})           # PC 시계 10분 늦음, 100초 전에 읽음
    p = await _prog(pcs)
    ok("KR-8 ★PC 시계가 10분 늦어도 kina_read_age_s ≈ 100(서버 시계) — raw 판독 시각이면 700★",
       95 <= int(p.get("kina_read_age_s") or 0) <= 130, str(p.get("kina_read_age_s")))
    await _post(pcs, {"total_kina": 790_000_000, "merge": True, "kina_read_at": U(-30 + 240), "kina_seq": 2, "sent_at": U(240)})
    p = await _prog(pcs)
    ok("KR-8b ★PC 시계가 4분 빨라도 merge 판독 나이 ≈ 30 — raw 면 미래라 0★",
       25 <= int(p.get("kina_read_age_s") or 0) <= 60 and p.get("total_kina") == 790_000_000,
       f"{p.get('kina_read_age_s')} {p.get('total_kina')}")
    # 표식이 한 번도 없던 카드 = 모름 10^9 (정수)
    pc2 = "PC-KR2"
    await db.upsert_status(pc2, {"pc_id": pc2, "status": "idle"})
    await _post(pc2, {"total_kina": 500_000_000, "collected_at": U(-60)})
    p = await _prog(pc2)
    ok("KR-5 판독 표식이 없던 카드 → kina_read_age_s = 10^9 (정수, kina_age_s 는 따로)",
       p.get("kina_read_age_s") == UNK and isinstance(p.get("kina_read_age_s"), int) and int(p.get("kina_age_s") or 0) < 200,
       f"{p.get('kina_read_age_s')} {p.get('kina_age_s')}")
    pc3 = "PC-KR3"
    await db.upsert_status(pc3, {"pc_id": pc3, "status": "idle"})
    main._fv_snap["body"] = None           # 상태만 바뀐 카드는 3초 캐시가 늦게 본다(설계) — 여기선 새로 조립
    p = await _prog(pc3)
    ok("KR-6 char_info 가 아예 없는 카드도 10^9", p.get("kina_read_age_s") == UNK, str(p.get("kina_read_age_s")))
    # 카드 삭제 뒤 같은 id 로 다시 오면 옛 판독 시각이 남지 않는다(kina_read 도 지운다 — 기존 규칙)
    await db.delete_pc_all_data(main.ns("main", pc))
    await db.upsert_status(pc, {"pc_id": pc, "status": "idle"})
    await _post(pc, {"total_kina": 100_000_000, "collected_at": U(-30)})
    p = await _prog(pc)
    ok("KR-7 카드 삭제 뒤엔 옛 판독 시각이 안 남는다(10^9)", p.get("kina_read_age_s") == UNK, str(p.get("kina_read_age_s")))


# ── p6: 수집 중이던 판독이 판매 기록 뒤에 도착 ──
def E(ds):
    return (datetime.now(timezone.utc) + timedelta(seconds=ds)).timestamp()


async def _sell(pc, delta, tid, **why):
    main.FV_TOKEN = TOK
    r = await main.fv_kina_adjust(Req({"pc_id": pc, "delta_kina": delta, "why": dict({"tid": tid}, **why)},
                                      api_key=None, headers=H))
    return r.status_code, _body(r)


async def _stored(pc):
    return (await db.get_char_info(main.ns("main", pc)) or {}).get("total_kina")


async def t_p6():
    # 판독 1 (한 시간 전) 1000 → 인계(200초 전) → 창고 판독 2(100초 전, 판매 반영 700) 가 수집 중 → 팜뷰가 지금 차감 기록 → 판독 2 도착
    pc = "PC-P6A"
    await _post(pc, {"total_kina": 1000_000_000, "kina_read_at": U(-3600), "kina_seq": 1, "sent_at": U(0),
                     "collected_at": U(-3600)})
    code, b = await _sell(pc, -300_000_000, "tid-P6A", hand_at=E(-200))
    ok("P6-0 대조 — 차감 200 (1000→700)", code == 200 and b.get("after") == 700_000_000, f"{code} {b}")
    await _post(pc, {"total_kina": 700_000_000, "kina_read_at": U(-100), "kina_seq": 2, "sent_at": U(0), "collected_at": U(0)})
    v = await _stored(pc)
    ok("P6-a ★인계가 판독 전이면 늦게 온 판독(판매 반영 700)에서 또 빼지 않는다 — 옛 코드는 400(두 번 빼기)★", v == 700_000_000, str(v))

    # 인계가 판독 ★뒤★ — 판독(1000, 판매 모름)이 늦게 와도 그 판매는 뺀다(P0 v3 반증 ③ 그대로)
    pc = "PC-P6B"
    await _post(pc, {"total_kina": 1000_000_000, "kina_read_at": U(-3600), "kina_seq": 1, "sent_at": U(0),
                     "collected_at": U(-3600)})
    await _sell(pc, -300_000_000, "tid-P6B", hand_at=E(-50))
    await _post(pc, {"total_kina": 1000_000_000, "kina_read_at": U(-100), "kina_seq": 2, "sent_at": U(0), "collected_at": U(0)})
    v = await _stored(pc)
    ok("P6-b ★인계가 판독 뒤면 판독이 모르는 판매 — 뺀다(1000→700)★", v == 700_000_000, str(v))

    # 인계 시각 모름(옛 팜뷰) — 예전대로 뺀다(KINA_UNKNOWN_HAND_DEDUCT)
    pc = "PC-P6C"
    await _post(pc, {"total_kina": 1000_000_000, "kina_read_at": U(-3600), "kina_seq": 1, "sent_at": U(0),
                     "collected_at": U(-3600)})
    await _sell(pc, -300_000_000, "tid-P6C")
    await _post(pc, {"total_kina": 1000_000_000, "kina_read_at": U(-100), "kina_seq": 2, "sent_at": U(0), "collected_at": U(0)})
    v = await _stored(pc)
    ok("P6-c 인계 시각이 없으면 예전대로 판독 뒤 기록된 차감을 뺀다(1000→700)", v == 700_000_000 and db.KINA_UNKNOWN_HAND_DEDUCT is True, str(v))

    # 팜뷰 시계가 빨라 인계가 «미래»(하루 안)로 와도 받는다 — 판독(100초 전)보다 뒤이므로 뺀다
    pc = "PC-P6D"
    await _post(pc, {"total_kina": 1000_000_000, "kina_read_at": U(-3600), "kina_seq": 1, "sent_at": U(0),
                     "collected_at": U(-3600)})
    await _sell(pc, -300_000_000, "tid-P6D", hand_at=E(+3000))
    await _post(pc, {"total_kina": 1000_000_000, "kina_read_at": U(-100), "kina_seq": 2, "sent_at": U(0), "collected_at": U(0)})
    ok("P6-d 미래 인계(팜뷰 시계 빠름, 하루 안)는 받고 «판독 뒤 인계» 로 뺀다(700)", await _stored(pc) == 700_000_000,
       str(await _stored(pc)))

    # 같은 초 — 인계와 판독이 같은 초면 «판독 전» 으로 친다(모르면 덜 빼는 쪽)
    pc = "PC-P6E"
    await _post(pc, {"total_kina": 1000_000_000, "kina_read_at": U(-3600), "kina_seq": 1, "sent_at": U(0),
                     "collected_at": U(-3600)})
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=100)
    await _sell(pc, -300_000_000, "tid-P6E", hand_at=base.timestamp() + 0.4)
    await _post(pc, {"total_kina": 700_000_000, "kina_read_at": base.strftime("%Y-%m-%dT%H:%M:%S"), "kina_seq": 2,
                     "sent_at": U(0), "collected_at": U(0)})
    ok("P6-h 인계와 판독이 같은 초면 판독 전으로 친다 — 또 안 뺀다(700)", await _stored(pc) == 700_000_000, str(await _stored(pc)))

    # 모양 검사 — 글자·bool·밀리초·음수·하루 넘는 미래는 400(조용히 버리면 두 번 빼기가 조용히 돌아온다), None 은 «모름»
    got = []
    for h in ("2026-09-24 10:00:00", True, E(0) * 1000, -5, E(2 * 86400)):
        c, e = await _sell("PC-P6A", -1, f"tid-P6X-{len(got)}", hand_at=h)
        got.append(c)
    ok("P6-e why.hand_at 모양이 틀리면 400(글자·bool·밀리초·음수·하루 넘는 미래)", got == [400] * 5, str(got))
    c, e = await _sell("PC-P6A", -1, "tid-P6N", hand_at=None)
    ok("P6-f hand_at:null 은 «모름» 으로 받는다(200)", c == 200, f"{c} {e}")
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as cn:
        async with cn.execute("SELECT why FROM kina_adjust WHERE tid='tid-P6A'") as cur:
            w = json.loads((await cur.fetchone())[0])
    ok("P6-g 장부 why 에 hand_at 이 그대로 남는다(감사)", abs(float(w.get("hand_at") or 0) - E(-200)) < 30, str(w))


# ── notify ──
SENT: list = []


async def _tg_ok(chat, text):
    SENT.append(text)
    return 1000 + len(SENT)


async def _tg_fail(chat, text):
    SENT.append(text)
    return None


async def _n(body, headers=H):
    main.FV_TOKEN = TOK
    r = await main.fv_notify(Req(body, api_key=None, headers=headers))
    return r.status_code, _body(r)


def _reset():
    SENT.clear()
    main._FV_NOTIFY_TRIES.clear()
    main._FV_NOTIFY_SENT_TS.clear()
    main._FV_NOTIFY_INFLIGHT.clear()
    main._FV_NOTIFY_SENT.clear()
    main._FV_NOTIFY_UNRECORDED.clear()


async def t_limit_math():
    """아이온2 합동 반증 D11 — 시간 상한 대기 초는 ★상한을 넘게 만든 그 보냄★(len−cap 번째)이 1시간에서 빠질 때까지다.
    [0](가장 옛 것)으로 재면 sold_fail 아닌 key 는 5칸 몫만큼 짧게 말해 팜뷰가 너무 일찍 다시 두드린다."""
    now = 1_000_000.0
    keep_t, keep_s = list(main._FV_NOTIFY_TRIES), list(main._FV_NOTIFY_SENT_TS)
    try:
        main._FV_NOTIFY_TRIES[:] = []
        main._FV_NOTIFY_SENT_TS[:] = [now - 3500 + 100 * i for i in range(main.FV_NOTIFY_PER_HOUR)]   # 3500초 전 … 1600초 전
        k = main.FV_NOTIFY_RESERVED                  # len − cap(15) = 5
        w_other = main._fv_notify_limit(now, "x:1")
        w_sold = main._fv_notify_limit(now, main.FV_NOTIFY_RESERVED_PREFIX + "t:1")
        ok("N-21 ★D11 시간 상한 대기 = 1시간 − (지금 − len−cap 번째 보냄) — sold_fail 아닌 key 는 [5] 기준 600초, [0] 이면 100초★",
           w_other == round(3600 - (now - main._FV_NOTIFY_SENT_TS[k]), 1) == 600.0, f"{w_other}")
        ok("N-21b sold_fail: key 는 20칸 다 차면 [0] 기준 100초", w_sold == 100.0, f"{w_sold}")
    finally:
        main._FV_NOTIFY_TRIES[:] = keep_t
        main._FV_NOTIFY_SENT_TS[:] = keep_s


async def t_notify():
    real = (main.tg_send_text, main.tg_enabled, main.tenant_chat_id, main.fv_notify_get, main.fv_notify_record)
    main.tg_enabled, main.tenant_chat_id = (lambda: True), (lambda t: "1")
    main.tg_send_text = _tg_ok
    _reset()
    try:
        code, b = await _n({"key": "k", "text": "x"}, headers={})
        ok("N-1 토큰 없으면 401", code == 401 and b.get("ok") is False, f"{code} {b}")
        bad = [({"text": "x"}, "key 없음"), ({"key": "k"}, "text 없음"), ({"key": "k" * 201, "text": "x"}, "key 너무 김"),
               ({"key": "k", "text": "x", "pc_id": "all"}, "pc all"), ({"key": 5, "text": "x"}, "key 숫자"), ("x", "본문 문자열")]
        got = []
        for bd, lb in bad:
            c, e = await _n(bd)
            got.append((lb, c, e.get("ok")))
        ok("N-2 잘못된 본문은 400 {ok:false} (key·text 필수, pc all 금지)", all(c == 400 and o is False for _, c, o in got) and not SENT,
           str(got))
        code, b = await _n({"key": "sold_fail:t1", "text": "차감 24시간 실패 — 챈가룽 2100만", "pc_id": "PC-N1"})
        ok("N-3 ★첫 알림은 텔레그램으로 한 통 — 200 sent·message_id★",
           code == 200 and b.get("sent") is True and b.get("message_id") == 1001 and SENT == ["[팜뷰] PC-N1 | 차감 24시간 실패 — 챈가룽 2100만"],
           f"{code} {b} {SENT}")
        code, b = await _n({"key": "sold_fail:t1", "text": "다른 글", "pc_id": "PC-N1"})
        ok("N-4 ★같은 key 는 다시 안 보낸다 — 200 dup:true·sent_at★", code == 200 and b.get("dup") is True and b.get("sent_at") and len(SENT) == 1,
           f"{code} {b} {len(SENT)}")
        ok("N-4b ★기억으로 답한 dup 은 장부를 안 쓴다(요청 수 1 그대로 — dup 은 상한이 없으니 DB 쓰기도 없다)★",
           (await db.fv_notify_get("sold_fail:t1") or {}).get("n") == 1, str(await db.fv_notify_get("sold_fail:t1")))

        async def _db_dead(*a, **k):
            raise sqlite3.OperationalError("database is locked")
        main.fv_notify_get, main.fv_notify_record = _db_dead, _db_dead
        code, b = await _n({"key": "sold_fail:t1", "text": "x"})
        main.fv_notify_get, main.fv_notify_record = real[3], real[4]
        ok("N-4c ★기억으로 답한 dup 은 DB 를 읽지도 않는다(DB 가 죽어도 200 dup)★", code == 200 and b.get("dup") is True, f"{code} {b}")
        main._FV_NOTIFY_SENT.clear()                     # 재배포 흉내 — 프로세스 기억이 없어도 표가 막는다
        code, b = await _n({"key": "sold_fail:t1", "text": "x"})
        ok("N-5 ★재시작 뒤에도 같은 key 는 dup(표가 막는다)★", code == 200 and b.get("dup") is True and len(SENT) == 1, f"{code} {b}")
        code, b = await _n({"key": "sold_fail:t1", "text": "x"})   # 재시작 뒤 둘째 dup — 첫 dup 이 기억에 올렸으니 장부 안 씀
        ok("N-5b 재시작 뒤 둘째 dup 은 기억으로(sent_at 도 장부 값)", code == 200 and b.get("dup") is True and b.get("sent_at"), f"{code} {b}")
        rows = {r["key"]: r for r in _body(await main.fv_notify_list(Req({}, api_key=None, headers=H)))["items"]}
        r1 = rows.get("sold_fail:t1") or {}
        ok("N-6 ★감사 장부 — status sent 그대로·요청 수 2(보냄 + 재시작 뒤 첫 dup)·보낸 글 그대로·message_id★",
           r1.get("status") == "sent" and r1.get("n") == 2 and r1.get("text") == "차감 24시간 실패 — 챈가룽 2100만"
           and r1.get("message_id") == 1001 and r1.get("sent_at"), str(r1))
        lines = [x.get("message") or "" for x in await db.get_logs(main.ns("main", "PC-N1"), limit=50)]
        ok("N-7 카드 로그에 «[팜뷰 알림] … (key …)» 한 줄", sum("[팜뷰 알림]" in x and "key sold_fail:t1" in x for x in lines) == 1, str(lines[:3]))

        # 텔레그램 실패 → 502 retry, key 는 안 쓰인다 → 다시 보내면 간다
        main.tg_send_text = _tg_fail
        code, b = await _n({"key": "sold_fail:t2", "text": "y"})
        main.tg_send_text = _tg_ok
        code2, b2 = await _n({"key": "sold_fail:t2", "text": "y"})
        ok("N-8 ★텔레그램 실패는 502 retry:true — 같은 key 재전송은 보낸다(실패가 key 를 먹지 않는다)★",
           code == 502 and b.get("retry") is True and code2 == 200 and b2.get("sent") is True, f"{code} {b} / {code2} {b2}")
        rows = {r["key"]: r for r in await db.fv_notify_recent(50)}
        ok("N-8b 장부 — 실패 뒤 성공은 sent, 요청 수 2", (rows.get("sold_fail:t2") or {}).get("status") == "sent"
           and (rows.get("sold_fail:t2") or {}).get("n") == 2, str(rows.get("sold_fail:t2")))

        # 분 상한 — 이미 이번 분에 3번(t1 1 + t2 실패·성공 2) → 새 key 는 429, dup 은 그래도 200
        code, b = await _n({"key": "sold_fail:t3", "text": "z"})
        ok("N-9 ★1분 상한(3) 넘으면 429 limited·retry:true·retry_after_s>0, 안 보낸다★",
           code == 429 and b.get("retry") is True and b.get("limited") is True and 0 < float(b.get("retry_after_s") or 0) <= 60
           and len(SENT) == 3, f"{code} {b} {len(SENT)}")
        code, b = await _n({"key": "sold_fail:t1", "text": "x"})
        ok("N-9b 상한 중에도 이미 보낸 key 는 200 dup(상한을 안 먹는다)", code == 200 and b.get("dup") is True, f"{code} {b}")
        rows = {r["key"]: r for r in await db.fv_notify_recent(50)}
        ok("N-9c 장부 — 상한에 걸린 key 는 limited 로 남는다(감사)", (rows.get("sold_fail:t3") or {}).get("status") == "limited",
           str(rows.get("sold_fail:t3")))
        # 시간 상한 — 10분 전 시도 20개
        _reset()
        main._FV_NOTIFY_SENT_TS.extend([main.time.time() - 600] * main.FV_NOTIFY_PER_HOUR)
        code, b = await _n({"key": "sold_fail:t3", "text": "z"})
        ok("N-10 ★1시간 상한(보냄 20) 넘으면 429 — 가장 옛 보냄이 빠질 때까지(≈3000초)★",
           code == 429 and 2900 <= float(b.get("retry_after_s") or 0) <= 3000 and not SENT, f"{code} {b}")
        _reset()
        main._FV_NOTIFY_TRIES.extend([main.time.time() - 3700] * 50)     # 한 시간 넘은 시도·보냄은 안 센다
        main._FV_NOTIFY_SENT_TS.extend([main.time.time() - 3700] * 50)
        code, b = await _n({"key": "sold_fail:t3", "text": "z"})
        ok("N-10b 한 시간 넘은 시도는 안 센다 — 상한 뒤 같은 key 가 나간다", code == 200 and b.get("sent") is True, f"{code} {b}")
        ok("N-10c 지난 시도·보냄은 기억에서 버린다(목록이 끝없이 안 자란다)",
           len(main._FV_NOTIFY_TRIES) == 1 and len(main._FV_NOTIFY_SENT_TS) == 1,
           f"{len(main._FV_NOTIFY_TRIES)} {len(main._FV_NOTIFY_SENT_TS)}")
        # ★sold_fail 몫 (아이온2 반증 b)★ — 다른 알림이 15통 보냈으면 다른 key 는 429, sold_fail 은 나간다
        _reset()
        main._FV_NOTIFY_SENT_TS.extend([main.time.time() - 600] * (main.FV_NOTIFY_PER_HOUR - main.FV_NOTIFY_RESERVED))
        code, b = await _n({"key": "other:x1", "text": "o"})
        code2, b2 = await _n({"key": "sold_fail:r1", "text": "s"})
        ok("N-10d ★sold_fail 몫 5칸 — 다른 알림 15통 뒤 다른 key 는 429, sold_fail 은 나간다★",
           code == 429 and 2900 <= float(b.get("retry_after_s") or 0) <= 3000 and code2 == 200 and b2.get("sent") is True,
           f"{code} {b.get('retry_after_s')} / {code2} {b2}")
        # ★실패는 시간 상한에 안 센다 (반증 b)★ — 19통 보낸 뒤 실패 3번(분 상한만 먹는다) → 1분 뒤 sold_fail 이 나간다
        _reset()
        main._FV_NOTIFY_SENT_TS.extend([main.time.time() - 600] * (main.FV_NOTIFY_PER_HOUR - 1))
        main.tg_send_text = _tg_fail
        fails = [(await _n({"key": f"sold_fail:f{i}", "text": "f"}))[0] for i in range(3)]
        main.tg_send_text = _tg_ok
        main._FV_NOTIFY_TRIES[:] = [t - 61 for t in main._FV_NOTIFY_TRIES]      # 1분 지남
        code, b = await _n({"key": "sold_fail:f0", "text": "f"})
        ok("N-10e ★텔레그램 실패는 시간 상한(보냄)에 안 센다 — 실패 3번 뒤에도 20번째 sold_fail 이 나간다★",
           fails == [502, 502, 502] and code == 200 and b.get("sent") is True and len(main._FV_NOTIFY_SENT_TS) == main.FV_NOTIFY_PER_HOUR,
           f"{fails} {code} {b} {len(main._FV_NOTIFY_SENT_TS)}")
        code, b = await _n({"key": "sold_fail:f1", "text": "f"})
        ok("N-10f 대조 — 보냄 20 이면 sold_fail 도 429", code == 429, f"{code} {b}")

        # 텔레그램 미설정 → 503 retry:false
        _reset()
        main.tg_enabled = lambda: False
        code, b = await _n({"key": "sold_fail:t4", "text": "q"})
        main.tg_enabled = lambda: True
        ok("N-11 텔레그램 미설정이면 503 reason:disabled·retry:false, 안 보낸다", code == 503 and b.get("retry") is False
           and b.get("reason") == "disabled" and not SENT, f"{code} {b}")

        # 같은 key 동시 두 요청 → 한 번만
        _reset()

        async def _slow(chat, text):
            await asyncio.sleep(0.2)
            return await _tg_ok(chat, text)
        main.tg_send_text = _slow
        (c1, b1), (c2, b2) = await asyncio.gather(_n({"key": "sold_fail:t5", "text": "w"}), _n({"key": "sold_fail:t5", "text": "w"}))
        main.tg_send_text = _tg_ok
        ok("N-12 ★같은 key 동시 두 요청 → 한 번만 보낸다(다른 쪽 409 busy·retry)★",
           len(SENT) == 1 and sorted([c1, c2]) == [200, 409] and (b1.get("busy") or b2.get("busy")), f"{c1} {c2} {SENT}")
        code, b = await _n({"key": "sold_fail:t5", "text": "w"})
        ok("N-12b busy 뒤 재전송은 dup", code == 200 and b.get("dup") is True and len(SENT) == 1, f"{code} {b}")

        # ★text 1000자에서 자른다 (아이온2 M7)★
        _reset()
        code, b = await _n({"key": "sold_fail:long", "text": "가" * 1500})
        ok("N-20 ★text 는 1000자에서 자른다(텔레그램 글 = «[팜뷰] » + 1000자)★",
           code == 200 and SENT == ["[팜뷰] " + "가" * 1000] and (await db.fv_notify_get("sold_fail:long") or {}).get("text") == "가" * 1000,
           f"{code} {[len(x) for x in SENT]}")
        # 짝 없는 대리 문자 → 500 이 아니라 보낸다
        _reset()
        code, b = await _n({"key": "sold_fail:\ud800", "text": "a\udfffb"})
        ok("N-13 짝 없는 대리 문자도 500 이 아니다(? 로 바꿔 보냄)", code == 200 and b.get("sent") is True and "\udfff" not in SENT[0],
           f"{code} {b} {SENT}")

        # DB 가 잠기면 503 retry — 보내지 않는다
        _reset()

        async def _locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")
        main.fv_notify_get = _locked
        code, b = await _n({"key": "sold_fail:t6", "text": "e"})
        main.fv_notify_get = real[3]
        ok("N-14 ★장부 읽기가 DB 오류면 503 retry:true, 안 보낸다(중복 여부를 모르므로)★", code == 503 and b.get("retry") is True and not SENT,
           f"{code} {b}")
        # 보낸 뒤 장부 쓰기가 죽어도 200 sent, 같은 프로세스 재전송은 dup
        main.fv_notify_record = _locked
        code, b = await _n({"key": "sold_fail:t7", "text": "r"})
        code2, b2 = await _n({"key": "sold_fail:t7", "text": "r"})
        main.fv_notify_record = real[4]
        ok("N-15 ★보낸 뒤 장부 쓰기가 죽어도 200 sent · 재전송은 dup(두 번 안 간다)★",
           code == 200 and b.get("sent") is True and code2 == 200 and b2.get("dup") is True and len(SENT) == 1, f"{code} {b} / {code2} {b2}")
        # 반증 #3 — 장부가 살아난 뒤 dup 요청은 그 key 를 sent 로 고쳐 적는다 → 재시작(기억 비움) 뒤에도 dup
        code3, b3 = await _n({"key": "sold_fail:t7", "text": "r"})
        main._FV_NOTIFY_SENT.clear()
        code4, b4 = await _n({"key": "sold_fail:t7", "text": "r"})
        ok("N-15b ★장부 쓰기 실패 뒤 dup 요청이 장부를 sent 로 고친다 — 재시작 뒤에도 안 보낸다★",
           b3.get("dup") is True and b4.get("dup") is True and len(SENT) == 1
           and (await db.fv_notify_get("sold_fail:t7") or {}).get("status") == "sent", f"{b3} {b4} {len(SENT)}")

        # sent 는 끈적하다 — 뒤에 적히는 결과가 덮지 않는다
        await db.fv_notify_record("sold_fail:t8", None, "원래 글", "sent", 77)
        await db.fv_notify_record("sold_fail:t8", "PC-X", "다른 글", "limited", None)
        r8 = await db.fv_notify_get("sold_fail:t8")
        ok("N-16 장부 — sent 뒤 limited 가 status·text·message_id 를 안 덮는다(n 만 오른다)",
           r8["status"] == "sent" and r8["text"] == "원래 글" and r8["message_id"] == 77 and r8["n"] == 2, str(r8))
        # 보관 30일 — 오래된 key 는 지워진다(같은 key 가 다시 오면 새 알림)
        async with __import__("aiosqlite").connect(db.DB_PATH) as c:
            await c.execute("UPDATE fv_notify SET last_at=? WHERE key=?", (U(-31 * 86400), "sold_fail:t8"))
            await c.commit()
        await db.fv_notify_record("sold_fail:t9", None, "z", "sent", 1)
        ok("N-17 30일 넘은 장부 행은 지운다", await db.fv_notify_get("sold_fail:t8") is None, "")
        r = await main.fv_notify_list(Req({}, api_key=None, headers={}))
        ok("N-18 감사 목록(GET)도 토큰 없으면 401", r.status_code == 401, str(r.status_code))
        rq = Req({}, api_key=None, headers=H)
        rq.query_params = {"limit": "x"}
        r = await main.fv_notify_list(rq)
        ok("N-18b 감사 목록 limit 가 숫자가 아니면 400", r.status_code == 400, str(r.status_code))
    finally:
        main.tg_send_text, main.tg_enabled, main.tenant_chat_id, main.fv_notify_get, main.fv_notify_record = real
        _reset()


def t_routes():
    have = {(m, r.path) for r in main.app.routes for m in (getattr(r, "methods", None) or ())}
    ok("N-19 라우트 — POST·GET /api/fv/notify 가 있다", {("POST", "/api/fv/notify"), ("GET", "/api/fv/notify")} <= have, "")


def test_all():
    run_all([t_kina_read_age, t_p6, t_limit_math, t_notify, t_routes])


if __name__ == "__main__":
    test_all()
    finish("test_fv_r3d", MIN_CHECKS)
