# -*- coding: utf-8 -*-
"""[대시보드] #271 (2026-09-27 주인님) — 버그스샷이 쓸모없이 쌓인다 · 라이브를 안 보는데 WS 가 나간다.

  R  버그스샷 정리: 사고 증거 48시간(최신 6 은 남김) · 수확 재료 (PC,종류) 최신 6 영구 · 학습 120장+7일 · 핀은 안 지움
  W  숨은 대시보드(탭 뒤·최소화·크기 0 iframe)엔 상태·로그를 안 보낸다 · 알림·ping 은 보낸다 · 보이면 전량 한 번

    cd updater/server && python -X utf8 tests/test_bug_retention_ws.py
"""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from _harness import main, ok, run_all, finish, WebSocketDisconnect   # noqa: E402

MIN_CHECKS = 32
C = TestClient(main.app, raise_server_exceptions=False)
NOW = time.time()
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def nm(pc, hours_ago, tag, i=0):
    t = datetime.fromtimestamp(NOW - hours_ago * 3600 - i, timezone.utc)
    return f"{pc}_{t.strftime('%Y%m%d_%H%M%S')}_{pc}_{t.strftime('%Y%m%d_%H%M%S')}_{tag}.png"


def _sess(method, path, **kw):
    C.cookies.set("session", main.new_session("main"))
    try:
        return C.request(method, path, **kw)
    finally:
        C.cookies.clear()


async def t_plan():
    P = main._bug_prune_plan
    old = [nm("PC-01", 72, "stuck", i) for i in range(10)]
    new = [nm("PC-01", 1, "stuck", i) for i in range(2)]
    d = set(P(old + new, NOW))
    ok("R-1 사고 증거는 48시간이 지나면 지운다 — 단 최신 6장은 남긴다", len(d) == 6 and not (d & set(new))
       and d == set(sorted(old)[:6]), str(len(d)))
    fresh = [nm("PC-01", 1, "fail", i) for i in range(45)]
    ok("R-2 신선해도 개수 상한(40)은 그대로", set(P(fresh, NOW)) == set(sorted(fresh)[:5]), "")
    h2 = [nm("PC-01", 24 * 30, "profcard2", i) for i in range(9)]
    h3 = [nm("PC-01", 24 * 30, "profcard3", i) for i in range(3)]
    d = set(P(h2 + h3, NOW))
    ok("R-3 ★수확 재료는 나이와 무관★ — (PC, 종류) 마다 최신 6장(profcard2 9장 → 3장만, profcard3 은 그대로)",
       d == set(sorted(h2)[:3]), str(len(d)))
    c2 = [nm("PC-01", 24 * 60, "plrow_PC-01_2_CAND", i) for i in range(7)]
    c3 = [nm("PC-01", 24 * 60, "plrow_PC-01_3_CAND", i) for i in range(7)]
    ok("R-3b 계정줄 후보 크롭(_CAND)은 두 달 지나도 계정마다 최신 6장", set(P(c2 + c3, NOW)) == {sorted(c2)[0], sorted(c3)[0]}, "")
    L = [nm("PC-01", 1, "ocrlearn_cube_count_7", i) for i in range(130)]
    ok("R-4 학습 크롭은 120장 상한", set(P(L, NOW)) == set(sorted(L)[:10]), "")
    L2 = [nm("PC-01", 24 * 8, "ocrdiff_kina_L1_G2", i) for i in range(5)] + [nm("PC-01", 1, "ocrlearn_x", i) for i in range(15)]
    ok("R-5 학습 크롭은 7일이 지나면 지운다", set(P(L2, NOW)) == set(L2[:5]), "")
    ok("R-6 핀은 안 지운다", sorted(old)[0] not in P(old + new, NOW, frozenset({sorted(old)[0]})), "")
    odd = ["PC-01_x%02d_bug.png" % i for i in range(12)]
    ok("R-7 시각을 못 읽는 이름은 나이로 안 지운다", main._bug_age_s("PC-01_bug.png", NOW) is None
       and P(odd, NOW) == [], "")
    k = main._bug_kind(nm("PC-01", 1, "_lc12-switch-relogin-fail"))
    ok("R-8 사고 증거 이름이 수확 재료로 잘못 묶이지 않는다", k == ("incident", ""), str(k))


async def t_files():
    d = main.tenant_bugs_dir("main")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    mine = [nm("PC-01", 72, "stuck", i) for i in range(10)]
    other = [nm("PC-01b", 72, "stuck", i) for i in range(10)] + [nm("PC-01_sub", 72, "stuck", i) for i in range(10)]
    for f in mine + other:
        open(os.path.join(d, f), "wb").write(PNG)
    n = main._prune_bugs(d, "PC-01")
    left = set(os.listdir(d))
    ok("R-9 업로드 정리는 ★그 pc_id 것만★(PC-01b·PC-01_sub 는 안 건드림)", n == 4 and set(other) <= left, str(n))
    dry = main._bug_sweep_all(dry=True)
    ok("R-10 dry=True 는 세기만 한다", dry["deleted"] == 8 and set(os.listdir(d)) == left
       and dry["by_kind"]["incident"]["n"] == 8, str(dry))
    main._BUG_COUNT_CACHE["main"] = (time.time(), {"x": 1})
    rep = main._bug_sweep_all()
    ok("R-11 실제 훑기는 지우고 캐시를 버린다", rep["deleted"] == 8 and len(os.listdir(d)) == 18
       and "main" not in main._BUG_COUNT_CACHE, str(rep["deleted"]))
    # 업로드 경로
    oldf = nm("PC-02", 72, "stuck", 7)                    # i 가 클수록 오래됐다
    for i in range(8):
        open(os.path.join(d, nm("PC-02", 72, "stuck", i)), "wb").write(PNG)
    r = C.post("/bugs/PC-02", headers={"X-Api-Key": "testkey"},
               files={"file": ("PC-02_20260927_010101_stuck.png", PNG, "image/png")})
    ok("R-12 업로드하면 그 PC 의 오래된 증거가 정리된다(최신 6장만)", r.status_code == 200 and not os.path.exists(os.path.join(d, oldf))
       and len([f for f in os.listdir(d) if f.startswith("PC-02_")]) == 6, r.text[:80])


async def t_pins():
    d = main.tenant_bugs_dir("main")
    f = nm("PC-03", 72, "stuck", 7)                       # 가장 오래된 것(정리 대상)에 핀
    for i in range(8):
        open(os.path.join(d, nm("PC-03", 72, "stuck", i)), "wb").write(PNG)
    ok("R-13 핀·미리보기는 main 세션만", C.post("/bugs/pin/" + f).status_code == 401
       and C.get("/diag/bugs_prune").status_code == 401, "")
    r = _sess("POST", "/bugs/pin/" + f)
    ok("R-14 핀을 꽂으면 settings 에 남는다", r.status_code == 200 and r.json()["pinned"] is True, r.text[:100])
    main._BUG_PINS.clear()
    main._BUG_PINS_LOADED[0] = False                       # 재배포 흉내 — 설정에서 다시 읽는다
    j = _sess("GET", "/diag/bugs_prune").json()
    ok("R-15 ★재배포 뒤에도 핀이 살아 있고 미리보기가 그 파일을 빼고 센다★", f in j["pins"] and j["by_kind"]["incident"]["n"] == 1,
       str(j)[:160])
    main._bug_sweep_all()
    ok("R-16 핀 꽂힌 파일은 훑기가 안 지운다", os.path.exists(os.path.join(d, f)), "")
    ok("R-17 없는 파일엔 핀을 못 꽂는다(404)", _sess("POST", "/bugs/pin/nope.png").status_code == 404, "")
    r = _sess("POST", "/bugs/pin/" + f, params={"on": "0"})
    ok("R-18 핀 풀기", r.json()["pinned"] is False and f not in main._BUG_PINS, "")
    keep = main.get_setting

    async def boom(k):
        raise RuntimeError("db")
    main.get_setting = boom
    main._BUG_PINS_LOADED[0] = False
    try:
        for i in range(8):
            open(os.path.join(d, nm("PC-04", 72, "stuck", i)), "wb").write(PNG)
        r = C.post("/bugs/PC-04", headers={"X-Api-Key": "testkey"},
                   files={"file": ("PC-04_x_stuck.png", PNG, "image/png")})
        n4 = len([x for x in os.listdir(d) if x.startswith("PC-04_")])
    finally:
        main.get_setting = keep
    ok("R-19 ★핀을 못 읽으면 지우지 않는다★(업로드는 성공)", r.status_code == 200 and n4 == 9, str(n4))


class DashWS:
    def __init__(self, hidden=False, embedded=False, ua="UA-test"):
        self.query_params = {"h": "1" if hidden else "0", "e": "1" if embedded else "0"}
        self.headers = {"user-agent": ua, "x-forwarded-for": "1.2.3.4, 10.0.0.1", "origin": "https://x"}
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self.cookies = {"session": main.new_session("main")}
        self.sent = []
        self.inq = asyncio.Queue()

    async def accept(self):
        pass

    async def receive_text(self):
        m = await self.inq.get()
        if m is None:
            raise WebSocketDisconnect(code=1000)
        return m

    async def send_text(self, m):
        self.sent.append(json.loads(m).get("type"))

    async def close(self, code=1000):
        pass


async def _settle():
    for _ in range(40):
        await asyncio.sleep(0.05)


async def t_ws():
    main._PUSH.clear()
    main._FEED.clear()
    a, b = DashWS(), DashWS(hidden=True, embedded=True, ua="FarmView")
    ta = asyncio.create_task(main.websocket_endpoint(a))
    tb = asyncio.create_task(main.websocket_endpoint(b))
    await _settle()
    await main.push_state("main")
    await _settle()
    ok("W-1 보이는 화면은 상태를 받는다", "state" in a.sent, str(a.sent))
    ok("W-1b 상태 전송도 소켓별 tx 에 센다", main.manager.meta[id(a)]["tx"] > 0, "")
    ok("W-2 ★숨은 화면(h=1)은 상태를 안 받는다★", not any(t in ("state", "state_diff") for t in b.sent), str(b.sent))
    a.sent.clear(); b.sent.clear()
    await main.manager.broadcast({"type": "alert", "message": "x"}, "main")
    await main.manager.broadcast({"type": "log", "pc_id": "PC-01", "message": "m"}, "main")
    await main.manager.broadcast({"type": "ping"}, "main")
    await _settle()
    ok("W-3 알림(소리)·ping 은 숨어도 간다", b.sent == ["alert", "ping"], str(b.sent))
    ok("W-4 로그는 보이는 화면에만", a.sent == ["alert", "log", "ping"], str(a.sent))
    j = _sess("GET", "/diag/egress").json()
    cl = {c["ua"]: c for c in j["ws_clients"]}
    ok("W-5 /diag/egress 에 소켓마다 ip(첫 홉)·ua·embedded·hidden·tx", cl.get("FarmView", {}).get("hidden") is True
       and cl["FarmView"]["embedded"] is True and cl["FarmView"]["ip"] == "1.2.3.4" and cl["UA-test"]["tx"] > 0, str(j["ws_clients"])[:300])
    b.sent.clear()
    await b.inq.put(json.dumps({"t": "vis", "h": 0}))
    await _settle()
    ok("W-6 ★보이게 되면 전량(state) 한 번★(숨어서 한 판도 못 받았다)", "state" in b.sent and "state_diff" not in b.sent, str(b.sent))
    await a.inq.put(json.dumps({"t": "vis", "h": 1}))
    await b.inq.put(json.dumps({"t": "vis", "h": 1}))
    await _settle()
    before = main._PERF.get("push_state_skipped", {}).get("n", 0) if isinstance(main._PERF.get("push_state_skipped"), dict) else None
    a.sent.clear(); b.sent.clear()
    await main.push_state("main")
    await _settle()
    ok("W-7 ★전부 숨으면 만들지도 보내지도 않는다★", not main.manager.watching("main") and a.sent == [] and b.sent == [],
       f"{a.sent} {b.sent} {before}")
    await a.inq.put("not json {\"vis\"")
    await _settle()
    ok("W-8 깨진 vis 글은 무시(소켓 안 죽음)", main.manager._is_active(a), "")
    await a.inq.put(None)
    await b.inq.put(None)
    await asyncio.gather(ta, tb)
    ok("W-9 끊기면 meta 도 지운다(새지 않는다)", id(a) not in main.manager.meta and id(b) not in main.manager.meta, "")


def _node(js):
    node = shutil.which("node")
    if not node:
        return None, "node 없음"
    d = tempfile.mkdtemp(prefix="vis271_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), ""
    except Exception:
        return None, r.stderr[-300:]


def _fn(src, name):
    i = src.index("function " + name + "(")
    dd, k = 0, src.index("{", i)
    while True:
        c = src[k]
        dd += (c == "{") - (c == "}")
        k += 1
        if dd == 0:
            return src[i:k]


async def t_js():
    src = main.HTML_DASHBOARD
    js = ("let document={hidden:false}; let innerWidth=800, innerHeight=600; let _wsVisSent=false, _wsLastMsg=0;\n"
          "const sent=[]; let _ws={readyState:1, send:(m)=>sent.push(JSON.parse(m).h)};\n"
          + _fn(src, "_wsHid") + "\n" + _fn(src, "_wsVis") + r"""
_wsVis();                       // 보임 → 보임: 안 보냄
document.hidden=true; _wsVis(); _wsVis();   // 숨음: 한 번만
document.hidden=false; _wsVis();            // 보임: 한 번
innerWidth=0; _wsVis();                     // 크기 0 iframe 도 숨음
innerWidth=800; _wsVis();                   // 보임
console.log(JSON.stringify({sent}));
""")
    o, err = _node(js)
    ok("W-10 화면 JS 는 숨음/보임이 ★바뀔 때만★ 한 번씩 알린다(크기 0 도 숨음)", o is not None and o["sent"] == [1, 0, 1, 0], str(o or err))
    ok("W-11 접속 주소에 숨김·iframe 여부를 싣고, 숨은 동안엔 90초 감시견이 안 끊는다",
       "/ws?h=${_wsHid()?1:0}&e=${window.top!==window?1:0}" in src
       and "if(!_wsHid() && _ws && _ws.readyState===1 && Date.now()-_wsLastMsg>90000)" in src, "")


def test_all():
    run_all([t_plan, t_files, t_pins, t_ws, t_js])
    finish("test_bug_retention_ws", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
