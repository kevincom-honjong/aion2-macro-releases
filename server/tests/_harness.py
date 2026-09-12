# -*- coding: utf-8 -*-
"""[대시보드] 시험 공통 발판 — 임시 DB/폴더로 main.py 를 ★진짜로★ 띄운다.

★규칙★
  · 운영 DB·볼륨은 절대 안 건드린다 — 전부 임시 폴더(환경변수로 갈아끼움).
  · 검사는 「있다」(grep) 가 아니라 「먹힌다」(실제 호출) 로 쓴다.
  · 모든 시험 파일은 `finish(name, MIN_CHECKS)` 로 끝낸다 — 검사가 조용히 빠지면 래칫이 잡는다.
"""
import asyncio
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="dashtest_")
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "t.db"))
os.environ.setdefault("BUGS_DIR", os.path.join(_TMP, "bugs"))
os.environ.setdefault("DASHBOARD_PASSWORD", "harness")
os.environ.setdefault("API_KEY", "testkey")
os.environ.setdefault("TTS_DIR", os.path.join(_TMP, "tts"))

SRV = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRV not in sys.path:
    sys.path.insert(0, SRV)
os.chdir(SRV)

import aiosqlite                                     # noqa: E402
import database as db                                # noqa: E402
import main                                          # noqa: E402
from fastapi import HTTPException, WebSocketDisconnect   # noqa: E402

__all__ = ["main", "db", "aiosqlite", "ok", "FakeWS", "Req", "run_all", "finish",
           "HTTPException", "WebSocketDisconnect", "ConnCounter", "TG_SENT"]

# ★텔레그램은 밖으로 안 나간다★ — 시험이 진짜 봇 API 를 두드리면 느리고(404) 사람 폰에 갈 수도 있다.
#   기본은 기록만 한다. 특정 시험이 실패를 흉내내려면 main.tg_send_text 를 잠깐 바꾸고 되돌린다.
TG_SENT: list = []


async def _tg_record(chat, text, **kw):
    TG_SENT.append((chat, text))
    return 1


main.tg_send_text = _tg_record
main.TELEGRAM_BOT_TOKEN = ""          # tg_enabled() 가 False → 필요한 시험만 켠다

_RAN: list = []
_BAD: list = []


def ok(name: str, cond, detail: str = "") -> bool:
    _RAN.append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not cond:
        _BAD.append(name)
    return bool(cond)


class ConnCounter:
    """aiosqlite.connect 호출 수를 센다 — N+1 회귀 감시."""

    def __init__(self):
        self.n = 0
        self._real = aiosqlite.connect

    def __enter__(self):
        outer = self

        def _patched(*a, **k):
            outer.n += 1
            return outer._real(*a, **k)
        aiosqlite.connect = _patched
        db.aiosqlite.connect = _patched
        return self

    def __exit__(self, *e):
        aiosqlite.connect = self._real
        db.aiosqlite.connect = self._real


class FakeWS:
    """핸들러가 실제로 쓰는 것만 흉내낸 WebSocket."""

    def __init__(self, key: str = "testkey"):
        self.query_params = {"key": key}
        self.headers = {}
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self.cookies = {}
        self.sent = []
        self.closed = None
        self._gate = asyncio.Event()
        self._die = False

    async def accept(self):
        pass

    async def receive_text(self):
        await self._gate.wait()
        self._gate.clear()
        if self._die:
            raise WebSocketDisconnect(code=1005)
        return "{}"

    async def send_text(self, m):
        self.sent.append(m)

    async def close(self, code=1000):
        self.closed = code
        self._die = True
        self._gate.set()

    def die(self):
        self._die = True
        self._gate.set()


class Req:
    """FastAPI Request 흉내 — 핸들러가 실제로 쓰는 것만."""

    def __init__(self, body=None, api_key="testkey", session=None, headers=None, host="127.0.0.1"):
        self._body = body or {}
        self.headers = {}
        if api_key:
            self.headers["X-Api-Key"] = api_key
        if headers:
            self.headers.update(headers)
        self.cookies = {"session": session} if session else {}
        self.client = type("C", (), {"host": host})()
        self.query_params = {}

    async def json(self):
        return self._body


def run_all(tests):
    """async 시험 함수 목록을 순서대로. DB 는 처음 한 번 만든다."""
    async def _go():
        await db.init_db()
        for t in tests:
            r = t()
            if asyncio.iscoroutine(r) or hasattr(r, "__await__"):   # 동기 시험도 섞어 쓴다
                await r
    asyncio.run(_go())


def finish(name: str, min_checks: int):
    """정산 + 래칫. pytest 에서는 assert, CLI 에서는 종료코드."""
    print("-" * 60)
    if len(_RAN) < min_checks:
        _BAD.append("래칫(%d < %d)" % (len(_RAN), min_checks))
        print("★검사 수가 래칫 아래다★ %d < %d — 검사가 조용히 빠졌다" % (len(_RAN), min_checks))
    print("%s: %d/%d %s" % (name, len(_RAN) - len(_BAD), len(_RAN), "✔" if not _BAD else "✘"))
    if _BAD:
        print("실패:", ", ".join(_BAD))
    if __name__ != "__main__" and "pytest" in sys.modules:
        assert not _BAD, "실패: " + ", ".join(_BAD)
        return
    sys.exit(1 if _BAD else 0)
