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

MIN_CHECKS = 20


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
    old_il, old_us = main.insert_logs, main.upsert_status

    async def slow_logs(pc, ents):
        order.append("logs-start")
        await asyncio.sleep(0.8)
        order.append("logs-end")

    async def fast_up(pc, payload):
        order.append("status")

    main.insert_logs = slow_logs
    try:
        la = (_dt.now(_tz.utc) - _td(seconds=3)).strftime("%Y-%m-%dT%H:%M:%S")
        msgs = [json.dumps({"type": "log", "logs": [{"level": "info", "message": "x%d" % i} for i in range(50)]}),
                json.dumps({"type": "status", "payload": {"status": "paused", "last_active": la}})]
        t0 = _t.monotonic()
        ws = await _drive(msgs, fast_up)
        ok("W432-a ★느린 로그 묶음(0.8초)이 앞에 있어도 상태 보고는 로그가 끝나기 전에 처리된다★",
           order.index("status") < order.index("logs-end"), str(order))
    finally:
        main.insert_logs = old_il
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
    main._lag_note("recv", "PC-LAG", la)
    main._lag_note("recv", "PC-LAG", (_dt.now(_tz.utc) - _td(seconds=13)).strftime("%Y-%m-%dT%H:%M:%S"))
    ex = main._LAG_PC["PC-LAG"][-1]
    ok("W432-e 초과분 = raw − 그 PC 최소값(시계차 상쇄): 3초 바닥 뒤 13초면 약 10초", 8.5 <= ex <= 11.5, str(ex))
    ok("W432-f 바닥을 모르는 PC 의 send 는 기록하지 않는다(0 거짓말 금지)", main._LAG["send"].__len__() == n_send)
    ok("W432-g 쓸 수 없는 last_active 는 조용히 무시", main._lag_note("recv", "PC-X", "garbage") is None and "PC-X" not in main._LAG_BASE)
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


def inspect_src(f):
    import inspect
    return inspect.getsource(f)


def test_all():
    run_all([t_all, t_432])
    finish("test_ws_dblock", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
