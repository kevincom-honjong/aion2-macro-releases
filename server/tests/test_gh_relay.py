# -*- coding: utf-8 -*-
"""[대시보드] #255 (2026-09-26 주인님 «서버 중계로 바꾼 뒤 비공개») — 릴리스 저장소 비공개 대비 GitHub 중계.

  ① 서버의 GitHub 읽기에만 GH_READ_TOKEN(헤더) — jsDelivr·S3 등 다른 호스트에는 절대 안 싣는다 · 값은 어디에도 안 나간다
  ② /check 가 exe·업데이터를 ★서버의 서명된 기한부 주소★ /dl/<tok>/<asset> 로 준다(끝 조각 = 에셋 이름 → 내부망 시드 그대로)
  ③ /dl 은 서명·기한·지금 판(sha256) 셋 다 맞아야 준다 · ETag/304 · 디스크 캐시는 sha 가 맞는 파일만 · 24대 동시에도 상류 1번
  ④ DL_RELAY=0 이면 옛 GitHub 주소(되돌리는 손잡이)

    cd updater/server && python -X utf8 tests/test_gh_relay.py
"""
import hashlib
import io
import os
import re
import tempfile
import threading
import time
import contextlib

import httpx
from fastapi.testclient import TestClient

from _harness import main, ok, run_all, finish   # noqa: E402
import gh_relay as GHR                              # noqa: E402

MIN_CHECKS = 55
C = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
EXE = b"MZ-fake-macro-exe-" * 1000
UPD = b"MZ-fake-updater-" * 500
SHA_EXE = hashlib.sha256(EXE).hexdigest()
SHA_UPD = hashlib.sha256(UPD).hexdigest()
TOK = "ghp_TESTTOKEN_do_not_leak_1234567890"
VER = {"exe": {"version": "1.1.1013", "sha256": SHA_EXE},
       "rental": {"version": "1.1.1013", "sha256": SHA_EXE},
       "updater": {"version": "9.9.9", "sha256": SHA_UPD,
                   "download_url": "https://raw.githubusercontent.com/kevincom-honjong/aion2-macro-releases/main/exe/updater.exe"},
       "images": {}}


def set_ver():
    main._version_cache["data"] = dict(VER)
    main._version_cache["ts"] = time.time()


def check(headers=None):
    set_ver()
    h = {"X-Api-Key": "testkey"}
    h.update(headers or {})
    return C.post("/check", json={"exe_version": "1.1.1000", "updater_version": "0.0.1", "image_hashes": {}}, headers=h)


def t_headers():
    keep = GHR.GH_READ_TOKEN
    GHR.GH_READ_TOKEN = TOK
    try:
        gh = [GHR.gh_headers(u).get("Authorization") for u in (
            "https://api.github.com/repos/x/y", "https://raw.githubusercontent.com/x/y/main/a", "https://github.com/x/y/releases/download/v1/a")]
        other = [GHR.gh_headers(u).get("Authorization") for u in (
            "https://cdn.jsdelivr.net/gh/x/y@main/a", "https://objects.githubusercontent.com/github-production-release-asset/1",
            "https://github-releases.githubusercontent.com/x", "https://evil.com/?h=github.com", "https://api.github.com.evil.com/x")]
        ok("G-1 토큰은 GitHub 세 호스트에만 실린다", gh == ["Bearer " + TOK] * 3, str(gh))
        ok("G-2 ★jsDelivr·S3 서명 주소·흉내 호스트에는 절대 안 실린다★", other == [None] * 5, str(other))
        ok("G-3 token_state 는 값이 아니라 set/unset", GHR.token_state() == "set", "")
    finally:
        GHR.GH_READ_TOKEN = keep


def t_sign():
    sec = b"k" * 32
    t = GHR.sign(sec, "macro-1.1.1013.exe", now=1000.0)
    ok("G-4 서명 맞음 → 통과", GHR.verify(sec, "macro-1.1.1013.exe", t, now=1001.0), t)
    ok("G-5 다른 에셋 이름에는 안 맞는다", not GHR.verify(sec, "rental-1.1.1013.exe", t, now=1001.0), "")
    ok("G-6 기한 지나면 거부", not GHR.verify(sec, "macro-1.1.1013.exe", t, now=1000.0 + GHR.DL_TTL_S + 1), "")
    bad = t[:-2] + ("AA" if not t.endswith("AA") else "BB")
    ok("G-7 한 글자 바꾼 서명·다른 비밀·쓰레기는 거부",
       not GHR.verify(sec, "macro-1.1.1013.exe", bad, now=1001.0) and not GHR.verify(b"z" * 32, "macro-1.1.1013.exe", t, now=1001.0)
       and not GHR.verify(sec, "macro-1.1.1013.exe", "zz", now=1001.0), "")


def _seed_cache(d):
    os.makedirs(d, exist_ok=True)
    for asset, data, sha in (("macro-1.1.1013.exe", EXE, SHA_EXE), ("updater.exe", UPD, SHA_UPD)):
        open(GHR.cached_path(d, asset, sha), "wb").write(data)


async def t_check_and_dl():
    d = tempfile.mkdtemp(prefix="dl_")
    keep = main.DL_CACHE_DIR
    main.DL_CACHE_DIR = d
    _seed_cache(d)
    try:
        r = check()
        j = r.json()
        eu = (j.get("exe_update") or {}).get("download_url", "")
        uu = (j.get("updater_update") or {}).get("download_url", "")
        ok("G-8 /check exe 주소 = 서버 /dl/<서명>/macro-<ver>.exe (GitHub 아님)",
           r.status_code == 200 and re.match(r"^https?://[^/]+/dl/[0-9a-f]+\.[A-Za-z0-9_-]+/macro-1\.1\.1013\.exe$", eu) is not None
           and "github" not in eu, eu)
        ok("G-9 ★끝 조각 = 에셋 이름 그대로★ — 업데이터가 만드는 내부망 시드 주소가 안 바뀐다", eu.rsplit("/", 1)[-1] == "macro-1.1.1013.exe", eu)
        ok("G-10 업데이터 자가업데이트 주소도 서버 /dl/…/updater.exe (version.json 의 raw 주소가 아님)",
           re.search(r"/dl/[^/]+/updater\.exe$", uu) is not None and "githubusercontent" not in uu, uu)
        ok("G-11 sha256 은 그대로 실린다(업데이터·시드 검증)", j["exe_update"].get("sha256") == SHA_EXE, "")
        path = "/" + eu.split("/", 3)[3]
        g = C.get(path)
        ok("G-12 /dl 서명 주소 → 200 · 바이트 그대로 · ETag=sha256", g.status_code == 200 and g.content == EXE
           and g.headers.get("etag") == '"%s"' % SHA_EXE, "%s %s" % (g.status_code, g.headers.get("etag")))
        g2 = C.get(path, headers={"If-None-Match": '"%s"' % SHA_EXE})
        ok("G-13 If-None-Match 같으면 304(본문 없음)", g2.status_code == 304 and not g2.content, str(g2.status_code))
        gu = C.get("/" + uu.split("/", 3)[3])
        ok("G-14 업데이터도 받는다", gu.status_code == 200 and gu.content == UPD, str(gu.status_code))
        tok = path.split("/")[2]
        bad = [C.get(p).status_code for p in (
            "/dl/%s/rental-1.1.1013.exe" % tok,                       # 서명이 다른 이름 것
            "/dl/%s.zz/macro-1.1.1013.exe" % tok,                     # 서명 훼손
            "/dl/%s/macro-1.1.1012.exe" % GHR.sign(main.DL_SIGN_SECRET, "macro-1.1.1012.exe"),   # 지금 판이 아님
            "/dl/%s/..%%2fmain.py" % tok,
            "/dl/%s/main.py" % GHR.sign(main.DL_SIGN_SECRET, "main.py"))]
        ok("G-15 다른 이름·훼손·옛 판·경로 조작·exe 아닌 이름은 전부 404", bad == [404] * 5, str(bad))
        old = GHR.sign(main.DL_SIGN_SECRET, "macro-1.1.1013.exe", now=time.time() - GHR.DL_TTL_S - 10)
        ok("G-16 기한 지난 서명 404", C.get("/dl/%s/macro-1.1.1013.exe" % old).status_code == 404, "")
        r = check({"X-Forwarded-Proto": "https"})
        ok("G-17 앞단이 https 면 주소도 https(서명을 평문에 안 싣는다)",
           r.json()["exe_update"]["download_url"].startswith("https://"), r.json()["exe_update"]["download_url"][:40])
        nk = C.post("/check", json={"exe_version": "1.1.1000", "updater_version": "9.9.9"})
        ok("G-18 키 없는 /check 에는 exe 주소 자체가 없다(예전 규칙 그대로 — 서명도 안 나간다)", "exe_update" not in nk.json(), str(nk.json())[:120])
        nku = (nk.json().get("updater_update") or {}).get("download_url", "")
        set_ver()
        nr = C.post("/check", json={"exe_version": "1.1.1000", "updater_version": "0.0.1", "edition": "rental"}).json()
        nre = (nr.get("exe_update") or {}).get("download_url", "")
        set_ver()
        nk2 = C.post("/check", json={"exe_version": "1.1.1000", "updater_version": "0.0.1"}).json()
        nku = (nk2.get("updater_update") or {}).get("download_url", "")
        ok("G-18b ★키 없는 요청엔 서명 주소를 안 준다(반증 2차 #3)★ — 업데이터·자칭 rental 은 예전 GitHub 주소 그대로",
           nku.startswith("https://raw.githubusercontent.com/") and nre.startswith("https://github.com/")
           and "/dl/" not in nku + nre, "%s | %s" % (nku[:60], nre[:60]))
        _old = main.SESSION_SECRET
        main.SESSION_SECRET = os.urandom(32)          # 세션 비밀이 바뀌어도(재배포) 서명은 그대로
        try:
            ok("G-18c 서명은 SESSION_SECRET 이 아니라 DL_SIGN_SECRET — 세션 비밀이 바뀌어도 받던 주소가 산다",
               C.get(path, headers={"If-None-Match": '"%s"' % SHA_EXE}).status_code == 304, "")
        finally:
            main.SESSION_SECRET = _old
        main.DL_RELAY = False
        try:
            r = check()
            ok("G-19 DL_RELAY=0 → 옛 GitHub 릴리스 주소(되돌리는 손잡이)",
               r.json()["exe_update"]["download_url"].startswith("https://github.com/kevincom-honjong/aion2-macro-releases/releases/download/v1.1.1013/"),
               r.json()["exe_update"]["download_url"])
        finally:
            main.DL_RELAY = True
    finally:
        main.DL_CACHE_DIR = keep


def t_ensure():
    d = tempfile.mkdtemp(prefix="dlens_")
    calls = []

    def handler(req):
        calls.append(str(req.url))
        time.sleep(0.05)
        if "bad" in str(req.url):
            return httpx.Response(200, content=b"tampered")
        if "down" in str(req.url):
            return httpx.Response(503, content=EXE)   # 본문이 맞아도 200 이 아니면 안 받는다
        return httpx.Response(200, content=EXE)

    keep = GHR.upstreams
    GHR.upstreams = lambda client, asset, api_timeout=20.0: [("down", "https://x/down", {}), ("bad", "https://x/bad", {}), ("good", "https://x/good", {})]
    try:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        res = []
        ths = [threading.Thread(target=lambda: res.append(GHR.ensure(d, "macro-1.1.1013.exe", SHA_EXE, client=client, log=lambda m: None)))
               for _ in range(8)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        ok("G-20 ★8대 동시★ — 상류는 한 번씩만(down·bad·good 3회), 전원 같은 파일", len(calls) == 3 and all(p for p, _ in res)
           and len({p for p, _ in res}) == 1, "%d %s" % (len(calls), res[:2]))
        ok("G-21 sha 가 틀린 상류(bad)는 버리고 다음 상류", open(res[0][0], "rb").read() == EXE, "")
        ok("G-22 .part 찌꺼기가 안 남는다", not [f for f in os.listdir(d) if f.endswith(".part")], str(os.listdir(d)))
        n = len(calls)
        p2, why = GHR.ensure(d, "macro-1.1.1013.exe", SHA_EXE, client=client, log=lambda m: None)
        ok("G-23 캐시 적중이면 상류를 안 부른다", why == "hit" and len(calls) == n, why)
        GHR.upstreams = lambda client, asset, api_timeout=20.0: [("bad", "https://x/bad", {})]
        p3, why3 = GHR.ensure(d, "updater.exe", SHA_UPD, client=client, log=lambda m: None)
        ok("G-24 모든 상류가 sha 불일치면 None(틀린 파일을 절대 캐시·서빙 안 함)", p3 is None and "sha" in why3
           and not [f for f in os.listdir(d) if "updater" in f], "%s %s" % (p3, why3))
        ok("G-25 이름·sha 형식이 이상하면 상류를 안 부른다", GHR.ensure(d, "../x.exe", SHA_EXE, client=client)[0] is None
           and GHR.ensure(d, "macro-1.exe", "zz", client=client)[0] is None, "")
    finally:
        GHR.upstreams = keep


def t_upstreams_and_leak():
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers.get("authorization")))
        if "/releases/tags/" in str(req.url):
            return httpx.Response(200, json={"assets": [{"name": "macro-1.1.1013.exe", "url": "https://api.github.com/repos/r/releases/assets/7"}]})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    keep = GHR.GH_READ_TOKEN
    GHR.GH_READ_TOKEN = TOK
    try:
        up = GHR.upstreams(client, "macro-1.1.1013.exe")
        ok("G-26 토큰이 있으면 비공개 경로(릴리스 API · octet-stream)가 먼저, 공개 주소는 뒤",
           [n for n, _, _ in up] == ["release-api", "release-public"] and up[0][2].get("Accept") == "application/octet-stream", str([n for n, _, _ in up]))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            p, why = GHR.ensure(tempfile.mkdtemp(prefix="dlk_"), "macro-1.1.1013.exe", SHA_EXE, client=client)
        ok("G-27 ★실패 로그·사유에 토큰이 안 나온다★", p is None and TOK not in buf.getvalue() and TOK not in why, buf.getvalue()[:160])
        h = C.get("/health").json()
        ok("G-28 /health 는 gh_token='set' 만(값 없음) · dl_relay", h.get("gh_token") == "set" and TOK not in str(h) and h.get("dl_relay") is True, str({k: h.get(k) for k in ("gh_token", "dl_relay")}))
        _kd = main.DL_CACHE_DIR
        main.DL_CACHE_DIR = tempfile.mkdtemp(prefix="dlc_")
        _seed_cache(main.DL_CACHE_DIR)          # 채워 둔 캐시 — 미리 채우기가 진짜 GitHub 을 부르지 않게
        try:
            body = check().text
        finally:
            main.DL_CACHE_DIR = _kd
        ok("G-29 /check 응답에도 토큰이 없다", TOK not in body, "")
    finally:
        GHR.GH_READ_TOKEN = keep
    GHR.GH_READ_TOKEN = ""
    try:
        up = GHR.upstreams(client, "macro-1.1.1013.exe")
        ok("G-30 토큰이 없으면(지금 공개) 공개 릴리스 주소만 · 인증 헤더 없음", [n for n, _, _ in up] == ["release-public"] and not up[0][2], str(up))
    finally:
        GHR.GH_READ_TOKEN = keep


async def t_refute_fixes():
    """반증 1차(2026-09-26) 고친 것 — M1 HEAD · M2 채우기 하나·실패 60초·전체 상한 · M3 IP 상한 · L3 깨진 JSON."""
    import asyncio
    d = tempfile.mkdtemp(prefix="dlr_")
    keep_dir, keep_ens = main.DL_CACHE_DIR, GHR.ensure
    main.DL_CACHE_DIR = d
    _seed_cache(d)
    try:
        set_ver()
        path = "/dl/%s/macro-1.1.1013.exe" % GHR.sign(main.DL_SIGN_SECRET, "macro-1.1.1013.exe")
        h = C.head(path)
        ok("G-31 HEAD → 200 · 본문 없음 · ETag (배포 확인 스크립트 requests.head)", h.status_code == 200 and not h.content
           and h.headers.get("etag") == '"%s"' % SHA_EXE, str(h.status_code))
        calls = []

        def slow_ok(cache_dir, asset, sha, client=None, log=print):
            calls.append(asset)
            time.sleep(0.2)
            return os.path.join(cache_dir, "x"), "good"
        GHR.ensure = slow_ok
        rs = await asyncio.gather(*[main._dl_get("rental-1.1.1013.exe", "a" * 64) for _ in range(10)])
        ok("G-32 ★빈 캐시에 10개 동시★ — 채우기(스레드) 한 번, 모두 같은 답", len(calls) == 1 and len(set(rs)) == 1, "%d %s" % (len(calls), rs[0]))
        calls.clear()

        def fail(cache_dir, asset, sha, client=None, log=print):
            calls.append(asset)
            return None, "release-public:404"
        GHR.ensure = fail
        r1 = await main._dl_get("rental-1.1.1013.exe", "b" * 64)
        r2 = await main._dl_get("rental-1.1.1013.exe", "b" * 64)
        ok("G-33 실패는 60초 동안 곧바로 None — 상류를 다시 안 부른다", r1[0] is None and r2 == (None, "recent-fail") and len(calls) == 1, "%s %s %d" % (r1, r2, len(calls)))

        def boom(*a, **k):
            raise RuntimeError("x")
        GHR.ensure = boom
        r3 = await main._dl_get("rental-1.1.1013.exe", "c" * 64)
        ok("G-34 채우기가 예외를 던져도 None(이름만) · 기다리던 표가 안 남는다", r3 == (None, "RuntimeError") and ("rental-1.1.1013.exe", "c" * 64) not in main._DL_INFLIGHT
           and not [k for k in main._DL_INFLIGHT if k[0] == "rental-1.1.1013.exe"], str(r3))
        ok("G-34b ★시험이 진짜 GitHub 을 부르지 않았다★(미리 채우기 표가 비었다)", not main._DL_INFLIGHT, str(list(main._DL_INFLIGHT)))
    finally:
        GHR.ensure = keep_ens
        main.DL_CACHE_DIR = keep_dir
    keep_n = main.DL_RATE_N
    main.DL_RATE_N = 3
    main._DL_HITS.clear()
    try:
        tok = GHR.sign(main.DL_SIGN_SECRET, "updater.exe")
        codes = [C.get("/dl/%s/updater.exe" % tok, headers={"If-None-Match": '"%s"' % SHA_UPD}).status_code for _ in range(4)]
        ok("G-35 한 IP 가 창 안에서 상한을 넘으면 429", codes == [304, 304, 304, 429], str(codes))
        ok("G-36 ★함대 한 IP 여유★ — 기본 상한 ≥ 24대×(exe+업데이터)×재시도 4", keep_n >= 24 * 2 * 4, str(keep_n))
    finally:
        main.DL_RATE_N = keep_n
        main._DL_HITS.clear()
    keep_dl = GHR.DL_DEADLINE_S
    GHR.DL_DEADLINE_S = -1
    GHR.upstreams, keep_up = (lambda client, asset, api_timeout=20.0: [("good", "https://x/good", {})]), GHR.upstreams
    try:
        c = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=EXE)))
        p, why = GHR.ensure(tempfile.mkdtemp(prefix="dld_"), "macro-1.1.1013.exe", SHA_EXE, client=c, log=lambda m: None)
        ok("G-37 전체 상한을 넘으면 그만(업데이터 180초 첫 바이트 안에 502 로 끝난다)", p is None and "deadline" in why, why)
        GHR.DL_DEADLINE_S = 1.0

        def slow():
            for i in range(20):
                time.sleep(0.1)
                yield EXE[i * 900:(i + 1) * 900]
        c = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=slow())))
        d = tempfile.mkdtemp(prefix="dld2_")
        p, why = GHR.ensure(d, "macro-1.1.1013.exe", SHA_EXE, client=c, log=lambda m: None)
        ok("G-37b 받는 도중(느린 상류)에도 전체 상한에서 끊는다 · .part 안 남김", p is None and "TimeoutError" in why
           and not os.listdir(d), "%s %s" % (why, os.listdir(d)))
    finally:
        GHR.DL_DEADLINE_S = keep_dl
        GHR.upstreams = keep_up
    keep_t = GHR.GH_READ_TOKEN
    GHR.GH_READ_TOKEN = TOK
    try:
        c = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"<html>not json")))
        up = GHR.upstreams(c, "macro-1.1.1013.exe")
        ok("G-38 릴리스 API 가 JSON 이 아니어도 예외 없이 공개 주소로", [n for n, _, _ in up] == ["release-public"], str(up))
    finally:
        GHR.GH_READ_TOKEN = keep_t


def t_prewarm():
    """/check 가 새 판을 광고할 때 exe·업데이터 캐시를 미리 채운다(진짜 함수 · ensure 는 가짜)."""
    calls = []

    def fake(cache_dir, asset, sha, client=None, log=print):
        calls.append((asset, sha))
        return None, "fake"
    keep_e, keep_p, keep_d = GHR.ensure, main._dl_prewarm, main.DL_CACHE_DIR
    GHR.ensure, main._dl_prewarm = fake, main._dl_prewarm_real
    main.DL_CACHE_DIR = tempfile.mkdtemp(prefix="dlp_")
    main._DL_FAIL.clear()
    try:
        check()
        t0 = time.time()
        while len(calls) < 2 and time.time() - t0 < 5:
            time.sleep(0.05)
        ok("G-39 /check 한 번 → exe·업데이터 둘 다 미리 채우기(sha 까지 맞게)",
           sorted(calls) == sorted([("macro-1.1.1013.exe", SHA_EXE), ("updater.exe", SHA_UPD)]), str(calls))
        _seed_cache(main.DL_CACHE_DIR)
        n = len(calls)
        main._DL_FAIL.clear()
        check()
        time.sleep(0.3)
        ok("G-40 이미 캐시에 있으면 미리 채우기를 안 한다", len(calls) == n, str(len(calls) - n))
    finally:
        GHR.ensure, main._dl_prewarm, main.DL_CACHE_DIR = keep_e, keep_p, keep_d
        main._DL_FAIL.clear()


def t_refute2():
    """반증 2차(아이온2 2026-09-26) — #1 멈춘 상류·시간초과 · #2 서명 키 · #4 경로 가드·sha 캐시 이름 · #5 접근 로그."""
    import asyncio
    import logging
    import socket
    # #1 멈춘 상류: 헤더와 조금 보내고 멈추는 진짜 소켓 — 전체 상한 안에서 끝나야 한다(예전엔 읽기 한 번에 120초)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        conns = []
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                continue
            c.recv(4096)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n" + b"x" * 100)
            conns.append(c)                 # 닫지 않고 멈춘다
        for c in conns:
            c.close()
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    keep_dl, keep_up = GHR.DL_DEADLINE_S, GHR.upstreams
    GHR.DL_DEADLINE_S = 1.5
    GHR.upstreams = lambda client, asset, api_timeout=20.0: [("stall", "http://127.0.0.1:%d/x" % port, {})]
    try:
        d = tempfile.mkdtemp(prefix="dls_")
        t0 = time.monotonic()
        p, why = GHR.ensure(d, "macro-1.1.1013.exe", SHA_EXE, log=lambda m: None)
        el = time.monotonic() - t0
        ok("G-41 ★멈춘 상류도 전체 상한 안에서 끝난다★(상한 1.5초 → 3초 안, 예전 판은 읽기 한 번 120초)",
           p is None and el < 3.0 and "Timeout" in why and not os.listdir(d), "%.2fs %s" % (el, why))
    finally:
        stop.set()
        th.join(2)
        srv.close()
        GHR.DL_DEADLINE_S, GHR.upstreams = keep_dl, keep_up

    async def _async_part():
        keep_e = GHR.ensure
        calls = []
        try:
            def to(cache_dir, asset, sha, client=None, log=print):
                calls.append(1)
                return None, "stall:ReadTimeout"
            GHR.ensure = to
            main._DL_FAIL.clear()
            await main._dl_get("rental-1.1.1013.exe", "d" * 64)
            await main._dl_get("rental-1.1.1013.exe", "d" * 64)
            ok("G-42 시간 초과는 실패로 잡아 두지 않는다 — 다음 시도가 곧바로 다시 채운다", len(calls) == 2, str(len(calls)))

            def slow(cache_dir, asset, sha, client=None, log=print):
                time.sleep(0.6)
                return None, "x:404"
            GHR.ensure = slow
            keep_dd = GHR.DL_DEADLINE_S
            GHR.DL_DEADLINE_S = -19.8                 # 기다림 상한 = 상한 + 20 = 0.2초
            try:
                r = await main._dl_get("rental-1.1.1013.exe", "e" * 64)
            finally:
                GHR.DL_DEADLINE_S = keep_dd
            ok("G-43 기다리는 요청은 상한+20초에 끝난다(채우기는 계속)", r == (None, "wait-timeout"), str(r))
            await asyncio.sleep(0.8)
        finally:
            GHR.ensure = keep_e
            main._DL_FAIL.clear()
    return _async_part()     # run_all 이 코루틴을 기다린다


def t_refute2_sync():
    # #2 서명 키 — 볼륨 파일은 한 번 만들고 그 뒤로 같은 값 · env 가 있으면 env
    d = tempfile.mkdtemp(prefix="dlk2_")
    kf = os.path.join(d, "sub", "dl_sign.key")
    keep_env = main._SESSION_SECRET_ENV
    main._SESSION_SECRET_ENV = ""
    os.environ.pop("DL_SIGN_KEY", None)
    try:
        k1 = main._dl_sign_key(kf)
        k2 = main._dl_sign_key(kf)
        ok("G-44 ★서명 키는 볼륨 파일로 재부팅에도 같다★(SESSION_SECRET 미설정)", k1 == k2 and len(k1) == 32 and os.path.isfile(kf), "")
        main._SESSION_SECRET_ENV = "set"
        ok("G-44b SESSION_SECRET 이 설정돼 있으면 그것", main._dl_sign_key(kf) == main.SESSION_SECRET, "")
        os.environ["DL_SIGN_KEY"] = "abc"
        ka = main._dl_sign_key(kf)
        ok("G-44c DL_SIGN_KEY 가 먼저 · 늘 같은 값", ka == main._dl_sign_key(kf) and ka not in (k1, main.SESSION_SECRET), "")
    finally:
        main._SESSION_SECRET_ENV = keep_env
        os.environ.pop("DL_SIGN_KEY", None)
    # #4 경로 가드 — sha 가 있다고 쳐도(expected_sha 무력화) 이상한 이름은 채우기 전에 404
    bad_names = ["..\\x.exe", "x.exe", "macro-1.1.1013.exe.bak", "macro-1.1.1013.EXE", "updater.exe.part", "macro-..exe", ".env"]
    ok("G-45 ASSET_RE 는 함대 이름 셋 꼴만", all(not GHR.ASSET_RE.match(n) for n in bad_names)
       and all(GHR.ASSET_RE.match(n) for n in ("macro-1.1.1013.exe", "rental-1.1.1013.exe", "updater.exe")), "")
    calls = []
    keep_sha, keep_e = GHR.expected_sha, GHR.ensure
    GHR.expected_sha = lambda ver, asset: SHA_EXE
    GHR.ensure = lambda *a, **k: (calls.append(a[1]), (None, "x"))[1]
    main._DL_FAIL.clear()
    try:
        codes = [C.get("/dl/%s/%s" % (GHR.sign(main.DL_SIGN_SECRET, n), n.replace("\\", "%5C"))).status_code
                 for n in ("..\\x.exe", "x.exe", "macro-1.1.1013.exe.bak", ".env")]
        ok("G-46 ★dl_asset 의 이름 가드★ — 서명이 맞고 sha 가 있어도 이상한 이름은 404 · 채우기 0번",
           codes == [404] * 4 and not calls, "%s %s" % (codes, calls))
    finally:
        GHR.expected_sha, GHR.ensure = keep_sha, keep_e
    # #4 캐시 이름이 sha 로 갈린다 — 옛 updater.exe 가 남아 있어도 새 sha 에 적중하지 않는다
    d = tempfile.mkdtemp(prefix="dlsha_")
    old = GHR.cached_path(d, "updater.exe", "0" * 64)
    os.makedirs(d, exist_ok=True)
    open(old, "wb").write(b"OLD")
    keep_up = GHR.upstreams
    GHR.upstreams = lambda client, asset, api_timeout=20.0: [("good", "https://x/good", {})]
    try:
        c = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=UPD)))
        p, why = GHR.ensure(d, "updater.exe", SHA_UPD, client=c, log=lambda m: None)
        ok("G-47 ★옛 updater.exe 캐시에 적중하지 않는다★(이름이 sha 로 갈림) — 새 파일을 받는다",
           p and p != old and why == "good" and open(p, "rb").read() == UPD, "%s %s" % (p, why))
        ok("G-47b 같은 이름·다른 sha 는 다른 캐시 경로", GHR.cached_path(d, "updater.exe", SHA_UPD) != old, "")
    finally:
        GHR.upstreams = keep_up
    # #5 접근 로그 가림
    import logging
    rec = logging.LogRecord("uvicorn.access", 20, "", 0, '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", "/dl/1a2b.XyZ_-9/macro-1.1.1013.exe", "1.1", 200), None)
    flt = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, main._DlTokMask)]
    for f in flt:
        f.filter(rec)
    ok("G-48 ★접근 로그에 서명이 안 남는다★ — uvicorn.access 에 가림막이 달려 있고 /dl/…/ 로 바뀐다",
       flt and rec.getMessage() == '1.2.3.4:5 - "GET /dl/…/macro-1.1.1013.exe HTTP/1.1" 200', rec.getMessage())


def test_all():
    run_all([t_headers, t_sign, t_check_and_dl, t_ensure, t_upstreams_and_leak, t_refute_fixes, t_prewarm, t_refute2, t_refute2_sync])
    finish("test_gh_relay", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
