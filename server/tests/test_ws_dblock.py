# -*- coding: utf-8 -*-
"""[대시보드] #431 — 매크로 WS 가 «database is locked» 한 번에 닫히지 않는다 (2026-10-03).

실측(/diag/perf): ws_closes 400건 중 349건이 OperationalError: database is locked 로 닫힘, build_full_state 최대 5.02초
(= sqlite 기본 대기 5초). 고친 것: ① 연결 대기 DB_BUSY_S(기본 30초) ② WS 메시지 처리는 잠김이면 물러서며 3번 다시 ③ 걸린 곳 기록.
"""
import asyncio
import json
import sqlite3

from _harness import main, ok, FakeWS, run_all, finish   # noqa: E402
import database as D                                         # noqa: E402

MIN_CHECKS = 69


class Feed(FakeWS):
    def __init__(self, msgs):
        super().__init__(key="testkey")
        self.msgs = list(msgs)
        self.done = asyncio.Event()

    async def receive_text(self):
        if self.msgs:
            return self.msgs.pop(0)
        self.done.set()
        return await super().receive_text()


async def _drive(msgs, fake_upsert):
    old = main.upsert_status
    main.upsert_status = fake_upsert
    ws = Feed(msgs)
    t = asyncio.ensure_future(main.macro_websocket(ws, "PC-01"))
    try:
        w = asyncio.ensure_future(ws.done.wait())
        await asyncio.wait({w, t}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
        w.cancel()
        await asyncio.sleep(0.2)
    finally:
        main.upsert_status = old
        ws.die()
        try:
            await asyncio.wait_for(t, 5)
        except Exception:
            pass
    return ws


async def t_all():
    ok("W431-a sqlite 연결 대기가 5초 기본이 아니다(DB_BUSY_S)", D.DB_BUSY_S >= 30, str(D.DB_BUSY_S))
    import inspect
    ok("W431-b database.py 의 모든 연결이 timeout=DB_BUSY_S",
       "aiosqlite.connect(DB_PATH)" not in inspect.getsource(D))
    e = sqlite3.OperationalError("database is locked")
    ok("W431-c _db_is_locked: locked 만 참", main._db_is_locked(e) and not main._db_is_locked(sqlite3.OperationalError("no such table"))
       and not main._db_is_locked(ValueError("database is locked")))
    n0 = main._DB_LOCKED["n"]
    calls = []

    async def flaky(pc, payload):
        calls.append(1)
        if len(calls) <= 2:
            raise sqlite3.OperationalError("database is locked")

    ws = await _drive([json.dumps({"type": "status", "payload": {"status": "idle"}})], flaky)
    ok("W431-d ★잠김 2번 뒤 성공 — 같은 메시지를 3번째에 처리하고 소켓은 안 닫힌다★", len(calls) == 3 and ws.closed is None, "%s %s" % (len(calls), ws.closed))
    ok("W431-e 잠김은 /diag/perf db_locked 에 센다(어디서 걸렸는지 키 포함)",
       main._DB_LOCKED["n"] == n0 + 2 and any(k.startswith("ws:status") for k in main._DB_LOCKED["by"]), str(main._DB_LOCKED)[:300])
    calls.clear()

    async def always(pc, payload):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    ws = await _drive([json.dumps({"type": "status", "payload": {"status": "idle"}}),
                       json.dumps({"type": "pong"})], always)
    ok("W431-f ★계속 잠겨도 3번 뒤 그 메시지만 버린다 — 소켓은 살아서 다음 메시지(pong)를 받는다★", len(calls) == 3 and ws.closed is None and not ws.msgs, "%s %s" % (len(calls), ws.closed))
    calls.clear()

    async def boom(pc, payload):
        calls.append(1)
        raise ValueError("boom")

    ws = await _drive([json.dumps({"type": "status", "payload": {"status": "idle"}})], boom)
    ok("W431-g 잠김이 아닌 오류는 예전처럼 다시 안 하고 루프 밖으로(1번만 호출)", len(calls) == 1, str(len(calls)))
    ok("W431-h /diag/perf 소스에 db_locked 가 나온다", '"db_locked": _DB_LOCKED' in inspect.getsource(main.diag_perf))
    calls.clear()
    ws = await _drive([json.dumps({"type": "status", "payload": []})], boom)
    ok("W431-i 목록 payload 는 처리 없이 건너뜀(루프 계속 · upsert 0번)", len(calls) == 0 and ws.closed is None, str(len(calls)))
    ok("W431-j ocr_label 연결도 같은 대기", __import__("ocr_label")._busy_s() == D.DB_BUSY_S)


async def t_432():
    """#432 — 로그 묶음이 같은 소켓의 상태 보고를 막지 않는다 · 로그는 한 연결·한 커밋 · 지연 계측."""
    import time as _t
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    order = []
    old_il, old_us = main.log_buffer_put, main.upsert_status

    async def slow_logs(pc, ents):
        order.append("logs-start")
        await asyncio.sleep(0.8)
        order.append("logs-end")

    async def fast_up(pc, payload):
        order.append("status")

    main.log_buffer_put = slow_logs
    try:
        la = (_dt.now(_tz.utc) - _td(seconds=3)).strftime("%Y-%m-%dT%H:%M:%S")
        msgs = [json.dumps({"type": "log", "logs": [{"level": "info", "message": "x%d" % i} for i in range(50)]}),
                json.dumps({"type": "status", "payload": {"status": "paused", "last_active": la}})]
        t0 = _t.monotonic()
        ws = await _drive(msgs, fast_up)
        ok("W432-a ★느린 로그 묶음(0.8초)이 앞에 있어도 상태 보고는 로그가 끝나기 전에 처리된다★",
           order.index("status") < order.index("logs-end"), str(order))
    finally:
        main.log_buffer_put = old_il
        main.upsert_status = old_us
    ok("W432-b 로그 일꾼이 끝까지 처리했다(소켓이 끝날 때 남은 묶음을 기다림)", "logs-end" in order, str(order))
    ok("W432-c 메시지 종류별 처리 시간이 /diag/perf 에 센다(status·log)",
       main._PERF.get("ws_msg_status", {}).get("n", 0) >= 1 and main._PERF.get("ws_msg_log", {}).get("n", 0) >= 1)
    r = main._lag_report()
    ok("W432-d 지연 계측: recv·stored 단계가 쌓이고 p50/p95/max 를 낸다",
       r["stages"]["recv"].get("n", 0) >= 1 and "p95" in r["stages"]["recv"] and r["stages"]["stored"].get("n", 0) >= 1, str(r)[:300])
    main._LAG_BASE.pop("PC-LAG", None)
    main._lag_note("send", "PC-LAG", la)
    n_send = main._LAG["send"].__len__()
    main._lag_note("recv_all", "PC-LAG", la)
    _la13 = (_dt.now(_tz.utc) - _td(seconds=13)).strftime("%Y-%m-%dT%H:%M:%S")
    main._lag_note("recv_all", "PC-LAG", _la13)
    main._lag_note("recv", "PC-LAG", _la13)
    ex = main._LAG_PC["PC-LAG"][-1]
    ok("W432-e 초과분 = raw − 그 PC 최소값(시계차 상쇄): 3초 바닥 뒤 13초면 약 10초", 8.5 <= ex <= 11.5, str(ex))
    ok("W432-f 바닥을 모르는 PC 의 send 는 기록하지 않는다(0 거짓말 금지)", main._LAG["send"].__len__() == n_send)
    ok("W432-g 쓸 수 없는 last_active 는 조용히 무시", main._lag_note("recv_all", "PC-X", "garbage") is None and "PC-X" not in main._LAG_BASE)
    ok("W432-h /diag/perf 에 status_lag", '"status_lag": _lag_report()' in inspect_src(main.diag_perf))

    # insert_logs: 한 연결·한 커밋, 줄 수, prune 카운터
    import tempfile, os as _os
    old_path = D.DB_PATH
    d = tempfile.mkdtemp()
    D.DB_PATH = _os.path.join(d, "t.db")
    try:
        await D.init_db()
        n_conn = [0]
        real = D.aiosqlite.connect

        def counting(*a, **k):
            n_conn[0] += 1
            return real(*a, **k)

        D.aiosqlite.connect = counting
        try:
            await D.insert_logs("PC-L", [("info", "a%d" % i) for i in range(50)])
        finally:
            D.aiosqlite.connect = real
        import sqlite3 as _sq
        rows = _sq.connect(D.DB_PATH).execute("SELECT level,message FROM logs WHERE pc_id='PC-L' ORDER BY id").fetchall()
        ok("W432-i ★50줄이 연결 1번으로 순서대로 저장된다(예전: 줄마다 연결 50번)★",
           n_conn[0] == 1 and len(rows) == 50 and rows[0][1] == "a0" and rows[-1][1] == "a49", "%s %s" % (n_conn[0], len(rows)))
        await D.insert_logs("PC-L", [])
        ok("W432-j 빈 묶음은 아무 일도 안 한다", len(_sq.connect(D.DB_PATH).execute("SELECT 1 FROM logs WHERE pc_id='PC-L'").fetchall()) == 50)
    finally:
        D.DB_PATH = old_path


async def t_432b():
    """#432-b — 상태 처리 단계별 계측 · 뜨거운 쓰기(상태·로그)만 synchronous=NORMAL."""
    import tempfile, os as _os
    ok("W432b-a 기본 HOT_SYNC 는 NORMAL(Railway env HOT_SYNC=FULL 로 되돌림)", D.HOT_SYNC == "NORMAL", D.HOT_SYNC)
    old_path = D.DB_PATH
    D.DB_PATH = _os.path.join(tempfile.mkdtemp(), "t.db")
    seen = []
    try:
        await D.init_db()
        real = D.aiosqlite.connect

        class Spy:
            def __init__(self, cm):
                self.cm = cm

            async def __aenter__(self):
                self.db = await self.cm.__aenter__()
                orig = self.db.execute

                def ex(sql, *a, **k):
                    if "synchronous" in str(sql):
                        seen.append(str(sql))
                    return orig(sql, *a, **k)
                self.db.execute = ex
                return self.db

            async def __aexit__(self, *a):
                return await self.cm.__aexit__(*a)

        D.aiosqlite.connect = lambda *a, **k: Spy(real(*a, **k))
        try:
            await D._upsert_status_db("PC-S", {"status": "idle"})      # 쓰기-즉시 경로(사망 전환·PC_FLUSH_S=0)
            n1 = len(seen)
            await D.insert_logs("PC-S", [("info", "x")])
            await D.insert_log("PC-S", "info", "y")
            n2 = len(seen)
            await D.set_setting("k431", "v")
            n3 = len(seen)
        finally:
            D.aiosqlite.connect = real
        ok("W432b-b 상태 보고·로그 쓰기는 PRAGMA synchronous=NORMAL 을 건다", n1 == 1 and n2 == 3 and "NORMAL" in seen[0], str(seen))
        ok("W432b-c ★#434: 설정 같은 나머지 쓰기도 연결마다 NORMAL 을 건다(DB_SYNC 기본 NORMAL) · 뜨거운 쪽은 중복으로 안 건다★",
           n3 == n2 + 1 and D.DB_SYNC == "NORMAL", str(seen))
        t = D.TIMING
        ok("W432b-d db_timing: upsert 단계(connect/select/insert/commit)·logs_batch_write 가 센다",
           all(k in t and t[k]["n"] >= 1 for k in ("upsert_connect", "upsert_select", "upsert_insert", "upsert_commit", "logs_batch_write")), str(list(t)))
    finally:
        D.DB_PATH = old_path
    src = inspect_src(main.macro_websocket)
    ok("W432b-e 상태 갈래가 단계별로 잰다(ws_st_upsert·abyss·push)", all(k in src for k in ("ws_st_upsert", "ws_st_abyss", "ws_st_push")))
    ok("W432b-f /diag/perf 에 db_timing", '"db_timing"' in inspect_src(main.diag_perf))


async def t_434():
    """#434 — WS 로그는 소켓 합쳐 트랜잭션 하나 · 상주 연결 · 모든 연결 NORMAL."""
    import tempfile, os as _os, sqlite3 as _sq, time as _t
    old_path, old_fl = D.DB_PATH, D.LOG_FLUSH_S
    D.DB_PATH = _os.path.join(tempfile.mkdtemp(), "t.db")
    D.LOG_FLUSH_S = 3.0
    try:
        await D.init_db()
        n_conn = [0]
        real = D.aiosqlite.connect

        def counting(*a, **k):
            n_conn[0] += 1
            return real(*a, **k)

        D.aiosqlite.connect = counting
        try:
            for pc in ("PC-A", "PC-B", "PC-C"):
                await D.log_buffer_put(pc, [("info", "%s-%d" % (pc, i)) for i in range(20)])
            ok("W434-a ★버퍼에 쌓는 동안 DB 연결 0번★", n_conn[0] == 0 and D.log_stats()["buffered"] == 60, str(n_conn[0]))
            n = await D.flush_logs()
        finally:
            D.aiosqlite.connect = real
        c = _sq.connect(D.DB_PATH)
        ok("W434-b ★3개 PC 60줄이 연결 1번으로 저장(예전: PC 묶음마다 1번씩 3번)★", n == 60 and n_conn[0] == 1
           and c.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == 60, "%s %s" % (n, n_conn[0]))
        rows = c.execute("SELECT message FROM logs WHERE pc_id='PC-B' ORDER BY id").fetchall()
        ok("W434-c PC 별 순서가 보존된다", [r[0] for r in rows] == ["PC-B-%d" % i for i in range(20)])
        ok("W434-d 빈 버퍼 flush 는 아무 일도 안 한다", await D.flush_logs() == 0)
        # 상한: 가장 오래된 줄부터 버림
        old_max = D.LOG_BUF_MAX
        D.LOG_BUF_MAX = 5
        try:
            await D.log_buffer_put("PC-D", [("info", "d%d" % i) for i in range(8)])
            ok("W434-e 상한(5) 넘으면 오래된 3줄을 버리고 센다", D.log_stats()["buffered"] == 5 and D.log_stats()["dropped"] >= 3, str(D.log_stats()))
        finally:
            D.LOG_BUF_MAX = old_max
        # 실패하면 줄이 되돌아온다
        real_conn = D.connect_hot

        def boom(*a, **k):
            raise RuntimeError("db down")
        D.connect_hot = boom
        try:
            r = await D.flush_logs()
        finally:
            D.connect_hot = real_conn
        ok("W434-f ★flush 실패 시 줄을 잃지 않고 되돌린다 · last_err 기록★", r == 0 and D.log_stats()["buffered"] == 5 and D.log_stats()["last_err"], str(D.log_stats()))
        ok("W434-g 다시 flush 하면 저장된다", await D.flush_logs() == 5 and D.log_stats()["last_err"] is None)
        # 카드 삭제 시 버퍼의 줄이 되살아나지 않는다
        await D.log_buffer_put("PC-Z", [("info", "z")])
        await D.log_buffer_put("PC-Z.upd", [("info", "zu")])
        await D.delete_pc_all_data("PC-Z")
        ok("W434-h 삭제한 PC 의 버퍼 줄도 지워진다", all(r[0] not in ("PC-Z", "PC-Z.upd") for r in D._LOG_BUF))
        # 즉시 모드
        D.LOG_FLUSH_S = 0
        await D.log_buffer_put("PC-I", [("info", "now")])
        ok("W434-i LOG_FLUSH_S=0 이면 예전처럼 바로 저장(버퍼 0)",
           D.log_stats()["buffered"] == 0 and _sq.connect(D.DB_PATH).execute("SELECT COUNT(*) FROM logs WHERE pc_id='PC-I'").fetchone()[0] == 1)
        D.LOG_FLUSH_S = 3.0
        await D.log_buffer_put("PC-J", [("info", "dump")])
        dump = await D.get_pc_dump("PC-J")
        ok("W434-j 카드 덤프는 버퍼 줄까지 싣는다", any("dump" in str(x) for x in dump.get("logs", [])), str(list(dump))[:200])
        # keeper
        await D.keeper_open()
        ok("W434-k keeper 연결이 열리고 닫힌다 · 두 번 열어도 하나", D._KEEPER is not None and await D.keeper_open() is None)
        await D.keeper_close()
        ok("W434-l keeper 닫힘", D._KEEPER is None)
        # 모든 연결 NORMAL
        async with D.connect_db() as db:
            async with db.execute("PRAGMA synchronous") as cur:
                v = (await cur.fetchone())[0]
        ok("W434-m ★아무 연결이나 synchronous=NORMAL(1)★", v == 1, str(v))
        rep = D.held_report()
        ok("W434-n held_report 에 분당 쓰기 수·log_buf·db_sync", all(k in rep for k in ("write_txns_per_min", "log_buf", "db_sync", "keeper")), str(list(rep)))
    finally:
        D.DB_PATH, D.LOG_FLUSH_S = old_path, old_fl
        D._LOG_BUF.clear()
    # ── 보강: 버퍼 줄은 팜뷰 커서를 붙든다 · 알람 줄은 DB 를 안 기다린다
    old_path2 = D.DB_PATH
    D.DB_PATH = _os.path.join(tempfile.mkdtemp(), "t2.db")
    D.LOG_FLUSH_S = 3.0
    try:
        await D.init_db()
        ok("W434-p 비었으면 pending_min 없음", D.log_pending_min() is None)
        await D.log_buffer_put("PC-P", [("info", "p")])
        pm = D.log_pending_min()
        ok("W434-q ★버퍼에 줄이 있으면 pending_min = 그 줄 시각★", pm == D._LOG_BUF[0][3], str(pm))
        seen_mid = []
        real_ins = D.connect_hot

        def spy(*a, **k):
            seen_mid.append(D.log_pending_min())
            return real_ins(*a, **k)
        D.connect_hot = spy
        try:
            await D.flush_logs()
        finally:
            D.connect_hot = real_ins
        ok("W434-r ★저장 중에도 pending_min 이 유지되고 끝나면 None★", seen_mid and seen_mid[0] == pm and D.log_pending_min() is None, str(seen_mid))
        # 알람 줄: DB(flush)가 느려도 _alarm_event 는 바로 돌아온다
        real_fl = D.flush_logs

        async def slow_flush():
            await asyncio.sleep(0.8)
            return 0
        D.flush_logs = slow_flush
        try:
            t0 = _t.monotonic()
            await main._alarm_event("main", "PC-06", "사망 알람")
            el = _t.monotonic() - t0
        finally:
            D.flush_logs = real_fl
        ok("W434-s ★_alarm_event 는 느린 저장(0.8초)을 기다리지 않는다·줄은 버퍼에 있다★",
           el < 0.3 and any("[알람] PC-06" in r[2] for r in D._LOG_BUF), "%.2f %s" % (el, D._LOG_BUF[-1:]))
        await asyncio.sleep(0.9)
        await D.flush_logs()
        rows = _sq.connect(D.DB_PATH).execute("SELECT message FROM logs WHERE message LIKE '[알람] PC-06%'").fetchall()
        ok("W434-t 알람 줄은 결국 저장된다", len(rows) == 1, str(rows))
    finally:
        D.DB_PATH = old_path2
        D._LOG_BUF.clear()
    ok("W434-u 팜뷰 이벤트·로그 조회 둘 다 버퍼 줄 시각 미만까지만 낸다", "log_pending_min" in inspect_src(main.fv_events) and inspect_src(main).count("log_pending_min()") >= 2)
    src = inspect_src(main.macro_websocket)
    ok("W434-o WS 로그 갈래는 log_buffer_put 을 쓴다(insert_logs 직접 호출 없음)", "log_buffer_put" in src and "insert_logs(" not in src)


async def t_435():
    """#435 — 방송 조립 읽기 캐시(stale-while-revalidate) · 업데이터 상태 write-behind."""
    import tempfile, os as _os, sqlite3 as _sq
    old_path, old_rc = D.DB_PATH, D.READ_CACHE_S
    D.DB_PATH = _os.path.join(tempfile.mkdtemp(), "t3.db")
    D.READ_CACHE_S = 5.0
    try:
        await D.init_db()
        n = [0]

        async def loader():
            n[0] += 1
            return [{"a": n[0]}]
        v1 = await D.cached_read("k", loader)
        v2 = await D.cached_read("k", loader)
        ok("W435-a ★두 번째 읽기는 DB(loader)를 안 부른다 · 복사본을 준다★", n[0] == 1 and v1 == v2 and v1 is not v2, str(n))
        v2[0]["a"] = 99
        ok("W435-b 호출부가 고쳐도 캐시는 그대로", (await D.cached_read("k", loader))[0]["a"] == 1)
        D._RC["k"][1] -= 10                       # 만료시킨다
        v3 = await D.cached_read("k", loader)
        ok("W435-c ★만료돼도 옛 값을 바로 준다(기다리지 않음)★", v3[0]["a"] == 1, str(v3))
        await asyncio.sleep(0.05)
        ok("W435-d 갱신은 뒤에서 끝난다 · 다음 읽기는 새 값", (await D.cached_read("k", loader))[0]["a"] == 2 and n[0] == 2, str(n))
        D.read_cache_clear("k")
        ok("W435-e 무효화하면 다시 읽는다", (await D.cached_read("k", loader))[0]["a"] == 3)
        # 쓰는 쪽 무효화
        await D.cached_read("char_info", loader)
        D._char_info_bump()
        ok("W435-f char_info 쓰기(bump)가 캐시를 비운다", "char_info" not in D._RC)
        await D.cached_read("slot_filters", loader)
        await D.upsert_slot_filters("PC-F", {"a": 1})
        ok("W435-g 슬롯 필터 쓰기가 캐시를 비운다", "slot_filters" not in D._RC)
        await D.cached_read("deaths", loader)
        await D.delete_pc_all_data("PC-F")
        ok("W435-h 카드 삭제는 전체 캐시를 비운다", not D._RC)
        # 끄면 매번 읽는다
        D.READ_CACHE_S = 0
        m0 = n[0]
        await D.cached_read("z", loader); await D.cached_read("z", loader)
        ok("W435-i READ_CACHE_S=0 이면 매번 읽는다", n[0] == m0 + 2)
        D.READ_CACHE_S = 5.0
        # get_all_statuses: DB 몫은 캐시, 메모리 보고는 즉시 보인다
        await D._upsert_status_db("PC-DB", {"status": "idle"})
        a1 = await D.get_all_statuses()
        await D._upsert_status_db("PC-DB2", {"status": "idle"})       # 캐시 뒤 DB 에만 생김
        await D.upsert_status("PC-MEM", {"status": "hunting"})        # 메모리
        a2 = await D.get_all_statuses()
        ids2 = [x.get("pc_id") or x.get("_updated_at") for x in a2]
        ok("W435-j ★메모리 보고(PC-MEM)는 캐시와 상관없이 바로 보인다 · DB 에만 새로 생긴 행은 캐시가 풀릴 때까지 안 보인다★",
           any(x.get("status") == "hunting" for x in a2) and len(a2) == len(a1) + 1, "%d %d" % (len(a1), len(a2)))
        # 업데이터 상태 write-behind
        con = lambda: _sq.connect(D.DB_PATH)
        await D.upsert_updater_status("PC-U", {"pc_id": "PC-U", "macro_state": "running", "updater_version": "3.1"})
        ok("W435-k ★업데이터 보고는 DB 에 안 쓰고(메모리) 바로 읽힌다★",
           con().execute("SELECT COUNT(*) FROM updater_status WHERE pc_id='PC-U'").fetchone()[0] == 0
           and any(x.get("pc_id") == "PC-U" for x in await D.get_all_updater_statuses()))
        await D.flush_statuses()
        ok("W435-l flush 가 업데이터 행을 같은 트랜잭션으로 저장", con().execute("SELECT COUNT(*) FROM updater_status WHERE pc_id='PC-U'").fetchone()[0] == 1)
        t_before = D._UPD_MEM["PC-U"]["at"]
        await asyncio.sleep(1.1)
        await D.upsert_updater_status("PC-U", {"pc_id": "PC-U", "macro_state": "running", "updater_version": "3.1"})
        ok("W435-m ★같은 값 재보고는 저장 대상이 아니다(dirty 0) · 그래도 «받은 시각» 은 갱신★",
           not D._UPD_DIRTY and D._UPD_MEM["PC-U"]["at"] > t_before and D.ST_STATS.get("upd_unchanged_skipped", 0) >= 1)
        await D.upsert_updater_status("PC-U", {"pc_id": "PC-U", "macro_state": "stopped", "updater_version": "3.1"})
        ok("W435-n 값이 바뀌면 저장 대상", "PC-U" in D._UPD_DIRTY)
        old_fl = D.LOG_FLUSH_S
        D.LOG_FLUSH_S = 3.0
        try:
            c0 = con().execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            await D.insert_log("PC-IL", "info", "buffered-line", created_at="2026-10-04T01:02:03")
            await D.insert_log("PC-IL", "warn", "direct-line", direct=True)
            ok("W435-q ★insert_log 는 버퍼로(DB 0줄 증가) · direct=True 만 바로★",
               con().execute("SELECT COUNT(*) FROM logs").fetchone()[0] == c0 + 1 and D.log_stats()["buffered"] == 1)
            ok("W435-r log_has 는 버퍼 줄도 본다(먼저 flush)", await D.log_has("PC-IL", "buffered-line"))
            r = con().execute("SELECT created_at, level FROM logs WHERE message='buffered-line'").fetchone()
            ok("W435-s 클라 시각(created_at)과 레벨이 그대로 저장", r == ("2026-10-04T01:02:03", "info"), str(r))
        finally:
            D.LOG_FLUSH_S = old_fl
            D._LOG_BUF.clear()
        await D.delete_pc_all_data("PC-U")
        ok("W435-o 카드 삭제는 업데이터 메모리도 지운다", "PC-U" not in D._UPD_MEM and "PC-U" not in D._UPD_DIRTY)
    finally:
        D.DB_PATH, D.READ_CACHE_S = old_path, old_rc
        D._UPD_MEM.clear(); D._UPD_DIRTY.clear(); D._ST_MEM.clear(); D._ST_DIRTY.clear(); D.read_cache_clear()
    src = inspect_src(main._build_full_state_inner)
    ok("W435-p 방송 조립은 char_info·slot_filters·deaths 를 cached_read 로 읽는다", src.count("cached_read(") >= 3, str(src.count("cached_read(")))


async def t_432f():
    """#432-f — 하트비트(옛 last_active)는 지연 표본이 아니다: status 가 바뀐 보고만 recv/stored 로 센다."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    main._LAG_ST.pop("PC-01", None)
    main._LAG["recv"].clear(); main._LAG["recv_all"].clear(); main._LAG["stored"].clear()
    main._LAG_BASE.pop("PC-01", None)
    old = _dt.now(_tz.utc) - _td(seconds=25)
    f = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S")

    async def up(pc, payload):
        pass

    msgs = [json.dumps({"type": "status", "payload": {"status": "hunting", "last_active": f(_dt.now(_tz.utc))}}),     # 전이(처음)
            json.dumps({"type": "status", "payload": {"status": "hunting", "last_active": f(old)}}),                  # 하트비트(옛 값)
            json.dumps({"type": "status", "payload": {"status": "hunting", "last_active": f(old)}}),
            json.dumps({"type": "status", "payload": {"status": "paused", "last_active": f(_dt.now(_tz.utc))}})]       # 전이
    await _drive(msgs, up)
    ok("W432f-a 4건 중 전이 2건만 recv·stored, 전부는 recv_all", len(main._LAG["recv"]) == 2 and len(main._LAG["stored"]) == 2 and len(main._LAG["recv_all"]) == 4,
       "%s %s %s" % (len(main._LAG["recv"]), len(main._LAG["stored"]), len(main._LAG["recv_all"])))
    ok("W432f-b ★옛 last_active 하트비트가 전이 지연(recv)을 부풀리지 않는다(최대 < 5초)★ — recv_all 쪽만 20초대", max(main._LAG["recv"]) < 5 and max(main._LAG["recv_all"]) > 15,
       "%s %s" % (main._LAG["recv"], main._LAG["recv_all"]))
    ok("W432f-c 리포트에 recv_all 단계와 설명", "recv_all" in main._lag_report()["stages"] and "하트비트" in main._lag_report()["note"])


def inspect_src(f):
    import inspect
    return inspect.getsource(f)


def test_all():
    run_all([t_all, t_432, t_432b, t_434, t_435, t_432f])
    finish("test_ws_dblock", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
