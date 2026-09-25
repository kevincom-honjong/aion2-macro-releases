# -*- coding: utf-8 -*-
"""[대시보드] pin_reset (2026-09-26 PIN 3차, SHARED_ISSUES_아이온2 «pin_reset») — 대시보드 버튼 → 매크로 명령 계약.

  매크로(lc loot._dispatch_remote_command 인라인)가 받는 모양:
    POST /command/{pc_id} {"command":"pin_reset","args":{"label":"b"}}   · "labels":["a","c"] · "labels":"a, c" · args 없음(지금 계정)
  서버는 ★그대로★ 큐에 넣어 그 pc_id(접미사 b/c/d 유지, §B3) 폴링에 내보내야 한다. 비밀 없음 — 이력에 마스킹 없이 보인다.
  대시보드: 카드 메뉴에 계정별 «🔓 PIN 계정 N» 버튼 하나씩(한 PC 명령 — 일괄·함대 버튼은 없다).

    cd updater/server && python -X utf8 tests/test_pin_reset.py
"""
import json
import os
import re

from fastapi.testclient import TestClient

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 46
C = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
SESS = main.new_session("main")


def post_cmd(pc, body):
    C.cookies.clear()
    C.cookies.set("session", SESS)
    try:
        return C.post("/command/" + pc, json=body)
    finally:
        C.cookies.clear()


async def poll(pc):
    return json.loads(bytes((await main.poll_command(pc, Req())).body))


async def ack(pc, cid):
    await main.ack_cmd(pc, cid, Req())


def _args(x):
    a = x.get("args")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            return {}
    return a if isinstance(a, dict) else {}


LAST_NO_HANDLER = "1.1.1012"   # ★사실★ — pin_reset 처리기가 없는 마지막 릴리스(lc a701b11, 아이온2 2026-09-26). 문턱은 이보다 커야 한다


async def report(pc, ver):
    await main.receive_report(pc, Req({"pc_id": pc, "status": "hunting", "macro_version": ver}))


async def stamp(pc, at):
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE pc_status SET updated_at=? WHERE pc_id=?", (at, pc))
        await c.commit()


async def t_queue():
    for pc in ("PC-PN1b", "PC-PN2"):
        await report(pc, "1.1.1013")                   # 처리기 있는 매크로(버전 문 통과)
    cases =[("label b", {"label": "b"}), ("labels list", {"labels": ["a", "c"]}), ("labels str", {"labels": "a, c"})]
    for name, args in cases:
        r = post_cmd("PC-PN1b", {"command": "pin_reset", "args": args})
        ok("P-1 %s — 세션 POST /command 가 받는다(200·ok·id)" % name, r.status_code == 200 and r.json().get("ok") and r.json().get("id"),
           "%s %s" % (r.status_code, r.text[:120]))
        p = await poll("PC-PN1b")
        a = {k: v for k, v in (p.get("args") or {}).items() if not k.startswith("_")}
        ok("P-2 %s — ★그 접미사 pc_id(PC-PN1b)★ 폴링에 pin_reset · args 그대로" % name,
           p.get("command") == "pin_reset" and a == args, str(p))
        await ack("PC-PN1b", p["id"])
    b = await poll("PC-PN1")
    ok("P-3 접미사를 뗀 base(PC-PN1) 큐에는 안 간다(§B3 — 매크로가 PC-PN1b 로 돈다)", b.get("command") is None, str(b))
    r = post_cmd("PC-PN2", {"command": "pin_reset"})
    p = await poll("PC-PN2")
    ok("P-4 args 없음(지금 계정) — 빈 args 로 나간다", r.status_code == 200 and p.get("command") == "pin_reset"
       and not {k for k in (p.get("args") or {}) if not k.startswith("_")}, str(p))
    ok("P-4b 사람 명령 표시 _by=human", (p.get("args") or {}).get("_by") == "human", str(p))
    hist = main._strip_cmds(await db.get_recent_commands(50, ns_prefix=""), "main")
    h = [x for x in hist if x.get("command") == "pin_reset" and x.get("pc_id") == "PC-PN1b"]
    ok("P-5 명령 이력에 label 이 마스킹 없이 보인다(비밀 없음)",
       any(_args(x).get("label") == "b" for x in h), str(h[:2])[:200])


def t_dashboard():
    src = open(os.path.join(os.path.dirname(main.__file__), "main.py"), encoding="utf-8").read()
    ok("D-1 카드 메뉴에 PIN 칸(cm-pin-box)과 계정별 버튼 생성기", 'id="cm-pin-box"' in src and "function refreshPinButtons(" in src)
    ok("D-2 메뉴를 열 때마다 버튼을 새로 그린다(openCardMenu → refreshPinButtons)",
       re.search(r"function openCardMenu\([^)]*\)\s*\{[\s\S]{0,3000}?refreshPinButtons\(pc_id\)", src) is not None)
    ok("D-3 버튼은 sendCmd(id,'pin_reset',{label}) — 계정 n → ACCT_LABELS[n-1]",
       re.search(r"async function pinResetFromMenu\(n\)\{\s*const id = menuPcId;[^}]*?\n  const label = ACCT_LABELS\[n-1\];"
                 r"[\s\S]{0,900}?sendCmd\(id, 'pin_reset', \{label: label\}\)", src) is not None)
    trk = re.search(r"const CMD_TRACK = \{(.*?)\n\};", main.HTML_DASHBOARD, re.S).group(1)
    sil = re.search(r"const CMD_SILENT = \[(.*?)\];", main.HTML_DASHBOARD, re.S).group(1)
    ok("D-4 ★칩 없음★ — CMD_SILENT 에 있고 CMD_TRACK 에 없다(물리 PC 한 칸 pendingCmds 에서 막힌 전환의 칩을 덮지 않게)",
       "'pin_reset'" in sil and "pin_reset" not in trk, "")
    ok("D-5 ★한 PC 명령★ — 따옴표 붙은 pin_reset 은 보내는 곳은 sendCmd 한 곳뿐(JS 는 CMD_SILENT·sendCmd 두 자리 · 서버의 \"pin_reset\" 은 전부 비교·팜뷰 제외 목록)",
       src.count("'pin_reset'") == 2 and "sendCmd(id, 'pin_reset'" in src and 'if command == "pin_reset":' in src
       and len(re.findall(r'"pin_reset"', src)) == len(re.findall(r'(?:[=!]= |frozenset\(\{)"pin_reset"', src)),
       "%d %d" % (src.count("'pin_reset'"), src.count('"pin_reset"')))
    js = open(os.path.join(os.path.dirname(main.__file__), "static", "dashboard.js"), encoding="utf-8").read()
    ok("D-6 static 미러에도 있다(verify --sync-static)", "function refreshPinButtons(" in js and "'pin_reset'" in js)


def fv_call(body):
    keep = main.FV_TOKEN
    main.FV_TOKEN = "fv-pin-token-0123456789abcdef"
    try:
        h = {"X-FV-Token": main.FV_TOKEN}
        return C.post("/api/fv/command", json=body, headers=h), C.get("/api/fv/command", headers=h)
    finally:
        main.FV_TOKEN = keep


async def t_farmview_closed():
    """반증 1 — 팜뷰 명령표는 CMD_TRACK/CMD_SILENT 를 파싱해 만든다 → 화면에 넣으면 팜뷰(7대·confirm_fleet 함대)에도 열렸다."""
    main._FV_CMDS.clear()
    ok("F-1 팜뷰 명령표에 pin_reset 이 없다(CMD_SILENT 에 있어도)", "pin_reset" not in main._fv_cmds(), "")
    g = None
    for pcs in (["PC-PN3"], ["PC-PN3", "PC-PN4"]):
        r, g = fv_call({"cmd": "pin_reset", "pc": pcs, "args": {"label": "a"}})
        ok("F-2 팜뷰 POST /api/fv/command pin_reset(%d대) → 400 · 큐에 안 들어감" % len(pcs),
           r.status_code == 400 and (await poll("PC-PN3")).get("command") is None, "%s %s" % (r.status_code, r.text[:120]))
    lst = json.dumps(g.json(), ensure_ascii=False)
    ok("F-3 팜뷰 GET /api/fv/command 목록에도 없다(다른 명령은 그대로 — start·live_on 있음)",
       g.status_code == 200 and "pin_reset" not in lst and '"start"' in lst and '"live_on"' in lst, lst[:160])
    keep = main._fv_parse_cmd_table
    main._FV_CMDS.clear()
    main._fv_parse_cmd_table = lambda: {}             # 명령표 파싱이 비었다(화면 코드가 바뀌어 정규식이 못 읽음)
    try:
        r, _ = fv_call({"cmd": "start", "pc": ["PC-PN3"]})
    finally:
        main._fv_parse_cmd_table = keep
        main._FV_CMDS.clear()
    ok("F-4 ★닫힌 채로 실패★ — 명령표가 비면 팜뷰 명령을 전부 503 으로 거절(옛: 전부 통과) · 큐 비었음",
       r.status_code == 503 and (await poll("PC-PN3")).get("command") is None, "%s %s" % (r.status_code, r.text[:120]))


async def t_version_gate():
    """아이온2 HIGH — 옛 매크로(1.1.1012 = lc a701b11)엔 처리기가 없다: 사람 명령이라 도는 전환을 끊고 모르는 명령 알람.
    서버는 ★그 물리 PC 의 최신 보고 버전★ 이 _PIN_RESET_MIN_VER 미만이거나 모르면 409 · 큐에 안 넣는다."""
    mv = main._PIN_RESET_MIN_VER
    js = re.search(r"const PIN_RESET_MIN_VER = '([^']*)';", main.HTML_DASHBOARD)
    ok("V-0 문턱은 화면 JS 한 줄에서 읽는다(서버 값 = JS 값, 형식 N.N.N)",
       js is not None and mv == js.group(1) and re.fullmatch(r"\d+\.\d+\.\d+", mv or "") is not None, "%s %s" % (mv, js and js.group(1)))
    ok("V-0b ★문턱 > %s★(처리기 없는 마지막 판 lc a701b11) — '1.1.103' 같은 오타가 문을 열지 못한다" % LAST_NO_HANDLER,
       mv is not None and main._ver_tuple(mv) > main._ver_tuple(LAST_NO_HANDLER), str(mv))
    vj = os.path.join(os.path.dirname(os.path.dirname(main.__file__)), "version.json")
    rel = json.load(open(vj, encoding="utf-8")).get("exe", {}).get("version") if os.path.isfile(vj) else None
    ok("V-0c 릴리스된 판(version.json)이 문턱보다 낮으면 그 판은 처리기 없는 판이어야 한다(= %s 이하)" % LAST_NO_HANDLER,
       rel is None or main._ver_at_least(rel, mv) or main._ver_tuple(rel) <= main._ver_tuple(LAST_NO_HANDLER), "%s vs %s" % (rel, mv))
    for pc, ver, why in (("PC-PV1", "1.1.1012", "함대 현재판"), ("PC-PV2", "1.1.999", "★숫자 비교★ — 문자열이면 999 > 1013 으로 통과"),
                         ("PC-PV3", None, "보고 없음(버전 모름)")):
        if ver:
            await report(pc, ver)
        r = post_cmd(pc, {"command": "pin_reset", "args": {"label": "a"}})
        q = await poll(pc)
        ok("V-1 %s v%s(%s) → 409 · 큐 비었음" % (pc, ver, why),
           r.status_code == 409 and "1.1.1013" in r.text and q.get("command") is None, "%s %s %s" % (r.status_code, r.text[:100], q))
    await report("PC-PV4", "1.1.1020")
    r = post_cmd("PC-PV4", {"command": "pin_reset", "args": {"label": "a"}})
    ok("V-2 v1.1.1020(문턱 위) → 200 · 큐에 들어감", r.status_code == 200 and (await poll("PC-PV4")).get("command") == "pin_reset",
       "%s %s" % (r.status_code, r.text[:100]))
    await report("PC-PV5", "1.1.1012")                 # 옛 카드(계정1)
    await report("PC-PV5b", "1.1.1013")                # 같은 PC 의 지금 카드 — exe 는 PC 당 하나
    await stamp("PC-PV5", "2026-09-26T10:00:00")
    await stamp("PC-PV5b", "2026-09-26T10:05:00")
    r = post_cmd("PC-PV5b", {"command": "pin_reset", "args": {"label": "a"}})
    ok("V-3 가장 최근 보고·받는 카드 둘 다 새 판이면 200", r.status_code == 200, "%s %s" % (r.status_code, r.text[:100]))
    r = post_cmd("PC-PV5", {"command": "pin_reset", "args": {"label": "a"}})
    ok("V-3b ★받는 카드 자신의 보고가 옛 판★이면 PC 최신이 새 판이어도 409(옛 매크로가 옛 카드 id 로 폴링 중일 수 있다)",
       r.status_code == 409, "%s %s" % (r.status_code, r.text[:100]))
    await stamp("PC-PV5", "2026-09-26T10:09:00")       # 이제 옛 판이 가장 최근 보고
    r = post_cmd("PC-PV5b", {"command": "pin_reset", "args": {"label": "b"}})
    ok("V-4 가장 최근 보고가 옛 판이면 새 판 카드 id 로 보내도 409", r.status_code == 409, "%s %s" % (r.status_code, r.text[:100]))
    r = post_cmd("PC-PV1", {"command": "start"})
    ok("V-5 다른 명령은 버전 문과 무관(옛 판에도 start 200)", r.status_code == 200, str(r.status_code))
    keep = main._PIN_RESET_MIN_VER
    main._PIN_RESET_MIN_VER = None                     # 화면 JS 한 줄을 못 읽었다
    try:
        r = post_cmd("PC-PV4", {"command": "pin_reset", "args": {"label": "a"}})
    finally:
        main._PIN_RESET_MIN_VER = keep
    ok("V-6 ★문턱을 못 읽으면 닫힌 채로★ — 새 판(v1.1.1020)이어도 409", r.status_code == 409, "%s %s" % (r.status_code, r.text[:100]))


async def _status_of(cid):
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as c:
        cur = await c.execute("SELECT status FROM commands WHERE id=?", (cid,))
        r = await cur.fetchone()
    return r[0] if r else None


async def t_delivery_gate():
    """반증 2차 #1 — 넣을 땐 새 판, 배달 전 롤백(deploy_to --ver·재시작) → 폴링·WS 재접속 어느 쪽으로도 안 나가고 취소된다."""
    await report("PC-PV6", "1.1.1013")
    r1 = post_cmd("PC-PV6", {"command": "pin_reset", "args": {"label": "a"}})
    r2 = post_cmd("PC-PV6", {"command": "start"})
    await report("PC-PV6", "1.1.1012")                 # 롤백
    p = await poll("PC-PV6")
    ok("W-1 ★폴링★: 롤백된 PC 에 밀려 있던 pin_reset 은 건너뛰고 다음 명령(start)을 준다",
       r1.status_code == 200 and r2.status_code == 200 and p.get("command") == "start", str(p))
    st = await _status_of(r1.json()["id"])
    ok("W-2 건너뛴 pin_reset 은 cancelled(이력에 보인다 · 다시 안 나간다)", st == "cancelled", str(st))
    logs = await db.get_logs("PC-PV6", 50) if hasattr(db, "get_logs") else []
    ok("W-2b 취소 사유가 그 PC 로그에 남는다(«pin_reset 배달 안 함(취소) — 매크로 v1.1.1012 < v…»)",
       any("pin_reset 배달 안 함" in str(x.get("message", "")) and "1.1.1012" in str(x.get("message", "")) for x in logs), str(logs[:2])[:200])
    await report("PC-PV7", "1.1.1013")
    q1 = post_cmd("PC-PV7", {"command": "pin_reset", "args": {"label": "b"}})
    q2 = post_cmd("PC-PV7", {"command": "stop"})
    await report("PC-PV7", "1.1.1012")
    import asyncio
    from _harness import FakeWS
    ws = FakeWS()
    task = asyncio.create_task(main.macro_websocket(ws, "PC-PV7"))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if any('"stop"' in m for m in ws.sent):
            break
    ws.die()
    try:
        await asyncio.wait_for(task, timeout=5)
    except Exception:
        pass
    cmds = [json.loads(m).get("command") for m in ws.sent if '"type": "command"' in m]
    ok("W-3 ★WS 재접속 배달★: pin_reset 은 안 보내고 stop 은 보낸다", "pin_reset" not in cmds and "stop" in cmds, str(cmds))
    ok("W-4 WS 경로에서 건너뛴 pin_reset 도 cancelled", q1.status_code == 200 and await _status_of(q1.json()["id"]) == "cancelled",
       str(await _status_of(q1.json()["id"])))


def _pin_js():
    src = main.HTML_DASHBOARD
    a = src.index("const PIN_RESET_MIN_VER")
    b = src.index("\n}\n", src.index("function verAtLeast(")) + 3
    c = src.index("function refreshPinButtons(")
    d = src.index("async function setCardOnly(")
    return src[a:b] + "\n" + src[c:d]


_DOM = r"""
const _els = {};
function _mk(tag){ return {tag, id:'', className:'', textContent:'', title:'', disabled:false, style:{}, onclick:null,
  children:[], get childElementCount(){ return this.children.length; },
  set innerHTML(v){ this.children.forEach(ch => { delete _els[ch.id]; }); this.children = []; },
  appendChild(ch){ this.children.push(ch); if (ch.id) _els[ch.id] = ch; return ch; } }; }
const document = { createElement: _mk, getElementById: id => _els[id] || null };
_els['cm-pin-box'] = _mk('span'); _els['cm-pin-box'].id = 'cm-pin-box';
const MAX_ACCT = 5, ACCT_LABELS = 'abcde';
let state = {}, menuPcId = null; const SENT = [], TOASTS = [];
function baseId(p){ return p.replace(/[b-z]$/, ''); }
function currentAcctNum(){ return 1; }
function confirm(){ return true; }
function closeCardMenu(){}
function showToast(t){ TOASTS.push(t); }
function loadCmdHistory(){}
async function sendCmd(id, c, a){ SENT.push([id, c, a]); return true; }
"""


async def t_js():
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    ok("J-0 node 로 화면 코드를 실행한다(없으면 빨간불)", node is not None, "")
    if not node:
        return
    js = _DOM + _pin_js() + r"""
(async () => {
  const box = document.getElementById('cm-pin-box');
  const out = {};
  out.cmp = [verAtLeast('1.1.999','1.1.1013'), verAtLeast('1.1.1013','1.1.1013'), verAtLeast('1.1.1014','1.1.1013'),
             verAtLeast('1.2','1.1.1013'), verAtLeast('','1.1.1013'), verAtLeast(null,'1.1.1013')];
  state['PC-1'] = {macro_version:'1.1.1012'}; refreshPinButtons('PC-1');
  out.old = box.children.map(c => [c.id, c.disabled, c.textContent]);
  menuPcId = 'PC-1'; await pinResetFromMenu(2); out.oldSent = SENT.length;
  state['PC-1'] = {macro_version:'1.1.1013'}; refreshPinButtons('PC-1');
  out.nw = box.children.map(c => [c.id, c.disabled]);
  menuPcId = 'PC-1'; await pinResetFromMenu(2); out.sent = SENT.slice();
  state['PC-1'] = {macro_version:'1.1.1000'}; refreshPinButtons('PC-1');
  out.back = box.children.map(c => c.id);
  console.log(JSON.stringify(out));
})();
"""
    d = tempfile.mkdtemp(prefix="pin_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    ok("J-1 화면 코드가 node 에서 돈다", r.returncode == 0, r.stderr[-300:])
    if r.returncode != 0:
        return
    o = json.loads(r.stdout)
    ok("J-2 verAtLeast 는 숫자 비교(999<1013 · 같음 참 · 1.2>1.1.1013 · 빈값/null 거짓)",
       o["cmp"] == [False, True, True, True, False, False], str(o["cmp"]))
    ok("J-3 ★v1.1.1012 카드 → PIN 버튼 없음★(잠긴 안내 한 줄뿐, disabled)",
       len(o["old"]) == 1 and o["old"][0][0] == "cm-pin-old" and o["old"][0][1] is True and "1.1.1013" in o["old"][0][2], str(o["old"]))
    ok("J-4 옛 판에서 pinResetFromMenu 를 불러도(옛 메뉴) sendCmd 안 함", o["oldSent"] == 0, str(o["oldSent"]))
    ok("J-5 v1.1.1013 카드 → 계정 버튼 5개(cm-pin-1..5, 누를 수 있음)",
       [x[0] for x in o["nw"]] == ["cm-pin-%d" % k for k in range(1, 6)] and not any(x[1] for x in o["nw"]), str(o["nw"]))
    ok("J-6 계정 2 → sendCmd('PC-1','pin_reset',{label:'b'}) 한 번", o["sent"] == [["PC-1", "pin_reset", {"label": "b"}]], str(o["sent"]))
    ok("J-7 다시 옛 판 카드를 열면 버튼이 사라진다", o["back"] == ["cm-pin-old"], str(o["back"]))


def test_all():
    run_all([t_queue, t_dashboard, t_farmview_closed, t_version_gate, t_delivery_gate, t_js])
    finish("test_pin_reset", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
