# -*- coding: utf-8 -*-
"""[대시보드] OCR 라벨링(2026-09-23 · 장부 #108) — ★실제 라우트로 부른다(TestClient)★.

라우터가 main.py 에 안 붙어 있으면(= 옛 코드) 모든 라우트 검사가 404 로 FAIL 한다.
  C1 캡차 거부(대소문자·전각·앞공백·__·한글) · 거부된 것은 한 바이트도 안 남는다
  A1 인증 — 매크로 키(submit·labels) / 세션(화면·queue·img·label·skip·undo·history·stats)
  D1 정확 중복 → count · N1 비슷한 것(dhash ≤4) → 한 묶음 ×N, 라벨 하나가 전부에
  K1 한글 라벨 왕복("아트라니아 협곡") · 파일 이름에 라벨·한글 0
  B1 잘못된 이미지 · U1 되돌리기(대기로 → 맨 앞 · 고치기 되돌리기) · Q1 나중에 순서
  Z1 크기 상한 413(디코드 뒤 · 디코드 전 base64 길이 · Content-Length) · R1 속도 상한 429
  E1 디스크 상한: 대기 묶음만 오래된 것부터 · 나쁨은 파일만 · 라벨 된 것은 HARD_CAP 에서만(줄은 영구)
  S1 통계 불일치율 · I1 since 커서 증분 · T1 테넌트 격리 · P1 경로 순회 · J1 화면 JS(node·check_js·키 처리)
  F1 팜뷰 /api/fv/ocr/* (장부 #125) — 같은 저장소: 팜뷰 라벨↔웹 기록 · 팜뷰 되돌리기가 웹 라벨을 · 토큰 404/401 ·
     FV_TENANT 밖은 안 보임 · 빈 text 400 · 대기 아닌 skip 409 · 에러 {error,code}
  RF1~RF10 · M1~M2 — 반증 시험(test_refute_ocr.py R1~R10) 이식(2026-09-23 밤): labels 는 대표만(near 는 힌트) ·
     DB epoch → reset · 되돌린 묶음은 비우기가 안 지운다 · 64비트 밖 id 400/404 · 캡차 어디든 · 테넌트 폴더 해시 ·
     테넌트별 비우기·507 · 속도 키 정규화·테넌트 합 · 청크 본문 상한 · cleared_near · 보이지 않는 라벨 400 · 비운 파일 되살리기

    cd updater/server && python -X utf8 tests/test_ocr_label.py
"""
import base64
import hashlib
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import main, db, ok, run_all, finish   # noqa: E402

import ocr_label as OL                                # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

MIN_CHECKS = 268     # 2026-09-24 v4 델타 반증 +3(V2-f~h 상한 제출은 비우기 안 돌림) · v4 반증 2차 +5(V2 묶음 줄 상한) · 2026-09-23 (밤) 반증 R1~R10 이식 뒤 실측 260 — 검사를 더하면 같이 올린다

KEY = "testkey"
KEY2 = "ocrkey2"       # 두 번째 테넌트(격리 시험) — 헤더라 ASCII
C = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
SESS = main.new_session("main")
PNG = b"\x89PNG\r\n\x1a\n"
JPG = b"\xff\xd8\xff\xe0"


def img(seed, n=256, head=PNG):
    """매직만 맞는 이미지 바이트(내용은 seed 로 달라진다)."""
    h = hashlib.sha256(str(seed).encode()).digest()
    return head + (h * (n // 32 + 1))[:n]


def b64(raw):
    return base64.b64encode(raw).decode()


def sub(site, seed, dh="0000000000000000", pc="PC-OT", key=KEY, raw=None, **kw):
    body = {"pc_id": pc, "site": site, "prompt": "이 글자를 읽어라", "img_b64": b64(raw if raw is not None else img(seed)),
            "gemini_answer": kw.pop("gem", ""), "local_answer": kw.pop("loc", ""), "dhash": dh, "ts": 1790000000.0}
    body.update(kw)
    return C.post("/ocr/submit", json=body, headers={"X-Api-Key": key} if key else {})


def S(method, url, sess=None, **kw):
    """세션 요청(쿠키는 요청마다 새로 — 다른 테넌트 세션과 안 섞이게)."""
    C.cookies.clear()
    if sess is not False:
        C.cookies.set("session", sess or SESS)
    try:
        return getattr(C, method)(url, **kw)
    finally:
        C.cookies.clear()


_EP: dict = {}         # 키 → 서버가 마지막으로 준 epoch (매크로가 cursor 와 같이 저장하는 값)


def labels(since=0, key=KEY, epoch=None):
    """매크로처럼 부른다 — epoch 를 안 주면 그 키로 마지막에 받은 epoch 를 같이 보낸다(since>0 일 때).
    epoch="" 은 「안 보낸다」(옛 매크로)."""
    params = {"since": since}
    ep = _EP.get(key, "") if epoch is None else epoch
    if ep and since:
        params["epoch"] = ep
    r = C.get("/ocr/labels", params=params, headers={"X-Api-Key": key})
    try:
        j = r.json()
        if isinstance(j, dict) and j.get("epoch"):
            _EP[key] = j["epoch"]
    except Exception:
        pass
    return r


def queue(sess=None):
    return S("get", "/ocr/queue", sess=sess, params={"limit": 100}).json()


def q_ids(sess=None):
    return [it["id"] for it in queue(sess)["items"]]


def files_under(root):
    out = []
    for d, _, fs in os.walk(root):
        out += [os.path.join(d, f) for f in fs]
    return out


class fresh_store:
    """★DB·OCR 폴더를 잠깐 새것으로★ — 디스크 상한·통계처럼 다른 시험의 줄이 섞이면 안 되는 것용."""

    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix="ocrfresh_")
        self.saved = (db.DB_PATH, OL.OCR_DIR, OL.OCR_DISK_CAP, OL.OCR_DISK_HARD_CAP)
        db.DB_PATH = os.path.join(self.d, "f.db")
        OL.OCR_DIR = os.path.join(self.d, "ocr")
        return self

    def __exit__(self, *e):
        db.DB_PATH, OL.OCR_DIR, OL.OCR_DISK_CAP, OL.OCR_DISK_HARD_CAP = self.saved


# ───────────────── A1 — 인증 · 라우트가 붙어 있다 ─────────────────

def t_auth():
    r = sub("map", 1, key=None)
    ok("A1-a /ocr/submit 키 없으면 403(라우트 있음 — 404 아님)", r.status_code == 403, str(r.status_code))
    r = sub("map", 1, key="wrongkey")
    ok("A1-b /ocr/submit 틀린 키 403", r.status_code == 403, str(r.status_code))
    r = C.get("/ocr/labels")
    ok("A1-c /ocr/labels 키 없으면 403", r.status_code == 403, str(r.status_code))
    r = S("post", "/ocr/submit", json={"pc_id": "PC-OT", "site": "map", "img_b64": b64(img(1)), "dhash": "0" * 16})
    ok("A1-d /ocr/submit 은 세션으로는 안 된다(403)", r.status_code == 403, str(r.status_code))
    r = S("get", "/ocr/label", sess=False)
    ok("A1-e 화면 /ocr/label 로그인 안 했으면 /login 으로", r.status_code in (302, 307) and r.headers.get("location") == "/login",
       "%s %s" % (r.status_code, r.headers.get("location")))
    r = S("get", "/ocr/label")
    ok("A1-f 화면 /ocr/label 세션이면 200 html · no-store",
       r.status_code == 200 and "text/html" in r.headers.get("content-type", "") and 'id="ans"' in r.text
       and "no-store" in r.headers.get("cache-control", ""), str(r.status_code))
    for method, url, kw in (("get", "/ocr/queue", {}), ("get", "/ocr/img/1", {}), ("post", "/ocr/label", {"json": {"id": 1, "label": "x"}}),
                            ("post", "/ocr/skip", {"json": {"id": 1}}), ("post", "/ocr/undo", {"json": {}}),
                            ("get", "/ocr/history", {}), ("get", "/ocr/stats", {})):
        r = S(method, url, sess=False, **kw)
        ok("A1-g %s %s 세션 없으면 401" % (method.upper(), url), r.status_code == 401, str(r.status_code))
    r = C.get("/ocr/queue", headers={"X-Api-Key": KEY})
    ok("A1-h /ocr/queue 는 매크로 키로는 안 된다(401)", r.status_code == 401, str(r.status_code))
    r = S("get", "/ocr/queue", sess="forged.token")
    ok("A1-i 위조 세션 401", r.status_code == 401, str(r.status_code))


# ───────────────── C1 — 캡차 ─────────────────

def t_captcha():
    before_files = len(files_under(OL.OCR_DIR))
    before_q = queue()["pending"]
    for site in ("captcha", "CAPTCHA_login", "Captcha-x", "  captcha", "ｃａｐｔｃｈａ", "__captcha", "캡차", "캡챠_입력", "captcha/../map"):
        r = sub(site, "cap" + site, pc="PC-CAP")
        ok("C1-a 캡차 site %r → 400" % site, r.status_code == 400 and "캡차" in r.json().get("detail", ""), "%s %s" % (r.status_code, r.text[:60]))
    ok("C1-b 거부된 캡차는 파일도 줄도 안 남는다", len(files_under(OL.OCR_DIR)) == before_files and queue()["pending"] == before_q)
    r = sub("map_capital", "notcap", pc="PC-CAP")
    ok("C1-c captcha 로 시작하지 않는 site 는 받는다", r.status_code == 200, r.text[:80])


# ───────────────── V1 — 입력 검증 ─────────────────

def t_validate():
    r = sub("map", "v1", raw=b"GIF89a" + b"x" * 50)
    ok("V1-a PNG/JPEG 가 아니면 400", r.status_code == 400, r.text[:80])
    r = C.post("/ocr/submit", json={"pc_id": "PC-V", "site": "map", "img_b64": "!!!notb64", "dhash": "0" * 16},
               headers={"X-Api-Key": KEY})
    ok("V1-b base64 가 아니면 400", r.status_code == 400, r.text[:80])
    for dh in ("xyz", "0" * 15, "0" * 17, 12345):
        r = sub("map", "v1d", dh=dh, pc="PC-V")
        ok("V1-c dhash %r → 400" % (dh,), r.status_code == 400, r.text[:60])
    r = sub("map", "v1p", pc="")
    ok("V1-d pc_id 없으면 400", r.status_code == 400, r.text[:60])
    r = C.post("/ocr/submit", json=[1, 2], headers={"X-Api-Key": KEY})
    ok("V1-e 본문이 목록이면 400(500 아님)", r.status_code == 400, r.text[:60])
    r = C.post("/ocr/submit", content=b"pc_id=PC&site=map", headers={"X-Api-Key": KEY, "Content-Type": "application/x-www-form-urlencoded"})
    ok("V1-f JSON 이 아니면 400", r.status_code == 400, r.text[:60])
    r = sub("jpgsite", "jpg1", raw=img("jpg1", head=JPG), pc="PC-V")
    ok("V1-g JPEG 는 받는다", r.status_code == 200, r.text[:80])
    g = S("get", "/ocr/img/%d" % r.json()["id"])
    ok("V1-h JPEG 는 image/jpeg 로 되돌려준다 · 바이트 그대로",
       g.status_code == 200 and g.headers.get("content-type", "").startswith("image/jpeg") and g.content == img("jpg1", head=JPG),
       "%s %s" % (g.status_code, g.headers.get("content-type")))
    r = sub("map", "v1u", **{"img_b64": "data:image/png;base64," + b64(img("v1u"))})
    ok("V1-i data: URL 앞머리는 떼고 받는다", r.status_code == 200, r.text[:80])


# ───────────────── D1 · N1 · K1 — 중복 · 묶음 · 한글 왕복 ─────────────────

def t_dedup_exact():
    p0 = queue()["pending"]
    r1 = sub("dedup", "same", dh="1111111111111111", pc="PC-D1").json()
    r2 = sub("dedup", "same", dh="1111111111111111", pc="PC-D1").json()
    r3 = sub("dedup", "same", dh="1111111111111111", pc="PC-D2").json()
    ok("D1-a 첫 장 new · 다음은 exact · 같은 줄", (r1["dup"], r2["dup"], r3["dup"]) == ("new", "exact", "exact")
       and r1["id"] == r2["id"] == r3["id"], str((r1, r3)))
    ok("D1-b count 가 3", r3["count"] == 3, str(r3))
    ok("D1-c 대기열에는 하나만 늘었다", queue()["pending"] == p0 + 1)
    it = next(x for x in queue()["items"] if x["id"] == r1["cluster"])
    ok("D1-d 대기열 항목이 ×3", it["count"] == 3 and it["members"] == 1, str(it))
    ok("D1-e 파일은 한 장", len([f for f in files_under(OL.OCR_DIR) if os.path.basename(f).startswith(hashlib.sha1(img("same")).hexdigest())]) == 1)
    S("post", "/ocr/label", json={"id": r1["cluster"], "label": "중복"})
    r4 = sub("dedup", "same", dh="1111111111111111", pc="PC-D1").json()
    ok("D1-f 라벨 된 뒤 같은 이미지 → count 만 올리고 라벨을 바로 돌려준다",
       r4["dup"] == "exact" and r4["count"] == 4 and r4["status"] == "labeled" and r4["label"] == "중복", str(r4))


KR = "아트라니아 협곡"


def t_near_cluster_and_korean():
    base = sub("near", "n0", dh="00000000000000ff", pc="PC-N").json()
    n1 = sub("near", "n1", dh="00000000000000fe", pc="PC-N").json()      # 거리 1
    n2 = sub("near", "n2", dh="0000000000000fff", pc="PC-N").json()      # 거리 4
    far = sub("near", "n3", dh="ffffffff00000000", pc="PC-N").json()     # 멀다
    n5 = sub("near", "n5", dh="000000000000001f", pc="PC-N").json()      # 거리 3 (ff→1f)
    ok("N1-a 거리 1·4·3 은 같은 묶음(near), 먼 것은 새 묶음",
       (n1["dup"], n2["dup"], n5["dup"], far["dup"]) == ("near", "near", "near", "new")
       and n1["cluster"] == n2["cluster"] == n5["cluster"] == base["cluster"] != far["cluster"],
       str([(x["dup"], x["cluster"], x["near_dist"]) for x in (n1, n2, n5, far)]))
    ok("N1-b 거리 5 는 안 붙는다", sub("near", "n6", dh="0000000000001fff", pc="PC-N").json()["dup"] == "new")
    it = next(x for x in queue()["items"] if x["id"] == base["cluster"])
    ok("N1-c 대기열은 대표 한 장 ×4 (구성원 4 · 썸네일 3)",
       it["count"] == 4 and it["members"] == 4 and it["img"] == base["id"] and len(it["thumbs"]) == 3, str(it))
    c0 = labels(0).json()["cursor"]
    r = S("post", "/ocr/label", json={"id": base["cluster"], "label": KR})
    ok("N1-d 라벨 하나 저장 → 구성원 4", r.status_code == 200 and r.json()["members"] == 4, r.text[:120])
    j = labels(c0)
    lab = j.json()
    # ★반증 R1 (2026-09-23 밤)★ 정답(labels)은 주인님이 본 ★대표 한 장★ 뿐 — 구성원 셋은 near(dhash) 힌트로만
    ok("N1-e /ocr/labels 의 labels(정답)에는 묶음 대표 한 장만 · 구성원은 안 나간다(RF1)",
       set(lab["labels"].get("near", {})) == {hashlib.sha1(img("n0")).hexdigest()}
       and set(lab["labels"]["near"].values()) == {KR}, json.dumps(lab, ensure_ascii=False)[:200])
    ok("K1-a 한글 라벨이 한 글자도 안 바뀌고 돌아온다", all(v == KR for v in lab["labels"]["near"].values()))
    ok("K1-b 응답 바이트가 UTF-8 (x·? 로 안 깨짐)", KR.encode("utf-8") in j.content or "\\uc544" in j.text, j.text[:120])
    ok("K1-c near 맵(dhash→라벨)도 한글 그대로", set(lab["near"]["near"]) == {"00000000000000ff", "00000000000000fe",
                                                                  "0000000000000fff", "000000000000001f"}
       and set(lab["near"]["near"].values()) == {KR}, str(lab["near"]))
    n7 = sub("near", "n7", dh="00000000000000fd", pc="PC-N").json()
    ok("N1-f 라벨 된 묶음에 새로 붙은 비슷한 이미지는 라벨을 바로 받는다",
       n7["dup"] == "near" and n7["status"] == "labeled" and n7["label"] == KR, str(n7))
    lab2 = labels(lab["cursor"]).json()
    ok("N1-g 그 새 이미지만 증분으로 나간다 — ★near 힌트로만★(labels 에는 없다, RF1)",
       "near" not in lab2["labels"] and lab2["near"].get("near") == {"00000000000000fd": KR} and lab2["reset"] is False,
       str(lab2))
    names = [os.path.relpath(f, OL.OCR_DIR) for f in files_under(OL.OCR_DIR)]
    ok("K1-d 디스크 경로에 라벨·한글이 없다(파일명은 sha1)",
       names and all(n.isascii() for n in names)
       and all(re.fullmatch(r"[0-9a-f]{40}\.(png|jpg)", os.path.basename(n)) for n in names),
       str([n for n in names if not n.isascii()][:3]))
    kr = sub("아트라니아", "krsite", pc="PC-N").json()
    lab3 = S("post", "/ocr/label", json={"id": kr["cluster"], "label": "하늘 섬 ②"})
    got = labels(lab2["cursor"]).json()["labels"]
    ok("K1-e 한글 site 키도 그대로 · 라벨 기호까지 왕복", lab3.status_code == 200 and got.get("아트라니아", {}).get(
        hashlib.sha1(img("krsite")).hexdigest()) == "하늘 섬 ②", str(got))
    ok("K1-f 한글 site 폴더는 해시(ASCII)", all(p.isascii() for p in (os.path.relpath(f, OL.OCR_DIR) for f in files_under(OL.OCR_DIR))))


# ───────────────── B1 · U1 · Q1 · H1 ─────────────────

def t_bad_undo_skip_history():
    a = sub("flow", "fa", dh="ffff000000000000", pc="PC-F").json()["cluster"]
    b = sub("flow", "fb", dh="0000ffff00000000", pc="PC-F").json()["cluster"]
    c = sub("flow", "fc", dh="00000000ffff0000", pc="PC-F").json()["cluster"]
    ids = [i for i in q_ids() if i in (a, b, c)]
    ok("Q1-a 들어온 순서 a,b,c", ids == [a, b, c], str(ids))
    r = S("post", "/ocr/skip", json={"id": a})
    ids = [i for i in q_ids() if i in (a, b, c)]
    ok("Q1-b a 를 나중에 → b,c,a", r.status_code == 200 and ids == [b, c, a], str(ids))
    r = S("post", "/ocr/skip", json={"id": 999999})
    ok("Q1-c 없는 id 나중에 → 404", r.status_code == 404, str(r.status_code))
    c0 = labels(0).json()["cursor"]
    r = S("post", "/ocr/label", json={"id": b, "bad": True})
    ok("B1-a 잘못된 이미지 저장", r.status_code == 200 and r.json()["status"] == "bad", r.text[:80])
    ok("B1-b 대기열에서 빠진다", b not in q_ids())
    lb = labels(c0).json()
    ok("B1-c /ocr/labels bad 에 sha1→dhash, labels 에는 없다",
       lb["bad"].get("flow") == {hashlib.sha1(img("fb")).hexdigest(): "0000ffff00000000"} and "flow" not in lb["labels"], str(lb))
    r = S("post", "/ocr/label", json={"id": c, "label": "첫답"})
    u = S("post", "/ocr/undo", json={})
    ok("U1-a 되돌리기 → c 가 대기로", u.status_code == 200 and u.json()["id"] == c and u.json()["status"] == "pending", u.text[:80])
    ok("U1-b 되돌린 c 는 대기열 맨 앞", q_ids()[0] == c, str(q_ids()[:3]))
    lc = labels(lb["cursor"]).json()
    ok("U1-c 매크로에는 cleared 로 알린다(라벨에서 빠진다)",
       hashlib.sha1(img("fc")).hexdigest() in lc["cleared"].get("flow", []) and "flow" not in lc["labels"], str(lc))
    S("post", "/ocr/label", json={"id": c, "label": "원래답"})
    S("post", "/ocr/label", json={"id": c, "label": "고친답"})
    h = S("get", "/ocr/history").json()["items"]
    ok("H1-a 최근 라벨에 고친 값이 맨 위", h and h[0]["id"] == c and h[0]["label"] == "고친답", str(h[:1]))
    ok("H1-b 나쁨도 기록에 보인다(고칠 수 있게)", any(x["id"] == b and x["status"] == "bad" for x in h))
    u = S("post", "/ocr/undo", json={})
    ok("U1-d 고치기 되돌리기 → 앞 라벨로", u.json().get("status") == "labeled" and u.json().get("label") == "원래답", u.text[:80])
    got = labels(0).json()["labels"]["flow"][hashlib.sha1(img("fc")).hexdigest()]
    ok("U1-e /ocr/labels 도 앞 라벨", got == "원래답", got)
    r = S("post", "/ocr/label", json={"id": b, "label": "사실은정상"})
    lb2 = labels(0).json()
    ok("H1-c 나쁨을 라벨로 고치면 bad 에서 빠지고 labels 로",
       lb2["labels"]["flow"].get(hashlib.sha1(img("fb")).hexdigest()) == "사실은정상"
       and hashlib.sha1(img("fb")).hexdigest() not in lb2["bad"].get("flow", {}), str(lb2["bad"]))
    for body, want in (({"id": a, "label": "   "}, 400), ({"id": "x", "label": "a"}, 400), ({"id": True, "label": "a"}, 400),
                       ({"id": 999999, "label": "a"}, 404)):
        r = S("post", "/ocr/label", json=body)
        ok("B1-d 라벨 %r → %d" % (body, want), r.status_code == want, str(r.status_code))
    r = S("post", "/ocr/label", content=b"id=1&label=a", headers={"Content-Type": "application/x-www-form-urlencoded"})
    ok("B1-e 라벨 본문이 JSON 이 아니면 400", r.status_code == 400, str(r.status_code))
    r = S("post", "/ocr/label", json={"id": a, "label": "앞뒤공백 "})
    ok("B1-f 라벨 앞뒤 공백은 걷는다", r.json().get("label") == "앞뒤공백", r.text[:80])


# ───────────────── Z1 · R1 — 크기 · 속도 ─────────────────

def t_size_and_rate():
    r = sub("size", "big", raw=PNG + b"\0" * (OL.OCR_IMG_MAX + 1), pc="PC-Z1")
    ok("Z1-a 디코드 뒤 512KB 초과 → 413", r.status_code == 413, "%s %s" % (r.status_code, r.text[:60]))
    r = sub("size", "edge", raw=PNG + b"\1" * (OL.OCR_IMG_MAX - len(PNG)), pc="PC-Z2")
    ok("Z1-b 딱 512KB 는 받는다", r.status_code == 200, "%s %s" % (r.status_code, r.text[:60]))
    r = C.post("/ocr/submit", json={"pc_id": "PC-Z3", "site": "size", "img_b64": "A" * (OL.OCR_B64_MAX + 4), "dhash": "0" * 16},
               headers={"X-Api-Key": KEY})
    ok("Z1-c base64 길이만으로 413(디코드 전에 — 깨진 base64 여도 400 이 아니라 413)", r.status_code == 413, str(r.status_code))
    r = C.post("/ocr/submit", content=b"{" + b" " * (OL.OCR_BODY_CAP + 10) + b"}",
               headers={"X-Api-Key": KEY, "Content-Type": "application/json"})
    ok("Z1-d Content-Length 가 상한 넘으면 읽기 전에 413", r.status_code == 413, str(r.status_code))
    codes = [sub("rate", "r%d" % (i % 3), pc="PC-RATE").status_code for i in range(OL.OCR_RATE_PER_MIN)]
    ok("R1-a 분당 %d장까지는 받는다(중복도 센다)" % OL.OCR_RATE_PER_MIN, set(codes) == {200}, str(set(codes)))
    r = sub("rate", "r0", pc="PC-RATE")
    ok("R1-b 그다음은 429", r.status_code == 429, str(r.status_code))
    ok("R1-c 다른 PC 는 그대로 받는다", sub("rate", "r0", pc="PC-RATE2").status_code == 200)
    for k in list(OL._RATE):                       # 창 전부를 61초 전으로(키 모양에 안 기댄다 — PC 키·테넌트 키)
        OL._RATE[k] = type(OL._RATE[k])([t - 61 for t in OL._RATE[k]])
    ok("R1-d 1분이 지나면 다시 받는다", sub("rate", "r1", pc="PC-RATE").status_code == 200)


# ───────────────── E1 — 디스크 상한 ─────────────────

def _rows(sql, args=()):
    import sqlite3
    con = sqlite3.connect(db.DB_PATH)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def t_disk_evict():
    SZ = 10000
    with fresh_store():
        OL.OCR_DISK_CAP = 10 ** 9
        OL.OCR_DISK_HARD_CAP = 10 ** 9
        mk = lambda s, dh, pc: sub("ev", s, dh=dh, pc=pc, raw=img(s, n=SZ - len(PNG))).json()   # noqa: E731
        lab = mk("L1", "0000000000000001", "PC-E1")       # 가장 오래된 — 라벨 됨
        bad = mk("B1", "00000000000f0000", "PC-E1")       # 나쁨
        p1 = mk("P1", "000000000f000000", "PC-E1")        # 대기(오래된)
        p2 = mk("P2", "0000000f00000000", "PC-E1")        # 대기
        S("post", "/ocr/label", json={"id": lab["cluster"], "label": "보존"})
        S("post", "/ocr/label", json={"id": bad["cluster"], "bad": True})
        ok("E1-a 준비: 4장 40000바이트", _rows("SELECT SUM(nbytes) FROM ocr_img WHERE on_disk=1")[0][0] == 4 * SZ)
        OL.OCR_DISK_CAP = 4 * SZ + 100           # 한 장 더 오면 넘친다
        OL.OCR_DISK_HARD_CAP = 10 * SZ
        n1 = mk("N1", "000f000000000000", "PC-E2")
        left = {r[0] for r in _rows("SELECT id FROM ocr_cluster")}
        ok("E1-b 가장 오래된 ★대기★ 묶음(P1)만 지웠다 — 라벨·나쁨은 그대로",
           p1["cluster"] not in left and {lab["cluster"], bad["cluster"], p2["cluster"], n1["cluster"]} <= left, str(left))
        ok("E1-c 지운 대기의 파일도 없다", not any(os.path.basename(f).startswith(hashlib.sha1(img("P1", n=SZ - len(PNG))).hexdigest())
                                         for f in files_under(OL.OCR_DIR)))
        mk("N2", "0f00000000000000", "PC-E2")
        mk("N3", "f000000000000000", "PC-E2")     # 대기 P2·N1 이 차례로 빠진다
        st = dict(_rows("SELECT c.status, COUNT(*) FROM ocr_img i JOIN ocr_cluster c ON c.id=i.cluster_id WHERE i.on_disk=1 GROUP BY c.status"))
        ok("E1-d 대기만 계속 비운다(라벨 1·나쁨 1 파일 유지)", st.get("labeled") == 1 and st.get("bad") == 1, str(st))
        OL.OCR_DISK_CAP = 2 * SZ + 100            # 라벨 1 + 새것 1 만 들어갈 자리
        mk("N4", "ff00000000000000", "PC-E3")
        bad_disk = _rows("SELECT on_disk FROM ocr_img WHERE cluster_id=?", (bad["cluster"],))
        lab_disk = _rows("SELECT on_disk FROM ocr_img WHERE cluster_id=?", (lab["cluster"],))
        ok("E1-e 대기가 바닥나면 나쁨 ★파일만★ 지운다(줄은 남는다)", bad_disk == [(0,)], str(bad_disk))
        ok("E1-f 라벨 된 것은 CAP 을 넘어도 HARD_CAP 전엔 안 지운다", lab_disk == [(1,)], str(lab_disk))
        lb = labels(0).json()
        ok("E1-g 나쁨 파일이 지워져도 bad 목록은 유지", hashlib.sha1(img("B1", n=SZ - len(PNG))).hexdigest() in lb["bad"].get("ev", {}))
        OL.OCR_DISK_HARD_CAP = 1 * SZ + 100       # 이제 라벨 된 것까지
        mk("N5", "ff0000000000000f", "PC-E3")
        lab_disk = _rows("SELECT on_disk FROM ocr_img WHERE cluster_id=?", (lab["cluster"],))
        lb = labels(0).json()
        ok("E1-h HARD_CAP 을 넘으면 라벨 된 ★파일★ 도 지운다", lab_disk == [(0,)], str(lab_disk))
        ok("E1-i 그래도 라벨 줄은 영구(/ocr/labels 에 그대로)",
           lb["labels"].get("ev", {}).get(hashlib.sha1(img("L1", n=SZ - len(PNG))).hexdigest()) == "보존", str(lb["labels"]))
        g = S("get", "/ocr/img/%d" % lab["id"])
        ok("E1-j 파일이 비워진 이미지는 404", g.status_code == 404, str(g.status_code))
        orphans = _rows("SELECT COUNT(*) FROM ocr_img i LEFT JOIN ocr_cluster c ON c.id=i.cluster_id WHERE c.id IS NULL")[0][0]
        ok("E1-k 고아 줄 0", orphans == 0, str(orphans))
        # 비슷한 대기 묶음이 비우기로 지워지는 바로 그 제출 — 고아 줄이 안 생긴다(비우기가 묶음 찾기보다 먼저)
        OL.OCR_DISK_CAP = 10 ** 9
        OL.OCR_DISK_HARD_CAP = 10 ** 9
        for (cid,) in _rows("SELECT id FROM ocr_cluster WHERE status='pending'"):
            S("post", "/ocr/label", json={"id": cid, "label": "정리"})      # T1 이 가장 오래된 대기가 되게
        tgt = mk("T1", "0000ffff00000000", "PC-E4")
        used = _rows("SELECT SUM(nbytes) FROM ocr_img WHERE on_disk=1")[0][0]
        oldest = _rows("SELECT id FROM ocr_cluster WHERE status='pending' ORDER BY created, id LIMIT 1")[0][0]
        OL.OCR_DISK_CAP = used + 100
        nn = mk("T2", "0000fffe00000000", "PC-E4")
        orphans = _rows("SELECT COUNT(*) FROM ocr_img i LEFT JOIN ocr_cluster c ON c.id=i.cluster_id WHERE c.id IS NULL")[0][0]
        ok("E1-l 비슷한 대기 묶음이 그 제출의 비우기로 지워져도 고아 줄 0 · 새 묶음으로",
           orphans == 0 and oldest == tgt["cluster"] and nn.get("dup") == "new"
           and tgt["cluster"] not in {r[0] for r in _rows("SELECT id FROM ocr_cluster")},
           "orphans=%s oldest=%s tgt=%s nn=%s" % (orphans, oldest, tgt["cluster"], nn))


# ───────────────── S1 — 통계 ─────────────────

def t_stats():
    with fresh_store():
        a = sub("st", "s1", dh="000000000000000f", pc="PC-S", gem="아트라니아협곡", loc="아트라니아 협곡").json()
        sub("st", "s2", dh="000000000000001f", pc="PC-S", gem="아트라니아 협곡", loc="엉뚱")
        sub("st", "s3", dh="000000000000003f", pc="PC-S", gem="다른답")
        sub("st", "s4", dh="000000000000007f", pc="PC-S", gem="")
        p = sub("st", "s5", dh="ffff000000000000", pc="PC-S", gem="아무거나").json()
        b = sub("st", "s6", dh="0000ffff00000000", pc="PC-S", gem="x").json()
        S("post", "/ocr/label", json={"id": a["cluster"], "label": KR})
        S("post", "/ocr/label", json={"id": b["cluster"], "bad": True})
        st = S("get", "/ocr/stats").json()["sites"]["st"]
        ok("S1-a 묶음 수 대기 1 · 라벨 1 · 나쁨 1 · 이미지 6", (st["pending"], st["labeled"], st["bad"], st["images"]) == (1, 1, 1, 6), str(st))
        ok("S1-b Gemini: 빈 답은 분모에서 뺀다(3장) · 공백 차이는 같은 답 · 1장 불일치 → 0.3333",
           (st["gemini_compared"], st["gemini_disagree"], st["gemini_disagree_rate"]) == (3, 1, 0.3333), str(st))
        ok("S1-c 로컬: 2장 중 1장 불일치 → 0.5", (st["local_compared"], st["local_disagree"], st["local_disagree_rate"]) == (2, 1, 0.5), str(st))
        ok("S1-d 대기·나쁨의 답은 불일치율에 안 들어간다", p["status"] == "pending")
        emp = S("get", "/ocr/stats").json()
        ok("S1-e 디스크 합계·상한도 준다", emp["disk_bytes"] > 0 and emp["disk_cap"] == OL.OCR_DISK_CAP)


# ───────────────── I1 — since 커서 ─────────────────

def t_cursor():
    j0 = labels(0).json()
    c0 = j0["cursor"]
    j1 = labels(c0).json()
    ok("I1-a 바뀐 게 없으면 빈 증분 · 커서 그대로", j1["cursor"] == c0 and not j1["labels"] and not j1["bad"] and not j1["cleared"], str(j1))
    x = sub("cur", "c1", pc="PC-I").json()
    ok("I1-b 대기(라벨 전) 이미지는 증분에 안 나온다", labels(c0).json()["cursor"] == c0)
    S("post", "/ocr/label", json={"id": x["cluster"], "label": "하나"})
    j2 = labels(c0).json()
    ok("I1-c 라벨 하나 → 그 한 장만 · 커서 전진", j2["labels"] == {"cur": {hashlib.sha1(img("c1")).hexdigest(): "하나"}}
       and j2["cursor"] > c0, str(j2))
    ok("I1-d 새 커서로 다시 물으면 비었다", not labels(j2["cursor"]).json()["labels"])
    for bad in ("abc", "-1", "1.5"):
        r = labels(bad)
        ok("I1-e since=%s → 400" % bad, r.status_code == 400, str(r.status_code))
    j3 = labels(j2["cursor"] + 10 ** 6).json()
    ok("I1-f 커서가 서버보다 앞이면 reset=true 로 처음부터(서버 DB 가 새로 생긴 경우)",
       j3["reset"] is True and j3["labels"].get("cur") and j3["cursor"] == j2["cursor"], str(j3)[:120])
    old = OL.OCR_LABELS_PAGE
    try:
        OL.OCR_LABELS_PAGE = 2
        pg = labels(0).json()
        seen = sum(len(v) for k in ("labels", "bad") for v in pg[k].values()) + sum(len(v) for v in pg["cleared"].values())
        pg2 = labels(pg["cursor"]).json()
        ok("I1-g 한 번에 OCR_LABELS_PAGE 줄 · more=true · 이어 읽기", pg["more"] is True and seen <= 2 and pg2["cursor"] > pg["cursor"],
           str((pg["more"], seen, pg["cursor"], pg2["cursor"])))
    finally:
        OL.OCR_LABELS_PAGE = old


# ───────────────── T1 — 테넌트 격리 ─────────────────

def t_tenant():
    main.TENANTS["ocrt"] = {"password": "ocr비번2", "api_key": KEY2, "expires": "", "chat_id": ""}
    main.PW_TO_TENANT["ocr비번2"] = "ocrt"
    main.KEY_TO_TENANT[KEY2] = "ocrt"
    s2 = main.new_session("ocrt")
    m = sub("iso", "same_img", dh="0000000000000abc", pc="PC-T").json()
    S("post", "/ocr/label", json={"id": m["cluster"], "label": "본판"})
    t = sub("iso", "same_img", dh="0000000000000abc", pc="PC-T", key=KEY2).json()
    ok("T1-a 같은 이미지라도 다른 테넌트는 새 줄(본판 라벨이 안 샌다)", t["dup"] == "new" and t["status"] == "pending" and t["label"] is None, str(t))
    ok("T1-b 다른 테넌트 /ocr/labels 에 본판 라벨 없음", "iso" not in labels(0, key=KEY2).json()["labels"])
    ids2 = q_ids(s2)
    ok("T1-c 다른 테넌트 대기열에는 제 것만", ids2 == [t["cluster"]], str(ids2))
    r = S("post", "/ocr/label", sess=s2, json={"id": m["cluster"], "label": "훔치기"})
    ok("T1-d 남의 묶음에 라벨 → 404", r.status_code == 404, str(r.status_code))
    ok("T1-e 본판 라벨은 그대로", labels(0).json()["labels"]["iso"][hashlib.sha1(img("same_img")).hexdigest()] == "본판")
    r = S("get", "/ocr/img/%d" % m["id"], sess=s2)
    ok("T1-f 남의 이미지 파일 → 404", r.status_code == 404, str(r.status_code))
    S("post", "/ocr/label", sess=s2, json={"id": t["cluster"], "label": "지인답"})
    ok("T1-g 각자 라벨", labels(0, key=KEY2).json()["labels"]["iso"][hashlib.sha1(img("same_img")).hexdigest()] == "지인답"
       and labels(0).json()["labels"]["iso"][hashlib.sha1(img("same_img")).hexdigest()] == "본판")
    u = S("post", "/ocr/undo", sess=s2, json={})
    ok("T1-h 되돌리기도 제 테넌트 것만", u.json().get("id") == t["cluster"], u.text[:80])
    S("post", "/ocr/label", json={"id": m["cluster"], "label": "본판2"})           # 본판의 가장 새 기록
    u = S("post", "/ocr/undo", sess=s2, json={})
    ok("T1-l 다른 테넌트의 되돌리기가 ★본판의 더 새 기록★ 을 안 건드린다(되돌릴 게 없으면 404)",
       u.status_code == 404 and labels(0).json()["labels"]["iso"][hashlib.sha1(img("same_img")).hexdigest()] == "본판2",
       "%s %s" % (u.status_code, u.text[:80]))
    ok("T1-i 다른 테넌트 파일은 @테넌트 폴더", any(os.path.relpath(f, OL.OCR_DIR).startswith("@ocrt") for f in files_under(OL.OCR_DIR)))
    st2 = S("get", "/ocr/stats", sess=s2).json()["sites"]
    ok("T1-j 통계도 제 것만", set(st2) == {"iso"}, str(set(st2)))
    hist2 = S("get", "/ocr/history", sess=s2).json()["items"]
    ok("T1-k 기록도 제 것만", all(h["id"] == t["cluster"] for h in hist2), str(hist2)[:100])


# ───────────────── P1 — 경로 순회 ─────────────────

def t_traversal():
    root = os.path.realpath(OL.OCR_DIR)
    for site, want in (("../../evil", "evil"), ("..\\..\\win", "win"), ("/etc/passwd", "etc_passwd"), ("a/../../b", "a_______b"),
                       ("CON", "CON"), ("map\x00x", "map_x")):
        r = sub(site, "pt" + site, pc="PC-P")
        sha = hashlib.sha1(img("pt" + site)).hexdigest()
        paths = [os.path.realpath(p) for p in files_under(OL.OCR_DIR) if os.path.basename(p).startswith(sha)]
        stored = _rows("SELECT site FROM ocr_img WHERE sha1=?", (sha,))
        ok("P1-a site %r → 저장 키 %r · 파일은 OCR_DIR 안" % (site, want),
           r.status_code == 200 and len(paths) == 1 and all(os.path.commonpath([root, p]) == root for p in paths)
           and stored == [(want,)], "%s %s %s %s" % (r.status_code, r.text[:60], paths, stored))
    for site in ("../..", "///", "", None, 123):
        r = sub(site, "ptx%s" % site, pc="PC-P")
        ok("P1-b site %r → 400(남는 글자 없음)" % (site,), r.status_code == 400, str(r.status_code))
    for rel in ("../x", "..\\x", "/abs", ""):
        try:
            OL._abs(rel)
            bad = True
        except ValueError:
            bad = False
        ok("P1-c _abs(%r) 는 OCR_DIR 밖을 거부" % rel, not bad)
    ok("P1-d Windows 예약 이름 폴더는 피한다", OL._site_dir("main", "CON") == "s_CON" and OL._site_dir("main", "nul") == "s_nul")
    ok("P1-e 라벨은 파일 경로 어디에도 안 들어간다(_site_dir 는 라벨을 받지 않는다)", OL._site_dir("main", "아트라니아").startswith("u"))


# ───────────────── J1 — 화면 JS ─────────────────

def _script():
    h = OL.PAGE_HTML
    return h[h.index("<script>") + 8:h.rindex("</script>")]


def _node(js):
    node = shutil.which("node")
    d = tempfile.mkdtemp(prefix="ocrjs_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-800:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def _find_check_js():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for p in (os.getenv("CHECK_JS", ""),
              os.path.join(here, "..", "..", "web", ".claude", "skills", "aion2-dashboard", "check_js.py"),
              r"C:\Users\USER\Desktop\src\web\.claude\skills\aion2-dashboard\check_js.py"):
        if p and os.path.isfile(p):
            return p
    return None


_FAKE_DOM = r"""
const els = {}; const calls = []; let resp = {};
function mk(id){ return {id, textContent:'', value:'', hidden:false, style:{}, className:'', checked:false, children:[], _h:{},
  classList:{add(){}, remove(){}}, get firstChild(){ return this.children[0] || null; },
  removeChild(c){ this.children.splice(this.children.indexOf(c), 1); }, appendChild(c){ this.children.push(c); },
  addEventListener(t, f){ (this._h[t] = this._h[t] || []).push(f); }, focus(){}, select(){}, removeAttribute(){ this.src=''; },
  clientWidth:500, naturalWidth:0, naturalHeight:0, src:'', alt:''}; }
globalThis.document = {getElementById(id){ return els[id] || (els[id] = mk(id)); }, createElement(t){ return mk('_' + t); }, title:''};
globalThis.window = {innerHeight:800, addEventListener(){}};
globalThis.location = {href:''};
globalThis.localStorage = {getItem(){ return null; }, setItem(){}};
globalThis.Image = function(){ this.src=''; };
globalThis.setInterval = function(){ return 0; };
globalThis.fetch = async function(url, opt){ calls.push({url, method: opt.method, body: opt.body ? JSON.parse(opt.body) : null});
  const key = opt.method + ' ' + url.split('?')[0]; const j = resp[key] || {ok:true};
  return {ok:true, status:200, json: async () => j}; };
const tick = () => new Promise(r => setTimeout(r, 5));
function key(k, extra){ const ev = Object.assign({key:k, isComposing:false, shiftKey:false, ctrlKey:false, metaKey:false,
  _pd:false, preventDefault(){ this._pd = true; }}, extra || {}); els['ans']._h.keydown.forEach(f => f(ev)); return ev; }
const I = (id, site) => ({id, img: id * 10, site, status:'pending', label:null, count:2, members:2, prompt:'읽어라<b>', gemini:'힌트'+id,
  local:'', pc:'PC-01', thumbs:[id * 10 + 1]});
resp['GET /ocr/queue'] = {pending:3, items:[I(1,'s<img src=x>'), I(2,'s2'), I(3,'s3')], suggest:{'s2':['가','나']}};
resp['GET /ocr/history'] = {items:[{id:9, img:90, site:'h', status:'labeled', label:'옛답', count:1, members:1, thumbs:[], gemini:'', prompt:''}]};
"""


def t_page_js():
    js = _script()
    ok("J1-a 화면 JS 에 innerHTML 0곳(사용자 값은 전부 textContent/value/src)", "innerHTML" not in js and "insertAdjacentHTML" not in js
       and "document.write" not in js)
    if not shutil.which("node"):
        ok("J1-b node 가 있어야 한다(화면 JS 는 node 로만 시험된다)", False)
        return
    d = tempfile.mkdtemp(prefix="ocrchk_")
    p = os.path.join(d, "page.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True, encoding="utf-8")
    ok("J1-b node --check 통과", r.returncode == 0, (r.stderr or "")[-200:])
    cj = _find_check_js()
    if not cj:
        ok("J1-c check_js.py 를 찾아야 한다(web/.claude/skills/aion2-dashboard 또는 CHECK_JS)", False)
    else:
        os.makedirs(os.path.join(d, "static"))
        shutil.copy(main.__file__, os.path.join(d, "main.py"))       # 정본 래칫이 그대로 돌게
        open(os.path.join(d, "static", "ocr_label.html"), "w", encoding="utf-8").write(OL.PAGE_HTML)
        r = subprocess.run([sys.executable, "-X", "utf8", cj, os.path.join(d, "main.py")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        ok("J1-c check_js.py(문법 + 미정의 이름 + 래칫) 가 이 화면까지 통과",
           r.returncode == 0 and "ocr_label.html" in r.stdout, (r.stdout or r.stderr)[-300:])
    pure = js[js.index("/*PURE-BEGIN*/"):js.index("/*PURE-END*/")]
    out = _node(pure + r"""
const E = (k, o) => Object.assign({key:k, isComposing:false, shiftKey:false, ctrlKey:false, metaKey:false}, o || {});
const res = {
  enter: decideKey(E('Enter'), '답', 'h'), enterEmpty: decideKey(E('Enter'), '   ', 'h'),
  enterIme: decideKey(E('Enter', {isComposing:true}), '협곡', 'h'), escIme: decideKey(E('Escape', {isComposing:true}), '협', 'h'),
  esc: decideKey(E('Escape'), '쓰던것', 'h'), tabFill: decideKey(E('Tab'), '', 'h'), tabTyped: decideKey(E('Tab'), 'a', 'h'),
  tabNoHint: decideKey(E('Tab'), '', ''), undo: decideKey(E('z', {ctrlKey:true}), '', ''), undoTyped: decideKey(E('z', {ctrlKey:true}), 'a', ''),
  letter: decideKey(E('a'), '', 'h'),
  merge1: mergeQueue(2, [{id:1},{id:2},{id:3}], {}, 0).map(x => x.id),
  merge2: mergeQueue(null, [{id:1},{id:2}], {1: 1000}, 5000).map(x => x.id),
  merge3: mergeQueue(null, [{id:1},{id:2}], {1: 1000}, 99000).map(x => x.id),
  grew: [queueGrew(-1, 5), queueGrew(3, 4), queueGrew(4, 4)], pct: [pctText(null), pctText(0.3333), pctText(1)]
};
console.log(JSON.stringify(res));
""")
    ok("J1-d Enter(값 있음)=저장 · 빈 칸 Enter=아무것도", out["enter"] == "save" and out["enterEmpty"] == "none", str(out))
    ok("J1-e ★한글 조합 중 Enter 는 조합이 끝난 뒤 저장★ · 조합 중 Esc 는 건너뛰기 아님",
       out["enterIme"] == "save_after_compose" and out["escIme"] == "none", str(out))
    ok("J1-f Esc=나중에 · 빈 칸 Tab=힌트 채우기(값 있거나 힌트 없으면 보통 Tab)",
       out["esc"] == "skip" and out["tabFill"] == "fill" and out["tabTyped"] == "none" and out["tabNoHint"] == "none", str(out))
    ok("J1-g 빈 칸 Ctrl+Z=되돌리기 · 글자 있으면 입력칸 되돌리기(가로채지 않음)",
       out["undo"] == "undo" and out["undoTyped"] == "none" and out["letter"] == "none", str(out))
    ok("J1-h 대기열 합치기: 보고 있는 것은 맨 앞 유지 · 방금 처리한 것은 15초 가림",
       out["merge1"] == [2, 1, 3] and out["merge2"] == [2] and out["merge3"] == [1, 2], str(out))
    ok("J1-i 소리 조건·% 표기", out["grew"] == [False, True, False] and out["pct"] == ["-", "33.3%", "100%"], str(out))
    # ── 화면 전체를 가짜 DOM 위에서 부팅해 키 흐름을 끝까지 ──
    flow = _node(_FAKE_DOM + js + r"""
(async () => {
  await tick(); await tick();
  const r = {};
  r.site1 = els['site'].textContent; r.prompt = els['prompt'].textContent; r.img1 = els['img'].src;
  r.count = els['qcount'].textContent; r.hist = els['hist'].children.length;
  els['ans'].value = '정답 하나';
  const e1 = key('Enter'); await tick();
  r.post1 = calls.filter(c => c.method === 'POST').map(c => [c.url, c.body]);
  r.pd1 = e1._pd; r.site2 = els['site'].textContent; r.ans2 = els['ans'].value; r.dl2 = els['dl'].children.map(o => o.value);
  const e2 = key('Escape'); await tick();
  r.post2 = calls.filter(c => c.method === 'POST').slice(1).map(c => [c.url, c.body]); r.site3 = els['site'].textContent;
  els['ans'].value = '협곡';
  const e3 = key('Enter', {isComposing:true});
  r.pd3 = e3._pd; r.before = calls.filter(c => c.method === 'POST').length;
  els['ans']._h.compositionend.forEach(f => f({})); await tick(); await tick();
  r.post3 = calls.filter(c => c.method === 'POST').slice(2).map(c => [c.url, c.body]);
  resp['POST /ocr/undo'] = {ok:true, id:3, status:'pending', label:null};
  els['ans'].value = '';
  key('z', {ctrlKey:true}); await tick(); await tick();
  r.post4 = calls.filter(c => c.method === 'POST').slice(3).map(c => c.url);
  els['hist'].children[0]._h.click[0](); await tick();
  r.edit = [els['editing'].hidden, els['ans'].value, els['site'].textContent];
  els['ans'].value = '고친답'; key('Enter'); await tick();
  r.post5 = calls.filter(c => c.method === 'POST').slice(4).map(c => [c.url, c.body]);
  els['btn-bad']._h.click[0](); await tick();
  r.post6 = calls.filter(c => c.method === 'POST').slice(5).map(c => [c.url, c.body]);
  console.log(JSON.stringify(r));
})().catch(e => { console.log(JSON.stringify({err: String(e && e.stack || e)})); });
""")
    ok("J1-j 부팅: 첫 항목 표시(사이트 원문은 textContent 로 — 태그 그대로 글자)",
       flow.get("site1") == "s<img src=x>" and flow.get("prompt") == "읽어라<b>" and flow.get("img1") == "/ocr/img/10", str(flow)[:300])
    ok("J1-k 대기 카운터·기록 목록", flow.get("count") == "대기 3" and flow.get("hist") == 1, str(flow)[:200])
    ok("J1-l Enter → POST /ocr/label {id:1,label} · 다음 항목 · 입력칸 비움 · 그 사이트 자동완성",
       flow.get("post1") == [["/ocr/label", {"id": 1, "label": "정답 하나"}]] and flow.get("pd1") is True
       and flow.get("site2") == "s2" and flow.get("ans2") == "" and flow.get("dl2") == ["가", "나"], str(flow)[:400])
    ok("J1-m Esc → POST /ocr/skip {id:2} · 다음 항목", flow.get("post2") == [["/ocr/skip", {"id": 2}]] and flow.get("site3") == "s3",
       str(flow.get("post2")))
    ok("J1-n 조합 중 Enter 는 막지 않고 보내지도 않는다 → compositionend 뒤 저장",
       flow.get("pd3") is False and flow.get("before") == 2 and flow.get("post3") == [["/ocr/label", {"id": 3, "label": "협곡"}]],
       str(flow.get("post3")))
    ok("J1-o 빈 칸 Ctrl+Z → POST /ocr/undo", flow.get("post4") == ["/ocr/undo"], str(flow.get("post4")))
    ok("J1-p 기록 누르면 고치기(값 채움) → Enter 로 그 id 저장",
       flow.get("edit") == [False, "옛답", "h"] and flow.get("post5") == [["/ocr/label", {"id": 9, "label": "고친답"}]], str(flow)[-300:])
    ok("J1-q [잘못된 이미지] → {id, bad:true}", bool(flow.get("post6")) and flow["post6"][0][0] == "/ocr/label"
       and flow["post6"][0][1].get("bad") is True, str(flow.get("post6")))
    ok("J1-r 흐름 중 예외 없음", "err" not in flow, flow.get("err", "")[:300])


# ───────────────── F1 — 팜뷰 /api/fv/ocr/* (2026-09-23 밤 · 장부 #125) ─────────────────

FV_TOK = "fv-ocr-test-token-0123456789abcdef"
ITEM_KEYS = {"id", "img", "site", "status", "label", "created", "at", "count", "members", "prompt", "gemini", "local", "pc", "thumbs"}
STAT_KEYS = {"pending", "labeled", "bad", "images", "hits", "gemini_compared", "gemini_disagree", "gemini_disagree_rate",
             "local_compared", "local_disagree", "local_disagree_rate"}
FV_ROUTES = (("get", "/api/fv/ocr/queue", {}), ("get", "/api/fv/ocr/img/1", {}), ("post", "/api/fv/ocr/label", {"json": {"id": 1, "text": "x"}}),
             ("post", "/api/fv/ocr/bad", {"json": {"id": 1}}), ("post", "/api/fv/ocr/skip", {"json": {"id": 1}}),
             ("post", "/api/fv/ocr/undo", {"json": {}}), ("get", "/api/fv/ocr/history", {}), ("get", "/api/fv/ocr/stats", {}))


def FV(method, url, tok=FV_TOK, **kw):
    C.cookies.clear()
    h = dict(kw.pop("headers", {}) or {})
    if tok is not None:
        h["X-FV-Token"] = tok
    return getattr(C, method)(url, headers=h, **kw)


def _errshape(r, code):
    try:
        j = r.json()
    except Exception:
        return False
    # ★FV 에러 모양은 main._fv_err 정본 — {ok:false, error, err, code}(2026-09-24 v4: 팜뷰 dashCmd 가 j.ok===false·j.err 로 읽는다)★
    return (r.status_code == code and set(j) == {"ok", "error", "err", "code"} and j["ok"] is False and j["code"] == code
            and isinstance(j["error"], str) and bool(j["error"]) and j["err"] == j["error"])


def t_fv():
    old = (main.FV_TOKEN, main.FV_TENANT)
    main.TENANTS.setdefault("ocrt", {"password": "ocr비번2", "api_key": KEY2, "expires": "", "chat_id": ""})
    main.KEY_TO_TENANT[KEY2] = "ocrt"
    try:
        main.FV_TOKEN = ""
        res = [FV(m, u, **kw) for m, u, kw in FV_ROUTES]
        ok("F1-a FV_TOKEN 미설정 → /api/fv/ocr/* 8개 전부 404 {error,code}", all(_errshape(r, 404) for r in res),
           str([r.status_code for r in res]))
        main.FV_TOKEN = FV_TOK
        main._KEY_FAILS.clear()
        res = [FV(m, u, tok=None, **kw) for m, u, kw in FV_ROUTES]
        ok("F1-b 토큰 없음 → 8개 전부 401 {error,code}", all(_errshape(r, 401) for r in res), str([r.status_code for r in res]))
        res = [FV(m, u, tok="wrong-token", **kw) for m, u, kw in FV_ROUTES]
        ok("F1-c 틀린 토큰 → 8개 전부 401", all(_errshape(r, 401) for r in res), str([r.status_code for r in res]))
        r = FV("get", "/api/fv/ocr/queue", tok=None, params={"limit": "abc"})
        ok("F1-d 토큰 없는 limit=abc 는 401(422·400 아님 — 토큰부터)", _errshape(r, 401), str(r.status_code))
        r = FV("get", "/api/fv/ocr/img/abc", tok=None)
        ok("F1-e 토큰 없는 img/abc 는 401(422 아님)", _errshape(r, 401), str(r.status_code))
        main._KEY_FAILS.clear()           # 틀린 토큰 18번이 같은 IP 의 매크로 키 검사를 잠그지 않게
        r = FV("get", "/api/fv/ocr/queue", tok=None, headers={"X-Api-Key": KEY})
        ok("F1-f 매크로 키·세션으로는 FV 가 안 열린다(401)", r.status_code == 401 and S("get", "/api/fv/ocr/stats").status_code == 401)
        main._KEY_FAILS.clear()
        with fresh_store():
            r = FV("post", "/api/fv/ocr/undo", json={})
            ok("F1-g 되돌릴 게 없으면 404 {error,code}", _errshape(r, 404), r.text[:80])
            a = sub("fvs", "fa", dh="ffff000000000000", pc="PC-FV", gem="답A").json()
            sub("fvs", "fa2", dh="fffe000000000000", pc="PC-FV")                 # a 와 한 묶음
            b = sub("fvs", "fb", dh="0000ffff00000000", pc="PC-FV").json()
            c = sub("fvs", "fc", dh="00000000ffff0000", pc="PC-FV").json()
            o = sub("fvs", "fo", dh="000000000000ffff", pc="PC-FV", key=KEY2).json()    # 다른 테넌트
            fq = FV("get", "/api/fv/ocr/queue", params={"limit": 50})
            wq = S("get", "/ocr/queue", params={"limit": 50})
            ok("F1-h FV queue = 웹 queue 와 같은 몸통(now 빼고)", fq.status_code == 200 and
               {k: v for k, v in fq.json().items() if k != "now"} == {k: v for k, v in wq.json().items() if k != "now"},
               fq.text[:200])
            fj = fq.json()
            ok("F1-i FV queue 모양 = 계약(pending·items·suggest·now, Item 14칸)",
               set(fj) == {"pending", "items", "suggest", "now"} and all(set(it) == ITEM_KEYS for it in fj["items"])
               and fj["pending"] == 3, str(fj)[:200])
            ok("F1-j ×N — 묶음 a 는 count 2 · members 2",
               [(it["count"], it["members"]) for it in fj["items"] if it["id"] == a["cluster"]] == [(2, 2)])
            ok("F1-k FV_TENANT 가 아닌 테넌트의 묶음은 안 보인다", o["cluster"] not in [it["id"] for it in fj["items"]])
            r = FV("post", "/api/fv/ocr/label", json={"id": o["cluster"], "text": "남의것"})
            ok("F1-l 남의 테넌트 묶음 라벨 → 404 {error,code}", _errshape(r, 404), r.text[:80])
            r = FV("get", "/api/fv/ocr/img/%d" % o["id"])
            ok("F1-m 남의 테넌트 이미지 → 404 {error,code}", _errshape(r, 404), r.text[:80])
            main.FV_TENANT = "ocrt"
            ids_o = [it["id"] for it in FV("get", "/api/fv/ocr/queue").json()["items"]]
            main.FV_TENANT = old[1]
            ok("F1-n FV_TENANT 를 바꾸면 그 테넌트 것만 보인다(테넌트 = FV_TENANT)", ids_o == [o["cluster"]], str(ids_o))
            r = FV("get", "/api/fv/ocr/img/%d" % a["id"])
            ok("F1-o FV 이미지 바이트 그대로 · image/png", r.status_code == 200 and r.content == img("fa")
               and r.headers.get("content-type", "").startswith("image/png"), str(r.status_code))
            for body, why in (({"id": a["cluster"], "text": ""}, "빈 text"), ({"id": a["cluster"], "text": "   "}, "공백 text"),
                              ({"id": a["cluster"]}, "text 없음"), ({"text": "x"}, "id 없음"), ({"id": "x", "text": "x"}, "id 문자"),
                              ([1], "본문 목록")):
                r = FV("post", "/api/fv/ocr/label", json=body)
                ok("F1-p FV label %s → 400 {error,code}" % why, _errshape(r, 400), r.text[:80])
            r = FV("get", "/api/fv/ocr/queue", params={"limit": "abc"})
            ok("F1-q FV limit=abc → 400 {error,code}", _errshape(r, 400), r.text[:80])
            # 팜뷰에서 단 라벨 → 웹 기록에 · 웹에서 단 라벨 → 팜뷰 기록에
            r = FV("post", "/api/fv/ocr/label", json={"id": a["cluster"], "text": " 아트라니아 협곡 "})
            rj = r.json()
            ok("F1-r FV label 응답 = 계약 모양 · 앞뒤 공백 걷음 · 묶음 2장",
               r.status_code == 200 and set(rj) == {"ok", "id", "status", "label", "prev_status", "prev_label", "members", "pending"}
               and rj["label"] == KR and rj["status"] == "labeled" and rj["members"] == 2 and rj["pending"] == 2, r.text[:200])
            wh = S("get", "/ocr/history").json()["items"]
            ok("F1-s 팜뷰 라벨이 웹 /ocr/history 에 라벨로 보인다", bool(wh) and wh[0]["id"] == a["cluster"] and wh[0]["label"] == KR
               and wh[0]["status"] == "labeled", str(wh[:1])[:160])
            S("post", "/ocr/label", json={"id": b["cluster"], "label": "웹에서"})
            fh = FV("get", "/api/fv/ocr/history").json()["items"]
            ok("F1-t 웹 라벨이 팜뷰 /api/fv/ocr/history 에 보인다 · Item 모양",
               bool(fh) and fh[0]["id"] == b["cluster"] and fh[0]["label"] == "웹에서" and all(set(x) == ITEM_KEYS for x in fh),
               str(fh[:1])[:160])
            ok("F1-u 매크로 /ocr/labels 에 두 쪽 라벨이 같이 세진다",
               set(labels(0).json()["labels"].get("fvs", {}).values()) == {KR, "웹에서"})
            u = FV("post", "/api/fv/ocr/undo", json={})
            ok("F1-v 팜뷰 되돌리기가 ★웹에서 단★ 라벨을 되돌린다(같은 테넌트)",
               u.status_code == 200 and u.json() == {"ok": True, "id": b["cluster"], "status": "pending", "label": None}, u.text[:120])
            ok("F1-w 되돌린 묶음은 웹 대기열 맨 앞", q_ids()[0] == b["cluster"], str(q_ids()[:3]))
            S("post", "/ocr/undo", json={})
            ok("F1-x 웹 되돌리기가 ★팜뷰에서 단★ 라벨을 되돌린다", a["cluster"] in q_ids()
               and not labels(0).json()["labels"].get("fvs"))
            r = FV("post", "/api/fv/ocr/bad", json={"id": c["cluster"]})
            ok("F1-y FV bad → status bad · label null", r.status_code == 200 and r.json()["status"] == "bad" and r.json()["label"] is None,
               r.text[:120])
            ok("F1-z 팜뷰 나쁨이 웹 기록에 나쁨으로", any(x["id"] == c["cluster"] and x["status"] == "bad"
                                                 for x in S("get", "/ocr/history").json()["items"]))
            r = FV("post", "/api/fv/ocr/skip", json={"id": c["cluster"]})
            ok("F1-A 대기 아닌 묶음 skip → 409 {error,code}", _errshape(r, 409), r.text[:80])
            r = FV("post", "/api/fv/ocr/skip", json={"id": 999999})
            ok("F1-B 없는 묶음 skip → 404 {error,code}", _errshape(r, 404), r.text[:80])
            before = q_ids()
            r = FV("post", "/api/fv/ocr/skip", json={"id": before[0]})
            ok("F1-C FV skip → {ok,id} · 웹 대기열 맨 뒤로", r.json() == {"ok": True, "id": before[0]} and q_ids() == before[1:] + before[:1],
               "%s %s" % (before, q_ids()))
            r = FV("post", "/api/fv/ocr/bad", json={})
            ok("F1-D FV bad id 없음 → 400 {error,code}", _errshape(r, 400), r.text[:80])
            st = FV("get", "/api/fv/ocr/stats").json()
            ok("F1-E FV stats = 웹 stats · 계약 키", st == S("get", "/ocr/stats").json()
               and set(st) == {"sites", "disk_bytes", "disk_cap", "disk_hard_cap"} and set(st["sites"].get("fvs", {})) == STAT_KEYS
               and st["sites"]["fvs"]["bad"] == 1, str(st)[:200])
            r = FV("get", "/api/fv/ocr/history", params={"limit": "0"})
            ok("F1-F FV history limit 범위 밖은 1~200 으로 자른다(0 → 1)", r.status_code == 200 and len(r.json()["items"]) == 1, r.text[:80])
    finally:
        main.FV_TOKEN, main.FV_TENANT = old
        main._KEY_FAILS.clear()


# ───────────────── RF1~RF10 · M1~M2 — 반증 시험(refute_ocr/tests/test_refute_ocr.py R1~R10) 이식 (2026-09-23 밤) ─────────────────
# 각 검사는 「옳은 동작」을 단언한다. 고치기 전 ocr_label.py 에서 FAIL(재현) → 고친 뒤 OK.
# 이름에 [지킴] 이 붙은 것만 예외 — 준비 확인·「고치다가 멀쩡한 것을 깨지 않았다」 회귀 방지라 고치기 전에도 OK 다.

BIG = 2 ** 70


def _lab(cid, text, sess=None):
    return S("post", "/ocr/label", sess=sess, json={"id": cid, "label": text})


def _reset_rate():
    OL._RATE.clear()
    main._KEY_FAILS.clear()


def _guard(prefix, fn):
    """이식 검사 하나가 예외로 죽어도 뒤 검사가 돌게 — 예외는 그 자리에서 FAIL 한 줄."""
    try:
        r = fn()
        return r
    except Exception as e:                       # noqa: BLE001
        ok("%s 예외 없이 끝나야 한다" % prefix, False, "%s: %s" % (type(e).__name__, str(e)[:200]))


def rf1_near_not_exact():
    """R1 (High) — near 로 붙은(주인님이 본 적 없는) 이미지는 labels(정답)에 안 나간다."""
    with fresh_store():
        _reset_rate()
        a = sub("num", "twelve", dh="0000000000000000", pc="PC-RF1").json()
        pre = sub("num", "pre", dh="0000000000000003", pc="PC-RF1").json()          # 라벨 ★전에★ 붙은 구성원(거리 2)
        _lab(a["cluster"], "12")
        b = sub("num", "thirteen", dh="0000000000000001", pc="PC-RF1").json()       # 라벨 ★뒤에★ 붙은 구성원(거리 1)
        sha_a, sha_b, sha_p = (hashlib.sha1(img(x)).hexdigest() for x in ("twelve", "thirteen", "pre"))
        f = labels(0).json()
        ok("RF1-a 라벨 뒤 near 로 붙은 새 이미지는 labels(정답)에 없다", sha_b not in f["labels"].get("num", {}),
           "dup=%s labels=%s" % (b.get("dup"), f["labels"]))
        ok("RF1-b 라벨 전에 붙은 구성원도 labels 에 없다 · 대표만 있다", f["labels"].get("num") == {sha_a: "12"}, str(f["labels"]))
        ok("RF1-c [지킴] 구성원은 near(dhash → 라벨) 힌트로 나간다",
           f["near"].get("num") == {"0000000000000000": "12", "0000000000000001": "12", "0000000000000003": "12"}, str(f["near"]))
        ok("RF1-d /ocr/submit near 합류 응답은 label_match:\"near\"(정답이라 하지 않는다)",
           b.get("dup") == "near" and b.get("status") == "labeled" and b.get("label_match") == "near" and b.get("near_dist") == 1, str(b))
        ra = sub("num", "twelve", dh="0000000000000000", pc="PC-RF1").json()
        ok("RF1-e 대표의 정확 재제출 → dup exact · label_match exact", ra.get("dup") == "exact" and ra.get("label_match") == "exact"
           and ra.get("label") == "12", str(ra))
        rb = sub("num", "thirteen", dh="0000000000000001", pc="PC-RF1").json()
        ok("RF1-f 구성원의 정확 재제출도 label_match near · near_dist = 대표와의 거리",
           rb.get("dup") == "exact" and rb.get("label_match") == "near" and rb.get("near_dist") == 1, str(rb))
        rp = sub("num", "pre", dh="0000000000000003", pc="PC-RF1").json()
        ok("RF1-g 대기 묶음 응답은 label_match null", pre.get("label_match") is None and rp.get("label_match") == "near", "%s %s" % (pre, rp))
        c = labels(0).json()["cursor"]
        _lab(a["cluster"], "13")                                                    # 고치기 → 대표만 labels 로 다시
        g = labels(c).json()
        ok("RF1-h 고치기 뒤 증분도 labels 는 대표만, near 는 셋 다", g["labels"].get("num") == {sha_a: "13"}
           and set(g["near"].get("num", {})) == {"0000000000000000", "0000000000000001", "0000000000000003"}
           and sha_p not in json.dumps(g["labels"]), str(g)[:200])


def rf2_epoch():
    """R2 (High) — DB 세대(epoch). 새 DB 의 커서가 옛 커서를 넘어도 reset 을 준다."""
    with fresh_store():
        _reset_rate()
        for i in range(3):
            _lab(sub("old", "o%d" % i, dh=DH6[i], pc="PC-RF2").json()["cluster"], "o%d" % i)
        f0 = labels(0).json()
        c_old, ep_old = f0["cursor"], f0.get("epoch")
        ok("RF2-a /ocr/labels 가 epoch(16자리 16진수)를 준다", isinstance(ep_old, str) and re.fullmatch(r"[0-9a-f]{16}", ep_old or "") is not None,
           str(ep_old))
        ok("RF2-b epoch 는 ocr_meta 표에 한 줄", _rows("SELECT v FROM ocr_meta WHERE k='epoch'") == [(ep_old,)],
           str(_rows("SELECT * FROM ocr_meta")))
        OL._INITED.discard(db.DB_PATH)                                              # 서버 재시작 흉내 — 표 만들기를 다시 탄다
        f1 = labels(c_old, epoch=ep_old).json()
        ok("RF2-c 재시작해도 같은 DB 면 같은 epoch · reset 없음 · 빈 증분", f1.get("epoch") == ep_old and f1["reset"] is False
           and not f1["labels"] and f1["cursor"] == c_old, str(f1)[:160])
        f2 = labels(c_old, epoch="").json()
        ok("RF2-d since>0 인데 epoch 없음(옛 매크로) → reset:true · 처음부터 전부", f2["reset"] is True and len(f2["labels"].get("old", {})) == 3,
           str(f2)[:160])
        f3 = labels(c_old, epoch="0123456789abcdef").json()
        ok("RF2-e since>0 인데 epoch 다름 → reset:true", f3["reset"] is True and len(f3["labels"].get("old", {})) == 3, str(f3)[:160])
        f4 = labels(0, epoch="").json()
        ok("RF2-f since=0 은 epoch 없어도 reset 아님(처음 받기)", f4["reset"] is False, str(f4)[:120])
    with fresh_store():                                                             # 서버 DB 재생성 — 커서가 옛 것을 넘는다
        _reset_rate()
        for i in range(5):
            _lab(sub("new", "n%d" % i, dh=DH6[i], pc="PC-RF2").json()["cluster"], "n%d" % i)
        f = labels(c_old, epoch=ep_old).json()
        ok("RF2-g DB 재생성 뒤 옛 커서(%d)·옛 epoch → reset:true · 새 라벨 5건 전부" % c_old,
           f["reset"] is True and len(f["labels"].get("new", {})) == 5 and f["cursor"] >= 5, "reset=%s labels=%d cursor=%s" % (
               f["reset"], len(f["labels"].get("new", {})), f["cursor"]))
        ok("RF2-h 새 DB 의 epoch 는 옛 것과 다르다", f.get("epoch") and f["epoch"] != ep_old, "%s %s" % (f.get("epoch"), ep_old))


def rf3_cleared_survives_eviction():
    """R3 — 되돌린(한 번이라도 라벨 된) 대기 묶음은 비우기 1단계가 안 지운다 → cleared 가 반드시 간다."""
    sha_a = hashlib.sha1(img("A")).hexdigest()
    sz = len(img("A"))
    with fresh_store():                                                             # 반증 R3 그대로
        _reset_rate()
        OL.OCR_DISK_CAP = OL.OCR_DISK_HARD_CAP = 10 ** 9
        a = sub("ev", "A", dh="0000000000000000", pc="PC-RF3").json()
        _lab(a["cluster"], "wrong")
        c1 = labels(0).json()["cursor"]                                             # 매크로가 "wrong" 을 가져갔다
        S("post", "/ocr/undo", json={})                                             # 주인님: 틀렸다 → 대기
        OL.OCR_DISK_CAP = sz + 10                                                   # 다음 한 장이 오면 가장 오래된 대기(A)를 비우려 한다
        sub("ev", "B", dh="ffffffffffffffff", pc="PC-RF3")
        ok("RF3-a 되돌린 A(ocr_hist 있음)는 비우기 1단계가 안 지운다", a["cluster"] in {r[0] for r in _rows("SELECT id FROM ocr_cluster")},
           str(_rows("SELECT id, status FROM ocr_cluster")))
        f = labels(c1).json()
        ok("RF3-b 매크로는 cleared 로 A 를 받는다('wrong' 이 영구로 남지 않는다)",
           f["reset"] is False and sha_a in f["cleared"].get("ev", []), json.dumps(f, ensure_ascii=False)[:200])
        OL.OCR_DISK_CAP = 1
        for i in range(3):
            sub("ev", "C%d" % i, dh=DH6[2 + i], pc="PC-RF3")
        ok("RF3-c 상한이 바닥이어도 A 는 끝까지 안 지워진다",
           a["cluster"] in {r[0] for r in _rows("SELECT id FROM ocr_cluster")}
           and _rows("SELECT COUNT(*) FROM ocr_hist WHERE cluster_id=?", (a["cluster"],))[0][0] == 1)
    with fresh_store():                                                             # [지킴] 라벨 안 된 대기는 여전히 비운다
        _reset_rate()
        OL.OCR_DISK_CAP = OL.OCR_DISK_HARD_CAP = 10 ** 9
        p = sub("ev", "P", dh="0f0f0f0f0f0f0f0f", pc="PC-RF3").json()
        a = sub("ev", "A", dh="0000000000000000", pc="PC-RF3").json()
        _lab(a["cluster"], "wrong")
        S("post", "/ocr/undo", json={})
        OL.OCR_DISK_CAP = 2 * sz + 10
        sub("ev", "B", dh="ffffffffffffffff", pc="PC-RF3")
        left = {r[0] for r in _rows("SELECT id FROM ocr_cluster")}
        ok("RF3-d [지킴] 한 번도 라벨 안 된 대기(P)는 여전히 비운다 · 되돌린 A 는 남긴다", p["cluster"] not in left and a["cluster"] in left,
           str(left))


def rf4_big_ids():
    """R4 — 부호 있는 64비트 밖 id → 본문 400 · 경로 404 (7 라우트, 500 금지)."""
    with fresh_store():
        _reset_rate()
        main.FV_TOKEN, oldtok = FV_TOK, main.FV_TOKEN
        try:
            for v in (BIG, -BIG, 2 ** 63):
                rs = {
                    "web img": S("get", "/ocr/img/%d" % v).status_code,
                    "web label": _lab(v, "x").status_code,
                    "web skip": S("post", "/ocr/skip", json={"id": v}).status_code,
                    "fv img": FV("get", "/api/fv/ocr/img/%d" % v).status_code,
                    "fv label": FV("post", "/api/fv/ocr/label", json={"id": v, "text": "x"}).status_code,
                    "fv bad": FV("post", "/api/fv/ocr/bad", json={"id": v}).status_code,
                    "fv skip": FV("post", "/api/fv/ocr/skip", json={"id": v}).status_code,
                }
                want = {k: (404 if k.endswith("img") else 400) for k in rs}
                ok("RF4-a id=%s → 본문 id 400 · 경로 img id 404 (7 라우트, 500 없음)" % ("2^70" if v == BIG else "-2^70" if v == -BIG else "2^63"),
                   rs == want, str(rs))
            r = FV("post", "/api/fv/ocr/label", json={"id": BIG, "text": "x"})
            ok("RF4-b 팜뷰 범위 밖 id 도 {error,code} 400", _errshape(r, 400), r.text[:80])
            edge = [_lab(2 ** 63 - 1, "x").status_code, S("get", "/ocr/img/%d" % (2 ** 63 - 1)).status_code]
            ok("RF4-c [지킴] 경계 2^63-1 은 범위 안 → 없는 묶음 404", edge == [404, 404], str(edge))
        finally:
            main.FV_TOKEN = oldtok
            main._KEY_FAILS.clear()


def rf5_captcha_anywhere():
    """R5 — 원문 전체를 정규화한 뒤 captcha·캡차·캡챠 가 ★어디든★ 있으면 400."""
    with fresh_store():
        _reset_rate()
        before = len(files_under(OL.OCR_DIR))
        cases = ["recaptcha", "reCAPTCHA_v2", "hCaptcha", "login_captcha", "." * 42 + "captcha", "_" * 60 + "captcha",
                 "сaptcha", "cаptcha", "СAPTCHA", "captсha", "cарtсhа",           # 키릴 с·а·р
                 "cap‍tcha", "c.a.p.t.c.h.a", "ćaptcha", "ｒｅｃａｐｔｃｈａ", "CΑPTCHΑ",   # 영폭 끼움·구두점·결합부호·전각·그리스 Α
                 "캡 차", "cap tcha", "로그인캡차", "보안_캡챠", "캡​챠"]
        res = {repr(s): sub(s, "cap" + s, pc="PC-RFC%d" % i) for i, s in enumerate(cases)}
        codes = {k: r.status_code for k, r in res.items()}
        ok("RF5-a 캡차류 site %d가지 전부 400" % len(cases), all(v == 400 for v in codes.values()), str({k: v for k, v in codes.items() if v != 400}))
        ok("RF5-b 거부 사유가 캡차(다른 검사보다 먼저)", all("캡차" in r.json().get("detail", "") for r in res.values()))
        r = C.post("/ocr/submit", json={"pc_id": "PC-RFC", "site": "hcaptcha", "img_b64": "!!!notb64", "dhash": "zz"},
                   headers={"X-Api-Key": KEY})
        ok("RF5-c 캡차면 base64·dhash 를 보기도 전에 400 캡차", r.status_code == 400 and "캡차" in r.json().get("detail", ""), r.text[:80])
        n_img = (_rows("SELECT COUNT(*) FROM ocr_img")[0][0]
                 if _rows("SELECT COUNT(*) FROM sqlite_master WHERE name='ocr_img'")[0][0] else 0)   # 표조차 안 생겼으면 0
        ok("RF5-d 거부된 것은 파일 0 · 줄 0", len(files_under(OL.OCR_DIR)) == before and n_img == 0, "files=%d rows=%d" % (
            len(files_under(OL.OCR_DIR)) - before, n_img))
        okc = {s: sub(s, "okc" + s, pc="PC-RFK%d" % i, dh="%016x" % (i * 0x1111)).status_code
               for i, s in enumerate(["map_capital", "capture", "캡쳐", "odd_energy", "apt", "cha_cap"])}
        ok("RF5-e [지킴] 캡차가 아닌 이름은 받는다(capture·캡쳐·map_capital)", all(v == 200 for v in okc.values()), str(okc))
        ok("RF5-f 정규화 함수 단위", OL.captcha_norm("  Re-CAPTCHA​ v2 ") == "recaptchav2" and OL.captcha_norm("캡 차") == "캡차"
           and OL.captcha_norm(None) == "", OL.captcha_norm("  Re-CAPTCHA​ v2 "))


def rf6_tenant_dirs():
    """R6 — 한글·대문자 테넌트 이름은 해시 폴더 → 두 테넌트가 한 폴더를 안 쓴다."""
    for name, key in (("홍길동", "k_hong"), ("김철수", "k_kim")):
        main.TENANTS[name] = {"password": "pw_" + key, "api_key": key, "expires": "", "chat_id": ""}
        main.KEY_TO_TENANT[key] = name
    dirs = {t: OL._tenant_dir(t) for t in ("홍길동", "김철수", "Kim", "kim", "ocrt", "a b", "a_b", "con", "")}
    ok("RF6-a 테넌트 폴더가 전부 다르다(한글 둘·대소문자 둘·공백/밑줄)", len(set(dirs.values())) == len(dirs), str(dirs))
    ok("RF6-b 소문자 ASCII 안전 이름은 @이름 그대로 · 나머지는 @@해시(ASCII)",
       dirs["ocrt"] == "@ocrt" and dirs["kim"] == "@kim" and all(v.startswith("@@") and v.isascii() for k, v in dirs.items()
                                                                  if k not in ("ocrt", "kim", "a_b")), str(dirs))
    with fresh_store():
        _reset_rate()
        OL.OCR_DISK_CAP = OL.OCR_DISK_HARD_CAP = 10 ** 9
        sub("odd", "SAME", pc="PC-H", key="k_hong")
        k = sub("odd", "SAME", pc="PC-K", key="k_kim").json()
        rels = dict(_rows("SELECT tenant, relpath FROM ocr_img"))
        ok("RF6-c 같은 이미지라도 두 한글 테넌트의 파일 경로가 다르다", len(set(rels.values())) == 2, str(rels))
        sz = len(img("SAME"))
        OL.OCR_DISK_CAP = sz + sz - 1                                               # 홍길동의 다음 한 장 → 홍길동 대기 비우기
        sub("odd", "OTHER", pc="PC-H", key="k_hong", dh="ffffffffffffffff")
        r = S("get", "/ocr/img/%d" % k["id"], sess=main.new_session("김철수"))
        ok("RF6-d 홍길동의 비우기가 김철수의 파일을 안 지운다", r.status_code == 200 and r.content == img("SAME"),
           "GET=%d on_disk=%s" % (r.status_code, _rows("SELECT on_disk FROM ocr_img WHERE id=?", (k["id"],))))


def rf7_tenant_scoped_eviction():
    """R7 — 비우기는 제출한 테넌트 것만 · 테넌트마다 OCR_DISK_CAP · 전체 HARD_CAP 을 못 지키면 507."""
    main.TENANTS.setdefault("ocrt", {"password": "ocr비번2", "api_key": KEY2, "expires": "", "chat_id": ""})
    main.KEY_TO_TENANT[KEY2] = "ocrt"
    with fresh_store():
        _reset_rate()
        OL.OCR_DISK_CAP = OL.OCR_DISK_HARD_CAP = 10 ** 9
        sz = len(img("x"))
        for i in range(3):
            sub("m", "main%d" % i, dh=DH6[i], pc="PC-M")
        main_files = sorted(r[0] for r in _rows("SELECT relpath FROM ocr_img WHERE tenant='main'"))
        before = queue()["pending"]
        rent = [sub("t", "rent%d" % i, dh=DH6[i], key=KEY2, pc="PC-R%d" % i).json() for i in range(4)]
        OL.OCR_DISK_CAP = 2 * sz + 10                                               # ★테넌트마다★ 2장
        sub("t", "rent4", dh=DH6[4], key=KEY2, pc="PC-R4")
        after = queue()["pending"]
        left = {r[0] for r in _rows("SELECT id FROM ocr_cluster WHERE tenant='ocrt'")}
        ok("RF7-a 렌탈 테넌트의 제출이 main 의 대기 묶음을 안 지운다", after == before == 3, "main pending %d → %d" % (before, after))
        ok("RF7-b [지킴] 렌탈은 ★자기★ 오래된 대기부터 지운다(rent0~2)", not ({rent[0]["cluster"], rent[1]["cluster"], rent[2]["cluster"]} & left)
           and rent[3]["cluster"] in left, str(left))
        ok("RF7-c main 파일은 한 장도 안 지워졌다", all(os.path.isfile(os.path.join(OL.OCR_DIR, p)) for p in main_files)
           and _rows("SELECT COUNT(*) FROM ocr_img WHERE tenant='main' AND on_disk=1")[0][0] == 3)
        OL.OCR_DISK_CAP = 10 ** 9
        total = _rows("SELECT SUM(nbytes) FROM ocr_img WHERE on_disk=1")[0][0]
        OL.OCR_DISK_HARD_CAP = total + 10                                           # 전체가 한 장만큼 모자란다
        snap = _rows("SELECT id, on_disk FROM ocr_img ORDER BY id")
        r = sub("t", "rent5", dh=DH6[5], key=KEY2, pc="PC-R5")
        ok("RF7-d 렌탈이 지울 게 없고 전체가 HARD_CAP 을 넘으면 507 {detail}", r.status_code == 507 and "detail" in r.json(),
           "%s %s" % (r.status_code, r.text[:80]))
        ok("RF7-e 507 때는 누구 것도 안 지웠다(줄·on_disk 그대로)", _rows("SELECT id, on_disk FROM ocr_img ORDER BY id") == snap)
        _lab(rent[3]["cluster"], "렌탈답", sess=main.new_session("ocrt"))
        r = sub("t", "rent6", dh=DH6[5], key=KEY2, pc="PC-R5")
        ok("RF7-f 렌탈에 라벨 된 파일이 생기면 ★그것만★ 비워 받는다(200)", r.status_code == 200
           and _rows("SELECT on_disk FROM ocr_img WHERE id=?", (rent[3]["id"],)) == [(0,)]
           and _rows("SELECT COUNT(*) FROM ocr_img WHERE tenant='main' AND on_disk=1")[0][0] == 3, "%s %s" % (r.status_code, r.text[:80]))
        rm = sub("m", "main9", dh="ffffffffffffffff", pc="PC-M")
        ok("RF7-g main 도 지울 게 없으면 507(남의 라벨 파일에 손대지 않는다)", rm.status_code == 507
           and _rows("SELECT COUNT(*) FROM ocr_img WHERE tenant='ocrt' AND on_disk=1")[0][0] == 2, "%s %s" % (rm.status_code, rm.text[:80]))


def rf8_rate_key():
    """R8 — 속도 키 = 테넌트 + 정규화한 기본 pc id · 테넌트 합 상한."""
    with fresh_store():
        _reset_rate()
        codes = [sub("rv", "rv%d" % i, pc=pc, dh="%016x" % i).status_code
                 for i, pc in enumerate(["PC-RV"] * 30 + ["pc-rv"] * 30 + ["PC-RVb"] * 30 + ["PC-RV."] * 30)]
        ok("RF8-a 대소문자·구두점 변형은 한 PC(≤60 — PC-RV 계열 30 + 숫자 없는 PC-RVb 30)", codes.count(200) == 60,
           "accepted=%d/120" % codes.count(200))
        _reset_rate()
        c2 = [sub("rv2", "x%d" % i, pc=pc, dh="%016x" % i).status_code for i, pc in enumerate(["PC-22"] * 30 + ["pc-22d", "PC 22", "ＰＣ－２２", "PC-22z"])]
        ok("RF8-b 계정 접미사(b~z)·공백·전각 PC-22 변형도 같은 창 → 31번째부터 429", c2[:30] == [200] * 30 and c2[30:] == [429] * 4, str(c2[28:]))
        ok("RF8-c [지킴] 다른 번호(PC-23)는 따로", sub("rv2", "y", pc="PC-23").status_code == 200)
        ok("RF8-d base_pc 단위", [OL.base_pc(x) for x in ("PC-22d", "pc 22", "PC-22", "PC-RVb", "PC-2a", "")] ==
           ["pc22", "pc22", "pc22", "pcrvb", "pc2a", ""], str([OL.base_pc(x) for x in ("PC-22d", "pc 22", "PC-22", "PC-RVb", "PC-2a", "")]))
        _reset_rate()
        old = OL.OCR_RATE_TENANT_PER_MIN
        try:
            OL.OCR_RATE_TENANT_PER_MIN = 5
            c3 = [sub("rv3", "z%d" % i, pc="PC-T%d" % i, dh="%016x" % i).status_code for i in range(7)]
            ok("RF8-e 테넌트 합 상한 — pc id 를 바꿔 끼워도 6번째부터 429", c3 == [200] * 5 + [429] * 2, str(c3))
            r = sub("rv3", "zz", pc="PC-T9", key=KEY2)
            ok("RF8-f 다른 테넌트는 그 상한과 무관", r.status_code == 200, str(r.status_code))
            ok("RF8-g 429 본문 {detail}", "테넌트" in sub("rv3", "z9", pc="PC-T8").json().get("detail", ""))
        finally:
            OL.OCR_RATE_TENANT_PER_MIN = old
            _reset_rate()


async def _asgi(path, chunks, headers):
    """Content-Length 없는 청크 본문을 ASGI 로 직접 — 몇 바이트를 읽었는지 센다."""
    state = {"i": 0, "read": 0, "status": None, "body": b""}

    async def receive():
        i = state["i"]
        if i >= len(chunks):
            return {"type": "http.disconnect"}
        state["i"] += 1
        state["read"] += len(chunks[i])
        return {"type": "http.request", "body": chunks[i], "more_body": i < len(chunks) - 1}

    async def send(m):
        if m["type"] == "http.response.start":
            state["status"] = m["status"]
        elif m["type"] == "http.response.body":
            state["body"] += m.get("body", b"")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"content-type", b"application/json"), (b"transfer-encoding", b"chunked")] + headers,
             "client": ("127.0.0.1", 5555), "server": ("t", 80)}
    await main.app(scope, receive, send)
    return state


async def rf9_chunked_cap():
    """R9 — 본문을 읽으면서 세다가 상한을 넘는 순간 413(Content-Length 없어도)."""
    with fresh_store():
        _reset_rate()
        head = b'{"pc_id":"PC-CH","site":"chunk","dhash":"0000000000000000","img_b64":"'
        chunks = [head] + [b"A" * 65536] * 64 + [b'"}']                            # 4MB
        st = await _asgi("/ocr/submit", chunks, [(b"x-api-key", KEY.encode())])
        ok("RF9-a 청크 제출 본문은 OCR_BODY_CAP(%dKB) 근처에서 끊고 413" % (OL.OCR_BODY_CAP // 1024),
           st["status"] == 413 and st["read"] <= OL.OCR_BODY_CAP + 65536 * 2, "read=%dKB status=%s" % (st["read"] // 1024, st["status"]))
        small = json.dumps({"pc_id": "PC-CH", "site": "chunk", "dhash": "0" * 16, "img_b64": b64(img("chunk-ok"))}).encode()
        st = await _asgi("/ocr/submit", [small[:40], small[40:]], [(b"x-api-key", KEY.encode())])
        ok("RF9-b [지킴] 상한 안의 청크 제출은 그대로 200", st["status"] == 200, "%s %s" % (st["status"], st["body"][:80]))
        big = [b'{"id":1,"label":"'] + [b"x" * 65536] * 32 + [b'"}']               # 2MB
        st = await _asgi("/ocr/label", big, [(b"cookie", ("session=" + SESS).encode())])
        ok("RF9-c 사람 쪽 /ocr/label 청크 본문도 OCR_SMALL_BODY_CAP 근처에서 413", st["status"] == 413
           and st["read"] <= OL.OCR_SMALL_BODY_CAP + 65536 * 2, "read=%dKB status=%s" % (st["read"] // 1024, st["status"]))
        main.FV_TOKEN, oldtok = FV_TOK, main.FV_TOKEN
        try:
            st = await _asgi("/api/fv/ocr/label", big, [(b"x-fv-token", FV_TOK.encode())])
            ok("RF9-d 팜뷰 label 청크 본문도 413 {error,code}", st["status"] == 413 and json.loads(st["body"] or b"{}").get("code") == 413
               and st["read"] <= OL.OCR_SMALL_BODY_CAP + 65536 * 2, "read=%dKB status=%s %s" % (st["read"] // 1024, st["status"], st["body"][:60]))
        finally:
            main.FV_TOKEN = oldtok
            main._KEY_FAILS.clear()


def rf10_cleared_near():
    """R10 — 되돌린 묶음은 cleared(sha1) 와 함께 cleared_near(dhash) 로도 알린다 → 매크로가 near 힌트를 지운다."""
    with fresh_store():
        _reset_rate()
        dh = "00000000000000aa"
        a = sub("nc", "N", dh=dh, pc="PC-RF10").json()
        m2 = sub("nc", "N2", dh="00000000000000ab", pc="PC-RF10").json()            # 같은 묶음 구성원
        _lab(a["cluster"], "7")
        f0 = labels(0).json()
        ok("RF10-a [지킴] 준비: near 에 대표·구성원 dhash 둘", set(f0["near"].get("nc", {})) == {dh, "00000000000000ab"} and m2["dup"] == "near",
           str(f0["near"]))
        S("post", "/ocr/undo", json={})
        f = labels(f0["cursor"]).json()
        ok("RF10-b 되돌린 묶음의 dhash 가 cleared_near 로 온다(대표·구성원 둘 다)",
           f["reset"] is False and sorted(f.get("cleared_near", {}).get("nc", [])) == [dh, "00000000000000ab"], json.dumps(f)[:200])
        ok("RF10-c [지킴] cleared(sha1) 도 그대로 · labels·near 에는 없다", len(f["cleared"].get("nc", [])) == 2 and not f["labels"] and not f["near"],
           json.dumps(f)[:200])
        ok("RF10-d cleared_near 는 늘 있는 칸(비어도 {})", labels(f["cursor"]).json().get("cleared_near") == {})


def m1_invisible_label():
    """M1 — 서식 문자(Cf)·한글 채움 문자만 있는 라벨은 빈 라벨 400."""
    with fresh_store():
        _reset_rate()
        a = sub("zw", "zw", pc="PC-M1").json()
        codes = {repr(t): _lab(a["cluster"], t).status_code for t in ("​", "​‍﻿", " ⁠ ", "ㅤ", "‎‏")}
        ok("M1-a 보이지 않는 글자만 있는 라벨 → 400", set(codes.values()) == {400}, str(codes))
        r = _lab(a["cluster"], "​12‍")
        ok("M1-b 앞뒤·사이의 영폭 글자는 걷고 저장", r.status_code == 200 and r.json().get("label") == "12", r.text[:80])
        main.FV_TOKEN, oldtok = FV_TOK, main.FV_TOKEN
        try:
            r = FV("post", "/api/fv/ocr/label", json={"id": a["cluster"], "text": "​﻿"})
            ok("M1-c 팜뷰도 400 {error,code}", _errshape(r, 400), r.text[:80])
        finally:
            main.FV_TOKEN = oldtok
            main._KEY_FAILS.clear()
        ok("M1-d 라벨은 그대로 12", labels(0).json()["labels"]["zw"] == {hashlib.sha1(img("zw")).hexdigest(): "12"})


def m2_restore_on_resubmit():
    """M2 — 비우기로 파일만 지워진 이미지가 정확히 다시 오면 파일을 되살린다."""
    with fresh_store():
        _reset_rate()
        OL.OCR_DISK_CAP = OL.OCR_DISK_HARD_CAP = 10 ** 9
        a = sub("rs", "A", dh="0000000000000000", pc="PC-M2").json()
        S("post", "/ocr/label", json={"id": a["cluster"], "bad": True})
        OL.OCR_DISK_CAP = len(img("A")) + 10                                        # 다음 한 장 → 나쁨 파일만 지운다(2단계)
        sub("rs", "B", dh="ffffffffffffffff", pc="PC-M2")
        ok("M2-a [지킴] 준비: 나쁨 A 의 파일이 비워졌다", _rows("SELECT on_disk FROM ocr_img WHERE id=?", (a["id"],)) == [(0,)]
           and S("get", "/ocr/img/%d" % a["id"]).status_code == 404)
        OL.OCR_DISK_CAP = 10 ** 9
        r = sub("rs", "A", dh="0000000000000000", pc="PC-M2").json()
        g = S("get", "/ocr/img/%d" % a["id"])
        ok("M2-b 정확히 다시 오면 파일을 되살린다(on_disk=1 · 바이트 그대로 · count 2)",
           r.get("dup") == "exact" and r.get("count") == 2 and g.status_code == 200 and g.content == img("A")
           and _rows("SELECT on_disk FROM ocr_img WHERE id=?", (a["id"],)) == [(1,)], "%s %s" % (r, g.status_code))
        OL.OCR_DISK_CAP = len(img("A")) + 10
        sub("rs", "C", dh="0f0f0f0f0f0f0f0f", pc="PC-M2")                         # 다시 비움(대기 B → 나쁨 A 파일)
        ok("M2-c [지킴] 준비: A 파일이 다시 비워졌다", _rows("SELECT on_disk FROM ocr_img WHERE id=?", (a["id"],)) == [(0,)])
        OL.OCR_DISK_CAP = 10 ** 9
        OL.OCR_DISK_HARD_CAP = _rows("SELECT SUM(nbytes) FROM ocr_img WHERE on_disk=1")[0][0]   # 되살릴 자리가 없다
        r = sub("rs", "A", dh="0000000000000000", pc="PC-M2")
        ok("M2-d [지킴] 전체가 가득이면 되살리기만 건너뛴다(200 · count 는 센다 · 507 아님 · 파일은 그대로 없음)",
           r.status_code == 200 and r.json().get("count") == 3 and _rows("SELECT on_disk FROM ocr_img WHERE id=?", (a["id"],)) == [(0,)],
           "%s %s" % (r.status_code, r.text[:80]))


DH6 = ["0000000000000000", "ffffffffffffffff", "00000000ffffffff", "ffffffff00000000", "0f0f0f0f0f0f0f0f", "f0f0f0f0f0f0f0f0"]


def t_refute_ports():
    for name, fn in (("RF1", rf1_near_not_exact), ("RF2", rf2_epoch), ("RF3", rf3_cleared_survives_eviction), ("RF4", rf4_big_ids),
                     ("RF5", rf5_captcha_anywhere), ("RF6", rf6_tenant_dirs), ("RF7", rf7_tenant_scoped_eviction), ("RF8", rf8_rate_key),
                     ("RF10", rf10_cleared_near), ("M1", m1_invisible_label), ("M2", m2_restore_on_resubmit)):
        _guard(name, fn)
    _reset_rate()


async def t_refute_ports_async():
    try:
        await rf9_chunked_cap()
    except Exception as e:                       # noqa: BLE001
        ok("RF9 예외 없이 끝나야 한다", False, "%s: %s" % (type(e).__name__, str(e)[:200]))
    _reset_rate()


def v2_cluster_row_cap():
    """v4 반증 2차(아이온2) — 라벨 된 묶음에 비슷한(near) 이미지가 계속 오면 ocr_img 줄이 끝없이 쌓였다. 묶음당 줄 상한."""
    _reset_rate()
    first = sub("capsite", "cap-rep", dh="00000000000000f0", pc="PC-CAP").json()
    cid = first["cluster"]
    _lab(cid, "정답")
    last = None
    for i in range(200):
        if i % 25 == 0:
            _reset_rate()
        dh = "%016x" % (0xf0 ^ (1 << (i % 3)))            # 대표와 거리 1 — 전부 같은 묶음(near)
        last = sub("capsite", "cap-%d" % i, dh=dh, pc="PC-CAP")
    n_rows = _rows("SELECT COUNT(*) FROM ocr_img WHERE cluster_id=?", (cid,))[0][0]
    ok("V2-a ★old: near 200장을 넣어도 그 묶음의 줄 수는 상한(OCR_CLUSTER_ROWS_MAX) 이하★",
       n_rows <= OCR_CLUSTER_ROWS_MAX_T(), "rows=%s cap=%s" % (n_rows, OCR_CLUSTER_ROWS_MAX_T()))
    j = last.json() if last is not None else {}
    ok("V2-b 상한 뒤 제출도 200 · 같은 묶음 · 라벨은 힌트(near)로만 · capped 표시",
       last is not None and last.status_code == 200 and j.get("cluster") == cid and j.get("label_match") == "near"
       and j.get("capped") is True, str(j)[:200])
    tot = _rows("SELECT COALESCE(SUM(count),0) FROM ocr_img WHERE cluster_id=?", (cid,))[0][0]
    ok("V2-c 제출 수는 그대로 센다(201 = 대표 1 + near 200)", tot == 201, str(tot))
    files = _rows("SELECT COUNT(*) FROM ocr_img WHERE cluster_id=? AND on_disk=1", (cid,))[0][0]
    ok("V2-d 파일도 줄 수만큼만(상한 뒤엔 새 파일 없음)", files <= OCR_CLUSTER_ROWS_MAX_T(), str(files))
    _reset_rate()
    j = sub("capsite", "cap-rep-twin", dh="00000000000000f0", pc="PC-CAP").json()
    ok("V2-e 상한 뒤 대표와 dHash 가 같은(바이트는 다른) 이미지 — 대표 줄에 세도 label_match 는 near(정답 아님)",
       j.get("capped") is True and j.get("cluster") == cid and j.get("label_match") == "near", str(j)[:200])
    _reset_rate()


def v2_capped_does_not_evict():
    """v4 델타 반증(아이온2 d3_ocr) — 상한에 걸린 제출은 파일을 안 쓰는데도 ⑥ 비우기가 먼저 돌아 ★남의 대기 묶음을 통째로 지웠다★."""
    SZ = 10000
    with fresh_store():
        OL.OCR_DISK_CAP = 10 ** 9
        OL.OCR_DISK_HARD_CAP = 10 ** 9
        _reset_rate()
        a = sub("cx", "A", dh="ffffffffffffffff", pc="PC-CX", raw=img("A", n=SZ)).json()     # 가장 오래된 대기 묶음
        b = sub("cx", "B0", dh="0000000000000000", pc="PC-CX", raw=img("B0", n=100)).json()
        for i in range(1, OCR_CLUSTER_ROWS_MAX_T()):
            if i % 25 == 0:
                _reset_rate()
            sub("cx", "B%d" % i, dh="0000000000000001", pc="PC-CX", raw=img("B%d" % i, n=100))
        n_b = _rows("SELECT COUNT(*) FROM ocr_img WHERE cluster_id=?", (b["cluster"],))[0][0]
        used = _rows("SELECT SUM(nbytes) FROM ocr_img WHERE on_disk=1")[0][0]
        OL.OCR_DISK_CAP = used + 100                  # 새 파일(SZ) 한 장이면 대기를 비워야 하는 자리
        _reset_rate()
        j = sub("cx", "hot", dh="0000000000000003", pc="PC-CX", raw=img("hot", n=SZ)).json()
        left = {r[0] for r in _rows("SELECT id FROM ocr_cluster")}
        ok("V2-f ★old: 꽉 찬 묶음(%d줄)에 센 제출(capped)은 비우기를 안 돌린다 — 대기 묶음 A 가 그대로★" % n_b,
           j.get("capped") is True and a["cluster"] in left, "%s left=%s" % (str(j)[:120], left))
        a_disk = _rows("SELECT on_disk FROM ocr_img WHERE cluster_id=?", (a["cluster"],))
        ok("V2-g A 의 파일도 그대로(on_disk=1)", a_disk == [(1,)], str(a_disk))
        n2 = sub("cx", "new", dh="0f0f0f0f0f0f0f0f", pc="PC-CX", raw=img("new", n=SZ)).json()
        left = {r[0] for r in _rows("SELECT id FROM ocr_cluster")}
        ok("V2-h 대조 — 파일을 쓰는 새 묶음은 예전처럼 가장 오래된 대기(A)를 비운다(상한 판 V2-f 가 헛시험이 아니다)",
           n2.get("dup") == "new" and a["cluster"] not in left and n2.get("cluster") in left, "%s left=%s" % (n2, left))
    _reset_rate()


def OCR_CLUSTER_ROWS_MAX_T():
    return getattr(OL, "OCR_CLUSTER_ROWS_MAX", 64)


def test_all():
    run_all([t_auth, t_captcha, t_validate, t_dedup_exact, t_near_cluster_and_korean, t_bad_undo_skip_history,
             t_size_and_rate, t_disk_evict, t_stats, t_cursor, t_tenant, t_traversal, t_fv, t_refute_ports, t_refute_ports_async,
             t_page_js, v2_cluster_row_cap, v2_capped_does_not_evict])
    finish("test_ocr_label", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
