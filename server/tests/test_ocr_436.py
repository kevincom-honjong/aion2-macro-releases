# -*- coding: utf-8 -*-
"""[대시보드] #436 (2026-10-04 주인님 «OCR 쌓인 걸로 프로그램 개선, 쌓이게만 하지 말 것») — 프로그램 방 분석이 요구한 서버 변경 넷.

  (1) /ocr/history · /api/fv/ocr/history 에 offset 쪽매김(total · next_offset) — 라벨 전량을 훑어 분석할 수 있게
  (2) norm_answer 가 끝의 «%» 를 무시 — 진행도 «25» vs «25%» 를 제미나이 오답으로 세지 않는다
  (3) /ocr/stats 가 맵 이름 site 의 제미나이 답을 «매크로 snap 뒤» 값으로 비교(ocr_names = lc/map_names 사본, 시험이 같은지 지킨다)
  (4) 닫힌 목록 site(각성전 목표 3문구 · 난이도 7이름)도 정확히 맞으면 사람 대기열 대신 auto — 공백 무시, 정식 표기로 라벨

    cd updater/server && python -X utf8 tests/test_ocr_436.py
"""
import base64
import hashlib
import os
import re

import aiosqlite
from fastapi.testclient import TestClient

from _harness import main, db, ok, run_all, finish   # noqa: E402
import ocr_label as OL                                # noqa: E402
import ocr_names as ON                                # noqa: E402

MIN_CHECKS = 24
KEY = "testkey"
FVTOK = "fv436"
C = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
SESS = main.new_session("main")
PNG = b"\x89PNG\r\n\x1a\n"
_N = [0]
MAP_RAW, MAP = "analytics.py:_map_loop", "analytics_py__map_loop"
OBJ_RAW, OBJ = "awakening.py:_read_objective", "awakening_py__read_objective"
DIF_RAW, DIF = "awakening.py:_read_diff_label", "awakening_py__read_diff_label"


def far(i):
    return hashlib.sha256(("f436-%d" % i).encode()).hexdigest()[:16]


def sub(site_raw, gem, dh, local=""):
    _N[0] += 1
    h = hashlib.sha256(("i436%d" % _N[0]).encode()).digest()
    body = {"pc_id": "PC-436", "site": site_raw, "prompt": "p", "img_b64": base64.b64encode(PNG + (h * 9)[:256]).decode(),
            "gemini_answer": gem, "local_answer": local, "dhash": dh, "ts": 1790000000.0,
            "prompt_sha1": "a1b2c3d4e5f6", "sha1": hashlib.sha1(("k436%d" % _N[0]).encode()).hexdigest()}
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


def FV(method, url, **kw):
    main.FV_TOKEN = FVTOK
    main.FV_TENANT = "main"
    kw.setdefault("headers", {})["X-FV-Token"] = FVTOK
    return getattr(C, method)(url, **kw)


def stats(site):
    return S("get", "/ocr/stats").json()["sites"].get(site, {})


async def _st(cid):
    async with aiosqlite.connect(db.DB_PATH) as c:
        cur = await c.execute("SELECT status, label FROM ocr_cluster WHERE id=?", (cid,))
        return tuple(await cur.fetchone())


def t_norm():
    n = OL.norm_answer
    ok("N-1 ★끝의 % 무시: «25» == «25%» == « 25 % » == «25%%»★", n("25") == n("25%") == n(" 25 % ") == n("25%%") == "25",
       str([n("25"), n("25%"), n(" 25 % ")]))
    ok("N-2 가운데 %·다른 숫자는 구별", n("2%5") != n("25") and n("25%") != n("26%"))
    ok("N-3 전각 ％ 도(NFKC) · 빈/None 은 그대로 빈", n("25％") == "25" and n("") == "" and n(None) == "" and n("%") == "")
    ok("N-4 예전 규칙 그대로: 공백·대소문자·전각", n(" ＡＢ c ") == "abc")


def t_stats_pct():
    r = sub("info_collector.py:_progress", "25%", far(1))
    S("post", "/ocr/label", json={"id": r["cluster"], "label": "25"})
    r2 = sub("info_collector.py:_progress", "26%", far(2))
    S("post", "/ocr/label", json={"id": r2["cluster"], "label": "25"})
    st = stats(OL.site_safe("info_collector.py:_progress"))
    ok("P-1 ★«25%» vs 라벨 25 는 일치 · «26%» 는 불일치 → compared 2 · disagree 1★",
       (st.get("gemini_compared"), st.get("gemini_disagree")) == (2, 1), str(st)[:300])


def t_stats_snap():
    a = sub(MAP_RAW, "정령의 성", far(10))          # 한 글자 오독 — 매크로는 «정령의 섬» 으로 snap
    S("post", "/ocr/label", json={"id": a["cluster"], "label": "정령의 섬"})
    b = sub(MAP_RAW, "아무말", far(11))
    S("post", "/ocr/label", json={"id": b["cluster"], "label": "정령의 섬"})
    c = sub(MAP_RAW, "남쪽 갈라진 추락지", far(12))   # 목록과 멀다 → snap 안 됨 → 불일치
    S("post", "/ocr/label", json={"id": c["cluster"], "label": "갈라진 남쪽 추락지"})
    st = stats(MAP)
    ok("S-1 ★맵 이름: 한 글자 오독은 snap 뒤 비교라 일치 · 먼 오독은 불일치 → compared 3 · disagree 2★",
       (st.get("gemini_compared"), st.get("gemini_disagree")) == (3, 2), str(st)[:300])
    ok("S-2 snap 은 맵 이름 site 에서만(다른 site 의 같은 오독은 불일치)", OL._snap_names.snap("정령의 성") == "정령의 섬")
    d = sub("info_collector.py:_other", "정령의 성", far(13))
    S("post", "/ocr/label", json={"id": d["cluster"], "label": "정령의 섬"})
    st2 = stats(OL.site_safe("info_collector.py:_other"))
    ok("S-3 다른 site 는 원문 비교 그대로(불일치 1)", (st2.get("gemini_compared"), st2.get("gemini_disagree")) == (1, 1), str(st2)[:200])


def t_names_sync():
    ok("S-4 서버 사본 = 17개 이름, 자동 닫기 16개는 그 부분집합", len(ON.MAP_NAMES) == 17 and set(OL.OCR_AUTO_SITES[MAP]) <= set(ON.MAP_NAMES))
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "lc", "map_names.py")
    if os.path.exists(src):
        txt = open(src, encoding="utf8").read()
        names = tuple(re.findall(r'"([^"]+)"', txt[txt.index("MAP_NAMES = ("):txt.index(")\n", txt.index("MAP_NAMES = ("))]))
        ok("S-5 ★lc/map_names.py 의 목록과 같다(사본이 어긋나면 불일치율이 거짓)★", names == ON.MAP_NAMES, str(set(names) ^ set(ON.MAP_NAMES)))
        ns = {}
        exec(compile(txt, src, "exec"), ns)
        probes = ["정령의 성", "베르테론 요세 폐허", "루쓰레인 구릉지", "갈라진 추락지", "", "홍옥의섬", "xyz"]
        ok("S-6 snap 결과가 lc 와 같다", all(ns["snap"](p) == ON.snap(p) for p in probes), str([(p, ns["snap"](p), ON.snap(p)) for p in probes]))
    else:
        ok("S-5 (lc 없음 — 건너뜀)", True)
        ok("S-6 (lc 없음 — 건너뜀)", True)


async def t_auto():
    r = sub(OBJ_RAW, "방 안의 몬스터를 모두 처치", far(20))
    ok("A-1 ★각성전 목표: 공백이 달라도 3문구 중 하나면 auto · 라벨은 정식 표기★",
       (await _st(r["cluster"])) == ("auto", "방안의몬스터를모두처치"), str(r))
    r2 = sub(OBJ_RAW, "보스를처치하기", far(21))
    ok("A-2 공백 없는 답도 auto", (await _st(r2["cluster"]))[0] == "auto")
    bad = {}
    for i, g in enumerate(["텍스트 없음", "", "보스를 처치하기 시작", "방안의몬스터를모두처치 / 보스를처치하기", "다음 방의 입구를 열어"]):
        bad[g] = sub(OBJ_RAW, g, far(30 + i)).get("status")
    ok("A-3 목록 밖·빈 답·«텍스트 없음»·두 문구는 예전처럼 pending", all(v == "pending" for v in bad.values()), str(bad))
    d = sub(DIF_RAW, "극한", far(40))
    ok("A-4 난이도 라벨: 7이름 정확히 맞으면 auto", (await _st(d["cluster"])) == ("auto", "극한"), str(d))
    d2 = sub(DIF_RAW, "극한▼", far(41))
    ok("A-5 «극한▼» 같은 꼬리는 pending(사람이 본다)", d2.get("status") == "pending", str(d2))
    # 근접 묶음: auto 묶음에 다른 답이 붙으면 대기로 되돌린다(기존 규칙)
    near = "%016x" % (int(far(20), 16) ^ 1)
    n2 = sub(OBJ_RAW, "엉뚱한 문구", near)
    ok("A-6 ★near-dup 규칙 유지: auto 묶음에 다른 답이 붙으면 같은 묶음이 pending 으로★",
       n2["cluster"] == r["cluster"] and (await _st(r["cluster"]))[0] == "pending", "%s %s" % (n2, await _st(r["cluster"])))
    ok("A-7 규칙(단위): 두 사이트 이름 수 3 · 7 / 공백 무시는 이 둘뿐(맵은 예전처럼 글자 그대로)",
       len(OL.OCR_AUTO_SITES[OBJ]) == 3 and len(OL.OCR_AUTO_SITES[DIF]) == 7 and OL.auto_label(MAP, ["홍옥의섬"]) is None
       and OL.auto_label(OBJ, ["보스를 처치하기"]) == "보스를처치하기")


def t_history():
    ids = []
    for i in range(5):
        r = sub("hist436", "x%d" % i, far(60 + i))
        S("post", "/ocr/label", json={"id": r["cluster"], "label": "L%d" % i})
        ids.append(r["cluster"])
    pages, off, guard = [], 0, 0
    while off is not None and guard < 20:
        pg = S("get", "/ocr/history", params={"limit": 3, "offset": off}).json()
        pages.append(pg)
        off = pg["next_offset"]
        guard += 1
    seen = [x["id"] for pg in pages for x in pg["items"]]
    tot = pages[0]["total"]
    ok("H-1 ★offset 쪽매김: limit 3 으로 끝까지 훑으면 전부 한 번씩(중복 0), total 과 같다★",
       len(seen) == len(set(seen)) == tot and set(ids) <= set(seen) and tot >= 5 and len(pages) >= 2, "%s tot=%s pages=%d" % (seen, tot, len(pages)))
    ok("H-2 next_offset: 중간은 이어서 읽을 자리 · 끝은 null · total 은 쪽마다 같다",
       pages[0]["next_offset"] == 3 and pages[-1]["next_offset"] is None and len({pg["total"] for pg in pages}) == 1,
       str([pg["next_offset"] for pg in pages]))
    p2 = S("get", "/ocr/history", params={"limit": 3, "offset": 3}).json()
    ok("H-3 예전 키 items 그대로 · offset 생략 = 0", S("get", "/ocr/history").json()["offset"] == 0)
    big = S("get", "/ocr/history", params={"limit": 100000}).json()
    ok("H-4 limit 상한은 OCR_HISTORY_MAX(500)", len(big["items"]) <= OL.OCR_HISTORY_MAX)
    ok("H-5 화면은 틀린 offset 을 0 으로 · 팜뷰는 400",
       S("get", "/ocr/history", params={"offset": "abc"}).json()["offset"] == 0
       and FV("get", "/api/fv/ocr/history", params={"offset": "-1"}).status_code == 400
       and FV("get", "/api/fv/ocr/history", params={"offset": "x"}).status_code == 400)
    f = FV("get", "/api/fv/ocr/history", params={"limit": 3, "offset": 3}).json()
    ok("H-6 팜뷰 history 도 같은 쪽 · total/next_offset", [x["id"] for x in f["items"]] == [x["id"] for x in p2["items"]] and "total" in f and "next_offset" in f)


def test_all():
    run_all([t_norm, t_stats_pct, t_stats_snap, t_names_sync, t_auto, t_history])
    finish("test_ocr_436", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
