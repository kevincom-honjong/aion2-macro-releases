# -*- coding: utf-8 -*-
"""[대시보드] #456 (2026-10-14 주인님 «정지(out 된거 대시보드에 비활성해놔 햇걸려») — 정지(OUT) 계정은 회색 비활성으로.

  예전(#276): 정지 카드가 빨강(=오프라인·고장과 같은 색)이라 «죽은 건가» 로 읽혀 헷갈렸다.
  지금: ① 카드·탭이 회색 점선·«정지(OUT)» ② 계정 탭은 활성 뒤로(번호는 그대로) ③ 계정 메뉴의 그 계정 버튼은 처음부터 못 누른다
        ④ 전환 3 길(카드메뉴·다중선택·전부 1번으로)은 정지 계정을 누르자마자 막는다(서버 _ban_check_cmd 가 정본).
  서버·매크로·FV 계약은 안 바꾼다(banned·banned_slots 는 이미 있다).

    cd updater/server && python -X utf8 tests/test_out_disabled.py
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

from _harness import main, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 18


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
    d = tempfile.mkdtemp(prefix="od456_")
    pth = os.path.join(d, "t.js")
    open(pth, "w", encoding="utf-8").write(js)
    rr = subprocess.run([node, pth], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(rr.stdout.strip().splitlines()[-1]), rr.stderr[-500:]
    except Exception:
        return None, (rr.stderr or rr.stdout)[-500:]


async def t_source():
    src = main.HTML_DASHBOARD
    m = re.search(r"banned:\s+\{label:'정지\(OUT\)'[^}]*\}", src)
    cfg = m.group(0) if m else ""
    ok("OD-1 ★정지(OUT) 팔레트는 빨강이 아니다(회색 비활성) — 오프라인(빨강)과 겹치지 않는다★",
       bool(cfg) and "red" not in cfg and "gray" in cfg and "online:false" in cfg, cfg)
    ok("OD-2 카드: 정지면 .card-out(점선·흐림) · 깜빡이지 않는다 · 비활성 안내 줄",
       "${pc.banned?' card-out':''}" in src and "(!pc.banned && (st==='hunting'" in src and 'class="out-note"' in src
       and ".card-out{border-style:dashed}" in src and ".card-out > *:not(.out-note){opacity:.5;filter:grayscale(1)}" in src, "")
    css = src[src.index(".acct-tab-out{"):src.index(".acct-tab-out{") + 200]
    ok("OD-3 탭: OUT 은 빨강이 아니라 회색(#276 의 #f87171 제거)", "#f87171" not in css.split("}")[0] and "#6b7280" in css, css[:120])
    ok("OD-4 탭 순서는 outLast · 정지 목록은 outSlotsOf 한 곳(카드 banned_slots 직접 조회 제거)",
       "outLast(tabItems)" in src and "const outSet = outSlotsOf(s.base);" in src and "(s.top.banned_slots||[]).includes(k)" not in src, "")
    ok("OD-5 세 전환 길 전부 가드: fullAccountSwitch · switchAccountSelected · switchAllToFirst",
       "if (outSlotsOf(base).has(n)) { showToast(" in src and "if (outSlotsOf(baseId(t)).has(n)) { outSkip.push" in src
       and "if (outSlotsOf(b).has(1)) { outSk.push(b); continue; }" in src, "")
    ok("OD-6 static 미러가 같다(--sync-static)", os.path.exists(os.path.join(os.path.dirname(__file__), "..", "static", "dashboard.js"))
       and "function outSlotsOf(base){" in open(os.path.join(os.path.dirname(__file__), "..", "static", "dashboard.js"), encoding="utf-8").read(), "")


async def t_behavior():
    src = main.HTML_DASHBOARD
    node = shutil.which("node")
    ok("OD-0 node 가 있어야 한다", bool(node))
    if not node:
        return
    pre = r"""
const MAX_ACCT = 5;
const ACCT_LABELS = 'abcdefghi'.slice(0, MAX_ACCT);
const ACCT_SUFFIX = ACCT_LABELS.slice(1);
const ACCT_SUF_RE = new RegExp('([' + ACCT_SUFFIX + '])$');
let state = {};
const toasts = [], sent = [];
const showToast = m => toasts.push(String(m));
const esc = x => String(x);
""" + "\n".join([
        _fn(src, "isAcctSuf"), _fn(src, "acctNoOfSuf"), _fn(src, "baseId"), _fn(src, "acctNumOf"),
        _fn(src, "cardCfg"), _fn(src, "outSlotsOf"), _fn(src, "outLast"),
    ])
    st = r"""
const STATUS_CFG = {offline:{label:'오프라인',online:false}, hunting:{label:'사냥중',online:true},
                    no_account:{label:'계정 없음',online:false}, banned:{label:'정지(OUT)',online:false}};
"""
    # ── 1) outSlotsOf · outLast
    js1 = pre + st + r"""
const o = {};
state = {
  'PC-02':  {pc_id:'PC-02',  banned_slots:[2], status:'hunting'},
  'PC-02b': {pc_id:'PC-02b', banned_slots:[2], banned:true, status:'no_account'},
  'PC-03':  {pc_id:'PC-03',  status:'hunting'},
  'PC-15':  {pc_id:'PC-15',  banned_slots:['1','3']},
  'PC-16c': {pc_id:'PC-16c', banned:true},          // banned_slots 없이 자기 번호만(옛 서버)
};
o.pc02 = [...outSlotsOf('PC-02')].sort();
o.pc03 = [...outSlotsOf('PC-03')];
o.pc15 = [...outSlotsOf('PC-15')].sort();
o.pc16 = [...outSlotsOf('PC-16')];
o.none = [...outSlotsOf('PC-99')];
o.order = outLast([{k:1,out:false},{k:2,out:true},{k:3,out:false},{k:4,out:true}]).map(x => x.k);
o.orderNoOut = outLast([{k:1,out:false},{k:2,out:false}]).map(x => x.k);
o.orderAllOut = outLast([{k:1,out:true},{k:2,out:true}]).map(x => x.k);
console.log(JSON.stringify(o));
"""
    o, err = _node(js1)
    ok("OD-7 node 실행(helpers)", o is not None, err)
    if o is None:
        return
    ok("OD-8 ★outSlotsOf: 카드들의 banned_slots 합집합 + banned 카드 자신 · 문자열 번호도 · 모르는 PC 는 비어 있다★",
       o["pc02"] == [2] and o["pc03"] == [] and o["pc15"] == [1, 3] and o["pc16"] == [3] and o["none"] == [], str(o))
    ok("OD-9 ★outLast: 활성 먼저·정지 뒤(번호·서로의 순서 보존) · 없으면/전부면 그대로★",
       o["order"] == [1, 3, 2, 4] and o["orderNoOut"] == [1, 2] and o["orderAllOut"] == [1, 2], str(o))

    # ── 2) buildStack 이 실제로 탭을 활성→정지 순으로 그린다
    js2 = pre + st + r"""
const RETIRED = new Set();
const groupAcctMaps = b => ({ids:{1:'id1',2:'id2',3:'id3'}, plats:{}, servers:{}});
const groupChars = l => ({n:0, accts:0, out:0, outAccts:0, fromMap:true});
const isHuntDone = () => false;
function buildCard(pc){ return '<card ' + pc.pc_id + '/>'; }
""" + _fn(src, "buildStack") + r"""
state = {
  'PC-02':  {pc_id:'PC-02',  status:'hunting', banned_slots:[2], acct_total:3},
  'PC-02b': {pc_id:'PC-02b', status:'no_account', banned:true, banned_slots:[2], acct_total:3},
  'PC-02c': {pc_id:'PC-02c', status:'hunting', banned_slots:[2], acct_total:3},
};
const list = Object.values(state);
const html = buildStack({base:'PC-02', list, top:state['PC-02'], n:3});
const tabs = [...html.matchAll(/<button type="button" class="([^"]*)"[^>]*>(\d)(<i class="tout">OUT<\/i>)?/g)]
              .map(m => ({n: +m[2], out: !!m[3], cls: m[1]}));
console.log(JSON.stringify({tabs, hasOutCls: tabs.filter(t => t.out).every(t => t.cls.includes('acct-tab-out'))}));
"""
    o2, err2 = _node(js2)
    ok("OD-10 node 실행(buildStack)", o2 is not None, err2)
    if o2 is not None:
        ok("OD-11 ★탭: 1·3(활성) 다음에 2(OUT) — 번호 라벨은 그대로 · OUT 탭은 acct-tab-out★",
           [t["n"] for t in o2["tabs"]] == [1, 3, 2] and [t["out"] for t in o2["tabs"]] == [False, False, True] and o2["hasOutCls"], str(o2))

    # ── 3) 계정 메뉴 버튼
    js3 = pre + st + r"""
const btn = {};
for (let k = 1; k <= MAX_ACCT; k++) btn['cm-acct-' + k] = {style:{}, disabled:false, textContent:'', title:''};
const box = {childElementCount: MAX_ACCT};
const document = {getElementById: id => id === 'cm-acct-box' ? box : (btn[id] || null)};
const acctAvail = () => new Set([1, 2, 3]);
const currentAcctNum = () => 1;
function switchAccountDirect(){}
""" + _fn(src, "refreshAcctButtons") + r"""
state = {'PC-02': {pc_id:'PC-02', banned_slots:[2]}, 'PC-02b': {pc_id:'PC-02b', banned:true, banned_slots:[2]}};
refreshAcctButtons('PC-02');
const g = n => ({dis: btn['cm-acct-' + n].disabled, txt: btn['cm-acct-' + n].textContent, title: btn['cm-acct-' + n].title});
console.log(JSON.stringify({b1: g(1), b2: g(2), b3: g(3), b4: g(4)}));
"""
    o3, err3 = _node(js3)
    ok("OD-12 node 실행(refreshAcctButtons)", o3 is not None, err3)
    if o3 is not None:
        ok("OD-13 ★계정 메뉴: 정지 계정2 는 처음부터 disabled(OUT 표시·이유 툴팁) · 계정1·3 은 눌린다 · 자격증명 없는 4 는 그대로 disabled★",
           o3["b2"]["dis"] is True and o3["b2"]["txt"].endswith("OUT") and "정지(OUT)" in o3["b2"]["title"]
           and o3["b1"]["dis"] is False and o3["b3"]["dis"] is False and o3["b4"]["dis"] is True
           and "정지(OUT)" not in o3["b4"]["title"], str(o3))

    # ── 4) 전환 가드 3 길 — 명령이 아예 안 나간다
    js4 = pre + st + r"""
const sentCmds = [];
async function sendCmd(id, cmd, args){ sentCmds.push([id, cmd, args && args.acct_no]); return true; }
const confirm = () => true;
const liveCardOf = base => Object.values(state).find(p => baseId(p.pc_id) === base && cardOnline(p)) || null;
const cardOnline = p => !!p && (p.status === 'hunting' || p.status === 'idle');
const loadCmdHistory = () => {};
const selectedPcs = new Set();
const clearSelection = () => selectedPcs.clear();
const withBulk = async f => f();
const isFakePc = () => false;
const normAcct = x => ({'1':'a','2':'b','3':'c'})[String(x)] || null;
const acctNum = v => ACCT_LABELS.indexOf(v) + 1;
let promptAns = '2';
const prompt = () => promptAns;
""" + _fn(src, "fullAccountSwitch", "async function ") + "\n" + _fn(src, "switchAccountSelected", "async function ") \
        + "\n" + _fn(src, "switchAllToFirst", "async function ") + r"""
(async () => {
  const o = {};
  state = {
    'PC-02':  {pc_id:'PC-02',  status:'hunting', banned_slots:[2]},
    'PC-02b': {pc_id:'PC-02b', status:'no_account', banned:true, banned_slots:[2]},
    'PC-03':  {pc_id:'PC-03',  status:'hunting'},
    'PC-04b': {pc_id:'PC-04b', status:'idle', banned_slots:[1]},          // 계정1 이 OUT 인 PC(지금은 계정2 에서 놀고 있다)
    'PC-05b': {pc_id:'PC-05b', status:'idle'},
  };
  // (a) 카드메뉴: 정지 계정2 로는 명령이 안 나간다 / 3 으로는 나간다
  o.a_out = await fullAccountSwitch('PC-02', 2);
  o.a_ok  = await fullAccountSwitch('PC-02', 3);
  o.a_cmds = sentCmds.splice(0);
  o.a_toast = toasts.splice(0);
  // (b) 다중선택 → 계정2: PC-02(정지) 는 빼고 PC-03 만
  selectedPcs.add('PC-02'); selectedPcs.add('PC-03'); promptAns = '2';
  await switchAccountSelected();
  o.b_cmds = sentCmds.splice(0);
  // (c) 선택 전부가 정지 → 명령 0 + 이유 토스트
  selectedPcs.clear(); selectedPcs.add('PC-02'); toasts.length = 0;
  await switchAccountSelected();
  o.c_cmds = sentCmds.splice(0);
  o.c_toast = toasts.splice(0);
  // (d) 전부 1번으로: 계정1 이 OUT 인 PC-04 는 빠진다, PC-05 는 나간다
  toasts.length = 0;
  await switchAllToFirst();
  o.d_cmds = sentCmds.splice(0);
  console.log(JSON.stringify(o));
})();
"""
    o4, err4 = _node(js4)
    ok("OD-14 node 실행(전환 가드)", o4 is not None, err4)
    if o4 is not None:
        ok("OD-15 ★카드메뉴: 정지 계정2 로 전환 명령 0건(+이유 토스트) · 계정3 은 그대로 나간다★",
           o4["a_out"] is False and o4["a_ok"] is True and len(o4["a_cmds"]) == 1 and o4["a_cmds"][0][2] == 3
           and any("정지(OUT)" in t for t in o4["a_toast"]), str(o4))
        ok("OD-16 ★다중선택 계정2: 정지(OUT)인 PC-02 는 빼고 PC-03 만 나간다 · 전부 정지면 0건+이유★",
           [c[0] for c in o4["b_cmds"]] == ["PC-03"] and o4["c_cmds"] == [] and any("정지(OUT)" in t for t in o4["c_toast"]), str(o4))
        ok("OD-17 ★전부 1번으로: 계정1 이 정지(OUT)인 PC-04 는 빠지고 PC-05 만 나간다★",
           [c[0] for c in o4["d_cmds"]] == ["PC-05b"], str(o4["d_cmds"]))


def test_all():
    run_all([t_source, t_behavior])
    finish("test_out_disabled", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
