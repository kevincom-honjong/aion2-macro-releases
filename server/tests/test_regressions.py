# -*- coding: utf-8 -*-
"""[대시보드] 회귀 가드 — 2026-09-10~11 에 고친 것들이 도로 무너지면 빨간불 (실제 호출).

옮겨온 근거: web/.claude/ops/servfix_test.py 48검사(2026-09-11) — 그 판은 ★고친 것을 12가지로
일부러 되돌려 전부 잡히는 것★ 까지 확인한 것이다. 담당 경로 안에 두어 다음 모델이
`python tests/run_all.py` 한 번으로 돌릴 수 있게 한다.
"""
import asyncio
import json
import time

from _harness import (main, db, aiosqlite, ok, FakeWS, Req, run_all, finish,   # noqa: E402
                      HTTPException, ConnCounter)

MIN_CHECKS = 34     # 실측 35검사 — 하나만 빠져도 잡힌다 (검사를 더하면 같이 올린다)


async def t_db_layer():
    # 인덱스와 ★함수가 실제로 던지는 SQL★ 이 그 인덱스를 타는가
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT name FROM sqlite_master WHERE type='index'") as cur:
            idx = {r[0] for r in await cur.fetchall()}
    ok("① idx_logs_created 인덱스가 생긴다", "idx_logs_created" in idx)
    seen = []
    _real = aiosqlite.connect

    def _spy(*a, **k):
        c = _real(*a, **k)
        ex = c.execute

        def _w(sql, *aa, **kk):
            seen.append(sql)
            return ex(sql, *aa, **kk)
        c.execute = _w
        return c
    aiosqlite.connect = _spy
    db.aiosqlite.connect = _spy
    try:
        await db.get_logs_since("2026-01-01T00:00:00", 200)
    finally:
        aiosqlite.connect = _real
        db.aiosqlite.connect = _real
    sql = next((q for q in seen if "FROM logs" in q), "")
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("EXPLAIN QUERY PLAN " + sql, ("2026-01-01T00:00:00", 200)) as cur:
            plan = " ".join(str(x) for r in await cur.fetchall() for x in r)
    ok("①-b FarmView 이벤트 쿼리가 전량 스캔이 아니다", "idx_logs_created" in plan and "SCAN" not in plan, plan[:90])
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("INSERT INTO logs(pc_id,level,message,created_at) VALUES('PC-ORD','info','id앞시각뒤','2026-09-11T00:00:09')")
        await c.execute("INSERT INTO logs(pc_id,level,message,created_at) VALUES('PC-ORD','info','id뒤시각앞','2026-09-11T00:00:01')")
        await c.commit()
    got = [r["message"] for r in await db.get_logs_since("2026-09-10T00:00:00", 10, "PC-ORD")]
    ok("①-c 결과가 시각순(커서와 같은 기준)", got == ["id뒤시각앞", "id앞시각뒤"], str(got))

    with ConnCounter() as cc:
        await db.insert_log("PC-T1", "info", "한 줄")
    ok("② 로그 한 줄에 연결 1개", cc.n == 1, "연결 %d" % cc.n)
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.executemany("INSERT INTO logs(pc_id, level, message, created_at) VALUES('PC-T3','info',?,?)",
                            [("m%d" % i, "2026-09-11T00:00:00") for i in range(3400)])
        await c.commit()
    db._LOG_SINCE_PRUNE["PC-T3"] = db.LOG_PRUNE_EVERY - 1
    await db.insert_log("PC-T3", "info", "정리 유발")
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT COUNT(*) FROM logs WHERE pc_id='PC-T3'") as cur:
            cnt = (await cur.fetchone())[0]
    ok("②-b 3000줄 상한이 지켜진다", cnt == 3000, "%d줄" % cnt)

    await db.insert_command("t::PC-01", "start", {})
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("PRAGMA data_version") as cur:
            dv0 = (await cur.fetchone())[0]
    rows = await db.get_pending_commands("t::PC-01", "t::all", 8)
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("PRAGMA data_version") as cur:
            dv1 = (await cur.fetchone())[0]
    ok("③ 만료할 게 없으면 조회가 DB 를 안 바꾼다", dv0 == dv1 and len(rows) == 1)
    old = (db.datetime.now(db.timezone.utc) - db.timedelta(seconds=db.COMMAND_MAX_AGE_SEC + 60)).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("INSERT INTO commands(pc_id,command,args,status,created_at) VALUES('t::PC-02','start','{}','pending',?)", (old,))
        await c.commit()
    rows = await db.get_pending_commands("t::PC-02", "t::all", 8)
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT status FROM commands WHERE pc_id='t::PC-02'") as cur:
            st = [r[0] for r in await cur.fetchall()]
    ok("③-b 15분 만료 규칙은 그대로", st == ["expired"] and rows == [])

    c2 = await db.insert_command("t::PC-03", "start", {})
    await db.cancel_command(c2)
    rv = await db.ack_command(c2)
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT status FROM commands WHERE id=?", (c2,)) as cur:
            s2 = (await cur.fetchone())[0]
    ok("④ 취소된 명령을 뒤늦은 ack 이 못 되돌린다", s2 == "cancelled" and rv is False)
    c3 = await db.insert_command("t::PC-04", "start", {})
    ok("④-b 정상 ack 은 된다", await db.ack_command(c3) is True)

    ok("⑤ _like_prefix 가 `_`·`%`·`\\` 를 막는다", db._like_prefix("a_b%c\\d") == "a\\_b\\%c\\\\d")
    await db.insert_command("aXb::PC-01", "남의것", {})
    await db.insert_command("a_b::PC-01", "내것", {})
    got = [r["command"] for r in await db.get_recent_commands(20, ns_prefix="a_b")]
    ok("⑤-b `a_b` 테넌트가 `aXb` 행을 안 집는다", got == ["내것"], str(got))

    await db.upsert_char_info("t::PC-05", 12345, [{"slot": 1, "name": "가"}], merge=False, collected_at="2026-09-11T00:00:00")
    await db.upsert_char_info("t::PC-05", 0, [{"slot": 1, "name": "가"}], merge=False, collected_at="2026-09-11T01:00:00")
    ok("⑥ 키나만 못 읽은 판이 기존 창고키나를 안 지운다", (await db.get_char_info("t::PC-05"))["total_kina"] == 12345)
    await db.upsert_char_info("t::PC-05", 999, [{"slot": 1, "name": "가"}], merge=False, collected_at="2026-09-11T02:00:00")
    ok("⑥-b 새 값이 오면 덮는다", (await db.get_char_info("t::PC-05"))["total_kina"] == 999)

    # 재접속 배달 — 여러 건, 오래된 순
    ids = [await db.insert_command("t::PC-06", "cmd%d" % i, {"n": i}) for i in range(5)]
    rows = await db.get_pending_commands("t::PC-06", "t::all", 8)
    ok("⑦ 밀린 명령을 오래된 순으로 여러 건 준다", [r["id"] for r in rows] == ids)
    ok("⑦-b 단건판은 맨 앞 한 건", (await db.get_pending_command("t::PC-06", "t::all"))["id"] == ids[0])


async def t_state_and_ws():
    for i in range(1, 13):
        await db.upsert_status("PC-%02d" % i, {"pc_id": "PC-%02d" % i, "status": "hunting"})
        await db.upsert_char_info("PC-%02d" % i, 100 + i, [{"slot": 1, "name": "n%d" % i}])
    with ConnCounter() as cc:
        rows = await main._build_full_state("main")
    n12 = cc.n
    for i in range(13, 25):
        await db.upsert_status("PC-%02d" % i, {"pc_id": "PC-%02d" % i, "status": "hunting"})
        await db.upsert_char_info("PC-%02d" % i, 100 + i, [{"slot": 1, "name": "n%d" % i}])
    with ConnCounter() as cc:
        rows2 = await main._build_full_state("main")
    ok("⑧ 카드가 2배가 돼도 DB 연결 수가 안 는다(N+1 없음)", n12 == cc.n and cc.n <= 9, "12장→%d · 24장→%d" % (n12, cc.n))
    one = [r for r in rows2 if r["pc_id"] == "PC-07"][0]
    ok("⑧-b char_info 값이 카드에 그대로", one.get("_total_kina") == 107 and one.get("chars") == ["n7"])

    # 소켓 소유권 — 재접속한 새 소켓을 옛 실패가 못 지운다 (send 경로 + 핸들러 finally 경로)
    main.macro_ws_connections.clear()
    main._WS_KEPT[0] = 0

    class Boom:
        async def send_text(self, m):
            raise RuntimeError("죽은 소켓")
    dead, live = Boom(), FakeWS()
    main.macro_ws_connections["PC-Z"] = dead
    orig = dead.send_text

    async def _swap(m):
        main.macro_ws_connections["PC-Z"] = live
        await orig(m)
    dead.send_text = _swap
    sent = await main.send_command_to_macro("PC-Z", "start", {}, 1)
    ok("⑨ 옛 소켓의 전송 실패가 새 연결 자리를 안 지운다", sent is False and main.macro_ws_connections.get("PC-Z") is live)
    key = main.ns("main", "PC-01")
    a, b = FakeWS(), FakeWS()
    ta = asyncio.create_task(main.macro_websocket(a, "PC-01"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if main.macro_ws_connections.get(key) is a:
            break
    tb = asyncio.create_task(main.macro_websocket(b, "PC-01"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if main.macro_ws_connections.get(key) is b:
            break
    a.die()
    await asyncio.wait_for(ta, timeout=5)
    await asyncio.sleep(0.05)
    ok("⑨-b 핸들러: 옛 연결이 죽어도 새 연결이 자리에 남는다", main.macro_ws_connections.get(key) is b and a.closed == 1012)
    b.die()
    await asyncio.wait_for(tb, timeout=5)

    # 대시보드 /ws — 어느 길로 나가든 목록에서 빠진다
    main.manager.active = []
    tok = main.new_session("main")
    ws = FakeWS()
    ws.cookies = {"session": tok}
    task = asyncio.create_task(main.websocket_endpoint(ws))
    for _ in range(60):
        await asyncio.sleep(0.01)
        if main.manager.active:
            break
    joined = len(main.manager.active) == 1
    ws.die()
    await asyncio.wait_for(task, timeout=5)
    ok("⑩ 대시보드 WS 가 붙고 끊기면 빠진다", joined and len(main.manager.active) == 0)
    _real = main._build_full_state_inner

    async def _boom(t="main"):
        raise RuntimeError("일부러")
    main._build_full_state_inner = _boom
    ws2 = FakeWS()
    ws2.cookies = {"session": tok}
    await main.websocket_endpoint(ws2)
    main._build_full_state_inner = _real
    ok("⑩-b 초기 조립이 터져도 목록에 안 남는다", len(main.manager.active) == 0)

    # push_state — 보는 사람 없으면 안 만들고, 예외 판도 계측에 남는다
    main._PERF.clear()
    main.manager.active = []
    await main.push_state("main")
    ok("⑪ 보는 사람이 없으면 상태를 안 만든다", main._PERF.get("push_state_skipped", {}).get("n") == 1 and "build_full_state" not in main._PERF)
    fake = FakeWS()
    main.manager.active = [(fake, "main")]
    main._build_full_state_inner = _boom
    try:
        await main.push_state("main")
    except RuntimeError:
        pass
    main._build_full_state_inner = _real
    ok("⑪-b push_state 가 예외로 죽어도 계측에 남는다", main._PERF.get("push_state_failed", {}).get("n") == 1)
    main.manager.active = []

    # 굳음 감시기 — 일부러 루프를 막으면 적는다
    main._STALLS.clear()
    t = asyncio.create_task(main._loop_watchdog())
    await asyncio.sleep(0.2)
    time.sleep(1.6)
    await asyncio.sleep(0.7)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    ok("⑫ 루프를 1.6초 막으면 감시기가 적는다", bool(main._STALLS) and main._STALLS[-1]["stall_s"] >= 1.0, str(main._STALLS[-1:]))


async def t_alarm_logs_guards():
    main.TELEGRAM_BOT_TOKEN = "T"
    main.TENANTS.setdefault("main", {})["chat_id"] = "12345"
    main._TG_MUTE.clear()
    main._TG_MUTE[main.ns("main", "PC-01")] = time.time() + 3600
    r = await main.telegram_send("PC-01", Req({"text": "그냥 알림"}))
    b = json.loads(bytes(r.body))
    ok("⑬ 보통 알림은 음소거에 걸리고 ok:False", b.get("muted") is True and b.get("ok") is False, str(b)[:80])
    ok("⑬-b 생략을 그 PC 로그에 남긴다", any("중계 생략" in (x.get("message") or "") for x in await db.get_logs("PC-01", limit=10)))
    r2 = await main.telegram_send("PC-01", Req({"text": "⛔ 정지했습니다"}))
    ok("⑬-c ⛔ 는 음소거를 뚫는다", json.loads(bytes(r2.body)).get("muted") is not True)
    main._TG_MUTE.clear()
    main.TELEGRAM_BOT_TOKEN = ""

    many = {"logs": [{"level": "info", "message": "L%d" % i} for i in range(70)]}
    b = json.loads(bytes((await main.receive_logs("PC-06", Req(many))).body))
    ok("⑭ 로그 배치는 저장한 수를 답한다(50/70/버림20)", b.get("count") == 50 and b.get("received") == 70 and b.get("dropped") == 20, str(b))
    ok("⑭-b 버렸다는 사실이 로그로 남는다", any("20줄 버림" in (x.get("message") or "") for x in await db.get_logs("PC-06", limit=200)))

    threw = None
    try:
        await main.dashboard_send_updater_command("all", Req({"command": "update"}, api_key=None, session=main.new_session("main")))
    except HTTPException as e:
        threw = e.status_code
    ok("⑮ /updater/command/all 은 A7 로 400", threw == 400)
    r = await main.dashboard_send_updater_command("PC-01", Req({"command": "update"}, api_key=None, session=main.new_session("main")))
    ok("⑮-b 한 대 지정은 200", r.status_code == 200)

    # WS ack 이 이력을 방송한다 — 대시보드가 붙어 있으면 cmd_history 가 온다
    cid = await db.insert_command("PC-A1", "find_host", {})
    fake = FakeWS()
    main.manager.active = [(fake, "main")]
    ws = FakeWS()
    task = asyncio.create_task(main.macro_websocket(ws, "PC-A1"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if main.macro_ws_connections.get("PC-A1") is ws:
            break
    ws._gate.set()          # receive_text 가 "{}" 를 돌려주게 — 아래 ack 프레임을 넣기 위한 준비
    await asyncio.sleep(0.02)
    # ack 프레임을 직접 넣는다
    orig_recv = ws.receive_text

    async def _recv_once():
        ws.receive_text = orig_recv
        return json.dumps({"type": "ack", "command_id": cid})
    ws.receive_text = _recv_once
    ws._gate.set()
    for _ in range(60):
        await asyncio.sleep(0.01)
        if any('"cmd_history"' in m for m in fake.sent):
            break
    ws.die()
    try:
        await asyncio.wait_for(task, timeout=5)
    except Exception:
        pass
    main.manager.active = []
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT status FROM commands WHERE id=?", (cid,)) as cur:
            st = (await cur.fetchone())[0]
    ok("⑯ WS ack 이 DB 를 acked 로 하고 대시보드에 이력을 방송한다",
       st == "acked" and any('"cmd_history"' in m for m in fake.sent), "status=%s 방송=%d" % (st, len(fake.sent)))


async def t_bug_cache():
    main._PERF.clear()
    main._BUG_COUNT_CACHE.clear()
    import os
    bdir = main.tenant_bugs_dir("main")
    os.makedirs(bdir, exist_ok=True)
    for f in os.listdir(bdir):
        os.remove(os.path.join(bdir, f))
    for i in range(3):
        open(os.path.join(bdir, "PC-0%d_20260910_010101_x.png" % (i + 1)), "wb").close()
    c1 = main._bug_counts("main")
    c2 = main._bug_counts("main")
    ok("⑰ 버그 개수 캐시가 두 번째부터 먹는다", sum(c1.values()) == 3 and c1 == c2 and main._PERF.get("bug_counts_cached", {}).get("n") == 1)
    open(os.path.join(bdir, "PC-09b_20260910_010101_z.png"), "wb").close()
    main._bug_cache_bust("main")
    c3 = main._bug_counts("main")
    ok("⑰-b 버스트 뒤 다시 세고 접미사는 베이스로(PC-09b→PC-09)", c3.get("PC-09") == 1 and "PC-09b" not in c3, str(c3))
    c3["가짜"] = 9
    ok("⑰-c 캐시는 사본을 준다", "가짜" not in main._bug_counts("main"))


def test_all():
    run_all([t_db_layer, t_state_and_ws, t_alarm_logs_guards, t_bug_cache])
    finish("test_regressions", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
