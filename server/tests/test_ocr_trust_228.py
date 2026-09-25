# -*- coding: utf-8 -*-
"""[대시보드] #228 (2026-09-25 주인님 «OCR 80개 찼다») — 믿을 만한 site 의 새 묶음 자동 닫기.

  사람 라벨 ≥ OCR_TRUST_MIN_LABELED(200) 이고 최근 OCR_TRUST_WINDOW(200) 장의 제미나이 답이 라벨과 전부 같은 site 는
  새 묶음을 status "auto"(label = 제미나이 답) 로 닫는다. OCR_TRUST_SPOT_EVERY(20) 번째마다 1건은 대기(표본 검사).
  표본에 사람이 다른 답을 주면 창이 깨끗해질 때까지 꺼진다. 다른 site·맵 이름 site 는 그대로.
  ★옛 코드에서는 T-2 부터 실패한다(auto 가 안 생긴다)★.

    cd updater/server && python -X utf8 tests/test_ocr_trust_228.py
"""
import base64
import hashlib
import time

import aiosqlite
from fastapi.testclient import TestClient

from _harness import main, db, ok, run_all, finish   # noqa: E402
import ocr_label as OL                                # noqa: E402

MIN_CHECKS = 23
KEY = "testkey"
C = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
SESS = main.new_session("main")
PNG = b"\x89PNG\r\n\x1a\n"
_N = [0]
KINA = "info_collector_py__read_server_kina_open"
KINA_RAW = "info_collector.py:_read_server_kina_open"


def img(seed):
    h = hashlib.sha256(str(seed).encode()).digest()
    return PNG + (h * 9)[:256]


def far(i):
    return hashlib.sha256(("t228-%d" % i).encode()).hexdigest()[:16]


def sub(gem, site=KINA_RAW, dh=None):
    _N[0] += 1
    body = {"pc_id": "PC-TR", "site": site, "prompt": "창고 키나 숫자", "img_b64": base64.b64encode(img("tr%d" % _N[0])).decode(),
            "gemini_answer": gem, "local_answer": "", "dhash": dh or far(_N[0]), "ts": 1790000000.0}
    r = C.post("/ocr/submit", json=body, headers={"X-Api-Key": KEY})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    return r.json()


def S(method, url, **kw):
    C.cookies.clear()
    C.cookies.set("session", SESS)
    try:
        return getattr(C, method)(url, **kw)
    finally:
        C.cookies.clear()


async def _st(cid):
    async with aiosqlite.connect(db.DB_PATH) as c:
        cur = await c.execute("SELECT status, label FROM ocr_cluster WHERE id=?", (cid,))
        return tuple(await cur.fetchone())


_T = [1000.0]


async def seed(site, n, bad_at=(), empty_at=(), multi_at=()):
    """사람 라벨 묶음 n 개(한 장씩)를 곧바로 넣는다. bad_at 의 번째(0=가장 옛것)는 제미나이 답이 라벨과 다르다.
    labeled_at 은 넣는 순서대로 늘어난다(나중에 넣은 것이 최근)."""
    await OL.ensure_tables()
    async with aiosqlite.connect(db.DB_PATH) as c:
        for k in range(n):
            _T[0] = max(_T[0] + 0.001, time.time())     # 사람 라벨(실제 시각)과 같은 시계 — 나중에 넣은 것이 최근
            _N[0] += 1
            lab = "%d" % (100000 + _N[0])
            gem = "" if k in empty_at else (lab if k not in bad_at else lab + "9")
            cur = await c.execute("INSERT INTO ocr_cluster(tenant, site, rep_dhash, status, label, qorder, created, labeled_at) "
                                  "VALUES('main', ?, ?, 'labeled', ?, ?, ?, ?)", (site, far(10**6 + _N[0]), lab, _T[0], _T[0], _T[0]))
            cid = cur.lastrowid
            cur = await c.execute("INSERT INTO ocr_img(tenant, site, sha1, dhash, cluster_id, gemini, created, nbytes, on_disk) "
                                  "VALUES('main', ?, ?, ?, ?, ?, ?, 0, 0)",
                                  (site, hashlib.sha1(b"s%d" % _N[0]).hexdigest(), far(10**6 + _N[0]), cid, gem, _T[0]))
            await c.execute("UPDATE ocr_cluster SET rep_img=? WHERE id=?", (cur.lastrowid, cid))
            if k in multi_at:                           # 같은 묶음에 한 장 더(같은 답)
                await c.execute("INSERT INTO ocr_img(tenant, site, sha1, dhash, cluster_id, gemini, created, nbytes, on_disk) "
                                "VALUES('main', ?, ?, ?, ?, ?, ?, 0, 0)",
                                (site, hashlib.sha1(b"m%d" % _N[0]).hexdigest(), far(10**6 + _N[0]), cid, gem, _T[0]))
        await c.commit()


async def t_threshold():
    await seed(KINA, 199)
    r = sub("123,456")
    ok("T-1 사람 라벨 199(하한 200 미만)면 새 묶음도 대기", r["status"] == "pending", str(r)[:120])
    await seed(KINA, 1)
    async with aiosqlite.connect(db.DB_PATH) as c:
        tr = await OL.site_trusted(c, "main", KINA)
    ok("T-1b 200 번째 라벨로 믿음(최근 200 장 불일치 0)", tr["trusted"] and tr["compared"] == 200 and tr["disagree"] == 0, str(tr))


async def t_spot_ratio():
    got = [sub("%d,000" % i) for i in range(40)]
    st = [x["status"] for x in got]
    ok("T-2 ★믿는 site 의 새 묶음 40 개 → auto 38 · 대기 2(20 번째마다 표본)★", st.count("auto") == 38 and st.count("pending") == 2
       and st[19] == "pending" and st[39] == "pending", str(st))
    first = got[0]
    ok("T-3 auto 라벨 = 제미나이 답 그대로", (await _st(first["cluster"])) == ("auto", "0,000"), str(await _st(first["cluster"])))
    q = S("get", "/ocr/queue", params={"limit": 100}).json()["items"]
    qid = {it["id"] for it in q}
    ok("T-4 auto 는 대기열에 없고 표본 2 개만 있다", not any(x["cluster"] in qid for x in got if x["status"] == "auto")
       and {got[19]["cluster"], got[39]["cluster"]} <= qid, str(sorted(qid))[:120])
    s = S("get", "/ocr/stats").json()["sites"][KINA]
    ok("T-5 stats 는 auto 를 따로 센다(38) · 불일치율 분모에는 안 넣는다(사람 라벨 200 그대로)",
       s["auto"] == 38 and s["labeled"] == 200 and s["gemini_compared"] == 200, str(s))


async def t_disagree_turns_off():
    spot = sub("777")                                   # 41 번째 — auto
    for _ in range(18):
        sub("1")
    spot = sub("888,888")                               # 60 번째 = 표본(대기)
    ok("T-6 60 번째는 표본으로 대기", spot["status"] == "pending", str(spot)[:120])
    lr = S("post", "/ocr/label", json={"id": spot["cluster"], "label": "888,889"})
    ok("T-6b 사람이 표본에 다른 답을 줬다", lr.status_code == 200, lr.text[:120])
    after = [sub("5,555")["status"] for _ in range(25)]
    ok("T-7 ★표본 하나가 다르면 그 site 는 꺼진다 — 다음 새 묶음은 전부 대기★", after == ["pending"] * 25, str(after))
    async with aiosqlite.connect(db.DB_PATH) as c:
        tr = await OL.site_trusted(c, "main", KINA)
    ok("T-7b 창 안 불일치 1", not tr["trusted"] and tr["disagree"] == 1, str(tr))
    await seed(KINA, 199)
    r = sub("42")
    ok("T-8 깨끗한 라벨이 199 장 더 와도 그 불일치가 아직 최근 200 안이면 계속 꺼짐", r["status"] == "pending", str(r)[:120])
    await seed(KINA, 1)
    r = sub("43")
    ok("T-9 ★창이 다시 깨끗해지면(불일치가 201 번째로 밀림) 다시 켜진다★", r["status"] == "auto", str(r)[:120])


async def t_other_sites():
    r = sub("123", site="sale.py:_sell_ocr_count")
    ok("T-10 라벨 없는 다른 site 는 그대로 대기", r["status"] == "pending", str(r)[:120])
    MAP = "analytics_py__map_loop"
    await seed(MAP, 200)
    r = sub("어딘가 새 이름", site="analytics.py:_map_loop")
    ok("T-11 맵 이름 site 는 라벨이 많아도 #218 규칙만(목록 밖 이름은 대기)", r["status"] == "pending", str(r)[:120])
    r = sub("   ")
    ok("T-12 믿는 site 여도 답이 비면 대기", r["status"] == "pending", str(r)[:120])
    # 근접 합류 — auto 묶음에 다른 답이 붙으면 대기로, 대기 묶음에 붙은 새 줄은 그 묶음을 안 닫는다
    for k in range(3):                                  # 표본(20 번째)에 걸리면 다음 것으로
        base = far(5000 + k)
        a = sub("9,999", dh=base)
        if a["status"] == "auto":
            break
    b = sub("9,998", dh=base[:-1] + ("1" if base[-1] != "1" else "0"))
    ok("T-13 auto 묶음에 다른 답이 붙으면 대기로", a["status"] == "auto" and a["cluster"] == b["cluster"]
       and (await _st(a["cluster"]))[0] == "pending", "%s %s %s" % (a["status"], b["cluster"], await _st(a["cluster"])))
    async with aiosqlite.connect(db.DB_PATH) as cc:
        cur = await cc.execute("SELECT v FROM ocr_meta WHERE k=?", ("trust_n:main:" + KINA,))
        v = (await cur.fetchone())
    ok("T-15 표본 셈은 DB(ocr_meta)에 — 재배포에도 20 번째가 이어진다", v is not None and int(v[0]) >= 60, str(v))


async def t_counts_apart():
    A = "t228_multi"
    await seed(A, 199, multi_at=(5,))
    async with aiosqlite.connect(db.DB_PATH) as c:
        tr = await OL.site_trusted(c, "main", A)
    ok("T-17 묶음 199 개에 비교 이미지 200 장이어도 ★사람 라벨 묶음 200 미만★ 이면 안 믿는다",
       not tr["trusted"] and tr["labeled"] == 199, str(tr))
    B = "t228_empty"
    await seed(B, 200, empty_at=tuple(range(10)))
    async with aiosqlite.connect(db.DB_PATH) as c:
        tr = await OL.site_trusted(c, "main", B)
    ok("T-18 라벨 묶음 200 이어도 ★답 있는 비교 이미지가 200 장 미만(190)★ 이면 안 믿는다",
       not tr["trusted"] and tr["labeled"] == 200 and tr["compared"] == 190, str(tr))


async def _age(c, site):
    await c.execute("UPDATE ocr_cluster SET labeled_at=labeled_at-1000, created=created-1000 WHERE site=?", (site,))
    await c.execute("UPDATE ocr_img SET created=created-1000 WHERE site=?", (site,))
    await c.commit()


async def t_window_arrival_and_bad():
    """반증 1·4 — 옛 라벨 묶음에 오늘 붙은 오독 · 표본을 나쁨으로 찍음 → 둘 다 꺼진다."""
    D = "t228_arrival"
    DR = "t228_arrival"                                  # site_safe 그대로
    await seed(D, 201)                                   # 201 — 옛 순서(라벨 시각)면 옛 묶음이 창 밖으로 밀린다
    async with aiosqlite.connect(db.DB_PATH) as c:
        cur = await c.execute("SELECT id, rep_dhash, label FROM ocr_cluster WHERE site=? ORDER BY labeled_at LIMIT 1", (D,))
        old_cid, old_dh, old_lb = await cur.fetchone()
        await _age(c, D)                                 # seed 시계가 실제 시각을 앞지를 수 있다 — 이 site 는 과거로
        await c.execute("UPDATE ocr_cluster SET labeled_at=labeled_at-100000 WHERE id=?", (old_cid,))   # 아주 옛 라벨
        await c.execute("UPDATE ocr_img SET created=created-100000 WHERE cluster_id=?", (old_cid,))
        await c.commit()
        tr0 = await OL.site_trusted(c, "main", D)
    ok("T-19 준비: 200 장 깨끗 → 믿음", tr0["trusted"], str(tr0))
    r = sub(old_lb + "7", site=DR, dh=old_dh[:-1] + ("1" if old_dh[-1] != "1" else "0"))
    async with aiosqlite.connect(db.DB_PATH) as c:
        tr = await OL.site_trusted(c, "main", D)
    ok("T-20 ★옛 라벨 묶음에 오늘 붙은 오독 이미지는 최근 창에 든다 → 꺼진다(옛: 옛 라벨 시각에 묻혀 계속 믿음)★",
       r["cluster"] == old_cid and not tr["trusted"] and tr["disagree"] == 1, "%s %s %s" % (r["cluster"], old_cid, tr))
    E = "t228_bad"
    await seed(E, 200)
    async with aiosqlite.connect(db.DB_PATH) as c:
        await _age(c, E)
        cur = await c.execute("INSERT INTO ocr_cluster(tenant, site, rep_dhash, status, qorder, created) VALUES('main', ?, ?, 'pending', ?, ?)",
                              (E, far(9999), time.time(), time.time()))
        bcid = cur.lastrowid
        await c.execute("INSERT INTO ocr_img(tenant, site, sha1, dhash, cluster_id, gemini, created, nbytes, on_disk) "
                        "VALUES('main', ?, ?, ?, ?, '31,337', ?, 0, 0)", (E, hashlib.sha1(b"bad228").hexdigest(), far(9999), bcid, time.time()))
        await c.commit()
    br = S("post", "/ocr/label", json={"id": bcid, "bad": True})
    async with aiosqlite.connect(db.DB_PATH) as c:
        tr = await OL.site_trusted(c, "main", E)
    ok("T-21 ★표본을 «잘못된 이미지» 로 찍었는데 제미나이가 답을 냈으면 불일치 — 꺼진다★",
       br.status_code == 200 and not tr["trusted"] and tr["disagree"] == 1, "%s %s" % (br.status_code, tr))


def t_consts():
    ok("T-16 문턱 = 주인님 지시(라벨 200 · 최근 200 · 20 중 1)",
       (OL.OCR_TRUST_MIN_LABELED, OL.OCR_TRUST_WINDOW, OL.OCR_TRUST_SPOT_EVERY) == (200, 200, 20), "")


def test_all():
    # 이 시험은 수백 장을 한 번에 낸다 — 속도 상한은 test_ocr_label 이 지킨다. ★모듈 수준에서 바꾸면 pytest 수집 때
    #   다른 파일까지 상한이 풀린다★ → 여기서만 올리고 되돌린다.
    keep = (OL.OCR_RATE_PER_MIN, OL.OCR_RATE_TENANT_PER_MIN)
    OL.OCR_RATE_PER_MIN = OL.OCR_RATE_TENANT_PER_MIN = 10 ** 6
    try:
        run_all([t_threshold, t_spot_ratio, t_disagree_turns_off, t_other_sites, t_counts_apart, t_window_arrival_and_bad, t_consts])
    finally:
        OL.OCR_RATE_PER_MIN, OL.OCR_RATE_TENANT_PER_MIN = keep
    finish("test_ocr_trust_228", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
