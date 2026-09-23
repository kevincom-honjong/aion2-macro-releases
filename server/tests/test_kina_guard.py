# -*- coding: utf-8 -*-
"""배포 반증 D (2026-09-23 밤, 아이온2 d_agent fuzz.py·skew.py·mutrun.py) — 창고키나 입력 방어 · PC 시계 · 보안 몇 가지.

  K-1~3  /char_info 범위·형 — 2^63·1e30·1e300·40자리·bool·음수·목록 시각이 500 이었다 → 400
  K-4    미래 판독 시각(2099) 하나가 카드를 영영 얼렸다 → 버리고 로그, 다음 진짜 판독은 들어간다
  K-5    순번 독(한 번에 크게 뛴 순번) · 순번이 되돌아간 카드(로컬 파일 잃음)도 얼지 않는다
  K-6    PC 시계 10분 빠름 → 판매 전 판독이 판매 뒤로 보여 차감이 되살아났다(1000, 맞는 값 700) → sent_at 으로 옮긴다
  K-7    같은 판독 시각(같은 초) — 순번이 커졌을 때만 새것 (뮤턴트 `<=`→`<`)
  K-8    시계가 뒤로 뛴 뒤 옛 판독 재전송도 옛것으로(받은 판독 시각 목록)
  K-9~10 kina_adjust 2^63 근처 500 → 400 · 같은 tid 를 다른 카드에 → 409(예전엔 dup:true 로 «뺐다»)
  K-11   팜뷰 명령 PC 목록 중복은 한 번
  K-12   API 키 추측과 팜뷰 토큰 추측은 락아웃 칸이 따로
  K-13   FV_CLAIM_ACK_SEC 하한 — 45초 조용함으로는 «모름» 이 안 된다 (뮤턴트 90→30)

    cd updater/server && python -X utf8 tests/test_kina_guard.py
"""
import json
from datetime import datetime, timedelta, timezone

import aiosqlite
from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 44
TOK = "fvsecret-kguard"
H = {"X-FV-Token": TOK}
C1 = [{"slot": 1, "name": "러닝"}]


def _utc(delta_s):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%S")


async def _raw_post(pc, body, key="testkey", host="127.0.0.1"):
    """→ (상태코드, 저장된 total_kina)."""
    try:
        r = await main.receive_char_info(pc, Req(dict({"characters": C1}, **body), api_key=key, host=host))
        code = r.status_code
    except main.HTTPException as e:
        code = e.status_code
    info = await db.get_char_info(main.ns("main", pc)) or {}
    return code, info.get("total_kina")


async def _post(pc, body):
    code, v = await _raw_post(pc, body)
    assert code == 200, (code, body)
    return v


async def _sell(pc, delta, tid):
    main.FV_TOKEN = TOK
    try:
        r = await main.fv_kina_adjust(Req({"pc_id": pc, "delta_kina": delta, "why": {"tid": tid}}, api_key=None, headers=H))
    except main.HTTPException as e:
        return e.status_code, {}
    return r.status_code, json.loads(bytes(r.body))


async def _logs(pc):
    return [x["message"] for x in await db.get_logs(main.ns("main", pc), 50)]


async def t_ranges():
    main._KEY_FAILS.clear()
    pc = "PC-KG1"
    await _post(pc, {"total_kina": 1000})
    bad = {"2^63": 2 ** 63, "1e30 int": 10 ** 30, "1e300 float": 1e300, "40자리 문자열": "9" * 40,
           "bool": True, "음수": -5, "음수 문자열": "-1,000", "inf 문자열": "inf"}
    got = {k: await _raw_post(pc, {"total_kina": v}) for k, v in bad.items()}
    ok("K-1 ★total_kina 범위·형 — 2^63·1e30·1e300·40자리·bool·음수는 400(500 이 아니다), 저장값 그대로★",
       all(c == 400 and v == 1000 for c, v in got.values()), str(got))
    ok("K-1b 상한 딱 그 값은 받는다", (await _raw_post("PC-KG1b", {"total_kina": db.KINA_MAX}))[0] == 200)
    sbad = {"2^63": 2 ** 63, "1e308": 1e308, "1e18 float": 1e18, "bool": True, "문자열": "5", "음수": -1, "2^53": 2 ** 53}
    got = {k: (await _raw_post(pc, {"total_kina": 900, "kina_seq": v, "kina_read_at": _utc(-5)}))[0] for k, v in sbad.items()}
    ok("K-2 kina_seq 2^63·1e308·float·bool·문자열·음수·2^53 → 400", all(c == 400 for c in got.values()), str(got))
    got = [(await _raw_post(pc, {"total_kina": 900, "collected_at": v}))[0] for v in (["2026-09-23T00:00:00"], {"a": 1}, 5)]
    ok("K-3 collected_at 목록·객체·숫자 → 400(500 이었다)", got == [400, 400, 400], str(got))
    c, v = await _raw_post(pc, {"total_kina": 900, "kina_read_at": ["x"], "sent_at": {"a": 1}})
    ok("K-3b kina_read_at·sent_at 이 엉터리 형이면 표식만 무시하고 저장(L-33 과 같은 규칙)", c == 200 and v == 900, "%s %s" % (c, v))
    ok("K-3c 판정 함수: 9999-12-31 끝값·오프셋 끝값도 예외 없이 «버림»",
       db.kina_read_judge("9999-12-31T23:59:59", "0001-01-01T00:00:00")[1] is None
       and db.kina_read_judge("9999-12-31T23:59:59", None)[1] is None
       and db.kina_read_judge("0001-01-01T00:00:00", "9999-12-31T23:59:59")[1] is None)


async def t_future_and_seq_poison():
    pc = "PC-KG2"
    await _post(pc, {"total_kina": 500, "kina_read_at": _utc(-60), "kina_seq": 1})
    v = await _post(pc, {"total_kina": 999, "kina_read_at": "2099-01-01T00:00:00", "kina_seq": 2})
    ok("K-4 ★2099 년 판독은 창고키나를 안 덮는다★", v == 500, str(v))
    ok("K-4b 그 PC 로그에 «무시 … PC 시계» 한 줄(A2)", any("PC 시계" in m and "무시" in m for m in await _logs(pc)))
    v = await _post(pc, {"total_kina": 777, "kina_read_at": _utc(-5), "kina_seq": 2})
    ok("K-4c ★그 뒤 진짜 판독은 들어간다 — 카드가 얼지 않는다(반증 D #3)★", v == 777, str(v))
    v = await _post(pc, {"total_kina": 780, "kina_read_at": _utc(+120), "kina_seq": 3})
    ok("K-4d 5분 안쪽으로 빠른 시계(+2분)는 받는다", v == 780, str(v))
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT read_srv FROM kina_read WHERE pc_id=?", (main.ns("main", pc),)) as cur:
            srv = (await cur.fetchone())[0]
    ok("K-4e ★저장되는 서버 판독 시각은 미래가 될 수 없다(받은 시각 이하)★", srv <= _utc(1), "%s vs %s" % (srv, _utc(0)))
    # 순번 독: 한 번에 크게 뛴 순번은 믿지 않는다 → 그 뒤 정상 순번·시각 판독이 막히지 않는다
    pc = "PC-KG3"
    await _post(pc, {"total_kina": 500, "kina_read_at": _utc(-120), "kina_seq": 5})
    v = await _post(pc, {"total_kina": 600, "kina_read_at": _utc(-60), "kina_seq": 5 + db.KINA_SEQ_JUMP_MAX + 1})
    ok("K-5 크게 뛴 순번의 판독도 시각이 새것이면 값은 받는다", v == 600, str(v))
    t6 = _utc(-30)
    v = await _post(pc, {"total_kina": 650, "kina_read_at": t6, "kina_seq": 6})
    ok("K-5b ★뛴 순번은 저장되지 않아 다음 정상 판독(순번 6)이 «옛것» 이 안 된다★", v == 650, str(v))
    # 순번이 되돌아감(로컬 파일 잃음) — 시각이 새것이면 받는다
    v = await _post(pc, {"total_kina": 660, "kina_read_at": _utc(-10), "kina_seq": 1})
    ok("K-5c 순번이 1 로 되돌아간 카드도 새 판독은 받는다(영영 얼지 않는다)", v == 660, str(v))
    v = await _post(pc, {"total_kina": 111, "kina_read_at": t6, "kina_seq": 6})
    ok("K-5d ★그 뒤 옛 판독(순번 6) 재전송은 무시 — 옛 순번이 더 커도★", v == 660, str(v))
    v = await _post(pc, {"total_kina": 222, "kina_read_at": _utc(-200), "kina_seq": 7})
    ok("K-5e 처음 보는 옛 판독(순번 7, 서버 시각 3분 전)도 무시 — 순번만으로 정하지 않는다", v == 660, str(v))
    # 10초(KINA_TIE_S) 안쪽에서는 순번이 가른다 — 뛴 순번이 저장되면 다음 정상 판독이 막힌다 (뮤턴트 «순번 뜀 가드 끔», v2)
    pc = "PC-KG3b"
    await _post(pc, {"total_kina": 500, "kina_read_at": _utc(-40), "kina_seq": 5})
    await _post(pc, {"total_kina": 510, "kina_read_at": _utc(-37), "kina_seq": 5 + db.KINA_SEQ_JUMP_MAX + 1})
    v = await _post(pc, {"total_kina": 520, "kina_read_at": _utc(-34), "kina_seq": 6})
    ok("K-5f ★10초 안쪽 — 뛴 순번 뒤의 정상 판독(순번 6)도 받는다★", v == 520, str(v))


async def t_skew():
    # skew.py — PC 시계가 10분 빠르다. 판매 ★전★ 에 읽은 판독이 판매 ★뒤★ 에 (늦게) 도착
    pc = "PC-KG4"
    await _post(pc, {"total_kina": 1000, "kina_read_at": _utc(-3600), "sent_at": _utc(-3600 + 600)})
    await _sell(pc, -300, "tid-KG4")
    v = await _post(pc, {"total_kina": 1000, "kina_read_at": _utc(-20 + 600), "sent_at": _utc(+600)})
    ok("K-6 ★PC 시계 +10분 · 판매 전 판독(늦게 도착) → 700(sent_at 으로 시계를 옮긴다, 반증 D #4)★", v == 700, str(v))
    v = await _post(pc, {"total_kina": 1000, "kina_read_at": _utc(-20 + 600), "sent_at": _utc(+600)})
    ok("K-6b 같은 판독 재전송 — 700 그대로", v == 700, str(v))
    v = await _post(pc, {"total_kina": 700, "kina_read_at": _utc(+5 + 600), "sent_at": _utc(+600)})
    ok("K-6c 판매 뒤 새 판독은 그대로 700", v == 700, str(v))
    # sent_at 없이 10분 빠른 판독 = 받는 시각보다 5분 넘게 뒤 → 버림(되살리지 않는다)
    pc = "PC-KG5"
    await _post(pc, {"total_kina": 1000, "kina_read_at": _utc(-3600)})
    await _sell(pc, -300, "tid-KG5")
    v = await _post(pc, {"total_kina": 1000, "kina_read_at": _utc(+600)})
    ok("K-6d sent_at 없는 +10분 판독은 버린다 — 차감이 되살아나지 않는다(700)", v == 700, str(v))
    # PC 시계가 느리다(-10분): 판매 뒤에 읽은 판독 → sent_at 으로 옮기면 판매 뒤 = 그대로
    pc = "PC-KG6"
    await _post(pc, {"total_kina": 1000, "kina_read_at": _utc(-3600 - 600), "sent_at": _utc(-3600 - 600)})
    await _sell(pc, -300, "tid-KG6")
    v = await _post(pc, {"total_kina": 700, "kina_read_at": _utc(-600), "sent_at": _utc(-600)})
    ok("K-6e 느린 시계(-10분)의 판매 뒤 판독 → 판매를 두 번 빼지 않는다(700)", v == 700, str(v))


async def t_same_second():
    pc = "PC-KG7"
    t = _utc(-100)
    await _post(pc, {"total_kina": 500, "kina_read_at": t, "kina_seq": 3})
    v = await _post(pc, {"total_kina": 999, "kina_read_at": t, "kina_seq": 3})
    ok("K-7 ★같은 판독 시각·같은 순번(재전송)은 값이 달라도 옛것 (뮤턴트 <= → <)★", v == 500, str(v))
    v = await _post(pc, {"total_kina": 999, "kina_read_at": t})
    ok("K-7b 같은 판독 시각·순번 없음도 옛것", v == 500, str(v))
    v = await _post(pc, {"total_kina": 600, "kina_read_at": t, "kina_seq": 4})
    ok("K-7c 같은 초에 다시 읽음(순번 4) → 새것", v == 600, str(v))
    v = await _post(pc, {"total_kina": 610, "kina_read_at": _utc(-101), "kina_seq": 3})
    ok("K-7d 순번이 둘 다 있으면 순번이 정한다 — 옛 순번은 옛것", v == 600, str(v))
    v = await _post(pc, {"total_kina": 620, "kina_read_at": _utc(-99), "kina_seq": 4})
    ok("K-7e ★10초 안·같은 순번·다른 판독 시각은 옛것 — 순번이 같으면 새로 안 읽은 것 (뮤턴트 <= → <, v2)★", v == 600, str(v))


async def t_clock_jump():
    # 판독 A(10분 전) 받음 → PC 시계가 1시간 뒤로 뜀 → 판독 B → A 재전송(새 sent_at)
    pc = "PC-KG8"
    a = _utc(-600)
    await _post(pc, {"total_kina": 500, "kina_read_at": a, "sent_at": _utc(0)})
    await _post(pc, {"total_kina": 400, "kina_read_at": _utc(-3600 - 60), "sent_at": _utc(-3600)})
    v = (await db.get_char_info(main.ns("main", pc)))["total_kina"]
    ok("K-8 시계가 뒤로 뛴 뒤의 새 판독도 받는다(sent_at 으로 옮기면 1분 전)", v == 400, str(v))
    v = await _post(pc, {"total_kina": 500, "kina_read_at": a, "sent_at": _utc(-3600)})
    ok("K-8b ★옛 판독 A 의 재전송은 옛것 — 받은 판독 시각을 기억한다(옮긴 시각은 «지금» 이 되지만)★", v == 400, str(v))


async def t_adjust():
    pc = "PC-KG9"
    await _post(pc, {"total_kina": db.KINA_MAX - 10})
    code, b = await _sell(pc, 100, "tid-KG9-big")
    ok("K-9 ★kina_adjust 가 상한을 넘기면 400(2^63 근처 500 이었다), 값 그대로★",
       code == 400 and (await db.get_char_info(main.ns("main", pc)))["total_kina"] == db.KINA_MAX - 10, "%s %s" % (code, b))
    await _post("PC-KG10", {"total_kina": 500})
    await _post("PC-KG11", {"total_kina": 500})
    c1, b1 = await _sell("PC-KG10", -100, "tid-KG-X")
    c2, b2 = await _sell("PC-KG11", -100, "tid-KG-X")
    ok("K-10 ★같은 tid 를 다른 카드에 → 409, 그 카드는 안 뺀다(예전엔 dup:true·ok:true)★",
       c1 == 200 and c2 == 409 and (await db.get_char_info(main.ns("main", "PC-KG11")))["total_kina"] == 500,
       "%s %s %s" % (c2, b2, (await db.get_char_info(main.ns("main", "PC-KG11")))["total_kina"]))
    c3, b3 = await _sell("PC-KG10", -100, "tid-KG-X")
    ok("K-10b 같은 카드의 같은 tid 는 예전처럼 dup:true(두 번 안 뺀다)", c3 == 200 and b3.get("dup") is True
       and (await db.get_char_info(main.ns("main", "PC-KG10")))["total_kina"] == 400, str(b3))


async def t_fleet_dedupe():
    main.FV_TOKEN = TOK
    main.FV_TENANT = "main"
    await db.upsert_status("PC-KG12", {"pc_id": "PC-KG12", "status": "idle"})
    sent = []
    od = main._dispatch_macro_command

    async def disp(t, p, c, a):
        sent.append(p)
        return {"ok": True}
    main._dispatch_macro_command = disp
    try:
        r = await main.fv_command_send(Req({"cmd": "start", "pc": ["PC-KG12"] * 7}, api_key=None, headers=H))
        b = json.loads(bytes(r.body))
    finally:
        main._dispatch_macro_command = od
    ok("K-11 ★같은 PC 7번 적은 목록은 한 번만 보낸다(반증 D)★", len(sent) == 1 and b.get("targets") == 1, "%s %s" % (sent, b))


async def t_lockout_buckets():
    main._KEY_FAILS.clear()
    main.FV_TOKEN = TOK
    host = "10.9.9.9"
    for _ in range(main.KEY_MAX_FAILS + 2):
        main.check_api_key(Req(api_key="wrong-key", host=host))
    r = await main.fv_updcmd_list(Req(api_key=None, headers=H, host=host), since="0")
    ok("K-12 ★API 키 30번 틀려도 같은 IP 의 올바른 팜뷰 토큰은 통과(칸이 따로)★", r.status_code == 200, str(r.status_code))
    ok("K-12b API 키 칸은 잠겼다(정답 키도 창이 끝날 때까지 거부 — 원래 규칙)",
       main.check_api_key(Req(api_key="testkey", host=host)) is None)
    main._KEY_FAILS.clear()
    host = "10.9.9.8"
    for _ in range(main.KEY_MAX_FAILS + 2):
        await main.fv_updcmd_list(Req(api_key=None, headers={"X-FV-Token": "bad"}, host=host), since="0")
    ok("K-12c 팜뷰 토큰 30번 틀려도 같은 IP 의 매크로 API 키는 통과", main.check_api_key(Req(api_key="testkey", host=host)) == "main")
    r = await main.fv_updcmd_list(Req(api_key=None, headers={"X-FV-Token": "bad"}, host=host), since="0")
    ok("K-12d 틀린 토큰의 칸은 잠겼다(429)", r.status_code == 429, str(r.status_code))
    r = await main.fv_updcmd_list(Req(api_key=None, headers=H, host=host), since="0")
    ok("K-12e ★같은 IP 에서 틀린 토큰 30번이어도 올바른 팜뷰 토큰은 통과(v2 반증 1부 — 칸 = IP+토큰)★",
       r.status_code == 200, str(r.status_code))
    # 토큰을 바꿔 가며 찍는 추측 — IP 전체 칸이 FV_IP_MAX_FAILS 에서 잠그고, 그땐 올바른 토큰도 막는다(추측을 평가하지 않게)
    main._KEY_FAILS.clear()
    host = "10.9.9.7"
    for i in range(main.FV_IP_MAX_FAILS - 1):
        await main.fv_updcmd_list(Req(api_key=None, headers={"X-FV-Token": "g%d" % i}, host=host), since="0")
    r = await main.fv_updcmd_list(Req(api_key=None, headers=H, host=host), since="0")
    ok("K-12f 서로 다른 틀린 토큰이 문턱 −1 이면 올바른 토큰은 통과", r.status_code == 200, str(r.status_code))
    await main.fv_updcmd_list(Req(api_key=None, headers={"X-FV-Token": "last"}, host=host), since="0")
    r = await main.fv_updcmd_list(Req(api_key=None, headers=H, host=host), since="0")
    ok("K-12g ★서로 다른 틀린 토큰 합이 문턱이면 IP 전체 429 — 올바른 토큰도(추측을 평가하지 않는다)★",
       r.status_code == 429, str(r.status_code))
    main._KEY_FAILS.clear()
    main._KEY_FAILS.clear()


async def t_claim_ack_lower_bound():
    cid = await db.insert_updater_command("PC-KG13", "restart", {})
    ok("K-13-pre 팜뷰가 집는다", await db.claim_updater_command_for_fv(cid, fv_id=987654321))
    t45 = (datetime.now(timezone.utc) - timedelta(seconds=45)).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE updater_commands SET updated_at=? WHERE id=?", (t45, cid)); await c.commit()
    unk: list = []
    await db.get_pending_updater_command("PC-KG13", "all", unknown_out=unk, fv_quiet=True)

    async def st():
        async with aiosqlite.connect(db.DB_PATH) as c:
            async with c.execute("SELECT status FROM updater_commands WHERE id=?", (cid,)) as cur:
                return (await cur.fetchone())[0]
    ok("K-13 ★팜뷰가 조용해도 집은 지 45초면 아직 fv_claimed(«모름» 은 90초부터 — 뮤턴트 90→30)★",
       await st() == "fv_claimed" and unk == [], "%s %s" % (await st(), unk))
    ok("K-13b 상수 하한 — 팜뷰 한 대 20초 × 차례 처리를 덮는다", db.FV_CLAIM_ACK_SEC >= 60, str(db.FV_CLAIM_ACK_SEC))
    t95 = (datetime.now(timezone.utc) - timedelta(seconds=95)).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE updater_commands SET updated_at=? WHERE id=?", (t95, cid)); await c.commit()
    await db.get_pending_updater_command("PC-KG13", "all", unknown_out=unk, fv_quiet=True)
    ok("K-13c 95초면 fv_unknown(업데이터엔 안 준다)", await st() == "fv_unknown" and [u["id"] for u in unk] == [cid],
       "%s %s" % (await st(), unk))


def test_all():
    run_all([t_ranges, t_future_and_seq_poison, t_skew, t_same_second, t_clock_jump, t_adjust, t_fleet_dedupe,
             t_lockout_buckets, t_claim_ack_lower_bound])
    finish("test_kina_guard", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
