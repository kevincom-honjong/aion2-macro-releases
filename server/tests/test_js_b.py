# -*- coding: utf-8 -*-
"""[대시보드] 대시보드 JS 반증 B 묶음(B-JS1~B-JS11, 2026-09-23) 회귀 가드 — 실제 실행.

반증 에이전트가 확정한 11건을 다시는 못 무너지게 한다. JS 는 main.HTML_DASHBOARD 에서 함수를
그대로 잘라 node 로 돌리고(grep 이 아니라 실행), 시간 버그는 ★시간대를 바꿔★ 두 번 돈다
(주인님=한국 KST · 직원=베트남 UTC+7 휴대폰).
  JS1  단일수집(merge) 캐릭의 수집 시각이 안 찍혀 리셋 뒤 새 값(표층 0·각성 0)이 14:00:00·3/3 으로 보정됨(+서버 합계)
  JS2  aiStaleDays 가 UTC last_active 를 로컬로 읽음
  JS3  매크로 보고값(macro_version·acct_num·slot·completed_time)이 HTML/onclick 에 그대로 박힘
  JS4  selCmd/bulkCmd 가 스택 카드마다 1건 → 한 매크로에 여러 건 · 확인창 숫자가 카드 수
  JS5  안 도는 계정 카드로 openLive → 영영 안 뜸
  JS6  UTC 시각을 로컬처럼 표시(정보 모달·명령 내역·로그 모달)
  JS7  베트남 표 PC 정렬이 거꾸로('PC-03'→-3) · 빈 값이 0 으로 앞에
  JS8  전체선택·일괄명령에 가짜·은퇴·계정없음 PC 가 섞임
  JS9  첫 /status 가 던지면 초기화가 통째로 죽음
  JS10 캐릭 표 그룹 뱃지와 행 빨강의 성역 판정이 다름
  JS11 리셋 경계가 브라우저 로컬 시간(베트남에서 2시간 어긋남)
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 81     # 2026-09-23 실측값 — node 가 없으면 JS 가 빠지므로 일부러 빨간불

TZS = ("Asia/Seoul", "Asia/Ho_Chi_Minh")


# ───────────────── 자르기 · 실행 발판 (test_summary 와 같은 규칙) ─────────────────

def _js_func(src: str, name: str) -> str:
    """`function name(` 부터 짝 맞는 `}` 까지. 문자열·템플릿(${} 중첩)·주석 처리."""
    if name == "esc":      # 정규식 리터럴 안의 따옴표를 문자열로 오인한다 — 한 줄짜리라 줄로 자른다
        return _block(src, "function esc(v){", "}[c])); }")
    i = src.index("function %s(" % name)
    # ★기본값 인자(args={})의 { 를 본문으로 잡지 않게★ 괄호 짝을 먼저 넘는다
    pd, j = 0, src.index("(", i)
    while True:
        pd += {"(": 1, ")": -1}.get(src[j], 0)
        if pd == 0:
            break
        j += 1
    j = src.index("{", j)
    if src[max(0, i - 6):i] == "async ":
        i -= 6
    depth, k, n = 0, j, len(src)
    stack = []
    while k < n:
        c = src[k]
        if stack and stack[-1] == "tpl":
            if c == "\\":
                k += 2
                continue
            if c == "`":
                stack.pop()
            elif c == "$" and src[k + 1:k + 2] == "{":
                stack.append(depth)
                depth += 1
                k += 2
                continue
            k += 1
            continue
        if c in "'\"":
            q = c
            k += 1
            while k < n and src[k] != q:
                k += 2 if src[k] == "\\" else 1
            k += 1
            continue
        if c == "`":
            stack.append("tpl")
            k += 1
            continue
        if src.startswith("//", k):
            k = src.index("\n", k)
            continue
        if src.startswith("/*", k):
            k = src.index("*/", k) + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if stack and stack[-1] == depth:
                stack.pop()
            elif depth == 0:
                return src[i:k + 1]
        k += 1
    raise ValueError("짝이 안 맞음: " + name)


def _grab(src, names, optional=()):
    """필수 함수는 없으면 ValueError(→ 그 묶음 FAIL), 선택 함수(새 도우미)는 있으면 넣는다."""
    out = [_js_func(src, n) for n in names]
    for n in optional:
        if ("function %s(" % n) in src:
            out.append(_js_func(src, n))
    return "\n".join(out)


def _line(src, prefix, required=True):
    for ln in src.splitlines():
        if ln.strip().startswith(prefix):
            return ln.strip()
    if required:
        raise ValueError("줄 없음: " + prefix)
    return ""


def _block(src, start, end):
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j + len(end)]


def _run_node(js: str, tz: str = "Asia/Seoul"):
    node = shutil.which("node")
    if not node:
        return None
    d = tempfile.mkdtemp(prefix="jsb_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write("process.env.TZ=%s;\n" % json.dumps(tz) + js)
    env = dict(os.environ, TZ=tz)
    r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])
    return json.loads(r.stdout.strip().splitlines()[-1])


_DOM = r"""
const els = {};
function mkEl(id){
  return els[id] || (els[id] = {id, textContent:'', innerHTML:'', title:'', value:'', className:'', style:{},
    dataset:{}, childElementCount:0, offsetWidth:100, offsetHeight:100, clientWidth:100, clientHeight:100,
    classList:{add(){}, remove(){}, toggle(){}, contains(){ return false; }},
    getBoundingClientRect(){ return {width:100,height:100,left:0,top:0,right:100,bottom:100}; },
    appendChild(){}, querySelectorAll(){ return []; }, querySelector(){ return null; },
    addEventListener(){}, closest(){ return null; }, setAttribute(){}, remove(){}});
}
const document = {getElementById: mkEl, querySelectorAll(){ return []; }, querySelector(){ return null; },
  addEventListener(){}, createElement(){ return mkEl('_n' + Math.random()); }, body:{appendChild(){}},
  documentElement:{classList:{toggle(){}}}, hidden:false};
const window = {innerWidth:1200, innerHeight:900, addEventListener(){}, scrollX:0, scrollY:0};
const localStorage = {getItem(){ return null; }, setItem(){}, removeItem(){}};
const toasts = [];
function showToast(m){ toasts.push(String(m)); }
const RealDate = Date;
function at(iso, f){
  const t = new RealDate(iso).getTime();
  global.Date = class extends RealDate { constructor(...a){ if (a.length === 0) super(t); else super(...a); }
    static now(){ return t; } };
  try { return f(); } finally { global.Date = RealDate; }
}
"""


def _need_node(tag):
    if not shutil.which("node"):
        ok("%s node 가 있어야 한다(대시보드 JS 는 node 로만 시험된다)" % tag, False)
        return False
    return True


# ───────────────── JS1 — 서버: 단일수집 캐릭 자기 시각 ─────────────────

OLD_CA = "2026-09-01T00:00:00"     # 확실히 지난 수요일 05시 KST 이전


async def t_js1_server():
    pid = "PC-31"
    full = {"total_kina": 100, "collected_at": OLD_CA, "characters": [
        {"slot": 1, "name": "가", "awakening_ticket": 1, "abyss_time": "03:00:00", "daily_ticket": 5},
        {"slot": 2, "name": "나", "awakening_ticket": 1, "abyss_time": "05:00:00", "daily_ticket": 5}]}
    await main.receive_char_info(pid, Req(body=full))
    merge = {"merge": True, "collected_at": "2026-09-23T00:00:00", "characters": [
        {"slot": 1, "name": "가", "awakening_ticket": 0, "abyss_time": "00:00:00", "daily_ticket": 0}]}
    await main.receive_char_info(pid, Req(body=merge))
    info = await db.get_char_info(pid)
    by = {c.get("slot"): c for c in info["chars"]}
    ok("JS1-a 행 collected_at 은 전체수집 시각 그대로(2026-07-25 규약 유지)", info["collected_at"] == OLD_CA,
       str(info["collected_at"]))
    ca1 = str(by[1].get("collected_at") or "")
    ok("JS1-b merge 된 슬롯1 에 자기 수집 시각이 찍힌다(서버 수신 시각)", ca1 > "2026-09-22", ca1)
    ok("JS1-c merge 안 된 슬롯2 는 캐릭 시각이 없다(행 시각으로 폴백)", not by[2].get("collected_at"), str(by[2]))

    _s = main._require_session
    main._require_session = lambda r: "main"
    try:
        resp = await main.get_all_characters(Req())
    finally:
        main._require_session = _s
    rows = [r for r in json.loads(resp.body)["characters"] if r["pc_id"] == pid]
    rb = {r["slot"]: r for r in rows}
    ok("JS1-d /characters 슬롯1 collected_at = 캐릭 자기 시각", str(rb[1]["collected_at"]) == ca1,
       str(rb[1]["collected_at"]))
    ok("JS1-e /characters 슬롯2 collected_at = 행 시각(옛 행 호환)", rb[2]["collected_at"] == OLD_CA,
       str(rb[2]["collected_at"]))

    agg = await main._fv_char_agg("main")
    aw = (agg.get(pid) or {}).get("awakening_ticket")
    ok("JS1-f 서버 합계 각성 = 슬롯1 새 값 0 + 슬롯2 리셋보정 3 = 3(옛: 6)", aw == 3, str(aw))

    # 화면: /characters 행을 그대로 JS 리셋 보정에 넣는다
    if not _need_node("JS1-g"):
        return
    src = main.HTML_DASHBOARD
    try:
        fns = _grab(src, ("fmtTs", "collectedAtDate", "lastDailyReset", "lastWeeklyReset", "isBeforeReset",
                          "resetAwareTicket", "isSurfaceZero", "displayAbyssTime"),
                    optional=("kstGameDayNum", "fmtKstTs"))
        consts = _line(src, "const KST_OFF_MS", required=False)
    except Exception as e:
        ok("JS1-g 함수를 잘라낸다", False, str(e))
        return
    js = (_DOM + consts + "\n" + fns + "\nconst rows = " + json.dumps(rows) + ";\n" + r"""
const o = {};
for (const r of rows) o[r.slot] = {abyss: displayAbyssTime(r), aw: resetAwareTicket(r.collected_at, r.awakening_ticket, 3),
  daily: resetAwareTicket(r.collected_at, r.daily_ticket, 14)};
console.log(JSON.stringify(o));
""")
    for tz in TZS:
        try:
            o = _run_node(js, tz)
        except Exception as e:
            ok("JS1-g[%s] node 실행" % tz, False, str(e)[:300])
            continue
        ok("JS1-g[%s] 리셋 뒤 merge 된 표층 00:00:00 은 그대로(14:00:00 아님)" % tz, o["1"]["abyss"] == "00:00:00", str(o))
        ok("JS1-h[%s] 리셋 뒤 merge 된 각성 0·일일 0 은 그대로(3/3·14 아님)" % tz,
           o["1"]["aw"] == 0 and o["1"]["daily"] == 0, str(o["1"]))
        ok("JS1-i[%s] 옛 행 폴백 슬롯2 는 리셋 보정(각성 3·일일 14)" % tz,
           o["2"]["aw"] == 3 and o["2"]["daily"] == 14, str(o["2"]))


# ───────────────── JS2 · JS6 · JS11 — 시간 ─────────────────

_TIME_FUNCS = ("fmtTs", "collectedAtDate", "lastDailyReset", "lastWeeklyReset", "isHuntDone", "isDungeonDone",
               "isBeforeReset", "resetAwareTicket", "aiStaleDays", "aiGameDay", "fmtAt")
_TIME_OPT = ("kstGameDayNum", "fmtKstTs", "fmtLocalAt")

_TIME_SCENARIO = r"""
const o = {};
o.wk_wed0600 = at('2026-09-22T21:00:00Z', () => lastWeeklyReset().toISOString());   // 수 06:00 KST
o.wk_wed0430 = at('2026-09-22T19:30:00Z', () => lastWeeklyReset().toISOString());   // 수 04:30 KST
o.dy_0630 = at('2026-09-22T21:30:00Z', () => lastDailyReset().toISOString());
o.dy_0400 = at('2026-09-22T19:00:00Z', () => lastDailyReset().toISOString());
o.hunt_yday = at('2026-09-22T21:30:00Z', () => isHuntDone([{completed:true, completed_time:'2026-09-22 20:00:00'}]));
o.hunt_today = at('2026-09-22T21:30:00Z', () => isHuntDone([{completed:true, completed_time:'2026-09-23 05:10:00'}]));
o.dg_before = at('2026-09-22T21:30:00Z', () => isDungeonDone({dungeon_done_at:'2026-09-23 04:00:00'}));
o.dg_after = at('2026-09-22T21:30:00Z', () => isDungeonDone({dungeon_done_at:'2026-09-23 05:30:00'}));
o.tk_after = at('2026-09-22T23:00:00Z', () => resetAwareTicket('2026-09-22T21:30:00', 1, 3));   // 수집 06:30 KST
o.tk_before = at('2026-09-22T23:00:00Z', () => resetAwareTicket('2026-09-22T19:00:00', 1, 3));  // 수집 04:00 KST
o.gday = at('2026-09-22T21:30:00Z', () => aiGameDay());
o.stale0 = at('2026-09-23T01:00:00Z', () => aiStaleDays({last_active:'2026-09-23T00:59:50'}));
o.stale0b = at('2026-09-23T06:00:00Z', () => aiStaleDays({last_active:'2026-09-23T05:59:50'}));
o.stale3 = at('2026-09-23T01:00:00Z', () => aiStaleDays({last_active:'2026-09-20T12:00:00'}));
o.fmtAt = fmtAt('2026-09-23T01:00:00');
console.log(JSON.stringify(o));
"""


def t_time():
    if not _need_node("JS-time"):
        return
    src = main.HTML_DASHBOARD
    try:
        fns = _grab(src, _TIME_FUNCS, _TIME_OPT)
        consts = _line(src, "const KST_OFF_MS", required=False)
    except Exception as e:
        ok("JS-time 함수를 잘라낸다", False, str(e))
        return
    js = _DOM + consts + "\n" + fns + "\n" + _TIME_SCENARIO
    want_at = {"Asia/Seoul": "2026-09-23 10:00", "Asia/Ho_Chi_Minh": "2026-09-23 08:00"}
    for tz in TZS:
        try:
            o = _run_node(js, tz)
        except Exception as e:
            ok("JS-time[%s] node 실행" % tz, False, str(e)[:300])
            continue
        ok("JS11-a[%s] 수 06:00 KST 의 주간 경계 = 수 05:00 KST" % tz, o["wk_wed0600"] == "2026-09-22T20:00:00.000Z", o["wk_wed0600"])
        ok("JS11-b[%s] 수 04:30 KST 의 주간 경계 = 지난 수 05:00 KST" % tz, o["wk_wed0430"] == "2026-09-15T20:00:00.000Z", o["wk_wed0430"])
        ok("JS11-c[%s] 일일 경계 06:30 KST → 오늘 05:00 KST" % tz, o["dy_0630"] == "2026-09-22T20:00:00.000Z", o["dy_0630"])
        ok("JS11-d[%s] 일일 경계 04:00 KST → 어제 05:00 KST" % tz, o["dy_0400"] == "2026-09-21T20:00:00.000Z", o["dy_0400"])
        ok("JS11-e[%s] isHuntDone: 어제 20시(KST) 완료는 오늘 완료가 아니다" % tz, o["hunt_yday"] is False, str(o["hunt_yday"]))
        ok("JS11-f[%s] isHuntDone: 오늘 05:10(KST) 완료는 완료" % tz, o["hunt_today"] is True, str(o["hunt_today"]))
        ok("JS11-g[%s] isDungeonDone: 수 04:00 KST 소진은 리셋 전(미완)" % tz, o["dg_before"] is False, str(o["dg_before"]))
        ok("JS11-h[%s] isDungeonDone: 수 05:30 KST 소진은 완료" % tz, o["dg_after"] is True, str(o["dg_after"]))
        ok("JS11-i[%s] 리셋 뒤(06:30 KST) 수집한 각성 1 은 1 그대로" % tz, o["tk_after"] == 1, str(o["tk_after"]))
        ok("JS11-j[%s] 리셋 전(04:00 KST) 수집한 각성 1 → 3" % tz, o["tk_before"] == 3, str(o["tk_before"]))
        ok("JS11-k[%s] aiGameDay 수 06:30 KST = 2026-09-23" % tz, o["gday"] == "2026-09-23", str(o["gday"]))
        ok("JS2-a[%s] 10초 전 보고(UTC) → 0일" % tz, o["stale0"] == 0, str(o["stale0"]))
        ok("JS2-b[%s] 15시 KST 10초 전 보고 → 0일" % tz, o["stale0b"] == 0, str(o["stale0b"]))
        ok("JS2-c[%s] 9/20 21시 KST 보고, 9/23 10시 KST → 3일" % tz, o["stale3"] == 3, str(o["stale3"]))
        ok("JS6-a[%s] 정보 모달 수집 시각 = 보는 기기 로컬" % tz, o["fmtAt"] == want_at[tz], str(o["fmtAt"]))


def t_js6_lists():
    if not _need_node("JS6"):
        return
    src = main.HTML_DASHBOARD
    try:
        fns = _grab(src, ("esc", "escAttr", "fmtTs", "collectedAtDate", "renderCmdHistory", "loadLogs"),
                    ("fmtLocalAt",))
    except Exception as e:
        ok("JS6 함수를 잘라낸다", False, str(e))
        return
    js = _DOM + fns + r"""
function pendFromHistory(){ return false; } function pendSweep(){} function scheduleRenderNow(){}
function _basePc(x){ return x; }
const lines = [];
function appendLogLine(level, msg){ lines.push(msg); }
let logModalPc = 'PC-01', logModalSrc = 'macro';
async function fetch(){ return {ok:true, json: async () => ({logs:[{level:'INFO', created_at:'2026-09-23T01:00:05', message:'m'}]})}; }
(async () => {
  renderCmdHistory([{id:1, created_at:'2026-09-23T01:00:05', pc_id:'PC-01', command:'start', status:'acked'}]);
  await loadLogs();
  console.log(JSON.stringify({hist: els['cmd-history'].innerHTML, line: lines[0] || ''}));
})().catch(e => { console.error(e && e.stack || e); process.exit(3); });
"""
    want = {"Asia/Seoul": "10:00:05", "Asia/Ho_Chi_Minh": "08:00:05"}
    for tz in TZS:
        try:
            o = _run_node(js, tz)
        except Exception as e:
            ok("JS6[%s] node 실행" % tz, False, str(e)[:300])
            continue
        ok("JS6-b[%s] 명령 내역 시각 = 로컬 %s" % (tz, want[tz]), want[tz] in o["hist"], re.sub(r"\s+", " ", o["hist"])[:120])
        ok("JS6-c[%s] 로그 모달 줄머리 = 로컬 %s" % (tz, want[tz]), o["line"].startswith(want[tz]), o["line"])


# ───────────────── JS3 · JS10 — 주입 · 빨강 판정 ─────────────────

EVIL = "<img src=x onerror=alert(1)>"


def t_js3_inject():
    if not _need_node("JS3"):
        return
    src = main.HTML_DASHBOARD
    try:
        fns = _grab(src, ("esc", "escAttr", "isAcctSuf", "acctNoOfSuf", "baseId", "acctNumOf", "isMultiAcct",
                          "acctTagSpread", "acctRow", "buildDailyProgress", "openCardMenu"))
        consts = "\n".join(_line(src, p) for p in ("const MAX_ACCT", "const ACCT_LABELS", "const ACCT_SUFFIX"))
    except Exception as e:
        ok("JS3 함수를 잘라낸다", False, str(e))
        return
    js = _DOM + consts + "\n" + fns + r"""
const EVIL = %s;
const STATUS_CFG = {hunting:{online:true, text:'', badge:'', label:'사냥'}, offline:{online:false, text:'', badge:'', label:'끔'}};
let state = {'PC-05': {pc_id:'PC-05', status:'hunting', acct_num: EVIL, acct_total: 2, macro_version: EVIL},
             'PC-05b': {pc_id:'PC-05b', status:'offline'}};
let menuPcId = null;
function closeCardMenu(){} function refreshAcctButtons(){} function refreshCardOnlyButtons(){} function refreshParsecButtons(){}
function groupAcctMaps(){ return {ids:{}, servers:{}, plats:{}}; }
function platLabel(){ return null; }
function surfaceZeroSlots(){ return new Set(); }
function dpDone(c){ return !!c.completed; }
function dpMarks(){ return ''; }
function fmtKinaShort(n){ return String(n); }
const CLASS_LABEL = {};
const o = {};
o.num = acctNumOf('PC-05');
o.tag = acctTagSpread('PC-05');
o.row = acctRow(state['PC-05']);
o.dp = buildDailyProgress([{slot: EVIL, completed:false}, {slot:2, completed:true, completed_time:'2026-09-23 "><img src=y>'}],
                          null, [], {pc_id:'PC-05'});
try { openCardMenu('PC-05', {stopPropagation(){}, clientX:10, clientY:10}); } catch(e) { o.menuErr = String(e); }
o.menu = els['menu-pc-label'].innerHTML;
console.log(JSON.stringify(o));
""" % json.dumps(EVIL)
    try:
        o = _run_node(js)
    except Exception as e:
        ok("JS3 node 실행", False, str(e)[:300])
        return
    ok("JS3-a acctNumOf 는 매크로 문자열 acct_num 을 정수로만(→1)", o["num"] == 1, str(o["num"]))
    ok("JS3-b acctTagSpread 에 <img 주입 없음", "<img" not in o["tag"], o["tag"][-80:])
    ok("JS3-c acctRow 에 <img 주입 없음", "<img" not in o["row"], re.sub(r"\s+", " ", o["row"])[:120])
    ok("JS3-d 일일 칩 slot 아이콘에 <img 주입 없음", "<img" not in o["dp"], re.sub(r"\s+", " ", o["dp"])[:160])
    ok("JS3-e 일일 칩 title 이 completed_time 으로 안 깨진다(값이 이스케이프돼 들어감)",
       '"><im' not in o["dp"] and "&quot;&gt;&lt;im" in o["dp"], re.sub(r"\s+", " ", o["dp"])[-300:])
    ok("JS3-f 카드 메뉴 머리가 그려졌다(시험이 헛돌지 않음)", "PC-05" in o["menu"], o.get("menuErr", "")[:120])
    ok("JS3-g 카드 메뉴 머리 macro_version·계정번호에 <img 주입 없음", "<img" not in o["menu"], o["menu"][:160])


_CT_FUNCS = ("esc", "escAttr", "isAcctSuf", "baseId", "nmTicketText", "nmTicketFull", "collectedAtDate",
             "lastDailyReset", "lastWeeklyReset", "isBeforeReset", "isSurfaceZero", "displayAbyssTime",
             "renderCharTable")


def t_js3_js10_table():
    if not _need_node("JS10"):
        return
    src = main.HTML_DASHBOARD
    try:
        fns = _grab(src, _CT_FUNCS, ("kstGameDayNum", "isRowRed"))
        consts = "\n".join(filter(None, (_line(src, "const NIGHTMARE_TICKET_MAX"), _line(src, "const MAX_ACCT"),
                                          _line(src, "const ACCT_LABELS"), _line(src, "const ACCT_SUFFIX"),
                                          _line(src, "const KST_OFF_MS", required=False))))
    except Exception as e:
        ok("JS10 함수를 잘라낸다", False, str(e))
        return
    rows = [
        {"pc_id": "PC-01", "slot": 1, "name": "a", "gear_power": 2800, "sanctuary": "2/5"},     # 행 안 빨강
        {"pc_id": "PC-02", "slot": 1, "name": "b", "gear_power": 2800, "sanctuary": "1/1"},     # 행 빨강
        {"pc_id": "PC-03", "slot": 1, "name": "c", "gear_power": 2800, "sanctuary": "5/5"},     # 둘 다 빨강
        {"pc_id": "PC-04", "slot": "x);alert(1)//", "name": "d", "arcana_image": True, "equip_image": True},
        {"pc_id": "PC-04", "slot": '"]);alert(2);//', "name": "e"},
    ]
    js = _DOM + consts + "\n" + fns + r"""
let state = {};
let charTableData = %s;
let charTableSort = {key:'pc_id', asc:true};
function groupAcctMaps(){ return {ids:{}, servers:{}, plats:{}}; }
function acctNumOf(){ return 1; } function acctTagSpread(){ return ''; } function acctIdTag(){ return ''; }
renderCharTable();
console.log(JSON.stringify({html: els['char-tbody'].innerHTML}));
""" % json.dumps(rows)
    try:
        o = _run_node(js)
    except Exception as e:
        ok("JS10 node 실행", False, str(e)[:300])
        return
    html = o["html"]
    parts = re.split(r'(?=<tr class="bg-gray-700/80)', html)
    res = {}
    for p in parts:
        m = re.search(r"font-weight:800;[^>]*>(PC-\d+)<", p)
        if not m:
            continue
        badge = re.search(r'<span class="text-red-400 text-xs">\((\d+)\)</span>', p)
        res[m.group(1)] = (int(badge.group(1)) if badge else 0, p.count("bg-red-950/40"))
    ok("JS10-0 그룹 헤더 4개(PC-01~04)를 읽었다", len(res) == 4, str(res))
    for pc in ("PC-01", "PC-02", "PC-03"):
        b, r = res.get(pc, (-1, -2))
        ok("JS10-%s 그룹 뱃지 수 == 빨간 행 수 (%s)" % (pc[-1], pc), b == r, "badge=%s rows=%s" % (b, r))
    handlers = re.findall(r'on(?:click|change)="([^"]*)"', html)
    slot_h = [h for h in handlers if re.search(r"showScreenshot|toggleSlotFilter|collectSlot|selectAllSlots", h)]
    ok("JS3-h 캐릭 표 onclick/onchange 에서 slot 을 찾았다", len(slot_h) >= 8, str(len(slot_h)))
    bad = [h for h in slot_h if "alert" in h]
    ok("JS3-i slot 이 onclick/onchange 코드로 주입되지 않는다", not bad, str(bad)[:200])
    sel = re.findall(r"selectAllSlots\('PC-04', ([^)]*), (?:true|false)\)", html)
    ok("JS3-j selectAllSlots 인자가 숫자 배열로 온전(따옴표로 속성이 안 끊김)",
       len(sel) == 2 and all(re.fullmatch(r"\[[\d,]*\]", s) for s in sel), str(sel)[:160])


# ───────────────── JS7 — 베트남 표 정렬 ─────────────────

def t_js7_sort():
    if not _need_node("JS7"):
        return
    src = main.HTML_DASHBOARD
    try:
        fns = _grab(src, ("esc", "_ta", "_vietnamVal", "renderVietnam"), ("_vietnamCmp",))
        consts = _block(src, "const VN_T = {", "\n};") + "\n" + _block(src, "const VIETNAM_COLS = [", "\n];")
    except Exception as e:
        ok("JS7 함수를 잘라낸다", False, str(e))
        return
    data = [
        {"pc_id": "PC-10", "slot": 1, "gear_power": 3000},
        {"pc_id": "PC-03", "slot": 1, "gear_power": ""},
        {"pc_id": "PC-22d", "slot": 1, "gear_power": 2500},
        {"pc_id": "PC-01", "slot": 2, "gear_power": 2900},
        {"pc_id": "PC-01", "slot": "x);alert(1)//", "gear_power": None},
    ]
    js = _DOM + consts + "\n" + fns + r"""
let vietnamLang = 'vi', vietnamData = %s, vietnamSort = {key:'pc_id', asc:true};
function vnDone(){ return false; }
function parseOddEnergy(v){ return Number(v) || 0; }
function order(){ renderVietnam(); const h = els['vietnam-body'].innerHTML;
  return [...h.matchAll(/<tr[^>]*>[\s\S]*?<\/tr>/g)].map(m => { const t = m[0].match(/<td class="px-3[^>]*>([^<]*)<\/td>/g) || [];
    return t.map(x => x.replace(/<[^>]+>/g, '')); }); }
const o = {};
o.pcAsc = order().map(r => r[0]);
vietnamSort = {key:'pc_id', asc:false}; o.pcDesc = order().map(r => r[0]);
vietnamSort = {key:'gear_power', asc:true}; o.gpAsc = order().map(r => r[2]);
vietnamSort = {key:'gear_power', asc:false}; o.gpDesc = order().map(r => r[2]);
o.html = els['vietnam-body'].innerHTML;
console.log(JSON.stringify(o));
""" % json.dumps(data)
    try:
        o = _run_node(js)
    except Exception as e:
        ok("JS7 node 실행", False, str(e)[:300])
        return
    ok("JS7-a PC 오름차순 = 01,01,03,10,22d", o["pcAsc"] == ["PC-01", "PC-01", "PC-03", "PC-10", "PC-22d"], str(o["pcAsc"]))
    ok("JS7-b PC 내림차순 = 22d,10,03,01,01", o["pcDesc"] == ["PC-22d", "PC-10", "PC-03", "PC-01", "PC-01"], str(o["pcDesc"]))
    ok("JS7-c 장비 오름차순: 숫자 작은 순, 빈 값은 맨 뒤",
       o["gpAsc"][:3] == ["2,500", "2,900", "3,000"] and o["gpAsc"][3:] == ["–", "–"], str(o["gpAsc"]))
    ok("JS7-d 장비 내림차순: 큰 순, 빈 값은 여전히 맨 뒤",
       o["gpDesc"][:3] == ["3,000", "2,900", "2,500"] and o["gpDesc"][3:] == ["–", "–"], str(o["gpDesc"]))
    bad = [h for h in re.findall(r'onchange="([^"]*)"', o["html"]) if "alert" in h]
    ok("JS3-k 베트남 표 vnToggle 에 slot 주입 없음", not bad, str(bad)[:160])


# ───────────────── JS4 · JS5 · JS8 — 선택 · 일괄 · 라이브 ─────────────────

_SEL_STATE = {
    "PC-22": {"pc_id": "PC-22", "status": "hunting"},
    "PC-22b": {"pc_id": "PC-22b", "status": "offline"},
    "PC-22c": {"pc_id": "PC-22c", "status": "offline"},
    "PC-05": {"pc_id": "PC-05", "status": "hunting"},
    "PC-TEST": {"pc_id": "PC-TEST", "status": "hunting"},
    "PC-DEMOb": {"pc_id": "PC-DEMOb", "status": "hunting"},
    "PC-09": {"pc_id": "PC-09", "status": "hunting"},
    "PC-06": {"pc_id": "PC-06", "status": "no_account"},
}


def t_js4_js5_js8():
    if not _need_node("JS4"):
        return
    src = main.HTML_DASHBOARD
    try:
        fns = _grab(src, ("isAcctSuf", "baseId", "isFakePc", "isExcludedPc", "liveCardOf", "selectedBases",
                          "stackIds", "selectAllPcs", "updateSelBar", "clearSelection", "withBulk", "bulkCmd",
                          "selCmd", "sellAllSel", "settleSel", "openLive", "closeLive"),
                    ("cmdTargetOf", "cmdTargets"))
        consts = "\n".join(_line(src, p) for p in ("const MAX_ACCT", "const ACCT_LABELS", "const ACCT_SUFFIX",
                                                    "const FAKE_PC_BASES"))
    except Exception as e:
        ok("JS4 함수를 잘라낸다", False, str(e))
        return
    js = _DOM + consts + "\n" + fns + r"""
let state = %s;
let RETIRED = new Set(['PC-09']);
let selectedPcs = new Set();
let _bulkDepth = 0;
const STATUS_CFG = {hunting:{online:true}, offline:{online:false}, no_account:{online:false}};
let raw = [], confirms = [];
// sendCmd 흉내 — 진짜 sendCmd(사고 307)처럼 안 도는 카드는 살아 있는 형제로 돌려 ★실제로 닿는 id★ 를 적는다
async function sendCmd(id, command){
  raw.push([id, command]);
  return true;
}
function effective(id){ const on = (STATUS_CFG[(state[id]||{}).status||'offline']||{}).online;
  if (on) return id; const l = liveCardOf(baseId(id)); return (l && l.pc_id) || id; }
function confirm(t){ confirms.push(String(t)); return true; }
function alert(){}
function loadCmdHistory(){}
function getSalePrice(){ return 1000; } function isSalePriceConfirmed(){ return true; }
let livePc = null, liveTimer = null, liveFails = 0, liveArmedAt = 0;
function liveTick(){} function setInterval(){ return 1; } function clearInterval(){}
function tally(){ const c = {}; raw.forEach(([id]) => { const e = effective(id); c[e] = (c[e]||0) + 1; }); return c; }
(async () => {
  const o = {};
  selectAllPcs(); o.selAll = [...selectedPcs].sort(); clearSelection();
  raw = []; await bulkCmd('start'); o.bulk = tally(); o.bulkToast = toasts[toasts.length-1];
  ['PC-22','PC-22b','PC-22c','PC-05'].forEach(i => selectedPcs.add(i));
  raw = []; confirms = []; await selCmd('collect_info'); o.sel = tally(); o.selConfirm = confirms[0] || '';
  ['PC-22','PC-22b','PC-22c'].forEach(i => selectedPcs.add(i));
  confirms = []; await sellAllSel(); o.sellConfirm = confirms[0] || '';
  ['PC-22','PC-22b','PC-22c'].forEach(i => selectedPcs.add(i));
  confirms = []; await settleSel(); o.settleConfirm = confirms[0] || '';
  raw = []; await openLive('PC-22b'); o.livePc = livePc; o.liveOn = raw.map(x => x[0]);
  raw = []; await closeLive(); o.liveOff = raw.map(x => x[0]);
  console.log(JSON.stringify(o));
})().catch(e => { console.error(e && e.stack || e); process.exit(3); });
""" % json.dumps(_SEL_STATE)
    try:
        o = _run_node(js)
    except Exception as e:
        ok("JS4 node 실행", False, str(e)[:300])
        return
    ok("JS8-a 전체선택에 가짜·은퇴·계정없음 PC 없음",
       o["selAll"] == ["PC-05", "PC-22", "PC-22b", "PC-22c"], str(o["selAll"]))
    ok("JS8-b 일괄 명령이 가짜·은퇴·계정없음 PC 로 안 나간다",
       set(o["bulk"]) == {"PC-22", "PC-05"}, str(o["bulk"]))
    ok("JS4-a 일괄 명령: 도는 매크로 PC-22 에 1건만(스택 3장)", o["bulk"].get("PC-22") == 1, str(o["bulk"]))
    ok("JS4-b 일괄 토스트 = 물리 PC 2대", "2대" in (o["bulkToast"] or ""), str(o["bulkToast"]))
    ok("JS4-c 선택 명령: PC-22 1건 · PC-05 1건", o["sel"] == {"PC-22": 1, "PC-05": 1}, str(o["sel"]))
    ok("JS4-d 선택 명령 확인창 = 2대", "2대" in o["selConfirm"], o["selConfirm"][:60])
    ok("JS4-e 판매 확인창 = 물리 PC 1대(카드 3장)", "선택 1대" in o["sellConfirm"], o["sellConfirm"][:40])
    ok("JS4-f 준비 확인창 = 물리 PC 1대(카드 3장)", "선택 1대" in o["settleConfirm"], o["settleConfirm"][:40])
    ok("JS5-a 안 도는 PC-22b 로 열면 화면은 PC-22 를 당긴다", o["livePc"] == "PC-22", str(o["livePc"]))
    ok("JS5-b live_on 도 PC-22 로", o["liveOn"] == ["PC-22"], str(o["liveOn"]))
    ok("JS5-c live_off 도 같은 PC-22 로", o["liveOff"] == ["PC-22"], str(o["liveOff"]))


# ───────────────── JS9 — 초기화가 첫 /status 실패에도 산다 ─────────────────

def t_js9_init():
    if not _need_node("JS9"):
        return
    src = main.HTML_DASHBOARD
    try:
        mark = src.index("// ─── 초기화 ──")
        i = src.index("(async()=>{", mark)
        j = src.index("\n})();", i)
        iife = src[i:j + len("\n})();")]
    except Exception as e:
        ok("JS9 초기화 IIFE 를 잘라낸다", False, str(e))
        return
    js = _DOM + r"""
let state = {}; let RETIRED = new Set(); let _ws = null, _wsLastMsg = 0; let STATE_VER = -1;
const calls = []; const ivs = [];
['renderCards','loadCmdHistory','loadCharTable','connectWS','loadSalePrice','loadAwakenPreset','loadServerSummary',
 'updResultSweep','rotAllowBadge','checkServerBoot'].forEach(n => { global[n] = () => { calls.push(n); }; });
global.setInterval = (fn, ms) => { ivs.push([fn, ms]); return ivs.length; };
global.clearInterval = () => {};
let fetchOk = false;
global.fetch = async (u) => { if (!fetchOk) throw new Error('net down');
  return {ok:true, json: async () => ({pcs:[{pc_id:'PC-01'}], retired:['PC-09']})}; };
process.on('unhandledRejection', () => {});
""" + iife + r"""
setTimeout(async () => {
  const o = {calls: [...new Set(calls)], nIv: ivs.length};
  fetchOk = true;
  for (const [fn, ms] of ivs.filter(x => x[1] === 5000)) await fn();
  o.stateAfter = Object.keys(state); o.retired = [...RETIRED];
  // WS state 가 먼저 왔으면(STATE_VER≠-1) 늦은 /status 재시도가 그 위를 덮지 않는다(B2-5)
  state = {'PC-77': {}}; STATE_VER = 5; fetchOk = true;
  for (const [fn, ms] of ivs.filter(x => x[1] === 5000)) await fn();
  o.stateFeed = Object.keys(state);
  console.log(JSON.stringify(o));
}, 50);
"""
    try:
        o = _run_node(js)
    except Exception as e:
        ok("JS9 node 실행", False, str(e)[:300])
        return
    ok("JS9-a 첫 /status 실패에도 connectWS 가 돈다", "connectWS" in o["calls"], str(o["calls"]))
    ok("JS9-b 첫 /status 실패에도 checkServerBoot 가 돈다", "checkServerBoot" in o["calls"], str(o["calls"]))
    ok("JS9-c 주기 작업(setInterval)이 걸린다", o["nIv"] >= 5, str(o["nIv"]))
    ok("JS9-d /status 가 살아나면 재시도가 state·은퇴 목록을 채운다",
       o["stateAfter"] == ["PC-01"] and o["retired"] == ["PC-09"], str(o))
    ok("JS9-e WS state 가 먼저 오면 /status 재시도가 그것을 덮지 않는다(B2-5)", o.get("stateFeed") == ["PC-77"], str(o.get("stateFeed")))


def test_all():
    run_all([t_js1_server, t_time, t_js6_lists, t_js3_inject, t_js3_js10_table, t_js7_sort, t_js4_js5_js8,
             t_js9_init])
    finish("test_js_b", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
