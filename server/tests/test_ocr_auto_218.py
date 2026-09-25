# -*- coding: utf-8 -*-
"""[대시보드] #218 (2026-09-25 주인님 «OCR 판별 — 홍옥의 섬 계속 올라오네») — 맵 이름 자동 닫기.

  site analytics_py__map_loop 에서 묶음의 ★모든★ 제미나이 답이 strip 뒤 알려진 16개 이름 중 하나와 글자 그대로 같으면
  status "auto"(라벨 = 그 이름) — 사람 대기열·기록·매크로 라벨(/ocr/labels)에 안 나간다. 그 밖은 예전처럼 pending.
  이미 대기 중인 것도 같은 규칙으로 쓴다(auto_sweep — 배포 뒤 첫 ensure_tables 에서 한 번). /ocr/stats 는 auto 를 따로 센다.
  ★옛 코드에서는 A-1 부터 실패한다(auto 가 없다)★.

    cd updater/server && python -X utf8 tests/test_ocr_auto_218.py
"""
import base64
import hashlib

import aiosqlite
from fastapi.testclient import TestClient

from _harness import main, db, ok, run_all, finish   # noqa: E402
import ocr_label as OL                                # noqa: E402

MIN_CHECKS = 24
KEY = "testkey"
C = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
SESS = main.new_session("main")
SITE_RAW = "analytics.py:_map_loop"
SITE = "analytics_py__map_loop"
PNG = b"\x89PNG\r\n\x1a\n"
_N = [0]


def img(seed):
    h = hashlib.sha256(str(seed).encode()).digest()
    return PNG + (h * 9)[:256]


def sub(gem, dh, site=SITE_RAW):
    _N[0] += 1
    body = {"pc_id": "PC-AU", "site": site, "prompt": "지역 이름", "img_b64": base64.b64encode(img("au%d" % _N[0])).decode(),
            "gemini_answer": gem, "local_answer": "", "dhash": dh, "ts": 1790000000.0,
            # 매크로 열쇠 둘 — 없으면 ts 방식 내보내기가 줄을 통째로 빼서 A-8b 가 아무것도 못 본다(반증 #4)
            "prompt_sha1": "a1b2c3d4e5f6", "sha1": hashlib.sha1(("k%d" % _N[0]).encode()).hexdigest()}
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


def q_ids():
    return {it["id"] for it in S("get", "/ocr/queue", params={"limit": 100}).json()["items"]}


def stats():
    return S("get", "/ocr/stats").json()["sites"].get(SITE, {})


async def _st(cid):
    async with aiosqlite.connect(db.DB_PATH) as c:
        cur = await c.execute("SELECT status, label FROM ocr_cluster WHERE id=?", (cid,))
        return tuple(await cur.fetchone())


async def _set(cid, status, label=None):
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE ocr_cluster SET status=?, label=? WHERE id=?", (status, label, cid))
        await c.commit()


# 거리 먼 dhash 들(서로 안 묶이게) — 한 비트씩만 다른 짝은 같은 묶음
def far(i):
    return hashlib.sha256(("far%d" % i).encode()).hexdigest()[:16]     # 서로 해밍 ~32 — near_max(4) 로 안 묶인다


async def t_submit():
    r = sub("홍옥의 섬", far(0))
    ok("A-1 ★알려진 이름 그대로면 status auto·라벨 그 이름 — 사람 대기열에 없다(옛: pending)★",
       r.get("status") == "auto" and r["cluster"] not in q_ids() and (await _st(r["cluster"])) == ("auto", "홍옥의 섬"),
       str(r))
    r2 = sub("  정령의 섬 \n", far(1))
    ok("A-2 앞뒤 공백·줄바꿈만 걷는다 → auto(정령의 섬)", (await _st(r2["cluster"])) == ("auto", "정령의 섬"), str(r2))
    bad = {}
    for i, g in enumerate(["홍옥의섬", "홍옥의 섬입니다", "", "홍옥의  섬", "Hongok", "홍옥의 섬 / 정령의 섬"]):
        rr = sub(g, far(10 + i))
        bad[g] = rr.get("status")
    ok("A-3 글자가 하나라도 다르면(띄어쓰기·꼬리·빈 답·두 이름) pending — 짐작해 맞추지 않는다",
       all(v == "pending" for v in bad.values()), str(bad))
    r3 = sub("홍옥의 섬", far(30), site="info_collector.py:_harvest")
    ok("A-4 다른 site 는 같은 답이어도 pending(맵 이름 site 만)", r3.get("status") == "pending", str(r3))
    # 근접 묶음 — auto 묶음에 다른 답이 붙으면 대기로 되돌린다
    base = far(40)
    ra = sub("영원의 섬", base)
    rb = sub("엘듄강 중류", base[:-1] + ("1" if base[-1] != "1" else "0"))
    ok("A-5 ★auto 묶음에 다른 이름이 붙으면 pending 으로 되돌린다(사람이 본다)★",
       ra["cluster"] == rb["cluster"] and (await _st(ra["cluster"]))[0] == "pending" and ra["cluster"] in q_ids(),
       "%s %s %s" % (ra["cluster"], rb["cluster"], await _st(ra["cluster"])))
    # 대기 묶음(모르는 답) + 알려진 이름 → 섞였으니 그대로 대기
    base = far(50)
    rc = sub("모르는 곳", base)
    rd = sub("홍옥의 섬", base[:-1] + ("1" if base[-1] != "1" else "0"))
    ok("A-6 모르는 답 묶음에 알려진 이름이 붙어도 섞였으면 대기", rc["cluster"] == rd["cluster"] and (await _st(rc["cluster"]))[0] == "pending",
       str(await _st(rc["cluster"])))
    # 같은 답끼리 붙으면 auto 유지
    base = far(60)
    re_ = sub("아울라우 부락", base)
    rf = sub("아울라우 부락", base[:-1] + ("1" if base[-1] != "1" else "0"))
    ok("A-7 같은 이름끼리 묶이면 auto 그대로", re_["cluster"] == rf["cluster"] and (await _st(re_["cluster"])) == ("auto", "아울라우 부락"),
       str(await _st(re_["cluster"])))


async def t_outputs():
    rl = sub("x", far(200), site="info_collector.py:_harvest")
    S("post", "/ocr/label", json={"id": rl["cluster"], "label": "사람 라벨"})
    lab = C.get("/ocr/labels", params={"since": 0}, headers={"X-Api-Key": KEY}).json()
    names = set()
    for v in (lab.get("labels") or {}).values():
        names |= set(v.values())
    for v in (lab.get("near") or {}).values():
        names |= set(v.values())
    ok("A-8 매크로 /ocr/labels 에 auto 라벨이 안 나간다(사람이 본 정답만)", not ({"홍옥의 섬", "정령의 섬"} & names), str(names)[:200])
    ts = C.get("/ocr/labels", params={"since": "0.0"}, headers={"X-Api-Key": KEY}).json()
    ok("A-8b ts 방식에도 auto 줄이 없다(열쇠 있는 줄이 실제로 나오는 판에서)",
       any(r.get("label") == "사람 라벨" for r in ts.get("labels") or []) and not any(r.get("status") == "auto" or r.get("label") in ("홍옥의 섬", "정령의 섬") for r in ts.get("labels") or []), str(ts)[:200])
    h = S("get", "/ocr/history", params={"limit": 200}).json()["items"]
    ok("A-9 기록(history)에 auto 가 없다", not any(it["status"] == "auto" for it in h), str([it["status"] for it in h]))
    st = stats()
    ok("A-10 ★/ocr/stats 가 auto 를 따로 센다★(auto 3: 홍옥·정령·아울라우 / pending 은 따로)",
       st.get("auto") == 3 and st.get("labeled") == 0, str(st))
    keep, main.FV_TOKEN = main.FV_TOKEN, "fv-218-token-0123456789abcdef"
    try:
        fv = C.get("/api/fv/ocr/stats", headers={"X-FV-Token": main.FV_TOKEN}).json()
    finally:
        main.FV_TOKEN = keep
    ok("A-10b FV stats 도 같은 값", ((fv.get("sites") or {}).get(SITE) or {}).get("auto") == 3, str(fv)[:200])


async def t_owner_and_sweep():
    r = sub("붉은 가시 왕관섬", far(70))
    cid = r["cluster"]
    lr = S("post", "/ocr/label", json={"id": cid, "label": "붉은 가시 왕관섬"})
    ok("A-11 주인님이 auto 묶음에 라벨을 주면 labeled(auto 는 막지 않는다)", lr.status_code == 200 and (await _st(cid))[0] == "labeled",
       "%s %s" % (lr.status_code, lr.text[:120]))
    ur = S("post", "/ocr/undo", json={})
    ok("A-11b ★반증: auto 묶음 라벨을 되돌리면 대기(맨 앞)로 — auto 로 돌아가 영영 숨지 않는다★",
       ur.status_code == 200 and ur.json().get("status") == "pending" and (await _st(cid))[0] == "pending"
       and sorted(q_ids()) and next(iter(it["id"] for it in S("get", "/ocr/queue", params={"limit": 100}).json()["items"])) == cid,
       "%s %s %s" % (ur.status_code, ur.text[:120], await _st(cid)))
    # 옛 데이터 흉내 — 이미 pending 으로 쌓인 맵 이름 묶음
    olds = [sub("드라나 가공구역", far(80 + i))["cluster"] for i in range(3)]
    mixed = sub("어딘가", far(90))["cluster"]
    for c in olds:
        await _set(c, "pending")
    # 사람이 한 번 손댔다가 되돌린 묶음(ocr_hist 있음)은 안 건드린다
    held = sub("영원의 섬", far(95))["cluster"]
    await _set(held, "pending")
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("INSERT INTO ocr_hist(tenant, cluster_id, prev_status, prev_label, new_status, new_label, at, undone) "
                        "VALUES('main', ?, 'pending', NULL, 'labeled', 'x', 1, 1)", (held,))
        await c.commit()
    dry = await OL.auto_sweep(dry=True)
    after_dry = [(await _st(c))[0] for c in olds]
    ok("A-12 쓸기 dry 는 셈만(3 닫힘 예정, 아무것도 안 바꿈)", dry["closed"] == 3 and dry["by_label"] == {"드라나 가공구역": 3}
       and after_dry == ["pending"] * 3, "%s %s" % (dry, after_dry))
    real = await OL.auto_sweep()
    ok("A-13 ★쓸기: 대기 중인 맵 이름 묶음을 auto 로 닫는다★", real["closed"] == 3 and [await _st(c) for c in olds] == [("auto", "드라나 가공구역")] * 3,
       str(real))
    ok("A-14 쓸기는 섞인 답·사람 손댄 묶음은 안 닫는다", (await _st(mixed))[0] == "pending" and (await _st(held))[0] == "pending",
       "%s %s" % (await _st(mixed), await _st(held)))
    b = far(95)
    near = sub("영원의 섬", b[:-1] + ("1" if b[-1] != "1" else "0"))
    ok("A-14b ★사람이 손댄 묶음은 알려진 이름이 새로 붙어도(제출 길) auto 로 안 닫는다★",
       near["cluster"] == held and (await _st(held))[0] == "pending", "%s %s %s" % (near["cluster"], held, await _st(held)))
    # 배포 뒤 첫 ensure_tables 가 한 번 쓴다
    c2 = sub("영원의 섬", far(100))["cluster"]
    await _set(c2, "pending")
    OL._INITED.discard(OL._dbp())
    await OL.ensure_tables()
    ok("A-15 ★배포(프로세스) 뒤 첫 ensure_tables 가 쓸기를 한 번 돈다★", (await _st(c2)) == ("auto", "영원의 섬"), str(await _st(c2)))


async def t_evict():
    r = sub("환영신의 정원", far(110))
    cid = r["cluster"]
    async with aiosqlite.connect(db.DB_PATH) as c:
        cur = await c.execute("SELECT id FROM ocr_img WHERE cluster_id=?", (cid,))
        iid = (await cur.fetchone())[0]
        used = await OL._disk_used(c, "main")
        keep = OL.OCR_DISK_CAP
        OL.OCR_DISK_CAP = used
        try:
            await OL.evict_for(c, "main", 10_000_000)
            await c.commit()
        finally:
            OL.OCR_DISK_CAP = keep
        cur = await c.execute("SELECT on_disk FROM ocr_img WHERE id=?", (iid,))
        od = (await cur.fetchone())[0]
    ok("A-16 디스크 비우기: auto 이미지 파일은 나쁨과 같은 층에서 지운다(줄·라벨은 남는다 — 안 쌓인다)",
       od == 0 and (await _st(cid)) == ("auto", "환영신의 정원"), "on_disk=%s %s" % (od, await _st(cid)))


def t_rule_unit():
    f = OL.auto_label
    ok("A-17 규칙: 모든 답이 같은 한 이름", f(SITE, ["홍옥의 섬", " 홍옥의 섬"]) == "홍옥의 섬", "")
    ok("A-18 규칙: 답이 없으면(빈 목록) None", f(SITE, []) is None, "")
    ok("A-19 규칙: None·숫자 답은 빈 답으로 — 하나라도 섞이면 None", f(SITE, ["홍옥의 섬", None]) is None, "")
    ok("A-20 규칙: 16개 이름 그대로(주인님 목록) · 다른 site 는 None",
       len(OL.OCR_AUTO_SITES[SITE]) == 16 and f("x", ["홍옥의 섬"]) is None and f(SITE, ["어비스 회랑"]) == "어비스 회랑", "")


def test_all():
    run_all([t_submit, t_outputs, t_owner_and_sweep, t_evict, t_rule_unit])
    finish("test_ocr_auto_218", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
