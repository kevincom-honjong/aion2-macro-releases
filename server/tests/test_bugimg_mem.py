# -*- coding: utf-8 -*-
"""[대시보드] 버그스샷 보기용 JPEG + 메모리 진단 (2026-09-27 Railway 청구: /bugs/image 1위 · Memory 920MB).

  GET  /bugs/image/{fn}                 원본 PNG 그대로(에이전트 템플릿 수확은 무손실이 필요)
  GET  /bugs/image/{fn}?fmt=jpg&q=&w=   보기용 JPEG — 대시보드 썸네일이 쓴다
  GET  /diag/mem · POST /diag/mem/trim  main 세션만

    cd updater/server && python -X utf8 tests/test_bugimg_mem.py
"""
import io
import os

from fastapi.testclient import TestClient
from PIL import Image

from _harness import main, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 19
C = TestClient(main.app, raise_server_exceptions=False)
FN = "PC-01_20260927_010203_PC-01_20260927_100203_test-shot.png"


def _png():
    d = main.tenant_bugs_dir("main")
    os.makedirs(d, exist_ok=True)
    im = Image.new("RGB", (1280, 720))
    px = im.load()
    for y in range(720):                           # 게임 화면처럼 매끈한 결 + 잔잔한 잡음(PNG 가 잘 못 누른다)
        for x in range(1280):
            n = (x * 1103515245 + y * 12345) >> 7 & 7
            px[x, y] = ((x // 5 + n) % 256, (y // 3 + n) % 256, ((x + y) // 8 + n) % 256)
    p = os.path.join(d, FN)
    im.save(p, "PNG")
    return p


def _get(path, sess="main", **params):
    if sess:
        C.cookies.set("session", main.new_session(sess))
    try:
        return C.get(path, params=params)
    finally:
        C.cookies.clear()


async def t_bugimg():
    p = _png()
    raw = open(p, "rb").read()
    ok("B-1 세션 없으면 401", C.get("/bugs/image/" + FN).status_code == 401, "")
    r = _get("/bugs/image/" + FN)
    ok("B-2 ★기본은 원본 PNG 바이트 그대로★(에이전트 수확 계약)", r.status_code == 200 and r.content == raw
       and r.headers["content-type"] == "image/png", str(r.headers.get("content-type")))
    ok("B-3 immutable 캐시 헤더(브라우저가 다시 안 받는다)", "immutable" in r.headers.get("cache-control", ""), "")
    j = _get("/bugs/image/" + FN, fmt="jpg")
    im = Image.open(io.BytesIO(j.content))
    ok("B-4 ?fmt=jpg → JPEG, 크기 그대로", j.headers["content-type"] == "image/jpeg" and im.format == "JPEG"
       and im.size == (1280, 720), str(im.size))
    ok("B-5 JPEG 이 원본보다 작다", len(j.content) < len(raw), f"{len(j.content)} vs {len(raw)}")
    t = _get("/bugs/image/" + FN, fmt="jpg", q="70", w="640")
    ti = Image.open(io.BytesIO(t.content))
    ok("B-6 w=640 → 640x360 썸네일, 더 작다", ti.size == (640, 360) and len(t.content) < len(j.content), str(ti.size))
    lo = _get("/bugs/image/" + FN, fmt="jpg", q="1", w="5")
    lo40 = _get("/bugs/image/" + FN, fmt="jpg", q="40", w="160")
    ok("B-7 q·w 는 범위로 잘린다(q≥40, w≥160)", Image.open(io.BytesIO(lo.content)).size == (160, 90)
       and lo.content == lo40.content, f"{len(lo.content)} vs {len(lo40.content)}")
    ok("B-8 q 가 숫자가 아니면 400", _get("/bugs/image/" + FN, fmt="jpg", q="x").status_code == 400, "")
    ok("B-9 없는 파일 404(jpg 도)", _get("/bugs/image/nope.png", fmt="jpg").status_code == 404, "")
    ok("B-10 경로 탈출 안 된다", _get("/bugs/image/..%2F..%2Ft.db", fmt="jpg").status_code == 404, "")
    # 변환 실패 → 원본으로 (Pillow 가 없거나 깨진 파일)
    bad = os.path.join(main.tenant_bugs_dir("main"), "PC-01_20260927_000000_broken.png")
    open(bad, "wb").write(b"not a png")
    b = _get("/bugs/image/PC-01_20260927_000000_broken.png", fmt="jpg")
    ok("B-11 ★변환 못 하면 원본을 준다★(X-Fmt-Fallback)", b.status_code == 200 and b.content == b"not a png"
       and b.headers.get("x-fmt-fallback") == "png", str(b.headers.get("x-fmt-fallback")))
    # 캐시 상한
    keep = main.BUG_JPEG_CACHE_MAX
    main.BUG_JPEG_CACHE_MAX = 1
    try:
        main._BUG_JPEG_CACHE.clear()
        _get("/bugs/image/" + FN, fmt="jpg", w="320")
        _get("/bugs/image/" + FN, fmt="jpg", w="480")
        n = len(main._BUG_JPEG_CACHE)
    finally:
        main.BUG_JPEG_CACHE_MAX = keep
    ok("B-12 변환본 캐시는 바이트 상한을 넘지 않는다", n == 0, str(n))
    html = main.DASHBOARD_HTML if hasattr(main, "DASHBOARD_HTML") else ""
    if not html:
        src = open(main.__file__, encoding="utf8").read()
        html = src
    tiny = os.path.join(main.tenant_bugs_dir("main"), "PC-01_20260927_000001_crop.png")
    Image.new("RGB", (340, 16), (20, 20, 20)).save(tiny, "PNG")
    tb = _get("/bugs/image/PC-01_20260927_000001_crop.png", fmt="jpg")
    ok("B-14 ★JPEG 이 더 크면(작은 단색 크롭) 원본을 준다★", tb.headers["content-type"] == "image/png"
       and tb.content == open(tiny, "rb").read(), str(tb.headers.get("content-type")))
    ok("B-13 대시보드 썸네일은 ?fmt=jpg, 클릭은 원본", "?fmt=jpg&q=70&w=640\" data-orig=\"/bugs/image/" in html
       and "window.open(this.dataset.orig" in html, "")


async def t_mem():
    ok("M-1 /diag/mem 세션 없으면 401 · 렌탈 세션도 401", C.get("/diag/mem").status_code == 401
       and _get("/diag/mem", sess="t2").status_code == 401, "")
    main._MEMTEST_BIG = {"k%d" % i: ("%04d" % i) * 250 for i in range(200)}   # ≈200KB 짜리 전역
    try:
        j = _get("/diag/mem").json()
    finally:
        del main._MEMTEST_BIG
    names = [r["name"] for r in j["globals_over_64k"]]
    row = next((r for r in j["globals_over_64k"] if r["name"] == "_MEMTEST_BIG"), None)
    ok("M-2 큰 전역을 이름·개수·바이트로 보여준다", row is not None and row["len"] == 200 and row["bytes"] > 200_000, str(names[:5]))
    ok("M-3 proc 칸이 있다(리눅스 밖에선 빈 dict)", isinstance(j["proc"], dict) and "types_top" not in j, "")
    jt = _get("/diag/mem", types="1").json()
    ok("M-4 ?types=1 → 종류별 개수", isinstance(jt.get("types_top"), list) and len(jt["types_top"]) > 0, "")
    C.cookies.set("session", main.new_session("main"))
    try:
        tr = C.post("/diag/mem/trim")
    finally:
        C.cookies.clear()
    ok("M-5 trim 은 main 만 · 리눅스 밖에선 ok:false 로 이유를 준다(죽지 않는다)",
       C.post("/diag/mem/trim").status_code == 401 and tr.status_code == 200 and "ok" in tr.json(), tr.text[:120])


def test_all():
    run_all([t_bugimg, t_mem])
    finish("test_bugimg_mem", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
