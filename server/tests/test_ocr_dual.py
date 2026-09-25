# -*- coding: utf-8 -*-
"""#108 OCR 두 형식 (2026-09-24 아이온2 결정 «서버가 둘 다 받게 더하기만») — 정본 CONTRACT_OCR.md §2·§3-b.

  D  /ocr/submit — dhash 16자리(64비트)·64자리(256비트) 둘 다 · 길이 기록 · 길이가 다르면 안 묶임 · 매크로 칸 저장
  T  /ocr/labels ts 방식 — (site, prompt_sha1, sha1) 행 · since=ts · 대표만 · bad/undo · 쪽 경계 · 시계가 멈춰도 증가
  M  옛 표(열 없음)에 열을 더한다 — 옛 줄·커서 방식 그대로
  R  ★매크로 lc/ocr_label.py 를 그대로 불러★ before/after → ocr_label_net._send → 서버 200 → 팜뷰 라벨
     → ocr_label_net.poll_now(requests 가 실제로 만드는 since 글자) → _merge → before() 적중 · bad · undo
    cd updater/server && python -X utf8 tests/test_ocr_dual.py
"""
import asyncio
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from urllib.parse import urlsplit, parse_qs

from _harness import main, db, ok, run_all, finish   # noqa: E402

import ocr_label as OL                                # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

MIN_CHECKS = 72
KEY = "testkey"
TOK = "fvsecret-dual"
C = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
HERE = os.path.dirname(os.path.abspath(__file__))
LC = os.path.normpath(os.path.join(HERE, "..", "..", "..", "lc"))
PNG = b"\x89PNG\r\n\x1a\n"
D64 = "0123456789abcdef" * 4


class _Store:
    """DB·OCR 디렉터리를 잠깐 새것으로(다른 시험과 안 섞이게)."""

    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix="ocrdual_")
        self.saved = (db.DB_PATH, OL.OCR_DIR, OL.OCR_LABELS_PAGE, OL.OCR_RATE_PER_MIN, OL.OCR_RATE_TENANT_PER_MIN)
        db.DB_PATH = os.path.join(self.d, "s.db")
        OL.OCR_DIR = os.path.join(self.d, "ocr")
        OL.OCR_RATE_PER_MIN = OL.OCR_RATE_TENANT_PER_MIN = 10 ** 6
        main.FV_TOKEN, main.FV_TENANT = TOK, "main"
        return self

    def __exit__(self, *e):
        db.DB_PATH, OL.OCR_DIR, OL.OCR_LABELS_PAGE, OL.OCR_RATE_PER_MIN, OL.OCR_RATE_TENANT_PER_MIN = self.saved
        shutil.rmtree(self.d, ignore_errors=True)


def img(seed, n=200):
    h = hashlib.sha256(str(seed).encode()).digest()
    return PNG + (h * (n // 32 + 1))[:n]


def b64(raw):
    return base64.b64encode(raw).decode()


def sub(site, raw, dh, pc="PC-DU", **kw):
    body = {"pc_id": pc, "site": site, "prompt": "p", "img_b64": b64(raw), "gemini_answer": "", "local_answer": "",
            "dhash": dh, "ts": 1790000000.0}
    body.update(kw)
    return C.post("/ocr/submit", json=body, headers={"X-Api-Key": KEY})


def FV(method, url, **kw):
    C.cookies.clear()
    return getattr(C, method)(url, headers={"X-FV-Token": TOK}, **kw)


def lab(cid, text):
    r = FV("post", "/api/fv/ocr/label", json={"id": cid, "text": text})     # 팜뷰 본문은 {id, text}
    assert r.status_code == 200, r.text
    return r


def ts_labels(since="0.0", **kw):
    p = {"since": since}
    p.update(kw)
    return C.get("/ocr/labels", params=p, headers={"X-Api-Key": KEY})


def rows(sql, args=()):
    c = sqlite3.connect(db.DB_PATH)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


# ─── D. 제출 두 형식 ─────────────────────────────────────────────────────────
def t_submit_dual():
    with _Store():
        r = sub("d_site", img("d1"), D64)
        ok("D-1 64자리(256비트) dhash → 200", r.status_code == 200, "%s %s" % (r.status_code, r.text[:120]))
        r2 = sub("d_site", img("d2"), "0f0f0f0f0f0f0f0f")
        ok("D-2 16자리(64비트) dhash 는 그대로 200", r2.status_code == 200, r2.text[:120])
        bits = dict(rows("SELECT dhash, dhash_bits FROM ocr_img"))
        ok("D-3 길이를 따로 적는다(dhash_bits 256·64)", bits.get(D64) == 256 and bits.get("0f0f0f0f0f0f0f0f") == 64, str(bits))
        bad = {"15자리": "0" * 15, "17자리": "0" * 17, "32자리": "0" * 32, "63자리": "0" * 63, "65자리": "0" * 65,
               "16진 아님": "g" * 64, "빈칸": "", "가운데 공백": "0" * 32 + " " + "0" * 31, "줄바꿈 꼬리": "0" * 64 + "\n0"}
        got = {k: sub("d_site", img("bad" + k), v).status_code for k, v in bad.items()}
        ok("D-4 16·64 가 아니면 400(길이·글자·꼬리)", set(got.values()) == {400}, str(got))
        r = sub("d_site", img("d-up"), D64.upper())
        ok("D-5 대문자 64자리는 소문자로 받아 200", r.status_code == 200 and
           rows("SELECT dhash FROM ocr_img WHERE id=?", (r.json()["id"],))[0][0] == D64, r.text[:120])
        ok("D-5b 같은 dhash(대문자로 왔어도) → 같은 묶음(near, 거리 0)",
           r.json()["dup"] == "near" and r.json()["near_dist"] == 0, r.text[:160])
        # 길이가 다르면 절대 안 묶인다 — 둘 다 0 이어도
        a = sub("mix", img("m1"), "0" * 16).json()
        b = sub("mix", img("m2"), "0" * 64).json()
        ok("D-6 16자리 0 과 64자리 0 은 다른 묶음(길이가 다르면 안 묶인다)",
           a["dup"] == "new" and b["dup"] == "new" and a["cluster"] != b["cluster"], "%s / %s" % (a, b))
        ok("D-6b hamming 길이 다르면 아주 먼 거리", OL.hamming("0" * 16, "0" * 64) >= 1 << 20 and OL.hamming("", "0") >= 1 << 20)
        # 256비트는 같은 값만 묶는다(한 자리 차이 4~5비트 — lc/ocr_label.py 머리)
        one = "1" + "0" * 63
        c1 = sub("n256", img("n1"), "0" * 64).json()
        c2 = sub("n256", img("n2"), one).json()
        c3 = sub("n256", img("n3"), "0" * 64).json()
        ok("D-7 256비트: 1비트만 달라도 새 묶음(OCR_NEAR_MAX_256=0)", c2["dup"] == "new" and c2["cluster"] != c1["cluster"], str(c2))
        ok("D-7b 256비트: 같은 dhash·다른 바이트 → 같은 묶음(near)", c3["dup"] == "near" and c3["cluster"] == c1["cluster"], str(c3))
        c4 = sub("n64", img("q1"), "0" * 16).json()
        c5 = sub("n64", img("q2"), "000000000000000f").json()
        ok("D-7c 64비트 문턱(4)은 그대로 — 4비트 차이는 near", c5["dup"] == "near" and c5["cluster"] == c4["cluster"], str(c5))
        # 매크로 칸
        raw = img("x1")
        s64 = b64(raw)
        r = sub("x.py:fn", raw, D64, prompt_sha1="abcdef012345", sha1=hashlib.sha1(s64.encode()).hexdigest(),
                phash="00ff00ff00ff00ff")
        row = rows("SELECT site, site_raw, prompt_sha1, csha1, phash, sha1 FROM ocr_img WHERE id=?", (r.json()["id"],))[0]
        ok("D-8 site 원문 저장(site_raw) · 저장 열쇠는 site_safe", row[0] == "x_py_fn" and row[1] == "x.py:fn", str(row))
        ok("D-8b 매크로 열쇠 저장(prompt_sha1·csha1=b64 문자열 sha1·phash) · 서버 sha1 은 바이트 sha1 그대로",
           row[2] == "abcdef012345" and row[3] == hashlib.sha1(s64.encode()).hexdigest() and row[4] == "00ff00ff00ff00ff"
           and row[5] == hashlib.sha1(raw).hexdigest(), str(row))
        r = sub("x_py_fn", raw, D64)
        ok("D-9 ★양쪽 표기★ 'x.py:fn' 으로 온 그림이 'x_py_fn' 으로 다시 와도 같은 줄(exact)",
           r.json()["dup"] == "exact" and r.json()["count"] == 2, r.text[:160])
        raw2 = img("x2")
        r = sub("y.py:g", raw2, D64, prompt_sha1="ZZ-not-hex", phash="short")
        row = rows("SELECT prompt_sha1, csha1, phash FROM ocr_img WHERE id=?", (r.json()["id"],))[0]
        ok("D-10 틀린 매크로 칸은 거부하지 않고 빈칸(200) · sha1 을 안 보내면 서버가 b64 문자열로 잰다",
           r.status_code == 200 and row[0] == "" and row[2] == "" and row[1] == hashlib.sha1(b64(raw2).encode()).hexdigest(),
           str(row))


# ─── T. ts 방식 ──────────────────────────────────────────────────────────────
def _msub(site, seed, pkey="a1b2c3d4e5f6", dh=D64):
    raw = img(seed)
    s = b64(raw)
    r = sub(site, raw, dh, prompt_sha1=pkey, sha1=hashlib.sha1(s.encode()).hexdigest(), phash="0123456789abcdef")
    return r.json(), hashlib.sha1(s.encode()).hexdigest()


def t_ts_mode():
    with _Store():
        j = ts_labels("0.0").json()
        ok("T-1 since=0.0 → ts 방식 {labels:[], mode:ts, since, more, epoch}",
           j.get("mode") == "ts" and j.get("labels") == [] and j.get("more") is False and "epoch" in j and j.get("since") == 0.0,
           str(j))
        x, csha = _msub("gem.py:read", "t1")
        j = ts_labels("0.0").json()
        ok("T-2 라벨 전(대기)은 안 나온다", j["labels"] == [], str(j))
        lab(x["cluster"], "1,234")
        j = ts_labels("0.0").json()
        r0 = j["labels"][0] if j["labels"] else {}
        ok("T-3 라벨 → 행 하나 {site 원문, prompt_sha1, sha1(매크로), label, ts, deleted:false, dhash, phash}",
           len(j["labels"]) == 1 and r0.get("site") == "gem.py:read" and r0.get("prompt_sha1") == "a1b2c3d4e5f6"
           and r0.get("sha1") == csha and r0.get("label") == "1,234" and r0.get("deleted") is False
           and r0.get("dhash") == D64 and r0.get("phash") == "0123456789abcdef" and r0.get("ts", 0) > 1e9, str(j))
        ok("T-3b 응답 since = 받은 최대 ts", j["since"] == r0.get("ts"), str(j))
        j2 = ts_labels(repr(r0["ts"])).json()
        ok("T-4 since=그 ts 로 다시 → 비었다(같은 행 두 번 안 온다)", j2["labels"] == [], str(j2))
        FV("post", "/api/fv/ocr/bad", json={"id": x["cluster"]})
        j3 = ts_labels(repr(r0["ts"])).json()
        ok("T-5 나쁨 → label 'bad image' · ts 가 더 크다",
           len(j3["labels"]) == 1 and j3["labels"][0]["label"] == "bad image" and j3["labels"][0]["ts"] > r0["ts"]
           and j3["labels"][0]["deleted"] is False, str(j3))
        FV("post", "/api/fv/ocr/undo", json={})
        FV("post", "/api/fv/ocr/undo", json={})
        j4 = ts_labels(repr(j3["labels"][0]["ts"])).json()
        ok("T-6 되돌려 대기로 → deleted:true 행(매크로가 열쇠를 지운다)",
           len(j4["labels"]) == 1 and j4["labels"][0]["deleted"] is True and j4["labels"][0]["sha1"] == csha, str(j4))
        # 대표만 — 같은 dhash 다른 바이트(구성원)는 라벨이 붙어도 행이 안 나간다(반증 R1)
        y, _ = _msub("gem.py:read", "t2")
        z, zsha = _msub("gem.py:read", "t3")
        ok("T-7 준비: 같은 dhash → 같은 묶음", z["cluster"] == y["cluster"] and z["dup"] == "near", str(z))
        lab(y["cluster"], "77")
        j5 = ts_labels(repr(j4["labels"][0]["ts"])).json()
        ok("T-7b ★묶음 대표 한 장만★ 행으로 나간다(구성원은 사람이 본 그림이 아니다)",
           len(j5["labels"]) == 1 and j5["labels"][0]["sha1"] != zsha, str(j5))
        # 매크로 열쇠 없는 옛 형식은 ts 방식에 안 나가고, 커서 방식엔 그대로
        o = sub("old_site", img("o1"), "1234123412341234").json()
        lab(o["cluster"], "옛")
        j6 = ts_labels("0.0").json()
        ok("T-8 매크로 열쇠(prompt_sha1·sha1) 없는 줄은 ts 행에 안 나간다", all(r["site"] != "old_site" for r in j6["labels"]),
           str(j6)[:200])
        jc = C.get("/ocr/labels", params={"since": 0}, headers={"X-Api-Key": KEY}).json()
        ok("T-8b 커서 방식(since=0 정수)은 옛 모양 그대로 {labels:{site:{sha1:label}}, cursor, epoch}",
           isinstance(jc.get("labels"), dict) and jc["labels"].get("old_site") and "cursor" in jc and "mode" not in jc, str(jc)[:200])
        jm = C.get("/ocr/labels", params={"since": 0, "mode": "ts"}, headers={"X-Api-Key": KEY}).json()
        ok("T-9 mode=ts 면 정수 since 도 ts 방식", jm.get("mode") == "ts" and isinstance(jm.get("labels"), list), str(jm)[:120])
        for bad in ("-0.5", "nan", "inf", "1e999", "1.2.3", "2e11"):
            rr = ts_labels(bad)
            ok("T-10 ts since=%s → 400" % bad, rr.status_code == 400, str(rr.status_code))
        # 옛 형식으로 먼저 와서 라벨 된 대표에 매크로가 같은 그림을 보내면 열쇠가 채워지고 행이 나간다
        raw = img("o1")
        s = b64(raw)
        before = rows("SELECT MAX(lts) FROM ocr_img")[0][0]    # 매크로 since 가 그 옛 라벨 시각을 이미 넘었다(가장 나쁜 쪽)
        rr = sub("old_site", raw, "1234123412341234", prompt_sha1="0badc0ffee00", sha1=hashlib.sha1(s.encode()).hexdigest())
        j7 = ts_labels(repr(before)).json()
        ok("T-11 옛 대표에 매크로 칸이 채워지면(exact) 그 라벨이 ts 행으로 나간다",
           rr.json()["dup"] == "exact" and len(j7["labels"]) == 1 and j7["labels"][0]["label"] == "옛"
           and j7["labels"][0]["prompt_sha1"] == "0badc0ffee00", str(j7))
        rr = sub("old_site", raw, "1234123412341234", prompt_sha1="111111111111", sha1="2" * 40)
        j8 = ts_labels(repr(j7["since"])).json()
        ok("T-11b 있던 열쇠는 안 바꾼다(다른 프롬프트로 같은 그림 — 처음 열쇠가 남는다)", j8["labels"] == [] and
           rows("SELECT prompt_sha1 FROM ocr_img WHERE id=?", (rr.json()["id"],))[0][0] == "0badc0ffee00", str(j8))


def t_ts_paging_and_clock():
    with _Store():
        OL.OCR_LABELS_PAGE = 2
        cs = []
        for i in range(3):
            x, _ = _msub("pg.py:f", "pg%d" % i, dh=("%064x" % (i + 1)))
            cs.append(x["cluster"])
        real = OL.time

        class _Frozen:                                   # ★시계가 멈춰도★ 라벨 ts 는 늘 커진다
            @staticmethod
            def time():
                return 1790000000.0

            def __getattr__(self, k):
                return getattr(real, k)
        OL.time = _Frozen()
        try:
            for c in cs:
                lab(c, "v%d" % c)
        finally:
            OL.time = real
        a = ts_labels("0.0").json()
        tss = [r["ts"] for r in a["labels"]]
        ok("T-12 한 쪽 2행 · more:true", len(a["labels"]) == 2 and a["more"] is True, str(a))
        ok("T-12b 시계가 멈춰도 ts 가 엄격히 증가(_next_lts)", len(tss) == 2 and tss[0] < tss[1], str(tss))
        b = ts_labels(repr(a["since"])).json()
        ok("T-12c 받은 since 로 이어 읽으면 나머지 1행 · more:false",
           len(b["labels"]) == 1 and b["more"] is False and b["labels"][0]["label"] == "v%d" % cs[2], str(b))
        # 같은 ts 가 쪽 경계에 걸리면 그 ts 는 다음 쪽으로 통째로(갈라지지 않게)
        rows_ = rows("SELECT id FROM ocr_img ORDER BY id")
        c = sqlite3.connect(db.DB_PATH)
        c.execute("UPDATE ocr_img SET lts=5.0 WHERE id IN (?,?)", (rows_[1][0], rows_[2][0]))
        c.execute("UPDATE ocr_img SET lts=4.0 WHERE id=?", (rows_[0][0],))
        c.commit()
        c.close()
        p1 = ts_labels("0.0").json()
        p2 = ts_labels(repr(p1["since"])).json()
        ok("T-13 쪽 경계의 같은 ts 는 안 갈라진다(1행 → 다음에 2행)",
           [r["ts"] for r in p1["labels"]] == [4.0] and [r["ts"] for r in p2["labels"]] == [5.0, 5.0], "%s / %s" % (p1, p2))
        OL.OCR_LABELS_PAGE = 1
        c = sqlite3.connect(db.DB_PATH)
        c.execute("UPDATE ocr_img SET lts=6.0")
        c.commit()
        c.close()
        p3 = ts_labels("0.0").json()
        ok("T-13b 한 ts 가 한 쪽보다 크면 그 ts 는 통째로(3행)", len(p3["labels"]) == 3 and p3["since"] == 6.0, str(p3)[:200])


# ─── M. 옛 표에 열 더하기 ────────────────────────────────────────────────────
def _old_schema():
    c = sqlite3.connect(db.DB_PATH)
    c.executescript("""
        CREATE TABLE ocr_img (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, site TEXT NOT NULL,
          sha1 TEXT NOT NULL, dhash TEXT NOT NULL, cluster_id INTEGER NOT NULL, pc_id TEXT, prompt TEXT, gemini TEXT,
          local TEXT, ts REAL, created REAL NOT NULL, last_seen REAL, count INTEGER NOT NULL DEFAULT 1,
          nbytes INTEGER NOT NULL DEFAULT 0, relpath TEXT, mime TEXT, on_disk INTEGER NOT NULL DEFAULT 1,
          seq INTEGER NOT NULL DEFAULT 0, UNIQUE(tenant, site, sha1));
        INSERT INTO ocr_img(tenant, site, sha1, dhash, cluster_id, created) VALUES('main','old','aa','0000000000000000',1,1.0);
    """)
    c.commit()
    c.close()


async def t_migrate():
    with _Store():
        _old_schema()
        await OL.ensure_tables()
        cols = {r[1] for r in rows("PRAGMA table_info(ocr_img)")}
        ok("M-1 옛 표에 열 여섯 더함(site_raw·prompt_sha1·csha1·phash·dhash_bits·lts)",
           {"site_raw", "prompt_sha1", "csha1", "phash", "dhash_bits", "lts"} <= cols, str(sorted(cols)))
        ok("M-2 옛 줄은 그대로(새 칸은 NULL)", rows("SELECT site, sha1, lts, prompt_sha1 FROM ocr_img") == [("old", "aa", None, None)],
           str(rows("SELECT * FROM ocr_img")))
        OL._INITED.discard(OL._dbp())
        await OL.ensure_tables()
        ok("M-3 두 번 돌려도 된다(있으면 건너뜀)", len({r[1] for r in rows("PRAGMA table_info(ocr_img)")}) == len(cols))
        ok("M-4 ts 인덱스", any(r[0] == "ix_ocr_img_lts" for r in rows("SELECT name FROM sqlite_master WHERE type='index'")))
    # ★M-5 옛 표 위 첫 요청 8개 동시 (2026-09-24 아이온2 반증: «duplicate column name» 500 하나씩)★
    with _Store():
        _old_schema()
        OL._INITED.discard(OL._dbp())
        res = await asyncio.gather(*[OL.ensure_tables() for _ in range(8)], return_exceptions=True)
        errs = [repr(e) for e in res if isinstance(e, BaseException)]
        ok("M-5 ★옛 표 위 ensure_tables 8개 동시 — 예외 0★", not errs, str(errs)[:300])
        ok("M-5b 그 뒤 열 여섯이 다 있다", {"site_raw", "prompt_sha1", "csha1", "phash", "dhash_bits", "lts"}
           <= {r[1] for r in rows("PRAGMA table_info(ocr_img)")})
    # M-6 잠금 밖(다른 프로세스 흉내)에서 겹쳐 더해도 «duplicate column» 은 삼킨다
    with _Store():
        _old_schema()
        res = await asyncio.gather(*[OL._ensure_tables_once(OL._dbp()) for _ in range(4)], return_exceptions=True)
        errs = [repr(e) for e in res if isinstance(e, BaseException)]
        ok("M-6 잠금 밖 4개 겹쳐도 «duplicate column» 로 안 죽는다", not any("duplicate" in e.lower() for e in errs), str(errs)[:300])


# ─── R. 매크로 코드 그대로 왕복 ──────────────────────────────────────────────
def _load_macro():
    p = os.path.join(LC, "ocr_label.py")
    if not os.path.exists(p):
        return None
    srv = sys.modules.get("ocr_label")
    sys.path.insert(0, LC)
    try:
        spec = importlib.util.spec_from_file_location("ocr_label", p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ocr_label"] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(LC)
        sys.modules["ocr_label"] = srv
    return mod


@contextlib.contextmanager
def _as_macro(M):
    """ocr_label_net._ol() 은 `import ocr_label` 로 짝을 찾는다 — 매크로 쪽 호출 동안만 매크로 모듈을 그 이름에 둔다."""
    srv = sys.modules["ocr_label"]
    sys.modules["ocr_label"] = M
    try:
        yield
    finally:
        sys.modules["ocr_label"] = srv


class _Shim:
    """ocr_label_net.requests 자리 — ★requests 가 실제로 만드는 URL 글자★ 를 그대로 TestClient 로."""

    def __init__(self):
        import requests as rq
        self.rq = rq
        self.calls = []

    class _R:
        def __init__(self, r):
            self.status_code, self._r = r.status_code, r

        def json(self):
            return self._r.json()

    def post(self, url, json=None, headers=None, timeout=None):
        u = urlsplit(url)
        r = C.post(u.path, json=json, headers=headers)
        self.calls.append(("POST", u.path, json, r))
        return self._R(r)

    def get(self, url, params=None, headers=None, timeout=None):
        full = self.rq.Request("GET", url, params=params).prepare().url
        u = urlsplit(full)
        r = C.get(u.path + ("?" + u.query if u.query else ""), headers=headers)
        self.calls.append(("GET", u.path + "?" + u.query, None, r))
        return self._R(r)


def _crop_b64(text):
    import cv2
    import numpy as np
    g = np.full((24, 120), 30, np.uint8)
    cv2.putText(g, text, (4, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.6, 230, 1, cv2.LINE_AA)
    okk, buf = cv2.imencode(".png", g)
    return base64.b64encode(buf.tobytes()).decode()


def macro_ocr(M, img_b64, prompt, answer):
    """gemini_ocr.ocr 흉내 — 매크로 훅 before/after 를 그대로. site = 'test_ocr_dual.py:macro_ocr'."""
    ctx = M.before(img_b64, prompt)
    if ctx.get("hit") is None:
        M.after(ctx, img_b64, prompt, answer, local_answer=None)
    return ctx


def t_macro_roundtrip():
    M = _load_macro()
    ok("R-0 매크로 lc/ocr_label.py 를 불러왔다(없으면 이 시험이 아무것도 안 본 것)", M is not None, LC)
    if M is None:
        return
    net = M._net
    with _Store() as st:
        shim = _Shim()
        saved = (net.requests, net.ensure_thread)
        # ★lc 3fea151 #216 제출 정책★ — 숫자 프롬프트는 로컬 불일치·1/20 표본만 올린다(정책 시험은 lc ocrsubmit216_test).
        #   이 시험은 «올린 한 장이 서버까지 가는 길» 이라 정책을 «올린다» 로 고정한다(없는 옛 lc 면 그대로).
        pol = getattr(M, "_policy", None)
        saved_pol = getattr(pol, "should_submit", None)
        if pol is not None:
            pol.should_submit = lambda *a, **k: True
        net.requests, net.ensure_thread = shim, (lambda: None)   # 데몬 스레드는 안 띄운다 — _send/poll_now 를 직접
        net._q.clear()
        net._sent.clear()
        M._labels.clear()
        M._st.update(since=0.0, loaded=False)
        LOG = []
        M.configure(server_url="http://srv.test", api_key=KEY, pc_id="PC-MR", labels_path=os.path.join(st.d, "labels.json"),
                    log=LOG.append)
        try:
            with _as_macro(M):
                im = _crop_b64("1,234/840")
                dh, ph = M.hashes(im)
                ok("R-1 매크로 hashes = dhash 64자리 · phash 16자리", len(dh) == 64 and len(ph) == 16, "%s %s" % (dh, ph))
                ctx = macro_ocr(M, im, "숫자만 읽어라", "1,234/840")
                ok("R-2 처음엔 적중 없음 · 큐에 한 장", ctx.get("hit") is None and len(net._q) == 1 and ctx["site"] ==
                   "test_ocr_dual.py:macro_ocr", str(ctx)[:200])
                item = net._q.popleft()
                sent = net._send(item)
                body = shim.calls[-1][2]
                resp = shim.calls[-1][3]
                ok("R-3 ★매크로 _send 그대로 → 서버 200★", sent is True and resp.status_code == 200,
                   "%s %s" % (resp.status_code, resp.text[:200]))
                ok("R-3b 보낸 본문 = 매크로 계약 칸 전부", {"site", "line", "prompt", "prompt_sha1", "img_b64", "sha1", "gemini_answer",
                                                   "local_answer", "dhash", "phash", "ts", "fn", "pc_id"} <= set(body), str(sorted(body)))
                rid = resp.json()["id"]
                row = rows("SELECT site, site_raw, prompt_sha1, csha1, dhash, dhash_bits, phash FROM ocr_img WHERE id=?", (rid,))[0]
                ok("R-4 서버가 매크로 열쇠를 그대로 적었다(site 원문·pkey·sha1(b64)·256비트)",
                   row[1] == ctx["site"] and row[2] == ctx["pkey"] and row[3] == ctx["sha1"] and row[4] == dh and row[5] == 256
                   and row[6] == ph and row[0] == "test_ocr_dual_py_macro_ocr", str(row))
                n0 = net.poll_now()
                q0 = shim.calls[-1][1]
                ok("R-5 라벨 전 조회: since=0.0 글자 → ts 방식 · 합친 행 0", n0 == 0 and parse_qs(urlsplit(q0).query).get("since") == ["0.0"],
                   "%s %s" % (n0, q0))
                cid = resp.json()["cluster"]
                ok("R-6 팜뷰에서 라벨", lab(cid, "1,234/840").status_code == 200)
                n1 = net.poll_now()
                ok("R-7 ★poll_now → _merge 1행★", n1 == 1, "%s %s" % (n1, shim.calls[-1][3].text[:300]))
                ctx2 = macro_ocr(M, im, "숫자만 읽어라", "틀린답")
                ok("R-8 ★같은 그림 다시 → 적중(제미나이 건너뜀) · 큐에 안 들어감★", ctx2.get("hit") == "1,234/840" and len(net._q) == 0,
                   str(ctx2)[:200])
                ok("R-8b 적중 로그", any("적중" in x for x in LOG), str(LOG[-3:]))
                n2 = net.poll_now()
                q2 = shim.calls[-1][1]
                ok("R-9 다시 조회 → 0행 · since 가 받은 ts 로 전진", n2 == 0 and float(parse_qs(urlsplit(q2).query)["since"][0]) > 1e9,
                   "%s %s" % (n2, q2))
                cache = json.load(open(os.path.join(st.d, "labels.json"), encoding="utf-8"))
                ok("R-10 매크로 캐시 파일에 그 행", len(cache.get("labels", [])) == 1 and cache["labels"][0]["label"] == "1,234/840",
                   str(cache)[:200])
                FV("post", "/api/fv/ocr/bad", json={"id": cid})
                net.poll_now()
                lk = M.lookup(ctx["site"], ctx["pkey"], ctx["sha1"])
                ok("R-11 나쁨 → 매크로 lookup kind=bad(제미나이 그대로)", lk["kind"] == "bad", str(lk))
                FV("post", "/api/fv/ocr/undo", json={})
                net.poll_now()
                ok("R-12 되돌리기(나쁨→라벨) → 다시 적중", M.lookup(ctx["site"], ctx["pkey"], ctx["sha1"])["kind"] == "hit")
                FV("post", "/api/fv/ocr/undo", json={})
                net.poll_now()
                lk = M.lookup(ctx["site"], ctx["pkey"], ctx["sha1"])
                ok("R-13 되돌리기(라벨→대기) → deleted 로 지워져 적중 없음", lk["kind"] in ("", "near") and (ctx["site"], ctx["pkey"], ctx["sha1"])
                   not in M._labels, str(lk))
                # 다른 프롬프트 = 다른 열쇠 — 같은 그림이어도 남의 라벨을 안 쓴다
                lab(cid, "1,234/840")
                net.poll_now()
                ctx3 = macro_ocr(M, im, "다른 프롬프트", "x")
                ok("R-14 같은 그림·다른 프롬프트는 적중 아님(열쇠에 prompt_sha1)", ctx3.get("hit") is None, str(ctx3)[:160])
                net._q.clear()
                im2 = _crop_b64("1,235/840")
                ctx4 = macro_ocr(M, im2, "숫자만 읽어라", "1,235/840")
                ok("R-15 한 글자 다른 그림은 적중 아님(sha1 정확 일치만)", ctx4.get("hit") is None, str(ctx4)[:160])
                net._q.clear()
        finally:
            net.requests, net.ensure_thread = saved
            if pol is not None:
                pol.should_submit = saved_pol
            net._q.clear()
            M.configure(server_url=None, api_key=None, pc_id=None, log=None)


# ─── A. 프롬프트 예시값 교체(매크로 1.1.1006) — 옛·새 prompt_sha1 둘 다 ───────────────
def t_alias():
    import re as _re
    src = os.path.normpath(os.path.join(HERE, "..", "..", "..", "SHARED_ISSUES_아이온2.md"))
    try:
        txt = open(src, encoding="utf-8").read()
    except OSError:
        txt = ""
    sec = txt.split("제미나이 프롬프트 예시값 교체", 1)[-1] if "제미나이 프롬프트 예시값 교체" in txt else ""
    doc = set()
    for m in _re.finditer(r"\|\s*((?:[0-9a-f]{12}\s*/\s*)*[0-9a-f]{12})\s*→\s*([0-9a-f]{12})\s*\|", sec):
        for o in _re.findall(r"[0-9a-f]{12}", m.group(1)):
            doc.add((o, m.group(2)))
    ok("A-1 서버 표 = SHARED_ISSUES_아이온2 표(옛→새 21쌍, 문서가 바뀌면 빨간불)", doc == set(OL.PROMPT_SHA1_OLD_NEW) and len(doc) == 21,
       "문서만: %s / 서버만: %s" % (sorted(doc - set(OL.PROMPT_SHA1_OLD_NEW)), sorted(set(OL.PROMPT_SHA1_OLD_NEW) - doc)))
    ok("A-2 새 키 62fd6da2e23d 는 옛 키 셋(1804·1808·1827) · 옛 키는 새 키 하나 · 모르는 키는 없음",
       OL.psha_aliases("62fd6da2e23d") == ["1f58164c266d", "44a66f2d988e", "8430599954af"]
       and OL.psha_aliases("1f58164c266d") == ["62fd6da2e23d"] and OL.psha_aliases("a1b2c3d4e5f6") == [] and OL.psha_aliases("") == [])
    with _Store():
        x, csha = _msub("info_collector.py:read_kina", "al1", pkey="1f58164c266d")
        lab(x["cluster"], "7,777")
        j = ts_labels("0.0").json()
        ks = sorted(r["prompt_sha1"] for r in j["labels"])
        ok("A-3 옛 키로 온 라벨 → 옛·새 두 행(같은 sha1·라벨·ts)", ks == ["1f58164c266d", "62fd6da2e23d"]
           and len({(r["sha1"], r["label"], r["ts"]) for r in j["labels"]}) == 1, str(j)[:300])
        y, _ = _msub("info_collector.py:read_kina", "al2", pkey="62fd6da2e23d", dh="f" * 64)
        lab(y["cluster"], "8,888")
        j2 = ts_labels(repr(j["since"])).json()
        ok("A-4 새 키(합쳐진 것)로 온 라벨 → 새 + 옛 셋 = 네 행",
           sorted(r["prompt_sha1"] for r in j2["labels"]) == ["1f58164c266d", "44a66f2d988e", "62fd6da2e23d", "8430599954af"],
           str(j2)[:300])
        ok("A-4b 응답 since 는 여전히 최대 ts(별칭 행이 커서를 흐리지 않는다)", j2["since"] == max(r["ts"] for r in j2["labels"]))
        FV("post", "/api/fv/ocr/undo", json={})
        j3 = ts_labels(repr(j2["since"])).json()
        ok("A-5 되돌림(deleted)도 별칭까지 네 행 다", len(j3["labels"]) == 4 and all(r["deleted"] for r in j3["labels"]), str(j3)[:300])
        M = _load_macro()
        if M is not None:
            M._labels.clear()
            M._st.update(since=0.0, loaded=True)
            M.configure(labels_path=os.path.join(tempfile.gettempdir(), "ocrdual_alias_labels.json"))
            with _as_macro(M):
                M.merge_rows(ts_labels("0.0").json()["labels"])
                hit_new = M.lookup("info_collector.py:read_kina", "62fd6da2e23d", csha)
                hit_old = M.lookup("info_collector.py:read_kina", "1f58164c266d", csha)
            ok("A-6 ★매크로 새 판이 옛 판 라벨로 적중★(열쇠 62fd…·같은 그림) · 옛 판도 그대로", hit_new["kind"] == "hit" and
               hit_new["label"] == "7,777" and hit_old["kind"] == "hit", "%s %s" % (hit_new, hit_old))
            try:
                os.remove(os.path.join(tempfile.gettempdir(), "ocrdual_alias_labels.json"))
            except OSError:
                pass
        else:
            ok("A-6 매크로 모듈 없음", False, LC)


def test_all():
    run_all([t_submit_dual, t_ts_mode, t_ts_paging_and_clock, t_migrate, t_macro_roundtrip, t_alias])
    finish("test_ocr_dual", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
