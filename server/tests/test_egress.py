# -*- coding: utf-8 -*-
"""[대시보드] 나가는 바이트 계기판 (2026-09-26 주인님 «레일웨이 결제 비용이 왜이러냐» — Network $51.62 ≈ 1TB/월).

  ① `_EgressMiddleware` — 앱이 소켓에 넘긴 바이트를 경로 묶음별로(HTTP 본문 · WS 보낸 글자) · 오래 사는 WS 는 1MB 마다
  ② GET /diag/egress (main 세션만) — 경로·/img 파일·/check 가 권한 이미지·텔레그램 사진 올림
  ③ /img 가드 — 상류 해시가 version.json 과 다르면 안 내줌(503, 작게) · 한 IP·한 파일 한 시간 상한(429)

    cd updater/server && python -X utf8 tests/test_egress.py
"""
import asyncio
import hashlib
import time

from fastapi.testclient import TestClient

from _harness import main, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 17
C = TestClient(main.app, raise_server_exceptions=False)
PNG = b"\x89PNG-fake-" * 3000
SHA = hashlib.sha256(PNG).hexdigest()


def _reset():
    for k in ("route", "img", "check_offer", "out"):
        main._EGRESS[k].clear()
    main._EGRESS["since"] = time.time() - 3600
    main._IMG_IP_HITS.clear()
    main._IMG_CAP_HITS["n"] = 0


def _ver(images):
    main._version_cache["data"] = {"exe": {"version": "1.1.1013", "sha256": "0" * 64},
                                   "updater": {"version": "9.9.9", "sha256": "1" * 64}, "images": images}
    main._version_cache["ts"] = time.time()


def t_keys():
    cases = {"/": "/", "/health": "/health", "/img/a b.png": "/img/*", "/dl/tok/macro-1.exe": "/dl/*",
             "/api/fv/snapshot": "/api/fv/snapshot", "/api/fv/pc/PC-01": "/api/fv/pc", "/bugs/image/PC-01/x.png": "/bugs/image/*",
             "/live/PC-01.jpg": "/live/*", "/ws/macro/PC-01": "/ws/macro", "/ws": "/ws", "/updater/logs/PC-01": "/updater/logs",
             "/diag/egress": "/diag/egress"}
    got = {p: main._egress_key(p) for p in cases}
    ok("E-1 경로 묶음 — PC·파일·서명 같은 가변 조각은 * (표가 안 붇는다)", got == cases,
       str({k: v for k, v in got.items() if cases[k] != v}))
    _reset()
    for i in range(main._EGRESS_KEYS_MAX + 50):
        main._egress_add("img", "f%d.png" % i, 1)
    ok("E-2 표 크기 상한 — 넘치면 (기타) 하나로", len(main._EGRESS["img"]) <= main._EGRESS_KEYS_MAX + 1
       and main._EGRESS["img"]["(기타)"]["n"] == 50, str(len(main._EGRESS["img"])))


def t_http_counted():
    _reset()
    r = C.get("/health")
    d = main._EGRESS["route"].get("/health") or {}
    ok("E-3 HTTP 본문 바이트를 그대로 센다(/health)", r.status_code == 200 and d.get("bytes") == len(r.content) and d.get("n") == 1,
       "%s %d" % (d, len(r.content)))


async def t_ws_counted():
    _reset()
    sent = []

    async def app(scope, receive, send):
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": "가" * 100})          # utf-8 300 B
        await send({"type": "websocket.send", "bytes": b"x" * 50})
        await send({"type": "websocket.send", "text": "y" * (1 << 20)})     # 1MB 넘음 → 흘려 적기
        mid = dict(main._EGRESS["route"].get("WS /ws/macro") or {})
        sent.append(mid)
        await send({"type": "websocket.send", "text": "z" * 10})

    async def _send(m):
        pass

    async def _recv():
        return {"type": "websocket.connect"}
    mw = main._EgressMiddleware(app)
    await mw({"type": "websocket", "path": "/ws/macro/PC-01"}, _recv, _send)
    d = main._EGRESS["route"].get("WS /ws/macro") or {}
    ok("E-4 WS 보낸 글자(텍스트는 utf-8 바이트)·바이너리 합", d.get("bytes") == 300 + 50 + (1 << 20) + 10, str(d))
    ok("E-5 ★오래 사는 WS 는 끊기기 전에도 1MB 마다 적힌다★", (sent[0] or {}).get("bytes") == 300 + 50 + (1 << 20), str(sent))


def t_diag():
    _reset()
    main._egress_add("route", "/img/*", 5_000_000)
    r = C.get("/diag/egress")
    ok("E-6 /diag/egress 는 세션 없으면 401", r.status_code == 401, str(r.status_code))
    C.cookies.set("session", main.new_session("main"))
    try:
        j = C.get("/diag/egress").json()
    finally:
        C.cookies.clear()
    top = (j.get("route") or [{}])[0]
    ok("E-7 경로별 합·시간당·월 환산(한 시간 5MB → 3.6GB/월)", top.get("key") == "/img/*" and top.get("mb_per_h") == 5.0
       and 3.5 <= top.get("gb_per_month", 0) <= 3.7 and "usd_per_month_at_0.05" in j, str(top))


async def t_img_guards():
    _reset()
    _ver({"a.png": SHA})
    main._IMG_PROXY_CACHE.clear()
    keep_fetch, keep_n = main._fetch_image_upstream, main.IMG_IP_CAP_N
    calls = []

    def bad(fname, sources):
        calls.append(fname)
        return b"OLD-BYTES", "raw", []
    main._fetch_image_upstream = bad
    try:
        r = C.get("/img/a.png")
        ok("E-8 ★상류가 옛 판(해시 ≠ json)이면 내주지 않는다★ — 작은 503 · 캐시에도 안 담음",
           r.status_code == 503 and len(r.content) < 200 and "a.png" not in main._IMG_PROXY_CACHE, "%s %d" % (r.status_code, len(r.content)))

        def good(fname, sources):
            calls.append(fname)
            return PNG, "raw", []
        main._fetch_image_upstream = good
        main.IMG_IP_CAP_N = 3
        main._IMG_IP_HITS.clear()
        codes = [C.get("/img/a.png").status_code for _ in range(4)]
        ok("E-9 한 IP 가 한 파일을 한 시간에 상한 넘게 당기면 429(작게)", codes == [200, 200, 200, 429], str(codes))
        d = main._EGRESS["img"].get("a.png") or {}
        ok("E-10 /img 파일별 바이트 — 내준 것만 센다(429·503 은 0)", d.get("n") == 3 and d.get("bytes") == 3 * len(PNG), str(d))
        ok("E-11 막은 횟수가 남는다", main._IMG_CAP_HITS["n"] == 1, str(main._IMG_CAP_HITS))
        ok("E-12 ★기본 상한이 함대 한 번 업데이트를 안 막는다★ — 24대 × 4(재시도) 의 여러 배", keep_n >= 24 * 4 * 5, str(keep_n))
        main._IMG_IP_HITS.clear()
        main.IMG_IP_CAP_N = keep_n
        fleet = [C.get("/img/a.png", headers={"x-forwarded-for": "203.0.113.7"}).status_code for _pc in range(24) for _try in range(4)]
        ok("E-12b ★공인 IP 하나 뒤 24대가 같은 파일을 4번씩(96회)★ — 전부 200", fleet == [200] * 96,
           "%d 개 중 200 이 아닌 것 %s" % (len(fleet), sorted(set(fleet))))
        main._IMG_IP_HITS.clear()
        other = C.get("/img/a.png", headers={"x-forwarded-for": "9.9.9.9"})
        ok("E-13 다른 파일·다른 곳은 따로 센다", other.status_code == 200, str(other.status_code))
    finally:
        main._fetch_image_upstream, main.IMG_IP_CAP_N = keep_fetch, keep_n
        main._IMG_PROXY_CACHE.clear()


def t_check_offer():
    _reset()
    _ver({"a.png": SHA, "b.png": "2" * 64})
    C.post("/check", json={"exe_version": "1.1.1013", "updater_version": "9.9.9", "image_hashes": {"a.png": SHA}},
           headers={"X-Api-Key": "testkey"})
    C.post("/check", json={"exe_version": "1.1.1013", "updater_version": "9.9.9", "image_hashes": {"a.png": SHA}},
           headers={"X-Api-Key": "testkey"})
    t = main._EGRESS["check_offer"]
    ok("E-14 ★/check 가 같은 이미지를 몇 번 권했나★ — 고리 탐지(b.png 두 번, 맞는 a.png 는 0)",
       (t.get("b.png") or {}).get("n") == 2 and "a.png" not in t, str(t))


async def t_telegram_out():
    _reset()
    keep = main._tg_call

    async def fake(method, data=None, files=None, timeout=None, **kw):
        return {"ok": True, "result": {"message_id": 1}}
    main._tg_call = fake
    try:
        await main.tg_send_photo("1", "cap", b"p" * 1234)
    finally:
        main._tg_call = keep
    d = main._EGRESS["out"].get("telegram sendPhoto") or {}
    ok("E-15 텔레그램 사진 올림도 나가는 바이트로 센다", d.get("bytes") == 1234 and d.get("n") == 1, str(d))
    ok("E-16 계기판은 main 의 미들웨어에 걸려 있다", any(getattr(m, "cls", None) is main._EgressMiddleware for m in main.app.user_middleware), "")


def test_all():
    run_all([t_keys, t_http_counted, t_ws_counted, t_diag, t_img_guards, t_check_offer, t_telegram_out])
    finish("test_egress", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
