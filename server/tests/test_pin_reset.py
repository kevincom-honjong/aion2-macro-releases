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

MIN_CHECKS = 19
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


async def t_queue():
    cases = [("label b", {"label": "b"}), ("labels list", {"labels": ["a", "c"]}), ("labels str", {"labels": "a, c"})]
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
    ok("D-5 ★한 PC 명령★ — 따옴표 붙은 pin_reset 은 정해진 세 자리뿐(CMD_SILENT · sendCmd 한 곳 · 팜뷰 제외 목록)",
       src.count("'pin_reset'") == 2 and src.count('"pin_reset"') == 1 and "sendCmd(id, 'pin_reset'" in src,
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


def test_all():
    run_all([t_queue, t_dashboard, t_farmview_closed])
    finish("test_pin_reset", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
