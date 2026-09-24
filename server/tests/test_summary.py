# -*- coding: utf-8 -*-
"""[대시보드] 전광판 요약(/summary = /api/fv/snapshot global.totals) 회귀 가드 — 실제 호출.

2026-09-23 사후 반증(아이온2 독립 에이전트)이 1dd6afbe 에서 잡은 4건을 다시는 못 무너지게 한다.
  #1 PC-TEST·PC-DEMO 가 서버 합계(구독 「모름」 +1)에 들어갔다 — 화면 dkSubCount 는 뺀다.
  #2 서버 합계가 늘 정수라 한 번도 못 읽은 칸이 「0」 으로 떴다 — 모름은 null.
  #3 FV_API.md 는 「카드 그대로」 인데 제외 PC 카드의 캐릭 합이 0/None — 문서를 코드에 맞췄다(여기선 코드 쪽 모양을 못 박는다).
  #4 /summary 실패 시 옛 값 무기한 · 회랑 툴팁이 화면 계산이라 타일 숫자와 어긋남.
JS 는 main.HTML_DASHBOARD 에서 함수를 그대로 잘라 node 로 돌린다(grep 이 아니라 실행).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 79     # 2026-09-23 실측값으로 맞춘다 — node 없으면 JS 가 빠지므로 일부러 빨간불

NOW_ISO = "2099-01-01T00:00:00"     # 리셋 보정이 끼지 않게 먼 미래 수집 시각


def _row(pid, total_kina=0, status="hunting"):
    return {"pc_id": pid, "status": status, "_total_kina": total_kina,
            "_bug_count": 0, "macro_version": "1.0.0", "daily_progress": []}


def _info(pid, chars):
    return {"pc_id": pid, "collected_at": NOW_ISO, "chars": chars}


class _Patch:
    """main 전역을 잠깐 갈아끼우고 되돌린다."""

    def __init__(self, rows, infos, retired=(), no_account=(), corridor=None):
        self.rows, self.infos = rows, infos
        self.retired, self.no_account = set(retired), set(no_account)
        self.corridor = corridor or {}

    def __enter__(self):
        self._o = (main._build_full_state, main.get_all_char_info,
                   set(main.RETIRED_PCS), set(main.NO_ACCOUNT_PCS), dict(main.CORRIDOR_PROG))

        async def _rows(tenant):
            return [dict(r) for r in self.rows]

        async def _infos():
            return [json.loads(json.dumps(i)) for i in self.infos]
        main._build_full_state = _rows
        main.get_all_char_info = _infos
        main.RETIRED_PCS.clear()
        main.RETIRED_PCS.update(self.retired)
        main.NO_ACCOUNT_PCS.clear()
        main.NO_ACCOUNT_PCS.update(self.no_account)
        main.CORRIDOR_PROG.clear()
        main.CORRIDOR_PROG.update(self.corridor)
        return self

    def __exit__(self, *e):
        main._build_full_state, main.get_all_char_info = self._o[0], self._o[1]
        main.RETIRED_PCS.clear()
        main.RETIRED_PCS.update(self._o[2])
        main.NO_ACCOUNT_PCS.clear()
        main.NO_ACCOUNT_PCS.update(self._o[3])
        main.CORRIDOR_PROG.clear()
        main.CORRIDOR_PROG.update(self._o[4])


SUB_CH = {"odd_energy": "300(+1,195)/840", "trade_kina": 100, "gakin_kina": 0}


async def t_exclusion_and_null():
    rows = [_row("PC-01", 1000), _row("PC-02", 10), _row("PC-TEST", 5), _row("PC-DEMO", 7),
            _row("PC-05", 50), _row("PC-06", 0, "no_account"), _row("PC-07b", 3)]
    infos = [_info("PC-01", [dict(SUB_CH)]),
             _info("PC-DEMO", [dict(SUB_CH, trade_kina=7)]),
             _info("PC-05", [dict(SUB_CH, trade_kina=50, awakening_ticket=2)]),
             _info("PC-06", [dict(SUB_CH, trade_kina=60, awakening_ticket=1)])]
    with _Patch(rows, infos, retired={"PC-05"}, no_account={"PC-06"}):
        snap = await main._fv_build_snapshot("main")
    t = snap["global"]["totals"]
    # #1 가짜 PC
    ok("#1 PC-TEST·PC-DEMO 는 구독 집계에 안 든다(모름 2 = PC-02·PC-07b)",
       t["subscribed"] == {"sub": 1, "nosub": 0, "unknown": 2}, str(t["subscribed"]))
    ok("#1-b PC-DEMO 의 거래키나가 합계에 안 든다", t["trade_kina"] == 100, str(t["trade_kina"]))
    ok("#1-c 창고키나 합계도 가짜·은퇴·계정없음 제외(1000+10+3)", t["total_kina"] == 1013, str(t["total_kina"]))
    ok("#1-d 판정 함수: PC-TEST·PC-DEMO 는 제외, 보통 계정 카드 PC-07b 는 아님",
       main._fv_pc_excluded("main", "PC-TEST") and main._fv_pc_excluded("main", "PC-DEMO")
       and not main._fv_pc_excluded("main", "PC-07b"))
    ok("#1-e 소문자 가짜도 제외", main._fv_pc_excluded("main", "pc-test"))
    # #2 모름 = null
    ok("#2 한 캐릭도 안 읽은 각성전은 null(제외 PC 가 읽은 값은 안 센다)",
       t["awakening_ticket"] is None, repr(t["awakening_ticket"]))
    ok("#2-b 읽었는데 0 인 각인키나는 0(null 아님)", t["gakin_kina"] == 0 and t["gakin_kina"] is not None,
       repr(t["gakin_kina"]))
    ok("#2-c 오드에너지 300(+1,195)/840 → 1495", t["odd_energy"] == 1495, repr(t["odd_energy"]))
    ok("#2-d total_kina·bugs 는 정수 유지",
       all(isinstance(t[k], int) and not isinstance(t[k], bool) for k in ("total_kina", "bugs")))
    ok("#2-d2 회랑 스냅샷이 하나도 없으면 corridor_remaining 은 null(모름)", t["corridor_remaining"] is None,
       repr(t["corridor_remaining"]))
    # #3 제외 카드 모양 (FV_API.md 2026-09-23 절과 같은 말)
    pcs = snap["pcs"]
    ok("#3 제외 PC 카드도 pcs 에 남는다", all(k in pcs for k in ("PC-TEST", "PC-DEMO", "PC-05", "PC-06")))
    ok("#3-b 제외 카드의 캐릭 합 4종 = 0 · subscribed = null",
       all(pcs[k]["progress"][f] == 0 for k in ("PC-05", "PC-06", "PC-DEMO")
           for f in ("trade_kina", "gakin_kina", "odd_energy", "awakening_ticket"))
       and all(pcs[k]["progress"]["subscribed"] is None for k in ("PC-05", "PC-06", "PC-DEMO")))
    ok("#3-c 제외 안 된 카드는 그대로(PC-01 거래 100·구독 True)",
       pcs["PC-01"]["progress"]["trade_kina"] == 100 and pcs["PC-01"]["progress"]["subscribed"] is True)
    ok("#3-d 카드 캐릭 합은 여전히 정수(카드 쪽 계약 「없으면 0」 불변)",
       all(isinstance(v["progress"][f], int) for v in pcs.values()
           for f in ("trade_kina", "gakin_kina", "odd_energy", "awakening_ticket")))
    ok("#3-e 내부 표식 _seen 이 응답에 새지 않는다", "_seen" not in json.dumps(snap, default=str))

    # 아무것도 못 읽은 함대
    with _Patch([_row("PC-01"), _row("PC-02")], []):
        t0 = (await main._fv_build_snapshot("main"))["global"]["totals"]
    ok("#2-e 정보수집 0건이면 캐릭 합 4종 전부 null",
       all(t0[k] is None for k in ("trade_kina", "gakin_kina", "odd_energy", "awakening_ticket")), str(t0))
    ok("#2-f 그때 구독은 전부 모름", t0["subscribed"] == {"sub": 0, "nosub": 0, "unknown": 2})
    # 빈 문자열은 못 읽은 것
    with _Patch([_row("PC-01")], [_info("PC-01", [{"trade_kina": "", "awakening_ticket": None}])]):
        t1 = (await main._fv_build_snapshot("main"))["global"]["totals"]
    ok("#2-g 빈 문자열·None 은 「읽음」 이 아니다", t1["trade_kina"] is None and t1["awakening_ticket"] is None, str(t1))
    with _Patch([_row("PC-01")], [_info("PC-01", [{"awakening_ticket": 0}])]):
        t2 = (await main._fv_build_snapshot("main"))["global"]["totals"]
    ok("#2-h 각성전 0 을 읽었으면 0", t2["awakening_ticket"] == 0, repr(t2["awakening_ticket"]))


async def t_corridor_detail_and_summary():
    now = time.time()
    cor = {"PC-01": {"remaining": 2, "total": 10, "ts": now},
           "PC-02": {"remaining": 1, "total": 9, "ts": 0},          # 리셋 전 = 낡음 → total
           "PC-05": {"remaining": 5, "total": 5, "ts": now},        # 은퇴 → 빠짐
           "PC-TEST": {"remaining": 4, "total": 4, "ts": now}}      # 가짜 → 빠짐
    with _Patch([_row("PC-01"), _row("PC-02")], [], retired={"PC-05"}, corridor=cor):
        snap = await main._fv_build_snapshot("main")
        _o = main._require_session
        main._require_session = lambda r: "main"
        try:
            resp = await main.dashboard_summary(Req())
        finally:
            main._require_session = _o
    t = snap["global"]["totals"]
    cd = t.get("corridor_detail") or {}
    ok("#4 corridor_detail 신선 1대·남음 2 / 낡음 1대·남음 9(정원)",
       cd == {"fresh_n": 1, "fresh_left": 2, "stale_n": 1, "stale_left": 9}, str(cd))
    ok("#4-b corridor_remaining = fresh_left + stale_left", t["corridor_remaining"] == 11, str(t["corridor_remaining"]))
    ok("#4-c 은퇴·가짜 PC 는 회랑 스냅샷에서도 빠진다",
       set(snap["global"]["corridor"]) == {"PC-01", "PC-02"}, str(sorted(snap["global"]["corridor"])))
    body = json.loads(resp.body)
    ok("#4-d /summary 는 스냅샷 global.totals 와 같은 모양·값", body == json.loads(json.dumps(t)), str(body)[:120])


# ───────────────── JS — HTML_DASHBOARD 에서 함수를 잘라 node 로 실행 ─────────────────

def _js_func(src: str, name: str) -> str:
    """`function name(` 부터 짝 맞는 `}` 까지. 문자열·템플릿(${} 중첩)·주석·정규식 없는 코드 가정."""
    i = src.index("function %s(" % name)
    j = src.index("{", i)
    if src[max(0, i - 6):i] == "async ":      # async 함수는 앞말까지 — 빠지면 await 가 문법 오류
        i -= 6
    depth, k, n = 0, j, len(src)
    stack = []          # 템플릿 안의 ${ } 깊이
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
                stack.pop()          # ${ … } 닫힘 → 아래 깔린 "tpl" 로 복귀
            elif depth == 0:
                return src[i:k + 1]
        k += 1
    raise ValueError("짝이 안 맞음: " + name)




_JS_STUBS = r"""
const els = {};
const tiles = {};
function mkEl(id){
  return els[id] || (els[id] = {id, textContent:'', innerHTML:'', title:'',
    parentElement: {title:''},
    closest(){ return tiles[id] || (tiles[id] = {title:''}); }});
}
const document = { getElementById: mkEl };
const STATUS_CFG = {hunting:{online:true}, offline:{online:false}};
const ACCT_SUFFIX = 'bcdefghi';
function isDungeonDone(){ return true; }
function dpDone(){ return false; }
function parseOddEnergy(v){ return Number(v) || 0; }
function fmtKinaKor(n){ return 'K' + n; }
function fmtKina(n){ return 'k' + n; }
function fmtKinaShort(n){ return 's' + n; }
const RETIRED = new Set();
function dkQuote(){}
function dkSubCount(){ return {sub: 9, nosub: 9, unknown: 9}; }
let charTableData = [];
const DK_SUM = {kina:0, trade:null};
let state = {};
let corridorRemaining = {};
let SERVER_SUMMARY = null, SERVER_SUMMARY_AT = 0;
let CHAR_TABLE_AT = 0, CORRIDOR_AT = 0;
"""

_JS_FUNCS = ("isAcctSuf", "baseId", "isFakePc", "sumClock", "ageNote", "serverSum", "serverSumNote",
             "summaryPcs", "redrawSummary", "loadServerSummary", "refreshSummary", "updateCorridorTile",
             "dkHero", "nmTicketText", "nmTicketFull", "nmTicketWarn", "isExcludedPc")
_JS_CONSTS = ("const SERVER_SUMMARY_TTL_MS", "const FAKE_PC_BASES", "const NIGHTMARE_TICKET_MAX")

FAKE_IDS = ["PC-TEST", "pc-test", "PC-TESTb", "pc-testb", "PC-DEMOc", " PC-TEST ", " pc-demo", "PC-DEMO",
            "PC-01", "PC-20b", "PC-TESTX", "PC-TESTE", "PC-DEM", "", "b"]


def _run_node(js: str):
    node = shutil.which("node")
    if not node:
        return None
    d = tempfile.mkdtemp(prefix="sumjs_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])
    return json.loads(r.stdout.strip().splitlines()[-1])


_SCENARIO = r"""
const out = {};
function snap(){ return {trade: els['cnt-trade-kina'].textContent, aw: els['cnt-awakening'].textContent,
  odd: els['cnt-odd-energy'].textContent, tk: els['cnt-total-kina'].textContent,
  trTitle: els['cnt-trade-kina'].title, dkTrade: DK_SUM.trade}; }
(async () => {
const NOW = sumClock();
// A. 서버값 신선 · 모름(null)
SERVER_SUMMARY = {trade_kina:null, awakening_ticket:null, odd_energy:null, total_kina:5, corridor_remaining:0};
SERVER_SUMMARY_AT = NOW;
charTableData = [{trade_kina: 9, awakening_ticket: 1, odd_energy: 3}];
refreshSummary([]); out.A = snap();
// B. 서버값 신선 · 읽었는데 0
SERVER_SUMMARY = {trade_kina:0, awakening_ticket:0, odd_energy:0, total_kina:0};
refreshSummary([]); out.B = snap();
// C. 서버값 61초 묵음 → 화면 계산으로
SERVER_SUMMARY = {trade_kina:999, awakening_ticket:99, odd_energy:99, total_kina:99};
SERVER_SUMMARY_AT = sumClock() - 61000;
refreshSummary([]); out.C = snap();
out.C_sum = serverSum();
// C2. 폴백 재료(캐릭 표)가 10분 묵었으면 툴팁이 그 나이를 말한다
CHAR_TABLE_AT = sumClock() - 600000;
out.C2_note = serverSumNote();
CHAR_TABLE_AT = sumClock();
out.C3_note = serverSumNote();
// C4. 빈 문자열은 폴백에서도 「못 읽음」
SERVER_SUMMARY = null;
charTableData = [{trade_kina: '', awakening_ticket: ''}];
refreshSummary([]); out.C4 = snap();
// D. 한 번도 못 받음
SERVER_SUMMARY = null; SERVER_SUMMARY_AT = 0;
out.D_sum = serverSum(); out.D_note = serverSumNote();
// E. 회랑 툴팁은 서버 corridor_detail
SERVER_SUMMARY = {corridor_remaining: 7, corridor_detail:{fresh_n:2,fresh_left:3,stale_n:1,stale_left:4}};
SERVER_SUMMARY_AT = sumClock();
corridorRemaining = {'PC-09': {remaining: 40, total: 50, stale: false}};
updateCorridorTile();
out.E = {num: els['cnt-corridor'].textContent, title: tiles['cnt-corridor'].title};
// E2. 서버에 회랑 스냅샷이 없으면(null) 「–」
SERVER_SUMMARY = {corridor_remaining: null, corridor_detail:{fresh_n:0,fresh_left:0,stale_n:0,stale_left:0}};
updateCorridorTile();
out.E2 = els['cnt-corridor'].textContent;
// F. 회랑 — 서버 끊김이면 화면 계산 숫자와 내역이 짝 + 회랑 목록 나이
SERVER_SUMMARY = {corridor_remaining: 7, corridor_detail:{fresh_n:2,fresh_left:3,stale_n:1,stale_left:4}};
SERVER_SUMMARY_AT = sumClock() - 61000;
CORRIDOR_AT = sumClock() - 1200000;
updateCorridorTile();
out.F = {num: els['cnt-corridor'].textContent, title: tiles['cnt-corridor'].title};

// G. loadServerSummary — 성공이면 시각을 찍고 다시 그린다
state = {'PC-01': {pc_id:'PC-01', status:'hunting'}};
charTableData = [{trade_kina: 9}];
SERVER_SUMMARY = null; SERVER_SUMMARY_AT = 0;
globalThis.fetch = async () => ({ok: true, status: 200, json: async () =>
  ({trade_kina: 5, awakening_ticket: 1, odd_energy: 2, total_kina: 3,
    subscribed: {sub: 3, nosub: 1, unknown: 0}, corridor_remaining: 0})});
els['cnt-trade-kina'].textContent = 'X';
let _clr = 0; const _ct = globalThis.clearTimeout;
globalThis.clearTimeout = (h) => { _clr++; return _ct(h); };
await loadServerSummary();
globalThis.clearTimeout = _ct;
out.G1 = {at: SERVER_SUMMARY_AT > 0, sum: !!serverSum(), trade: els['cnt-trade-kina'].textContent,
          subOn: els['dk-h-sub-on'].textContent,
          // 성공 직후 시각이 sumClock 기준이어야 TTL 이 실제로 끝난다(Date.now 로 찍으면 영원히 신선)
          ttl59: !!serverSum(sumClock() + 59000), ttl61: serverSum(sumClock() + 61000) === null,
          cleared: _clr};
// G2. HTTP 500 — 낡은 값이면 폴백으로 다시 그린다
SERVER_SUMMARY_AT = sumClock() - 61000;
globalThis.fetch = async () => ({ok: false, status: 500, json: async () => ({})});
els['cnt-trade-kina'].textContent = 'X';
await loadServerSummary();
out.G2 = {trade: els['cnt-trade-kina'].textContent, subOn: els['dk-h-sub-on'].textContent};
// G3. 네트워크 예외 — 역시 다시 그린다
globalThis.fetch = async () => { throw new Error('net'); };
els['cnt-trade-kina'].textContent = 'X';
await loadServerSummary();
out.G3 = els['cnt-trade-kina'].textContent;
// G4. 응답이 안 오면 10초 타이머로 끊는다(타이머를 바로 울려 확인)
const _st = globalThis.setTimeout;
let _toMs = [];
globalThis.setTimeout = (fn, ms) => { _toMs.push(ms); fn(); return 0; };
globalThis.fetch = (u, o) => new Promise((res, rej) => {
  if (!o || !o.signal) return;                      // 신호가 없으면 영원히 안 끝난다 → 시험이 멈춰 잡힌다
  if (o.signal.aborted) return rej(new Error('abort'));
  o.signal.addEventListener('abort', () => rej(new Error('abort')));
});
els['cnt-trade-kina'].textContent = 'X';
const g4 = await Promise.race([loadServerSummary().then(() => 'done'),
                               new Promise(r => _st(() => r('hang'), 2000))]);
globalThis.setTimeout = _st;
out.G4 = {how: g4, trade: els['cnt-trade-kina'].textContent, ms: _toMs};

// G5. redrawSummary — 한 칸이 던져도 나머지 칸은 그린다
{ const _rs = refreshSummary;
  refreshSummary = () => { throw new Error('boom'); };
  SERVER_SUMMARY = {corridor_remaining: 11, corridor_detail:{fresh_n:1,fresh_left:11,stale_n:0,stale_left:0}};
  SERVER_SUMMARY_AT = sumClock();
  els['cnt-corridor'].textContent = 'X';
  const _ce = console.error; console.error = () => {};
  let threw = false; try { redrawSummary(); } catch(e) { threw = true; }
  console.error = _ce;
  refreshSummary = _rs;
  out.G5 = {threw, corridor: els['cnt-corridor'].textContent}; }

// K. 가짜 PC 는 isExcludedPc 한 곳에서 빠진다 → 폴백 회랑 합계·효율 평균에서도 빠진다
out.K = ['PC-DEMO','PC-TESTb','pc-test','PC-01','PC-20b'].map(isExcludedPc);
SERVER_SUMMARY = null; SERVER_SUMMARY_AT = 0; CORRIDOR_AT = sumClock();
state = {'PC-01': {pc_id:'PC-01', status:'hunting', efficiency: 10},
         'PC-DEMO': {pc_id:'PC-DEMO', status:'hunting', efficiency: 90}};
corridorRemaining = {'PC-01': {remaining: 2, total: 5, stale: false},
                     'PC-TEST': {remaining: 0, total: 30, stale: true}};
updateCorridorTile();
out.K2 = els['cnt-corridor'].textContent;
dkHero();
out.K3 = els['dk-h-eff'].innerHTML;

// D2. 서버값을 한 번도 못 받았을 때(「대기 중」)도 폴백 재료 나이를 말한다
SERVER_SUMMARY = null; SERVER_SUMMARY_AT = 0; CHAR_TABLE_AT = sumClock() - 600000;
out.D2_note = serverSumNote();
CHAR_TABLE_AT = sumClock();

// H. dkHero — 효율 없는 함대는 「–」
state = {'PC-01': {pc_id:'PC-01', status:'hunting'}};
SERVER_SUMMARY_AT = sumClock();
dkHero();
out.H = els['dk-h-eff'].innerHTML;

// I. 가짜 PC 판정 · summaryPcs 모집단
out.I = FAKE_IDS.map(isFakePc);
state = {'PC-01': {pc_id:'PC-01'}, 'PC-TEST': {pc_id:'PC-TEST'}, 'PC-DEMO': {pc_id:'PC-DEMO'},
         'PC-TESTb': {pc_id:'PC-TESTb'}, 'PC-02b': {pc_id:'PC-02b'}};
out.I2 = summaryPcs().map(p => p.pc_id).sort();

// J. 악몽 티켓 — 주인님 #82: 상한 14 · 12↑ 경고 · 14↑ 빨강 · 넘는 실측은 그대로
out.J = {five: nmTicketText(5), zero: nmTicketText(0), nul: nmTicketText(null), empty: nmTicketText(''),
         over: nmTicketText(16), max: NIGHTMARE_TICKET_MAX, warnAt: NIGHTMARE_TICKET_WARN,
         full: [11, 12, 13, 14, 16, '14', null, '', '?'].map(nmTicketFull),
         warn: [11, 12, 13, 14, 16, '12', null, '', '?'].map(nmTicketWarn)};
console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(3); });
"""


def t_js():
    src = main.HTML_DASHBOARD
    consts = []
    for c in _JS_CONSTS:
        ok("JS-0 상수가 있다: " + c, c in src)
        if c in src:
            consts.append(src[src.index(c):src.index("\n", src.index(c))])
    try:
        fns = "\n".join(_js_func(src, n) for n in _JS_FUNCS)
    except Exception as e:
        ok("JS-0b 함수를 잘라낸다", False, str(e))
        return
    js = (_JS_STUBS + "\n".join(consts) + "\nconst FAKE_IDS = " + json.dumps(FAKE_IDS) + ";\n"
          + fns + "\n" + _SCENARIO)
    try:
        o = _run_node(js)
    except Exception as e:
        ok("JS-실행 node 가 함수를 돌린다", False, str(e)[:400])
        return
    if o is None:
        ok("JS-실행 node 가 있어야 한다(대시보드 JS 는 node 로만 시험된다)", False)
        return
    ok("JS-1 서버 null → 거래키나 「–」", o["A"]["trade"] == "–", str(o["A"]))
    ok("JS-1b 서버 null → 각성전 「–」", o["A"]["aw"] == "–")
    ok("JS-1c 서버 null → 오드 「–」", o["A"]["odd"] == "–")
    ok("JS-1d 서버 null → 히어로 거래키나 null(「수집 전」 문구 살아남)", o["A"]["dkTrade"] is None)
    ok("JS-1e 칸 툴팁에 출처 「서버 계산」", "서버 계산" in o["A"]["trTitle"], o["A"]["trTitle"])
    ok("JS-2 서버 0 → 거래키나 K0·각성전 0", o["B"]["trade"] == "K0" and o["B"]["aw"] == "0", str(o["B"]))
    ok("JS-2b 서버 0 → 히어로 거래키나 0(null 아님)", o["B"]["dkTrade"] == 0)
    ok("JS-3 61초 묵은 서버값은 버리고 화면 계산(K9)", o["C"]["trade"] == "K9" and o["C_sum"] is None, str(o["C"]))
    ok("JS-3b 그때 툴팁이 「끊김」 을 말한다", "끊김" in o["C"]["trTitle"], o["C"]["trTitle"])
    ok("JS-3c 각성전도 화면 계산(1)", o["C"]["aw"] == "1")
    ok("JS-3d 폴백 재료가 10분 묵었으면 「캐릭 표 10분째 못 받음」", "캐릭 표 10분째" in o["C2_note"], o["C2_note"])
    ok("JS-3e 방금 받은 캐릭 표면 나이를 안 붙인다", "캐릭 표" not in o["C3_note"], o["C3_note"])
    ok("JS-3f 폴백도 빈 문자열은 「–」(서버 _seen 과 같은 뜻)",
       o["C4"]["trade"] == "–" and o["C4"]["aw"] == "–", str(o["C4"]))
    ok("JS-4 한 번도 못 받았으면 serverSum null · 「대기 중」", o["D_sum"] is None and "대기" in o["D_note"])
    ok("JS-5 회랑 숫자 = 서버 7", o["E"]["num"] == "7", str(o["E"]))
    ok("JS-5b 회랑 툴팁 = 서버 내역(2대 남은 3 / 1대 남은 4)",
       "2대: 남은 3" in o["E"]["title"] and "1대: 남은 4" in o["E"]["title"], o["E"]["title"])
    ok("JS-5c 서버 회랑 null → 「–」", o["E2"] == "–", o["E2"])
    ok("JS-6 서버 끊기면 회랑 숫자·툴팁 둘 다 화면 계산(40)",
       o["F"]["num"] == "40" and "1대: 남은 40" in o["F"]["title"], str(o["F"]))
    ok("JS-6b 그때 회랑 목록 나이(20분)도 적는다", "회랑 목록 20분째" in o["F"]["title"], o["F"]["title"])
    ok("JS-8 loadServerSummary 성공 → 시각 찍음·서버값 유효·다시 그림(K5)",
       o["G1"]["at"] and o["G1"]["sum"] and o["G1"]["trade"] == "K5", str(o["G1"]))
    ok("JS-8b 성공 뒤 히어로 구독 O 는 서버값(3)", str(o["G1"]["subOn"]) == "3", str(o["G1"]))
    ok("JS-8c HTTP 500 이어도 다시 그린다 → 낡았으니 화면 계산(K9)", o["G2"]["trade"] == "K9", str(o["G2"]))
    ok("JS-8d 서버값이 낡으면 히어로 구독도 화면 계산(dkSubCount 9)", str(o["G2"]["subOn"]) == "9", str(o["G2"]))
    ok("JS-8e 네트워크 예외여도 다시 그린다", o["G3"] == "K9", o["G3"])
    ok("JS-8f 응답이 안 오면 타이머가 끊고 다시 그린다", o["G4"]["how"] == "done" and o["G4"]["trade"] == "K9",
       str(o["G4"]))
    g1 = o["G1"]
    ok("JS-8g 성공 뒤 59초는 신선, 61초면 serverSum null(TTL 이 실제로 끝난다)", g1["ttl59"] and g1["ttl61"], str(g1))
    ok("JS-8h 성공이어도 타임아웃 타이머를 치운다(clearTimeout)", g1["cleared"] >= 1, str(g1))
    ok("JS-8i 끊는 타이머는 10초 이하", bool(o["G4"]["ms"]) and all(0 < m <= 10000 for m in o["G4"]["ms"]), str(o["G4"]["ms"]))
    ok("JS-8j redrawSummary — 전광판 칸 하나가 던져도 회랑은 그린다",
       o["G5"]["threw"] is False and o["G5"]["corridor"] == "11", str(o["G5"]))
    ok("JS-12 isExcludedPc 가 가짜 PC 도 뺀다(PC-DEMO·PC-TESTb·pc-test), 진짜는 남긴다",
       o["K"] == [True, True, True, False, False], str(o["K"]))
    ok("JS-12b 서버 끊김 폴백 회랑 합계에 PC-TEST 옛 정원 30 이 안 들어간다(2)", o["K2"] == "2", o["K2"])
    ok("JS-12c 평균 효율에 PC-DEMO 90 이 안 섞인다(10.0)", o["K3"].startswith("10.0"), o["K3"])
    ok("JS-4b 「대기 중」 이어도 캐릭 표 나이를 말한다", "캐릭 표 10분째" in o["D2_note"], o["D2_note"])
    ok("JS-9 효율 가진 카드 0장이면 평균 효율 「–」", o["H"] == "–", o["H"])
    py = [main._is_fake_pc(x) for x in FAKE_IDS]
    ok("JS-10 가짜 PC 판정이 서버 _is_fake_pc 와 글자 하나까지 같다", o["I"] == py,
       str([(x, a, b) for x, a, b in zip(FAKE_IDS, o["I"], py) if a != b]))
    ok("JS-10b PC-TESTb·pc-testb·PC-DEMOc 도 가짜", all(o["I"][FAKE_IDS.index(x)] for x in ("PC-TESTb", "pc-testb", "PC-DEMOc")))
    ok("JS-10c 전광판 모집단(summaryPcs)에 가짜 PC 없음", o["I2"] == ["PC-01", "PC-02b"], str(o["I2"]))
    j = o["J"]
    ok("JS-11 악몽 상한 14(주인님 #82) — 「5/14」·「0/14」·빈 값 「–」", j["max"] == 14 and j["five"] == "5/14" and j["zero"] == "0/14"
       and j["nul"] == "–" and j["empty"] == "–", str(j))
    ok("JS-11b 14 이상 빨강(문자 '14' 도) · 13 이하·빈 값·«?» 는 아님",
       j["full"] == [False, False, False, True, True, True, False, False, False], str(j["full"]))
    ok("JS-11d 12·13 경고색 · 11 은 아님 · 14 이상은 빨강이 이겨 경고 아님 · 빈 값·«?» 색 없음",
       j["warnAt"] == 12 and j["warn"] == [False, True, True, False, False, True, False, False, False], str(j["warn"]))
    ok("JS-11e 상한 넘는 실측 16 은 버리지 않고 「16/14」", j["over"] == "16/14", str(j["over"]))
    # 정적 — 악몽 「/14」·「>=14」 하드코딩이 다시 생기지 않게
    # 두 방향 다 — `x >= 14` 도 `14 <= x` 도 (반증 2바퀴 M31: 뒤집은 비교가 옛 정규식을 빠져나갔다)
    nm_hard = (re.findall(r"nightmare_ticket[^\n]{0,60}?(?:/ ?14|>= ?14|> ?13)", src)
               + re.findall(r"(?:14|13) ?(?:<=|<|>=|>|===|==) ?[\w.\[\]'\"()]{0,30}nightmare_ticket", src))
    ok("JS-11c 악몽 티켓 14 하드코딩 0곳", not nm_hard, str(nm_hard[:3]))
    # 정적 — SERVER_SUMMARY 를 serverSum() 밖에서 직접 읽는 곳이 없어야 한다(낡음 우회 금지)
    allowed = ("let SERVER_SUMMARY", "if(!SERVER_SUMMARY)", "? SERVER_SUMMARY : null", "return SERVER_SUMMARY",
               "SERVER_SUMMARY = await r.json()")
    word = re.compile("\\" + "bSERVER_SUMMARY" + "\\" + "b")      # \b 를 문자열 조합으로 — 파일 도구가 먹지 않게
    stray, seen_any = [], 0
    for ln in src.splitlines():
        s_ = ln.strip()
        if s_.startswith("//"):
            continue
        if word.search(s_):
            seen_any += 1
            if not any(x in s_ for x in allowed):
                stray.append(s_)
    ok("JS-7 SERVER_SUMMARY 를 serverSum() 밖에서 읽는 곳 0 (그리고 검사가 실제로 줄을 봤다)",
       not stray and seen_any >= 4, "본 줄 %d · 밖 %s" % (seen_any, stray[:3]))


async def t_check_and_characters():
    # #5 배포 확인용 /check(pc_id=PC-TEST) 는 카드·상태를 하나도 안 만든다(응답만)
    _o = main._load_version_json_async

    async def _ver():
        return {"exe": {"version": "9.9.9", "sha256": "x"}, "images": {}, "updater": {"version": "0.0.1"}}
    main._load_version_json_async = _ver

    class _CheckReq(Req):
        base_url = "http://test/"
    try:
        before = await db.get_pc_dump("PC-TEST")
        resp = await main.updater_check(_CheckReq(body={"pc_id": "PC-TEST", "macro_version": "0.0.0",
                                                         "updater_version": "3.1.13", "exe_version": "0.0.0"}))
        after = await db.get_pc_dump("PC-TEST")
        rows = await main._build_full_state("main")
    finally:
        main._load_version_json_async = _o
    body = json.loads(resp.body)
    ok("#5 /check(PC-TEST) 응답은 그대로(exe_update 9.9.9)", (body.get("exe_update") or {}).get("version") == "9.9.9",
       str(body)[:120])
    cnt = lambda d: {k: len(v) for k, v in d.items() if isinstance(v, list)}   # noqa: E731
    ok("#5-b /check(PC-TEST) 가 DB 어느 표에도 행을 안 만든다", cnt(before) == cnt(after) and not any(cnt(after).values()),
       str(cnt(after)))
    ok("#5-c /check(PC-TEST) 뒤 카드 목록에 PC-TEST 없음", not any(main._is_fake_pc(r.get("pc_id")) for r in rows))

    # /characters — 악몽 티켓을 못 읽었으면 null(0 이면 「다 썼다」 로 읽힌다)
    with _Patch([], [_info("PC-01", [{"slot": 1, "name": "가"}, {"slot": 2, "name": "나", "nightmare_ticket": 0}])]):
        _s = main._require_session
        main._require_session = lambda r: "main"
        try:
            r = await main.get_all_characters(Req())
        finally:
            main._require_session = _s
    chars = json.loads(r.body).get("characters") or []
    nm = {c.get("slot"): c.get("nightmare_ticket") for c in chars}
    ok("#6 /characters 악몽 티켓 못 읽음 → null, 읽은 0 → 0", nm.get(1) is None and nm.get(2) == 0, str(nm))


async def t_guards():
    # 반증 2바퀴 M18 — 가짜 PC 는 순환 무장을 거절한다(「허용 목록 비어 있음」 같은 딴 이유가 아니라 가짜라서)
    _a = main._rot_allow

    async def _all(tenant):
        return ["*"]
    main._rot_allow = _all
    try:
        res = [await main._rot_arm("main", x) for x in ("PC-TEST", "PC-TESTb", "PC-DEMOc")]
    finally:
        main._rot_allow = _a
    ok("G-1 _rot_arm 가짜 PC(PC-TEST·PC-TESTb·PC-DEMOc)는 「가짜」 라서 거절",
       all(r[0] is False and "가짜" in r[1] for r in res), str(res))
    # 반증 2바퀴 M20 — 다른 테넌트의 저장 키(접두사 붙음)로 와도 은퇴 판정이 맞는다
    t_key = main.ns("t1", "PC-05")
    with _Patch([], [], retired=[t_key]):
        a = main._fv_pc_excluded("t1", t_key)
        b = main._fv_pc_excluded("t1", "PC-05")
        c = main._fv_pc_excluded("main", "PC-05")
        d = main._fv_pc_excluded("t1", main.ns("t1", "PC-TESTb"))
    ok("G-2 _fv_pc_excluded — 테넌트 접두사 키·맨 id 둘 다 은퇴로, main 은 아님, 접두사 가짜도 제외",
       (a, b, c, d) == (True, True, False, True), str((a, b, c, d)))


_FAKE_POP_FUNCS = ("renderCards", "dkSubCount", "autoIdleTargets", "switchAllToFirst", "aiBuildHunt",
                   "summaryPcs", "isExcludedPc", "dkHero")


def t_js_population():
    """가짜 PC 거르기가 함수마다 따로 있다 — 하나라도 빠지면 빨간불(반증 2바퀴 M01·M14~M17 이 살아남았다)."""
    src = main.HTML_DASHBOARD
    miss = []
    for n in _FAKE_POP_FUNCS:
        body = _js_func(src, n)
        if "isFakePc(" not in body and "isExcludedPc(" not in body:
            miss.append(n)
    ok("P-1 모집단 함수 %d개 전부 가짜 PC 를 거른다(isFakePc/isExcludedPc)" % len(_FAKE_POP_FUNCS), not miss, str(miss))
    squash = lambda b: re.sub(r"\s+", "", b)   # noqa: E731
    ok("P-2 loadCharTable 이 받은 시각을 찍는다(CHAR_TABLE_AT=sumClock())",
       "CHAR_TABLE_AT=sumClock()" in squash(_js_func(src, "loadCharTable")))
    ok("P-3 loadCorridorSummary 가 받은 시각을 찍는다(CORRIDOR_AT=sumClock())",
       "CORRIDOR_AT=sumClock()" in squash(_js_func(src, "loadCorridorSummary")))
    # dkSubCount 실제 실행 — 가짜·은퇴·계정없음 빼고 센다
    js = ("const ACCT_SUFFIX='bcdefghi'; const RETIRED=new Set(['PC-09']);\n"
          "let state={'PC-01':{},'PC-02b':{},'PC-DEMO':{},'PC-TESTb':{},'PC-09':{},'PC-06':{status:'no_account'}};\n"
          "function subState(id){ return id==='PC-02b' ? 'off' : 'on'; }\n"
          + "\n".join(_js_func(src, n) for n in ("isAcctSuf", "baseId", "isFakePc", "isExcludedPc", "dkSubCount"))
          + "\n" + [ln for ln in src.splitlines() if ln.strip().startswith("const FAKE_PC_BASES")][0]
          + "\nconsole.log(JSON.stringify(dkSubCount()));")
    try:
        r = _run_node(js)
    except Exception as e:
        r = {"err": str(e)[:300]}
    ok("P-4 dkSubCount 실행 — PC-01 on · PC-02b off 만(가짜·은퇴·계정없음 제외)",
       r == {"sub": 1, "nosub": 1, "unknown": 0}, str(r))


def test_all():
    run_all([t_exclusion_and_null, t_corridor_detail_and_summary, t_js, t_check_and_characters,
             t_guards, t_js_population])
    finish("test_summary", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
