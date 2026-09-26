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

MIN_CHECKS = 29
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
    seen = []
    keepf = main._FADVISE
    main._FADVISE = lambda fd, off, ln, adv: seen.append((off, ln, adv))
    try:
        r = C.post(
            "/bugs/PC-09", headers={"X-Api-Key": "testkey"},
            files={"file": ("PC-09_20260927_000000_stuck.png", raw, "image/png")})
    finally:
        main._FADVISE = keepf
    ok("B-15 ★업로드한 스샷은 페이지 캐시에서 내린다(DONTNEED)★", r.status_code == 200 and seen == [(0, 0, getattr(os, "POSIX_FADV_DONTNEED", 4))],
       str(seen))
    bdir = main.tenant_bugs_dir("main")
    seen_mid = {}
    real_fsync = os.fsync

    def spy_fsync(fd):
        main._bug_cache_bust("main")
        seen_mid["list"] = [b["filename"] for b in main._list_bug_files("main", "PC-08")]
        seen_mid["scan"] = [f for f in os.listdir(bdir) if f.endswith(".png") and f.startswith("PC-08_")]
        seen_mid["part"] = [f for f in os.listdir(bdir) if f.endswith(".part")]
        return real_fsync(fd)
    os.fsync = spy_fsync
    try:
        r = C.post("/bugs/PC-08", headers={"X-Api-Key": "testkey"},
                   files={"file": ("PC-08_20260927_000000_stuck.png", raw, "image/png")})
    finally:
        os.fsync = real_fsync
    fn8 = r.json().get("filename", "")
    ok("B-16 ★쓰는 중엔 목록·훑기에 안 보이고(.part 만 있다), 끝나면 온전한 파일★(반증 2026-09-27)",
       r.status_code == 200 and seen_mid.get("list") == [] and seen_mid.get("scan") == [] and len(seen_mid.get("part", [])) == 1
       and open(os.path.join(bdir, fn8), "rb").read() == raw and not [f for f in os.listdir(bdir) if f.endswith(".part")],
       str(seen_mid))

    def bad_fsync(fd):
        raise OSError(5, "EIO")
    os.fsync = bad_fsync
    try:
        r = C.post("/bugs/PC-07", headers={"X-Api-Key": "testkey"},
                   files={"file": ("PC-07_20260927_000000_stuck.png", raw, "image/png")})
    finally:
        os.fsync = real_fsync
    ok("B-17 ★fsync 실패(EIO)는 500 · 반쪽 파일도 임시 파일도 안 남는다★",
       r.status_code == 500 and not [f for f in os.listdir(bdir) if f.startswith("PC-07_") or f.endswith(".part")],
       f"{r.status_code} {[f for f in os.listdir(bdir) if 'PC-07' in f]}")
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


async def t_series():
    main._MEM_SERIES.clear()
    main._MEM_NEXT[0] = 0.0
    main._mem_sample_tick(1000.0)
    main._mem_sample_tick(1000.0 + main.MEM_SAMPLE_S - 1)
    n1 = len(main._MEM_SERIES)
    main._mem_sample_tick(1000.0 + main.MEM_SAMPLE_S)
    ok("M-6 5분에 한 점만(감시견 0.5초마다 불려도)", n1 == 1 and len(main._MEM_SERIES) == 2, f"{n1} {len(main._MEM_SERIES)}")
    for i in range(700):
        main._MEM_NEXT[0] = 0.0
        main._mem_sample_tick(float(i))
    ok("M-7 점은 576개(48시간)에서 잘린다", len(main._MEM_SERIES) == 576, str(len(main._MEM_SERIES)))
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for i in range(24):
            main._MEM_NEXT[0] = 0.0
            main._mem_sample_tick(float(i))
    calls = []
    keep = main._malloc_trim
    main._malloc_trim = lambda: calls.append(1) or 1
    try:
        for i in range(24):
            main._MEM_NEXT[0] = 0.0
            main._mem_sample_tick(float(i))
    finally:
        main._malloc_trim = keep
    ok("M-11 한 시간(12점)마다 malloc_trim", len(calls) == 2, str(len(calls)))
    ok("M-12 리눅스 밖에선 malloc_trim 이 -1(죽지 않는다)", main._malloc_trim() == -1, "")
    ok("M-10 ★48시간이 지나 점이 꽉 차도 [mem] 줄은 12점마다 계속★(반증 2026-09-27)",
       buf.getvalue().count("[mem]") == 2 and len(main._MEM_SERIES) == 576, str(buf.getvalue().count("[mem]")))
    import asyncio
    main._MEM_SERIES.clear()
    main._MEM_NEXT[0] = 0.0
    wd = asyncio.create_task(main._loop_watchdog())
    await asyncio.sleep(0.8)
    wd.cancel()
    ok("M-9 ★감시견이 실제로 점을 찍는다★(부팅 때 도는 그 코루틴)", len(main._MEM_SERIES) == 1, str(len(main._MEM_SERIES)))
    for i in range(300):
        main._MEM_NEXT[0] = 0.0
        main._mem_sample_tick(float(i))
    j = _get("/diag/mem").json()
    ok("M-8 /diag/mem 에 최근 288점(24시간)과 up_s", len(j["series_5min"]) == 288 and "up_s" in j["series_5min"][-1], "")


def test_all():
    run_all([t_bugimg, t_mem, t_series])
    finish("test_bugimg_mem", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
