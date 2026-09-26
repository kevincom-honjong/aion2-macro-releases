# -*- coding: utf-8 -*-
"""[대시보드] JSON·글자 응답 gzip 미들웨어 (2026-09-26 Railway 나가는 바이트 — /logs·/status·/characters 가 압축 없이 나갔다).

    cd updater/server && python -X utf8 tests/test_gzip_mw.py
"""
import gzip
import json

from _harness import main, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 14
BIG = json.dumps({"logs": [{"line": "매크로 로그 줄 %d — 사냥 중" % i} for i in range(400)]}, ensure_ascii=False).encode()


def _app(body_parts, ct=b"application/json", extra=(), status=200):
    async def app(scope, receive, send):
        hs = [(b"content-type", ct), (b"content-length", str(sum(len(b) for b in body_parts)).encode())] + list(extra)
        await send({"type": "http.response.start", "status": status, "headers": hs})
        for i, b in enumerate(body_parts):
            await send({"type": "http.response.body", "body": b, "more_body": i < len(body_parts) - 1})
    return app


async def _run(mw_app, method="GET", accept=b"gzip, deflate"):
    out = []

    async def send(m):
        out.append(m)

    async def recv():
        return {"type": "http.request", "body": b"", "more_body": False}
    hs = [(b"accept-encoding", accept)] if accept is not None else []
    await mw_app({"type": "http", "method": method, "path": "/x", "headers": hs}, recv, send)
    start = next(m for m in out if m["type"] == "http.response.start")
    body = b"".join(m.get("body") or b"" for m in out if m["type"] == "http.response.body")
    names = [k.lower() for k, _v in start["headers"]]
    assert names.count(b"content-length") <= 1 and names.count(b"vary") <= 1, "겹친 헤더 %s" % names
    return {k.lower(): v for k, v in start["headers"]}, body, start["status"]


async def t_gzip():
    G = main._GzipJsonMiddleware
    h, b, st = await _run(G(_app([BIG])))
    ok("Z-1 JSON 1KB 넘고 gzip 받으면 압축 · 풀면 원문 그대로", h.get(b"content-encoding") == b"gzip" and gzip.decompress(b) == BIG, "")
    ok("Z-2 content-length = 압축본 길이 · Vary: Accept-Encoding", h.get(b"content-length") == str(len(b)).encode()
       and h.get(b"vary") == b"Accept-Encoding", str(h))
    ok("Z-3 ★실제로 작아진다★(로그 JSON 30% 아래)", len(b) < len(BIG) * 0.3, "%d → %d" % (len(BIG), len(b)))
    h, b, _ = await _run(G(_app([BIG])), accept=None)
    ok("Z-4 gzip 을 안 받는 요청엔 그대로", b"content-encoding" not in h and b == BIG, "")
    h, b, _ = await _run(G(_app([BIG], ct=b"image/png")))
    ok("Z-5 그림(png)·exe 같은 것은 안 건드린다", b"content-encoding" not in h and b == BIG, "")
    h, b, _ = await _run(G(_app([BIG], extra=[(b"content-encoding", b"gzip")])))
    ok("Z-6 이미 인코딩된 응답(대시보드 HTML·FV)은 두 번 안 누른다", b == BIG and h.get(b"content-encoding") == b"gzip", "")
    h, b, _ = await _run(G(_app([b'{"ok":true}'])))
    ok("Z-7 1KB 아래는 그대로", b"content-encoding" not in h and b == b'{"ok":true}', "")
    parts = [BIG[:1000], BIG[1000:5000], BIG[5000:]]
    h, b, _ = await _run(G(_app(parts)))
    ok("Z-8 여러 조각으로 온 본문도 하나로 모아 압축", h.get(b"content-encoding") == b"gzip" and gzip.decompress(b) == BIG, "")
    h, b, _ = await _run(G(_app([BIG], ct=b"text/event-stream")))
    ok("Z-9 SSE(text/event-stream)는 모으지 않는다", b"content-encoding" not in h and b == BIG, "")
    h, b, _ = await _run(G(_app([BIG])), method="HEAD")
    ok("Z-10 HEAD 는 그대로", b"content-encoding" not in h, "")
    h, b, _ = await _run(G(_app([BIG], extra=[(b"vary", b"Cookie")])))
    ok("Z-11 원래 Vary 는 살리고 Accept-Encoding 을 더한다", h.get(b"vary") == b"Cookie, Accept-Encoding", str(h.get(b"vary")))
    keep = main.GZIP_MAX
    main.GZIP_MAX = 3000
    try:
        h, b, _ = await _run(G(_app(parts)))
        ok("Z-12 상한을 넘으면 압축 없이 전부 흘려보낸다(잘리지 않음)", b"content-encoding" not in h and b == BIG, "%d" % len(b))
    finally:
        main.GZIP_MAX = keep
    # 계기판이 압축 ★뒤★ 를 센다 — 순서: 계기판(바깥) → gzip(안쪽)
    main._EGRESS["route"].clear()
    await _run(main._EgressMiddleware(G(_app([BIG]))))
    d = main._EGRESS["route"].get("/x") or {}
    ok("Z-13 계기판은 압축된 바이트를 센다", 0 < d.get("bytes", 0) < len(BIG) * 0.3, str(d))
    mws = [m.cls for m in main.app.user_middleware]
    ok("Z-14 ★main 에서도 계기판이 gzip 보다 바깥★(user_middleware 앞쪽 = 바깥)",
       main._EgressMiddleware in mws and main._GzipJsonMiddleware in mws
       and mws.index(main._EgressMiddleware) < mws.index(main._GzipJsonMiddleware), str([c.__name__ for c in mws]))


def test_all():
    run_all([t_gzip])
    finish("test_gzip_mw", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
