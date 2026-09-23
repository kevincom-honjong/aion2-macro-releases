# -*- coding: utf-8 -*-
"""배포 반증 v2 (Part D + H 수정 뒤 반증 에이전트, 2026-09-23 밤) — 확인된 5건 + 그럴듯한 1건.

  S  창고키나 PC 시계 어긋남 — ★진짜 매크로 본문 모양★(sent_at 없음, collected_at = 보내기 직전 PC _now())
     1시간 느린 PC 는 판매를 두 번 뺐고(700→400), 10분 빠른 PC 는 새 판독이 전부 버려져 얼었고, 시계가 6시간 뒤로
     뛰면 6시간 얼었다. → collected_at 을 보낸 시각으로 쓰고(merge·재전송 제외), 카드별 어긋남을 배워 재전송에도 쓴다.
  F  팜뷰 명령 — ①fv_unknown 이 된 명령이 목록에 남아 재시작한 팜뷰가 옛 X 와 다시 누른 Y 를 둘 다 실행 ②집는 중에
     눌린 새 명령이 집힌 항목을 칸 밖으로 밀어내 응답 하나가 사라지면 영영 안 돎 ③다시 주는 항목에 redeliver 표식
  U  짝 없는 대리 문자(\\ud800) → 500 이었다 → 400
  G  빈 X-FV-Token 30번이 같은 IP 의 올바른 토큰을 5분 잠갔다
  Y  1000 년 미만 시각 — 리눅스 strftime 이 %Y 를 안 채워 strptime 500 (그럴듯함 → 막음)

    cd updater/server && python -X utf8 tests/test_breaker_v2.py
"""
import json
import time
from datetime import datetime, timedelta, timezone

import aiosqlite
from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 26
TOK = "fvsecret-brk2"
H = {"X-FV-Token": TOK}
C1 = [{"slot": 1, "name": "러닝"}]


def U(ds):
    return (datetime.now(timezone.utc) + timedelta(seconds=ds)).strftime("%Y-%m-%dT%H:%M:%S")


def _body(r):
    return json.loads(bytes(r.body))


async def _post(pc, body):
    """→ (상태코드, 저장된 total_kina)."""
    try:
        r = await main.receive_char_info(pc, Req(dict({"characters": C1}, **body)))
        code = r.status_code
    except main.HTTPException as e:
        code = e.status_code
    info = await db.get_char_info(main.ns("main", pc)) or {}
    return code, info.get("total_kina")


def _mac(total, read_ds, seq, skew_s, **kw):
    """진짜 매크로 새 판독 본문: PC 시계 = 서버 + skew_s. kina_read_at·collected_at 이 같은 PC 시계, sent_at 없음."""
    return dict({"total_kina": total, "kina_read_at": U(read_ds + skew_s), "kina_seq": seq,
                 "collected_at": U(skew_s)}, **kw)


async def _sell(pc, delta, tid, why=None):
    main.FV_TOKEN = TOK
    try:
        r = await main.fv_kina_adjust(Req({"pc_id": pc, "delta_kina": delta, "why": dict({"tid": tid}, **(why or {}))},
                                          api_key=None, headers=H))
        return r.status_code
    except main.HTTPException as e:
        return e.status_code


async def _age_sale(tid, sec):
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE kina_adjust SET at=? WHERE tid=?", (U(-sec), tid))
        await c.commit()


async def t_skew_macro_shape():
    # S-1 PC 시계 1시간 느림 — 판매 뒤 새 판독(700) → 700 (예전 400: 판매를 두 번 뺐다)
    pc = "PC-S1"
    await _post(pc, _mac(1000, -3600, 1, -3600))
    await _sell(pc, -300, "tid-S1")
    _, v = await _post(pc, _mac(700, 0, 2, -3600))
    ok("S-1 ★1시간 느린 PC 의 판매 뒤 새 판독 → 700(두 번 안 뺀다)★", v == 700, str(v))
    # S-2 PC 시계 10분 빠름 — 새 판독 셋이 다 들어간다(예전: 전부 버려져 1000 에 얼었다)
    pc = "PC-S2"
    await _post(pc, _mac(1000, -3600, 1, 600))
    outs = [await _post(pc, _mac(5000 + i * 1000, 0, 2 + i, 600)) for i in range(3)]
    ok("S-2 ★10분 빠른 PC 의 새 판독이 얼지 않는다(5000·6000·7000)★", [x[1] for x in outs] == [5000, 6000, 7000], str(outs))
    # S-3 시계가 6시간 뒤로 뜀 — 새 판독(순번 증가)이 들어간다
    pc = "PC-S3"
    await _post(pc, _mac(1000, -60, 10, 0))
    outs = [await _post(pc, _mac(900 - i, 0, 11 + i, -6 * 3600)) for i in range(3)]
    ok("S-3 ★시계가 6시간 뒤로 뛰어도 새 판독이 들어간다★", [x[1] for x in outs] == [900, 899, 898], str(outs))
    # S-4 재전송(resend:true)의 옛 collected_at 은 «보낸 시각» 으로 안 쓴다 — 판매 전 판독의 늦은 재전송 → 700
    pc = "PC-S4"
    await _post(pc, {"total_kina": 1000})                       # 표식 없는 옛 카드
    await _sell(pc, -300, "tid-S4")
    await _age_sale("tid-S4", 1800)                             # 판매 30분 전
    _, v = await _post(pc, {"total_kina": 1000, "kina_read_at": U(-3600), "kina_seq": 1,
                            "collected_at": U(-3600), "resend": True})
    ok("S-4 ★재전송의 옛 collected_at 을 보낸 시각으로 안 쓴다 — 판매 전 판독 → 700★", v == 700, str(v))
    # S-5 PC 시계 4분 빠름 + 판매 전 판독이 재전송으로 늦게 도착 → 배운 어긋남으로 옮겨 700 (예전 1000: 차감 되살아남)
    pc = "PC-S5"
    # 새 전송 셋 — 어긋남 +240 을 배운다(v2 반증 1부: 배운 값은 최근 3개가 서로 맞을 때만 쓴다)
    for i in range(3):
        await _post(pc, _mac(1000, -7200, 1, 240))
    await _sell(pc, -300, "tid-S5")
    await _age_sale("tid-S5", 180)                              # 판매 3분 전
    _, v = await _post(pc, {"total_kina": 1000, "kina_read_at": U(-300 + 240), "kina_seq": 2,
                            "collected_at": U(-300 + 240), "resend": True})
    ok("S-5 ★빠른 PC 의 판매 전 판독이 재전송으로 늦게 와도 차감이 안 되살아난다(700)★", v == 700, str(v))
    # S-6 merge 의 collected_at(옛 전체수집 시각)도 보낸 시각으로 안 쓴다 — merge 는 창고키나를 안 실으니 값 그대로
    pc = "PC-S6"
    await _post(pc, _mac(800, -10, 1, 0))
    _, v = await _post(pc, {"merge": True, "collected_at": U(-7200), "characters": C1})
    ok("S-6 merge 는 창고키나를 안 건드린다(800)", v == 800, str(v))
    _smp = main._PC_CLOCK.get(main.ns("main", pc)) or []
    ok("S-6b merge 의 옛 collected_at 으로 어긋남을 배우지 않는다", len(_smp) == 1 and abs(_smp[0][1]) < 60, str(_smp))
    # S-7 sent_at 이 오면 그것이 이긴다(collected_at 보다)
    pc = "PC-S7"
    await _post(pc, {"total_kina": 1000, "kina_read_at": U(-7200), "kina_seq": 1, "sent_at": U(-7200)})
    await _sell(pc, -300, "tid-S7")
    _, v = await _post(pc, {"total_kina": 700, "kina_read_at": U(-3600), "kina_seq": 2,
                            "collected_at": "2026-01-01T00:00:00", "sent_at": U(-3600)})
    ok("S-7 sent_at 이 있으면 그걸 쓴다(엉터리 collected_at 무시) → 700", v == 700, str(v))


def _reset():
    main.FV_TOKEN = TOK
    main.FV_TENANT = "main"
    main.FV_UPDCMD_QUEUE.clear(); main._UPDCMD_OWNER.clear(); main._UPDCMD_FV_PC.clear()
    main._UPDCMD_UP_PC.clear(); main._FV_ACKED_RECENT.clear(); main._FV_REBUILT[0] = True
    main._FV_LAST_SEEN[0] = time.monotonic()


async def _send(pc, cmd):
    return _body(await main.dashboard_send_updater_command(pc, Req({"command": cmd}, api_key=None,
                                                                     session=main.new_session("main"))))["id"]


async def _fvl(since=0):
    return _body(await main.fv_updcmd_list(Req(api_key=None, headers=H), since=str(since)))["cmds"]


async def _ack(pc, fid, ok_, **kw):
    return _body(await main.fv_updcmd_ack(Req({"pc": pc, "id": fid, "ok": ok_, **kw}, api_key=None, headers=H)))


async def _st(cid):
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT status FROM updater_commands WHERE id=?", (cid,)) as cur:
            r = await cur.fetchone()
    return r[0] if r else None


async def _age(cid, sec, col="updated_at"):
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute(f"UPDATE updater_commands SET {col}=? WHERE id=?", (U(-sec), cid))
        await c.commit()


async def t_fv_unknown_not_relisted():
    _reset(); pc = "PC-F2"
    x = await _send(pc, "restart")
    gx = [e for e in await _fvl() if e["pc"] == pc]
    main._FV_LAST_SEEN[0] = time.monotonic() - 999            # 팜뷰 조용
    await _age(x, 200)
    await main._fv_claim_sweep_once()
    y = await _send(pc, "restart")                            # 로그대로 사람이 다시 누름
    main._FV_LAST_SEEN[0] = time.monotonic()
    back = [e for e in await _fvl(0) if e["pc"] == pc]        # 재시작한 팜뷰(since=0, UPD_DONE 비었음)
    ok("F-2 ★fv_unknown 이 된 X 는 다시 안 나오고 새 Y 만 나온다(재시작 두 번 없음)★",
       await _st(x) == "fv_unknown" and [e["id"] for e in back] != [] and gx[0]["id"] not in [e["id"] for e in back]
       and len(back) == 1, "X=%s back=%s Y=%s" % (await _st(x), back, await _st(y)))
    r = await _ack(pc, gx[0]["id"], True)
    ok("F-2b 목록에 없어도 늦은 ack 는 id 로 짝 행을 닫는다(fv_unknown → fv_done)", await _st(x) == "fv_done",
       "ack=%s X=%s" % (r, await _st(x)))
    # 재배포 뒤에도 fv_unknown 은 다시 세우지 않는다
    _reset(); pc = "PC-F4"
    x = await _send(pc, "update")
    await _fvl()
    main._FV_LAST_SEEN[0] = time.monotonic() - 999
    await _age(x, 200)
    await main._fv_claim_sweep_once()
    main.FV_UPDCMD_QUEUE.clear(); main._UPDCMD_OWNER.clear(); main._UPDCMD_FV_PC.clear(); main._FV_REBUILT[0] = False
    main._FV_LAST_SEEN[0] = time.monotonic()
    got = [e for e in await _fvl(0) if e["pc"] == pc]
    ok("F-4 재배포 뒤 fv_unknown 은 목록에 다시 안 선다(«다시 보내지 않았습니다» 그대로)", got == [] and await _st(x) == "fv_unknown",
       "%s %s" % (got, await _st(x)))
    # 업데이터 폴링 쪽 fv_unknown 도 목록에서 뺀다
    _reset(); pc = "PC-F2u"
    x = await _send(pc, "update")
    await _fvl()
    main._FV_LAST_SEEN[0] = time.monotonic() - 999
    await _age(x, 200)
    await _body_poll(pc)
    main._FV_LAST_SEEN[0] = time.monotonic()
    ok("F-2c 업데이터 폴링이 fv_unknown 으로 바꾼 명령도 목록에서 빠진다",
       await _st(x) == "fv_unknown" and not [e for e in await _fvl(0) if e["pc"] == pc], str(await _st(x)))


async def _body_poll(pc):
    return _body(await main.updater_poll_command(pc, Req()))


async def t_fv_stale_claim_net():
    _reset(); pc = "PC-F2s"
    await _send(pc, "restart")
    g = [e for e in await _fvl() if e["pc"] == pc]
    for v in main.FV_UPDCMD_QUEUE.values():                  # 청소가 못 돌았다고 치고 집은 지 12분
        if v["id"] == g[0]["id"]:
            v["at"] = U(-(db.UPDATER_COMMAND_MAX_AGE_SEC + 120))
    ok("F-2d 안전망 — 11분 넘은 집힘은 청소 없이도 다시 안 준다", not [e for e in await _fvl(0) if e["pc"] == pc])


async def t_fv_redeliver_flag():
    _reset(); pc = "PC-F9"
    await _send(pc, "restart")
    a = [e for e in await _fvl() if e["pc"] == pc]
    b = [e for e in await _fvl(0) if e["pc"] == pc]
    ok("F-9 처음 주는 항목엔 redeliver 가 없다(옛 모양 {id,pc,act,at} 그대로)",
       a and set(a[0]) == {"id", "pc", "act", "at"}, str(a))
    ok("F-9b 다시 주는(집힌) 항목엔 redeliver:true", b and b[0].get("redeliver") is True and b[0]["id"] == a[0]["id"], str(b))


async def t_fv_push_during_claim():
    _reset(); pc = "PC-F1"
    u1 = await _send(pc, "update")
    real = main.claim_updater_command_for_fv
    state = {}

    async def claim_with_press(*a, **k):
        state["u2"] = await _send(pc, "restart")             # 집는 사이 사람이 restart 를 누름
        return await real(*a, **k)
    main.claim_updater_command_for_fv = claim_with_press
    try:
        first = [e for e in await _fvl() if e["pc"] == pc]
    finally:
        main.claim_updater_command_for_fv = real
    second = [e for e in await _fvl(0) if e["pc"] == pc]       # 첫 응답이 전선에서 사라짐
    ok("F-1 ★집는 중에 눌린 새 명령이 집힌 update 를 밀어내지 않는다 — 두 번째 목록에도 나온다★",
       first and first[0]["id"] in [e["id"] for e in second] and await _st(u1) == "fv_claimed",
       "first=%s second=%s u1=%s" % (first, second, await _st(u1)))
    ok("F-1b 새 restart 도 나온다(둘 다 실행)", sorted(e["act"] for e in second) == ["restart", "updater"], str(second))
    r = await _ack(pc, first[0]["id"], True)
    ok("F-1c 옆 칸으로 옮긴 같은 객체가 ack 로 닫힌다", await _st(u1) == "fv_done" and r.get("removed") is True, str(r))


async def t_surrogate_400():
    from fastapi.testclient import TestClient
    main.FV_TOKEN = TOK
    C = TestClient(main.app, raise_server_exceptions=False)
    await _post("PC-U1", {"total_kina": 1000})
    raw = '{"pc_id":"PC-U1","delta_kina":-1,"why":{"tid":"\\ud800"}}'
    r = C.post("/api/fv/kina_adjust", content=raw.encode(), headers=dict(H, **{"Content-Type": "application/json"}))
    ok("U-1 ★kina_adjust tid 에 짝 없는 대리 문자 → 400(500 아님)★", r.status_code == 400 and r.json().get("code") == 400,
       "%s %s" % (r.status_code, r.text[:80]))
    info = await db.get_char_info(main.ns("main", "PC-U1"))
    ok("U-1b 값은 그대로(1000)", info and info["total_kina"] == 1000, str(info and info["total_kina"]))
    raw = '{"total_kina":5,"characters":[{"slot":1,"name":"\\ud800"}]}'
    r = C.post("/char_info/PC-U1", content=raw.encode(), headers={"X-API-Key": "testkey", "Content-Type": "application/json"})
    ok("U-2 char_info 캐릭 이름에 대리 문자 → 400", r.status_code == 400, "%s %s" % (r.status_code, r.text[:80]))


async def t_empty_token():
    main.FV_TOKEN = TOK; main._KEY_FAILS.clear()
    host = "10.7.7.9"
    for _ in range(main.KEY_MAX_FAILS + 3):
        await main.fv_updcmd_list(Req(api_key=None, headers={}, host=host), since="0")
    r = await main.fv_updcmd_list(Req(api_key=None, headers=H, host=host), since="0")
    ok("G-1 ★빈 토큰 여러 번은 올바른 토큰을 잠그지 않는다(200)★", r.status_code == 200, str(r.status_code))
    for _ in range(main.KEY_MAX_FAILS + 1):
        await main.fv_updcmd_list(Req(api_key=None, headers={"X-FV-Token": "bad"}, host=host), since="0")
    r = await main.fv_updcmd_list(Req(api_key=None, headers={"X-FV-Token": "bad"}, host=host), since="0")
    ok("G-1b 틀린 토큰 30번은 여전히 잠근다(429 — 그 토큰의 칸, v2 반증 1부)", r.status_code == 429, str(r.status_code))
    main._KEY_FAILS.clear()


async def t_year_floor():
    ok("Y-1 1000 년 미만·2000 년 미만 시각은 못 읽은 것(None)",
       db._ca_utc("0999-01-01T00:00:00") is None and db._ca_utc("1999-12-31T23:59:59") is None
       and db._ca_utc("2026-09-23T00:00:00") == "2026-09-23T00:00:00")
    code, _ = await _post("PC-Y1", {"total_kina": 10, "kina_read_at": "0999-01-01T00:00:00", "kina_seq": 1,
                                    "sent_at": "0001-01-01T00:00:00+09:00"})
    ok("Y-1b 엉터리 옛 시각 판독은 200(500 아님)", code == 200, str(code))


async def t_timeout_stops_after_first():
    """시간 상한에 한 PC 가 걸리면 뒤 PC 는 시작하지 않는다 — 타이머가 일찍 깨 left 가 남아도(T-4 흔들림, 윈도 15ms)."""
    import asyncio
    main.FV_TOKEN = TOK
    pcs = ["PC-TT1", "PC-TT2", "PC-TT3"]
    for p in pcs:
        await db.upsert_status(p, {"pc_id": p, "status": "idle"})
    od, odl, real = main._dispatch_macro_command, main.FV_CMD_DEADLINE_S, main.asyncio.wait_for
    cur = [None]

    async def disp(tenant, pc_id, command, args):
        cur[0] = pc_id
        await asyncio.sleep(0.05)
        return {"ok": True, "id": 1}

    async def early(aw, timeout):
        await asyncio.sleep(0.01)          # 넣기 작업이 시작하게(cur 가 찍히게)
        if cur[0] == "PC-TT2":             # 둘째 PC 에서 타이머가 «일찍» 깼다 — 상한은 아직 한참 남음
            cur[0] = None
            raise asyncio.TimeoutError
        return await real(aw, timeout)
    main._dispatch_macro_command, main.FV_CMD_DEADLINE_S, main.asyncio.wait_for = disp, 5.0, early
    try:
        b = _body(await main.fv_command_send(Req({"cmd": "start", "pc": pcs}, api_key=None, headers=H)))
    finally:
        main._dispatch_macro_command, main.FV_CMD_DEADLINE_S, main.asyncio.wait_for = od, odl, real
    r = {x["pc"]: x for x in b.get("results", [])}
    ok("T-9 ★앞 PC 가 상한에 걸리면 뒤 PC 는 sent:false(시작 안 함)★",
       r.get("PC-TT2", {}).get("unknown") is True and r.get("PC-TT3", {}).get("sent") is False, str(b.get("results")))


def test_all():
    run_all([t_skew_macro_shape, t_fv_unknown_not_relisted, t_fv_stale_claim_net, t_fv_redeliver_flag,
             t_fv_push_during_claim, t_surrogate_400, t_empty_token, t_year_floor, t_timeout_stops_after_first])
    finish("test_breaker_v2", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
