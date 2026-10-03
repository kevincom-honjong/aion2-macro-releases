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

MIN_CHECKS = 10


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


def test_all():
    run_all([t_all])
    finish("test_ws_dblock", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
