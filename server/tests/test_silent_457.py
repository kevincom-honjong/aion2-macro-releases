# -*- coding: utf-8 -*-
"""[대시보드] #457 (2026-10-15 주인님 «대시보드 … 죽엇는데 어비스사냥한다고 떠잇고») — 매크로 무보고를 카드가 말한다.

  실측(운영 /diag/perf, 2026-10-15): 서버는 느리지 않았다 — build_full_state 평균 10.6ms · 전이 보고 수신 p95 0.89s ·
  방송까지 p95 1.6s · 화면 렌더 2~4ms · 3000회 반복에도 힙·DOM 안 늘음. 진짜 구멍은 ★매크로가 멎어도 900초(MACRO_SILENT_TIMEOUT)
  마지막 상태를 그대로 «어비스 사냥» 으로 내고, 화면은 그 나이(_macro_silent_s — STATE_VOLATILE 이라 카드가 안 바뀌면 안 닿는다)를
  아예 안 봤다★ 는 것.
  지금: ① 서버 _card_key 가 «90초 넘은 무보고의 분» 이 바뀔 때만 카드를 다시 보낸다(업데이터 N분전과 같은 방식)
        ② buildCard: ⚠ N분째 무보고 칩 · «마지막» 표시 · 깜빡임 끔 ③ /diag/status_trail — 서버가 받은 status 전이 흔적.

    cd updater/server && python -X utf8 tests/test_silent_457.py
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

from fastapi.testclient import TestClient

from _harness import main, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 20
C = TestClient(main.app, raise_server_exceptions=False)


def _fn(src, name, kw="function "):
    i = src.index(kw + name + "(")
    d, k = 0, src.index("{", i)
    while True:
        c = src[k]
        d += (c == "{") - (c == "}")
        k += 1
        if d == 0:
            return src[i:k]


def _node(js):
    node = shutil.which("node")
    d = tempfile.mkdtemp(prefix="sl457_")
    pth = os.path.join(d, "t.js")
    open(pth, "w", encoding="utf-8").write(js)
    rr = subprocess.run([node, pth], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(rr.stdout.strip().splitlines()[-1]), rr.stderr[-500:]
    except Exception:
        return None, (rr.stderr or rr.stdout)[-500:]


def _sess(path, **kw):
    C.cookies.set("session", main.new_session("main"))
    try:
        return C.get(path, **kw)
    finally:
        C.cookies.clear()


def _card(**kw):
    c = {"pc_id": "PC-01", "status": "abyss", "kina": 5, "_macro_silent_s": 3, "_updater_age_s": 5}
    c.update(kw)
    return c


async def t_server():
    m = main
    ok("SL-1 ★문턱 안(≤90초)은 칩 없음 · 넘으면 분(1+)★",
       [m._silent_minutes(_card(_macro_silent_s=s)) for s in (0, 3, 47, 90)] == [0, 0, 0, 0]
       and [m._silent_minutes(_card(_macro_silent_s=s)) for s in (91, 119, 120, 130, 299, 300)] == [1, 1, 2, 2, 4, 5],
       str([m._silent_minutes(_card(_macro_silent_s=s)) for s in (91, 119, 120, 130, 299, 300)]))
    ok("SL-2 ★이미 «보고 없음»을 말하는 상태는 칩 0★ (offline · no_account · other_account)",
       all(m._silent_minutes(_card(status=s, _macro_silent_s=9000)) == 0 for s in ("offline", "no_account", "other_account")), "")
    ok("SL-3 ★정지(OUT) 슬롯은 매크로가 거기 없으니 0 — 단 매크로가 그 슬롯에서 살아 있으면(macro_live) 센다(#276·#441)★",
       m._silent_minutes(_card(banned=True, _macro_silent_s=500)) == 0
       and m._silent_minutes(_card(banned=True, macro_live=True, _macro_silent_s=500)) == 8, "")
    ok("SL-4 모름·이상값은 0(모름을 «늙었다»로 읽지 않는다)",
       all(m._silent_minutes(_card(_macro_silent_s=v)) == 0 for v in (None, -1, "7", True, "x")), "")
    k = m._card_key
    ok("SL-5 ★초 단위로만 커지는 동안(3→60→90)은 카드를 다시 안 보낸다(P-3 유지)★",
       k(_card(_macro_silent_s=3)) == k(_card(_macro_silent_s=60)) == k(_card(_macro_silent_s=90)), "")
    ok("SL-6 ★90초를 넘는 순간 한 번 · 이후엔 분이 바뀔 때만★",
       k(_card(_macro_silent_s=90)) != k(_card(_macro_silent_s=91))
       and k(_card(_macro_silent_s=91)) == k(_card(_macro_silent_s=119))
       and k(_card(_macro_silent_s=119)) != k(_card(_macro_silent_s=120))
       and k(_card(_macro_silent_s=120)) == k(_card(_macro_silent_s=179)), "")
    ok("SL-7 ★되살아나면(다시 3초) 열쇠가 원래로 돌아와 칩이 사라진다★",
       k(_card(_macro_silent_s=3)) == k(_card(_macro_silent_s=3)) and k(_card(_macro_silent_s=200)) != k(_card(_macro_silent_s=3)), "")
    ok("SL-8 ★오프라인으로 박제된 카드(433시간)는 매분 다시 보내지 않는다★",
       k(_card(status="offline", _macro_silent_s=5000)) == k(_card(status="offline", _macro_silent_s=5060)), "")


async def t_trail():
    m = main
    m._TRAIL.clear()
    m._TRAIL_LAST.clear()
    pl = lambda st, la="2026-10-15T02:22:23": {"status": st, "last_active": la, "slot": 1, "map": "갈라진 북쪽 추락지", "hunt_progress": 0.0}   # noqa: E731
    m._trail_note("PC-T1", pl("abyss"), "ws")
    m._trail_note("PC-T1", pl("abyss"), "ws")          # 하트비트 — 적지 않는다
    m._trail_note("PC-T1", pl("dead"), "ws")
    m._trail_note("PC-T1", pl("abyss"), "http")
    tr = m._TRAIL.get("PC-T1") or []
    ok("SL-9 ★status 가 바뀐 보고만 적는다(하트비트 제외) · 어느 길로 왔는지 남는다★",
       [(e["from"], e["to"], e["via"]) for e in tr] == [(None, "abyss", "ws"), ("abyss", "dead", "ws"), ("dead", "abyss", "http")], str(tr))
    for i in range(m.TRAIL_KEEP + 15):
        m._trail_note("PC-T1", pl("dead" if i % 2 else "idle"), "ws")
    ok("SL-10 상한(TRAIL_KEEP)을 넘기지 않는다", len(m._TRAIL["PC-T1"]) == m.TRAIL_KEEP, str(len(m._TRAIL["PC-T1"])))
    try:
        m._trail_note("PC-T2", None, "ws")              # 이상 입력 — 던지지 않는다
        thrown = False
    except Exception:
        thrown = True
    ok("SL-11 이상 입력에도 던지지 않는다(보고 처리를 막으면 안 된다)", not thrown, "")
    r0 = C.get("/diag/status_trail")
    ok("SL-12 세션 없이는 401", r0.status_code == 401, str(r0.status_code))
    r1 = _sess("/diag/status_trail?pc=PC-T1&n=5")
    j = r1.json() if r1.status_code == 200 else {}
    ok("SL-13 ★세션이 있으면 그 PC 의 최근 n 건★", r1.status_code == 200 and len(j.get("trail") or []) == 5 and j.get("pc") == "PC-T1", str(r1.status_code))
    r2 = _sess("/diag/status_trail")
    ok("SL-14 pc 를 안 주면 PC 별 최근 3건", r2.status_code == 200 and len((r2.json().get("pcs") or {}).get("PC-T1") or []) == 3, "")
    # 두 길(WS·HTTP) 입구가 같은 함수를 부른다(§A12)
    src = open(main.__file__, encoding="utf-8").read()
    ok("SL-15 ★WS 입구와 HTTP /report 입구가 같은 _trail_note★",
       src.count('_trail_note(nspc, payload, "ws")') == 1 and src.count('_trail_note(nspc, data, "http")') == 1, "")


async def t_ui():
    src = main.HTML_DASHBOARD
    ok("SL-16 buildCard: 칩 · «마지막» · 깜빡임 끔이 한 변수(_silM)에서 나온다",
       "const _silM = (_silS > 90" in src and "${_silM?'<span class=\"text-amber-400 text-xs font-medium\">마지막</span> ':''}" in src
       and "!pc.banned && !_silM &&" in src and "idvChip||silentChip||bugBadge||doneBadges" in src, "")
    ok("SL-17 화면 문턱(90)과 서버 문턱이 같다", f"MACRO_SILENT_WARN_S = {main.MACRO_SILENT_WARN_S}" in open(main.__file__, encoding="utf-8").read()
       and main.MACRO_SILENT_WARN_S == 90, str(main.MACRO_SILENT_WARN_S))
    node = shutil.which("node")
    ok("SL-0 node 가 있어야 한다", bool(node))
    if not node:
        return
    # buildCard 를 통째로 돌리긴 무겁다 — 앞 12줄(_silM 계산)과 칩 식만 떼어 같은 입력으로 서버 규칙과 대조
    i = src.index("const _silS = (typeof pc._macro_silent_s")
    j = src.index("const pulse = ", i)
    calc = src[i:j]
    js = "const rows = [];\nfunction f(pc){ const st = pc.status||'offline';\n" + calc + "\n return _silM; }\n" + r"""
const cases = [
  {status:'abyss', _macro_silent_s:3}, {status:'abyss', _macro_silent_s:90}, {status:'abyss', _macro_silent_s:91},
  {status:'abyss', _macro_silent_s:130}, {status:'hunting', _macro_silent_s:310}, {status:'offline', _macro_silent_s:9000},
  {status:'no_account', _macro_silent_s:9000}, {status:'other_account', _macro_silent_s:9000},
  {status:'abyss', _macro_silent_s:500, banned:true}, {status:'abyss', _macro_silent_s:500, banned:true, macro_live:true},
  {status:'abyss'}, {status:'abyss', _macro_silent_s:null}, {status:'idle', _macro_silent_s:-1},
];
console.log(JSON.stringify(cases.map(f)));
"""
    got, err = _node(js)
    want = []
    for c in [
        {"status": "abyss", "_macro_silent_s": 3}, {"status": "abyss", "_macro_silent_s": 90}, {"status": "abyss", "_macro_silent_s": 91},
        {"status": "abyss", "_macro_silent_s": 130}, {"status": "hunting", "_macro_silent_s": 310}, {"status": "offline", "_macro_silent_s": 9000},
        {"status": "no_account", "_macro_silent_s": 9000}, {"status": "other_account", "_macro_silent_s": 9000},
        {"status": "abyss", "_macro_silent_s": 500, "banned": True}, {"status": "abyss", "_macro_silent_s": 500, "banned": True, "macro_live": True},
        {"status": "abyss"}, {"status": "abyss", "_macro_silent_s": None}, {"status": "idle", "_macro_silent_s": -1},
    ]:
        want.append(main._silent_minutes(c))
    ok("SL-18 ★화면 계산 == 서버 _silent_minutes (13가지 입력 전부)★", got == want and got == [0, 0, 1, 2, 5, 0, 0, 0, 0, 8, 0, 0, 0],
       "got=%s want=%s err=%s" % (got, want, err))
    ok("SL-19 static 미러가 같다(--sync-static)",
       "const _silM = (_silS > 90" in open(os.path.join(os.path.dirname(__file__), "..", "static", "dashboard.js"), encoding="utf-8").read(), "")


def test_all():
    run_all([t_server, t_trail, t_ui])
    finish("test_silent_457", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
