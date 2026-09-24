# -*- coding: utf-8 -*-
"""#125 OCR 판별 — /bugs 씨앗 (2026-09-24 아이온2 요청 · 주인님 «팜뷰 탭이 뜨자마자 바로 판별»).

매크로가 예전부터 /bugs 로 올리던 ocrdiff_*·oddfail_* 크롭을 OCR 큐로 옮긴다(ocr_label.seed_*).
  P  순수 파이썬 PNG 디코드·dHash (서버엔 Pillow 가 없다) — ★이 파일의 인코더는 필터 다섯을 따로 구현★ 해 디코더를 찌른다
  N  파일 이름 → site·pc·시각·로컬/제미나이 힌트 · 캡차·다른 버그스샷은 안 받는다
  S  씨앗 돌리기 · 몇 번 돌려도 같다 · 큐/이미지/팜뷰 · 첫 조회 자동 · 업로드 훅 · 가득(507) · 겹침
    cd updater/server && python -X utf8 tests/test_ocr_seed.py
"""
import asyncio
import os
import shutil
import struct
import tempfile
import zlib

from _harness import main, db, ok, run_all, finish   # noqa: E402

import ocr_label as OL                                # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

MIN_CHECKS = 44
KEY = "testkey"
TOK = "fvsecret-seed"
C = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
SESS = main.new_session("main")


# ─── 독립 PNG 인코더(필터 0~4 를 직접 계산) ─────────────────────────────────────
def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def png(w, h, px, ctype=0, filt=0, depth=8, interlace=0, plte=None, idat_split=1):
    """px = 줄마다 바이트(bytes, 길이 w*bpp). filt = 모든 줄에 쓸 필터(0~4) 또는 줄마다 목록."""
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = bytearray()
    prev = bytes(len(px[0]) if px else 0)
    for y in range(h):
        line = px[y]
        f = filt[y % len(filt)] if isinstance(filt, (list, tuple)) else filt
        out = bytearray()
        for x in range(len(line)):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            v = line[x]
            if f == 1:
                v -= a
            elif f == 2:
                v -= b
            elif f == 3:
                v -= (a + b) >> 1
            elif f == 4:
                v -= _paeth(a, b, c)
            out.append(v & 255)
        raw += bytes([f]) + out
        prev = line
    comp = zlib.compress(bytes(raw))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    ch = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, interlace))
    if plte:
        ch += chunk(b"PLTE", plte)
    k = max(1, len(comp) // idat_split)
    for i in range(0, len(comp), k):
        ch += chunk(b"IDAT", comp[i:i + k])
    return b"\x89PNG\r\n\x1a\n" + ch + chunk(b"IEND", b"")


def gray_rows(w, h, fn):
    return [bytes(fn(x, y) & 255 for x in range(w)) for y in range(h)]


def crop(seed, w=120, h=24):
    """글자 크롭 흉내 — seed 마다 무늬가 다르다."""
    return png(w, h, gray_rows(w, h, lambda x, y: ((x * (seed + 3)) ^ (y * 7 + seed * 13)) % 251), filt=[0, 1, 2, 3, 4])


# ─── P: PNG 디코드 · dHash ────────────────────────────────────────────────────
def t_png():
    w, h = 13, 7
    rows = gray_rows(w, h, lambda x, y: (x * 37 + y * 91 + x * y * 5) % 256)
    exp = b"".join(rows)
    for f in range(5):
        g = OL._png_gray(png(w, h, rows, filt=f))
        ok("P-1.%d 필터 %d 로 쓴 회색 PNG 를 바이트 그대로 되읽는다" % (f, f), g is not None and g[:2] == (w, h) and bytes(g[2]) == exp,
           str(g and bytes(g[2])[:10]))
    import random
    rnd = random.Random(125)
    tie = [bytes(rnd.choice((0, 1, 2, 3)) for _ in range(40)) for _ in range(30)]      # 값 0~3 무작위 — Paeth 동률(a≠b)이 잦다
    gt = OL._png_gray(png(40, 30, tie, filt=4))
    ok("P-1b Paeth 동률 규칙(a → b → c 순서) — 명암 낮은 그림", gt is not None and bytes(gt[2]) == b"".join(tie), "")
    g = OL._png_gray(png(w, h, rows, filt=[4, 3, 2, 1, 0], idat_split=4))
    ok("P-2 줄마다 다른 필터 + IDAT 여러 조각", g is not None and bytes(g[2]) == exp, "")
    rgb = [bytes(v for x in range(w) for v in ((x * 20) % 256, (y * 30) % 256, (x * y) % 256)) for y in range(h)]
    lum = bytes(((x * 20 % 256) * 299 + (y * 30 % 256) * 587 + (x * y % 256) * 114) // 1000 for y in range(h) for x in range(w))
    g2 = OL._png_gray(png(w, h, rgb, ctype=2, filt=4))
    ok("P-3 RGB → 밝기(299·587·114)", g2 is not None and bytes(g2[2]) == lum, "")
    rgba = [bytes(v for x in range(w) for v in ((x * 20) % 256, (y * 30) % 256, (x * y) % 256, 255)) for y in range(h)]
    g3 = OL._png_gray(png(w, h, rgba, ctype=6, filt=3))
    ga = [bytes(v for x in range(w) for v in ((x * 9) % 256, 128)) for y in range(h)]
    g4 = OL._png_gray(png(w, h, ga, ctype=4, filt=1))
    ok("P-4 RGBA·회색+알파도 읽는다", g3 is not None and bytes(g3[2]) == lum and g4 is not None
       and bytes(g4[2]) == bytes((x * 9) % 256 for y in range(h) for x in range(w)), "")
    pal = bytes([0, 0, 0, 255, 255, 255, 255, 0, 0])
    pr = [bytes((x + y) % 3 for x in range(w)) for y in range(h)]
    g5 = OL._png_gray(png(w, h, pr, ctype=3, plte=pal))
    lut = [0, 255, 76]
    ok("P-5 팔레트 PNG", g5 is not None and bytes(g5[2]) == bytes(lut[(x + y) % 3] for y in range(h) for x in range(w)), "")
    bad = [("16비트", png(w, h, [bytes(2 * w)] * h, depth=16)), ("인터레이스", png(w, h, rows, interlace=1)),
           ("잘림", png(w, h, rows)[:40]), ("PNG 아님", b"GIF89a" + b"\0" * 40), ("빈", b""),
           ("팔레트 없음", png(w, h, pr, ctype=3)),
           ("모르는 필터", png(w, h, rows)[:8] + b"garbage" * 5)]
    res = []
    for name, raw in bad:
        try:
            res.append((name, OL._png_gray(raw) is None and OL.png_dhash(raw) is None))
        except Exception as e:
            res.append((name, "exc:%s" % type(e).__name__))
    ok("P-6 못 읽는 PNG 는 전부 None(예외 없음)", all(v is True for _, v in res), str(res))
    # 헤더가 거대한 크기를 말하면 풀기 전에 거절(압축 폭탄)
    huge = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 50000, 50000, 8, 0, 0, 0, 0) + b"\0\0\0\0"
    ok("P-7 50000x50000 이라 말하는 PNG 는 풀지 않고 None", OL._png_gray(huge) is None, "")
    # 줄 필터 바이트가 5 이상이면 None
    rawbad = bytearray(zlib.decompress(zlib.compress(b"".join(b"\x07" + r for r in rows))))
    evil = png(w, h, rows)
    i = evil.index(b"IDAT")
    ln = struct.unpack(">I", evil[i - 4:i])[0]
    comp = zlib.compress(bytes(rawbad))
    evil = evil[:i - 4] + struct.pack(">I", len(comp)) + b"IDAT" + comp + b"\0\0\0\0" + evil[i + 4 + ln + 4:]
    ok("P-8 필터 번호 7 → None", OL._png_gray(evil) is None, "")
    # dHash — 9x8 에서 오른쪽이 늘 밝으면 전부 1, 늘 어두우면 전부 0, 크기가 커도 칸 평균
    up = png(9, 8, gray_rows(9, 8, lambda x, y: x * 20 + y))
    dn = png(9, 8, gray_rows(9, 8, lambda x, y: 200 - x * 20))
    big = png(90, 40, gray_rows(90, 40, lambda x, y: x * 2))
    ok("P-9 dHash: 오른쪽이 밝으면 ffff…, 어두우면 0000…, 10배 크기도 같은 답",
       OL.png_dhash(up) == "f" * 16 and OL.png_dhash(dn) == "0" * 16 and OL.png_dhash(big) == "f" * 16,
       "%s %s %s" % (OL.png_dhash(up), OL.png_dhash(dn), OL.png_dhash(big)))
    tiny = png(3, 2, gray_rows(3, 2, lambda x, y: x * 50))
    d = OL.png_dhash(tiny)
    ok("P-10 9x8 보다 작은 그림도 16자리(칸이 비지 않는다)", isinstance(d, str) and len(d) == 16 and int(d, 16) >= 0, str(d))
    a1, a2 = OL.png_dhash(crop(1)), OL.png_dhash(crop(1))
    ok("P-11 같은 그림 = 같은 dHash · 다른 무늬는 다름", a1 == a2 and a1 != OL.png_dhash(crop(40)), "%s %s" % (a1, OL.png_dhash(crop(40))))


# ─── N: 파일 이름 ─────────────────────────────────────────────────────────────
def t_names():
    p = OL.seed_parse("PC-04_20260924_011502_PC-04_20260924_101500_ocrdiff_odd_energy_L120c690s840_G120lu1c690rs840.png")
    ok("N-1 감사 불일치: site=odd_energy · 로컬/제미나이 힌트를 되읽는다",
       p and p["site"] == "odd_energy" and p["loc"] == "120,690/840" and p["gem"] == "120(+1,690)/840" and p["pc"] == "PC-04",
       str(p))
    from datetime import datetime, timezone
    exp_ts = datetime(2026, 9, 24, 1, 15, 2, tzinfo=timezone.utc).timestamp()
    ok("N-1b 시각은 서버가 붙인 UTC 이름에서(매크로 이름의 로컬 시각이 아니라)", p and abs(p["ts"] - exp_ts) < 1, str(p and p["ts"]))
    p = OL.seed_parse("PC-11b_20260924_011502_PC-11b_20260924_101500_ocrdiff_kina_Lempty_G12x34.png")
    ok("N-2 빈 값 «empty» → '' · 모르는 글자 x → ?", p and p["site"] == "kina" and p["loc"] == "" and p["gem"] == "12?34", str(p))
    p = OL.seed_parse("PC-07_20260924_011502_PC-07_20260924_101500_ocrdiff_combat_power.png")
    ok("N-3 섀도 불일치(값 없음): site 만", p and p["site"] == "combat_power" and p["gem"] == "" and p["loc"] == "", str(p))
    pn = OL.seed_parse("PC-05_20260924_011502_PC-05_20260924_101500_oddfail_narrow.png")
    pw = OL.seed_parse("PC-05_20260924_011502_PC-05_20260924_101500_oddfail_wide.png")
    ok("N-4 오드 실패: 좁은 = odd_energy · 넓은 = odd_energy_wide(자른 폭이 달라 묶음을 나눈다)",
       pn and pw and pn["site"] == "odd_energy" and pw["site"] == "odd_energy_wide", "%s %s" % (pn, pw))
    rej = ["PC-05_20260924_011502_PC-05_20260924_101500_ocrdiff_captcha_L1_G2.png",
           "PC-05_20260924_011502_PC-05_20260924_101500_ocrdiff_캡차.png",
           "PC-05_20260924_011502_login_captcha_ocrdiff_x.png",
           "PC-05_20260924_011502_PC-05_stuck_lobby.png",
           "PC-05_20260924_011502_PC-05_ocrlearn_kina_123.png",
           "ocrdiff_kina.png", "PC-05_2026092_011502_ocrdiff_kina.png", "PC-05_20260924_011502_ocrdiff_.png"]
    got = [(n, OL.seed_parse(n)) for n in rej]
    ok("N-5 캡차·다른 버그스샷·학습 크롭·이름 모양 틀림은 None", all(v is None for _, v in got), str([n for n, v in got if v]))
    p = OL.seed_parse("PC_X_20260924_011502_PC_X_20260924_101500_ocrdiff_a/../b.png")
    ok("N-6 site 의 경로 글자는 걷힌다(파일 경로가 못 된다)", p is None or ("/" not in p["site"] and ".." not in p["site"]), str(p))


# ─── S: 씨앗 ─────────────────────────────────────────────────────────────────
class _Store:
    """DB·OCR·/bugs 를 잠깐 새것으로."""

    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix="ocrseed_")
        self.saved = (db.DB_PATH, OL.OCR_DIR, OL.OCR_DISK_CAP, OL.OCR_DISK_HARD_CAP, main.BUGS_DIR)
        db.DB_PATH = os.path.join(self.d, "s.db")
        OL.OCR_DIR = os.path.join(self.d, "ocr")
        main.BUGS_DIR = os.path.join(self.d, "bugs")
        os.makedirs(main.BUGS_DIR)
        OL._SEED_DONE.clear()
        OL._SEED_BUSY.clear()
        main.FV_TOKEN, main.FV_TENANT = TOK, "main"
        return self

    def put(self, name, raw):
        with open(os.path.join(main.BUGS_DIR, name), "wb") as f:
            f.write(raw)

    def __exit__(self, *e):
        db.DB_PATH, OL.OCR_DIR, OL.OCR_DISK_CAP, OL.OCR_DISK_HARD_CAP, main.BUGS_DIR = self.saved
        OL._SEED_DONE.clear()
        shutil.rmtree(self.d, ignore_errors=True)


def S(method, url, **kw):
    C.cookies.clear()
    C.cookies.set("session", SESS)
    try:
        return getattr(C, method)(url, **kw)
    finally:
        C.cookies.clear()


def _pfx(pc, i):
    return "%s_20260924_0115%02d_%s_20260924_1015%02d_" % (pc, i, pc, i)


def _rows():
    import sqlite3
    c = sqlite3.connect(db.DB_PATH)
    try:
        return c.execute("SELECT site, pc_id, gemini, local, count, prompt FROM ocr_img ORDER BY id").fetchall()
    finally:
        c.close()


def t_seed():
    with _Store() as st:
        st.put(_pfx("PC-04", 1) + "ocrdiff_odd_energy_L120c690s840_G120lu1c690rs840.png", crop(1))
        st.put(_pfx("PC-05", 2) + "oddfail_narrow.png", crop(2))
        st.put(_pfx("PC-05", 3) + "oddfail_wide.png", crop(3, w=300))
        st.put(_pfx("PC-06", 4) + "ocrdiff_kina.png", crop(4))
        st.put(_pfx("PC-06", 5) + "ocrdiff_captcha_L1_G2.png", crop(5))              # 캡차 — 절대 안 들어간다
        st.put(_pfx("PC-06", 6) + "stuck_lobby.png", crop(6))                          # 다른 버그스샷
        st.put(_pfx("PC-06", 7) + "ocrdiff_kina_L1_G2.png", b"\x89PNG\r\n\x1a\nbroken")  # 깨진 PNG
        st.put(_pfx("PC-06", 8) + "ocrdiff_kina_L3_G4.png", png(10, 10, [bytes(20)] * 10, depth=16))
        r = S("post", "/ocr/seed_bugs")
        b = r.json()
        ok("S-1 ★씨앗: 네 장이 큐로(ocrdiff 2 · oddfail 2), 캡차·깨짐·16비트는 건너뜀★",
           r.status_code == 200 and b.get("added") == 4 and b.get("exists") == 0 and b.get("full") is False
           and b.get("skipped", {}).get("대상아님") == 1 and b.get("skipped", {}).get("디코드못함") == 2, str(b))
        ok("S-1b 씨앗은 캡차 이름 파일을 훑기는 해도 넣지 않는다", b.get("scanned") == 7, str(b))   # stuck_lobby 는 훑지도 않는다
        rows = _rows()
        sites = sorted(r_[0] for r_ in rows)
        ok("S-2 site·pc·힌트·출처가 줄에 남는다",
           sites == ["kina", "odd_energy", "odd_energy", "odd_energy_wide"]
           and ("odd_energy", "PC-04", "120(+1,690)/840", "120,690/840") in [r_[:4] for r_ in rows]
           and all(r_[5].startswith("seed:/bugs PC-") for r_ in rows), str(rows))
        q = S("get", "/ocr/queue", params={"limit": 100}).json()
        ok("S-3 대시보드 큐에 나온다(4묶음 — 좁은 오드 두 장은 무늬가 달라 각각)", q["pending"] == 4 and len(q["items"]) == 4,
           str(q["pending"]))
        it = [i for i in q["items"] if i["pc"] == "PC-04"][0]
        img = S("get", "/ocr/img/%s" % it["img"])
        ok("S-4 이미지는 원본 바이트 그대로", img.status_code == 200 and img.content == crop(1), str(img.status_code))
        r2 = S("post", "/ocr/seed_bugs").json()
        rows2 = _rows()
        ok("S-5 ★두 번 돌려도 같다 — added 0 · exists 4 · count 도 안 오른다★",
           r2.get("added") == 0 and r2.get("exists") == 4 and [x[4] for x in rows2] == [1, 1, 1, 1], "%s %s" % (r2, rows2))
        # 팜뷰 길
        r3 = C.post("/api/fv/ocr/seed_bugs", json={})
        ok("S-6 팜뷰 씨앗은 토큰 없으면 막힌다(401)", r3.status_code == 401, str(r3.status_code))
        r4 = C.post("/api/fv/ocr/seed_bugs", json={}, headers={"X-FV-Token": TOK})
        b4 = r4.json()
        ok("S-7 팜뷰 씨앗 모양 {ok, scanned, added, exists, skipped, full, more}",
           r4.status_code == 200 and {"ok", "scanned", "added", "exists", "skipped", "full", "more"} <= set(b4)
           and b4["added"] == 0, str(b4))
        r5 = S("post", "/ocr/seed_bugs", )
        ok("S-7b 세션 없으면 대시보드 씨앗도 막힌다", C.post("/ocr/seed_bugs").status_code in (401, 403), "")
        _ = r5


def t_auto_and_upload():
    with _Store() as st:
        st.put(_pfx("PC-09", 1) + "ocrdiff_kina_L1_G2.png", crop(11))
        r = C.get("/api/fv/ocr/queue", params={"limit": 50}, headers={"X-FV-Token": TOK})
        b = r.json()
        ok("S-8 ★팜뷰 큐를 처음 보면 씨앗이 저절로 돈다(탭이 뜨자마자 판별 거리)★",
           r.status_code == 200 and b.get("pending") == 1 and b["items"][0]["pc"] == "PC-09", str(b)[:300])
        st.put(_pfx("PC-09", 2) + "ocrdiff_kina_L3_G4.png", crop(12))
        b2 = C.get("/api/fv/ocr/queue", params={"limit": 50}, headers={"X-FV-Token": TOK}).json()
        ok("S-9 자동 씨앗은 프로세스마다 한 번(두 번째 조회는 폴더를 다시 안 훑는다)", b2.get("pending") == 1, str(b2.get("pending")))
        # 업로드 훅 — 새로 올라온 ocrdiff 는 곧바로 큐로
        r = C.post("/bugs/PC-10", headers={"X-Api-Key": KEY},
                   files={"file": ("PC-10_20260924_101500_ocrdiff_odd_energy_L1s840_G2s840.png", crop(13), "image/png")})
        b3 = C.get("/api/fv/ocr/queue", params={"limit": 50}, headers={"X-FV-Token": TOK}).json()
        ok("S-10 ★/bugs 업로드된 ocrdiff 는 곧바로 큐에(업로드 응답 200 그대로)★",
           r.status_code == 200 and b3.get("pending") == 2 and "PC-10" in [i["pc"] for i in b3["items"]], str(b3.get("pending")))
        r = C.post("/bugs/PC-10", headers={"X-Api-Key": KEY},
                   files={"file": ("PC-10_20260924_101501_stuck.png", crop(14), "image/png")})
        r2 = C.post("/bugs/PC-10", headers={"X-Api-Key": KEY},
                    files={"file": ("PC-10_20260924_101502_ocrdiff_captcha_L1_G2.png", crop(15), "image/png")})
        b4 = C.get("/api/fv/ocr/queue", params={"limit": 50}, headers={"X-FV-Token": TOK}).json()
        ok("S-11 보통 버그스샷·캡차 이름은 큐에 안 들어간다(업로드는 200)",
           r.status_code == 200 and r2.status_code == 200 and b4.get("pending") == 2, str(b4.get("pending")))
        # 훅이 터져도 업로드는 성공
        real = OL.seed_one

        async def _boom(*a, **k):
            raise RuntimeError("x")
        OL.seed_one = _boom
        try:
            r = C.post("/bugs/PC-10", headers={"X-Api-Key": KEY},
                       files={"file": ("PC-10_20260924_101503_ocrdiff_kina.png", crop(16), "image/png")})
        finally:
            OL.seed_one = real
        ok("S-12 씨앗 훅이 실패해도 /bugs 업로드는 200", r.status_code == 200, str(r.status_code))


def t_full_busy():
    with _Store() as st:
        for i in range(5):
            st.put(_pfx("PC-12", i) + "ocrdiff_kina_L%d_G%d.png" % (i, i), crop(20 + i))
        OL.OCR_DISK_CAP = 10 ** 12                         # 테넌트 상한(대기 비우기)은 안 닿게 — 전체 상한만
        OL.OCR_DISK_HARD_CAP = len(crop(20)) + 10          # 한 장만 들어갈 자리 → 두 번째에 507
        b = S("post", "/ocr/seed_bugs").json()
        ok("S-13 ★전체 상한이 차면 멈추고 full:true (507 을 삼키지 않고 알린다)★",
           b.get("full") is True and b.get("added") == 1 and b.get("scanned") == 2, str(b))
    with _Store() as st:
        st.put(_pfx("PC-13", 1) + "ocrdiff_kina.png", crop(30))
        OL._SEED_BUSY.add("main")
        try:
            b = S("post", "/ocr/seed_bugs").json()
        finally:
            OL._SEED_BUSY.discard("main")
        ok("S-14 같은 테넌트 씨앗이 도는 중이면 겹쳐 돌지 않는다(busy)", b.get("busy") is True and b.get("added") == 0, str(b))
        big = png(2100, 2000, [bytes(2100)] * 2000)
        st.put(_pfx("PC-13", 2) + "ocrdiff_kina_L1_G1.png", big)
        OL_MAX = OL.SEED_MAX_PIXELS
        OL.SEED_MAX_PIXELS = 1000
        try:
            b = S("post", "/ocr/seed_bugs").json()
        finally:
            OL.SEED_MAX_PIXELS = OL_MAX
        ok("S-15 픽셀 상한을 넘는 PNG 는 풀지 않고 건너뛴다", b.get("added") == 0 and b.get("skipped", {}).get("디코드못함") == 2,
           str(b))


async def t_first_wait():
    """★첫 조회는 SEED_FIRST_WAIT_S 까지만 기다린다★ (2026-09-24 아이온2 반증: 2,000장 34.8초 > 팜뷰 시간제한 10초)."""
    import time as _t
    with _Store() as st:
        for i in range(20):
            st.put(_pfx("PC-11", i) + "ocrdiff_kina.png", crop(40 + i))
        real, wait0 = OL.seed_one, OL.SEED_FIRST_WAIT_S
        calls = []

        async def _slow(*a, **k):
            calls.append(1)
            await asyncio.sleep(0.05)
            return "added"
        OL.seed_one, OL.SEED_FIRST_WAIT_S = _slow, 0.2
        try:
            t0 = _t.monotonic()
            await OL._auto_seed("main")
            dt = _t.monotonic() - t0
            task = OL._SEED_TASKS.get("main")
            ok("S-15 ★느린 씨앗(20장×0.05초)이어도 첫 조회는 상한(0.2초) 근처에서 돌아온다★", dt < 0.6, "%.2fs" % dt)
            ok("S-15b 씨앗은 뒤에서 계속 돈다(과제가 살아 있고 아직 다 안 훑음)",
               task is not None and not task.done() and 0 < len(calls) < 20, "calls=%d" % len(calls))
            await OL._auto_seed("main")
            ok("S-15c 도는 중에 다시 조회하면 새로 안 띄운다(같은 과제·곧바로)", OL._SEED_TASKS.get("main") is task, "")
            await task
            ok("S-15d 뒤 과제가 끝까지 훑고 DONE — 20장 한 번씩", len(calls) == 20 and "main" in OL._SEED_DONE, "calls=%d" % len(calls))
        finally:
            OL.seed_one, OL.SEED_FIRST_WAIT_S = real, wait0
            OL._SEED_TASKS.clear()


def test_all():
    run_all([t_png, t_names, t_seed, t_auto_and_upload, t_full_busy, t_first_wait])
    finish("test_ocr_seed", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
