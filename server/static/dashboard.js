// ★★계정 개수 — 여기 하나만 고치면 접미사·칩·메뉴가 전부 따라간다 (2026-08-24 사고 193)★★
//   ★서버 파이썬 쪽 MAX_ACCT / 매크로 lc/config.py 의 MAX_ACCT 와 같은 값이어야 한다★
//   `const` 는 호이스팅돼도 초기화 전엔 못 쓴다(TDZ) → ★스크립트 맨 앞★ 에 둔다.
//   ★MAX_ACCT 는 9 이하★ — 라벨 풀이 9글자다. 10 이면 ACCT_LABELS[9] 가 undefined 라
//   조용히 빈 chrome_label 이 나간다(파이썬은 IndexError 로 터지지만 JS 는 안 터진다).
const MAX_ACCT    = 5;
const ACCT_LABELS = 'abcdefghi'.slice(0, MAX_ACCT);   // 계정번호 n ↔ ACCT_LABELS[n-1]
const ACCT_SUFFIX = ACCT_LABELS.slice(1);             // 카드 pc_id 접미사(계정1 은 없음)
// ★정규식은 리터럴 대신 new RegExp★ — 리터럴 안에 줄바꿈이 섞여 <script> 가 통째로
//   죽은 적이 있다(2026-07-27 대시보드 백지). 문자열 조립이면 그 사고가 안 난다.
const ACCT_SUF_RE  = new RegExp('([' + ACCT_SUFFIX + '])$');       // 'PC-20c' → 'c'
const ACCT_SUF_RE2 = new RegExp('[0-9][' + ACCT_SUFFIX + ']$');    // 숫자 뒤 접미사만
// ★''.includes('') 가 true 라 빈 문자열을 따로 막는다★ — 옛 'bcd'.includes(c) 의 잠복 버그
function isAcctSuf(c){ return !!c && c.length === 1 && ACCT_SUFFIX.includes(c); }
// ★검증용 가짜 PC 판정 한 곳(2026-09-23)★ — 서버 _is_fake_pc 와 같은 규칙. 예전엔 renderCards 는
//   PC-TEST 만, dkSubCount 는 PC-TEST·PC-DEMO 를 따로 빼서 전광판 칸마다 모집단이 달랐다.
const FAKE_PC_BASES = ['PC-TEST', 'PC-DEMO'];
function isFakePc(id){ const s = String(id || '').trim(); return FAKE_PC_BASES.includes(baseId(s).toUpperCase()); }
// 접미사 → 계정번호(2~). 접미사가 아니면 0 → 호출부의 `|| 1` 폴백이 살아난다.
function acctNoOfSuf(c){ return isAcctSuf(c) ? ACCT_SUFFIX.indexOf(c) + 2 : 0; }

// ─── 상태 ────────────────────────────────────────────────────────────────────
let state = {};
let latestVersions = {macro:'', updater:''};
let selectedPcs = new Set();
// ★은퇴 목록(2026-09-22)★ — 서버 RETIRED_PCS 를 그대로 받는다. pc_status 행은 지워도
//   형제 카드(계정1)가 여전히 acct_ids 카탈로그를 보고해서, 이게 없으면 buildStack 이
//   "접속한 적 없는 계정" 자리에 회색 탭을 계속 그린다 — 그 탭을 여기서 거른다.
let RETIRED = new Set();
let logModalPc = null;
let logModalSrc = 'both';   // 'both' | 'macro' | 'upd' — 로그 모달이 지금 보고 있는 출처
let menuPcId = null;

// ★XSS 방어(2026-07-27 보안감사): 매크로가 보낸 값(PC이름/캐릭명/에러/맵/파일명)이
// innerHTML로 그대로 들어가고 있었다. API키를 가진 자(유출키·렌탈 고객)가 악성 문자열을
// 보고에 실어 보내면 대시보드를 여는 순간 실행되어 세션이 탈취된다(무클릭 저장형 XSS).
function esc(v){ return String(v==null?'':v).replace(/[&<>"'`=\/]/g, c => ({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','`':'&#96;','=':'&#61;','/':'&#47;'}[c])); }
function escAttr(v){ return esc(v); }
// ═══════════════════════════════════════════════════════════════════════════
// ★★사고 308-b (2026-08-28 주인님) — 「내가 눌렀는지」가 카드에 안 보인다★★
//   주인님 원문: "내가 만약에 시작을 눌럿는데 대시보드 카드는 이제 막 준비하고 시작은
//                하는데 카드를 보면 ★대기★ 라고 떠잇거든. 그래서 내가 실수로
//                '어? 내가 안눌럿나?' 이러면서 ★또 누르는 경우★ 가 있었거든.
//                그래서 뭐가 진행이 되면 ★어떤 상태인지 표기★ 되면 좋을거같은데"
//
// ★왜 안 보였나 (2026-08-28 실측)★
//   ① 카드의 상태 글자는 100% 매크로가 보고한 status 파생이다(STATUS_CFG → buildCard).
//      ★명령 큐를 카드 렌더가 한 번도 안 본다.★
//   ② 같은 날 낮에 넣은 낙관적 표시는 setCardOnly 안의 showToast 한 줄뿐이었고,
//      그건 카드가 아니라 ★화면 하단 토스트★ 다 — 다음 토스트가 0.3초 만에 덮고
//      2.5초면 사라지며, 여러 대에 보내면 마지막 하나만 남는다.
//   ③ status 가 실제로 바뀌기까지 수십 초~3분 걸린다. start 는 매크로가
//      _ensure_char_select_screen(timeout 60 · hard_cap 90) + 슬롯 진입을 다 지나야
//      report_status("hunting") 을 부르고, 보고 하트비트가 30초, 렌더 디바운스가 700ms 다.
//      ★그 구간 내내 카드는 정직하게 「대기」다.★ 그게 주인님이 또 누르시는 창이다.
//
// ★어디에 두나 — ★state 바깥★★
//   WS 가 msg.type==='state' 마다 `state={}` 로 통째로 갈아엎는다(함대 24대 × 30초
//   하트비트 = 1~2초에 한 번). state 에 얹은 표식은 그 즉시 소멸한다.
//   키는 ★물리 PC(base)★ 다 — 매크로는 물리 PC 당 한 대뿐이고, set_account 는
//   카드 pc_id 자체를 바꾸므로(PC-20b→PC-20c) pc_id 키는 미아가 된다.
//
// ★★해제 조건 — 유령 표시를 막는 5겹. 이 표가 곧 명세다★★
//   ┌─ ①효과 관측 : 누를 때 찍어둔 st0/acct0 와 ★달라졌고★ 기대값에 들어오면 즉시 제거
//   │                (CMD_TRACK.exp = 기대 status · CMD_TRACK.acct = 계정번호 변화)
//   │                → pendStep() 앞부분. WS state 가 올 때마다 pendSweep 이 돌린다.
//   ├─ ②ack       : 서버 cmd_history 의 acked → ⏳ 를 📨 로 ★올린다.★
//   │                ★ack 은 '받았다' 이지 '끝났다' 가 아니다★ 이므로 기대값이 있는
//   │                명령은 여기서 안 지운다. 기대값이 없는 명령만 4초 뒤 제거.
//   │                → pendFromHistory() (WS cmd_history / loadCmdHistory 공통 싱크)
//   ├─ ③서버 종결 : cmd_history 의 expired/cancelled → ⛔ 로 바꿔 ★남긴다★
//   │                (조용히 지우면 "됐나?" 가 다시 시작된다)
//   ├─ ④시간 초과 : 명령별 ttl(기본 90초 · start 3분 · 계정전환 7분) 넘으면 ⚠ 로 바꾸고
//   │                5분 뒤 자동 제거. 절대 상한은 서버 큐 만료 900초(COMMAND_MAX_AGE_SEC).
//   └─ ⑤사람      : ⚠/⛔ 막대를 클릭하면 지운다(pendDismiss). 진행 중인 것은 안 지운다.
//   그리고 새로고침·서버 재시작(checkServerBoot → location.reload)이면 이 맵은
//   메모리라 통째로 사라진다. ★사라져도 카드는 그대로 그려진다★ —
//   pendBar/pendChip 이 빈 문자열을 돌려줄 뿐이다(카드 구조를 안 건드린다).
// ═══════════════════════════════════════════════════════════════════════════
const PEND_TTL_DEFAULT = 90000;    // 기대 신호가 없는 명령의 기본 상한
const PEND_WARN_KEEP   = 300000;   // ⚠/⛔ 를 화면에 남겨두는 시간(그 뒤 자동 제거)
const PEND_HARD_MAX    = 900000;   // 서버 큐 만료(COMMAND_MAX_AGE_SEC=900)를 넘겨 우기지 않는다
// exp  = 이 status 가 되면 '진짜 시작됐다' (매크로 report_status 실측 기준)
// acct = status 가 아니라 ★계정번호 변화★ 로만 확인되는 명령(set_account 는 status 를 안 바꾼다)
// ttl  = 이 시간을 넘기면 ⚠. start 3분은 _ensure_char_select_screen(60+90초) 실측에서 나왔다.
const CMD_TRACK = {
  start:           {t:'▶ 사냥 시작',      ttl:180000, exp:['hunting','moving','selling','subquest','dungeon','nightmare','awakening','corridor','surface','abyss']},
  stop:            {t:'■ 정지',           ttl: 90000, exp:['idle','paused']},
  exit:            {t:'✕ 매크로 종료',    ttl:120000, exp:['offline']},
  restart:         {t:'♻ 매크로 재시작',  ttl:180000, exp:['offline']},
  go_home:         {t:'⌂ 귀환',           ttl:120000, exp:['moving','idle']},
  sell:            {t:'💰 판매',          ttl:180000, exp:['selling']},
  sell_all:        {t:'💰 전체 판매',     ttl:180000, exp:['selling']},
  prepare:         {t:'🧰 준비',          ttl:180000, exp:['selling','moving']},
  settle:          {t:'🧾 정산',          ttl:180000, exp:['selling','moving']},
  collect_info:    {t:'📋 정보수집',      ttl:180000, exp:['collecting']},
  switch_char:     {t:'🔀 캐릭 전환',     ttl:180000, exp:['switching']},
  daily_dungeon:   {t:'🏰 일일던전',      ttl:180000, exp:['dungeon']},
  kibel:           {t:'🏰 키벨',          ttl:180000, exp:['dungeon']},
  nightmare:       {t:'😈 악몽',          ttl:180000, exp:['nightmare','nightmare_wait']},
  awakening:       {t:'⚔ 각성전',         ttl:180000, exp:['awakening','awakening_wait']},
  corridor:        {t:'🌀 회랑',          ttl:180000, exp:['corridor']},
  surface:         {t:'🏔 표층',          ttl:600000, exp:['surface','abyss','idle','hunting','corridor','paused']},   // 사고 574 · #208 새 매크로는 `surface` 를 보낸다 — 옛 판 어휘(abyss…)도 그대로 받는다
  abyss:           {t:'🌌 어비스',        ttl:180000, exp:['abyss']},
  // ★사고 392★ 순회의 전환 구간은 이제 acct_switching 을 보고한다 — 안 넣으면
  //   전환 중인데 「기대 상태 미도달」로 보인다(캐릭 전환과 라벨을 갈랐기 때문).
  acct_tour:       {t:'🔁 전 계정 순회',  ttl:300000, exp:['collecting','switching','acct_switching']},
  set_account:     {t:'🪪 카드 계정 변경', ttl:300000, acct:true},
  switch_launcher: {t:'🔄 계정 전환(본컴+원격컴)', ttl:420000, acct:true},
  switch_account:  {t:'🔄 계정 전환(원격컴)',      ttl:300000, acct:true},
  find_host:       {t:'🔎 본컴 찾기',     ttl:180000},
  chrome_cdp:      {t:'🌐 크롬 재발사',   ttl:180000},
  chrome_view:     {t:'🖥 화면 보기',     ttl: 60000},
  kill_game:       {t:'⛔ 게임 강제종료', ttl:180000},
  plrow_shot:      {t:'📸 계정줄 수집',   ttl:120000},
  reset_progress:  {t:'↺ 진행도 초기화',  ttl: 60000},
};
// ★칩을 안 띄우는 명령★ — 라이브 화면·로그 요청처럼 사람이 결과를 즉시 눈으로 보는 것들.
//   여기에까지 칩을 띄우면 라이브를 켤 때마다 카드가 깜빡여 ★진짜 신호를 가린다.★
const CMD_SILENT = ['live_on','live_off','get_logs','request_logs','set_slot_filter',
                    'captcha_code','set_info','stop_tour','stop_nightmare','stop_corridor','stop_surface','netprobe'];

let pendingCmds = {};   // base(물리 PC) → 진행 표시 1건. 같은 PC 에 새 명령이 오면 ★덮어쓴다★
                        //   (누적하면 영영 안 지워진다 — 그 PC 의 매크로는 어차피 한 대뿐이다)
let _pendDupMemo = {cmd:'', at:0, yes:false};   // 일괄 전송(Promise.all) 때 confirm 을 1회로

// ★업데이트/재시작 결과만(2026-09-22, 주인님 지시)★ — pendingCmds 는 건드리지 않는다
//   (범위가 넓고 손대면 회귀 위험). ★기준은 버전★ — updater.version 이 바뀌면 ✓,
//   3분 안 안 바뀌면 ✗. 설명 문구는 카드에 안 띄운다(hover title 만).
// ★★B-CQ8 (2026-09-23) 증거가 없으면 ✗ 가 아니라 ?★★ — 예전엔 _updater_version 하나로만 봤다.
//   그런데 restart 와 「매크로만 바뀐 update」 는 업데이터 버전을 ★안 바꾼다★ → 멀쩡히 된 것이
//   3분 뒤 전부 ✗ 였다. 이제 증거 두 가지:
//     ① 버전 — macro_version 또는 _updater_version 이 바뀜(update 의 ✓)
//     ② 재기동 — uptime_hours(매크로 프로세스 시작부터, config._report_start_time)가 ★줄어듦★
//        (restart 의 ✓ · 이미 최신이던 update 의 ✓). 0.01h 반올림이라 0.02h 넘게 줄어야 인정.
//   ✗ 는 ★반대 증거★ 가 있을 때만: 3분 동안 uptime 이 끊김 없이 늘었다(=안 껐다) 또는
//   재기동은 됐는데 최신이 아닌 버전 그대로(update). 둘 다 없으면 '?'(판정 불가).
let UPD_RESULT = {};   // base → {cmd, before, beforeMacro, beforeUp, deadline, result: null|'ok'|'fail'|'unknown'}
function updResultStart(base, beforeVer, command){
  const p = state[base]||{};
  const up = (typeof p.uptime_hours === 'number') ? p.uptime_hours : null;
  UPD_RESULT[base] = {cmd: command||'update', before: beforeVer||'', beforeMacro: p.macro_version||'',
                      beforeUp: up, deadline: Date.now()+180000, result: null};
}
function updResultSweep(){
  let changed = false;
  Object.keys(UPD_RESULT).forEach(b=>{
    const e = UPD_RESULT[b];
    if (e.result) return;   // 이미 확정된 건 재판정 안 한다(다음 시도가 덮어씀)
    const p = state[b]||{};
    const curU = p._updater_version || '';
    const curM = p.macro_version || '';
    const verChanged = (curU && curU !== e.before) || (curM && e.beforeMacro && curM !== e.beforeMacro);
    const up = (typeof p.uptime_hours === 'number') ? p.uptime_hours : null;
    const hasUp = (up !== null && e.beforeUp !== null);
    const rebooted = hasUp && up < e.beforeUp - 0.02;
    const kept = hasUp && up > e.beforeUp + 0.02;          // 안 끊기고 계속 늘었다 = 재기동 없음
    const lm = (typeof latestVersions === 'object' && latestVersions) ? (latestVersions.macro || '') : '';
    const atLatest = !!(lm && curM && curM === lm);
    const done = Date.now() >= e.deadline;
    let r = null;
    if (e.cmd === 'restart') {
      if (rebooted) r = 'ok';
      else if (done) r = kept ? 'fail' : 'unknown';
    } else {
      if (verChanged || (rebooted && atLatest)) r = 'ok';
      else if (done) r = (kept || rebooted) ? 'fail' : 'unknown';
    }
    if (r) { e.result = r; changed = true; }
  });
  return changed;
}

// ★빠른 재렌더★ — 기존 scheduleRender 는 700ms 디바운스(WS 폭주용)라 버튼을 누른
//   사람에게는 늦다. 사람 클릭 경로만 80ms 로 따로 판다(둘이 겹쳐도 렌더가 두 번일 뿐 무해).
let _renderTimerFast=null;
function scheduleRenderNow(){ if(_renderTimerFast) return;
  _renderTimerFast=setTimeout(()=>{_renderTimerFast=null; try{renderCards();}catch(e){}},80); }

function pendSecs(e){ return Math.max(0, Math.round((Date.now()-e.at)/1000)); }
// 그 명령을 실제로 받은 ★카드★ 의 status. liveCardOf 는 온라인 카드만 돌려주므로
// exit/restart 처럼 offline 을 기대하는 명령에는 못 쓴다.
function pendStatusOf(e){ const p = state[e.pcId]; return p ? (p.status||'offline') : 'offline'; }

// ★★적대검증 치명2 — 일괄 전송 중에는 「또 누르기」를 묻지 않는다★★
//   키가 ★물리 PC(base)★ 인데 bulkCmd/selCmd/rotCmd 는 ★카드★ 단위로 돈다.
//   다계정 PC 는 카드가 2~5장이고, 뒷카드(other_account)는 사고 307 리다이렉트로
//   전부 같은 live 카드로 접힌다 → 두 번째 카드부터 pendingCmds[base] 가 이미 있어
//   confirm 이 뜬다. ★주인님은 한 번 눌렀는데 「0초 전에 이미 보냈습니다」★
//   그리고 「아니오」면 나머지가 조용히 드롭되는데 토스트는 「전체 N대」라고 말한다(§A2).
//   → 사람이 일괄을 ★의도적으로★ 누른 것이므로 그 안에서는 되묻지 않는다.
//     (일괄 자체의 확인창은 selCmd/rotCmd 가 이미 따로 갖고 있다)
let _bulkDepth = 0;
async function withBulk(fn){
  _bulkDepth++;
  try { return await fn(); } finally { _bulkDepth--; }
}

function pendRegister(pcId, command, args){
  if (CMD_SILENT.indexOf(command) >= 0) return null;
  const base = baseId(pcId);
  const spec = CMD_TRACK[command] || {};
  const st0  = ((state[pcId]||{}).status) || '';
  let exp = spec.exp || null;
  // ★이미 그 상태면 status 로는 못 가른다★ — '바뀌었을 때만' 인정해야 하는데
  //   처음부터 기대값이면 조건이 항상 참이라 ★누르자마자 거짓 성공★ 이 된다.
  //   → 기대값을 버리고 ack 해제로 정직하게 강등한다(그러면 ②·④ 가 받는다).
  if (exp && exp.indexOf(st0) >= 0) exp = null;
  // ★★적대검증 높음3 — 계정번호 변화로 못 재는 판이 둘 있다★★
  //   (a) ★같은 계정으로 강제 재정렬★ — 주인님이 2026-08-23 에 지시하신 기능이다
  //       ("현재 계정 버튼도 누를 수 있게 둔다 … 어긋난 짝을 맞춥니다").
  //       계정1 카드에서 [계정 1] 을 누르면 전환이 성공해도 번호는 1 그대로라
  //       `cur !== acct0` 가 영영 거짓 → ttl 420초 뒤 ★성공한 전환에 ⚠ 「응답 없음」★.
  //       그러면 주인님이 또 누르신다 = 308-b 가 막으려던 행동을 새로 만든다.
  //   (b) 온라인 카드가 없으면 currentAcctNum 이 0 을 준다 → 조건이 영영 거짓.
  //   → 둘 다 ★계정 기대값을 버리고★ ack+ttl 로 정직하게 강등한다.
  let _wantAcct = !!spec.acct;
  let _a0 = _wantAcct ? currentAcctNum(base) : 0;
  if (_wantAcct) {
    let _tgt = 0;
    try {
      const _v = args && (args.acct || args.account || args.acct_no || args.n);
      _tgt = parseInt(_v, 10) || 0;
    } catch (err) { _tgt = 0; }
    if (!_a0 || (_tgt && _tgt === _a0)) { _wantAcct = false; _a0 = 0; }
  }
  const e = {base:base, pcId:pcId, cmd:command, label:(spec.t||command),
             at:Date.now(), ttl:(spec.ttl||PEND_TTL_DEFAULT),
             exp:exp, acct:_wantAcct, acct0:_a0,
             st0:st0, id:null, idResolved:false, phase:'sent', icon:'⏳', note:'', clearAt:0};
  pendingCmds[base] = e;
  return e;
}

// 한 건을 한 칸 전진시킨다. ★화면에 보이는 변화가 있으면 true★ (호출부가 재렌더한다)
function pendStep(e){
  if (!e) return false;
  const age = Date.now() - e.at;
  // ══════════════════════════════════════════════════════════════════
  // ★★2026-09-08 주인님 — 「사냥시작 4분 52초째 응답없음, 이미 사냥은 하고있어」★★
  //   ★효과 관측(①)을 warn 이어도 계속 본다.★
  //   옛 코드는 warn 분기가 ★맨 앞에서 return★ 이라, 한 번 ⚠ 가 되면 그 뒤에
  //   실제로 사냥이 시작돼도 ★다시 안 봤다.★ 그런데 start 의 ttl 이 ★정확히 180초★
  //   인데 확인창 문구조차 「사냥 중으로 바뀌는 데 ★최대 3분★ 걸립니다」 라고 적어놨다 —
  //   ★3분을 넘기는 판은 정상인데도 반드시 ⚠ 로 남는다.★ 그리고 warn 은
  //   PEND_WARN_KEEP(5분) 을 더 버틴다 = ★도는 PC 에 「응답없음」이 최대 8분.★
  //   사람이 새로고침으로 지우는 수밖에 없었다(pendingCmds 는 브라우저 안에만 있다).
  //   ★효과가 왔으면 warn 이든 아니든 지운다★ — 그게 이 표시의 원래 뜻이다.
  // ══════════════════════════════════════════════════════════════════
  if (e.acct) {                                                // ①-a 계정번호가 실제로 바뀜
    const cur = currentAcctNum(e.base);
    if (cur && e.acct0 && cur !== e.acct0) { delete pendingCmds[e.base]; return true; }
  }
  if (e.phase === 'warn') {                                    // ⑤ ⚠/⛔ 는 잠깐 남기고 치운다
    if (e.exp) {                                               // ★warn 이어도 ①-b 를 본다★
      const stW = pendStatusOf(e);
      if (stW !== e.st0 && e.exp.indexOf(stW) >= 0) { delete pendingCmds[e.base]; return true; }
    }
    if (age > e.ttl + PEND_WARN_KEEP || age > PEND_HARD_MAX) { delete pendingCmds[e.base]; return true; }
    return false;
  }
  if (e.exp) {                                                 // ①-b status 가 실제로 바뀜
    // ★★적대검증 높음4 — 명령과 ★무관한★ 상태 전이가 표시를 지웠다★★
    //   start 의 기대값이 온라인 상태 9종이라, 매크로가 자기 사정으로 selling 이 되면
    //   (사고 234 자동재개 · 판매 루틴 · 순환) 우리 명령을 안 받았는데도 즉시 지워졌다.
    //   = 「눌렀나?」 창이 그대로 복구된다.
    //   ★ack 을 받았으면 그건 우리 명령이 확실하다★ → 바로 인정.
    //   ★ack 을 못 받았어도 5초는 기다린다★ — 누르자마자 뜬 낡은 상태로 지우지 않게.
    //   ※정직하게: ack 이 안 오는 판에서는 여전히 무관한 전이로 지워질 수 있다.
    //     그건 ★예전 상태(표시 없음)로 돌아가는 것★ 이라 새 소음은 아니다.
    //     반대로 ack 을 강제하면 ack 을 놓칠 때마다 ⚠ 가 떠서 더 나쁘다 — 그래서 안 했다.
    const st = pendStatusOf(e);
    if (st !== e.st0 && e.exp.indexOf(st) >= 0
        && (e.phase === 'ack' || age >= 5000)) {
      delete pendingCmds[e.base]; return true;
    }
  }
  if (e.phase === 'ack' && !e.exp && !e.acct) {                 // ② 기대 신호가 없는 명령만 ack 로 종료
    if (!e.clearAt) e.clearAt = Date.now() + 4000;              //    사람이 읽을 4초는 남긴다
    if (Date.now() >= e.clearAt) { delete pendingCmds[e.base]; return true; }
    return false;
  }
  if (age > e.ttl) {                                           // ④ 시간 초과 — 조용히 안 지운다
    e.phase = 'warn'; e.icon = '⚠';
    e.note = '응답 없음 — 맨 아래 「최근 명령 내역」 확인';
    return true;
  }
  return false;
}
function pendSweep(){
  let changed = false;
  Object.keys(pendingCmds).forEach(k => { if (pendStep(pendingCmds[k])) changed = true; });
  return changed;
}
// ②③ 서버 명령 이력에서 ack/만료/취소를 읽어 반영한다. cmd_history 는 WS 로 실시간으로 온다.
function pendFromHistory(cmds){
  let changed = false;
  (cmds||[]).forEach(c => {
    const b = baseId(String(c.pc_id||''));
    const e = pendingCmds[b];
    // ★warn 이어도 계속 본다 (2026-09-11)★ — 한 번 ⚠ 가 되면 그 뒤 서버가 보내는
    //   acked/expired/cancelled 를 영영 안 보던 자리다. pendStep 은 2026-09-08 에
    //   같은 이유로 고쳤는데 ★이 함수는 안 고쳤다★(같은 부류를 옆에 남겨둠).
    if (!e) return;
    // id 로 맞춘다. 서버 응답 json 을 못 읽었을 때만 (pc_id + command) 로 폴백한다.
    // ★idResolved 를 기다린다★ — 서버는 명령을 넣자마자 cmd_history 를 브로드캐스트하는데
    //   (main.py /command 의 _push_cmd_history), 그때 우리는 아직 fetch 응답을 못 읽어
    //   id 가 없다. 그 틈에 폴백을 쓰면 ★같은 PC·같은 명령의 옛 acked 줄★ 을 지금 것으로
    //   오인해 표시가 먼저 지워진다. 폴백은 'id 를 끝내 못 받았다' 가 확정된 뒤에만 쓴다.
    const same = (e.id != null && c.id === e.id) ||
                 (e.idResolved && e.id == null && String(c.command) === e.cmd && String(c.pc_id) === e.pcId);
    if (!same) return;
    if (c.status === 'acked') {
      if (e.phase !== 'ack') { e.phase = 'ack'; e.icon = '📨'; changed = true; }
    } else if (c.status === 'expired' || c.status === 'cancelled') {
      e.phase = 'warn'; e.icon = '⛔';
      e.note = (c.status === 'cancelled') ? '명령이 취소됐습니다'
                                          : '명령이 만료됐습니다 — 매크로가 안 가져갔습니다';
      changed = true;
    }
  });
  return changed;
}
function pendBarText(e){
  const s = pendSecs(e);
  const t = (s < 60) ? (s + '초째') : (Math.floor(s/60) + '분 ' + (s%60) + '초째');
  const tail = e.note ? e.note
             : (e.phase === 'ack' ? '매크로가 받았습니다 — 실행을 기다립니다'
                                  : '매크로 응답을 기다리는 중');
  return e.icon + ' ' + e.label + ' — ' + t + ' · ' + tail;
}
function pendChipText(e){ return e.icon + ' ' + pendSecs(e) + '초'; }
// ★B-CQ9 (2026-09-24 #114)★ 칩·막대 글자에는 ★매초 바뀌는 경과 초★ 가 들어 있어 대기 중인 카드는 렌더마다
//   HTML 이 달라 reconcileGrid 가 통째로 갈아 끼웠다(열린 메뉴·호버·선택이 매번 날아감). 글자는 data-pt 칸으로
//   표시하고 _rkNorm 이 그 글자를 비교에서 뺀다 — 초는 pendTick 이 제자리 textContent 로 갈아 끼운다.
//   초를 뺀 나머지(아이콘·명령·단계·사유)는 이 키로 속성에 남겨 ★바뀌면 여전히 새로 그린다★.
function pendKey(e){ return [e.icon, e.label, e.phase, e.note || ''].join('|'); }
// ★상태 글자 바로 옆★ 칩 (rotChip 과 같은 슬롯) — 「대기」 옆에 붙어 눈이 같이 본다
function pendChip(pc){
  const e = pendingCmds[baseId(pc.pc_id||'')];
  if (!e) return '';
  const c = e.phase === 'warn' ? 'bg-red-800/85 text-red-100 border-red-500'
          : (e.phase === 'ack' ? 'bg-sky-800/85 text-sky-100 border-sky-400 pulse'
                               : 'bg-amber-600/90 text-amber-50 border-amber-300 pulse');
  return `<span id="pcmd-chip-${escAttr(e.base)}" class="ml-1.5 shrink-0 px-1.5 py-0.5 rounded border text-xs font-bold leading-none ${c}"
                title="${escAttr(e.label)}" data-pt="${escAttr(pendKey(e))}">${esc(pendChipText(e))}</span>`;
}
// ★상태 줄 바로 밑 가로 막대★ — 칩은 좁아서 명령 이름이 안 들어간다.
//   주인님 요구("명령 이름 + 보낸 지 몇 초")를 실제로 채우는 건 이쪽이다.
function pendBar(pc){
  const e = pendingCmds[baseId(pc.pc_id||'')];
  if (!e) return '';
  const c = e.phase === 'warn' ? 'bg-red-900/55 border-red-600 text-red-200'
          : (e.phase === 'ack' ? 'bg-sky-900/60 border-sky-500 text-sky-100'
                               : 'bg-amber-900/60 border-amber-500 text-amber-100 pulse');
  const tip = e.phase === 'warn'
    ? '클릭하면 이 표시를 지웁니다'
    : '이 표시는 ①매크로 상태가 실제로 바뀌거나 ②명령이 만료·취소되거나 ③시간이 지나면 자동으로 사라집니다';
  return `<div id="pcmd-bar-${escAttr(e.base)}" data-base="${escAttr(e.base)}"
      onclick="event.stopPropagation();pendDismiss(this.dataset.base)" title="${escAttr(tip)}"
      class="mb-2 px-2 py-1 rounded border text-xs font-bold leading-tight truncate ${c}" data-pt="${escAttr(pendKey(e))}">${esc(pendBarText(e))}</div>`;
}
// ⑤ 사람이 치운다 — ★진행 중인 것은 안 지운다★(그건 ①~④ 가 할 일이다)
function pendDismiss(base){
  const e = pendingCmds[base];
  if (e && e.phase === 'warn') { delete pendingCmds[base]; scheduleRenderNow(); }
}
// 1초마다: 상태를 한 칸 전진시키고, 변화가 없으면 ★경과 초만 제자리에서 갈아 끼운다.★
//   (초당 카드 전체 innerHTML 재구축은 낭비다. textContent 라 XSS 여지도 없다)
function pendTick(){
  const keys = Object.keys(pendingCmds);
  if (!keys.length) return;
  if (pendSweep()) { scheduleRenderNow(); return; }
  keys.forEach(k => {
    const e = pendingCmds[k]; if (!e) return;
    const bar  = document.getElementById('pcmd-bar-'  + e.base);  if (bar)  bar.textContent  = pendBarText(e);
    const chip = document.getElementById('pcmd-chip-' + e.base);  if (chip) chip.textContent = pendChipText(e);
  });
}
setInterval(pendTick, 1000);
   // esc가 따옴표까지 막으므로 속성값에 그대로 안전

const STATUS_CFG = {
  hunting:      {label:'사냥 중', vi:'Đang săn',   bg:'bg-green-500/20',  border:'border-green-700',  badge:'bg-green-500',  text:'text-green-400',  online:true},
  selling:      {label:'판매 중', vi:'Đang bán',   bg:'bg-blue-500/20',   border:'border-blue-700',   badge:'bg-blue-500',   text:'text-blue-400',   online:true},
  abyss:        {label:'어비스', vi:'Abyss',   bg:'bg-fuchsia-500/20', border:'border-fuchsia-700', badge:'bg-fuchsia-500', text:'text-fuchsia-400', online:true},
  moving:       {label:'사냥 중', vi:'Đang săn',   bg:'bg-green-500/20',  border:'border-green-700',  badge:'bg-green-500',  text:'text-green-400',  online:true},
  switching:    {label:'캐릭 전환', vi:'Đổi nhân vật', bg:'bg-purple-500/20', border:'border-purple-700', badge:'bg-purple-400', text:'text-purple-400', online:true},
  // ★★사고 392 — ★계정전환 중인데 카드가 「대기」였다★★ (주인님 2026-09-01)
  //   원문: 「계정전환하고 있는데 그냥 대기라고 뜨니까 내가 뭐하는지 알수없다는거야」
  //   ★위 `switching` 은 캐릭(슬롯) 전환이다★ — 계정전환은 본컴 런처까지 갈아끼우는
  //   3~5분짜리 다른 일이라 라벨을 가른다. 옆에 switchStepChip 이 몇 단계인지 붙는다.
  acct_switching:{label:'계정 전환', vi:'Đổi tài khoản', bg:'bg-purple-500/20', border:'border-purple-700', badge:'bg-purple-400', text:'text-purple-300', online:true},
  connecting:   {label:'접속 중', vi:'Đang kết nối',   bg:'bg-orange-500/20', border:'border-orange-700', badge:'bg-orange-400', text:'text-orange-300', online:true},   // ★사고 489★ 스트림 붙이는 1~2분 — 「가만히 있네」가 아니라 하는 중
  reconnecting: {label:'재연결 중', vi:'Đang kết nối lại', bg:'bg-orange-500/20', border:'border-orange-700', badge:'bg-orange-400', text:'text-orange-400', online:true},
  captcha:      {label:'캡차', vi:'Captcha',      bg:'bg-pink-500/20',   border:'border-pink-700',   badge:'bg-pink-500',   text:'text-pink-400',   online:true},
  dead:         {label:'사망', vi:'Đã chết',      bg:'bg-red-500/20',    border:'border-red-700',    badge:'bg-red-500',    text:'text-red-400',    online:true},
  idle:         {label:'대기', vi:'Chờ',      bg:'bg-gray-700/20',   border:'border-gray-600',   badge:'bg-gray-500',   text:'text-gray-400',   online:true},
  subquest:     {label:'서브퀘', vi:'Nhiệm vụ phụ',   bg:'bg-lime-500/20',   border:'border-lime-700',   badge:'bg-lime-500',   text:'text-lime-400',   online:true},
  dungeon:      {label:'던전', vi:'Hầm ngục',    bg:'bg-purple-500/20', border:'border-purple-700', badge:'bg-purple-500', text:'text-purple-400', online:true},
  nightmare:    {label:'악몽', vi:'Ác mộng',    bg:'bg-pink-500/20',   border:'border-pink-700',   badge:'bg-pink-500',   text:'text-pink-400',   online:true},
  awakening:    {label:'각성전', vi:'Trận thức tỉnh',  bg:'bg-indigo-500/20', border:'border-indigo-700', badge:'bg-indigo-500', text:'text-indigo-400', online:true},
  awakening_wait:{label:'각성전 대기', vi:'Chờ trận thức tỉnh', bg:'bg-red-500/20', border:'border-red-700', badge:'bg-red-500', text:'text-red-400', online:true},
  nightmare_wait:{label:'악몽전 대기', vi:'Chờ trận ác mộng', bg:'bg-red-500/20', border:'border-red-700', badge:'bg-red-500', text:'text-red-400', online:true},
  corridor:     {label:'회랑', vi:'Hành lang',    bg:'bg-blue-500/20',   border:'border-blue-700',   badge:'bg-blue-500',   text:'text-blue-400',   online:true},
  // ★#208 (2026-09-25 주인님)★ 「표층 중인데 카드가 대기」 — 매크로가 표층 세션 중 `surface` 를 보낸다(회랑과 같은 방식).
  //   여기 없으면 STATUS_CFG[st]||offline 로 빨간 오프라인이 된다. 모양은 회랑과 같게(주인님 지시).
  surface:      {label:'표층 진입중', vi:'Vào tầng mặt', bg:'bg-blue-500/20', border:'border-blue-700', badge:'bg-blue-500', text:'text-blue-400', online:true},
  // ★매크로는 이 상태를 보내는데 대시보드가 몰랐다 (2026-08-28 하네스가 잡음)★
  //   lc/sealed_dungeon.py:49 report_status("sealed_dungeon") — 여기 없으면
  //   STATUS_CFG[st]||STATUS_CFG.offline 로 떨어져 ★봉인던전 도는 PC 가 빨간 오프라인★ 으로 보인다.
  sealed_dungeon:{label:'봉인던전', vi:'Hầm ngục phong ấn', bg:'bg-violet-500/20', border:'border-violet-700', badge:'bg-violet-500', text:'text-violet-400', online:true},
  // ★사고 393★ `wardrobe.py:39 report_status("wardrobe")` — sealed_dungeon 과 같은 부류.
  //   여기 없으면 옷장 합성 도는 PC 가 ★빨간 오프라인★ 으로 보인다. 게이트 18 이 잡았다.
  wardrobe:     {label:'옷장 합성', vi:'Ghép tủ đồ', bg:'bg-teal-500/20', border:'border-teal-700', badge:'bg-teal-500', text:'text-teal-400', online:true},
  collecting:   {label:'정보수집', vi:'Thu thập thông tin', bg:'bg-cyan-500/20',   border:'border-cyan-700',   badge:'bg-cyan-500',   text:'text-cyan-400',   online:true},
  paused:       {label:'일시정지', vi:'Tạm dừng', bg:'bg-amber-500/20',  border:'border-amber-700',  badge:'bg-amber-500',  text:'text-amber-400',  online:true},
  error:        {label:'에러', vi:'Lỗi',      bg:'bg-red-500/20',    border:'border-red-700',    badge:'bg-red-500',    text:'text-red-400',    online:true},
  // ★오프라인은 빨갛게 (2026-08-20 사용자 지시)★ — 카드가 자리를 안 옮기게 바꿨으니
  //   (아래로 안 내려간다) 죽었다는 걸 ★색으로★ 확실히 알려야 한다. 회색은 안 보인다.
  offline:      {label:'오프라인', vi:'Ngoại tuyến',  bg:'bg-red-950/40',    border:'border-red-800/70', badge:'bg-red-700',    text:'text-red-400',    online:false},
  other_account:{label:'다른 계정', vi:'Tài khoản khác', bg:'bg-gray-900/40', border:'border-gray-800', badge:'bg-purple-900', text:'text-purple-400/70', online:false},
  // ★계정 없음(2026-09-22, 주인님 지시)★ — PC-24: 「계정 없으니까 컴퓨터는 띄워 놓고
  //   계정은 없다고 표시해놔」. other_account 와 같은 처지(순환 제외·집계 제외)지만
  //   라벨이 달라야 한다("다른 계정" 이 아니라 "계정 없음") — 그래서 항목을 따로 둔다.
  no_account:   {label:'계정 없음', vi:'Không có tài khoản', bg:'bg-gray-900/40', border:'border-gray-800', badge:'bg-gray-700', text:'text-gray-400', online:false},
};
const LOG_COLOR = {error:'text-red-400', warn:'text-yellow-400', info:'text-gray-300', debug:'text-gray-600'};

// ★커맨드 덱(2026-08-16)★ — 카드 윗면에서 상태색이 새어 나온다.
//   ★상태 판정은 STATUS_CFG 를 안 건드리고 여기서만 한다★ — 라벨·뱃지·색은 그대로.
//
//   ★세기를 3등급으로 나눈 이유(2026-08-16 사용자 요청 "사냥중도 초록색 느낌")★
//   함대 20대 중 15대가 사냥중이다. 초록을 개입색과 같은 세기로 주면 화면이 초록 잔치가
//   되고 빨강/앰버가 묻힌다 — 이 디자인의 목적(문제 있는 놈만 눈에 띄기)이 죽는다.
//   그래서 색만 다른 게 아니라 ★알파·높이·모서리선 세기까지 3등급★으로 벌려 놓았다.
//     ok   민트 알파 1e(≈12%) · 높이 40px · 모서리선 .38  ← 잘 돌고 있음(대다수)
//     warn 앰버 알파 38(≈22%) · 높이 86px · 모서리선 .9   ← 손이 곧 필요함
//     act  코랄 알파 44(≈27%) · 높이 86px · 모서리선 1    ← 지금 개입
//   카드 바탕(#0c1120) 위에서 민트는 +(6,24,14), 코랄은 +(65,20,21) 만큼 밀린다.
//   밀림 폭이 3배 차이 나는 데다 높이도 2배라 초록이 15장 깔려도 빨강이 먼저 보인다.
//   세기를 바꾸려면 DK_TIER 한 곳만 고치면 된다.
// ★2026-08-16 사용자 지적: "색깔이 좀 옅어서 구분이 안되고, 그라데이션이 적어도
//   카드에 60~70%는 덮게 해"★ — 높이를 px(86/40)에서 ★카드 높이 비율★로 바꾼다.
//   카드는 계정 수·정보량에 따라 높이가 제각각이라 px 로는 어떤 카드는 1/3, 어떤 카드는
//   2/3 가 덮여 들쭉날쭉했다. %로 하면 카드가 커져도 덮는 비율이 같다.
//   ★등급 구분은 이제 '알파'가 맡는다★ — 높이가 다 비슷해졌으므로 색 진하기로 가른다.
//   (정상 초록이 15장 깔려도 빨강이 먼저 보여야 한다는 원칙은 그대로)
const DK_TIER = {
  act:  {a:'59', h:'72%', o:'1'},    // 개입 — 제일 진하게
  warn: {a:'47', h:'68%', o:'.92'},
  ok:   {a:'33', h:'62%', o:'.5'},   // 정상 — 옅지만 ★색은 알아볼 수 있게★
};
// ★★색은 상태마다 다르다, 등급은 급한 정도다 (2026-08-16 사용자 지적으로 재설계)★★
//   사용자 원문: "왜 대기가 노란색이야 헷갈리게. 각 상태들은 색깔이 달라야지"
//   ★내가 만든 문제★ — 첫 판에서 색을 코랄/앰버/민트 ★3개★로 뭉갰다. 그래서
//   대기·일시정지·재연결·판매 넷이 전부 같은 앰버가 됐다(성격이 다 다른데).
//   STATUS_CFG 는 원래 상태마다 색이 다른데(대기=회색) 빛샘만 뭉개서 서로 어긋났다.
//   → ★색 = STATUS_CFG 와 같은 계열로 상태마다 하나씩★ / ★등급 = 급한 정도★ 로 분리.
//     등급은 알파·높이·모서리선을 바꾸므로, 색이 20가지여도 정상(ok, 40px)은 옅게 깔리고
//     개입(act, 86px)은 여전히 먼저 눈에 들어온다 — 원래 의도가 안 깨진다.
const DK_BLEED = {
  // ── 개입(act) : 사람이 지금 손대야 한다 ─────────────────────────────────
  captcha:       ['#ff4fa3','act'],   // 핫핑크
  dead:          ['#ff5a4d','act'],   // 주홍빨강 — ★게임 안에서 죽음★ (매크로는 멀쩡)
  error:         ['#e0234a','act'],   // 진홍(크림슨) — ★매크로 자체 고장★ (성격이 다르다)
  awakening_wait:['#ff7a45','act'],   // 주홍 — 고장이 아니라 '눌러줘야 함'
  nightmare_wait:['#ff7a45','act'],
  offline:       ['#8fa3bd','act'],   // ★차가운 회청★ — 꺼진 건 경보색이 아니라 '부재'다.
                                      //   등급은 act 라 크게 새어 눈에는 띈다.
  // ── 주의(warn) : 곧 손이 필요하다. ★앰버는 '일시정지' 하나뿐★ ──────────
  paused:        ['#f2b53c','warn'],  // 앰버
  reconnecting:  ['#fb923c','warn'],  // 주황
  idle:          ['#94a3b8','warn'],  // ★회색 — STATUS_CFG 의 대기 색과 일치★
  // ── 정상 가동(ok) : 옅게만 ─────────────────────────────────────────────
  // ★hunting 과 moving 은 라벨이 둘 다 '사냥 중'★ 이라 반드시 같이 넣어야 한다
  // (하나만 넣으면 같은 글자인데 카드가 깜빡이며 색이 붙었다 떨어진다).
  hunting:   ['#3ddc9a','ok'], moving:    ['#3ddc9a','ok'],   // 민트
  selling:   ['#4a9eff','ok'],                                 // 파랑 — 판매는 정상 작업이다
  abyss:     ['#e879f9','ok'],                                 // 자홍
  corridor:  ['#38bdf8','ok'],                                 // 하늘
  surface:   ['#38bdf8','ok'],                                 // 하늘 — #208 회랑과 같게
  dungeon:   ['#a78bfa','ok'],                                 // 보라
  nightmare: ['#f472b6','ok'],                                 // 분홍
  awakening: ['#818cf8','ok'],                                 // 남보라
  subquest:  ['#a3e635','ok'],                                 // 라임
  collecting:['#22d3ee','ok'],                                 // 시안
  switching: ['#c084fc','ok'],                                 // 자보라
};
function dkBleed(el, st){
  if(!el) return;
  const b = DK_BLEED[st];
  if(b){
    const t = DK_TIER[b[1]] || DK_TIER.warn;
    el.classList.add('bleed');
    el.style.setProperty('--dk-bleed', b[0]+t.a);
    el.style.setProperty('--dk-edge', b[0]);
    el.style.setProperty('--dk-bleed-h', t.h);
    el.style.setProperty('--dk-edge-o', t.o);
  }else{
    el.classList.remove('bleed');
    ['--dk-bleed','--dk-edge','--dk-bleed-h','--dk-edge-o']
      .forEach(p => el.style.removeProperty(p));
  }
}
// ★히어로 채우기(2026-08-16)★ — 전광판(숫자 7개) 위에 '지금 몇 대를 봐야 하나'를 크게.
//   ★기존 집계 함수(refreshSummary)를 안 건드린다★ — 그쪽은 전광판 전용으로 그대로 두고
//   여기서 state 를 직접 훑는다. 실패해도 renderCards 의 try/catch 가 삼켜 화면은 멀쩡.
// ★오늘의 한마디(2026-08-16 사용자 요청)★ — 매일 하나씩 바뀐다.
//   ★난수를 안 쓴다★ — 새로고침할 때마다 바뀌면 '오늘의' 가 아니게 되고, 화면을
//   하루 종일 켜두는 관제화면에서 글자가 계속 갈아엎히면 산만하다. 날짜를 씨앗으로
//   삼아 ★그날 하루는 무조건 같은 문장★이 나오게 한다(KST 기준, 새벽 5시 리셋과 무관).
//   <em> 로 감싼 단어만 금색이 된다.
// ★★2026-08-18 전면 교체 (사용자 지시)★★
//   "명언 좀 괜찮은걸로 ★진짜 있는걸로★ 해라 / 12시간마다 갱신되게하고 / 명언수도 늘려놔"
//   옛 목록은 출처 없는 자기계발 문구가 대부분이었다("매일 1%씩 1년이면 37배" 는
//   계산 자체는 맞지만 — 1.01^365 ≈ 37.8 — 출처가 없어 울림이 없다는 지적).
//   → ★실존 인물이 실제로 한 말★ 만 남기고 전부 갈아엎었다.
//   원칙 ①출처가 확실한 것만 ②출처가 불분명하면 아예 안 쓴다(지어내지 않는다)
//        ③속담·고전은 출처를 '속담'/'논어'처럼 정직하게 표기
//   <em> 로 감싼 단어만 금색이 된다.
const DK_QUOTES = [
  // ── 동양 고전 ──────────────────────────────────────────────
  ["아는 것은 좋아하는 것만, 좋아하는 것은 <em>즐기는 것</em>만 못하다.", "공자 · 논어"],
  ["잘못하고도 고치지 않는 것, 그것이 <em>잘못</em>이다.", "공자 · 논어"],
  ["세 사람이 길을 가면 그중 반드시 나의 <em>스승</em>이 있다.", "공자 · 논어"],
  ["천 리 길도 <em>발밑</em>에서 시작된다.", "노자 · 도덕경"],
  ["가장 큰 그릇은 <em>늦게</em> 이루어진다.", "노자 · 도덕경"],
  ["남을 아는 자는 지혜롭고, <em>자기를 아는 자</em>는 밝다.", "노자 · 도덕경"],
  ["이기는 군대는 <em>먼저 이겨 놓고</em> 싸운다.", "손자 · 손자병법"],
  ["적을 알고 나를 알면 백 번 싸워도 <em>위태롭지 않다</em>.", "손자 · 손자병법"],
  ["싸우지 않고 굴복시키는 것이 <em>최선</em>이다.", "손자 · 손자병법"],
  ["하늘이 큰 일을 맡기려 할 때 먼저 그 마음을 <em>괴롭게</em> 한다.", "맹자"],
  ["아직 신에게는 <em>열두 척</em>의 배가 남아 있사옵니다.", "이순신"],
  ["오늘 걷지 않으면 내일은 <em>뛰어야</em> 한다.", "속담"],
  ["낙숫물이 <em>바위</em>를 뚫는다.", "속담"],
  ["급할수록 <em>돌아가라</em>.", "속담"],

  // ── 스토아 ────────────────────────────────────────────────
  ["우리가 두려워하는 일은 대개 <em>상상 속</em>에서 더 크다.", "세네카"],
  ["어디로 갈지 모르는 배에겐 어떤 바람도 <em>순풍</em>이 아니다.", "세네카"],
  ["삶이 짧은 게 아니라 우리가 <em>낭비</em>하는 것이다.", "세네카"],
  ["할 수 있는 일과 없는 일을 <em>가르는 것</em>이 자유의 시작이다.", "에픽테토스"],
  ["사건이 아니라 그것에 대한 <em>생각</em>이 우리를 흔든다.", "에픽테토스"],
  ["네가 가진 힘은 지금 <em>이 순간</em>에만 있다.", "마르쿠스 아우렐리우스"],
  ["행동을 가로막는 것이 곧 <em>길</em>이 된다.", "마르쿠스 아우렐리우스"],
  ["완벽한 사람이 되려 애쓰지 말고, 지금 <em>그런 사람이 되라</em>.", "마르쿠스 아우렐리우스"],

  // ── 과학·발명 ─────────────────────────────────────────────
  ["천재는 <em>1%의 영감</em>과 99%의 노력이다.", "에디슨"],
  ["실패한 게 아니다. 안 되는 방법 <em>1만 가지</em>를 찾았을 뿐.", "에디슨"],
  ["기회는 <em>준비된 자</em>에게만 미소짓는다.", "파스퇴르"],
  ["인생은 자전거 타기와 같다. 균형은 <em>움직여야</em> 잡힌다.", "아인슈타인"],
  ["실수해 본 적 없는 사람은 <em>새로운 것</em>을 시도한 적 없는 사람이다.", "아인슈타인"],
  ["거인의 <em>어깨</em> 위에 서서 더 멀리 보았다.", "뉴턴"],
  ["관찰의 영역에서 우연은 <em>준비된 정신</em>을 돕는다.", "파스퇴르"],

  // ── 정치·역사 ─────────────────────────────────────────────
  ["성공은 최종적이지 않고 실패는 치명적이지 않다. <em>계속</em>할 용기다.", "처칠"],
  ["지옥을 지나는 중이라면, <em>계속 걸어라</em>.", "처칠"],
  ["비관론자는 모든 기회에서 <em>어려움</em>을 본다.", "처칠"],
  ["끝나기 전까지는 항상 <em>불가능</em>해 보인다.", "넬슨 만델라"],
  ["나는 지지 않는다. 이기거나 <em>배우거나</em> 한다.", "넬슨 만델라"],
  ["하루라도 책을 읽지 않으면 입에 <em>가시</em>가 돋는다.", "안중근"],

  // ── 문학·예술 ─────────────────────────────────────────────
  ["서두르지 말되, <em>쉬지도</em> 마라.", "괴테"],
  ["할 수 있다고 믿는 순간, 이미 <em>절반</em>은 한 것이다.", "괴테"],
  ["시작하라. 대담함 속에 <em>천재성</em>이 있다.", "괴테"],
  ["나를 죽이지 못하는 것은 나를 <em>강하게</em> 만든다.", "니체"],
  ["왜 살아야 하는지 아는 사람은 <em>어떻게든</em> 견딘다.", "니체"],
  ["완벽함은 더할 게 없을 때가 아니라 <em>뺄 게 없을 때</em> 온다.", "생텍쥐페리"],
  ["배를 만들려면 나무가 아니라 <em>바다</em>를 그리워하게 하라.", "생텍쥐페리"],

  // ── 스포츠 ────────────────────────────────────────────────
  ["9000번 넘게 슛을 놓쳤다. 그래서 <em>성공</em>했다.", "마이클 조던"],
  ["재능은 경기를 이기고, <em>팀워크</em>는 우승을 가져온다.", "마이클 조던"],
  ["1만 가지 발차기를 한 번씩 한 사람보다 <em>한 가지</em>를 1만 번 한 사람이 무섭다.", "이소룡"],
  ["물처럼 되어라, <em>친구여</em>.", "이소룡"],
  ["챔피언은 체육관이 아니라 <em>내면</em>에서 만들어진다.", "무하마드 알리"],
  ["나는 훈련의 매 순간이 <em>싫었다</em>. 하지만 챔피언으로 살고 싶었다.", "무하마드 알리"],
  ["포기하면 그 순간 <em>시합 종료</em>입니다.", "안 선생님 · 슬램덩크"],

  // ── 현대 ─────────────────────────────────────────────────
  ["당신의 시간은 한정돼 있다. <em>남의 삶</em>을 살지 마라.", "스티브 잡스"],
  ["혁신은 <em>1000가지</em>를 거절하는 데서 온다.", "스티브 잡스"],
  ["빨리 움직이고 <em>부딪혀라</em>.", "마크 저커버그"],
  ["실패가 선택지가 아니라면 <em>혁신</em>도 선택지가 아니다.", "일론 머스크"],
  ["가장 위험한 건 <em>아무 위험</em>도 감수하지 않는 것이다.", "마크 저커버그"],
  ["계획이 없는 목표는 그저 <em>소원</em>일 뿐이다.", "생텍쥐페리"],
  ["측정할 수 없으면 <em>개선</em>할 수 없다.", "피터 드러커"],
  ["미래를 예측하는 최선의 방법은 그것을 <em>만드는</em> 것이다.", "피터 드러커"],
  ["단순함이 <em>궁극의 정교함</em>이다.", "레오나르도 다 빈치"],
  ["세상에서 가장 어려운 일은 <em>시작하는</em> 일이다.", "톨스토이"],
  ["아무것도 하지 않으면 <em>아무 일</em>도 일어나지 않는다.", "속담"],
  ["가장 좋은 나무를 심을 때는 20년 전, 그다음은 <em>지금</em>이다.", "속담"],
  ["천 마일의 여정도 <em>한 걸음</em>에서 시작된다.", "노자 · 도덕경"],

  // ── 2026-08-18 증설분 (사용자: "140개로 늘려 나에게 힘을 줄 수 있는 걸로") ──
  // ★흔히 잘못 붙는 말은 '진짜 출처'로 표기했다★ — 예: "탁월함은 습관"은
  //   아리스토텔레스가 아니라 그를 요약한 윌 듀런트의 문장이다.

  // ── 버티는 힘 ─────────────────────────────────────────────
  ["인간은 파괴될지언정 <em>패배하지</em> 않는다.", "헤밍웨이 · 노인과 바다"],
  ["또 실패하라. 더 <em>낫게</em> 실패하라.", "사무엘 베케트"],
  ["본래 땅 위엔 길이 없다. 걷는 사람이 많아지면 <em>길</em>이 된다.", "루쉰 · 고향"],
  ["죽는 날까지 하늘을 우러러 한 점 <em>부끄럼</em>이 없기를.", "윤동주 · 서시"],
  ["죽고자 하면 살고, 살고자 하면 <em>죽는다</em>.", "이순신"],
  ["고통은 피할 수 없지만 괴로움은 <em>선택</em>이다.", "무라카미 하루키"],
  ["절망의 한복판에서도 나는 <em>희망</em>을 세었다.", "괴테"],
  ["넘어지는 건 상관없다. 다만 <em>일어나는</em> 걸 잊지 마라.", "격언"],
  ["곤란은 사람을 <em>키운다</em>.", "마쓰시타 고노스케"],
  ["괴로움을 지나야 <em>즐거움</em>이 온다.", "채근담"],
  ["궁하면 변하고, 변하면 통하고, 통하면 <em>오래간다</em>.", "주역"],

  // ── 꾸준함 ───────────────────────────────────────────────
  ["우리가 반복하는 것이 우리다. 탁월함은 행위가 아니라 <em>습관</em>이다.", "윌 듀런트"],
  ["이기는 것은 <em>습관</em>이다. 불행히 지는 것도 그렇다.", "빈스 롬바르디"],
  ["나는 1526경기 중 80%를 이겼지만, <em>포인트</em>는 54%만 이겼다.", "로저 페더러"],
  ["무슨 생각을 해. <em>그냥 하는</em> 거지.", "김연아"],
  ["성공은 매일 반복한 <em>작은 노력</em>의 합이다.", "로버트 콜리어"],
  ["천리마도 한 번에 <em>열 걸음</em>을 갈 수 없다.", "순자"],
  ["도끼를 갈 시간이 없다는 나무꾼은 <em>영영</em> 나무를 못 벤다.", "격언"],
  ["아침에 일어나 <em>할 일</em>이 있다는 것, 그것이 행운이다.", "격언"],
  ["매일 조금씩. <em>그것이</em> 무서운 것이다.", "속담"],
  ["오늘 할 수 있는 일을 <em>내일로</em> 미루지 마라.", "벤저민 프랭클린"],
  ["준비에 실패하는 것은 곧 <em>실패</em>를 준비하는 것이다.", "벤저민 프랭클린"],
  ["시간을 사랑하라. 그것이 <em>인생</em>을 이루는 재료다.", "벤저민 프랭클린"],

  // ── 시작·용기 ────────────────────────────────────────────
  ["시작이 그 일의 <em>가장 중요한</em> 부분이다.", "플라톤"],
  ["검토되지 않은 삶은 <em>살 가치</em>가 없다.", "소크라테스"],
  ["할 수 있다고 믿으면 이미 <em>절반</em>은 온 것이다.", "시어도어 루스벨트"],
  ["할 수 있다고 생각하든 없다고 생각하든, <em>당신 말이 맞다</em>.", "헨리 포드"],
  ["함께 모이면 시작이고, 함께 일하면 <em>성공</em>이다.", "헨리 포드"],
  ["장애물이란 목표에서 눈을 뗐을 때 <em>보이는</em> 것이다.", "헨리 포드"],
  ["주사위는 <em>던져졌다</em>.", "율리우스 카이사르"],
  ["불가능이란 노력하지 않은 자의 <em>변명</em>이다.", "나폴레옹"],
  ["운명은 우리 행동의 절반을 지배하고, 나머지 절반은 <em>우리에게</em> 맡긴다.", "마키아벨리"],
  ["완벽은 <em>좋음</em>의 적이다.", "볼테르"],
  ["행운의 여신은 <em>대담한 자</em>를 돕는다.", "베르길리우스"],
  ["가장 위대한 영광은 넘어지지 않는 게 아니라 <em>매번 일어서는</em> 것이다.", "격언"],

  // ── 함께·사람 ────────────────────────────────────────────
  ["혼자서는 적은 일을, 함께라면 <em>많은 일</em>을 할 수 있다.", "헬렌 켈러"],
  ["삶은 대담한 모험이거나, <em>아무것도</em> 아니다.", "헬렌 켈러"],
  ["빨리 가려면 혼자, 멀리 가려면 <em>함께</em> 가라.", "아프리카 속담"],
  ["성공은 형편없는 <em>선생</em>이다. 똑똑한 사람을 지게 만든다.", "빌 게이츠"],
  ["능력에 열의를 곱하고, 거기에 <em>사고방식</em>을 곱한다.", "이나모리 가즈오"],
  ["10년을 보유할 생각이 없다면 <em>10분</em>도 보유하지 마라.", "워런 버핏"],
  ["남들이 두려워할 때 욕심을 내고, 욕심낼 때 <em>두려워하라</em>.", "워런 버핏"],

  // ── 생각·태도 ────────────────────────────────────────────
  ["내 삶엔 끔찍한 불행이 가득했다. <em>대부분</em>은 일어나지 않았다.", "몽테뉴"],
  ["우물 안 개구리에게 <em>바다</em>를 말할 수 없다.", "장자"],
  ["문제를 만든 것과 같은 사고로는 그 문제를 <em>못 푼다</em>.", "아인슈타인"],
  ["상상력은 지식보다 <em>중요하다</em>.", "아인슈타인"],

  // ── 실행 ─────────────────────────────────────────────────
  ["계획은 쓸모없지만 <em>계획하는 일</em>은 반드시 필요하다.", "아이젠하워"],
  ["급한 일과 <em>중요한 일</em>은 좀처럼 같지 않다.", "아이젠하워"],
  ["아는 것만으로는 부족하다. <em>적용</em>해야 한다.", "괴테"],
  ["의지만으론 부족하다. <em>실행</em>해야 한다.", "괴테"],
  ["행동이 항상 행복을 주진 않지만, 행동 없는 <em>행복</em>은 없다.", "벤저민 디즈레일리"],
  ["성공의 비결은 <em>목적의 불변</em>이다.", "벤저민 디즈레일리"],
  ["작게 시작하되, <em>시작</em>하라.", "격언"],
  ["잘 시작된 일은 <em>절반</em>이 끝난 것이다.", "아리스토텔레스"],
  ["기회는 <em>일하는 사람</em> 곁을 지나간다.", "격언"],
  ["모든 걸 빼앗겨도 마지막 자유, <em>태도를 고를</em> 자유는 남는다.", "빅터 프랭클"],
  // ── 2026-08-18 재구성 (사용자: "일하면서 힘날 말들로 구성해줘") ────────────
  //   관념적인 문장을 빼고 ★현장에서 손 움직일 때 힘이 되는 말★ 로 갈아끼웠다.
  ["이봐, <em>해봤어</em>?", "정주영"],
  ["길이 없으면 찾고, 찾아도 없으면 <em>만들면</em> 된다.", "정주영"],
  ["마누라와 자식 빼고 <em>다 바꿔라</em>.", "이건희"],
  ["세계는 넓고 <em>할 일</em>은 많다.", "김우중"],
  ["아마추어는 영감을 기다리고, 나머지는 그냥 <em>일하러</em> 간다.", "스티븐 킹"],
  ["영감은 아마추어의 것이다. 나머지는 그냥 <em>나와서 일한다</em>.", "척 클로스"],
  ["영감은 분명 존재한다. 다만 <em>일하는 중</em>에 찾아온다.", "피카소"],
  ["기회는 작업복을 입고 와서 <em>일처럼</em> 보인다.", "에디슨"],
  ["연습을 많이 할수록 <em>운이 좋아진다</em>.", "게리 플레이어"],
  ["아무도 나를 <em>구하러</em> 오지 않는다.", "데이비드 고긴스"],
  ["압박도 시련도 전부 내가 <em>올라설 기회</em>다.", "코비 브라이언트"],
  ["6시간 자고도 모자라면 <em>더 빨리</em> 자라.", "아널드 슈워제네거"],
  ["멈추지 마라. 그냥 <em>계속 가라</em>.", "필 나이트 · 슈독"],
  ["일할 때는 <em>일만</em> 생각하라.", "존 D. 록펠러"],
  ["가장 큰 장애물은 <em>내일부터</em> 하겠다는 마음이다.", "세네카"],
  ["낙망은 <em>청년의 죽음</em>이다.", "안창호"],
  ["졸속이라도 빠른 것이 <em>정교한 지연</em>보다 낫다.", "손자 · 손자병법"],
  ["일어나기 싫은 아침, 나는 <em>사람의 일</em>을 하러 태어났다.", "마르쿠스 아우렐리우스"],
  ["가장 중요한 때는 <em>지금</em>, 가장 중요한 일은 지금 하는 일이다.", "톨스토이"],
  ["비가 오면 <em>우산</em>을 펴라. 그뿐이다.", "마쓰시타 고노스케"],
  ["누구에게도 지지 않을 <em>노력</em>을 하라.", "이나모리 가즈오"],
  ["성공은 99%의 실패에서 태어난 <em>1%</em>다.", "혼다 소이치로"],
  ["오늘은 힘들고 내일은 더 힘들다. 그러나 <em>모레</em>는 아름답다.", "마윈"],
  ["성공은 최선을 다했다는 <em>마음의 평화</em>다.", "존 우든"],
];
function dkQuote(){
  const $ = id => document.getElementById(id);
  const el = $('dk-q-text'); if(!el) return;
  // ★12시간마다 갱신 (2026-08-18 사용자 지시)★ — 하루 종일 같은 문장은 지겹고,
  //   매 새로고침마다 바뀌면 산만하다. KST 기준 오전/오후로 딱 두 번만 바뀐다.
  //   씨앗이 '반나절 번호'라 새로고침해도 같은 문장이 유지된다.
  const kst = new Date(Date.now() + (9*60 + new Date().getTimezoneOffset())*60000);
  const halfDays = Math.floor(
    (Date.UTC(kst.getFullYear(), kst.getMonth(), kst.getDate())/3600000 + kst.getHours()) / 12);
  const n = DK_QUOTES.length;
  const [text, by] = DK_QUOTES[((halfDays % n) + n) % n];
  el.innerHTML = text;
  const byEl = $('dk-q-by');
  if(byEl) byEl.innerHTML = by ? `— <b>${by}</b>` : '';
  const dayEl = $('dk-q-day');
  if(dayEl) dayEl.textContent =
    `${kst.getMonth()+1}월 ${kst.getDate()}일 ${kst.getHours() < 12 ? '오전' : '오후'} · 한마디`;
}

// 전광판(refreshSummary)이 구한 합계를 히어로가 물려받는다 — ★한 곳에서만 센다★
const DK_SUM = {kina: 0, trade: null};

// ★구독 O/X 세기 (2026-08-29)★ — 오드에너지 분모 ≥840 = 구독, 560 = 해제.
//   ★계정(카드) 단위★ 라 pc_id 로 묶고 그 계정 캐릭 중 ★분모 최댓값★ 으로 판정한다
//   (aiBuildPlan 과 같은 규칙·같은 파서 — 두 화면이 다른 답을 내면 안 된다).
//   ★오드에너지를 아직 못 읽은 계정은 어느 쪽도 아니다★ — X 로 세지 않고 따로 알린다.
function dkSubCount(){
  // ★카드 뱃지(subBadge)와 ★같은 함수★ 로 센다 (2026-08-29)★ — 예전엔 여기서 따로
  //   `>= 840` 으로 세는 바람에 전광판 숫자와 카드 뱃지가 어긋날 수 있었다.
  //   ★renderCards 와 같은 모집단★ 을 쓴다 — PC-TEST/PC-DEMO 는 카드로 안 그린다(4409).
  let sub = 0, nosub = 0, unknown = 0;
  Object.keys(state || {}).forEach(id => {
    if (isExcludedPc(id)) return;   // ★가짜·계정없음·은퇴는 분모에서도 뺀다(2026-09-23)★ — unknown 도 아니다(isExcludedPc 한 곳)
    const v = subState(id);
    if (v === 'on') sub++; else if (v === 'off') nosub++; else unknown++;
  });
  return {sub, nosub, unknown};
}

// ★서버 전광판 숫자(2026-09-23)★ — 주인님 「팜뷰 전광판은 대시보드 전광판이 반영 안
//   되냐, 숫자가 왜 이렇게 다르냐」. 오늘 넣은 보정(은퇴·계정없음 제외·각성전 리셋)이
//   이 화면 JS 에만 있고 /api/fv/snapshot 은 옛 계산이라 서로 달랐다. 서버 GET /summary
//   가 /api/fv/snapshot 과 ★같은 함수★(_fv_build_snapshot) 로 계산해 준다.
// ★값을 바로 DOM 에 못 박지 않는다★ — refreshSummary·dkHero·updateCorridorTile 이
//   더 잦은 주기(상태 갱신마다)로 그 자리를 다시 그리므로, fetch 로 한 번 덮어써도
//   바로 다음 렌더에서 클라 계산으로 되돌아간다(깜빡임). 대신 SERVER_SUMMARY 에
//   저장해 두고, 저 세 함수가 ★있으면 그 값을 최종값으로★ 쓰게 한다(§A12 — 규칙은
//   여전히 서버 하나, 클라 계산은 서버값이 오기 전 첫 화면용 폴백으로만 남는다).
let SERVER_SUMMARY = null, SERVER_SUMMARY_AT = 0;
// 폴백 재료가 언제 것인지(2026-09-23 전수 #6·#7) — 서버가 끊겨 화면 계산으로 돌아가도 그 재료
//   (캐릭 표·회랑 목록)가 같이 낡았으면 「화면 계산」 이라는 말만으로는 거짓이 된다. 나이를 같이 적는다.
let CHAR_TABLE_AT = 0, CORRIDOR_AT = 0;
function ageNote(at, label, staleMs, now){
  const t = (now == null ? sumClock() : now);
  if(!at) return ' · ' + label + ' 아직 못 받음';
  return (t - at) > staleMs ? ' · ' + label + ' ' + Math.round((t - at)/60000) + '분째 못 받음' : '';
}
// ★시각은 performance.now()(단조 시계)★ — 벽시계(Date.now)는 PC 시계가 뒤로 가면 낡은 값을
//   그만큼 더 「신선」 하게 보고 「-N초 전」 을 적었다(2026-09-23 반증 B5).
function sumClock(){ return (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now(); }
// ★낡은 서버값을 영원히 믿지 않는다(2026-09-23 반증 #4)★ — /summary 가 실패하면 예전엔
//   마지막 값이 무기한 남았다. 주기(20초)의 세 배가 지나면 서버값을 버리고 화면 계산(폴백)으로
//   돌아간다. 요청 하나가 걸려도 10초에 끊는다.
const SERVER_SUMMARY_TTL_MS = 60000;
function serverSum(now){
  if(!SERVER_SUMMARY) return null;
  return ((now == null ? sumClock() : now) - SERVER_SUMMARY_AT) < SERVER_SUMMARY_TTL_MS ? SERVER_SUMMARY : null;
}
// 전광판 칸 툴팁에 붙일 출처 한 줄 — 서버값인지, 끊겨서 화면 계산인지 사람이 가를 수 있게.
function serverSumNote(now){
  const t = (now == null ? sumClock() : now);
  if(serverSum(t)) return '※ 서버 계산(팜뷰와 같은 값) · ' + Math.round((t - SERVER_SUMMARY_AT)/1000) + '초 전';
  return SERVER_SUMMARY
    ? '※ 서버 요약이 ' + Math.round((t - SERVER_SUMMARY_AT)/1000) + '초째 끊김 — 이 화면 계산으로 표시 중' + ageNote(CHAR_TABLE_AT, '캐릭 표', 300000, t)
    : '※ 서버 요약 대기 중 — 이 화면 계산으로 표시 중' + ageNote(CHAR_TABLE_AT, '캐릭 표', 300000, t);
}
// ★renderCards 와 같은 모집단(가짜 PC 제외)★ — 예전엔 20초마다 여기서만 PC-TEST 가 섞여
//   던전·캐릭 칸이 한 번 튀었다가 다음 renderCards 에 돌아왔다(2026-09-23 반증 B3).
function summaryPcs(){ return Object.values(state||{}).filter(p => !isFakePc(p.pc_id)); }
function redrawSummary(){
  try { refreshSummary(summaryPcs()); } catch(e){ console.error('refreshSummary', e); }
  try { updateCorridorTile(); } catch(e){ console.error('updateCorridorTile', e); }
  try { dkHero(); } catch(e){ console.error('dkHero', e); }
}
async function loadServerSummary(){
  const ac = (typeof AbortController === 'function') ? new AbortController() : null;
  const to = ac ? setTimeout(() => ac.abort(), 10000) : null;
  try{
    const r = await fetch('/summary', ac ? {cache:'no-store', signal: ac.signal} : {cache:'no-store'});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    SERVER_SUMMARY = await r.json();
    SERVER_SUMMARY_AT = sumClock();
  }catch(e){ console.error('서버 전광판 요약 실패', e); }
  finally{ if(to) clearTimeout(to); }
  redrawSummary();   // 실패해도 다시 그린다 — 낡았으면 serverSum() 이 null 이라 폴백으로 바뀐다
}

function dkHero(){
  const $ = id => document.getElementById(id);
  dkQuote();
  // ★제외 PC(가짜·은퇴·계정없음)는 평균·스파크라인에서 뺀다(2026-09-23 반증 2바퀴)★ —
  //   카드엔 안 보이는 PC-DEMO 90 이 PC-01 10 과 평균돼 50.0 으로 보였다.
  const pcs = Object.entries(state||{}).filter(([id])=>!isExcludedPc(id)).map(([,p])=>p);
  if(!pcs.length) return;
  const ef = pcs.filter(p=>p.efficiency);
  const avg = ef.length ? ef.reduce((a,p)=>a+p.efficiency,0)/ef.length : 0;
  // 효율을 가진 카드가 하나도 없으면 「0.0」 이 아니라 「–」(모름, 팜뷰 eff:null 과 같게)
  $('dk-h-eff').innerHTML = ef.length ? avg.toFixed(1)+'<i>%/h</i>' : '–';
  // ★키나 = 창고 + 거래 (2026-08-29 주인님)★ — 전광판이 센 값을 그대로 쓴다.
  const kw = DK_SUM.kina || 0, kt = DK_SUM.trade;
  const kEl = $('dk-h-kina');
  kEl.textContent = fmtKinaShort(kw + (kt || 0));
  kEl.parentElement.title = kt == null
    ? `창고키나 ${fmtKina(kw)} — ★거래키나는 아직 수집 전이라 안 더해졌습니다★`
    : `창고키나 ${fmtKina(kw)} + 거래키나 ${fmtKina(kt)} = ${fmtKina(kw + kt)}`;

  // ★구독 O / X★ — 서버값 있으면 그게 최종값(2026-09-23, §A12·/api/fv/snapshot 과 같은 계산)
  const ssH = serverSum();
  const sc = ssH ? (ssH.subscribed || {sub:0,nosub:0,unknown:0}) : dkSubCount();
  const on = $('dk-h-sub-on'), off = $('dk-h-sub-off'), box = $('dk-h-subbox');
  if (on)  on.textContent  = sc.sub;
  if (off) off.textContent = sc.nosub;
  if (box) box.title = `오드에너지 분모로 판정합니다 — 840=구독 / 560=구독 해제 (계정 단위)`
    + `\n구독 ${sc.sub}개 · 해제 ${sc.nosub}개`
    + (sc.unknown ? `\n★${sc.unknown}개는 오드에너지 미수집이라 판정 불가★ — 어느 쪽에도 안 셌습니다 (정보수집을 돌리면 채워집니다)` : '')
    + `\n` + serverSumNote();

  // 스파크라인 — 각 PC 효율을 이어 그린 실제 데이터(장식 아님)
  const vs = ef.map(p=>p.efficiency);
  const sp = $('dk-spark');
  if(sp && vs.length>1){
    const mx=Math.max(...vs), mn=Math.min(...vs), sc=Math.max(1,mx-mn);
    sp.innerHTML = `<polyline points="${vs.map((v,i)=>
      [i/(vs.length-1)*102+1, 24-((v-mn)/sc)*21].map(n=>n.toFixed(1)).join(',')).join(' ')}"
      fill="none" stroke="#3ddc9a" stroke-width="1.6" stroke-linejoin="round" opacity=".85"/>`;
  }
}

// 카드가 다시 그려질 때마다 훑는다(렌더 경로가 여러 갈래라 한 곳에서 처리)
function dkApplyBleed(){
  document.querySelectorAll('[id^="card-"]').forEach(el=>{
    const id = el.id.slice(5);
    const st = ((state[id]||{}).status)||'offline';
    dkBleed(el, st);
  });
}

function fmtKina(n) { return (!n&&n!==0)?'–':'₭'+Number(n).toLocaleString('en-US'); }
function fmtRate(n) { return (!n&&n!==0)?'–':'₭'+Number(n).toLocaleString('en-US')+'/hr'; }
// 큰 키나 축약: 1천만↑ → X.X억, 1만↑ → X만 (카드 창고키나가 길고 작게 보이던 것 개선)
function fmtKinaShort(n) {
  if (n==null) return '–';
  const a=Math.abs(n);
  if (a>=1e7) return '₭'+(n/1e8).toFixed(1)+'억';
  if (a>=1e4) return '₭'+Math.round(n/1e4).toLocaleString('en-US')+'만';
  return '₭'+Number(n).toLocaleString('en-US');
}
// 카드 안의 「N초 전」 은 이 꼴로 — 글자는 매초 바뀌지만 카드 HTML 비교(reconcileGrid)에서는 빼고,
//   남겨 둔 카드는 이 칸 글자만 새로 쓴다(2026-09-23). 없으면 살아 있는 카드가 매 렌더 통째로 갈려
//   호버·포커스·글자 선택이 날아갔다(반증 B2 참고).
function relSpan(iso) { return `<span data-rt="${escAttr(iso||'')}">${relTime(iso)}</span>`; }
function relTime(iso) {
  if (!iso) return '–';
  const d = Math.floor((Date.now()-new Date(iso+'Z').getTime())/1000);
  if (d<5) return '방금'; if (d<60) return d+'초 전';
  if (d<3600) return Math.floor(d/60)+'분 전'; return Math.floor(d/3600)+'시간 전';
}

const CLASS_LABEL = {gungsung:'궁성',spirit:'정령성',kumsung:'검성',chiyousung:'치유성'};

// ─── 완료 뱃지 (사냥=매일 05:00 / 각성전·일일던전=매주 수요일 05:00 초기화) ─────────
// 완료 판정에 '초기화 경계 이후 데이터'만 인정 — PC가 밤새 꺼져 있어도 어제 완료가
// 오늘 완료로 둔갑하지 않게 시각 게이트.
function fmtTs(d){const p=n=>String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;}
// ★B-JS11 (2026-09-23) 리셋 경계는 ★KST 로 계산한다★ — 보는 기기 시간대가 아니다★
//   예전엔 브라우저 로컬 05:00 을 썼다. 베트남(UTC+7) 직원 폰에선 경계가 07:00 KST 가 되고,
//   수요일 05~07시 KST 사이엔 주간 경계가 ★지난주★ 로 잡혔다. 게임 리셋은 한국 서버 기준이다.
//   반환값은 ★절대 시각(Date)★ — Date 비교(isBeforeReset)는 그대로 맞다.
//   KST 문자열(completed_time·dungeon_done_at, 매크로가 KST 로 찍는다)과 비교할 땐 fmtKstTs 로.
const KST_OFF_MS = 9 * 3600000, GAME_RESET_MS = 5 * 3600000;
function kstGameDayNum(ms){ return Math.floor((ms + KST_OFF_MS - GAME_RESET_MS) / 86400000); }   // 1970-01-01(목)=0
function fmtKstTs(d){const k=new Date(d.getTime()+KST_OFF_MS), p=n=>String(n).padStart(2,'0');
  return `${k.getUTCFullYear()}-${p(k.getUTCMonth()+1)}-${p(k.getUTCDate())} ${p(k.getUTCHours())}:${p(k.getUTCMinutes())}:${p(k.getUTCSeconds())}`;}
function lastDailyReset(){ return new Date(kstGameDayNum(Date.now()) * 86400000 + GAME_RESET_MS - KST_OFF_MS); }
function lastWeeklyReset(){   // 가장 최근 수요일 05:00 KST
  const D = kstGameDayNum(Date.now()), back = ((D + 4) % 7 - 3 + 7) % 7;   // (D+4)%7 = 요일(0=일), 3=수
  return new Date((D - back) * 86400000 + GAME_RESET_MS - KST_OFF_MS);
}
// ★B-JS6 (2026-09-23)★ 서버 시각(UTC naive) → ★보는 기기의 로컬 시각★ 문자열. 못 읽으면 원문 조각.
function fmtLocalAt(raw, a, b){
  const d = collectedAtDate(raw);
  return d ? fmtTs(d).slice(a, b) : String(raw || '').replace('T', ' ').slice(a, b);
}
function isHuntDone(dp){
  if(!dp||!dp.length) return false;
  const cut=fmtKstTs(lastDailyReset());   // ★B-JS11★ completed_time 은 KST 문자열
  return dp.every(c=>c.completed && ((c.completed_time||'').replace('T',' ')>=cut));
}
function isAwakenDone(pc_id){
  // 규칙(사용자 확정): 전 캐릭터 각성 티켓 0/3이면 스탬프 — 단순 판정.
  // (수요일 05시 초기화 후엔 게임이 3/3으로 돌아가고 다음 정보수집 때 DB 갱신돼 자연 소멸)
  const rows=charTableData.filter(r=>r.pc_id===pc_id);
  return rows.length>0 && rows.every(r=>parseInt(r.awakening_ticket)===0);
}
// ★★사고 610 (2026-09-20 주인님)★★ 표층 이용 시간이 0 인 캐릭은 ★카드에서 이름을 빨갛게★
//   주인님: 「표층 시간 00:00:00 이면 … 그 이름들 빨간색으로 표시되게 해줘 글자색」
//   근거 값은 /characters 의 abyss_time — 정보수집이 어비스 창 「기본 이용 시간」을 읽어 넣고,
//   표층 진입에서 0 을 보면 매크로가 00:00:00 으로 merge 한다(lc/surface_zero.record, 사고 574).
//   ★빈 값·null·'–' 는 빨강이 아니다★ — 「못 읽었다」와 「0 이다」는 다르다(사고 464).
//   카드 payload 엔 이름만 있어서(card["chars"]) charTableData 를 pc_id+slot 으로 조인한다.
function isSurfaceZero(v){
  const t = String(v == null ? '' : v).trim();
  if (!t || t === '–' || t === '-') return false;
  return t.includes(':') && /^[0:]+$/.test(t);
}
// ★공용: UTC naive 문자열 → Date (2026-09-23)★ — collected_at 은 lc/report_module._now()
//   (datetime.now(utc)) 문자열이라 ★'Z' 를 붙여야 UTC 로 정확히 파싱된다★(안 붙이면
//   브라우저가 로컬로 오해해 9시간 어긋난다 — 실측: 로그 원문 12:52 vs DB created_at 03:52).
//   isSurfaceZero·displayAbyssTime 이 이 하나만 쓴다(규칙이 둘로 갈리면 화면끼리 싸운다, §A12).
function collectedAtDate(raw){
  const t = String(raw || '').trim();
  if (!t) return null;
  const iso = t.replace(' ', 'T');
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  return isNaN(d.getTime()) ? null : d;
}
function isBeforeReset(raw){   // 가장 최근 수요일 05:00(KST) 이전 수집인가 — 모르면 false(모름≠이전)
  const d = collectedAtDate(raw);
  return !!d && d < lastWeeklyReset();
}
// ★주간 초기화 티켓 공용 보정(2026-09-23 — 주인님 「각성전은 왜 초기화 안 돼 있냐」)★
//   각성전(3)·일일던전(14) ★둘 다 확인됨★ 수요일 05시 초기화로 게임이 가득 찬 값으로
//   돌아간다(각성전: isAwakenDone 주석 · 일일던전: isDungeonDone 주석 "각성전과 같은
//   주간 리셋"). 리셋 전에 수집한 낮은 값은 다음 정보수집 전까지 옛 값 그대로였다 —
//   표층(abyss_time)과 ★같은 부류★ 라 같은 판정(isBeforeReset)을 재사용한다.
//   ★이미 가득 찬 값은 안 건드린다★(정말 그 값일 수도 있으니 굳이 덮어쓸 이유가 없다).
function resetAwareTicket(collected_at, raw, full){
  const n = parseInt(raw);
  if (isNaN(n) || n >= full) return raw;
  if (!collected_at || !isBeforeReset(collected_at)) return raw;
  return full;
}
// ★성역(2026-09-23, 주인님 게임 규칙 확정) — 수요일 05:00 초기화★
//   각성전·일일던전과 달리 「몇으로 돌아가는지」가 아니라 ★비었다는 사실★ 만 안다
//   (게임이 다시 채워주는 목표치를 우리가 모른다) — 그래서 값을 지어내지 않고
//   "초기화됨"(미완) 으로만 표시한다. sanctuary 는 "N/M" 문자열이라 숫자 보정과 다르다.
function resetAwareSanctuary(collected_at, raw){
  if (!raw) return raw;
  if (!collected_at || !isBeforeReset(collected_at)) return raw;
  return '초기화됨';
}
// ★2026-09-23 주인님 — 「수요일 새벽 5시 초기화인데 적용된 거지? 아직 빨강 남아있다」★★
//   원인: abyss_time 「00:00:00」 만 보고 ★언제 읽힌 값인지★ 안 봤다 — 리셋 전에 수집한
//   0 이 다음 정보수집 전까지 계속 빨갛다. lc/surface_zero.py 의 리셋 규칙(수요일 05시,
//   RESET_WEEKDAY=2·RESET_HOUR=5)과 ★같은 경계★ 를 여기서도 쓴다 — isDungeonDone 이
//   이미 쓰는 lastWeeklyReset() 그대로(규칙이 둘로 갈리면 화면끼리 싸운다, §A12).
//   ★행 자체에 수집 시각이 있으면 그걸 쓰고, 없으면 카드의 _char_collected_at 으로★.
function surfaceZeroSlots(pc){
  const out = new Set();
  const pc_id = pc && pc.pc_id;
  if (!pc_id) return out;
  for (const r of charTableData) {
    if (r.pc_id !== pc_id || !isSurfaceZero(r.abyss_time)) continue;
    const raw = r.collected_at || (pc && pc._char_collected_at) || '';
    if (!raw || isBeforeReset(raw)) continue;   // 수집 시각 없음·리셋 이전 값 = 빨강 아님
    out.add(Number(r.slot));
  }
  return out;
}
// ★표시값도 14:00:00 으로(2026-09-23 주인님 게임 규칙)★
//   「초기화라서 정보수집 없이도 표층 남은 시간이 14:00:00 이 된다고 생각하면 된다」.
//   ★DB 원본(r.abyss_time)은 안 건드린다★ — 캐릭터 테이블의 화면 표시만 바꾼다.
//   수집 시각을 모르면(collected_at 없음) 원본 그대로 — 모름을 14 로 단정하지 않는다(사고 464).
function displayAbyssTime(r){
  const v = r.abyss_time || '';
  if (!isSurfaceZero(v)) return v || '–';
  if (!r.collected_at || !isBeforeReset(r.collected_at)) return v;
  return '14:00:00';
}
// ★구독 O/X — PC명 옆 동그라미 뱃지 (2026-08-29 주인님 요청)★
//   판정은 ★오드에너지 분모★ 다: 840(또는 800)=구독 / 560=구독 해제.
//   ★lc/info_collector._subscribed(사고 219) · dkSubCount(3644) 와 같은 규칙·같은 파서★ —
//   세 곳이 다른 답을 내면 화면끼리 싸운다.
//   ★분모는 계정 단위 속성이다★ — baseId 가 아니라 ★그 카드에 떠 있는 계정(pc_id)★ 으로
//   본다. 스택을 넘겨 뒷계정을 보면 뱃지도 같이 바뀐다(그게 "계정마다" 의 뜻).
//   ★못 읽은 것을 X 로 읽지 않는다★ — 그게 사고 219 의 교훈이다(모름 ≠ 미구독). 회색 ?.
// ★문턱은 700 이다 — 840 이 아니다 (2026-08-29 실측으로 정정)★
//   `lc/info_collector._subscribed` 는 `>= 700` 으로 판정한다(분모 840 ★또는 800★ = 구독).
//   이 화면이 840 을 쓰고 있어서 ★분모 800 인 계정을 매크로는 「구독」, 화면은 「구독 X」★
//   로 서로 다르게 답했다. 실측: PC-17 슬롯1 이 `/800` 이다.
//   ★행동을 가르는 쪽(매크로)이 정본이다★ — 화면이 매크로를 따라간다.
const SUB_DEN_MIN = 700;
function subState(pc_id){
  let max = 0, seen = false;
  (charTableData||[]).forEach(r => {
    if (r.pc_id !== pc_id) return;
    const oe = aiParseOdd(r.odd_energy);
    if (!oe || !oe.max) return;
    seen = true; max = Math.max(max, oe.max);
  });
  if (!seen) return 'unknown';
  return max >= SUB_DEN_MIN ? 'on' : 'off';
}
function subBadge(pc_id){
  // ★은퇴·계정없음은 뱃지 자체를 안 그린다(2026-09-23)★ — "?"(모름)도 아니고 완전히 없음.
  //   charTableData 가 이미 걸러져 subState 는 'unknown' 을 주겠지만, 뱃지를 아예 숨기는
  //   것과 "?" 를 보여주는 것은 다른 신호라 여기서 명시적으로 가른다.
  if (isExcludedPc(pc_id)) return '';
  const s = subState(pc_id);
  const a = esc(pc_id || '');
  if (s === 'on')  return `<span class="sub-badge sub-on" title="구독 O — ${a} · 오드에너지 분모 840 (던전 한 판 80 = 2배 효율, 거래소·원격창고 사용 가능)">✓</span>`;
  if (s === 'off') return `<span class="sub-badge sub-off" title="★구독 X — ${a}★ · 오드에너지 분모 560 (던전 한 판 40, 거래소 판매 불가, 원격창고 불가)">✕</span>`;
  return `<span class="sub-badge sub-unk" title="구독 판정 불가 — ${a} 의 오드에너지를 아직 못 읽었습니다(정보수집을 돌리면 채워집니다). ★모름을 구독 X 로 읽지 않습니다★">?</span>`;
}
function isCorridorDone(pc_id){
  // 어비스 회랑 완료 = 매크로가 보고한 '남은 캐릭 수'가 0 (적 진영 제외 기준, 수·토 22시 리셋).
  // corridorRemaining은 /corridor/progress + WS로 채워진다. 보고가 없으면 뱃지 없음.
  // ★stale 이면 그 0 은 ★지난 판★ 의 0 이다 — 뱃지를 달면 2026-08-05 사고 재발.
  //   (서버가 만료본도 보내기 시작했으므로 여기서 명시적으로 막는다 — 2026-08-24)
  // ★은퇴·계정없음은 옛 스냅샷이 영영 안 지워진다(2026-09-23)★ — corridorRemaining 은
  //   pc_id 키의 클라이언트 캐시라 그 PC 가 다시는 안 돌아도 마지막 값이 그대로 남는다.
  if (isExcludedPc(pc_id)) return false;
  const v = corridorRemaining[pc_id];
  return !!v && !v.stale && typeof v.remaining === 'number' && v.remaining === 0;
}
function isDungeonDone(pc){
  // 일일던전(계정 티켓 14장) 소진 — 매크로가 소진 시각(dungeon_done_at)을 보고.
  // 각성전과 같은 주간 리셋(수요일 05시) 경계 이후 기록만 인정 → 경계 지나면 자연 소멸.
  const t=(pc.dungeon_done_at||'').replace('T',' ');
  return !!t && t>=fmtKstTs(lastWeeklyReset());   // ★B-JS11★ dungeon_done_at = 원격컴 로컬(KST) time.strftime
}

// ★★'오늘 끝냈나' 판정은 여기 한 곳만 쓴다 (2026-08-20 PC-12 실측)★★
//   daily_progress 의 completed 플래그는 늙지 않는다 — 며칠째 안 뜬 계정 카드는
//   옛 완주를 그대로 달고 있다(실측: 계정2 카드 6장이 08-18~19 완주를 오늘로 표시).
//   서버가 붙여주는 today 플래그(completed_time 이 오늘 게임일인가)를 함께 본다.
//   ★한 곳으로 모으는 이유★ 카드 줄·전광판·현재슬롯 판정이 제각각이면 화면 안에서
//   숫자가 서로 안 맞고, 그러면 주인님이 어느 것도 못 믿게 된다.
const dpDone = c => !!(c && c.completed) && c.today !== false;
// ★CDP 뱃지 (2026-08-20 사고 98)★ — find_host·계정전환·자동순환이 전부 CDP 전제다.
//   CDP 없는 PC 에 그 명령을 쏘면 4~5분 낭비하고 "크롬(CDP)이 안 잡힌다" 로 끝난다.
//   매크로가 30초마다 스스로 보고(cdp/cdp_at)하므로, ★명령을 쏘기 전에 여기서 보고 거른다.★
//   계정이 하나뿐인 PC 는 전환할 게 없으니 표시하지 않는다(잡음 제거).
// ★★info.txt 이름 ↔ 수집값 불일치를 보이게 한다 (2026-08-20 주인님 지적)★★
//   주인님: "10번 계정1 info에 캐릭터명 다시적엇는데 뭐가 덧씌어졋는지 대시보드카드에는 안바뀌네"
//   ★카드 이름은 info.txt 가 아니라 char_info(정보수집 OCR)가 이긴다★ (_build_full_state).
//   info.txt 의 acct_names 는 ★계정 순환 판정용★ 이라 카드 표시에 안 쓴다.
//   그래서 info.txt 를 아무리 고쳐도 카드는 안 바뀌고, 사람은 "왜 안 바뀌지" 로 헤맨다.
//   실측 PC-10: info.txt=[미르S2,찬솔S2] / 카드=[폭딜,케피,마리드] — 옛 수집값이 남아 있었다.
//   → 둘이 다르면 ★말해준다.★ 고치는 법(정보수집 재실행)까지 툴팁에 적는다.
const nameMismatch = pc => {
  const n = acctNoOfSuf(String(pc.pc_id||'').slice(-1)) || 1;
  const info = Object.values((pc.acct_names||{})[String(n)] || {}).filter(Boolean);
  const card = (pc.chars||[]).filter(Boolean);
  if (!info.length || !card.length) return '';
  const a = info.slice().sort().join('|'), b = card.slice().sort().join('|');
  if (a === b) return '';
  return ` · <span class="text-orange-400" title="info.txt 에 적힌 이름: ${esc(info.join(', '))}
카드에 보이는 이름(정보수집 OCR): ${esc(card.join(', '))}

카드는 수집값이 이깁니다. info.txt 를 고쳐도 안 바뀝니다 — 정보수집을 다시 돌리십시오.">이름≠</span>`;
};
// ★파섹 네이티브가 안 깔린 PC 를 표시한다★ (사고 326, 2026-08-29)
//   웹 파섹이 안 붙는 PC(15·16·17·21)에 ★네이티브 폴백★ 을 넣는데,
//   ★네이티브가 없으면 폴백 자체가 불가★ 하다 — 사람이 설치해야 한다.
//   ★있으면 아무것도 안 띄운다★ — 정상이 대부분이라 배지를 달면 노이즈만 된다.
//   보고가 아예 없으면(옛 매크로) 조용히 넘어간다 — ★모르는 것을 「없다」로 쓰지 않는다.★
const nativeMark = pc => {
  if (!('parsec_native' in (pc || {}))) return '';      // 옛 매크로 = 판정 불가
  if (String(pc.parsec_native || '').trim()) return ''; // 깔려 있다 = 조용히
  return ` · <span class="text-rose-400" title="파섹 네이티브 클라이언트가 없습니다 — 웹 파섹이 안 붙는 PC 라면 되살릴 방법이 없습니다(사람이 Parsec 을 설치해야 합니다)">파섹✕</span>`;
};

const cdpMark = pc => {
  const multi = Object.values(pc.acct_ids || {}).filter(v => String(v||'').trim()).length > 1;
  if (!multi) return '';
  if (pc.cdp === true)  return ` · <span class="text-emerald-500" title="크롬 CDP 붙음 — 계정전환·find_host·순환 가능">CDP</span>`;
  if (pc.cdp === false) return ` · <span class="text-amber-500" title="크롬 CDP 없음 — 계정전환·find_host·순환 불가 (chrome_cdp 명령 필요)">CDP✕</span>`;
  return ` · <span class="text-gray-600" title="CDP 보고 없음 — 매크로가 옛 버전(1.1.571 이하)이거나 죽어 있음">CDP?</span>`;
};

// ★표식 줄★ — 수집시각·CDP·파섹·이름불일치. cdpMark 류는 자기가 앞에 ' · ' 를 붙이므로
//   맨 앞에 남는 구분자를 여기서 한 번만 걷어낸다(「오늘 완료」를 지우면서 생긴 자리).
function dpMarks(pc) {
  const head = (pc._char_collected_at
      ? `<span class="text-cyan-600">수집 ${relSpan(pc._char_collected_at)}</span>` : '')
    + cdpMark(pc) + nativeMark(pc) + nameMismatch(pc);
  return head.replace(/^\s*\u00b7\s*/, '');
}

// ─── 오늘 진행 현황 ──────────────────────────────────────────────────────────
function buildDailyProgress(dp, activeSlot, charNames, pc) {
  if (!dp || !dp.length) return '';
  // ★오늘 완료만 센다★ — 서버가 붙인 today 플래그(=completed_time 이 오늘 게임일)를 쓴다.
  //   플래그가 없는 옛 응답이면 예전처럼 completed 만 본다(today!==false).
  // ★「오늘 완료 N/M」·「🔁 순환」을 지웠다 (2026-08-29 주인님)★
  //   완료는 아래 슬롯 칸(✓ 초록)과 계정 탭의 ✓ 가 이미 말하고,
  //   순환은 상태 옆 칩이 이미 말한다 — 같은 말을 세 번 하면 아무것도 안 읽힌다.
  const total = dp.length;
  const zeroSlots = surfaceZeroSlots(pc);   // ★사고 610★ 표층 시간 0 인 슬롯(★리셋 이후 값만★ 2026-09-23)
  const slots = dp.map(c => {
    const done = dpDone(c);
    const sZero = zeroSlots.has(Number(c.slot));        // ★사고 610★
    const isActive = !done && c.slot === activeSlot;
    // char_info OCR 이름 우선, 없으면 daily_progress 이름, 없으면 슬롯 번호
    const name = (charNames && charNames[c.slot-1]) || c.name || `${c.slot}`;
    const short = name.length > 3 ? name.slice(0,3) : name;
    const time = String(c.completed_time||'').slice(11,16);
    const cls = done
      ? 'bg-green-900/70 border-green-700 text-green-400'
      : isActive
        ? 'bg-yellow-900/70 border-yellow-600 text-yellow-300'
        : 'bg-gray-800/60 border-gray-700 text-gray-600';
    const icon = done ? '✓' : isActive ? '▶' : String(c.slot);
    const classLabel = isActive && pc.map ? (CLASS_LABEL[pc.map]||'') : '';
    return `<div class="flex flex-col items-center ${cls} border rounded-md px-1 py-0.5 text-center cursor-default"
      style="min-width:0" title="${escAttr(name)}${done?' ✓ '+escAttr(time):isActive?' 진행 중':''}${sZero?' · 표층 시간 0 (00:00:00)':''}">
      <span class="font-bold text-xs leading-none">${esc(icon)}</span>
      <span style="font-size:9px;line-height:1.2;max-width:100%;overflow:hidden;white-space:nowrap${sZero?';color:#f87171;font-weight:700':''}">${esc(short)}</span>
      ${classLabel?`<span style="font-size:8px;line-height:1;color:#9ca3af">${classLabel}</span>`:''}
    </div>`;
  }).join('');
  return `<div class="mt-2 pt-2 border-t border-gray-800/60">
    <div class="flex items-center justify-between mb-1">
      <span class="text-gray-400" style="font-size:10px">${dpMarks(pc)}</span>
      ${pc._total_kina?`<span class="text-yellow-400 font-semibold whitespace-nowrap" style="font-size:12px">창고키나 ${fmtKinaShort(pc._total_kina)}</span>`:''}
    </div>
    <div class="grid gap-1" style="grid-template-columns:repeat(${total},minmax(0,1fr))">${slots}</div>
  </div>`;
}

// ─── 카드 렌더링 ──────────────────────────────────────────────────────────────
// ★멀티계정(v1.1.412): 계정 칩★ — pc_id가 b/c/d로 끝나면 '계정 B' 칩을 붙여 같은 PC의
//   부계정임을 표시. 카드는 pc_id로 정렬돼 PC-03/PC-03b/PC-03c가 자연히 이웃하므로,
//   칩으로 "이 카드는 PC-03의 두 번째 계정"이 한눈에 읽힌다. 본계정(숫자로 끝)은 칩 없음.
// ★계정 표기는 사용자에게 1/2/3/4 (2026-08-15 사용자 지시: "abcd 하지 말고 1,2,3,4로")★
//   내부 프로토콜(account.txt·pc_id 접미사·switch_account args)은 a/b/c/d 그대로 —
//   함대 코드·기존 카드와의 호환을 위해 표기만 숫자로 바꾼다. 변환은 이 두 함수로만.
function acctNum(label){
  const l = String(label||'');
  const i = (l.length === 1) ? ACCT_LABELS.indexOf(l) : -1;
  return i < 0 ? '?' : i + 1;
}
function normAcct(input){
  const v = (input||'').trim().toLowerCase();
  let m = null;
  if (v.length === 1) {
    if (ACCT_LABELS.includes(v)) m = v;
    else if (v >= '1' && v <= '9' && +v <= MAX_ACCT) m = ACCT_LABELS[+v - 1];
  }
  if(!m) { if(v) alert('1~' + MAX_ACCT + ' 중 하나만 됩니다'); return null; }
  return m;
}
// 스프레드 그룹 헤더용 계정 태그 — "몇번 계정 · 어떤 아이디"(2026-08-15 사용자 지시).
// 부계정 카드(접미사) 또는 acct 필드 보고가 있는 카드만 표시. 없으면 기존 화면 불변.
function acctTagSpread(pcid){
  const st = state[pcid] || {};
  const isSub = isAcctSuf((pcid||'').slice(-1));
  // 본계정도 멀티계정 PC면 '계정 1' 표시 (2026-08-15 사용자: "계정1은 안 나온다")
  if (!isSub && !st.acct_id && !isMultiAcct(pcid)) return '';
  const n = acctNumOf(pcid);
  // 자기 카드에 없으면 전 계정 지도에서 — 접속 안 한 계정도 아이디가 나온다
  // ★아이디는 여기서 안 붙인다 (2026-08-18 사용자 지시: '서버 돈 아이디 순')★
  //   순서를 바꾸려면 조각이 나뉘어 있어야 한다 — 태그는 '계정 N' 만 책임진다.
  //
  // ★★'계정 N' 글자 → ★색깔 동그라미 숫자★ (2026-08-22 주인님 지시)★★
  //   원문: "PC-09 계정1 계정2 이렇게 나와있는데 이거 존나 보기힘들어 …
  //          1 에 동그라미 쳐져잇는거 그거 색깔 해가지고 좀크게 보일수잇게 바꿔"
  //   ★왜 안 보였나★ 10~12px 보라 글씨 하나라 PC 이름·서버·키나·아이디 사이에 묻혔다.
  //   스프레드는 ★PC 하나에 계정 줄이 여러 개★ 쌓이는 화면이라, 지금 보는 줄이 몇 번
  //   계정인지가 제일 먼저 읽혀야 한다. 크기(26px)·굵기·색으로 분리한다.
  //   색은 카드 칩(acctChip)과 ★같은 규약★ 을 쓴다 — 두 화면에서 계정1 이 다른 색이면
  //   그게 더 헷갈린다: 1=초록 2=보라 3=청록 4=주황 5=장미.
  //   ★MAX_ACCT 를 올리면 이 맵과 acctChip 의 색 맵을 ★둘 다★ 늘려야 한다★
  //   (2026-08-24 적대 리뷰: 계정5 를 추가하면서 acctChip 만 늘리고 여기를 빠뜨렸다)
  //   ★Tailwind 클래스 대신 인라인 스타일★ — 퍼지(purge)로 색 클래스가 빠져도
  //   이 뱃지는 반드시 보여야 한다(안 보이면 이 수정의 목적 자체가 사라진다).
  const AC = {
    1: {fg:'#6ee7b7', bg:'rgba(16,185,129,.20)', bd:'#34d399'},   // 초록
    2: {fg:'#c4b5fd', bg:'rgba(139,92,246,.20)', bd:'#a78bfa'},   // 보라
    3: {fg:'#5eead4', bg:'rgba(20,184,166,.20)', bd:'#2dd4bf'},   // 청록
    4: {fg:'#fcd34d', bg:'rgba(245,158,11,.20)', bd:'#fbbf24'},   // 주황
    5: {fg:'#fda4af', bg:'rgba(244,63,94,.20)',   bd:'#fb7185'},   // 장미 (2026-08-24 사고 193)
  }[n] || {fg:'#d1d5db', bg:'rgba(107,114,128,.20)', bd:'#9ca3af'};
  return `<span title="계정 ${n}" style="display:inline-flex;align-items:center;justify-content:center;`
       + `width:26px;height:26px;border-radius:9999px;border:2px solid ${AC.bd};`
       + `background:${AC.bg};color:${AC.fg};font-size:15px;font-weight:800;line-height:1;`
       + `flex:none;">${n}</span>`;
}
// 계정 아이디만 따로 — 스프레드 헤더에서 ★서버·돈 다음★에 놓는다 (2026-08-18 사용자 지시)
function acctIdTag(pcid){
  const st = state[pcid] || {};
  const id = st.acct_id || groupAcctMaps(baseId(pcid)).ids[acctNumOf(pcid)] || '';
  return id ? ` <span class="text-purple-300 text-xs font-normal ml-1">${esc(id)}</span>` : '';
}
// ★전 계정 지도(v1.1.424)★ — 살아있는 매크로가 info.txt의 전 계정 아이디/서버를 통째로
// 보고(acct_ids/acct_servers, 키="1".."4")하므로, 접속한 적 없는 계정 카드도 표기 가능
// (사용자: "계정1에 아이디가 안 나오네 / 계정2 서버를 못 읽는 것 같네").
function groupAcctMaps(base){
  // ★합집합 금지(2026-09-22, 주인님 「12번 3·4 아직도 있다」)★ — 예전엔 형제 카드
  //   전부의 acct_ids 를 Object.assign 으로 ★합쳤다★. info.txt 에서 계정을 지워도
  //   ★옛 카드의 스냅샷★(아직 안 갱신된 형제)이 지워진 번호를 계속 들고 있어서
  //   합집합에 그 번호가 되살아났다(실측: PC-12 07:26 스냅샷=1~4, PC-12b 10:57=1~2인데
  //   합치면 여전히 1~4). ★가장 최근에 보고한 카드 하나★ 의 지도만 쓴다 — 서버
  //   _build_full_state_inner 2차 패스(현역 선택)와 같은 규칙(last_active 최신).
  let latest = null;
  Object.values(state).forEach(p=>{
    if (baseId(p.pc_id||'') !== base) return;
    if (!latest || String(p.last_active||'') > String(latest.last_active||'')) latest = p;
  });
  return {
    ids:     (latest && latest.acct_ids) || {},
    servers: (latest && latest.acct_servers) || {},
    plats:   (latest && latest.acct_platforms) || {},   // ★카드 계정줄의 '구글' 표기용 (2026-08-21)★
  };
}
// ★플랫폼이 구글이면 카드에 아이디 대신 '구글' 을 적는다 (2026-08-21 주인님 지시)★
//   원문: "각 카드에 맨밑에 계정1 해서 아이디 나와있는 플랫폼이 구글인 경우에는
//          아이디말고 구글 이라고 적어둬"
//   구글 계정 PC(PC-07 · PC-14 · PC-17)는 지뢰 C1 대로 CDP 로그인이 구조적으로 안 된다.
//   카드에서 한눈에 구분돼야 '왜 이 PC 만 전환이 안 되지' 를 매번 다시 파지 않는다.
//   info.txt 의 플랫폼 표기가 '구글' / 'google' / 'Google 계정' 등으로 흔들리므로
//   ★부분일치 + 대소문자 무시★ 로 본다.
function isGooglePlat(v){
  return /구글|google/i.test(String(v||''));
}
// ★★카드 계정줄을 '아이디' 가 아니라 ★플랫폼★ 으로 적는다 (2026-08-21 주인님 지시)★★
//   원문: "대시보드 카드 밑에 계정해서 계정 나오는거 플램폼으로 표시하는게 나을거같다
//          NC 면 NC, 전화번호면 전화번호, 구글이면 구글"
//   ★왜 나은가★ 아이디는 길고(`selp9qsw539mh8r@naver.com`) 잘려서 식별이 안 되는데,
//   정작 운영에서 필요한 정보는 ★어떤 방식으로 로그인하는 계정인가★ 다.
//   구글이면 CDP 로그인에 세션 이관이 필요하고(§C1), 전화번호면 또 다르다.
//   실측 값(2026-08-21 함대 23대): 'NC' · '전화번호' · '구글' 세 가지.
//   아이디는 title 툴팁에 그대로 남겨 마우스만 올리면 확인된다.
const PLAT_STYLE = [
  [/구글|google/i,            '구글',    'text-amber-300/90'],
  [/전화|폰|phone|mobile/i,   '전화번호', 'text-emerald-300/90'],
  [/^\s*NC\s*$|엔씨|플레이엔씨|plaync/i, 'NC', 'text-sky-300/90'],
];
function platLabel(v){
  const t = String(v||'').trim();
  if (!t) return null;
  for (const [re, label, cls] of PLAT_STYLE) if (re.test(t)) return {label, cls};
  return {label: t, cls: 'text-gray-300/90'};   // 모르는 값도 그대로 보여준다(숨기지 않는다)
}
function acctNumOf(pcid){
  const c = (pcid||'').slice(-1);
  // ★B-JS3 (2026-09-23)★ acct_num 은 매크로 보고값 — ★정수로만★ 돌려준다. 이 값이 acctRow·acctTagSpread·
  //   카드 메뉴 머리에 HTML 로 박힌다(문자열이면 그대로 주입). 0·음수·못 읽음 = 1(예전 `|| 1` 과 같은 뜻).
  const n = parseInt((state[pcid]||{}).acct_num, 10);
  return acctNoOfSuf(c) || (n > 0 ? n : 1);
}
// 이 PC가 멀티계정인가 — 형제 계정 카드가 있거나(접미사 카드 존재) 매크로가 acct_total>1 보고.
function isMultiAcct(pid){
  const b = baseId(pid||'');
  if (((state[pid]||{}).acct_total || 0) > 1) return true;
  if (isAcctSuf((pid||'').slice(-1))) return true;
  return ACCT_SUFFIX.split('').some(s => state[b+s]);
}
// ★★acctChip() 은 2026-08-29 에 지웠다 (호출처 0)★★
//   주인님: "2번째줄에 계정3 이건 없애도될거같아 카드위에서 말해주고잇으니까"
//   — 지금 계정은 ①카드 위 ★계정 탭★ 과 ②카드 맨 아래 ★🔑 계정 N★ 줄이 말한다.
//   되살릴 일이 생기면 색 규약은 acctTagSpread() 의 동그라미 숫자와 맞출 것.
// ★★순환 단계 칩 — '전환중' 이 한눈에 보이게 (2026-08-21 주인님 요청)★★
//   원문: "그리고 대시보드에 전환중이라는 표시도 보여야할거같아"
//   예전엔 카드 맨 아래 10px 회색으로 `🔁 switching` 만 찍혀서 ①영어고 ②안 보였다.
//   순환이 실제로 뭘 하는 중인지가 안 보이면 "왜 안 넘어가지" 를 아무도 진단 못 한다.
//   hunting 은 평상시라 조용히, 나머지 3단계는 ★상태 이름 옆에 크게★ 띄운다.
const ROT_CHIP = {
  collecting: {t:'📋 정보수집중',  c:'bg-cyan-800/80 text-cyan-100 border-cyan-500',    p:true},
  switching : {t:'🔄 계정 전환중', c:'bg-amber-700/85 text-amber-100 border-amber-400', p:true},
  starting  : {t:'▶ 사냥 시작중',  c:'bg-green-800/80 text-green-100 border-green-500', p:true},
  hunting   : {t:'🔁 순환',        c:'bg-purple-900/60 text-purple-300 border-purple-700', p:false},
  // ★작업 순환 (2026-08-23)★ — 무슨 작업인지는 pc._rot_task 로 뒤에 붙는다
  tasking   : {t:'🔁 순환',        c:'bg-sky-800/85 text-sky-100 border-sky-400',       p:true},
};
function rotChip(pc) {
  const r = ROT_CHIP[pc._rot];
  if (!r) return '';
  const tgt = pc._rot_target ? ` → 계정${pc._rot_target}` : '';
  const tk  = pc._rot_task ? ` ${pc._rot_task}` : '';
  const title = pc._rot === 'switching'
      ? `계정 자동순환: 지금 계정을 바꾸는 중입니다${tgt}. 본컴 런처 → 원격컴 크롬 → 매크로 재시작 순서로 진행됩니다`
      : (pc._rot === 'tasking'    ? `전 계정 순환${tk}: 이 계정에서 작업이 끝나기를 기다리는 중입니다. 끝나면 다음 계정으로 전환합니다`
      : (pc._rot === 'collecting' ? '계정 자동순환: 완주를 감지해 캐릭터 정보를 수집하는 중입니다'
      : (pc._rot === 'starting'   ? '계정 자동순환: 전환이 끝나 사냥을 시작하는 중입니다'
      : '계정 자동순환 무장됨 — 완주하면 정보수집 후 다음 계정으로 넘어갑니다')));
  return `<span class="ml-1.5 shrink-0 px-1.5 py-0.5 rounded border text-xs font-bold leading-none ${r.c}${r.p?' pulse':''}"
                title="${title}">${r.t}${esc(tk)}${esc(tgt)}</span>`;
}
// ★★사고 391 — ★순회를 걸었는데 메인 카드에 안 보인다★★ (주인님 2026-09-01)
//   원문: 「아직도 대시보드에 순회라고안뜨는데?」
//   ★사고 380 에서 배지를 넣긴 했는데 ★사냥 탭(6689행) 한 곳에만★ 넣었다.★
//   주인님이 늘 보시는 ★메인 카드★ 에는 안 붙어서 「안 걸렸다」로 보였다.
//   /status 는 카드마다 tour("1/2 collect") · tour_seq("1→2") 를 이미 싣고 온다.
//   ★rotChip 과 다른 것이다★ — rotChip 은 `_rot`(완주 감지 자동순환)이고
//   이건 사람이/내가 명시적으로 건 `acct_tour` 다. 둘은 같이 떠도 된다.
//   ★값이 없으면 아예 안 그린다★ — 「안 걸림」과 「옛 매크로라 못 보냄」을 뭉개지 않는다.
function tourChip(pc) {
  // ★사고 391-b★ 순회 값은 ★그 순회를 시작한 계정 카드★ 에 실려 온다.
  //   전환이 끝나면 앞에 보이는 카드는 ★다른 계정★ 이라 자기 tour 가 비어 있고,
  //   그러면 「순회 안 걸림」으로 보인다(PC-01 실측: PC-01 에 '2/2 collect',
  //   화면에 보이는 PC-01b 는 ''). 사냥 탭(6502행)은 이미 스택 전체를 뒤진다 —
  //   ★같은 것을 두 곳이 다르게 보면 안 된다★(§A8-5). 여기도 형제까지 본다.
  let t = pc.tour, seqv = pc.tour_seq;
  if (!t) {
    for (const id of stackIds(pc.pc_id || '')) {
      const sib = state[id];
      if (sib && sib.tour) { t = sib.tour; seqv = sib.tour_seq; break; }
    }
  }
  if (!t) return '';
  const seq = seqv ? ` (${seqv})` : '';
  return `<span class="ml-1.5 shrink-0 px-1.5 py-0.5 rounded border text-xs font-bold leading-none"
                style="background:rgba(129,140,248,.2);color:#c7d2fe;border-color:#818cf8"
                title="${esc('전 계정 순회 진행도: ' + t + seq + ' — 이 계정 작업이 끝나면 다음 계정으로 전환합니다')}">🔁 ${esc(t)}</span>`;
}
// ★★사고 392 — ★몇 단계까지 왔는지★★ (주인님 2026-09-01)
//   「계정 전환」이라고만 떠도 3~5분 내내 같은 글자라 진행이 안 보인다.
//   매크로 Trace._row 가 단계마다 switch_step("09 계정 칩 클릭") 을 실어 보낸다.
//   ★계정전환 중일 때만 그린다★ — 전환이 끝나면 마지막 단계가 카드에 남아
//   「지금 그 단계다」로 읽히기 때문이다(값이 남아 있어도 안 그린다).
function switchStepChip(pc) {
  if ((pc.status || '') !== 'acct_switching') return '';
  const s = pc.switch_step;
  if (!s) return '';
  const bad = (pc.switch_mark === '✘');
  return `<span class="ml-1.5 shrink-0 px-1.5 py-0.5 rounded border text-xs font-bold leading-none"
                style="background:${bad ? 'rgba(244,63,94,.2)' : 'rgba(167,139,250,.18)'};
                       color:${bad ? '#fda4af' : '#ddd6fe'};
                       border-color:${bad ? '#f43f5e' : '#a78bfa'}"
                title="${esc('계정전환 진행 단계 — 총 15단계 (본컴 런처 → 원격컴 크롬 → 매크로 재시작)')}"
          >${esc(pc.switch_mark || '')} ${esc(s)}</span>`;
}
// ★카드 「💰 어비스」 줄 (2026-09-23 주인님 장부 #104)★ — 매크로 1.1.1004 숫자 칸(abyss_kina_state/gain/
//   rate/since/mins)으로 그린다. 옛 매크로(숫자 칸 없음)는 글자 칸 abyss_kina 를 ★예전 그대로★.
//   빨강은 「시간당 < abyss_red_rate」 이고 ★abyss_min_mins 분 넘게 잰 뒤★ 에만 — 그 전엔 튀니까 중립색.
//   문턱은 서버 설정(/summary 의 abyss.red_rate·min_mins) — renderAbyssTiles 가 serverSum() 에서 받아
//   ABYSS_TH 에 남긴다(설정값이라 요약이 낡아도 그대로 유효). 아직 못 받았으면 서버 기본값과 같은 수.
const ABYSS_TH_DEFAULT = {red_rate: 1000000, min_mins: 5};
let ABYSS_TH = null;
function abyssTh(){ return ABYSS_TH || ABYSS_TH_DEFAULT; }
function abyssCardLine(pc){
  const st = pc && pc.abyss_kina_state;
  const box = 'mt-1.5 text-xs rounded px-2 py-0.5 truncate border ';
  // 카드가 좁아 줄이 잘려도(truncate) 호버하면 전부 보이게 — 글자는 숫자·시각뿐이라 본문은 그대로, 툴팁만 escAttr
  const line = (cls, body) => `<div class="${box}${cls}" title="${escAttr(body)} — 어비스(Delete) 세션: 이번 구간(시작 시각부터) 번 키나와 시간당. 다음 세션 시작까지 유지">💰 어비스 ${body}</div>`;
  if (st === 'ok' || st === 'waiting') {
    // ★음수·NaN·문자는 0 으로(글자에 「+-5」·「NaN」 이 안 나가게, 2026-09-23 적대 검증)★
    const nn = v => { const n = Number(v); return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0; };
    const since = nn(pc.abyss_kina_since);
    // ★R7 — 시각은 KST(fmtKstTs)★ 매크로 글자 「(HH:MM부터)」·다른 KST 화면과 같게. 보는 기기(베트남 등) 시간대가 아니다.
    const hm = (since > 0 && since < 1e11) ? fmtKstTs(new Date(since * 1000)).slice(11, 16) : '?';
    const th = abyssTh(), mins = nn(pc.abyss_kina_mins), gain = nn(pc.abyss_kina_gain);
    // ★R5 — 매크로는 10분까지 waiting(lc/loot.py ABYSS_KINA_OK_MINS=10), 주인님 문턱은 min_mins(5)★
    //   waiting 이어도 gain·mins 가 실려 있고 mins ≥ 문턱이면 gain/mins 로 시간당을 재서 빨강/중립(서버 _abyss_billboard 와 같은 식).
    const hasNum = pc.abyss_kina_gain != null && pc.abyss_kina_gain !== '' && Number.isFinite(Number(pc.abyss_kina_gain)) && mins > 0;
    if (st === 'waiting' && !(hasNum && mins >= th.min_mins))
      return line('text-gray-400 bg-gray-800/40 border-gray-700', `측정 중 (${hm}부터)`);
    const rate = st === 'ok' ? nn(pc.abyss_kina_rate) : Math.floor(gain * 60 / mins);
    const txt = `+${fmtKinaKor(gain)} · 시간당 ${fmtKinaKor(rate)} (${hm}부터)`;
    if (mins < th.min_mins)
      return line('text-gray-300 bg-gray-800/60 border-gray-700', `${txt} (측정 ${mins}분)`);
    const cls = rate < th.red_rate ? 'text-red-300 bg-red-950/40 border-red-800' : 'text-amber-300 bg-amber-900/20 border-amber-800/40';
    return line(cls, txt);
  }
  // ★옛 매크로(숫자 칸 없음, 1.1.1003) 글자 칸 (2026-09-24 주인님 #159 «100만 이하 빨간 글씨 빠졌다 · 17번 − 돈 뜬다»)★
  //   ① 음수(「+-6,349,199 키나 · 시간당 -12,622,269」 — 매크로 판독 오류)는 숫자를 ★화면에 안 낸다★ → 회색 한 줄.
  //   ② 「+X 키나 · 시간당 Y (HH:MM~/부터)」 는 숫자 칸과 같은 규칙 — 문턱 분(min_mins) 전엔 중립, 뒤엔 Y < red_rate 빨강.
  //      잰 분은 KST 지금 − HH:MM(자정 넘김은 +24시간, 기기 시계가 1~2분 빨라 미래로 보이면 0분).
  //   ③ 못 읽는 모양은 예전 그대로(글자 esc).
  const lg = pc && pc.abyss_kina;
  if (!lg) return '';
  const ls = String(lg);
  if (/(^|[^\d,])-\s*\d/.test(ls))
    return line('text-gray-400 bg-gray-800/40 border-gray-700', '판독 오류 — 재측정 대기');
  const lm = ls.match(/^\+([\d,]+) 키나 · 시간당 ([\d,]+) \((\d{1,2}):(\d{2})(?:부터|~)/);
  if (lm) {
    const gain = Number(lm[1].replace(/,/g, '')), rate = Number(lm[2].replace(/,/g, ''));
    const nw = fmtKstTs(new Date()).slice(11, 16).split(':').map(Number);
    let mins = (nw[0] * 60 + nw[1]) - (Number(lm[3]) * 60 + Number(lm[4]));
    if (mins < 0) mins += 1440;
    if (mins > 1437) mins = 0;
    const th = abyssTh(), hm = lm[3].padStart(2, '0') + ':' + lm[4];
    const txt = `+${fmtKinaKor(gain)} · 시간당 ${fmtKinaKor(rate)} (${hm}부터)`;
    if (mins < th.min_mins)
      return line('text-gray-300 bg-gray-800/60 border-gray-700', `${txt} (측정 ${mins}분)`);
    return line(rate < th.red_rate ? 'text-red-300 bg-red-950/40 border-red-800' : 'text-amber-300 bg-amber-900/20 border-amber-800/40', txt);
  }
  return `<div class="mt-1.5 text-xs text-amber-300 bg-amber-900/20 border border-amber-800/40 rounded px-2 py-0.5 truncate" title="어비스(Delete) 세션 키나 정산 — 켤 때/끌 때 보유 키나 차액. 다음 세션 시작까지 유지">💰 어비스 ${esc(ls)}</div>`;
}

function buildCard(pc) {
  const st = pc.status||'offline';
  const cfg = STATUS_CFG[st]||STATUS_CFG.offline;
  const pulse = (st==='hunting'||st==='selling'||st==='abyss'||st==='awakening_wait')?' pulse':'';   // 각성전 대기 = 깜빡여서 눈에 띄게
  // ★묶음 중 하나라도 골라져 있으면 ✔(2026-09-23 B-JS8)★ — 고른 뒤 새 계정 id 가 앞장이 되면
  //   명령은 그 PC 로 가는데 카드엔 ✔ 가 없었다. 선택은 묶음(stackIds) 단위다.
  const sel = stackIds(pc.pc_id).some(id => selectedPcs.has(id)) ? ' card-sel' : '';
  const errHtml = (pc.errors||[]).slice(0,3).map(e=>
    `<div class="text-xs text-red-400 bg-red-900/30 rounded px-2 py-0.5">⚠ ${esc(e)}</div>`).join('');
  const bugBadge = (pc._bug_count||0)>0
    ? `<span class="px-1.5 py-0.5 bg-red-700/80 text-red-200 rounded text-xs font-bold leading-none cursor-pointer" onclick="event.stopPropagation();openBugsModal('${pc.pc_id}')">🐛 ${pc._bug_count}</span>`
    : '';
  // ★낡은 보고는 색칠하지 않는다 (2026-08-20 PC-23)★ — 업데이터가 마지막으로 전송에
  //   성공한 값을 신선도 검사 없이 초록으로 칠하는 바람에, 14.6시간 죽은 PC 가
  //   "업데이터 running" 으로 멀쩡해 보였다. 서버 OFFLINE_TIMEOUT 이 90초이므로
  //   그 3배(270초)를 넘으면 회색 + '(N분전)' 로 강등한다.
  const _uage = (typeof pc._updater_age_s === 'number') ? pc._updater_age_s : null;
  const _ustale = (_uage !== null && _uage > 270);
  const ucls = _ustale ? 'text-gray-600 line-through'
    : ({'running':'text-green-400','stopped':'text-gray-500','updating':'text-cyan-400','crashed':'text-red-400'}[pc._updater_state]||'text-gray-600');
  const uageTxt = _ustale ? `<span class="text-amber-600" title="업데이터 보고가 ${Math.floor(_uage/60)}분째 없음 — 화면의 상태는 그때 값입니다">(${Math.floor(_uage/60)}분전)</span>` : '';
  const mvcls = (pc.macro_version && latestVersions.macro && pc.macro_version !== latestVersions.macro) ? 'text-red-400' : 'text-gray-700';
  const uvcls = (pc._updater_version && latestVersions.updater && pc._updater_version !== latestVersions.updater) ? 'text-red-400' : 'text-gray-700';
  const macroVer = pc.macro_version ? `<span class="${mvcls}">매크로 v${esc(pc.macro_version)}</span>` : '';
  // ★업데이트/재시작 결과만(2026-09-22)★ — 버전이 바뀌면 ✓, 3분 안 안 바뀌면 ✗. 설명은 hover 로만.
  const _ur = UPD_RESULT[baseId(pc.pc_id||'')];
  const updResultTxt = (_ur && _ur.result)
    ? (_ur.result === 'ok'
        ? `<span class="text-green-400" title="${_ur.cmd==='restart'?'매크로 재기동 확인(가동시간 줄어듦)':'버전 바뀜/재기동 확인'}">✓</span>`
        : (_ur.result === 'unknown'
            ? `<span class="text-gray-500" title="3분 안에 판정할 증거 없음(보고 없음) — 로그 확인">?</span>`
            : `<span class="text-red-400" title="${_ur.cmd==='restart'?'3분 동안 가동시간이 안 끊김 — 재기동 안 됨':'3분 안에 버전이 안 바뀜('+esc(_ur.before)+' 그대로)'}">✗</span>`))
    : '';
  const updaterRow = (pc._updater_state&&pc._updater_state!=='unknown')
    ? `<div class="mt-1 flex items-center gap-1 text-gray-600 whitespace-nowrap overflow-hidden" style="font-size:10px">${macroVer}${macroVer?'<span class="text-gray-800">|</span>':''}<span>업데이터</span><span class="${ucls}">${esc(pc._updater_state)}</span>${uageTxt}${pc._updater_version?`<span class="${uvcls}">v${esc(pc._updater_version)}</span>`:''}${updResultTxt}</div>`
    : '';
  const activeSlot = pc.slot||0;
  const isOnline = (STATUS_CFG[st]||STATUS_CFG.offline).online;
  // ★효율 60% 미만은 빨강 (2026-08-29 주인님)★ — ★온라인 카드만★ 칠한다.
  //   꺼진 PC 는 마지막 값이 그대로 박제돼 있어 전부 빨개지면 신호가 노이즈가 된다.
  const _effLow = (typeof pc.efficiency === 'number') && pc.efficiency < 60 && isOnline;
  // ★[슬롯 캐릭이름] 태그는 2026-08-29 에 뺐다 (주인님: "1캐릭터이름 이것도 없애도될거같아")★
  //   지금 도는 캐릭은 아래 진행 칸의 ▶ 가 이미 노랗게 말한다 — 두 번 말하면서
  //   뱃지 줄을 밀어내 🏹⚔🏰🌀 가 잘렸다. 되살릴 거면 ★뱃지 줄이 아닌 곳★ 에 넣을 것.
  // 완료 스탬프(이모지만 — 색이 신호): 🏹초록=오늘 사냥 완료 / ⚔인디고=전 캐릭 각성 0/3 / 🏰보라=일일던전 티켓 소진
  const doneBadges =
    (isHuntDone(pc.daily_progress)?`<span class="done-badge done-hunt" title="오늘 사냥 완료 — 매일 새벽 5시 초기화">🏹</span>`:'') +
    (isAwakenDone(pc.pc_id)?`<span class="done-badge done-awaken" title="각성전 완료 — 전 캐릭 0/3 (수요일 새벽 5시 초기화)">⚔</span>`:'') +
    (isDungeonDone(pc)?`<span class="done-badge done-dungeon" title="일일던전 완료 — 계정 티켓 소진 (수요일 새벽 5시 초기화)">🏰</span>`:'') +
    (isCorridorDone(pc.pc_id)?`<span class="done-badge done-corridor" title="어비스 회랑 완료 — 전 캐릭 남은 회랑 0 (수·토 22시 초기화)">🌀</span>`:'');
  return `<div id="card-${pc.pc_id}"
    class="relative bg-gray-900 rounded-xl p-3 border ${cfg.border} ${cfg.bg}${sel} transition-all group cursor-pointer select-none"
    onclick="toggleSelect('${pc.pc_id}',event)"
    oncontextmenu="openCardMenu('${pc.pc_id}',event);return false">
    <div class="flex items-start justify-between mb-2">
      <div class="flex items-center gap-2 min-w-0">
        <span class="drag-handle shrink-0 cursor-grab active:cursor-grabbing text-gray-700 hover:text-gray-400 select-none" style="font-size:14px;line-height:1" title="드래그로 순서 변경">⠿</span>
        <div class="min-w-0">
          <!-- ★뱃지는 PC명과 같은 줄에 고정(shrink-0). 예전엔 flex-wrap 한 줄에
               캐릭터명 태그까지 같이 넣어서, 정보수집으로 캐릭명이 붙는 순간
               🏹⚔🏰 뱃지가 아래로 밀려났다(사용자 지적). 캐릭명은 아랫줄로 분리.★ -->
          <!-- ★이름 줄 = PC명(절대 안 잘림)+🐛+상태만. 나머지 뱃지류(계정칩·완료뱃지·캐릭명)는
               '한 줄'로 아랫줄에(2026-08-15 사용자 3연속 정정: "이름이 짤린다" → "스크린샷은
               이름 옆" → "이름이 또 짤리고 뱃지가 두 줄"). 칩이 이름 줄에 있으면 긴 상태
               문구와 겹쳐 PC명이 'PC...'로 뭉개졌다.★ -->
          <div class="font-bold text-base flex items-center gap-0">
            <!-- ★접미사(PC-20b) 노출 금지(v1.1.424 사용자: "20b 필요없어 그냥 20 하면 되고")
                 — 계정은 아랫줄 [계정 N] 칩이 말한다. 단일 계정 PC는 원래 접미사 없음.★ -->
            <span class="shrink-0">${esc(baseId(pc.pc_id||'')||'?')}</span>${subBadge(pc.pc_id)}
          </div>
        </div>
      </div>
      <!-- 상태 쪽이 양보한다(min-w-0+truncate) — PC명은 shrink-0라 절대 안 잘림 -->
      <div class="flex items-center gap-1 min-w-0">
        <span class="inline-flex items-center gap-1.5 text-base font-bold ${cfg.text} min-w-0">
          <span class="w-3 h-3 rounded-full ${cfg.badge}${pulse} shrink-0"></span>
          <span class="truncate">${cfg.label}</span>${switchStepChip(pc)}${tourChip(pc)}${rotChip(pc)}${pendChip(pc)}
        </span>
      </div>
    </div>
    <!-- ★★뱃지 줄 — 헤더 ★밖★ 전폭 한 줄 (2026-08-29 주인님)★★
         원문: "스크린샷을 2번째 제일왼쪽으로 내리고 뱃지들나오게 …
                2번째줄에서 오른쪽으로 쌓여가면서 좀 짤리는거같은데"
         ★왜 잘렸나★ 예전 2번째줄은 헤더(flex justify-between) ★안★ 에 있어서
         오른쪽 상태 문구와 폭을 나눠 가졌다. 거기에 [계정N]·[1 캐릭이름] 까지
         얹히니 whitespace-nowrap+overflow-hidden 이 뒤쪽 뱃지를 통째로 잘랐다
         (실측 스샷: 상태가 "사.." 로 뭉개질 만큼 왼쪽이 폭을 먹고 있었다).
         → 헤더 밖 전폭 줄로 빼고, 계정칩·캐릭명은 지웠다(탭·아랫줄이 이미 말한다). -->
    ${(bugBadge||doneBadges)?`<div class="flex items-center gap-1 mb-1 whitespace-nowrap overflow-hidden">${bugBadge}${doneBadges}</div>`:''}
    ${pendBar(pc)}
    <div class="grid grid-cols-2 gap-x-4 gap-y-1 text-sm mt-2">
      <div><span class="pv-k">진행도</span> <span class="pv-v">${pc.hunt_progress!=null ? Math.round(pc.hunt_progress)+' %' : '–'}</span></div>
      <div class="whitespace-nowrap"><span class="pv-k">효율</span> <span class="pv-v${_effLow?' pv-bad':''}"${_effLow?' title="효율 60%/h 미만 (주인님 기준). ★텔레그램 알람은 이것과 다른 문턱★ 이라 아직 안 나갑니다 — 알람은 15%/h 미만이 45분 이어질 때입니다"':''}>${pc.efficiency!=null ? pc.efficiency.toFixed(1)+'%/h' : '–'}</span></div>
      <div class="col-span-2"><span class="pv-k">맵</span> <span class="text-gray-100 font-medium">${esc(pc.map_name||'–')}</span></div>
      <div><span class="pv-k">업타임</span> <span class="text-gray-100 font-medium">${fmtSlotUptime(pc.slot_uptime, pc.slot||0, pc.uptime_hours)}</span></div>
      ${pc.server?`<div><span class="pv-k">서버</span> <span class="text-gray-100 font-medium">${esc(pc.server)}</span></div>`:''}
      <div><span class="pv-k">최근</span> <span class="text-gray-100 font-medium">${relSpan(pc.last_active)}</span></div>
      <div><span class="pv-k">사망(30분)</span> <span class="${(pc.deaths_30m||0)>0?'text-red-400 font-bold':'text-gray-100 font-medium'}">${pc.deaths_30m||0}회</span></div>
    </div>
    ${abyssCardLine(pc)}
    ${errHtml?`<div class="mt-2 space-y-0.5">${errHtml}</div>`:''}
    ${buildDailyProgress(pc.daily_progress, activeSlot, pc.chars, pc)}
    ${updaterRow}
    ${acctRow(pc)}
  </div>`;
}

// ★카드 맨 아래 '몇번 계정 · 어떤 아이디 · 서버' 줄(2026-08-15 사용자 지시)★ — 매크로가
//   상태 payload(acct_num/acct_id/acct_nick/acct_server, v1.1.422+)로 보고한다.
//   구버전 매크로는 필드가 없어 줄 자체가 안 뜬다(레이아웃 불변 = 함대 무해).
function acctRow(pc){
  // 멀티계정 PC면 acct 필드(v1.1.422+ 매크로)가 아직 없어도 '몇번 계정'만이라도 표시
  if (!pc.acct_id && !pc.acct_num && !isMultiAcct(pc.pc_id)) return '';
  const n = acctNumOf(pc.pc_id);
  // 자기 카드에 값이 없으면 전 계정 지도에서(v1.1.424) — 접속 안 한 계정도 채워진다
  const maps = groupAcctMaps(baseId(pc.pc_id));
  const id = pc.acct_id || maps.ids[n] || '';
  const srv = maps.servers[n] || pc.acct_server || '';
  const plat = maps.plats[n] || '';
  const pl   = platLabel(plat);
  // ★★NC 는 아이디를 다시 보여준다 (2026-08-23 주인님 지시)★★
  //   원문: "계정1 NC 해가지고 아래 나오는곳에 NC는 아이디 다시 나오게 해놔줘"
  //
  //   ★왜 NC 만 되돌리나★ 2026-08-21 에 계정줄을 아이디→플랫폼으로 바꾼 이유는
  //   '구글이냐 아니냐' 가 한눈에 보여야 해서였다(구글은 CDP 로그인이 구조적으로 막힌다, §C1).
  //   그건 지금도 맞다. 그런데 ★함대 대부분이 NC★ 라 카드가 죄다 'NC' 로 똑같아져서
  //   ★어느 계정인지 구분이 안 되는★ 부작용이 생겼다.
  //   → 소수라서 라벨 자체가 신호인 구글·전화번호는 라벨을 유지하고,
  //     다수라서 라벨이 신호가 못 되는 NC 는 아이디를 보여준다. 대신 작은 'NC' 표식은 남긴다.
  const isNC     = !!(pl && pl.label === 'NC');
  const useId    = (!pl || isNC) && !!id;
  const shown    = useId ? id : (pl ? pl.label : id);
  // ★★맨 아랫줄이 안 보인다 (2026-08-29 주인님: "존더 비비드하게")★★
  //   예전: 11px · 컨테이너 text-gray-500 · 아이디 text-gray-400 · 서버 cyan/80.
  //   전부 반투명·회색이라 카드 배경(#111827)에 묻혔다. → 12px + 불투명 + 굵게.
  const shownCls = useId ? 'text-slate-100' : (pl ? pl.cls : '');
  return `<div class="mt-2 pt-1.5 border-t border-gray-700/70 flex items-center gap-1.5 whitespace-nowrap overflow-hidden" style="font-size:12px">
      <span class="shrink-0 font-bold text-violet-300">🔑 계정 ${n}</span>
      ${isNC ? `<span class="shrink-0 font-bold text-sky-300" style="font-size:10px" title="플랫폼: NC">NC</span>` : ''}
      <span class="truncate font-semibold ${shownCls}" title="${esc(id || plat)}">${esc(shown)}</span>
      ${srv?`<span class="ml-auto shrink-0 srv-chip" title="게임 서버">${esc(srv)}</span>`:''}
    </div>`;
}

// ─── 드래그 순서 관리 ─────────────────────────────────────────────────────────
const DRAG_ORDER_KEY_ON  = 'card_order_online';
const DRAG_ORDER_KEY_OFF = 'card_order_offline';
let dragSrcId = null;
let dragSection = null;

function loadOrder(key) {
  try { return JSON.parse(localStorage.getItem(key)) || []; } catch(e) { return []; }
}
function saveOrder(key, ids) {
  localStorage.setItem(key, JSON.stringify(ids));
}
// ★★카드 순서가 제멋대로 날아가던 이유 (2026-08-18 사용자 지적)★★
//   "카드위치조정하는거 뭐 왜 지멋대로 되냐? 내가 수정해도 지멋대로 날아가는데?"
//
//   원인 두 개가 겹쳐 있었다:
//     ① 순서를 ★온라인/오프라인 두 목록으로 따로★ 저장했다.
//     ② saveCurrentOrder 가 ★그 순간 그 칸에 보이는 카드만★ 담아 통째로 덮어썼다.
//   PC 하나가 오프라인이 되면 오프라인 칸으로 옮겨간다. 그 뒤 온라인 칸에서 카드를
//   한 번만 끌면, 저장된 온라인 순서에서 ★그 PC 가 통째로 지워진다.★ 다시 온라인이
//   되면 '처음 보는 카드'라 맨 뒤에 이름순으로 붙는다.
//   계정 카드(PC-20b/c/d)는 전환마다 온·오프를 오가므로 특히 심했다.
//
//   → ★목록을 하나로 합치고, 저장은 '덮어쓰기'가 아니라 '병합'으로 바꾼다.★
//     지금 안 보이는 카드의 자리는 그대로 두고, 보이는 카드끼리만 자리를 재배치한다.
const DRAG_ORDER_KEY = 'card_order_v2';

// ★★순서의 키는 '계정' 이 아니라 '★PC★' 다 (2026-08-18 사용자 지적)★★
//   "지금 카드겹쳐잇는것때문에 그런가? 자리 안바뀌는거 고쳐봐" — 맞았다.
//   겹친 카드(멀티계정 스택)는 ★지금 활성인 계정 카드★ 를 대표로 세운다. 그래서
//   계정을 전환하면 대표 id 가 PC-20 → PC-20b 로 바뀐다. 순서를 그 id 로 저장하면
//   전환할 때마다 ★처음 보는 카드★ 가 돼 맨 뒤로 밀린다 — 자리가 안 지켜지는 이유.
//   → baseId(PC-20b → PC-20) 를 키로 쓴다. 계정이 뭐든 스택의 자리는 하나다.
function sortByOrder(pcs, key) {
  const order = loadOrder(DRAG_ORDER_KEY);
  const idx = {};
  order.forEach((id,i) => idx[id] = i);
  const k = p => baseId(p.pc_id || '');
  const known = pcs.filter(p => idx[k(p)] !== undefined).sort((a,b) => idx[k(a)] - idx[k(b)]);
  const fresh = pcs.filter(p => idx[k(p)] === undefined).sort((a,b) => k(a).localeCompare(k(b)));
  return [...known, ...fresh];
}

// grid 의 직계 자식 하나 = PC 묶음 하나. 그 자식에서 정렬 키(PC base)를 뽑는다.
//   겹친 카드 → <div id="stack-PC-20">, 한 장짜리 → <div id="card-PC-07">
function gridKeyOf(el) {
  const id = (el && el.id) || '';
  if (id.indexOf('stack-') === 0) return id.slice(6);
  if (id.indexOf('card-') === 0) return baseId(id.slice(5));
  return '';
}

function saveCurrentOrder(gridId, key) {
  const seenB = {};
  const visible = [...document.getElementById(gridId).children]
    .map(gridKeyOf)
    .filter(id => id && !seenB[id] && (seenB[id] = 1));
  if (!visible.length) return;
  const stored = loadOrder(DRAG_ORDER_KEY);
  const merged = stored.slice();
  const slots = [];
  merged.forEach((id,i) => { if (visible.indexOf(id) >= 0) slots.push(i); });
  // 저장된 적 없는 카드는 뒤에 자리를 새로 만든다
  visible.forEach(id => {
    if (merged.indexOf(id) < 0) { merged.push(id); slots.push(merged.length - 1); }
  });
  slots.sort((a,b) => a - b);
  slots.forEach((slot,k) => { merged[slot] = visible[k]; });
  saveOrder(DRAG_ORDER_KEY, merged);
}

// 옛 두 목록(card_order_online / card_order_offline)을 한 번만 합쳐 옮긴다
//   ★즉시 실행하지 않는다★ — baseId 는 한참 아래에 정의돼 있어 호이스팅에만 기대게 된다.
//   블록이 쪼개지는 순간 조용히 깨지므로, 첫 렌더 때 renderCards 가 부른다.
let _orderMigrated = false;
function migrateOrder(){
  if (_orderMigrated) return;
  _orderMigrated = true;
  try {
    if (localStorage.getItem(DRAG_ORDER_KEY)) return;
    const a = JSON.parse(localStorage.getItem('card_order_online')  || '[]') || [];
    const b = JSON.parse(localStorage.getItem('card_order_offline') || '[]') || [];
    const seen = {}, out = [];
    // ★옛 목록엔 계정 id(PC-20b)가 섞여 있다 — base 로 정규화하며 합친다★
    [...a, ...b].forEach(id => {
      const k = baseId(id || '');
      if (k && !seen[k]) { seen[k] = 1; out.push(k); }
    });
    if (out.length) saveOrder(DRAG_ORDER_KEY, out);
  } catch(e) {}
}

function setupDrag(gridId, orderKey) {
  const grid = document.getElementById(gridId);
  if (!grid) return;
  // ★grid 의 ★직계 자식★ 을 끈다 — 겹친 카드는 wrapper 가 자식이다 (2026-08-18)★
  //   예전엔 [id^="card-"] 로 찾아서 스택 안쪽 '앞 카드'만 집혔다. 그래서 앞장만
  //   빠져나가는 것처럼 보이고, 저장 때 wrapper 의 id 가 비어 아무것도 안 남았다.
  [...grid.children].forEach(card => {
    const handle = card.querySelector('.drag-handle');
    if (!handle) return;
    // ★부분 갱신 뒤엔 안 바뀐 카드가 그대로 남는다 — 두 번 묶지 않는다(2026-09-23)★
    if (card._dragBound) return;
    card._dragBound = true;
    card.setAttribute('draggable','false');
    // 핸들에서만 드래그 시작
    handle.addEventListener('mousedown', e => {
      e.stopPropagation();
      card.setAttribute('draggable','true');
      dragSrcId = gridKeyOf(card);
      dragSection = orderKey;
      // ★끌지 않고 놓으면 되돌린다(2026-09-23 반증 B2-3)★ — dragend 는 실제로 끌었을 때만 온다.
      //   예전엔 매 렌더가 카드를 새로 깔아 저절로 지워졌는데, 부분 갱신은 안 바뀐 카드를 그대로 둔다
      //   → 카드 몸통 전체가 끌리고, 나중에 다른 카드를 끌면 이 카드가 옮겨졌다.
      document.addEventListener('mouseup', () => {
        if (card.classList.contains('card-dragging')) return;
        card.setAttribute('draggable','false');
        if (dragSrcId === gridKeyOf(card)) { dragSrcId = null; dragSection = null; }
      }, {once: true});
    });
    handle.addEventListener('click', e => e.stopPropagation());
    card.addEventListener('dragstart', e => {
      if (!dragSrcId) { e.preventDefault(); return; }
      e.dataTransfer.effectAllowed='move';
      e.dataTransfer.setData('text/plain', dragSrcId);
      card.classList.add('card-dragging');
    });
    card.addEventListener('dragend', () => {
      card.setAttribute('draggable','false');
      card.classList.remove('card-dragging');
      grid.querySelectorAll('.card-dragover').forEach(el=>el.classList.remove('card-dragover'));
      dragSrcId=null; dragSection=null;
    });
    card.addEventListener('dragover', e => {
      if (!dragSrcId||dragSection!==orderKey) return;
      e.preventDefault();
      e.dataTransfer.dropEffect='move';
      grid.querySelectorAll('.card-dragover').forEach(el=>el.classList.remove('card-dragover'));
      card.classList.add('card-dragover');
    });
    card.addEventListener('dragleave', () => { card.classList.remove('card-dragover'); });
    card.addEventListener('drop', e => {
      e.preventDefault();
      card.classList.remove('card-dragover');
      const fromId = e.dataTransfer.getData('text/plain');
      const toId = gridKeyOf(card);
      if (!fromId || fromId===toId) return;
      // 끌려온 묶음의 ★직계 자식★ 을 찾는다(스택이면 wrapper, 한 장이면 카드)
      const fromEl = [...grid.children].find(el => gridKeyOf(el) === fromId);
      if (!fromEl) return;
      const rect = card.getBoundingClientRect();
      const after = e.clientY > rect.top + rect.height/2;
      if (after) { card.after(fromEl); } else { card.before(fromEl); }
      saveCurrentOrder(gridId, orderKey);
    });
  });
}

// ─── 멀티계정 카드 스택 (2026-08-15 사용자: "아이디 2개면 카드가 포커게임 카드 여러장처럼
//     겹쳐 보이게 — 3개면 3개, 4개면 4개") ─────────────────────────────────────
// base(물리 PC)별로 계정 카드를 묶어 맨 위 1장 + 뒤에 층층이 엿보이는 레이어로 그린다.
// 맨 위 = 클릭으로 앞세운 카드 > 온라인 카드 > 최근 활동 순. ★전환되면 온라인 카드가 자동으로
// 맨 위가 되므로 '내용도 그 계정 것으로 바뀜'이 저절로 성립(계정마다 pc_id·데이터 분리).★
// 장수 = max(실제 계정 카드 수, 매크로가 보고한 자격증명 계정 수 acct_total).
// 단일 계정 PC는 buildCard 그대로 = 함대 17대 화면 불변.
let stackFront = {};   // base → 사용자가 클릭으로 앞세운 pc_id
let stackLastOn = {};  // base → 마지막으로 관측된 온라인 pc_id (전환 감지 → 고정 자동 해제)
// ★★탭으로 계정을 바꾸면 선택(✔)도 같이 옮긴다 (2026-08-28 적대검증 [높2])★★
//   카드는 top 한 장만 그려진다. 계정2 카드를 고른 채 탭「1」로 옮기면 selectedPcs
//   에는 PC-20b 가 남는데 화면엔 ✔ 가 없다 → 하단 바는 "1개 선택" 인데 어디가 골라졌는지
//   안 보이고, 그 상태로 명령을 누르면 ★안 보이는 카드로 나간다.★
//   탭이 생겨 계정 전환이 쉬워진 만큼 훨씬 자주 난다.
function stackShow(base, pcid){
  [...selectedPcs].forEach(id => {
    if (baseId(id) === base && id !== pcid) { selectedPcs.delete(id); selectedPcs.add(pcid); }
  });
  stackFront[base] = pcid;
  renderCards();
  try { updateSelBar(); } catch(e) {}
}
// ═══════════════════════════════════════════════════════════════════════════
// ★★계정 탭 (2026-08-28 주인님 지시 — 직접 그린 그림 그대로)★★
//   ┌─┬─┐
//   │1│2│      ← 카드 ★위★ 에 번호 탭. 지금 보는 계정이 진하다.
//   ├─┴──────┐
//   │  카드   │
//   └────────┘
//   ★계정이 1개여도 「1」 탭을 그린다★ — 주인님: "한개만있어도 1 표시하게끔하면
//   다 카드 높낮이도 같을거고". 탭 줄이 항상 32px 이고, .acct-stack 이 grid 라
//   안쪽 카드까지 stretch 가 전달된다(적대검증 [높1] 수리).
//
//   ★예전 방식(.stack-layer)을 왜 버렸나★
//     · 누를 자리가 높이 16px 짜리 가로 띠였다 — 주인님: "버튼 누르기도 좇같고"
//     · padding-top 이 18px×(계정수-1) 이라 ★계정 수마다 카드 높이가 달랐다★
//
//   ★wrapper id 는 그대로 stack-<base> 다 (2026-08-18 사고)★ — 없으면 드래그가
//   wrapper 가 아니라 안쪽 앞 카드를 집어 "앞장만 넘어가고 원래대로 돌아오는" 버그가
//   난다. gridKeyOf() 가 stack- / card- 둘 다 읽으므로 ★한 장짜리도 wrapper 로 감싼다★
//   (그래야 탭 줄이 붙고 높이가 같아진다).
// ═══════════════════════════════════════════════════════════════════════════
function buildStack(s){
  const maps = groupAcctMaps(s.base);
  // ★★번호가 겹치거나 비어도 ★보고 있는 카드★ 는 반드시 탭을 갖는다 (적대검증 치1)★★
  //   초판은 byNum[acctNumOf(...)] = p 로 ★덮어썼다.★ 두 카드가 같은 번호를 내면
  //   (acctNumOf 는 접미사 우선, currentAcctNum 은 acct_num 우선 — 우선순위가 다르다)
  //   하나가 탭에서 사라지고, 그게 하필 s.top 이면 활성 탭이 0개가 된다.
  //   그 상태에서 다른 탭을 누르면 stackFront 가 고정돼 ★영영 못 돌아온다.★
  //   → ①먼저 온 카드가 이긴다 ②top 은 자기 번호 자리를 강제로 차지한다
  //     ③탭 개수는 실제 카드 번호의 최대값까지 넓힌다(띄엄띄엄 1·4 도 담기게)
  const byNum = {};
  s.list.forEach(p => { const k = acctNumOf(p.pc_id); if (!byNum[k]) byNum[k] = p; });
  const topNum = acctNumOf(s.top.pc_id);
  byNum[topNum] = s.top;
  // ★총 계정 수는 ★자격증명(acct_ids)★ 도 같이 봐야 한다★ — 옛 매크로는 acct_total 을
  //   안 보내므로 카드 수만 세면 「계정 5개인데 탭 2개」가 된다.
  const idNums = Object.keys(maps.ids || {})
      .filter(k => String((maps.ids || {})[k] || '').trim())
      .map(k => parseInt(k, 10)).filter(n => n >= 1 && n <= MAX_ACCT);
  const hiNum = Math.min(MAX_ACCT,
      Math.max(1, s.n, topNum, ...idNums, ...s.list.map(p => acctNumOf(p.pc_id))));
  let tabs = '';
  for (let k = 1; k <= hiNum; k++) {
    const g = byNum[k];
    // ★은퇴한 카드 번호는 회색 탭조차 안 그린다(2026-09-22, 주인님 「회색 카드 다 없애라」)★
    //   카드가 없는(g 없는) 자리만 거른다 — 실제로 도는 카드가 있으면(g 있음) 은퇴 대상이
    //   아닐 것이므로 건드리지 않는다(방어적 스코프).
    if (!g && RETIRED.has(k === 1 ? s.base : s.base + ACCT_LABELS[k-1])) continue;
    const cur = !!g && g.pc_id === s.top.pc_id;
    const on = g ? ((STATUS_CFG[g.status||'offline']||STATUS_CFG.offline).online) : false;
    // 접미사 pc_id 대신 아이디(전 계정 지도)로 — "20b" 노출 금지(v1.1.424 사용자)
    const gid = (g && g.acct_id) || maps.ids[k] || '';
    const plat = (maps.plats||{})[k] || '';
    const idTxt = (String(plat).indexOf('구글') >= 0) ? '구글' : gid;
    const stTxt = g ? ((STATUS_CFG[g.status||'offline']||STATUS_CFG.offline).label || '') : '';
    const rot = g ? (g._rot || '') : '';
    const tip = g
      ? `계정 ${k}${idTxt?' · '+idTxt:''} · ${stTxt}${rot?' · 🔁 순환중':''}`
        + (cur ? ' (지금 보는 계정)' : ' — 누르면 이 계정 카드를 봅니다')
      : `계정 ${k}${idTxt?' · '+idTxt:''} — 아직 카드가 없습니다(자격증명만 등록됨)`;
    // ★★사냥 다 끝난 계정은 탭에 ★초록 ✓★ (2026-08-29 주인님)★★
    //   원문: "계정이 사냥이 다끝나면 … 카드위에 숫자 1 V 이런식으로 초록색 체크"
    //   판정은 카드의 🏹 뱃지와 ★같은 함수★(isHuntDone) — 두 곳이 다르게 말하면 안 된다.
    //   ★카드가 없는 계정(acct-tab-none)은 판정 자체가 불가★ 라 아무 표시도 안 한다.
    const hdone = !!(g && isHuntDone(g.daily_progress));
    const cls = 'acct-tab' + (cur ? ' acct-tab-on' : '') + (g ? '' : ' acct-tab-none')
              + (hdone ? ' acct-tab-done' : '');
    const dot = on ? `<i class="tdot${rot?' tdot-rot':''}"></i>` : '';
    const chk = hdone ? `<i class="tchk">✓</i>` : '';
    const click = (g && !cur)
      ? ` onclick="event.stopPropagation();closeCardMenu();stackShow('${s.base}','${g.pc_id}')"` : '';
    tabs += `<button type="button" class="${cls}"${click} title="${esc(tip)}${hdone?' · 오늘 사냥 완료':''}">${k}${chk}${dot}</button>`;
  }
  // ══════════════════════════════════════════════════════════════════════
  // ★★총 ★캐릭★ 수 배지 (2026-08-28 주인님)★★
  //   주인님: "계정수가 아니라 ★캐릭수★ 를 적어야하는데"
  //   근거는 `acct_names` — 매크로가 info.txt 에서 읽어 보고하는
  //   ★{계정번호: {슬롯: 캐릭이름}} 전 계정 지도★ 다. 실측(PC-03):
  //     {"1":{6명}, "2":{2명}, "3":{2명}}  → 총 10캐릭
  //   ★같은 물리 PC 의 모든 카드가 같은 지도를 들고 있다★ 그래서 지금 보는 계정이
  //   어느 것이든 총계가 같다 = 탭을 눌러도 배지가 안 흔들린다.
  //   ★폴백★ 지도가 없으면(옛 매크로/수집 전) 카드별 daily_progress 길이를 더한다.
  //     그건 ★카드가 있는 계정만★ 세므로 실제보다 작을 수 있어 앰버로 알린다.
  // ══════════════════════════════════════════════════════════════════════
  const nameMap = {};
  s.list.forEach(p => Object.assign(nameMap, p.acct_names || {}));
  let nChar = 0, nCharAcct = 0;
  for (const k of Object.keys(nameMap)) {
    const cnt = Object.values(nameMap[k] || {}).filter(v => String(v || '').trim()).length;
    if (cnt > 0) { nChar += cnt; nCharAcct++; }
  }
  let charGap = '';
  if (!nChar) {                      // 지도가 없다 → 카드에서 센다(모자랄 수 있다)
    const seen = {};
    s.list.forEach(p => { const k = acctNumOf(p.pc_id);
      seen[k] = Math.max(seen[k] || 0, (p.daily_progress || []).length); });
    nChar = Object.values(seen).reduce((a, b) => a + b, 0);
    nCharAcct = Object.keys(seen).filter(k => seen[k] > 0).length;
    charGap = '캐릭 이름 정보가 없어 ★카드에 보인 슬롯만★ 셌습니다 (정보수집을 돌리면 정확해집니다)';
  } else if (nCharAcct < hiNum) {
    charGap = `계정 ${hiNum}개 중 ★${nCharAcct}개만★ 캐릭 정보가 있습니다 `
            + `(나머지는 정보수집이 안 됐거나 info.txt 에 캐릭 이름이 비어 있습니다)`;
  }
  const totTip = `이 PC 의 ★총 캐릭 ${nChar}명★ · 계정 ${hiNum}개`
    + (idNums.length && idNums.length !== hiNum ? ` (아이디 등록 ${idNums.length}개)` : '')
    + (charGap ? ` \u2014 ${charGap}` : '');
  const total = `<span class="acct-total${charGap ? ' acct-total-gap' : ''}" `
    + `title="${esc(totTip)}">캐릭 ${nChar}</span>`;
  return `<div id="stack-${s.base}" class="acct-stack">
    <div class="acct-tabs">${tabs}${total}</div>
    <div class="acct-body relative">${buildCard(s.top)}</div></div>`;
}

// 격자 부분 갱신 — items 순서대로 [{key, html}]. 같은 key 의 html 이 그대로면 그 DOM 을 ★건드리지 않는다★.
//   바뀐 것만 새 노드로 바꾸고, 순서가 다르면 옮기고, 없어진 것은 뺀다. 한 뿌리 요소가 아닌
//   html 이 섞이면 예전처럼 통째로 깐다(안전판). 반환 = 갈아끼운·뺀 노드 수(계측용).
let RENDER_STATS = {calls: 0, replaced: 0, kept: 0, full: 0};
// 「N초 전」 칸(relSpan)은 시각 값·글자를 비교에서 뺀다 — 하트비트마다 last_active 가 바뀌어도 카드는 남긴다.
//   남긴 카드는 새 HTML 의 시각 값을 ★같은 순서로★ 옮겨 적고 글자를 다시 쓴다(정규화가 같으면 칸 수·순서도 같다).
// ★B-CQ9★ 대기 명령 칩·막대(data-pt)의 글자(경과 초)도 뺀다 — 키(data-pt 값)는 남긴다(pendKey 설명).
const _rkNorm = h => String(h).replace(/data-rt="[^"]*">[^<]*/g, 'data-rt="">').replace(/(data-pt="[^"]*">)[^<]*/g, '$1');
const _rkUnesc = v => v.replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
function _rkTick(el, html){
  const vals = [...String(html).matchAll(/data-rt="([^"]*)"/g)].map(m => _rkUnesc(m[1]));
  el.querySelectorAll('[data-rt]').forEach((e, i) => {
    if (i < vals.length && e.dataset.rt !== vals[i]) e.dataset.rt = vals[i];
    const t = relTime(e.dataset.rt); if (e.textContent !== t) e.textContent = t;
  });
}
function reconcileGrid(grid, items){
  RENDER_STATS.calls++;
  const tpl = document.createElement('template');
  const cur = new Map();
  let clean = true;
  [...grid.children].forEach(el => { const k = el.dataset ? el.dataset.rk : null; if (k && !cur.has(k)) cur.set(k, el); else clean = false; });
  const fresh = items.map(it => {
    const old = clean ? cur.get(it.key) : null;
    const norm = _rkNorm(it.html);
    if (old && old._rkHtml === norm) { RENDER_STATS.kept++; _rkTick(old, it.html); return old; }
    tpl.innerHTML = String(it.html).trim();
    if (tpl.content.childElementCount !== 1) return null;
    const el = tpl.content.firstElementChild;
    el.dataset.rk = it.key; el._rkHtml = norm;
    RENDER_STATS.replaced++;
    return el;
  });
  if (!clean || fresh.some(x => !x)) {
    RENDER_STATS.full++;
    grid.innerHTML = items.map(it => it.html).join('');
    [...grid.children].forEach((el, i) => { if (items[i]) { el.dataset.rk = items[i].key; el._rkHtml = _rkNorm(items[i].html); } });
    return items.length;
  }
  const keep = new Set(fresh);
  let changed = 0;
  [...grid.children].forEach(el => { if (!keep.has(el)) { el.remove(); changed++; } });
  let prev = null;
  fresh.forEach(el => {
    const want = prev ? prev.nextElementSibling : grid.firstElementChild;
    if (want !== el) { grid.insertBefore(el, want); changed++; }
    prev = el;
  });
  return changed;
}

function renderCards() {
  migrateOrder();          // 옛 순서 목록 1회 이관(baseId 정의 뒤에 안전하게)
  // ★PC-TEST 는 화면에 안 띄운다 (2026-08-20 사용자: "거슬린다")★
  //   배포 검증이 pc_id=PC-TEST 로 /check 를 때리면서 카드가 생긴다. 지워도 다음
  //   검증 때 또 생기므로 ★렌더 단계에서 거른다★ (전광판 합계에서도 같이 빠진다).
  const pcs = Object.values(state)
    .filter(p => !isFakePc(p.pc_id))   // PC-DEMO 도(2026-09-23 — 전광판 모집단을 dkSubCount·서버와 같게)
    .sort((a,b)=>(a.pc_id||'').localeCompare(b.pc_id||''));
  const groups = {};
  pcs.forEach(p => { const b = baseId(p.pc_id||''); (groups[b] = groups[b] || []).push(p); });
  const isOn = p => (STATUS_CFG[p.status||'offline']||STATUS_CFG.offline).online;
  const stacks = Object.entries(groups).map(([b, list]) => {
    // ★계정이 실제로 바뀌면(온라인 카드가 달라지면) 수동 고정(stackFront)을 자동 해제★ —
    //   "전환하면 알아서 그 카드로 바뀌는 거지?"(2026-08-15 사용자)가 수동 고정보다 우선.
    //   고정은 같은 세션 안에서 뒷계정 데이터를 잠깐 볼 때만 유지된다.
    const onNow = (list.find(isOn) || {}).pc_id || '';
    if (stackLastOn[b] !== onNow) { delete stackFront[b]; stackLastOn[b] = onNow; }
    let top = list.find(p => p.pc_id === stackFront[b]);
    if (!top) top = list.find(isOn)
      || list.slice().sort((x,y)=>String(y.last_active||'').localeCompare(String(x.last_active||'')))[0];
    const n = Math.min(MAX_ACCT, Math.max(list.length, ...list.map(p => p.acct_total || 1)));
    return {base: b, list, top, n, online: list.some(isOn)};
  });
  // ★★오프라인 카드를 아래로 내리지 않는다 (2026-08-20 사용자 지시)★★
  //   원문: "오프라인이면 밑으로 내려가잖아 앞으로 그렇게하지말고 ★온라인 자리에 그대로
  //          놔두고 오프라인으로 명시만★ 해놓는걸로하자 오히려 헷갈리네"
  //   ★왜 헷갈렸나★ 카드가 자리를 옮기면 "20번이 어디 갔지" 를 매번 다시 찾아야 한다.
  //   PC 는 물리적으로 고정된 물건인데 화면에서만 돌아다니면 위치 기억이 무용지물이 된다.
  //   → 한 격자에 전부 두고 ★순서를 고정★ 한다. 죽었다는 건 카드 색·뱃지가 말한다.
  //   섹션/격자 자체는 남겨둔다(HTML·드래그 코드 건드리지 않음) — 비워서 숨긴다.
  const byTop = arr => { const m={}; arr.forEach(s=>m[s.top.pc_id]=s); return m; };
  const am = byTop(stacks);
  const all = sortByOrder(stacks.map(s=>s.top), DRAG_ORDER_KEY_ON).map(t=>am[t.pc_id]);
  const offCnt = stacks.filter(s=>!s.online).length;
  const go  = document.getElementById('grid-online');
  const gof = document.getElementById('grid-offline');
  // ★바뀐 카드만 갈아끼운다(2026-09-23 반응속도 2단계)★ — 예전엔 보고가 올 때마다 격자 전체를
  //   innerHTML 로 새로 깔아 스크롤 위치·열린 메뉴·입력 포커스·호버가 매번 날아갔다.
  if (all.length) reconcileGrid(go, all.map(s => ({key: s.base, html: buildStack(s)})));
  else { go.innerHTML = '<div class="text-gray-700 text-sm col-span-full text-center py-10">매크로 연결 없음</div>'; }
  gof.innerHTML = '';
  document.getElementById('online-count').textContent  = `(${all.length - offCnt}/${all.length})`;
  document.getElementById('offline-count').textContent = `(${offCnt})`;
  document.getElementById('offline-section').classList.add('hidden');
  refreshSummary(pcs);
  document.getElementById('pc-count').textContent = `PC ${pcs.length}대`;
  setupDrag('grid-online',  DRAG_ORDER_KEY_ON);
  setupDrag('grid-offline', DRAG_ORDER_KEY_OFF);
  try{ dkApplyBleed(); }catch(e){}   // 커맨드 덱: 이상 카드만 윗면 빛샘 (실패해도 화면은 멀쩡)
  try{ dkHero(); }catch(e){}         // 커맨드 덱: 히어로 요약 (기존 전광판은 그대로)
  // ★★사냥 탭 실시간 갱신 (2026-08-28 주인님 지시 "실시간으로")★★
  //   사냥 탭은 charTableData 가 아니라 ★state★ 를 본다. state 가 바뀌면 renderCards 가
  //   불리므로 여기에 붙여야 '실시간' 이 된다 — loadCharTable 쪽 훅(정보수집 갱신)만으로는
  //   ★사냥 상태가 바뀌어도 탭이 안 움직인다.★
  //   ★모달이 닫혀 있으면 아무 일도 안 한다★ (던전 탭 훅과 같은 규약)
  //   try 로 감싼 이유: aiFilter 는 let 이라 이 함수가 그 선언보다 먼저 불리면 TDZ 에러가
  //   난다. 화면 전체를 죽이느니 이 탭만 조용히 건너뛴다.
  try {
    const _am = document.getElementById('ai-modal');
    if (_am && !_am.classList.contains('hidden') && aiFilter === 'hunt') renderAiHunt();
  } catch(e) {}
}

function fmtKinaKor(n) {
  if (!n || n === 0) return '0';
  const eok = Math.floor(n / 100000000);
  const man = Math.floor((n % 100000000) / 10000);
  if (eok > 0 && man > 0) return `${eok}억 ${man.toLocaleString()}만`;
  if (eok > 0) return `${eok}억`;
  if (man > 0) return `${man.toLocaleString()}만`;
  return n.toLocaleString();
}

function parseOddEnergy(str) {
  // "300(+1,195)/840" → 300 + 1195 = 1495
  if (!str) return 0;
  const m = str.match(/^([\d,]+)(?:\(\+([\d,]+)\))?/);
  if (!m) return 0;
  const a = parseInt(m[1].replace(/,/g,''), 10) || 0;
  const b = m[2] ? parseInt(m[2].replace(/,/g,''), 10) : 0;
  return a + b;
}

function refreshSummary(pcs) {
  const c={online:0,offline:0,completedPcs:0,onlineChars:0,completedChars:0,totalKina:0};
  const seenPc = new Set();
  const dungeonLeft = new Set();   // 일일던전(계정 티켓) 안 끝난 PC — 오프라인 포함(계정 기준)
  pcs.forEach(p=>{
    const s=p.status||'offline';
    const isOnline = (STATUS_CFG[s]||STATUS_CFG.offline).online;
    if(isOnline) c.online++; else c.offline++;
    // ★계정 없음은 「안 끝남」 집계에도 안 넣는다(2026-09-23)★ — 할 계정이 없으므로
    //   '아직 못 끝냄'이 아니다. isDungeonDone 이 false(dungeon_done_at 비움)라고 넣으면
    //   완주 집계가 흐려진다(오늘 처음 화면과 같은 부류의 사고).
    if(!isDungeonDone(p) && s !== 'no_account') dungeonLeft.add(p.pc_id);
    const dp = p.daily_progress||[];
    if(dp.length>0 && dp.every(dpDone)) c.completedPcs++;
    // ★캐릭터 수 집계(2026-08-07)★ — 전광판은 대수가 아니라 캐릭터 수를 보여준다.
    //   캐릭 수는 daily_progress 길이(=슬롯 수)가 정본, 아직 없으면 chars 목록으로 보완.
    const nChars = dp.length || ((p.chars && p.chars.length) || 0);
    if(isOnline) c.onlineChars += nChars;   // ↓ 아래에서 '뒷카드 포함'으로 다시 계산한다
    c.completedChars += dp.filter(dpDone).length;
    // 창고키나: PC별 1회만 합산 (창고 공유 → 중복 방지)
    if(p._total_kina && !seenPc.has(p.pc_id)) {
      seenPc.add(p.pc_id);
      c.totalKina += p._total_kina;
    }
  });
  // ★★온라인 캐릭터 수는 '뒷카드까지' 센다 (2026-08-18 사용자 지적)★★
  //   "온라인 캐릭터 갯수가 안맞네? 뒤에카드까지포함해서 갯수맞춰야지"
  //   멀티계정 PC 는 활성 계정 카드만 online 이고, 나머지 계정 카드는 status=
  //   'other_account'(online:false) 라 캐릭터 집계에서 통째로 빠졌다. 하지만 그 PC 는
  //   켜져 있고 그 계정들의 캐릭터도 오늘 돌 대상이다 — 자리(PC)가 하나면 캐릭터도
  //   묶음 전체를 세야 숫자가 맞는다.
  //   → PC 묶음(baseId) 중 ★하나라도 온라인이면★ 그 묶음의 전 계정 캐릭터를 더한다.
  {
    const grp = {};
    pcs.forEach(p => {
      const b = baseId(p.pc_id || '');
      (grp[b] = grp[b] || []).push(p);
    });
    // ★★온라인 여부로 묶음을 건너뛰지 않는다 (2026-08-22 주인님 지시)★★
    //   주인님: "온라인캐릭터 말고 문구를 캐릭터로 바꾸고 카드 뒤의 캐릭터들도
    //            모두포함해서 집계를 하도록하게해"
    //   ★무엇이 틀렸나★ 뒷카드(other_account)는 이미 합치고 있었는데, 그 앞에
    //   `anyOn` 게이트가 있어서 ★묶음에 온라인 카드가 하나도 없으면 통째로 건너뛰었다.★
    //   실측(2026-08-22 16:1x): 표시 122 / 실제 153 — 차이 31.
    //     PC-17(6) · PC-19(9) · PC-20(8) · PC-21(8) 이 빠졌다.
    //     직원분들이 대시보드에서 끈 PC 들이라 카드가 전부 offline/other_account 였다.
    //   ★이 숫자는 '지금 몇 대가 켜져 있나' 가 아니라 '내가 굴리는 캐릭이 몇인가' 다.★
    //   PC 를 껐다고 캐릭터가 사라지는 게 아니므로 온라인 여부와 무관하게 전부 센다.
    //   (대수 정보는 아래 '온라인' 섹션 헤더와 이 칸 툴팁에 그대로 남는다)
    let n = 0;
    Object.values(grp).forEach(list => {
      list.forEach(p => {
        const dp = p.daily_progress || [];
        n += dp.length || ((p.chars && p.chars.length) || 0);
      });
    });
    c.onlineChars = n;
  }
  // 오드에너지 + 각성전 티켓 + 거래키나 합산 (charTableData 기준, 거래키나는 캐릭터별 소지라 전 캐릭 합산)
  let totalOdd = 0, totalAwaken = 0, awakenSeen = false, totalTrade = 0, tradeSeen = false;
  charTableData.forEach(r => {
    totalOdd += parseOddEnergy(r.odd_energy);
    // 빈 문자열은 「못 읽음」 — 서버 _fv_char_agg 의 _seen 과 같은 뜻(2026-09-23 반증 B6)
    if (r.awakening_ticket != null && r.awakening_ticket !== '') { awakenSeen = true; totalAwaken += (parseInt(r.awakening_ticket) || 0); }
    if (r.trade_kina != null && r.trade_kina !== '') { tradeSeen = true; totalTrade += (Number(r.trade_kina) || 0); }
  });
  const elOn = document.getElementById('cnt-online');
  elOn.textContent = c.onlineChars;
  // 숫자는 '전체 캐릭터', 대수 정보는 툴팁에 남긴다 (온라인/오프라인 구분은 여기서 확인)
  elOn.title = `전체 캐릭터 ${c.onlineChars}명 — 뒷카드(다른 계정)·오프라인 PC 포함`
             + ` / PC 온라인 ${c.online}대 · 오프라인 ${c.offline}대`;
  // ★서버값 있으면 그게 최종값(2026-09-23, §A12)★ — /api/fv/snapshot 과 같은 계산.
  //   아직 안 왔으면(첫 화면) 클라 계산을 폴백으로 보여준다.
  //   ★서버 합계는 「한 카드도 못 읽은 칸」을 null 로 준다(반증 #2)★ — 0 과 모름을 가른다.
  //   ★낡으면(60초) 서버값을 버린다(반증 #4)★ — serverSum() 이 null 을 준다.
  const ss = serverSum();
  const ssNote = serverSumNote();
  const elOdd = document.getElementById('cnt-odd-energy');
  elOdd.textContent = ss
    ? (ss.odd_energy > 0 ? ss.odd_energy.toLocaleString() : '–')
    : (totalOdd > 0 ? totalOdd.toLocaleString() : '–');
  elOdd.title = ssNote;
  const elAw = document.getElementById('cnt-awakening');
  elAw.textContent = ss
    ? (ss.awakening_ticket != null ? ss.awakening_ticket.toLocaleString() : '–')
    : (awakenSeen ? totalAwaken.toLocaleString() : '–');
  elAw.title = ssNote;
  const elTr = document.getElementById('cnt-trade-kina');
  elTr.textContent = ss
    ? (ss.trade_kina != null ? fmtKinaKor(ss.trade_kina) : '–')
    : (tradeSeen ? fmtKinaKor(totalTrade) : '–');
  elTr.title = ssNote;
  document.getElementById('cnt-dungeon-left').textContent=pcs.length ? String(dungeonLeft.size) : '–';
  const elDone = document.getElementById('cnt-completed');
  elDone.textContent = c.completedChars;
  elDone.title = `오늘 사냥을 끝낸 캐릭터 ${c.completedChars}명 · 전 캐릭 완료한 PC ${c.completedPcs}대 (새벽 5시 초기화)`;
  const elTk = document.getElementById('cnt-total-kina');
  elTk.textContent = ss ? fmtKinaKor(ss.total_kina || 0) : fmtKinaKor(c.totalKina);
  elTk.title = ssNote;
  // ★히어로가 ★같은 값★ 을 쓰게 넘겨둔다 (2026-08-29)★ — 따로 더하면 전광판과 갈린다.
  //   renderCards 안에서 refreshSummary 가 dkHero 보다 먼저 불린다(4389 → 4394).
  DK_SUM.kina  = ss ? (ss.total_kina || 0) : c.totalKina;
  DK_SUM.trade = ss ? (ss.trade_kina != null ? ss.trade_kina : null) : (tradeSeen ? totalTrade : null);
  try { renderAbyssTiles(ss, ssNote); } catch(e){ console.error('renderAbyssTiles', e); }
}

// ★어비스 수익 두 칸 (2026-09-23 주인님 장부 #104)★ — ★서버값만★ 쓴다. 「오늘」 은 서버가 PC 마다
//   구간(since)을 은행에 쌓은 값이라 화면에서 다시 셀 재료가 없다. 서버값이 없거나 낡았으면(60초)
//   「측정 대기」 — ★0 으로 칠하지 않는다★(모름 ≠ 0).
function renderAbyssTiles(ss, note){
  const a = ss && ss.abyss;
  const $ = id => document.getElementById(id);
  const tEl = $('cnt-abyss-today'), rEl = $('cnt-abyss-rate'), sub = $('cnt-abyss-rate-sub'), tile = $('tile-abyss-rate');
  if (a && a.red_rate != null)
    ABYSS_TH = {red_rate: a.red_rate, min_mins: a.min_mins != null ? a.min_mins : ABYSS_TH_DEFAULT.min_mins};
  if (!tEl || !rEl) return;
  tEl.textContent = (a && a.today != null) ? fmtKinaKor(a.today) : '측정 대기';
  tEl.title = (a ? `오늘(KST ${a.day}) 어비스에서 번 키나 — PC ${a.today_pcs}대 합 · 00:00 초기화\n` : '') + (note || '');
  const hasRate = !!(a && a.rate_sum != null);
  rEl.textContent = hasRate ? fmtKinaKor(a.rate_sum) : '측정 대기';
  const red = !!(hasRate && a.red);
  if (tile) tile.dataset.red = red ? '1' : '';
  if (sub) sub.textContent = a
    ? (hasRate ? `대당 ${fmtKinaKor(a.rate_avg)} · ` : '') + `${a.rate_n}대 측정 중 / ${a.wait_n}대 대기`
    : '';
  rEl.title = a
    ? `함대 시간당 합계 — ${a.min_mins}분 넘게 잰 PC 만 (${a.rate_n}대 측정 중 / ${a.wait_n}대 대기)`
      + (hasRate ? `\n대당 평균 ${fmtKinaKor(a.rate_avg)}` + (red ? ` — ★문턱 ${fmtKinaKor(a.red_rate)} 아래★` : '') : '')
      + '\n' + (note || '')
    : (note || '');
}

// ─── 선택 ─────────────────────────────────────────────────────────────────────
// ★★사고 337 (2026-08-30) — 선택 단위를 ★사람이 보는 것★ 에 맞춘다★★
//   주인님: "전체선택하고 몇개해제햇는데 ★해제한 번호도 들어가있어★. 저번에 고치라고한거임"
//   ★두 번째다.★ 2026-08-28 의 「선택 안 한 것도 업데이트해버리네」와 같은 뿌리인데,
//   그때는 ★확인창을 붙여 증상만 가렸다★(아래 selUpdaterCmd 주석). 그래서 재발했다.
//
//   화면에는 PC 당 카드가 ★한 장★(스택의 top)만 그려지는데, selectedPcs 에는
//   ★계정 카드 전부★ 가 들어간다(PC-22 · 22b · 22c · 22d · 22e). 그래서
//   전체선택이 24대인데 「61개」 였고, 카드를 눌러 빼면 ★top 하나만★ 빠졌다.
//   → 카드 한 장 = PC 한 대. 스택을 통째로 고르고 통째로 뺀다.
function stackIds(pc_id) {
  const b = baseId(pc_id || '');
  const out = Object.keys(state || {}).filter(id => baseId(id) === b);
  return out.length ? out : [pc_id];
}
function selectedBases() {
  return [...new Set([...selectedPcs].map(id => baseId(id)))].sort();
}
function toggleSelect(pc_id, e) {
  if (e && (e.target.tagName === 'BUTTON' || e.target.closest('.drag-handle'))) return;
  const ids = stackIds(pc_id);
  const on = !ids.some(id => selectedPcs.has(id));   // 하나라도 켜져 있으면 → 통째로 끈다
  ids.forEach(id => { if (on) selectedPcs.add(id); else selectedPcs.delete(id); });
  // 화면에 그려진 건 top 한 장이지만, 탭을 옮기면 다른 카드가 top 이 될 수 있으므로
  // 그 스택의 어떤 카드가 그려져 있든 표시를 맞춘다.
  ids.forEach(id => {
    const c = document.getElementById(`card-${id}`);
    if (c) c.classList.toggle('card-sel', on);
  });
  updateSelBar();
}

function clearSelection() {
  selectedPcs.clear();
  document.querySelectorAll('.card-sel').forEach(el=>el.classList.remove('card-sel'));
  updateSelBar();
}

function updateSelBar() {
  // ★사고 337★ ★물리 PC 수★ 를 센다 — 예전엔 계정 카드를 세서 24대가 「61개」 였다.
  const n = selectedBases().length;
  document.getElementById('sel-label').textContent = n > 0 ? `${n}대 선택` : '선택 없음';
}

function selectAllPcs() {
  // ★B-JS8 (2026-09-23)★ 가짜(PC-TEST·PC-DEMO)·은퇴·계정없음은 전체선택에 안 넣는다 — isExcludedPc 한 곳.
  Object.keys(state).filter(id => !isExcludedPc(id)).forEach(id=>selectedPcs.add(id));
  document.querySelectorAll('[id^="card-"]').forEach(el=>{ if (selectedPcs.has(el.id.slice(5))) el.classList.add('card-sel'); });
  updateSelBar();
}
// ★B-JS4 (2026-09-23) 명령이 ★실제로 닿을 카드★ — sendCmd 의 사고 307 우회와 같은 규칙★
//   안 도는 카드면 같은 PC 의 살아 있는 카드로. 살아 있는 카드가 없으면 자기 자신(큐에 남는다).
function cmdTargetOf(id){
  const st = (state[id]||{}).status || 'offline';
  if ((STATUS_CFG[st]||STATUS_CFG.offline).online) return id;
  const live = liveCardOf(baseId(id));
  return (live && live.pc_id) || id;
}
// 카드 id 목록 → 실제 대상 id(중복 없이). 스택 5장을 골라도 도는 매크로엔 ★1건★ 만 간다.
function cmdTargets(ids){ return [...new Set([...ids].map(cmdTargetOf))]; }

// ★멀티계정(v1.1.412 리뷰 결함 4/11): 업데이터 명령은 base id로★ — 업데이터는 PC 단위라
//   base id(PC-03)로만 폴링한다. 부계정 카드(PC-03b)로 보내면 아무도 안 가져가는 고아 명령이
//   된다. 접미사(b/c/d)를 벗겨 base로 보낸다. (매크로 명령 sendCmd는 그대로 계정별로 간다)
function baseId(id){ return (id && isAcctSuf(id.slice(-1))) ? id.slice(0,-1) : id; }
async function selUpdaterCmd(command, args={}) {
  if(selectedPcs.size===0){alert('PC를 선택하세요');return;}
  // ══════════════════════════════════════════════════════════════════════
  // ★★업데이터 명령에도 확인창 (2026-08-28 실사고)★★
  //   주인님: "카드만 업데이트할것만 선택해서 업데이트를 눌럿는데
  //            ★선택안한것도 업데이트해버리네★"
  //   실측: 14:46:54~14:47:00 ★6초 안에 23대★ 에 update 가 나갔다(전부 acked).
  //   곧이어 start{rotate} 8건 — ★사냥 중인 PC 를 끊고 순환이 다시 시작시켰다.★
  //   ★원인은 단정 못 한다★(오클릭인지 선택 잔류인지). 확실한 것은
  //   `selCmd`·`rotCmd`·`autoIdleCmd` 는 전부 묻는데 ★여기만 안 물었다★ 는 것이다.
  //   「전체선택」 버튼이 바로 옆에 있고 selectedPcs 는 카드가 사라져도 남는다.
  //   update 는 매크로를 ★멈췄다 재시작★ 하므로 사냥이 끊긴다 — 물어야 할 일이다.
  // ══════════════════════════════════════════════════════════════════════
  {
    const NL = String.fromCharCode(10);
    const bases = [...new Set([...selectedPcs].map(id => baseId(id)))].sort();
    // 지금 사냥/콘텐츠를 도는 PC 는 ★따로 세어 앞에 보여준다★ — 끊기는 쪽이다
    const busyNow = bases.filter(b => Object.values(state).some(p =>
        baseId(p.pc_id||'') === b && AUTO_IDLE_BUSY.includes(p.status)));
    const LBL = {update:'업데이트 + 재시작', restart:'업데이터 재시작',
                 update_only:'업데이트(재시작 없음)'};
    const what = LBL[command] || command;
    const lines = bases.map(b => '  \u00b7 ' + b
        + (busyNow.includes(b) ? '   \u2190 ★지금 사냥/콘텐츠 중★' : ''));
    if (!confirm('\u2191 ' + what + ' \u2014 ★' + bases.length + '대★' + NL + NL
        + (busyNow.length
             ? '★' + busyNow.length + '대가 지금 사냥/콘텐츠 중입니다 \u2014 끊깁니다.★' + NL + NL
             : '')
        + '대상:' + NL + lines.join(NL) + NL + NL
        + (command === 'update'
             ? '업데이트는 매크로를 ★멈췄다 다시 켭니다.★ 진행 중인 사냥/던전이 끊깁니다.' + NL
             : '')
        + '이 목록이 맞습니까?')) return;
  }
  const sent=new Set(); const failed=[];
  for(const id of selectedPcs) {
    const b=baseId(id); if(sent.has(b))continue; sent.add(b);   // 같은 PC의 여러 계정 카드 중복 제거
    // ★★body 는 {command, args} 다 (2026-08-22)★★ — 옛 코드는 `{command,...args}` 로
    //   args 를 ★평평하게 펴서★ 보냈는데 서버는 body["args"] 만 읽는다(dashboard_send_updater_command).
    //   그래서 인자가 조용히 사라졌다. 'update' 는 인자가 없어 지금껏 안 터졌을 뿐이다.
    //   (§B3 의 set_info 를 {"kv": {...}} 로 감싸는 것과 정확히 같은 함정)
    let ok=false;
    try {
      const res = await fetch(`/updater/command/${b}`, {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({command, args})});
      ok = res.ok;
    } catch(e) { ok = false; }
    if(!ok) failed.push(b);
  }
  loadUpdHistory();
  const n=sent.size;
  // ★★응답을 보고 말한다 (2026-08-22 사고 146)★★
  //   옛 코드는 fetch 결과를 ★쳐다보지도 않고★ 무조건 성공 토스트를 띄웠다.
  //   401/500 이어도 주인님 눈에는 "✓ 22대 업데이터 update" 로 보였다 — §A2 위반.
  //   주인님: "내가 업데이트를 눌러도 뭐 업데이트를 안 하는데 우짜냐 이거"
  if(failed.length){
    showToast(`⛔ ${n}대 중 ${failed.length}대 전송 실패: ${failed.slice(0,5).join(', ')}${failed.length>5?'…':''}`);
  } else if(command === 'update'){
    // ★★서버가 ★지금 무슨 버전을 광고 중인지★ 를 같이 보여준다 (사고 146)★★
    //   릴리스 직후 ~10분은 /check 가 옛 버전을 광고한다(_version_cache 300초 + raw 엣지 캐시).
    //   그 창에서 누르면 서버가 exe_update 를 빼고 주고 업데이터는 '최신' 으로 조용히 끝낸다.
    //   버전을 눈으로 보면 "왜 안 올라가지" 를 1초에 판정할 수 있다.
    let tail = '';
    try {
      const h = await (await fetch('/health')).json();
      if (h && h.serving_exe) {
        const age = Math.round(h.version_cache_age_s || 0);
        tail = ` · 서버가 광고 중인 버전 ${h.serving_exe} (캐시 ${age}초 전)`;
      }
    } catch(e) {}
    showToast(`✓ ${n}대에 update 전송됨${tail}`);
  } else {
    showToast(`✓ ${n}대 업데이터 ${command} 전송됨 (선택 해제)`);
  }
  clearSelection();   // ★명령 전송 완료 = 선택 자동 해제 — 중복 명령 방지★
}

// ─── 슬롯 필터 토글 / 전체선택·해제 ─────────────────────────────────────────
async function selectAllSlots(pc_id, slots, enabled) {
  if (!slots.length) return;
  const filters = {};
  slots.forEach(s => { filters[String(s)] = enabled; });
  const res = await fetch(`/slot_filter/${pc_id}`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({filters})
  });
  if (res.ok) {
    if (!state[pc_id]) state[pc_id] = {};
    state[pc_id].slot_filters = filters;
    renderCharTable();
  } else {
    showToast('✗ 필터 저장 실패');
  }
}

async function toggleSlotFilter(pc_id, slot, enabled) {
  const current = (state[pc_id] || {}).slot_filters || {};
  const merged = {...current, [String(slot)]: enabled};
  const res = await fetch(`/slot_filter/${pc_id}`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({filters: merged})
  });
  if (!res.ok) showToast('✗ 필터 저장 실패');
}

// ─── 명령 전송 ────────────────────────────────────────────────────────────────
// ═══ 실시간 화면 보기 (2026-07-31) ═══════════════════════════════════════════
// ★열려 있는 동안만 흐른다★ — 열 때 live_on, 닫을 때 live_off. 그리고 서버는 조회가
// 15초 끊기면 매크로에게 204로 '그만'을 돌려주므로, 탭을 그냥 닫아도 알아서 멈춘다.
// (함대 20대가 공인IP 하나를 공유해서, 켠 줄 모르고 계속 흐르는 상태를 만들면 안 된다.)
let livePc = null, liveTimer = null, liveImg = null, liveFails = 0, liveArmedAt = 0;

async function openLive(pc) {
  // ★B-JS5 (2026-09-23)★ 안 도는 계정 카드로 열면 live_on 은 sendCmd 가 살아 있는 카드로 돌리는데
  //   화면은 /live/<누른 id> 를 당겨 영영 안 떴다. 처음부터 실제 대상 id 로 — live_off·beacon 도 이 id.
  pc = cmdTargetOf(pc);
  livePc = pc; liveFails = 0; liveArmedAt = Date.now();
  document.getElementById('liveTitle').textContent = pc + ' — 실시간 화면';
  document.getElementById('liveStep').textContent = '연결 중…';
  document.getElementById('liveModal').classList.remove('hidden');
  await sendCmd(pc, 'live_on');
  if (liveTimer) clearInterval(liveTimer);
  // ★350ms 폴링★ — 매크로가 3fps(0.35초/장)로 올리므로 여기도 같은 박자로 당긴다.
  //   1000ms로 두면 서버엔 새 프레임이 있는데 화면은 1fps로 보인다(실사용 "존나 느리다").
  liveTimer = setInterval(liveTick, 350);
  liveTick();
}

async function closeLive() {
  const pc = livePc;
  livePc = null;
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  document.getElementById('liveModal').classList.add('hidden');
  document.getElementById('liveShot').src = '';
  if (pc) await sendCmd(pc, 'live_off');
}

async function liveTick() {
  if (!livePc) return;
  const pc = livePc;
  // 이미지: 캐시 무력화용 타임스탬프. onerror로 '아직 프레임 없음'을 구분한다.
  const img = document.getElementById('liveShot');
  img.onerror = () => {
    liveFails++;
    if (liveFails === 3) document.getElementById('liveStep').textContent =
      '프레임 대기 중… (매크로가 실행 중이어야 합니다)';
  };
  img.onload = () => { liveFails = 0; drawLiveOverlay(); };
  img.src = '/live/' + encodeURIComponent(pc) + '.jpg?t=' + Date.now();
  try {
    const r = await fetch('/live/' + encodeURIComponent(pc) + '/meta', {credentials:'include'});
    const m = await r.json();
    window.__liveMeta = m;
    if (m.alive) {
      document.getElementById('liveStep').textContent =
        (m.step || '(단계 없음)') + '   ·   ' + m.age + '초 전';
    }
    // ★live_on 재무장★ — 서버는 조회가 15초 끊기면 매크로에 204를 줘 스트림을 끝낸다.
    //   그런데 크롬은 ★백그라운드(비활성) 탭의 setInterval을 1분에 1회로 조인다★.
    //   다른 탭을 잠깐 보고 돌아오면 그 사이 스트림이 죽어 있고, 대시보드는 live_on을
    //   다시 보내지 않아 영구 정지였다. 프레임이 늙었거나 계속 안 오면 다시 켜라고 보낸다.
    //   live.start()는 이미 돌고 있으면 no-op이라 중복 전송은 무해하다.
    const stale = (!m.alive) || (m.age > 8);
    if (stale && Date.now() - liveArmedAt > 10000) {
      liveArmedAt = Date.now();
      sendCmd(livePc, 'live_on');
    }
  } catch(e) {}
}

// 클릭 좌표를 프레임 위에 겹쳐 그린다.
// ★이게 진단의 핵심★ — 2026-07-31 회랑이 서버선택창에 맹클릭하던 걸 로그 정지로만
// 추론해야 했다. 점이 찍혔으면 "엉뚱한 화면 같은 자리를 계속 누른다"가 한눈에 보인다.
function drawLiveOverlay() {
  const img = document.getElementById('liveShot');
  const cv = document.getElementById('liveCanvas');
  const m = window.__liveMeta || {};
  if (!img.naturalWidth) return;
  cv.width = img.clientWidth; cv.height = img.clientHeight;
  const ctx = cv.getContext('2d');
  ctx.clearRect(0,0,cv.width,cv.height);
  const sw = m.src_w || 1280, sh = m.src_h || 720;
  (m.clicks || []).forEach(c => {
    const [x, y, ago] = c;
    const px = x / sw * cv.width, py = y / sh * cv.height;
    const fade = Math.max(0.15, 1 - ago / 6);       // 오래된 클릭일수록 흐리게
    ctx.beginPath(); ctx.arc(px, py, 9, 0, Math.PI*2);
    ctx.strokeStyle = 'rgba(255,60,60,' + fade + ')'; ctx.lineWidth = 2; ctx.stroke();
    ctx.beginPath(); ctx.arc(px, py, 2.5, 0, Math.PI*2);
    ctx.fillStyle = 'rgba(255,60,60,' + fade + ')'; ctx.fill();
  });
}

window.addEventListener('beforeunload', () => {
  // 탭을 닫아도 명령이 나가게 (실패해도 서버 15초 TTL이 받아준다)
  if (livePc) navigator.sendBeacon('/command/' + encodeURIComponent(livePc),
    new Blob([JSON.stringify({command:'live_off',args:{}})], {type:'application/json'}));
});

async function sendCmd(pc_id, command, args={}) {
  // ★★사고 307 (2026-08-28 주인님 지시) — 아무 카드에서 눌러도 되게 한다★★
  //   주인님: "메인카드에서 오른쪽 클릭해서 카드바꾸면 바뀌긴하는데 ★다른 계정
  //            카드에서는 바뀌기 해도 안바뀌는지 의심된다★ 만약에 그런거면
  //            ★아무카드에서도 바뀌게 해줘★"
  //
  //   ★맞는 의심이었다.★ 매크로는 ★물리 PC 당 한 대★ 만 돌고, 자기 정체성 pc_id
  //   로만 명령을 가져간다. 그래서 활성이 아닌 카드(PC-20d 등)로 보낸 명령은
  //   ★아무도 안 가져가고 15분 뒤 만료★ 된다. 예전 코드는 그걸 ★토스트로 막기만★
  //   했는데(2026-08-15), 막는 것은 '왜 안 됐는지' 를 알려줄 뿐 ★일을 해주지는 않는다★.
  //
  //   ★고침★ 같은 물리 PC 에서 ★실제로 살아 있는 카드★ 로 자동으로 돌린다.
  //   매크로 명령은 어차피 그 한 대가 받아야 하는 것이고, 사람이 어느 카드에서
  //   눌렀는지는 ★의도와 무관한 우연★ 이다. 돌렸다는 사실은 토스트로 알린다
  //   (조용히 바꾸면 그것대로 §A2 위반이다).
  const _base307 = baseId(pc_id);
  const _st307 = ((state[pc_id]||{}).status) || 'offline';
  const _on307 = (STATUS_CFG[_st307]||STATUS_CFG.offline).online;
  if (!_on307) {
    const _live307 = liveCardOf(_base307);
    if (_live307 && _live307.pc_id && _live307.pc_id !== pc_id) {
      showToast(`↪ ${pc_id} 는 지금 안 도는 카드라 ★${_live307.pc_id}★ 로 보냅니다`);
      pc_id = _live307.pc_id;
    } else if (_st307 === 'other_account' || _st307 === 'no_account') {
      showToast(`⚠️ ${_base307} 에 도는 매크로가 없습니다 — 먼저 켜 주세요`);
      return false;
    }
  }
  // ★★사고 308-b — 「또 누르기」 는 막지 말고 ★묻는다★★★
  //   주인님: "내가 실수로 '어? 내가 안눌럿나?' 이러면서 또 누르는 경우가 있었거든"
  //   ★완전 차단은 안 한다★ — 정말 다시 보내야 할 때가 있다(매크로가 씹은 경우).
  //   ★일괄 전송 대응★ bulkCmd/selCmd/rotCmd 는 Promise.all 로 24대에 동시에 부른다.
  //   confirm 이 24번 뜨면 그게 더 나쁘므로 ★3초 안의 같은 명령은 첫 답을 재사용★ 한다
  //   (전부 같은 tick 에서 fetch 앞까지 동기로 달리므로 첫 답이 나머지에 그대로 적용된다).
  const _pk308 = baseId(pc_id);
  const _prev308 = pendingCmds[_pk308];
  //   ★_bulkDepth>0 = 사람이 일괄을 의도적으로 눌렀다★ — 카드마다 되묻지 않는다(치명2)
  if (_bulkDepth === 0 && _prev308 && _prev308.cmd === command && _prev308.phase !== 'warn') {
    if (_pendDupMemo.cmd === command && Date.now() - _pendDupMemo.at < 3000) {
      if (!_pendDupMemo.yes) return false;
    } else {
      const _yes308 = confirm(`⚠️ ${_pk308} 에 「${_prev308.label}」 을 ★${pendSecs(_prev308)}초 전★ 에 이미 보냈습니다.\n\n`
        + `아직 매크로 응답을 기다리는 중입니다.\n`
        + `(▶시작은 캐릭 선택 화면 진입까지 있어서 ★사냥 중으로 바뀌는 데 최대 3분★ 걸립니다)\n\n`
        + `그래도 한 번 더 보낼까요?`);
      _pendDupMemo = {cmd: command, at: Date.now(), yes: _yes308};
      if (!_yes308) return false;
    }
  }
  // ★★계정 자동순환 무장 신호 (2026-08-20)★★
  //   "시작을 눌러줫을때만 그작업을 하면되고" — 방아쇠는 ★사람이 누른 이 버튼★ 이다.
  //   ★왜 서버가 command=='start' 만 보면 안 되나★ /command 로 start 를 쏘는 건 이
  //   버튼만이 아니다 — 운영 스크립트(deploy_to.py·start_idle.py·up_and_start.py …)가
  //   전부 쓴다. 그러면 알람 조치로 한 대를 재개시키는 것만으로 순환까지 켜지고,
  //   up_and_start.py 한 번이면 함대 전체가 무장된다(CLAUDE.md A7 우회).
  //   → ★대시보드에서 나가는 start 에만★ 이 표시를 싣는다. 여기가 단일 초크포인트다.
  if (command === 'start') args = Object.assign({rotate: true}, args);
  // ★★사고 308-b — 누른 ★즉시★ 카드에 띄운다 (낙관적 표시)★★
  //   ★보내기 전★ 에 걸어야 체감이 0.1초가 된다. 서버가 200 을 안 주면 바로 거둬낸다(아래).
  //   ★무엇으로 해제되는지는 파일 위쪽 pendingCmds 주석의 5겹 표에 있다★
  //   (①효과 관측 ②ack ③서버 만료·취소 ④ttl ⑤사람 클릭).
  const _pend308 = pendRegister(pc_id, command, args);
  if (_pend308) scheduleRenderNow();
  // ★★적대검증 치명1 — fetch 는 ★던진다★ (서버 다운·Railway 재배포·와이파이 끊김)★★
  //   예전엔 try 가 없어서 예외가 그대로 위로 튀었고, 표시는 ★fetch 앞★ 에 이미
  //   등록돼 있어 3분 ⏳ + 5분 ⚠ = ★8분짜리 유령★ 이 남았다.
  //   명령은 브라우저 밖으로 한 번도 안 나갔는데 카드는 「수행 중」을 보여준다 —
  //   ★주인님이 「어? 눌렀네」 하고 안 누르시고, 명령은 존재하지 않는다.★
  //   게다가 그 3분 동안 「또 누르기」 가드가 재시도까지 막았고,
  //   안내가 가리키는 「최근 명령 내역」에는 그 줄이 아예 없었다.
  //   부수 피해: bulkCmd/selCmd 의 Promise.all 이 그 예외로 reject 돼
  //   토스트·loadCmdHistory·clearSelection 이 통째로 스킵됐다.
  //   → 여기서 잡고 ★표시를 즉시 거두고 사람에게 말한다.★
  let res;
  try {
    res = await fetch(`/command/${pc_id}`,{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({command,args})});
  } catch (e308) {
    if (_pend308 && pendingCmds[_pend308.base] === _pend308) {
      delete pendingCmds[_pend308.base]; scheduleRenderNow();
    }
    showToast(`⛔ ${pc_id} 「${command}」 전송 실패 — 서버에 못 닿았습니다`
              + ` (${(e308 && e308.message) ? e308.message : e308})`);
    return false;
  }
  // ★무장이 거부되면 반드시 말한다★ — start 자체는 200 이라 화면엔 아무 표시가 없었다.
  //   순환이 안 켜진 걸 사람이 알 방법이 카드의 🔁 배지 '없음' 뿐이면 아무도 못 알아챈다.
  // ★rotate 가 실린 모든 명령을 본다 (2026-09-12)★ — 예전엔 start 만 봐서, [🔁 전 계정 순환]
  //   으로 보낸 회랑·일일던전이 24대 전부 거부돼도 「시작」 토스트만 떴다.
  //   그리고 `=== false` 대신 `'armed' in j` — 서버가 예외로 armed 를 빼먹던 시절의
  //   「가장 나쁜 경우가 가장 조용한」 모양을 화면 쪽에서도 막는다.
  if (command === 'start' || (args && args.rotate)) {
    try {
      const j = await res.clone().json();
      if (j && ('armed' in j) && !j.armed) showToast(`⚠️ ${pc_id} 「${command}」 — 계정 순환은 무장 안 됨 (${j.why||''})`);
    } catch(e) {}
  }
  // ★사고 308-b — 서버가 주는 cmd_id 를 회수한다★
  //   /command 응답은 예전부터 {ok, id, ws, armed} 를 줬는데 여기서 ★버리고 있었다★.
  //   id 가 있어야 cmd_history 의 ack/만료/취소를 ★그 명령 한 건★ 에 맞출 수 있다.
  //   (res.clone() 은 원본 body 를 안 먹으므로 위 armed 검사와 같이 써도 안전하다)
  if (_pend308) {
    if (res.ok) {
      let _j308 = null;
      try { _j308 = await res.clone().json(); } catch(e) {}
      _pend308.id = (_j308 && typeof _j308.id !== 'undefined') ? _j308.id : null;
      _pend308.idResolved = true;   // 이 뒤에야 (pc_id+command) 폴백 매칭을 허용한다
    } else if (pendingCmds[_pend308.base] === _pend308) {
      // ★명령이 안 나갔으면 표시도 즉시 거둔다★ — 유령 표시의 첫 번째 원인이 이것이다.
      delete pendingCmds[_pend308.base]; scheduleRenderNow();
    }
  }
  return res.ok;
}

async function bulkCmd(command, args={}) {
  // ★★어떤 ▶시작이든 순환을 무장한다 (2026-08-21 주인님 지시)★★
  //   원문: "야 그러면 내가 오늘 새벽에도 전체를 선택하고 시작을해서 무장이 안되서
  //          순환이 안됐다는얘기잖아 뭔시작을눌러도 순환하게 바꿔나"
  //   ★무슨 일이 있었나★ 예전엔 여기서 rotate:false 를 실어 일괄/다중선택 start 가
  //   순환을 ★일부러 껐다.★ A7("한 클릭으로 함대 전원 무장")을 막으려던 것이었는데,
  //   그 결과 2026-08-21 새벽 주인님이 전체선택 → ▶시작을 누르셨을 때 함대 전원이
  //   ★무장 없이★ 돌았고, 완주한 13대가 정보수집도 계정전환도 못 한 채 8시간을 섰다.
  //   주인님이 허용목록을 '*' 로 전체 개방하셨으므로 그 방어의 근거도 사라졌다.
  //   → sendCmd 의 rotate:true 를 그대로 통과시킨다(여기서 덮어쓰지 않는다).
  // ★B-JS4·B-JS8 (2026-09-23)★ 가짜·은퇴·계정없음 제외 + 물리 PC 당 1건(오프라인 형제가 살아 있는 카드로
  //   우회돼 같은 매크로에 최대 5건이 쌓였다). 토스트 숫자도 물리 PC 수.
  const ids=cmdTargets(Object.keys(state).filter(id => !isExcludedPc(id)));
  if(!ids.length){showToast('연결된 PC 없음');return;}
  await withBulk(() => Promise.all(ids.map(id=>sendCmd(id,command,args))));
  showToast(`✓ ${command} → 전체 ${new Set(ids.map(baseId)).size}대`);
  loadCmdHistory();
}

// ★★[폐기됨] 일괄/다중선택 start 는 순환을 무장하지 않는다 (2026-08-20 → 2026-08-21 철회)★★
//   옛 근거: selCmd 는 '전체선택 → ▶시작' 한 클릭으로 함대 전원을 태우니 A7 위반이다.
//   ★왜 철회했나 (주인님 지시)★
//     "뭔시작을눌러도 순환하게 바꿔나"
//   그리고 그 방어가 실제로 만든 피해가 더 컸다 — 2026-08-21 새벽 전체선택 시작이
//   ★무장 없이★ 나가 완주한 13대가 정보수집·계정전환 없이 8시간을 섰다.
//   허용목록도 '*' 로 전체 개방됐으므로 '몰래 전원 무장' 이라는 우려 자체가 없어졌다.
//   ★A7 은 여전히 유효하다★ — 다만 그건 ★사람이 버튼을 누르는 것★ 이 아니라
//   ★내가 스크립트로 쏘는 것★ 을 막는 규칙이다(a7guard.py). 여기는 사람의 클릭이다.
async function selCmd(command, args={}) {
  if(!selectedPcs.size){alert('PC를 선택하세요');return;}
  const n = selectedBases().length;      // ★사고 337★ 물리 PC 수로 묻는다
  // ★★2026-08-28 주인님 지적 — "저거 누르니까 진짜 전pc가 다 돌던데?"★★
  //   이 줄의 이름이 「선택한 카드 1개만」 인데 실제로는 ★선택된 카드 전부★ 에 보낸다.
  //   ('1개만' 은 "순환 없이 그 계정 1회" 라는 뜻이었지 "PC 1대" 가 아니었다)
  //   게다가 rotCmd 와 달리 ★확인창이 없어서★ 한 번 눌리면 그대로 나간다.
  //   실제 이력: collect_info 가 5대(PC-03b·09·16b·17·22)에 나갔다.
  //   ★이게 §A7 이 막으려는 모양이다★ — 한 번 눌러 함대가 움직이는데 아무도 안 묻는다.
  //   → 2대 이상이면 대상 이름을 보여주고 묻는다. 1대면 예전처럼 바로 간다.
  if (n >= 2) {
    // ★사고 337★ 확인창도 ★물리 PC 이름★ 으로 접는다 — 예전엔 PC-22b·22c… 가
    //   그대로 나와서, 해제한 카드가 목록에 남은 것처럼 보였다(주인님 스샷).
    const names = selectedBases().join(', ');
    if (!confirm(`⚠️ ${command} 을 ★${n}대★ 에 보냅니다

${names}

` +
                 `(이 줄은 "순환 없이 1회" 라는 뜻이지 "PC 1대" 가 아닙니다)

진행할까요?`)) return;
  }
  // ★보내는 것은 안 바꾼다★ — 명령은 지금처럼 ★카드별★ 로 나간다(전체선택 때 이미 그랬다).
  //   바뀐 건 고르는 방법과 보여주는 숫자뿐이다(사고 337).
  // ★B-JS4 (2026-09-23)★ 실제 대상으로 접어서 보낸다 — 스택째 고르면(사고 337) 오프라인 형제마다
  //   sendCmd 가 살아 있는 카드로 돌려 같은 명령이 한 매크로에 여러 건 쌓였다.
  await withBulk(() => Promise.all(cmdTargets(selectedPcs).map(id=>sendCmd(id,command,args))));
  showToast(`✓ ${command} → 선택 ${n}대 (선택 해제됨)`);
  loadCmdHistory();
  clearSelection();   // ★명령 전송 완료 = 선택 자동 해제 — 같은 세트에 실수로 중복 명령 방지★
}

// ─── 전 계정 순환 (2026-08-23 주인님 지시) ──────────────────────────────────
// ★왜 selCmd 와 따로 있나★
//   주인님: "카드 오른쪽클릭해서 나오는 메뉴는 전부다 선택된 카드 … 그 계정에만 해당되는
//            일을 시키는거고 대시보드 위에 나와있는건 다들 순환구조 느낌"
//            "위쪽상단에 순환용이랑 선택카드만 하는거 두개로 나눠있는게 낫겟네"
//   selCmd 는 고른 카드 그 계정에 한 번 쏘고 끝이다. rotCmd 는 ★rotate:true★ 를 실어서
//   서버 순환 엔진을 무장시킨다 — 서버가 작업 끝을 보고 다음 계정으로 전환해 또 시킨다.
//
// ★물리 PC당 1건★ — 같은 PC 의 계정 카드가 여러 장 골라져도 매크로는 한 대뿐이다.
//   오프라인 카드로 보내면 아무도 안 가져가는 고아 명령이 되므로 살아있는 카드로 접는다
//   (switchAccountSelected · selUpdaterCmd 와 같은 이유·같은 방식).
const ROT_TASK_LABEL = {daily_dungeon:'일일던전', nightmare:'악몽', awakening:'각성',
                        corridor:'회랑', collect_info:'정보수집'};
// ═══════════════════════════════════════════════════════════════════════════
// ★★노는 PC 자동진행 (2026-08-25 주인님 지시)★★
//   "버튼 딱 누르면 아무것도 안하는 컴퓨터를 대상으로 전체 계정들 중에 할일
//    즉, 일일던전 악몽 회랑 같은거 해야할거 딱 자동진행하게 했으면좋겠어"
//
// ★대상 (주인님 확답)★ "완주한 pc들만 대상이 될수있겠지? 왜냐하면 완주안한애들은
//   사냥 계속하고잇을거니까 오프라인도 포함이지"
//   → 오늘 슬롯을 ★다 끝낸★ PC 만. 사냥/판매/수집/전환 중인 PC 는 건드리지 않는다.
//   → 오프라인도 포함한다. 명령은 큐에 남아 그 PC 가 돌아오면 가져간다.
//
// ★순서 (주인님 확답)★ "우선순위는 일일던전, 회랑, 악몽 순이겟지?"
//   서버 순환 엔진이 한 작업을 전 계정에 돌린 뒤 queue 에서 다음 작업을 꺼낸다.
//
// ★미리보기 후 확인 (주인님 확답)★ — 함대 전체가 움직이는 명령이라 무엇이 어디로
//   가는지 다 보여주고 확인을 받는다(§A7 정신).
// ═══════════════════════════════════════════════════════════════════════════
const AUTO_IDLE_TASKS = ['daily_dungeon', 'corridor', 'nightmare'];
// ★지금 뭘 하고 있는 상태★ — 여기 해당하면 자동으로는 손대지 않는다.
//   ★2026-08-28 정정★ 초판은 hunting/selling/collecting/switching/tasking/
//   reconnecting/starting/captcha 여덟 개뿐이었다. 그런데 STATUS_CFG 에는
//   moving(라벨이 '사냥 중'이다) · abyss · subquest · dungeon · nightmare ·
//   awakening · corridor · dead 도 있고 전부 ★지금 콘텐츠를 도는 중★ 이다.
//   빠져 있으면 「악몽 도는 중인 PC」 위에 순환을 또 걸어 작업이 겹친다.
//   (awakening_wait · nightmare_wait 는 서버 ROT_IDLE_SET 과 같이 '쉬는 자리' 로 본다)
//   ★'tasking'·'starting' 은 STATUS_CFG 에 없는 이름이라 뺐다 (2026-08-28 적대검증)★
//   있으면 AUTO_IDLE_DOING 이 라벨을 못 찾아 확인창에 ★"지금 tasking 중"★ 이 그대로 뜬다.
//   (STATUS_CFG 에 실제로 있는 키만 넣는다 — 아래 자기검사가 콘솔로 알려준다)
const AUTO_IDLE_BUSY = ['hunting','moving','selling','abyss','subquest','dead',
                        'dungeon','nightmare','awakening','corridor','surface','sealed_dungeon',   // #208 표층 = 세션 중
                        'collecting','switching','acct_switching','reconnecting','captcha'];   // ★사고 392★ 계정전환 중인 PC 를 깨우면 전환이 깨진다
// ★사람이 세워둔 것 / 사람이 와야 풀리는 것★ — 자동으로는 안 깨운다. 직접 고르면 예외.
//   ★awakening_wait · nightmare_wait 는 '노는 중' 이 아니다 (2026-08-28 매크로 소스 실측)★
//     · awakening_wait = 캐릭이 ★각성전 던전 안★ 에 서서 사람의 Scroll Lock 을 기다린다.
//       매크로가 daily_dungeon·nightmare 를 ★거부★ 한다(lc/loot.py:260 "세션 파괴").
//     · nightmare_wait = 악몽 최종보스를 ★사람이 손으로★ 잡아야 한다(lc/nightmare.py:404 알람).
//   여기 없으면 자동진행이 그 PC 를 골라 거부 알림을 두 번 울리고,
//   거부 목록에 없는 회랑은 ★진행 중인 각성전을 깬다.★
const AUTO_IDLE_HELD = ['paused', 'error', 'awakening_wait', 'nightmare_wait'];
// ★두 목록이 STATUS_CFG 와 어긋나면 확인창에 영어가 샌다★ — 첫 판이 'tasking'·'starting'
//   이라는 없는 키를 넣어 「지금 tasking 중」 이 뜰 뻔했다(적대검증이 잡음).
//   목록을 고칠 때 여기서 바로 콘솔로 알려준다.
try {
  const _bad = AUTO_IDLE_BUSY.concat(AUTO_IDLE_HELD).filter(k => !STATUS_CFG[k]);
  if (_bad.length) console.warn('[자동진행] STATUS_CFG 에 없는 상태:', _bad);
} catch(e) {}
const AUTO_IDLE_STLABEL = st => (STATUS_CFG[st] || {}).label || st || '오프라인';
// ★라벨 뒤에 '중' 을 또 붙이지 않는다★ — STATUS_CFG 라벨이 이미 '사냥 중'·'판매 중'
//   이라 그대로 이으면 「지금 ★사냥 중 중★」 이 된다(첫 판이 실측으로 그랬다).
const AUTO_IDLE_DOING = st => {
  const l = AUTO_IDLE_STLABEL(st);
  return (l.slice(-1) === '중') ? l : (l + ' 중');
};

// ═══════════════════════════════════════════════════════════════════════════
// ★★대상 고르기 — 2026-08-28 주인님 지시로 두 갈래가 됐다★★
//   "내가 ★특정 pc를 선택하고★ 저버튼을 누르면 그 pc만 자동진행 진행하고
//    만약에 ★아무것도 체크안하고★ 누르면 그냥 ★사냥 안하고있는애들★ 진행하면되고"
//
//   ① 선택 모드 — 고른 카드의 물리 PC 만. ★조건을 안 따진다★(주인님이 직접 지목한
//      것이므로). 대신 사냥 중·일시정지·이미 무장됨은 ★확인창에 경고로 전부 띄운다.★
//   ② 무선택 모드 — 함대 전체에서 '지금 사냥/콘텐츠를 안 도는' PC.
//      ★완주 조건을 뺐다★ — 예전 판은 '오늘 슬롯을 다 끝낸 PC' 만 골랐는데
//      주인님의 이번 문장은 「사냥 안 하고 있는 애들」 이다. 완주 여부는 확인창에
//      "완주 3/6" 으로 ★보여주기만★ 한다(조용히 빼지 않는다 — §A2).
//
//   ★제외한 PC 는 사유와 함께 돌려준다★ — 조용히 빼면 "왜 얘는 안 갔지" 가 된다.
// ═══════════════════════════════════════════════════════════════════════════
function autoIdleTargets(){
  const selBases = new Set([...selectedPcs].map(id => baseId(id)).filter(Boolean));
  const picked = selBases.size > 0;
  const byBase = {};
  for (const p of Object.values(state)) {
    const b = baseId(p.pc_id || '');
    if (!b) continue;
    (byBase[b] = byBase[b] || []).push(p);
  }
  const out = [], skip = [];
  // ★고른 카드가 지금 state 에 없으면 조용히 사라진다 (적대검증 [중5])★
  //   selectedPcs 는 WS 가 state 를 통째로 갈아엎어도 그대로 남는다. 그래서 사라진
  //   카드를 고른 상태가 실제로 생기는데, 그때 "대상이 없습니다" 만 뜨고 이유가 없었다.
  if (picked) {
    for (const b of selBases) {
      if (!Object.values(state).some(p => baseId(p.pc_id||'') === b)) {
        skip.push({base:b, why:'지금 화면에 그 PC 카드가 없습니다 (선택이 낡았습니다)'});
      }
    }
  }
  for (const b of Object.keys(byBase).sort()) {
    const cards = byBase[b];
    if (isFakePc(b)) {
      if (picked && selBases.has(b)) skip.push({base:b, why:'검증용 가짜 PC — 순환 대상이 아님'});
      continue;
    }
    if (picked && !selBases.has(b)) continue;             // 고른 것만
    const live = liveCardOf(b);
    // 판정 카드 = 온라인 카드, 없으면 ★가장 최근에 살아 있던★ 카드.
    //   other_account/no_account 카드는 뒤로 민다 — sendCmd 가 그 카드를 거부한다.
    const cur = live || cards.slice().sort((x,y) =>
        ((x.status === 'other_account' || x.status === 'no_account') -
         (y.status === 'other_account' || y.status === 'no_account')) ||
        String(y.last_active||'').localeCompare(String(x.last_active||'')))[0];
    if (!cur) continue;
    const dp = cur.daily_progress || [];
    const done = dp.filter(dpDone).length;
    const busySt = (cards.find(c => AUTO_IDLE_BUSY.includes(c.status)) || {}).status || '';
    const heldSt = (cards.find(c => AUTO_IDLE_HELD.includes(c.status)) || {}).status || '';
    const armed  = (cards.find(c => c._rot) || {})._rot || '';
    // ★★「사냥만 도는 순환」과 「할일까지 도는 순환」은 다르다 (2026-08-28 주인님)★★
    //   주인님: "내가 남은 할일 자동진행을 ★체크를 해서 하든 안해서하든★ 각 계정의
    //            할일들을 다하고 캐릭터들도 다하고 그냥 다음계정으로 넘어가서 또 하게끔"
    //   ★예전 문제★ 사냥 순환이 걸린 PC 를 「이미 무장됨」으로 통째로 제외했는데,
    //   그 순환은 사냥→수집→전환만 돌고 ★일일던전·회랑·악몽을 영영 안 한다.★
    //   그래서 그 PC 들은 자동진행 대상에서도 빠지고 스스로도 할 일을 안 했다.
    //   → 서버가 `_rot_full` 로 「할일까지 도는 순환인가」를 알려준다.
    //     사냥만 도는 순환이면 ★대상에 넣어 할 일을 얹는다.★
    const armedFull = !!(cards.find(c => c._rot_full) || null);
    if (!picked) {
      // 이미 ★할일까지★ 도는 순환이면 또 걸지 않는다 — 두 번 걸면 작업이 겹친다
      if (armedFull) { skip.push({base:b, why:'이미 할 일까지 도는 순환 중 (\u{1F501} ' + armed + ')'}); continue; }
      if (busySt) { skip.push({base:b, why:'지금 ' + AUTO_IDLE_DOING(busySt)}); continue; }
      if (heldSt) { skip.push({base:b, why:AUTO_IDLE_STLABEL(heldSt) + ' \u2014 ★사람이 와야 풀립니다★'}); continue; }
    }
    const warn = [];
    if (armedFull) warn.push('이미 할 일까지 도는 순환 중(\u{1F501} ' + armed + ') \u2014 ★덮어씁니다★');
    else if (armed) warn.push('지금 ★사냥만★ 도는 순환 중(\u{1F501} ' + armed + ') \u2014 여기에 할 일을 얹습니다');
    if (busySt) warn.push('★지금 ' + AUTO_IDLE_DOING(busySt) + '★ \u2014 끊고 시작합니다');
    if (heldSt) warn.push(AUTO_IDLE_STLABEL(heldSt) + ' 상태');
    out.push({base: b, id: cur.pc_id, off: !live,
              slots: dp.length, done: done,
              cur_status: cur.status || 'offline', warn: warn});
  }
  return {picked: picked, targets: out, skipped: skip};
}

// ★★사고 395 — ★자동순환을 켤 손잡이★★ (주인님 2026-09-01)
//   「지금 계정하나의 사냥 다끝나고 난뒤에 정보수집하는것도 없어진거같고」
//   실측 `rot_allow=''` = 카나리아 게이트가 아무도 통과 못 시킨다. ▶시작을 눌러도
//   「허용 목록이 비어 있음」으로 조용히 거부된다. 그런데 켜는 길이 API 뿐이었다.
//   ★지금 값을 먼저 보여준다★ — 「내가 뭘 바꾸는지」를 모르고 누르게 하지 않는다.
// ★사고 395★ 자동순환이 켜져 있는지 ★버튼 라벨에서 바로★ 보이게 한다.
//   꺼져 있으면 붉게 — 「기능이 통째로 죽어 있는데 아무도 모르는」 상태를 만들지 않는다.
async function rotAllowBadge(){
  const b = document.querySelector('button[onclick="rotAllowDlg()"]');
  if (!b) return;
  try {
    const j = await (await fetch('/rotate', {credentials:'same-origin'})).json();
    const al = j.allow || [], armed = Object.keys(j.rotating || {}).length;
    let tag;
    if (!al.length)              tag = '★꺼짐★';
    else if (al.indexOf('*')>=0) tag = '전체';
    else                         tag = al.length + '대';
    b.textContent = '🔁 자동순환 ' + tag + (armed ? ' · 무장 ' + armed + '대' : '');
    // ★꺼져 있으면 색으로 말한다★ — 글자만으로는 안 읽힌다(실측: 며칠간 아무도 몰랐다)
    b.style.background = al.length ? '' : 'rgba(244,63,94,.25)';
    b.style.borderColor = al.length ? '' : '#f43f5e';
    b.style.color = al.length ? '' : '#fda4af';
  } catch(e) { /* ★못 읽었다고 라벨을 거짓으로 바꾸지 않는다★ — 그대로 둔다 */ }
}
async function rotAllowDlg(){
  let cur = '', armed = 0;
  try {
    const r = await fetch('/rotate', {credentials:'same-origin'});
    const j = await r.json();
    cur = (j.allow || []).join(',');
    armed = Object.keys(j.rotating || {}).length;
  } catch(e) {
    alert('현재 설정을 못 읽었습니다: ' + e);
    return;
  }
  const NL = String.fromCharCode(10);
  const now = cur ? cur : '(비어 있음 — 아무도 무장하지 못합니다)';
  const v = prompt(
    '🔁 자동순환을 허용할 PC' + NL + NL +
    '완주하면 자동으로 정보수집 → 다음 계정으로 넘어갑니다.' + NL +
    '실제로 걸리려면 그 PC 의 ▶시작을 눌러야 합니다(허용은 전제조건입니다).' + NL + NL +
    '지금 허용: ' + now + NL +
    '지금 무장된 PC: ' + armed + '대' + NL + NL +
    '쉼표로 나열하세요. 전부 허용은 *  ·  전부 끄려면 비워두세요.' + NL +
    '★한 대로 시작해 넓히는 것이 원칙입니다★',
    cur);
  if (v === null) return;                    // 취소
  const pcs = String(v).trim();
  if (pcs === '*' && !confirm('★전 함대★ 에 자동순환을 허용합니다. 계속할까요?')) return;
  try {
    const r = await fetch('/rotate/allow', {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({pcs: pcs})});
    const j = await r.json();
    if (!r.ok || !j.ok) { alert('실패: ' + JSON.stringify(j)); return; }
    alert('자동순환 허용: ' + (j.allow || '(비어 있음)'));
  } catch(e) {
    alert('저장 실패: ' + e);
  }
}
async function autoIdleCmd(){
  const r = autoIdleTargets();
  const tg = r.targets, NLx = String.fromCharCode(10);
  const names = AUTO_IDLE_TASKS.map(t => ROT_TASK_LABEL[t] || t).join(' → ');
  const skipTxt = r.skipped.length
    ? NLx + NLx + '제외 ' + r.skipped.length + '대:' + NLx +
      r.skipped.map(x => '  · ' + x.base + ' \u2014 ' + x.why).join(NLx)
    : '';
  if (!tg.length) {
    alert((r.picked
        ? '고르신 PC 중에 보낼 수 있는 대상이 없습니다.'
        : `지금 자동진행할 PC가 없습니다.

대상 조건: 지금 사냥·콘텐츠·판매·수집·전환 중이 아니고, 일시정지/에러도 아니며,
순환이 아직 무장되지 않은 PC (오프라인 포함).
★특정 PC 만 하려면 카드를 고르고 다시 눌러주세요★ — 그때는 조건을 안 따집니다.`) + skipTxt);
    return;
  }
  const on = tg.filter(t => !t.off).length;
  const off = tg.length - on;
  const lines = tg.map(t => {
    const prog = t.slots ? ('  완주 ' + t.done + '/' + t.slots) : '  완주정보 없음';
    const w = t.warn.length ? NLx + '      \u26a0 ' + t.warn.join(' / ') : '';
    return '  · ' + t.base + (t.off ? '  (오프라인 \u2014 돌아오면 실행)' : '') + prog + w;
  });
  if (!confirm(`⚡ 남은 할 일 자동진행 — ${tg.length}대  ${r.picked ? '(★고른 PC만★)' : '(자동 탐색)'}

할 일: ★사냥(전 캐릭 완주)★ → ${names} → 정보수집
범위 : 각 PC의 ★전 계정★ — ★한 계정에서 위를 전부 끝내고★ 다음 계정으로 넘어가 또 합니다

대상 (온라인 ${on} / 오프라인 ${off}):
${lines.join(NLx)}${skipTxt}

계정 하나 넘어갈 때마다 본컴 런처 + 원격컴 크롬 + 매크로 재시작 = 1~2분.
되돌리려면 각 PC에 ■정지를 눌러야 합니다.

진행할까요?`)) return;
  // ★★start + queue = full 순환★★ (2026-08-28 주인님)
  //   예전엔 첫 작업(일일던전)을 바로 보냈다 = 사냥을 건너뛰고 던전부터 돌았다.
  //   이제 ★사냥부터★ 시작해서 완주하면 그 계정에서 할 일을 펴고, 끝나면 정보수집,
  //   그다음 계정으로 넘어가 또 사냥부터 한다.
  // ★withBulk 로 감싼다★ — 안 감싸면 sendCmd 의 「또 누르셨습니까」 중복확인이
  //   대상 대수만큼 연달아 튀어나온다(rotCmd·selCmd 는 이미 감싸고 있었다).
  await withBulk(() => Promise.all(
      tg.map(t => sendCmd(t.id, 'start', {rotate: true, queue: AUTO_IDLE_TASKS}))));
  showToast(`⚡ ${tg.length}대 자동진행 시작 — ${names}`);
  loadCmdHistory();
  clearSelection();
}

async function rotCmd(command) {
  if(!selectedPcs.size){alert('PC를 선택하세요');return;}
  const label = ROT_TASK_LABEL[command] || command;
  const byBase = {};
  for (const id of selectedPcs) {
    const b = baseId(id);
    const on = !!((STATUS_CFG[(state[id]||{}).status]||STATUS_CFG.offline).online);
    if (!byBase[b] || (on && !byBase[b].on)) byBase[b] = {id, on};
  }
  const targets = Object.values(byBase).map(x => {
    const live = liveCardOf(baseId(x.id));
    return live ? live.pc_id : x.id;
  });
  if(!confirm(`${targets.length}대 → 🔁 전 계정 순환 「${label}」

` +
              `각 PC가 지금 계정에서 ${label} 을 하고, 끝나면 ★다음 계정으로 통짜 전환★ 해서 또 합니다.
` +
              `계정을 한 바퀴 다 돌면 자동으로 끝나고 텔레그램으로 알립니다.
` +
              `(계정 하나 넘어갈 때마다 본컴 런처 + 원격컴 크롬 + 매크로 재시작 = 1~2분)

` +
              `진행할까요?`)) return;
  // ★withBulk 으로 감싼다★ — 안 감싸면 sendCmd 의 「또 누르셨습니까」 중복확인이
  //   대상 대수만큼 연달아 튀어나온다. (2026-08-28 적대검증 [높3] — 주석은
  //   「이미 감싸고 있었다」고 적혀 있었는데 실제로는 안 감싸고 있었다 = §A4)
  await withBulk(() => Promise.all(targets.map(id=>sendCmd(id, command, {rotate:true}))));
  showToast(`🔁 ${targets.length}대 전 계정 순환 「${label}」 시작`);
  loadCmdHistory();
  clearSelection();   // 명령 전송 완료 = 선택 자동 해제 (selCmd 와 같은 규칙)
}

// ─── 멀티계정: 선택 PC 일괄 자동 전환 (2026-08-15 사용자: "멀티선택해서 한꺼번에") ───
// ★물리 PC당 1건★ — 같은 PC의 계정 카드가 여러 장 선택돼도(PC-03 + PC-03b) 매크로는
// 한 대뿐이다. 온라인 카드가 곧 수신자이므로 base별로 온라인 카드를 골라 거기로만 보낸다.
// (오프라인 카드로 보내면 아무도 안 가져가는 고아 명령 — selUpdaterCmd의 dedup과 같은 이유)
// ★2026-08-18: 다중선택 계정전환도 통짜★ — 예전엔 switch_account(원격컴 크롬만)라
//   본컴 런처가 옛 계정 그대로 남았다. 짝이 안 맞으면 스트림이 영영 안 뜬다.
//   → switch_launcher 로 통일(본컴 → 원격컴 → 재시작). peer_id·파섹 비번은 서버가 채운다.
async function switchAccountSelected() {
  if(!selectedPcs.size){alert('PC를 선택하세요');return;}
  const v = normAcct(prompt(
    `선택 PC들을 전환할 계정 번호 (1~${MAX_ACCT})\n1 = 본계정 / 나머지 = 부계정\n` +
    `★본컴 런처 → 원격컴 크롬 → 매크로 재시작★ 까지 각 PC가 자동으로 합니다\n` +
    `(info.txt 계정N_아이디/비번 + 파섹 주소록 peer_id 필요)`));
  if(!v) return;
  const n = acctNum(v);
  const byBase = {};
  for (const id of selectedPcs) {
    const b = baseId(id);
    const on = !!((STATUS_CFG[(state[id]||{}).status]||STATUS_CFG.offline).online);
    if (!byBase[b] || (on && !byBase[b].on)) byBase[b] = {id, on};
  }
  // ★이미 그 계정인 PC는 뺀다★ — 본컴을 괜히 한 번 더 돌리면 게임만 끊긴다
  const targets = [], already = [];
  for (const x of Object.values(byBase)) {
    const live = liveCardOf(baseId(x.id));
    const t = live ? live.pc_id : x.id;
    if ((((t.match(ACCT_SUF_RE)||[])[1]) || 'a') === v) { already.push(baseId(t)); continue; }
    targets.push(t);
  }
  if(!targets.length){ showToast(`선택한 PC는 이미 전부 계정 ${n} 입니다`); clearSelection(); return; }
  if(!confirm(`${targets.length}대 → 계정 ${n} 통짜 전환` +
              (already.length ? `\n(이미 계정 ${n} 인 ${already.length}대는 제외)` : '') +
              `\n\n① 본컴 런처 계정 교체 + 게임 실행 (파섹 경유)\n② 원격컴 크롬 로그인 교체\n③ 매크로 재시작\n\n` +
              `★대당 1~2분, 게임 세션 끊김★. 진행할까요?`))return;
  await withBulk(() => Promise.all(targets.map(id=>sendCmd(id,'switch_launcher',
        {acct_no:n, acct_index:1, acct_label:`계정${n}`, chrome_label:v}))));
  showToast(`🔁 ${targets.length}대 계정 ${n} 통짜 전환 시작 (결과는 텔레그램)`);
  loadCmdHistory();
  clearSelection();
}

// ─── ★전부 1번으로★ (2026-09-04 주인님 지시) ────────────────────────────────
//   원문: 「대시보드에 버튼하나 만들어 전부다 1번으로 라는 버튼만들고
//          ★계정1 아닌 컴퓨터들만★ 전환하도록하게 하는 기능이야」
//   ★선택 없이★ 함대 전체를 훑어 ★지금 계정1 이 아닌 물리 PC★ 만 골라 통짜 전환한다.
//   · 물리 PC당 1건 — 온라인 카드(=수신자)로만 보낸다 (switchAccountSelected 와 같은 규칙)
//   · 이미 계정1 인 PC 는 뺀다 — 본컴을 괜히 돌리면 게임만 끊긴다
//   · 오프라인 PC 도 뺀다 — 아무도 안 가져가는 고아 명령이 된다
//   ★A7★ 이건 ★사람이 누르는 버튼★ 이다(a7guard 는 내가 스크립트로 쏘는 것을 막는 규칙).
//     그래도 몇 대가 나가는지·사냥 중이 몇 대인지 ★세어서 보여주고★ 확인을 받는다.
async function switchAllToFirst() {
  const byBase = {};
  for (const id of Object.keys(state)) {
    const b = baseId(id);
    if (isFakePc(id)) continue;
    const on = !!((STATUS_CFG[(state[id]||{}).status]||STATUS_CFG.offline).online);
    if (!byBase[b] || (on && !byBase[b].on)) byBase[b] = {id, on};
  }
  // ★★주인님 지시 (2026-09-04) — 「뭐 하고 있는애들은 그냥 전환 안 시켜도돼」★★
  //   처음엔 사냥 중인 PC 도 대상에 넣고 「끊깁니다」 경고만 했다. 그게 아니라
  //   ★일하고 있으면 아예 건드리지 않는다.★
  //   ★놀고 있는 것만 고른다★ — 화이트리스트로 둔다(블랙리스트는 새 상태가 생기면 샌다.
  //   실제로 sealed_dungeon·wardrobe 가 그렇게 새서 「빨간 오프라인」으로 보였다, 사고 393).
  const FREE = new Set(['idle', 'paused']);
  const targets = [], already = [], offline = [], busy = [];
  for (const x of Object.values(byBase)) {
    const b = baseId(x.id);
    if (!x.on) { offline.push(b); continue; }
    const live = liveCardOf(b);
    const t = live ? live.pc_id : x.id;
    const suf = (((t.match(ACCT_SUF_RE)||[])[1]) || 'a');
    if (suf === 'a') { already.push(b); continue; }
    const st = (state[t]||{}).status || '';
    if (!FREE.has(st)) {                      // ★일하는 중이면 건너뛴다★
      busy.push(`${b}(${(STATUS_CFG[st]||{}).label || st || '?'})`);
      continue;
    }
    targets.push(t);
  }
  if (!targets.length) {
    showToast(`전환할 PC가 없습니다 — 이미 계정1: ${already.length}대`
              + (busy.length ? ` · 일하는 중 ${busy.length}대` : '')
              + (offline.length ? ` · 오프라인 ${offline.length}대` : ''));
    return;
  }
  if (!confirm(`★${targets.length}대★ 를 계정1 로 통짜 전환합니다

`
      + `대상: ${targets.join(', ')}

`
      + (already.length ? `이미 계정1 (제외): ${already.length}대
` : '')
      + (offline.length ? `오프라인 (제외): ${offline.length}대
` : '')
      + (busy.length ? `일하는 중이라 ★건너뜁니다★: ${busy.join(', ')}
` : '')
      + `
① 본컴 런처 계정 교체 + 게임 실행 (파섹 경유)
`
      + `② 원격컴 크롬 로그인 교체
③ 매크로 재시작

`
      + `★대당 2~4분, 게임 세션 끊김★. 진행할까요?`)) return;
  await withBulk(() => Promise.all(targets.map(id => sendCmd(id, 'switch_launcher',
        {acct_no: 1, acct_index: 1, acct_label: '계정1', chrome_label: 'a'}))));
  showToast(`🔁 ${targets.length}대 → 계정1 통짜 전환 시작 (결과는 텔레그램)`);
  loadCmdHistory();
}

// ─── 판매(sell_all) — 거래소 지정가를 args.price로 전송 ─────────────────────────
// ★2026-08-14: 확정가의 정본을 localStorage → 서버 설정(/setting/sale_price)으로 이동★
// localStorage는 브라우저별이라 "사이트에서 바꿨는데 함대는 옛 가격" 혼선의 근원이었다.
// 이제 [확정]이 서버에 저장되고, 각 PC의 사냥종료 자동판매가 판매 시작마다 서버 확정가를
// 읽는다(sale.py _fetch_dashboard_price, v1.1.410+). localStorage는 서버 불통 시 폴백 캐시.
let salePriceServerOk=false;   // 이번 세션에 서버 확정가를 성공적으로 읽었는가
function getSalePrice() {
  const el=document.getElementById('sale-price');
  const v=parseInt((el&&el.value)||'0',10);
  return isNaN(v)?0:v;
}
function isSalePriceConfirmed(){ return salePriceServerOk || localStorage.getItem('sale_price_confirmed')==='1'; }
// ─── 각성전 난이도 프리셋 (2026-07-26) ────────────────────────────────────────
async function loadAwakenPreset(){
  try{
    const r=await fetch('/setting/awakening_preset');
    if(!r.ok)return;
    const v=(await r.json()).value||'default';
    const el=document.getElementById('awaken-preset');
    if(el)el.value=(v==='hard_up')?'hard_up':'default';
  }catch(e){}
}
async function setAwakenPreset(v){
  try{
    const r=await fetch('/setting/awakening_preset',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({value:v})});
    showToast(r.ok?(v==='hard_up'?'✓ 각성 난이도: 어려움→극한 (다음 입장부터)':'✓ 각성 난이도: 기본(자동)')
                  :'✗ 프리셋 저장 실패');
  }catch(e){showToast('✗ 프리셋 저장 실패');}
}

async function loadSalePrice() {
  const el=document.getElementById('sale-price'), btn=document.getElementById('sale-price-btn');
  if(!el||!btn) return;
  // 서버 확정가 1순위 — 어느 브라우저에서 열어도 같은 값(=함대가 실제로 쓰는 값)을 보여준다
  let v='';
  try{
    const r=await fetch('/setting/sale_price');
    if(r.ok){ v=String((await r.json()).value||'').trim(); if(v)salePriceServerOk=true; }
  }catch(e){}
  if(!v) v=localStorage.getItem('sale_price')||'';   // 서버 불통/미설정 → 옛 로컬 캐시
  if(v) el.value=v;                       // 프리셋에 없는 옛 저장값이면 select가 빈 값으로 남는다(재확정 유도)
  if(isSalePriceConfirmed()&&el.value){ el.disabled=true; el.classList.add('opacity-60'); btn.textContent='수정'; }
  else { el.disabled=false; el.classList.remove('opacity-60'); btn.textContent='확정'; }
}
async function toggleSalePrice() {
  const el=document.getElementById('sale-price'), btn=document.getElementById('sale-price-btn');
  if(isSalePriceConfirmed()){
    // 수정 모드 진입 — 서버 확정가 자체는 그대로 살아 있다(재확정 전까지 함대는 기존가 유지)
    salePriceServerOk=false;
    localStorage.setItem('sale_price_confirmed','0');
    el.disabled=false; el.classList.remove('opacity-60'); btn.textContent='확정'; el.focus();
  } else {
    const p=parseInt(el.value||'0',10);
    if(!p||p<=0){alert('거래소 가격을 선택하세요');return;}
    // ★서버에 저장해야 확정 — 실패하면 확정으로 치지 않는다(함대에 안 갔는데 갔다고 보이면 안 됨)★
    try{
      const r=await fetch('/setting/sale_price',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({value:String(p)})});
      if(!r.ok){showToast('✗ 가격 저장 실패 — 다시 시도하세요');return;}
    }catch(e){showToast('✗ 가격 저장 실패 — 다시 시도하세요');return;}
    salePriceServerOk=true;
    localStorage.setItem('sale_price', String(p));
    localStorage.setItem('sale_price_confirmed','1');
    el.disabled=true; el.classList.add('opacity-60'); btn.textContent='수정';
    showToast(`거래소 가격 확정: ${p.toLocaleString()} — 전 함대 다음 판매부터 자동 적용`);
  }
}
async function sellAllSel() {
  const p=getSalePrice();
  if(p<=0||!isSalePriceConfirmed()){alert('먼저 거래소 가격을 입력하고 [확정] 하세요');return;}
  if(!selectedPcs.size){alert('PC를 선택하세요');return;}
  if(!confirm(`선택 ${selectedBases().length}대 판매 실행\n거래소 지정가: ${p.toLocaleString()}`))return;   // ★B-JS4★ 물리 PC 수
  await selCmd('sell_all',{price:p});
}
async function sellAllCard(pc) {
  const p=getSalePrice();
  if(p<=0||!isSalePriceConfirmed()){alert('먼저 상단 거래소 가격을 입력하고 [확정] 하세요');return;}
  if(!confirm(`${pc} 판매 실행\n거래소 지정가: ${p.toLocaleString()}`))return;
  closeCardMenu();
  const ok=await sendCmd(pc,'sell_all',{price:p});
  showToast(ok?`✓ 판매 → ${pc} (거래소가 ${p.toLocaleString()})`:`✗ 판매 전송 실패`);
  loadCmdHistory();
}

// ─── 카드 메뉴 ────────────────────────────────────────────────────────────────
function openCardMenu(pc_id, e) {
  e.stopPropagation();
  const menu=document.getElementById('card-menu');
  // 같은 카드 다시 클릭 → 메뉴 닫기
  if(menuPcId===pc_id && !menu.classList.contains('hidden')) {
    closeCardMenu();
    return;
  }
  menuPcId=pc_id;
  // 헤더에 실시간 상태 + 매크로 버전 표시 (메뉴 v2 — 열 때마다 state에서 스냅샷)
  const pc=state[pc_id]||{};
  const cfg=STATUS_CFG[pc.status]||STATUS_CFG.offline;
  const ver=pc.macro_version?`v${esc(pc.macro_version)}`:'';   // ★B-JS3 (2026-09-23)★ 매크로 보고값 → esc
  const _mAcct = isMultiAcct(pc_id) ? ` <span class="text-purple-300" style="font-size:11px">계정 ${esc(acctNumOf(pc_id))}</span>` : '';
  document.getElementById('menu-pc-label').innerHTML=
    `<span class="font-bold text-gray-100">${baseId(pc_id)}${_mAcct}</span>`+
    `<span class="inline-flex items-center gap-1 ${cfg.text}" style="font-size:11px"><span class="w-2 h-2 rounded-full ${cfg.badge}"></span>${cfg.label}</span>`+
    (ver?`<span class="text-gray-500 ml-auto" style="font-size:10px">${ver}</span>`:'');
  refreshAcctButtons(pc_id);   // 계정 버튼 활성/비활성 (MAX_ACCT 만큼, 있는 계정만, 현재 계정 ✓)
  refreshCardOnlyButtons(pc_id); // ★카드만★ 버튼 (본컴 안 건드림, 2026-08-27)
  refreshParsecButtons(pc_id); // 파섹 주소 없는 PC는 눌러도 소용없으니 흐리게
  menu.classList.remove('hidden');
  // ★★폭도 ★실측★ 한다 (2026-08-23 주인님 지적)★★
  //   원문: "카드에 오른쪽클릭할때 창을 넘어가서 가릴때가잇는데"
  //   ★내가 만든 회귀다★ — 여기 폭이 상수 246/250 으로 박혀 있었는데(옛 폭 238px),
  //   같은 날 패널을 238 → 360px 로 키우면서 이 숫자를 안 고쳤다. 약 120px 넘쳤다.
  //   ★바로 아래 주석이 "높이는 상수로 박지 말고 실측한다" 고 말하고 있는데
  //     폭에는 그게 적용이 안 돼 있었다★ — 같은 함정이 한 칸 옆에 남아 있던 것이다.
  //   offsetWidth 는 hidden 을 벗긴 뒤라 실제 값을 준다 → 앞으로 폭을 바꿔도 안 깨진다.
  const mw = menu.offsetWidth;
  let left = e.clientX;
  if(left + mw > window.innerWidth - 8) left = window.innerWidth - 8 - mw;
  if(left < 8) left = 8;
  // ★높이는 상수로 박지 말고 실측한다(2026-08-15 리뷰)★ — 예전엔 '472px' 같은 상수를
  //   손으로 적어뒀는데, 메뉴에 줄이 추가될 때마다 상수가 뒤처져서 화면 아래쪽 카드를 누르면
  //   맨 밑 버튼들(업데이트·삭제)이 화면 밖으로 나가 클릭도 스크롤도 안 됐다.
  //   hidden 을 벗긴 뒤라 offsetHeight 가 실제 값을 준다 → 앞으로 줄이 늘어도 안 깨진다.
  // ★메뉴가 화면보다 길면 스크롤 (2026-08-23)★ — 예전엔 top 을 8 로 밀어붙였는데,
  //   그래도 화면보다 길면 ★아래쪽 버튼이 잘려서 못 누른다.★ 잘리느니 스크롤이 낫다.
  menu.style.maxHeight = (window.innerHeight - 16) + 'px';
  menu.style.overflowY = 'auto';
  const mh = Math.min(menu.offsetHeight, window.innerHeight - 16);
  let top = e.clientY + 4;
  if(top + mh > window.innerHeight - 8) top = window.innerHeight - 8 - mh;
  if(top < 8) top = 8;
  menu.style.top=top+'px'; menu.style.left=left+'px';
}

function closeCardMenu(){
  document.getElementById('card-menu').classList.add('hidden');
  menuPcId=null;
}

async function cardCmd(command, args={}) {
  if(!menuPcId) return;
  await sendCmd(menuPcId,command,args);
  showToast(`✓ ${command} → ${menuPcId}`);
  loadCmdHistory();
  closeCardMenu();
}

function cardCmdSwitch() {
  const slot=prompt(`${menuPcId} — 전환할 슬롯 번호 (1~9):`, '1');
  if(slot===null){closeCardMenu();return;}
  const n=parseInt(slot);
  if(isNaN(n)||n<1||n>9){alert('1~9 사이 숫자를 입력하세요');return;}
  cardCmd('switch_char',{slot:n});
}

function openLogFromMenu(){const id=menuPcId; closeCardMenu(); openLogModal(id);}
function liveFromMenu(){const id=menuPcId; closeCardMenu(); openLive(id);}
function lanFromMenu(){
  const id=menuPcId, st=(state[id]||{}), u=st.lan_url||'';
  closeCardMenu();
  // ★내부망 서버가 안 열린 PC★ — 아직 구버전이거나 info.txt에 lan_prefix가 없거나
  //   내부망 랜선이 안 꽂힌 경우다. 조용히 아무 일도 안 하면 원인을 알 수 없으니 알려준다.
  if(!/^http:\/\/[\d.]+:\d+\/\?k=/.test(u)){
    // ★★사고 384 (주인님 지시) — ★매크로가 죽어도 화면은 봐야 한다★★
    //   주인님: 「오프라인되면 볼수가없네」
    //   매크로 화면서버(8765)는 매크로 안에 있어 같이 죽는다. 그런데 화면이
    //   ★제일 필요한 순간이 바로 그때★ 다. 업데이터는 살아 있으므로
    //   보기 전용 서버(8767)를 열어두고, 여기서 그리로 폴백한다.
    //   ★보기만 된다★ — 조작은 8765(매크로)가 계속 맡는다.
    const v = st._view_url||'';
    if(/^http:\/\/[\d.]+:\d+\/frame\.jpg\?k=/.test(v)){
      showToast('매크로가 꺼져 있어 ★업데이터 보기 전용★ 화면을 엽니다 (조작은 안 됩니다)');
      window.open(v,'_blank');
      return;
    }
    showToast('내부망 주소 없음 — 매크로 v1.1.358+ 이고 info.txt에 lan_prefix= 가 있어야 합니다');
    return;
  }
  window.open(u,'_blank');
}

// ─── 파섹 원격 (2026-08-15) ───────────────────────────────────────────────────
// 두 갈래가 있고 성격이 완전히 다르다.
//
//   🌐 파섹 웹 : https://web.parsec.app/?peer_id=<ID>  를 ★새 탭★으로 연다.
//       ★다중 접속은 이쪽만 된다★ — 탭마다 독립 인스턴스(WASM+워커+WebRTC)라 여러 대를
//       동시에 띄울 수 있다. 관제컴에 설치할 게 없다. 크롬 전용·H.264 전용.
//       iframe으로는 못 넣는다(web.parsec.app 이 X-Frame-Options: DENY + frame-ancestors 'self').
//
//   🎮 파섹 앱 : parsec://peer_id=<ID>  — OS 프로토콜 핸들러라 ★이 브라우저를 띄운 PC★
//       (=관제컴)에 설치된 파섹이 열린다. 서버나 대상 PC가 뭘 실행하는 게 아니다.
//       화질·지연은 이쪽이 낫지만 ★창은 한 번에 하나★다: 파섹은 %APPDATA%\Parsec\lock_client
//       를 배타 잠금(CreateFile dwShareMode=0)으로 잡아 인스턴스를 1개만 허용하고, 두 번째
//       실행은 argv를 실행 중인 창에 넘기고 죽는다(2026-08-15 실측: 종료코드 0 + 그 창이
//       대상 PC로 갈아탐). 앱으로도 동시에 여러 대를 띄우려면 ★포터블 모드★를 써야 한다
//       (parsecd.exe + appdata.json + parsecd-<빌드>.dll 을 한 폴더에 두면 그 폴더에만 상태를
//       가둬서 잠금이 갈린다 — 파섹 공식 문서가 안내하는 방식. 폴더마다 로그인 1회 필요).
//
// ★★URI 꼬리 `&host_secret=&a=` 를 절대 빼지 말 것 (2026-08-15 실측으로 잡은 함정)★★
//   `parsec://peer_id=<ID>` 만 쓰면 ★1초 만에 -6107(peer 못 찾음)★ 로 죽는다. 가짜 peer_id를
//   넣었을 때와 완전히 같은 증상이라 원인을 찾기 어렵다.
//   원인: 셸/브라우저가 `scheme://authority` 를 `scheme://authority/` 로 ★정규화하면서 슬래시를
//   덧붙인다★ → peer_id 가 "<ID>/" 가 돼 조회에 실패한다.
//   파섹 자기 대시보드가 만드는 링크에 의미 없어 보이는 꼬리 `&a=` 가 붙어 있는 게 바로 이
//   슬래시를 받아내는 완충장치다(dash.parsec.app 번들:
//   `window.location.assign("parsec://peer_id="+e+"&host_secret="+t+"&a=")`).
//   실측 A/B: 꼬리 없음 → -6107 즉사 / 꼬리 있음 → status 20 정상 진행(명령줄 형식과 동일).
//   ※URI는 `&` 구분, 명령줄(parsecd.exe peer_id=x:client_vsync=1)은 `:` 구분 — 섞으면 안 된다.
//   host_secret 은 남의 PC에 붙는 공유용이라 내 PC엔 빈 값으로 둔다.
// ★peer_id 의 출처 = 서버 주소록(POST /parsec/map), ★매크로가 아니다★.
//   매크로 보고에 의존하면 매크로가 죽는 순간 파섹 버튼도 사라지는데, 원격으로 들어가 봐야
//   하는 때가 정확히 그때다(2026-08-15 사용자 지적). 서버가 주소록을 들고 있으므로 대상 PC가
//   꺼져 있어도 버튼은 살아 있다. 서버가 카드마다(부계정 카드 포함) 이미 채워 보내준다.
// ★형제 카드로 폴백하지 않는다★ — 서버가 번호로 카드마다(부계정 카드 포함) 이미 채워주므로
//   폴백은 불필요하고, 굳이 남겨두면 '내 카드엔 없는데 옆 카드 값으로 열어버리는' 추측이 된다.
//   엉뚱한 PC를 여느니 안 여는 게 낫다는 원칙 그대로.
function parsecPeerOf(id){
  return (((state[id]||{}).parsec_peer_id)||'').trim();
}

// 주소 없는 PC의 파섹 버튼은 흐리게 — 20대를 하나씩 눌러보게 만들지 않는다.
function refreshParsecButtons(pc_id){
  const has = !!parsecPeerOf(pc_id);
  for(const bid of ['cm-parsec-web','cm-parsec-app','cm-bonview']){
    const b=document.getElementById(bid);
    if(!b) continue;
    b.style.opacity = has ? '' : '0.35';
    b.title = has ? b.title : '이 PC는 파섹 주소가 없습니다 — 파섹 컴퓨터 이름을 번호로 바꾼 뒤 관제컴에서 parsec_multi.py push';
  }
}

// ★조용히 죽지 않는다★ — 내부망 버튼과 같은 원칙. 안 되면 왜 안 되는지 말해준다.
function _parsecPeerOrWarn(id){
  const pid=parsecPeerOf(id);
  if(!pid) showToast(`파섹 주소 없음 (${baseId(id)}) — 그 PC 파섹 이름을 번호로 바꾸고 관제컴에서 "parsec_multi.py push" 하세요`);
  return pid;
}

function parsecWebFromMenu(){
  const id=menuPcId; closeCardMenu();
  const pid=_parsecPeerOrWarn(id); if(!pid) return;
  // noopener — 새 탭이 opener.location 으로 이 대시보드 탭을 가짜 로그인 페이지로 바꿔치기하는
  //   경로를 끊는다(대시보드는 비번 로그인이라 피싱 표적이 된다). 반환값은 안 쓴다.
  window.open(`https://web.parsec.app/?peer_id=${encodeURIComponent(pid)}`,'_blank','noopener');
  showToast(`🌐 파섹 웹 → ${baseId(id)} (새 탭 — 탭을 닫으면 접속도 끝납니다)`);
}

// ★본컴 화면 받기(2026-08-16)★ — 위 [🌐 파섹 웹]과 ★주체가 다르다★.
//   파섹 웹  : 이 브라우저(관제컴)가 본컴에 붙는다 → 사람이 본다.
//   본컴 보기: ★원격컴의 크롬(CDP)★이 파섹 웹 탭을 열어 본컴에 붙고, CDP로 찍어
//              버그폴더에 떨군다 → 업데이터가 1분 내 업로드 → 여기 [🐞]에서 보인다.
//   후자가 런처 자동화의 진짜 경로다(조작 주체=원격컴, 본컴엔 설치 0).
//   peer_id 는 서버가 카드에 실어준 parsec_peer_id 를 그대로 args 로 넘긴다 —
//   매크로는 주소록을 조회하지 않는다(매크로↔파섹 분리 원칙).
async function bonComViewFromMenu(){
  const id=menuPcId; closeCardMenu();
  const pid=_parsecPeerOrWarn(id); if(!pid) return;
  const st=((state[id]||{}).status)||'';
  if(st==='hunting' && !confirm(`${id} 는 지금 사냥 중입니다.\n매크로가 거부할 수 있습니다. 그래도 보낼까요?`)) return;
  const ok=await sendCmd(id,'chrome_view',{
    url:`https://web.parsec.app/?peer_id=${encodeURIComponent(pid)}`,
    shots:3, gap:5, tag:'boncom', size:'1280,720'});
  showToast(ok?`🖥 ${id} → 본컴 화면 촬영 지시 (약 30초 뒤 🐞 버그에서 확인)`
              :`✗ ${id} 본컴 보기 명령 실패`);
}

// ★본컴 계정 전환(2026-08-16)★ — args 를 ★비워서★ 보낸다.
//   peer_id 와 파섹 아이디/비번은 ★서버가 배달 직전에 채운다★(enrich_cmd_args).
//   그래서 이 브라우저는 비번을 모르고, 명령 이력에도 '***' 로만 남는다.
// ★switchLauncherFromMenu 제거(2026-08-16)★ — [계정 1~4] 가 대체.
//   그 버튼은 '몇 번째 줄'만 물어 acct_no 가 안 실렸고 계정 오전환 사고를 냈다.

// ★파섹 자격증명 저장(2026-08-16)★ — 서버 설정에 넣어두면 20대가 공용으로 쓴다.
//   ★불러오지 않는다★ — 저장만 하고 화면에는 다시 안 띄운다(브라우저에 남기지 않으려고).
async function saveParsecCreds(){
  const idEl=document.getElementById('ps-id'), pwEl=document.getElementById('ps-pw');
  const pid=(idEl.value||'').trim(), ppw=pwEl.value||'';
  if(!pid && !ppw){ showToast('아이디/비번을 입력하세요'); return; }
  try{
    if(pid) await fetch('/setting/parsec_id',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:pid})});
    if(ppw) await fetch('/setting/parsec_pw',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({value:ppw})});
    pwEl.value='';                                  // 입력칸에서 즉시 지운다
    showToast('✓ 파섹 계정 저장됨 (전 PC 공용)');
  }catch(e){ showToast('✗ 저장 실패: '+e); }
}

// ─── 계정 세부정보 표 (2026-08-16) ────────────────────────────────────────────
// 각 PC info.txt 의 계정N_아이디/이메일/휴대폰 을 PC×계정 표로 편다.
// ★한 물리 PC의 값이 형제 카드에 흩어져 있다★ — 지금 돌고 있는 계정의 매크로가 info.txt
//   전체를 보고하는데, 계정 전환 직후엔 카드마다 최신도가 다르다. base 로 묶어 합친다
//   (먼저 찾은 비지 않은 값 우선). 안 그러면 계정2 카드가 현역일 때 계정1 줄이 빈다.
function acctRows(){
  const byBase = {};
  Object.values(state).forEach(p=>{
    const b = baseId(p.pc_id||''); if(!b) return;
    const m = byBase[b] || (byBase[b] = {ids:{}, emails:{}, phones:{}, plats:{}});
    [['acct_ids','ids'],['acct_emails','emails'],['acct_phones','phones'],
     ['acct_platforms','plats']].forEach(([src,dst])=>{
      const o = p[src] || {};
      for(const k in o){ if(o[k] && !m[dst][k]) m[dst][k] = o[k]; }
    });
  });
  const rows = [];
  Object.keys(byBase).forEach(b=>{
    const m = byBase[b];
    for(let n=1; n<=MAX_ACCT; n++){
      const k = String(n);
      const id = m.ids[k]||'', em = m.emails[k]||'', ph = m.phones[k]||'', pl = m.plats[k]||'';
      if(!id && !em && !ph && !pl) continue;   // 아무것도 안 적은 계정은 줄을 만들지 않는다
      rows.push({pc:b, n, pl, id, em, ph});
    }
  });
  const num = s => { const mm = String(s).match(/(\d+)/); return mm ? parseInt(mm[1]) : 9999; };
  rows.sort((a,b)=> num(a.pc)-num(b.pc) || a.n-b.n);
  return rows;
}

// ★칸 하나 = 클릭하면 그 값만 복사(2026-08-16 사용자 지시)★
//   값을 인라인 onclick 에 넣으면 따옴표·역슬래시가 든 값에서 깨진다. data 속성 + 위임으로 받는다.
function acctCell(val, cls, ph){
  if(!val) return `<td class="acct-td ${cls} text-gray-700/70">${ph || '—'}</td>`;
  return `<td class="acct-td acct-cp ${cls}" data-v="${esc(val)}" title="클릭하면 복사: ${esc(val)}">`
       + `<span class="acct-val">${esc(val)}</span></td>`;
}

function renderAcctTable(){
  const rows = acctRows(), el = document.getElementById('acct-table');
  const cnt = document.getElementById('acct-count');
  if(!rows.length){
    if(cnt) cnt.textContent = '';
    el.innerHTML = '<div class="text-gray-500 py-10 text-center">아직 올라온 계정 정보가 없습니다.'
      + '<br><span class="text-xs">각 PC의 info.txt 에 계정N_플랫폼 / 계정N_아이디 / 계정N_이메일 / 계정N_휴대폰 을 채우면 여기에 나옵니다.</span></div>';
    return;
  }
  const pcs = new Set(rows.map(r=>r.pc));
  if(cnt) cnt.textContent = `${pcs.size}대 · 계정 ${rows.length}개`;

  // PC가 바뀌는 줄에만 PC명을 찍고 위쪽에 구분선 — 20대가 쌓여도 덩어리로 읽힌다
  let prev = null;
  const body = rows.map(r=>{
    const head = (r.pc !== prev); prev = r.pc;
    return `<tr class="acct-row${head ? ' acct-group' : ''}">`
      + `<td class="acct-td acct-pc">${head ? esc(r.pc) : ''}</td>`
      + `<td class="acct-td acct-n"><span class="acct-chip">${r.n}</span></td>`
      + acctCell(r.pl, 'acct-plat', '플랫폼?')
      + acctCell(r.id, 'acct-id')
      + acctCell(r.em, 'acct-em')
      + acctCell(r.ph, 'acct-ph')
      + '</tr>';
  }).join('');

  el.innerHTML = '<table class="acct-table"><colgroup>'
    + '<col style="width:88px"><col style="width:52px"><col style="width:104px">'
    + '<col style="width:auto"><col style="width:auto"><col style="width:132px"></colgroup><thead><tr>'
    + '<th>PC</th><th>계정</th><th>플랫폼</th><th>아이디</th><th>이메일</th><th>휴대폰</th>'
    + '</tr></thead><tbody>' + body + '</tbody></table>';
}

// 위임 리스너 — 표를 다시 그려도 한 번만 붙는다
document.addEventListener('click', function(e){
  const td = e.target.closest && e.target.closest('#acct-table td.acct-cp');
  if(!td) return;
  const v = td.getAttribute('data-v') || '';
  if(!v) return;
  navigator.clipboard.writeText(v).then(()=>{
    td.classList.add('acct-hit');
    setTimeout(()=>td.classList.remove('acct-hit'), 600);
    showToast('📋 ' + (v.length > 34 ? v.slice(0,34)+'…' : v));
  }, ()=>showToast('복사 실패 — 브라우저가 클립보드를 막았습니다'));
});

function openAcctModal(){ renderAcctTable(); document.getElementById('acct-modal').classList.remove('hidden'); }
function closeAcctModal(){ document.getElementById('acct-modal').classList.add('hidden'); }
function copyAcctTable(){
  const t = ['PC\t계정\t플랫폼\t아이디\t이메일\t휴대폰']
    .concat(acctRows().map(r=>[r.pc, r.n, r.pl, r.id, r.em, r.ph].join('\t'))).join('\n');
  navigator.clipboard.writeText(t).then(
    ()=>showToast('📋 복사했습니다 — 엑셀에 그대로 붙여넣으세요'),
    ()=>showToast('복사 실패 — 브라우저가 클립보드를 막았습니다'));
}

function parsecAppFromMenu(){
  const id=menuPcId; closeCardMenu();
  const pid=_parsecPeerOrWarn(id); if(!pid) return;
  location.href = `parsec://peer_id=${encodeURIComponent(pid)}&host_secret=&a=`;   // 꼬리 필수 — 위 주석
  showToast(`🎮 파섹 앱 → ${baseId(id)} (관제컴 파섹이 이 PC로 갈아탑니다)`);
}

async function sellAllFromMenu() {
  if(!menuPcId) return;
  const pc=menuPcId, p=getSalePrice();
  if(p<=0||!isSalePriceConfirmed()){alert('먼저 상단 거래소 가격을 입력하고 [확정] 하세요');return;}
  if(!confirm(`${pc} 전 캐릭 판매 실행\n거래소 지정가: ${p.toLocaleString()}`))return;
  closeCardMenu();
  const ok=await sendCmd(pc,'sell_all',{price:p});
  showToast(ok?`✓ 판매 → ${pc} (거래소가 ${p.toLocaleString()})`:`✗ 판매 전송 실패`);
  loadCmdHistory();
}

// ─── 준비(prepare) — 전 캐릭 순회: 정산(계정1회)→추출→개인/서버창고→인벤정렬→귀환주문서 ───
async function settleSel() {
  if(selectedPcs.size===0){showToast('PC를 먼저 선택하세요');return;}
  if(!confirm(`선택 ${selectedBases().length}대 준비 실행\n(전 캐릭: 정산(계정1회)→추출→창고보관→정렬→귀환주문서)`))return;   // ★B-JS4★ 물리 PC 수
  await selCmd('prepare');
}

async function settleFromMenu() {
  if(!menuPcId) return;
  const pc=menuPcId;
  if(!confirm(`${pc} 준비 실행\n(전 캐릭: 정산(계정1회)→추출→창고보관→정렬→귀환주문서)`))return;
  closeCardMenu();
  const ok=await sendCmd(pc,'prepare',{});
  showToast(ok?`✓ 준비 → ${pc}`:`✗ 준비 전송 실패`);
  loadCmdHistory();
}

async function screenshotFromMenu() {
  if(!menuPcId) return;
  const id=menuPcId; closeCardMenu();
  const res = await fetch(`/updater/command/${baseId(id)}`, {   // 업데이터=base id (멀티계정)
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({command:'screenshot'})
  });
  if(res.ok) showToast(`📸 ${id} 스크린샷 명령 전송` + (baseId(id)!==id?` (PC 단위 — 결과는 ${baseId(id)} 버그폴더)`:''));
  else showToast(`✗ 스크린샷 명령 실패`);
}

async function deletePCFromMenu() {
  if(!menuPcId) return;
  if(!confirm(`${menuPcId} 를 목록에서 삭제하시겠습니까?\n(로그, 명령 기록, 업데이터 정보 모두 삭제됩니다)`)) return;
  const id=menuPcId; closeCardMenu();
  const res = await fetch(`/status/${id}`,{method:'DELETE'});
  if(!res.ok){showToast(`✗ 삭제 실패 (${res.status})`);return;}
  delete state[id]; selectedPcs.delete(id);
  renderCards(); updateSelBar();
  showToast(`🗑 ${id} 삭제됨`);
}

// ─── WebSocket ────────────────────────────────────────────────────────────────
// ★WS state 렌더 디바운스(성능, 2026-07-21): 17대가 30초 주기 보고 = state 브로드캐스트가
//   ~2초에 한 번인데 그때마다 카드 전체 innerHTML 재구축은 낭비(장시간 열어두면 체감 느려짐).
//   700ms로 모아서 1회만 렌더.★
let _renderTimer=null;
function scheduleRender(){ if(_renderTimer) return; _renderTimer=setTimeout(()=>{_renderTimer=null; renderCards();},700); }

// ★카드 상태 = 판 번호(2026-09-23 반응속도 2단계)★ — 서버는 앞 판을 가진 화면에 바뀐 카드만
//   보낸다(state_diff: base=앞 판, upd=바뀐 카드, del=없어진 카드, retired·latest 는 바뀔 때만).
//   base 가 내 판과 다르면(끊겼다 붙음·놓침) 적용하지 않고 전량을 다시 달라고 한다 — 틀린
//   조각을 덧대는 것보다 한 통 늦는 게 낫다. 전량(state)은 언제나 통째로 갈아엎는다(예전과 같음).
let STATE_VER = -1, _resyncAsked = false;
function applyStateMsg(msg, sock){
  if (msg.type === 'state') {
    state = {}; (msg.pcs||[]).forEach(p=>{ state[p.pc_id] = p; });
    RETIRED = new Set(msg.retired||[]);
    if (msg.latest) latestVersions = msg.latest;
    STATE_VER = (typeof msg.ver === 'number') ? msg.ver : -1;
    _resyncAsked = false;
    return true;
  }
  if (typeof msg.base !== 'number' || msg.base !== STATE_VER) {
    STATE_VER = -1;
    // 전량이 올 때까지 한 번만 조른다(이미 날아오던 조각들이 줄줄이 또 조르지 않게)
    if (!_resyncAsked) {
      _resyncAsked = true;
      try { if (sock && sock.readyState === 1) sock.send(JSON.stringify({type:'resync'})); } catch(e) {}
    }
    return false;
  }
  (msg.upd||[]).forEach(p=>{ state[p.pc_id] = p; });
  (msg.del||[]).forEach(id=>{ delete state[id]; });
  if (msg.retired) RETIRED = new Set(msg.retired);
  if (msg.latest) latestVersions = msg.latest;
  STATE_VER = msg.ver;
  return true;
}

let _ws=null, _wsLastMsg=0;
function connectWS() {
  const proto=location.protocol==='https:'?'wss':'ws';
  const ws=new WebSocket(`${proto}://${location.host}/ws`);
  _ws=ws; _wsLastMsg=Date.now();
  // ★새 소켓은 새 판부터(2026-09-23 반증 B2-2)★ — 앞 소켓에서 resync 를 조르고 전량을 못 받은 채
  //   끊기면 _resyncAsked 가 남아, 새 소켓에서 조각이 어긋나도 다시 안 졸라 화면이 멈췄다.
  STATE_VER = -1; _resyncAsked = false;
  ws.onopen=()=>{document.getElementById('ws-dot').className='w-2.5 h-2.5 rounded-full bg-green-500 transition-colors';};
  ws.onmessage=(e)=>{
    _wsLastMsg=Date.now();
    const msg=JSON.parse(e.data);
    if(msg.type==='state'||msg.type==='state_diff'){ if(applyStateMsg(msg, ws)){pendSweep();updResultSweep();scheduleRender();} }   // pendSweep = 사고 308-b ①효과 관측 해제(상태가 실제로 바뀌면 표시를 지운다)
    else if(msg.type==='log'&&logModalPc===msg.pc_id){appendLogLine(msg.level,msg.message);}
    else if(msg.type==='cmd_history'){renderCmdHistory(msg.commands||[]);}
    else if(msg.type==='char_info'){handleCharInfoMsg(msg);}
    else if(msg.type==='corridor_progress'){handleCorridorMsg(msg);}
    else if(msg.type==='alert'){handleAlert(msg);}
  };
  ws.onclose=(e)=>{
    document.getElementById('ws-dot').className='w-2.5 h-2.5 rounded-full bg-red-500 transition-colors';
    if(e&&e.code===1008){location.reload();return;}   // 세션 무효(만료 등) → 새로고침으로 로그인 이동
    setTimeout(connectWS,3000);
  };
}

// ★반개방 소켓 감시(2026-07-25, 사용자: "새로고침해야만 상태 바뀜"): 프록시/절전으로 WS가
//   close 이벤트 없이 조용히 죽으면 '연결된 척 수신 0'이 됨 — 함대가 30초마다 보고하므로
//   90초 무수신이면 죽은 것. close()로 onclose→재연결 경로를 강제 발동.★
setInterval(()=>{ if(_ws && _ws.readyState===1 && Date.now()-_wsLastMsg>90000){ try{_ws.close();}catch(err){} } },15000);

// ─── 회랑 진행 (2026-08-01): 전광판 '회랑 남음' 타일 + 스프레드 '회랑' 열 갱신 ──
let corridorRemaining={};   // {pc_id: {remaining, total, stale}}
// ★★'안 돈 PC' 를 0 으로 세지 않는다 (2026-08-24 주인님 지적)★★
//   만료 스냅샷(stale) = 수·토 22시 리셋을 지난 옛 판 → 이번 판은 ★한 칸도 안 돌았다.★
//   그러니 남은 수는 remaining(옛 값)이 아니라 ★total★ 이다.
//   실측 2026-08-24 01:24 — 고치기 전 29 / 참값 102(만료 14대가 통째로 0이었다).
//   ★자기 검증 장치★: 타일 title 에 '신선 n대 + 미착수 m대' 를 적어 둔다.
//   숫자가 또 이상해 보이면 마우스만 올리면 어느 쪽이 부풀었는지 바로 갈린다.
function updateCorridorTile(){
  let rem=0,has=false,nFresh=0,nStale=0,remStale=0;
  // ★은퇴·계정없음은 「회랑 남음」에서도 뺀다(2026-09-23, 주인님 —
  //   「회랑 계정들 전부 뱃지 달려 있는데 남음 31 은 뭐냐」)★
  //   corridorRemaining 은 pc_id 키 클라이언트 캐시라, 그 PC 가 은퇴·계정없음으로
  //   바뀌어도 마지막 스냅샷이 영영 안 지워진다 — stale 이면 ★total 전체★ 를 "안 돈
  //   것"으로 다시 더해서(위 주석 참고) 다시는 안 돌 PC 의 옛 정원이 매번 쌓였다.
  //   isCorridorDone(뱃지)과 ★같은 규칙★(isExcludedPc) 을 쓴다(§A12).
  Object.entries(corridorRemaining).forEach(([pc,v])=>{
    if(!v) return;
    if(isExcludedPc(pc)) return;
    if(v.stale){
      if(typeof v.total==='number'){has=true;nStale++;remStale+=v.total;rem+=v.total;}
    } else if(typeof v.remaining==='number'){has=true;nFresh++;rem+=v.remaining;}
  });
  const el=document.getElementById('cnt-corridor');
  if(el){
    // ★서버값 있으면 그게 최종값(2026-09-23, §A12)★ — 아래 상세(nFresh/nStale)는
    //   클라 계산에서 그대로 보여준다(툴팁용, 서버는 총합만 준다).
    const ss = serverSum();
    // 서버가 회랑 스냅샷을 하나도 못 가졌으면 null → 「–」(폴백의 has=false 와 같은 뜻)
    el.textContent = ss ? (ss.corridor_remaining != null ? String(ss.corridor_remaining) : '–') : (has?String(rem):'–');
    // ★툴팁 내역도 숫자와 같은 출처에서(2026-09-23 반증 #4)★ — 숫자는 서버, 내역은 화면 캐시면
    //   「남음 5 = 신선 3 + 미착수 4」 처럼 어긋났다. 서버값이 있으면 서버 corridor_detail 을 쓴다.
    const cd = (ss && ss.corridor_detail) ? ss.corridor_detail
      : {fresh_n: nFresh, fresh_left: rem-remStale, stale_n: nStale, stale_left: remStale};
    const t=el.closest('.stat-tile');
    // ★줄바꿈은 String.fromCharCode(10) 으로 만든다★ — 이 파일은 파이썬 문자열 안에
    //   들어 있어서 백슬래시 이스케이프가 중간 도구에 먹히는 일이 잦다(실제로 먹혔다).
    const NL = String.fromCharCode(10);
    if(t)t.title = '회랑을 아직 다 못 돈 캐릭터 수 (적 진영 제외 · 수·토 22시 리셋)'
      + NL + `· 이번 판에 보고한 ${cd.fresh_n}대: 남은 ${cd.fresh_left}`
      + NL + `· 리셋 뒤 아직 시작 안 한 ${cd.stale_n}대: 남은 ${cd.stale_left} (지난 판 정원 기준)`
      + NL + '※ 회랑을 한 번도 보고한 적 없는 PC 는 아직 여기에 안 들어갑니다'
      + NL + serverSumNote() + (ss ? '' : ageNote(CORRIDOR_AT, '회랑 목록', 600000));
  }
}
async function loadCorridorSummary(){
  try{
    const r=await fetch('/corridor/progress');if(!r.ok)return;
    const d=await r.json();CORRIDOR_AT=sumClock();corridorRemaining={};
    Object.entries(d.pcs||{}).forEach(([pc,v])=>{corridorRemaining[pc]={remaining:v.remaining,total:v.total,stale:!!v.stale};});
    updateCorridorTile();
    scheduleRender();   // 🌀 뱃지도 갱신 (만료로 사라진 PC 반영)
  }catch(e){}
}
// ★5분 주기 재조회★ — 수·토 22시 리셋 경계가 지나면 서버가 옛 스냅샷을 만료 처리하는데,
// 페이지를 안 새로고침해도 뱃지·타일이 5분 안에 따라오게 한다 (2026-08-05 "뱃지 안 사라짐" 사고)
setInterval(loadCorridorSummary,300000);
function handleCorridorMsg(msg){
  corridorRemaining[msg.pc_id]={remaining:(msg.data||{}).remaining,total:(msg.data||{}).total,stale:false};  // 방금 온 보고 = 신선
  updateCorridorTile();
  scheduleRender();  // 카드 🌀 회랑 완료 뱃지 즉시 반영
  loadCharTable();   // 스프레드 '회랑' 열 갱신 (악몽 진행도와 같은 패턴)
}
loadCorridorSummary();

// ─── 렌탈 계정 관리 (킬스위치) — main 계정 전용 (2026-08-06) ─────────────────
// 서버가 rental_kill 설정 하나에 '차단 테넌트 목록'을 담는다. 화면은 항상 ★전체 목록★을
// 다시 보내 부분 갱신으로 인한 유실을 막는다(체크 두 개를 빠르게 눌러도 마지막 상태가 정답).
let rentalTenants = [];
async function loadRentalTenants(){
  try{
    const r = await fetch('/tenants?t='+Date.now(), {cache:'no-store'});
    if(!r.ok) return;
    const d = await r.json();
    rentalTenants = d.tenants || [];
    // 렌탈 계정이 하나도 없으면 버튼도 숨긴다 (빈 패널을 열 이유가 없다)
    const btn = document.getElementById('rental-btn');
    if(btn && d.is_main && rentalTenants.length) btn.classList.remove('hidden');
  }catch(e){}
}
function openRentalModal(){
  document.getElementById('rental-modal').classList.remove('hidden');
  renderRentalList();
  loadRentalTenants().then(renderRentalList);
}
function closeRentalModal(){ document.getElementById('rental-modal').classList.add('hidden'); }
function renderRentalList(){
  const box = document.getElementById('rental-list');
  if(!box) return;
  if(!rentalTenants.length){ box.innerHTML = '<div class="text-gray-500">등록된 렌탈 계정이 없습니다.</div>'; return; }
  // ★이름이 아니라 인덱스를 넘긴다★ — 테넌트명에 따옴표가 섞이면 onclick 문자열이 깨진다
  box.innerHTML = rentalTenants.map((t,i)=>{
    const killed = !!t.killed;
    return `<div class="flex items-center justify-between gap-3 bg-gray-800/50 border ${killed?'border-rose-800':'border-gray-700'} rounded-lg px-3 py-2">
      <div class="min-w-0">
        <div class="font-semibold text-gray-200 truncate">${esc(t.name)}</div>
        <div class="text-[11px] ${killed?'text-rose-400':'text-emerald-400'}">${killed?'⛔ 이용 중지됨':'✅ 이용 중'}${t.has_chat?'':' · <span class="text-gray-500">텔레그램 미등록</span>'}</div>
      </div>
      <button onclick="toggleRentalKill(${i},${killed?'false':'true'})"
        class="shrink-0 px-3 py-1 rounded-lg text-xs font-semibold ${killed?'bg-emerald-800/70 hover:bg-emerald-600 text-emerald-100':'bg-rose-800/70 hover:bg-rose-600 text-rose-100'}">
        ${killed?'이용 재개':'이용 중지'}</button>
    </div>`;
  }).join('');
}
async function toggleRentalKill(idx, kill){
  const target = rentalTenants[idx];
  if(!target) return;
  const name = target.name;
  const msg = kill ? `'${name}' 계정의 이용을 중지할까요?\n\n· 대시보드 로그인 즉시 차단\n· 대여 프로그램은 10분 안에 자동 정지`
                   : `'${name}' 계정의 이용을 재개할까요?\n\n· 10분 안에 자동으로 다시 동작합니다`;
  if(!confirm(msg)) return;
  // 목록 전체를 다시 계산해서 보낸다 (서버는 이 한 줄이 곧 차단 명단)
  const names = rentalTenants.filter(t => (t.name===name) ? kill : t.killed).map(t=>t.name);
  try{
    const r = await fetch('/setting/rental_kill', {method:'POST', headers:{'Content-Type':'application/json'},
                          body: JSON.stringify({value: names.join(',')})});
    const d = await r.json().catch(()=>({}));
    if(!r.ok || !d.ok){ showToast('✗ 변경 실패'); return; }
    if(d.truncated) showToast('⚠ 목록이 너무 길어 잘렸습니다 — 확인 필요');
    showToast(kill ? `⛔ ${name} 이용 중지` : `✅ ${name} 이용 재개`);
  }catch(e){ showToast('✗ 변경 실패'); return; }
  await loadRentalTenants();
  renderRentalList();
}
loadRentalTenants();

// ─── 서버 재시작 감지 → 자동 새로고침 ────────────────────────────────────────
// ★새로고침 연쇄 막기 (2026-09-23 팜뷰 반증 #2)★ — 재배포 동안 옛·새 인스턴스가 번갈아 /ping 에
//   답하면 boot 가 A→B→A 로 흔들려 새로고침이 이어졌다(팜뷰 옆 창 «응답없음» 방아쇠 후보).
//   ① 진행 중 가드(겹친 폴링·새로고침 중 재호출 무시) ② 같은 새 boot 를 ★연속 두 번★ 봐야 한 번 새로고침
//   ③ 떠난 boot 는 sessionStorage 에 적어 새 화면이 옛 인스턴스 답을 기준값·변화로 안 읽는다
//   ④ 한 탭에서 60초 안 재새로고침 금지. sessionStorage 가 막혀도 ①②는 그대로 돈다.
let serverBoot=null, _bootCand=null, _bootBusy=false, _bootReloading=false;
const _BOOT_SS='dashBootReload', BOOT_RELOAD_MIN_GAP_MS=60000;
let BOOT_PING_TIMEOUT_MS=4000;
// sessionStorage 가 막힌 창(반증 J2 — 막히면 새로고침이 다시 이어졌다)은 window.name 에 적는다(같은 탭 새로고침에 남는다)
function _bootMem(){
  let raw=null;
  try{ raw=sessionStorage.getItem(_BOOT_SS); }catch(e){ try{ const n=String(window.name||''); if(n.startsWith(_BOOT_SS+':')) raw=n.slice(_BOOT_SS.length+1); }catch(_){} }
  try{ const m=JSON.parse(raw||'null'); return (m&&typeof m==='object')?m:{}; }catch(e){ return {}; }
}
function _bootMemSet(v){
  const t=JSON.stringify(v);
  try{ sessionStorage.setItem(_BOOT_SS, t); }catch(e){ try{ window.name=_BOOT_SS+':'+t; }catch(_){} }
}
async function checkServerBoot(){
  if(_bootBusy||_bootReloading) return;
  _bootBusy=true;
  try{
    // ★답 없는 ping 이 가드를 영영 쥐지 않게 4초 제한 (반증 J1)★
    const _ac=(typeof AbortController!=='undefined')?new AbortController():null;
    let _to=null;
    const _late=new Promise((_,rej)=>{ _to=setTimeout(()=>{ try{_ac&&_ac.abort();}catch(e){} rej(new Error('ping 시간초과')); }, BOOT_PING_TIMEOUT_MS); });
    let r; try{ r=await Promise.race([fetch('/ping',_ac?{cache:'no-store',signal:_ac.signal}:{cache:'no-store'}), _late]); } finally { clearTimeout(_to); }
    if(!r.ok)return;
    const b=(await r.json()).boot;
    if(!b) return;
    const m=_bootMem(); const old=Array.isArray(m.old)?m.old:[];
    if(serverBoot===null){ serverBoot=(old.includes(b)&&m.to)?m.to:b; return; }   // 최초 = 기준값(떠난 boot 면 옮겨 간 쪽)
    if(b===serverBoot||old.includes(b)){ _bootCand=null; return; }
    if(_bootCand!==b){ _bootCand=b; return; }                // 한 번 더 같은 새 boot 를 볼 때까지
    if(typeof m.at==='number' && Date.now()-m.at < BOOT_RELOAD_MIN_GAP_MS) return;
    _bootReloading=true;
    _bootMemSet({old:[...old, serverBoot].slice(-5), to:b, at:Date.now()});
    location.reload();                                        // boot 바뀜(두 번 확인) = 서버 재시작 → 새로고침 한 번
  }catch(e){/* 재시작 중이라 연결 실패 = 무시, 다음 폴링에서 감지 */}
  finally{ _bootBusy=false; }
}

// ─── 명령 내역 ────────────────────────────────────────────────────────────────
async function loadCmdHistory() {
  loadUpdHistory();
  const res=await fetch('/commands/recent'); if(!res.ok) return;
  renderCmdHistory((await res.json()).commands||[]);
}
// ★업데이터 명령도 내역에 보인다 (2026-09-23 배포 반증 #2)★ — 업데이터 큐는 10분 만료·같은 종류 덮어쓰기·
//   팜뷰 처리가 있는데 대시보드엔 보이는 곳이 없어 «눌렀는데 사라짐» 을 가를 수 없었다(사고 146 과 같은 눈).
const UPD_ST={pending:['대기','text-yellow-500'], acked:['업데이터 받음','text-green-500'],
  expired:['만료(10분 안 가져감)','text-red-400'], superseded:['덮임(같은 명령 다시 누름)','text-gray-500'],
  handed_noack:['업데이터가 받아감 — ack 없음(돌았을 수 있음)','text-amber-400'],
  fv_claimed:['팜뷰 처리 중','text-yellow-500'], fv_done:['팜뷰 완료','text-green-500'],
  fv_failed:['팜뷰 실패','text-red-400'], fv_unknown:['팜뷰 응답 없음 — 실행됐는지 모름','text-red-400']};
async function loadUpdHistory() {
  try {
    const res=await fetch('/updater/commands/recent?limit=20'); if(!res.ok) return;
    renderUpdHistory((await res.json()).commands||[]);
  } catch(e) {}
}
function renderUpdHistory(cmds) {
  const el=document.getElementById('upd-history'); if(!el) return;
  if(!cmds.length){el.innerHTML='<div class="text-gray-600">없음</div>';return;}
  el.innerHTML=cmds.map(c=>{
    const st=UPD_ST[c.status]||[String(c.status||''),'text-gray-500'];
    return `<div class="flex gap-2 items-center py-0.5">
      <span class="text-gray-600 shrink-0" title="보는 기기의 로컬 시각">${esc(fmtLocalAt(c.created_at, 11, 19))}</span>
      <span class="text-indigo-400 shrink-0">${esc(c.pc_id)}</span>
      <span class="text-gray-200">${esc(c.command)}</span>
      <span class="${st[1]} ml-auto shrink-0" title="${esc(c.status)}">${esc(st[0])}</span>
    </div>`;
  }).join('');
}
function renderCmdHistory(cmds) {
  // ★사고 308-b — ack/만료/취소를 카드 표시에 반영한다★
  //   이 함수는 WS `cmd_history` 와 loadCmdHistory() 의 ★공통 싱크★ 다.
  //   예전엔 여기서 카드를 다시 그리지 않아 ack 이 화면에 못 닿았다.
  try { if (pendFromHistory(cmds)) { pendSweep(); scheduleRenderNow(); } } catch(e) {}
  const el=document.getElementById('cmd-history');
  if(!cmds.length){el.innerHTML='<div class="text-gray-600">없음</div>';return;}
  el.innerHTML=cmds.map(c=>{
    const sc=c.status==='acked'?'text-green-500':(c.status==='pending'?'text-yellow-500':(c.status==='cancelled'?'text-red-400 line-through':'text-gray-500'));
    const cancelBtn = c.status==='pending'
      ? `<button onclick="cancelCmd(${c.id})" class="ml-1 text-gray-600 hover:text-red-400 transition-colors leading-none" title="취소">✕</button>`
      : '';
    return `<div class="flex gap-2 items-center py-0.5">
      <span class="text-gray-600 shrink-0" title="보는 기기의 로컬 시각">${esc(fmtLocalAt(c.created_at, 11, 19))}</span>
      <span class="text-indigo-400 shrink-0">${esc(c.pc_id)}</span>
      <span class="text-gray-200">${esc(c.command)}</span>
      <span class="${sc} ml-auto shrink-0">${esc(c.status)}</span>${cancelBtn}
    </div>`;
  }).join('');
}
async function cancelCmd(cmd_id) {
  // ★B-N1 (2026-09-23) 200 ≠ 취소★ — 서버는 이미 처리된 명령에 200 + {ok:false} 를 준다.
  //   예전엔 res.ok 만 봐서 못 취소한 것도 「✕ 명령 취소됨」 이었다. 본문 j.ok 로 가른다.
  // ★B-CQ10★ WS 로 이미 매크로에 간 명령은 {delivered:true} — 취소가 아니라 정지로 끊어야 한다.
  try {
    const res=await fetch(`/commands/${cmd_id}`,{method:'DELETE'});
    let j=null; try{ j=await res.json(); }catch(e){ j=null; }
    if(res.ok && j && j.ok===true) showToast('✕ 명령 취소됨');
    else if(j && j.delivered) showToast('⚠ 이미 전달됨 — 정지로 끊으세요');
    else showToast(res.ok ? '✗ 취소 못 함 — 이미 처리된 명령' : '✗ 취소 실패');
  } catch(e) { showToast('✗ 취소 실패'); }
}

// ─── 로그 모달 ────────────────────────────────────────────────────────────────
// ★★2026-08-20: 업데이터 로그를 같이 본다★★
//   매크로가 죽으면 그 PC 는 완전 실명이 된다(PC-23 사고). 그때 유일하게 살아 있는 눈이
//   업데이터인데, 그 로그는 C:\auto\updater.log 에만 있어서 대시보드로는 볼 수 없었다.
//   이제 /updater/logs/{basePc} 를 같이 읽어 ★시간순으로 섞어★ 보여준다.
//   줄 앞의 M(매크로)/U(업데이터) 뱃지로 출처를 구분한다.
function _basePc(id){ return ACCT_SUF_RE2.test(id||'') ? id.slice(0,-1) : (id||''); }

async function openLogModal(pc_id) {
  logModalPc=pc_id;
  document.getElementById('log-modal-title').textContent=`로그 — ${pc_id}`;
  document.getElementById('log-modal').classList.remove('hidden');
  renderLogTabs();
  await loadLogs();
}

function setLogSrc(src){ logModalSrc=src; renderLogTabs(); loadLogs(); }

function renderLogTabs(){
  // 탭 색은 ★여기서만★ 정한다(마크업에는 className 을 두지 않는다 — 두 군데에 있으면
  // 반드시 한쪽만 고쳐서 어긋난다).
  const on ='px-2 py-1 rounded bg-indigo-600 text-white font-semibold';
  const off='px-2 py-1 rounded bg-gray-800 text-gray-400 hover:bg-gray-700';
  const m={both:'log-tab-both',macro:'log-tab-macro',upd:'log-tab-upd'};
  for(const k in m){ const b=document.getElementById(m[k]); if(b) b.className=(logModalSrc===k?on:off); }
}

async function loadLogs(){
  const pc=logModalPc, src=logModalSrc;
  const el=document.getElementById('log-entries');
  if(!pc){ return; }
  el.innerHTML='<div class="text-gray-600">로딩 중...</div>';
  const wantM=(src==='both'||src==='macro'), wantU=(src==='both'||src==='upd');
  // ★병렬로 받는다★ — 순차로 받으면 한쪽 서버 지연이 그대로 두 배가 된다.
  //   한쪽이 실패해도(그 PC 는 업데이터 로그가 아직 없을 수 있다) 나머지는 그린다.
  const [rm,ru]=await Promise.all([
    wantM?fetch(`/logs/${encodeURIComponent(pc)}`).then(r=>r.ok?r.json():null).catch(()=>null)
         :Promise.resolve(null),
    wantU?fetch(`/updater/logs/${encodeURIComponent(_basePc(pc))}`).then(r=>r.ok?r.json():null).catch(()=>null)
         :Promise.resolve(null),
  ]);
  // ★늦게 온 결과가 화면을 덮지 않게★ — 탭을 연달아 누르거나 다른 PC 로 옮기면 먼저
  //   띄운 요청이 나중에 도착해 ★엉뚱한 PC 의 로그★를 그린다. 실제로 이 프로젝트에서
  //   "분명히 PC-20 을 열었는데 PC-10 로그가 보인다" 류의 오독이 나오는 경로다.
  if(logModalPc!==pc||logModalSrc!==src) return;
  if(!rm&&!ru){el.innerHTML='<div class="text-red-400">로드 실패</div>';return;}
  let rows=[];
  if(rm&&rm.logs) rows=rows.concat(rm.logs.map(l=>({...l,src:'M'})));
  if(ru&&ru.logs) rows=rows.concat(ru.logs.map(l=>({...l,src:'U'})));
  // created_at 은 "YYYY-MM-DDTHH:MM:SS" 고정폭이라 문자열 비교 = 시간 비교다.
  rows.sort((a,b)=>String(a.created_at||'').localeCompare(String(b.created_at||'')));
  if(rows.length>2000) rows=rows.slice(-2000);   // 두 소스를 합치면 최대 3000줄 — 상한을 건다
  el.innerHTML='';
  // ★B-JS6 (2026-09-23)★ 줄머리 시각은 보는 기기 로컬(created_at 은 UTC naive) — 매크로 원문 로그와 같은 벽시계
  rows.forEach(l=>appendLogLine(l.level,`${fmtLocalAt(l.created_at, 11, 19)} ${l.message}`,l.src));
  el.scrollTop=el.scrollHeight;
}

function appendLogLine(level, msg, src) {
  const el=document.getElementById('log-entries');
  const d=document.createElement('div');
  d.className=`${LOG_COLOR[level]||'text-gray-400'} whitespace-pre-wrap break-all leading-5`;
  // ★뱃지는 createElement + textContent 로만 붙인다★
  //   여기서 innerHTML 을 쓰면 ★로그 본문이 HTML 로 해석★돼 2026-07-27 XSS 감사 결론
  //   (로그는 전부 textContent 로만 그린다)이 통째로 되돌아간다. 로그 문자열에는
  //   게임/서버가 준 임의 문자가 그대로 들어온다.
  if(src){
    const b=document.createElement('span');
    b.className=(src==='U')?'mr-1 px-1 rounded bg-amber-900/60 text-amber-300'
                           :'mr-1 px-1 rounded bg-sky-900/60 text-sky-300';
    b.textContent=src;
    d.appendChild(b);
  }
  // src 없이 부르던 기존 호출(WS 실시간 로그)은 뱃지 없이 예전과 똑같이 그려진다.
  d.appendChild(document.createTextNode(msg));
  el.appendChild(d); el.scrollTop=el.scrollHeight;
}
function closeLogModal(){logModalPc=null;document.getElementById('log-modal').classList.add('hidden');}

async function requestLogs() {
  if (!logModalPc) return;
  await sendCmd(logModalPc, 'get_logs', {});
  showToast(`📥 ${logModalPc} 로그 요청 전송`);
  // 3초 후 자동 새로고침
  setTimeout(() => { if (logModalPc) openLogModal(logModalPc); }, 3000);
}

// ─── 토스트 ──────────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════
// ★★AI 던전 추천 (2026-08-22 주인님 지시)★★
//
//   주인님 원문: "직원들이 존나 헷갈려하고있어 오늘 어떤 캐릭터의 던전을 돌아야할지
//     … 내가 그걸 일일이 얘기해주는건 너무 번잡하고 내가 부재일때가 있으니까 힘들어"
//
//   ★기준은 주인님이 준 그대로다 (추측 금지)★
//     · 오드에너지 `840(+115)/840` 에서
//         앞 숫자   = ★매일 차는 에너지★ → 이걸 먼저 태워야 안 버린다
//         괄호 안   = 아이템으로 충전해둔 별도 에너지 (급하지 않다)
//         분모      = ★계정 단위★ 속성. 840=구독 / 560=구독 해제
//     · 구독이면 던전 한 판에 80 소모 = ★2배로 돈다★ → 우선순위 위
//     · 구독 해제면 한 판 40 + 거래소 판매 불가 + 원격창고 불가
//     · ★파워 전투력 300,000 이상만 던전 투입★ (주인님 운영 기준)
//
//   ★계정 단위다★ — 주인님: "840 560 이게 계정단위야 캐릭터단위가 아니라".
//   그래서 계정(카드)으로 묶고, 구독 배지는 계정에 붙인다.
//
//   완료 체크는 ★서버★ 에 저장한다 — 직원이 여러 명이라 브라우저에 두면 공유가 안 된다.
//   게임일은 ★새벽 5시★ 기준(주인님 지시). 5시 전이면 전날로 친다.
// ═══════════════════════════════════════════════════════════════════════════
let aiLang = localStorage.getItem('aiLang') || 'vi';   // ★기본 베트남어 (직원분들)★
// ★AI 모달 ★안에서만★ 쓰는 상태 라벨★ — 이름에 ai 를 박은 이유가 있다.
//   카드·전광판·자동진행 confirm 문장은 ★한국어 전용 영역★ 이다(페이지에 언어 전환이 없다).
//   거기서 이 함수를 부르면 주인님 화면이 베트남어로 바뀐다. ★부르지 말 것.★
//   AI 모달만 aiLang 을 따르는데 STATUS_CFG 라벨이 한국어라 한 줄에 두 언어가 섞였다
//   (실측 2026-08-28 프로덕션: `ĐANG SĂN` 배지 옆에 「사냥 중」).
function aiStLabel(st) {
  const c = STATUS_CFG[st];
  if (!c) return st || '';
  return (aiLang === 'vi' && c.vi) ? c.vi : (c.label || st || '');
}
// ★★던전 탭 — 상위/하위 통합 (2026-09-09 주인님 지시)★★
//   원문: "ai 버튼에 상위던전 하위던전 나눠서 나열했잖아 이젠 이름 던전 으로 해서
//          통합시켜서 리스팅해"
//   2026-08-23 에 파워 280,000 기준선으로 '상위'/'하위' 두 목록으로 갈랐던 것을
//   한 목록('dg')으로 합쳤다. ★파워로 거르지 않는다★ — 오드에너지를 읽은 캐릭은 전부 나온다.
//   정렬·구독배지·에너지 표시·완료 체크(키 'pc:slot')는 ★그대로★.
//   옛 저장값 'hi'/'lo' 는 'dg' 로 읽는다(통합 전에 열어 둔 브라우저).
let aiFilter = localStorage.getItem('aiFilter') || 'dg';   // 'dg' 던전 / 'hunt' 사냥 / 'kina' 키나
if (aiFilter === 'hi' || aiFilter === 'lo') aiFilter = 'dg';
let aiDone = { day: '', keys: [] };

const AI_T = {
  vi: { title:'🤖 Hầm ngục hôm nay', power:'Lực', energy:'Năng lượng', bonus:'thêm',
        slot:'Ô', chars:'nhân vật', sub:'Có đăng ký', nosub:'KHÔNG đăng ký',
        warn:'⚠ Không đăng ký — 1 lượt chỉ 40 NL, không bán được ở chợ, không dùng được kho từ xa',
        empty:'Chưa có dữ liệu. Hãy chạy thu thập thông tin trước.',
        fDg:'🏰 Hầm ngục', fHunt:'🏹 Đang săn', fKina:'💰 Kina',
        kinaTitle:'💰 Kina kho (chỉ tài khoản có gói)',
        kinaFoot:'Chỉ hiện tài khoản đang có gói (mẫu số Odd ≥700). Kina kho dùng chung theo tài khoản.',
        kinaNone:'Chưa đọc được kina kho của tài khoản có gói nào.',
        kinaSummary:(n,k)=>`${n} tài khoản · tổng ${k}`,
        kinaHPc:'PC', kinaHSrv:'Máy chủ', kinaHKina:'Kina kho',
        kinaHAcct:'Tài khoản', kinaAcct:(n)=>`TK${n}`,
        kinaSearch:'🔍 Tìm máy chủ…',
        kinaNoHit:(q)=>`Không có máy chủ nào khớp "${q}".`,
        kinaHSold:'Đã bán', kinaReset:'↺ Xoá dấu',
        kinaSold:(n,m)=>`Đã bán ${n}/${m}`,
        kinaResetTip:'Xoá toàn bộ dấu đã bán (tự động xoá lúc 5h sáng)',
        kinaResetAsk:(n)=>`Xoá ${n} dấu đã bán?`,
        kinaResetOk:'✓ Đã xoá dấu đã bán',
        huntTitle:'🏹 Chưa săn xong', huntNone:'✅ Tất cả tài khoản đã săn xong hôm nay.',
        huntBusy:'ĐANG SĂN', huntOff:'ngoại tuyến', huntSlot:'ô', huntLeft:'còn',
        huntBusyOther:(a)=>`máy này đang chạy tài khoản ${a}`,
        huntHuman:'⚠ CẦN NGƯỜI', huntHumanWhy:(a,w)=>`tài khoản ${a}: ${w} — máy tự đánh 2 lần không qua, cần người vào đánh tay`,
        huntHumanWho:(n,sl)=>` · nhân vật ${n} (ô ${sl})`,
        huntStale:(d)=>`im lặng ${d} ngày`, huntStaleNote:'thẻ cũ — không tính vào số còn lại',
        huntFoot:'Tài khoản chưa xong hôm nay (theo ô). Nếu máy đó đang săn bằng tài khoản khác thì có nhãn ĐANG SĂN. Tài khoản im lặng nhiều ngày được tách riêng và KHÔNG tính vào số còn lại. Tự cập nhật theo thời gian thực.',
        huntSummary:(pc,ac,sl)=>`${pc} máy · ${ac} tài khoản · còn ${sl} ô`,
        foot:'Tất cả nhân vật (không lọc theo lực) · tài khoản CÓ đăng ký lên trước · ưu tiên nhân vật còn nhiều năng lượng hằng ngày. Đánh dấu xong sẽ được lưu (vẫn ở nguyên chỗ), tự reset lúc 5 giờ sáng.',
        summary:(a,c,d)=>`${a} tài khoản · ${c} nhân vật · đã xong ${d}` },
  ko: { title:'🤖 오늘의 던전', power:'파워', energy:'에너지', bonus:'보너스',
        slot:'슬롯', chars:'캐릭', sub:'구독 O', nosub:'구독 X',
        warn:'⚠ 구독 해제 — 한 판 40에너지, 거래소 판매 불가, 원격창고 불가',
        empty:'데이터가 없습니다. 먼저 정보수집을 돌려주세요.',
        fDg:'🏰 던전', fHunt:'🏹 사냥', fKina:'💰 키나',
        kinaTitle:'💰 창고키나 (구독한 계정만)',
        kinaFoot:'구독한 계정만 보입니다(오드에너지 분모 ≥700). 창고키나는 계정 단위 공유값입니다.',
        kinaNone:'창고키나를 읽은 구독 계정이 아직 없습니다.',
        kinaSummary:(n,k)=>`구독 계정 ${n}개 · 합계 ${k}`,
        kinaHPc:'PC', kinaHSrv:'서버', kinaHKina:'창고키나',
        kinaHAcct:'계정', kinaAcct:(n)=>`계정${n}`,
        kinaSearch:'🔍 서버 검색…',
        kinaNoHit:(q)=>`"${q}" 에 맞는 서버가 없습니다. (읽은 게 없는 게 아니라 검색 결과입니다)`,
        kinaHSold:'판매완료', kinaReset:'↺ 체크 리셋',
        kinaSold:(n,m)=>`판매 ${n}/${m}`,
        kinaResetTip:'판매완료 체크를 전부 지웁니다 (새벽 5시에도 자동으로 지워집니다)',
        kinaResetAsk:(n)=>`판매완료 체크 ${n}개를 전부 지울까요?`,
        kinaResetOk:'✓ 판매완료 체크를 지웠습니다',
        huntTitle:'🏹 아직 사냥 안 끝난 계정', huntNone:'✅ 오늘 전 계정이 사냥을 마쳤습니다.',
        huntBusy:'사냥중', huntOff:'오프라인', huntSlot:'슬롯', huntLeft:'남음',
        huntBusyOther:(a)=>`이 컴퓨터는 지금 계정${a} 이 돌고 있습니다`,
        huntHuman:'⚠ 사람이 가야 함', huntHumanWhy:(a,w)=>`계정${a}: ${w} — 매크로가 2회 시도하고 못 잡았습니다`,
        huntHumanWho:(n,sl)=>` · ${n} (슬롯 ${sl})`,
        huntStale:(d)=>`${d}일째 소식 없음`, huntStaleNote:'옛 카드 — 남은 수에 안 셉니다',
        huntFoot:'오늘 슬롯을 다 못 끝낸 계정만 (슬롯 기준). 그 컴퓨터가 다른 계정으로 사냥 중이면 사냥중 배지가 붙습니다. 며칠째 안 뜬 계정은 따로 갈라 놓고 남은 수에 안 셉니다(옛 카드가 박제된 것이라 오늘 안 한 게 아닙니다). 실시간으로 갱신됩니다.',
        huntSummary:(pc,ac,sl)=>`${pc}대 · 계정 ${ac}개 · 남은 슬롯 ${sl}`,
        foot:'파워 구분 없이 전부 · 구독 계정이 위 · 매일 차는 에너지 많은 순. 완료 체크는 저장되며(자리는 안 움직임) 새벽 5시에 리셋됩니다.',
        summary:(a,c,d)=>`계정 ${a}개 · 캐릭 ${c}명 · 완료 ${d}` },
};

// ★게임일 — 새벽 5시 경계 (주인님 지시)★ 5시 전이면 전날로 친다.
function aiGameDay(){
  // ★B-JS11 (2026-09-23)★ 5시 경계는 ★KST★ — 베트남 폰에서도 한국 리셋에 맞춰 체크가 풀린다.
  return fmtKstTs(new Date(kstGameDayNum(Date.now()) * 86400000 - KST_OFF_MS)).slice(0, 10);
}

// `840(+115)/840` → {daily:840, bonus:115, max:840}. 못 읽으면 null.
function aiParseOdd(s){
  const m = String(s||'').match(/^\s*([\d,]+)\s*(?:\(\+?([\d,]+)\))?\s*\/\s*([\d,]+)/);
  if(!m) return null;
  const n = v => parseInt(String(v||'0').replace(/,/g,''), 10) || 0;
  return { daily:n(m[1]), bonus:n(m[2]), max:n(m[3]) };
}

async function aiLoadDone(){
  try{
    const r = await fetch('/setting/ai_dungeon_done');
    const j = await r.json();
    const v = JSON.parse(j.value || '{}');
    // ★게임일이 바뀌었으면 통째로 버린다 = 새벽 5시 리셋★
    aiDone = (v && v.day === aiGameDay()) ? {day:v.day, keys:v.keys||[]} : {day:aiGameDay(), keys:[]};
  }catch(e){ aiDone = {day:aiGameDay(), keys:[]}; }
}

// ★★판매완료 체크 (2026-08-31 주인님 지시) — aiDone 과 같은 규약★★
//   저장은 서버 설정이다(브라우저가 아니다) — 여럿이 같이 보는 화면이라
//   한 사람이 체크하면 다른 사람 화면에도 있어야 한다.
//   ★게임일(새벽 5시)이 바뀌면 통째로 버린다★ — 어제 판 것이 오늘 체크로 남으면
//   「판 줄 알았는데 안 팔림」이 된다.
let aiKinaSold = { day: '', keys: [] };

async function aiLoadKinaSold(){
  try{
    const r = await fetch('/setting/ai_kina_sold');
    const j = await r.json();
    const v = JSON.parse(j.value || '{}');
    aiKinaSold = (v && v.day === aiGameDay()) ? {day:v.day, keys:v.keys||[]}
                                              : {day:aiGameDay(), keys:[]};
  }catch(e){ aiKinaSold = {day:aiGameDay(), keys:[]}; }
}

async function aiSaveKinaSold(){
  aiKinaSold.day = aiGameDay();
  try{
    await fetch('/setting/ai_kina_sold', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({value: JSON.stringify(aiKinaSold)})});
  }catch(e){ showToast('⛔ 판매완료 저장 실패'); }
}

async function aiToggleKinaSold(key, el){
  const i = aiKinaSold.keys.indexOf(key);
  if (i >= 0) aiKinaSold.keys.splice(i,1); else aiKinaSold.keys.push(key);
  // ★줄은 안 움직인다★ — 그 자리에서 흐려지기만 한다(던전 탭과 같은 규칙)
  const tr = el && el.closest('tr');
  if (tr) tr.style.opacity = (i >= 0) ? '' : '.45';
  const c = document.getElementById('ai-kina-cnt');
  if (c) c.textContent = kinaSoldLabel();
  await aiSaveKinaSold();
}

function kinaSoldLabel(){
  const T = AI_T[aiLang] || AI_T.vi;
  const rows = kinaSorted();
  const n = rows.filter(r => aiKinaSold.keys.includes(r.pid)).length;
  return T.kinaSold(n, rows.length);
}

async function aiResetKinaSold(){
  const T = AI_T[aiLang] || AI_T.vi;
  if (!confirm(T.kinaResetAsk(aiKinaSold.keys.length))) return;
  aiKinaSold.keys = [];
  await aiSaveKinaSold();
  kinaPaint();
  showToast(T.kinaResetOk);
}

async function aiSaveDone(){
  aiDone.day = aiGameDay();
  try{
    await fetch('/setting/ai_dungeon_done', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({value: JSON.stringify(aiDone)})});
  }catch(e){ showToast('⛔ 완료 체크 저장 실패'); }
}

async function aiToggleDone(key, el){
  const i = aiDone.keys.indexOf(key);
  if (i >= 0) aiDone.keys.splice(i,1); else aiDone.keys.push(key);
  await aiSaveDone();
  renderAiPlan();
}

function setAiLang(l){
  aiLang = l; localStorage.setItem('aiLang', l);
  document.getElementById('ai-lang-vi').className = 'text-xs px-2 py-1 rounded font-bold ' + (l==='vi'?'bg-fuchsia-700 text-white':'bg-gray-700 text-gray-300');
  document.getElementById('ai-lang-ko').className = 'text-xs px-2 py-1 rounded font-bold ' + (l==='ko'?'bg-fuchsia-700 text-white':'bg-gray-700 text-gray-300');
  renderAiPlan();
}

// ★★계정 헤더에 '플랫폼 + 아이디' 를 크게 (2026-08-22 주인님 지시)★★
//   원문: "애들이 얘기하는게 컴퓨터에서 계정변경하는게 바로 눈에 안보인다고하니까
//          PC-01 구독 옆에 플랫폼이랑 아이디 빡 적어주는게 좋을거같아"
//   ★이 팝업은 '작업 지시서' 다★ — 직원분은 이걸 보고 ★그 PC 에서 계정을 바꾼다.★
//   그런데 어느 아이디로 바꿔야 하는지가 없으면, 결국 다시 물어봐야 한다
//   (이 기능을 만든 이유 자체가 '물어보는 걸 없애는 것' 이었다).
//   · 플랫폼(NC / 전화번호 / 구글)은 로그인 화면이 서로 달라서 먼저 알아야 한다
//   · ★구글은 눈에 띄게★ — 지뢰 C1: 구글 계정은 CDP 자동 로그인이 구조적으로 안 되고
//     사람이 직접 해야 한다. 색을 달리해 '이건 손이 더 간다' 를 미리 알린다
//   · 아이디는 ★보고 타이핑하는 값★ 이라 monospace + user-select:all (클릭 한 번에 전체 선택)
// info.txt 의 플랫폼 표기(NC / 전화번호 / 구글 …)를 현재 언어로. 모르는 값은 원문 유지.
function aiPlatLabel(raw){
  const v = String(raw||'').trim();
  if (!v) return '';
  if (/구글|google/i.test(v))            return aiLang==='vi' ? 'Google'      : '구글';
  if (/전화|폰|핸드폰|phone|번호/i.test(v)) return aiLang==='vi' ? 'Số điện thoại' : '전화번호';
  if (/카카오|kakao/i.test(v))            return aiLang==='vi' ? 'Kakao'       : '카카오';
  if (/네이버|naver/i.test(v))            return aiLang==='vi' ? 'Naver'       : '네이버';
  if (/애플|apple/i.test(v))              return aiLang==='vi' ? 'Apple'       : '애플';
  if (/^\s*nc\s*$/i.test(v) || /엔씨|플레이엔씨|plaync/i.test(v)) return 'NC';
  return v;                                  // ★모르는 값은 함부로 안 바꾼다★ — 원문이 정보다
}
function aiAcctInfo(pcid){
  const n  = acctNumOf(pcid);
  const M  = groupAcctMaps(baseId(pcid));
  const st = state[pcid] || {};
  const id   = st.acct_id || M.ids[n] || '';
  let   plat = (st.acct_platforms && st.acct_platforms[n]) || M.plats[n] || '';   // ★let★ — 아래에서 번역해 덮는다
  if (!id && !plat) return '';
  const goog = isGooglePlat(plat);
  // ★★플랫폼도 번역한다 (2026-08-22 주인님 지적)★★
  //   주인님: "베트남어로 보여야하는데 전화번호랑 구글은 한글이면 어떻게 ㅋㅋㅋ"
  //   ★i18n 은 '내가 쓴 문장' 만 번역하고 끝나기 쉽다★ — info.txt 에서 올라온 값
  //   (NC / 전화번호 / 구글)은 데이터라서 번역 대상에서 빠져 있었다.
  //   화면에 뜨는 글자는 출처가 어디든 그 화면 언어여야 한다.
  //   info.txt 표기가 흔들리므로(구글/google/Google 계정 …) 부분일치로 본다.
  plat = aiPlatLabel(plat);
  const pchip = plat
    ? `<span style="background:${goog?'rgba(234,179,8,.22)':'rgba(59,130,246,.20)'};`
      + `color:${goog?'#fde047':'#93c5fd'};border:1px solid ${goog?'#eab308':'#60a5fa'};`
      + `padding:1px 7px;border-radius:6px;font-size:12px;font-weight:800;flex:none">${esc(plat)}</span>`
    : '';
  const idtxt = id
    ? `<span style="font-family:ui-monospace,Consolas,monospace;font-size:14px;font-weight:700;`
      + `color:#e5e7eb;user-select:all;cursor:text" title="${aiLang==='vi'?'Nhấp để chọn toàn bộ':'클릭하면 전체 선택'}">${esc(id)}</span>`
    : '';
  return `<span style="display:inline-flex;align-items:center;gap:6px">${pchip}${idtxt}</span>`;
}

async function openAiPlan(){
  document.getElementById('ai-modal').classList.remove('hidden');
  await aiLoadDone();
  await aiLoadKinaSold();          // ★판매완료 체크도 서버에서 받아온다 (2026-08-31)★
  setAiLang(aiLang);
}

// ★항상 지금 정보수집 상태로 다시 계산한다 (주인님 지시)★ — 캐시하지 않는다.
function aiBuildPlan(){
  const acc = {};
  (charTableData||[]).forEach(r => {
    const oe = aiParseOdd(r.odd_energy);
    if (!oe) return;                                   // 오드 미수집 캐릭은 판단 불가 → 제외
    const pc = r.pc_id || '';
    if (!acc[pc]) acc[pc] = {pc, max:0, chars:[]};
    acc[pc].max = Math.max(acc[pc].max, oe.max);       // ★계정 단위 속성★
    acc[pc].chars.push({slot:r.slot, name:r.name||'', pw:Number(r.power_power)||0,
                        daily:oe.daily, bonus:oe.bonus});
  });
  const out = [];
  Object.values(acc).forEach(a => {
    // ★2026-08-31 — 여기만 840 으로 남아 있었다★ (2026-08-29 정정 때 빠졌다)
    //   dkSubCount·subBadge 는 SUB_DEN_MIN(700) 인데 여기만 840 이라, 분모 800 계정을
    //   ★던전 탭만 「구독 X」★ 로 봤다. 같은 모달 안에서 탭마다 답이 다르면 안 된다.
    const sub = a.max >= SUB_DEN_MIN;
    // ★파워로 거르지 않는다 (2026-09-09 통합)★ — 정렬 규칙은 그대로.
    const elig = a.chars.slice().sort((x,y) => y.daily - x.daily);
    if (!elig.length) return;
    elig.forEach(c => { c.key = a.pc + ':' + c.slot; });
    // ★★정렬은 완료 체크와 ★무관★ 해야 한다 (2026-08-22 주인님 지시)★★
    //   원문: "지금 체크하면 목록에서 없어진단말이야? 그러지말고 체크해도 그자리에 있게"
    //   ★초판 버그★ — 정렬 키를 '아직 안 한 것의 합' 으로 잡아서, 체크하는 순간 그 계정의
    //   점수가 떨어지고 ★목록이 통째로 재정렬★ 됐다. 사람 눈에는 '사라진' 것으로 보인다.
    //   작업 목록에서 자리가 움직이면 지금 어디까지 했는지를 잃는다 — 체크는 ★표시만★ 이고
    //   순서는 화면을 연 시점 그대로 고정한다.
    out.push({pc:a.pc, sub, max:a.max, chars:elig,
              energy: elig.reduce((s,c) => s + c.daily, 0)});   // 완료와 무관한 고정 키
  });
  // ★구독 계정 먼저★(2배 효율) → 그 안에서 일일 에너지 많은 순. 체크해도 안 바뀐다.
  out.sort((x,y) => (y.sub - x.sub) || (y.energy - x.energy));
  return out;
}

// ★던전/사냥/키나 전환 — 완료 체크는 건드리지 않는다(키가 pc:slot 이라 그대로 살아 있다)★
function setAiFilter(f){
  aiFilter = (f === 'hunt' || f === 'kina') ? f : 'dg';
  try{ localStorage.setItem('aiFilter', aiFilter); }catch(e){}
  renderAiPlan();
}

// ═══════════════════════════════════════════════════════════════════════════
// ★★사냥 탭 (2026-08-28 주인님 지시)★★
//
//   원문: "AI 버튼 안에 사냥 이라는 탭도 하나 만들고 거기다가 실시간으로 아직 사냥
//          안끝난애들 표시하게끔해놓고 만약 그컴퓨터의 다른계정이 사냥중이면
//          사냥중이라고 표시하게끔도 해줘"
//
//   ★던전 탭과 데이터가 다르다★ — 던전 추천은 charTableData(정보수집 OCR)를 보지만
//   이 탭은 ★state(실시간 카드)★ 를 본다. 그래서 WS 로 상태가 바뀌면 바로 다시 그린다.
//
//   ★'끝났다' 판정은 dpDone 한 곳만 쓴다★ (3578줄) — daily_progress 의 completed 는
//   늙지 않아서 며칠 전 완주가 오늘 완주로 읽힌다(2026-08-20 PC-12 실측). 서버가 붙여준
//   today 플래그를 함께 봐야 한다. ★여기서 따로 판정하면 카드와 숫자가 어긋난다.★
//
//   ★'사냥중' 은 계정이 아니라 컴퓨터 단위다★ — 한 물리 PC 에 계정 카드가 여러 장이고
//   (PC-22 · PC-22b · PC-22c…) 게임은 ★한 번에 한 계정만★ 돈다. 그래서 그 PC 의 카드
//   중 하나라도 사냥/작업 중이면 나머지 계정은 '지금은 못 도는 게 정상' 이다.
//   이걸 안 보여주면 직원분들이 "왜 안 도냐" 고 그 PC 를 또 건드린다.
// ═══════════════════════════════════════════════════════════════════════════
// ★★며칠째 안 뜬 계정을 '오늘 안 끝냄' 으로 세면 안 된다 (2026-08-28 본방 지적)★★
//   실측: PC-21d last_active=08-26 · PC-21e=08-25 인데 daily_progress 는 0/2 로 박제돼 있다.
//   그건 「오늘 안 했다」가 아니라 ★「그 계정이 요즘 안 돌았다」★ 다.
//   ★목록에서 빼지는 않는다★ — 안 끝난 건 사실이고, 빼면 그 계정이 영영 안 보인다.
//   대신 「N일째 소식 없음」 으로 흐리게 갈라 놔서 직원분들이 헛걸음하지 않게 한다.
//   경계는 게임일(새벽 5시) — aiGameDay 와 같은 규약을 쓴다.
function aiStaleDays(p){
  const la = p && p.last_active;
  if (!la) return 0;
  // ★B-JS2 (2026-09-23)★ last_active 는 UTC naive(lc/report_module._now) — new Date() 로 읽으면
  //   로컬로 오해해 9시간(베트남 7시간) 어긋나 방금 보고한 계정이 「1일째 소식 없음」 이 됐다.
  //   collectedAtDate 로 UTC 파싱 + 게임일은 KST 05:00 경계(kstGameDayNum).
  const a = collectedAtDate(la);
  if (!a) return 0;
  return Math.max(0, kstGameDayNum(Date.now()) - kstGameDayNum(a.getTime()));
}

// ★★「사냥중」과 「사람이 가야 함」은 갈라서 보여준다 (2026-08-28 본방 실측 확인)★★
//   `AUTO_IDLE_HELD` 는 ['paused','error','awakening_wait','nightmare_wait'] 인데
//   이걸 통째로 「사냥중」 으로 쓰면 틀린다 — paused·error 는 게임을 쥐고 있는 게 아니다.
//   ★두 wait 만 다르다★ (매크로 소스 실측):
//     · awakening_wait : 캐릭이 ★각성전 던전 안에 선 채★ 사람의 Scroll Lock 을 기다린다.
//       `lc/loot.py` 가 이 상태에서 daily_dungeon·nightmare·kill_game·plrow_shot 을 ★거부★ 하고
//       주석에 「일일던전이 끼어들면 세션 파괴」 라고 적어놨다. 하루 1회 콘텐츠다.
//     · nightmare_wait : 악몽 최종보스를 ★사람이 손으로★ 잡아야 하는 구간(알람이 이미 울린 뒤).
//   → 화면에서도 갈라야 한다. 「사냥중」은 기다리면 되지만 ★이건 지금 사람이 가야 한다.★
//   ※ 목록을 늘릴 때는 AUTO_IDLE_HELD 와의 차이를 하네스가 감시한다(hunt_test.js).
const HUNT_HUMAN_WAIT = ['awakening_wait', 'nightmare_wait'];

function aiBuildHunt(){
  const byBase = {};
  for (const p of Object.values(state)) {
    const b = baseId(p.pc_id || '');
    if (!b) continue;
    if (isFakePc(b)) continue;
    (byBase[b] = byBase[b] || []).push(p);
  }
  const out = [];
  Object.keys(byBase).sort().forEach(b => {
    const cards = byBase[b];
    // ★이 컴퓨터가 지금 무엇을 하고 있나★ — 계정이 아니라 PC 단위로 한 번만 본다
    const busy = cards.find(c => AUTO_IDLE_BUSY.includes(c.status)) || null;
    // ★사람이 가야 하는 상태는 따로★ — 기다리면 되는 '사냥중' 과 대응이 다르다
    const held = cards.find(c => HUNT_HUMAN_WAIT.includes(c.status)) || null;
    const accts = [];
    cards.forEach(p => {
      const dp = p.daily_progress || [];
      if (!dp.length) return;                       // 진행 정보 없음 = 판정 불가 → 뺀다
      const done = dp.filter(dpDone).length;
      if (done >= dp.length) return;                // 오늘 완주 → 뺀다
      const cfg = STATUS_CFG[p.status || 'offline'] || STATUS_CFG.offline;
      accts.push({ pc: p.pc_id, n: acctNumOf(p.pc_id) || 1,
                   done: done, total: dp.length, left: dp.length - done,
                   status: p.status || 'offline', label: aiStLabel(p.status || 'offline'),
                   online: !!cfg.online,
                   stale: aiStaleDays(p),          // ★며칠째 소식 없나 (0 = 오늘)★
                   busy: AUTO_IDLE_BUSY.includes(p.status) });
    });
    if (!accts.length) return;
    // ★오늘 도는 계정을 위로, 며칠째 안 뜬 것은 아래로★ 그 안에서 계정번호 순
    accts.sort((x, y) => (x.stale - y.stale) || (x.n - y.n));
    // ★남은 슬롯 합계는 '오늘 도는 계정' 만 센다★ — 박제된 옛 카드까지 더하면
    //   "40개 남았다" 같은 숫자가 부풀어 판단을 흐린다(실측: PC-21 이 6으로 잡혔다).
    const liveLeft = accts.filter(a => !a.stale).reduce((s, a) => s + a.left, 0);
    out.push({ base: b, busy: busy, busyAcct: busy ? (acctNumOf(busy.pc_id) || 1) : 0,
               // ★배지에 「사냥중」을 박아두면 거짓말을 한다★ (실렌더 2026-08-28: reconnecting 인데 「사냥중」)
               //   AUTO_IDLE_BUSY 에는 재연결·캐릭전환·캡차도 들어있다. 실제 상태를 같이 낸다.
               busySt: busy ? (busy.status || '') : '',
               held: held, heldSt: held ? held.status : '',
               // ★막힌 캐릭★ — 매크로가 보내는 wait_slot 으로 daily_progress 에서 이름을 찾는다.
               //   ★카드의 slot(활성 슬롯)을 쓰면 안 된다★ — 실측 PC-07b 는 슬롯1 에서 막혔는데
               //   그 뒤 슬롯2 로 넘어가 카드엔 slot:2 였다. 그대로 쓰면 엉뚱한 캐릭을 가리킨다.
               //   ★wait_slot 을 안 보내는 옛 매크로에서는 아무것도 안 띄운다★ — 틀린 이름보다 없는 게 낫다.
               heldWho: (() => {
                 const w = held && Number(held.wait_slot || 0);
                 if (!w) return null;
                 const row = (held.daily_progress || []).find(c => Number(c && c.slot) === w);
                 return { name: (row && row.name) || ('슬롯' + w), slot: w };
               })(),
               heldAcct: held ? (acctNumOf(held.pc_id) || 1) : 0,
               // ★사고 380 (주인님 지적)★ 「순회걸었다고 대시보드에는안뜨는데?」
               //   매크로가 tour="2/4 hunt" · tour_seq="1→2→3→4" 를 보낸다.
               //   ★옛 매크로는 안 보낸다★ → 값이 없으면 배지를 아예 안 그린다
               //   (없는 것과 「안 걸림」을 뭉개지 않는다).
               tour: (cards.find(c => c && c.tour) || {}).tour || "",
               tourSeq: (cards.find(c => c && c.tour) || {}).tour_seq || "",
               left: liveLeft, staleLeft: accts.reduce((s, a) => s + a.left, 0) - liveLeft,
               // ★live 카드가 하나도 없으면 「0 남음」이 거짓말을 한다★ (실측 PC-23: 2·2·4일)
               //   사람이 「할 일 없음」으로 읽는데 실제로는 며칠째 소식이 없는 PC 다.
               //   ★가장 최근 소식(최솟값)★ 을 쓴다 — 최댓값이면 4일이라 쓰고 2일 전 카드를 못 본 게 된다.
               //   목록에 남은 카드는 전부 left>=1 이므로 liveLeft===0 ⟺ 전부 stale 이다.
               minStale: liveLeft ? 0 : Math.min.apply(null, accts.map(a => a.stale)),
               accts: accts });
  });
  // ★맨 위 = 사람이 가야 하는 PC★ (각성전/악몽 대기 — 캐릭이 던전 안에 서 있다)
  //   그 다음 = 사냥도 안 하면서 안 끝난 PC. 사냥 중인 PC 는 정상 진행이라 맨 아래.
  out.sort((x, y) => (!!y.held - !!x.held) || (!!x.busy - !!y.busy) ||
                     (y.left - x.left) || String(x.base).localeCompare(String(y.base)));
  return out;
}

// ═══════════════════════════════════════════════════════════════════════════
// ★★키나 탭 (2026-08-31 주인님 지시)★★
//   원문: "사냥 옆에 탭하나 더만들자 키나 라고 만들고, 여기는 구독한거만 나타나게하고
//          하는 PC 랑 서버 창고키나 딱 나오게"
//
//   ★값을 새로 만들지 않는다★ — 셋 다 이미 정본이 있다:
//     구독     subState(pc_id)                  (문턱 700, 카드 뱃지와 ★같은 함수★)
//     창고키나  state[pc]._total_kina            (전광판 refreshSummary 가 쓰는 그 값)
//     서버명   groupAcctMaps(base).servers[n]   (카드 계정줄이 쓰는 그 지도)
//   ★창고키나는 계정 단위 공유값★ 이라 캐릭터별로 더하지 않는다(더하면 중복이다).
// ═══════════════════════════════════════════════════════════════════════════
function aiBuildKina(){
  const out = [];
  Object.values(state || {}).forEach(p => {
    const pid = p.pc_id || '';
    if (!pid || /^PC-(TEST|DEMO)/i.test(pid)) return;
    if (subState(pid) !== 'on') return;                 // ★구독한 것만★
    const n = acctNumOf(pid);
    const srv = (p.acct_servers && p.acct_servers[n]) ||
                groupAcctMaps(baseId(pid)).servers[n] || '';
    // ★★주인님 지시 (2026-09-05)★★
    //   「그거 컴퓨터랑 계정번호도 출력해야할거같고 그리고 4번컴퓨터에 계정1, 2
    //     둘다 구독되어잇으면 4번 계정1 서버 얼마, 4번 계정2 서버 얼마 이렇게도
    //     출력이되야할거같아」
    //   ★줄은 원래 계정(카드)마다 하나씩 나온다★ — 이 함수가 pc_id(접미사 포함)를
    //   돌기 때문이다. 문제는 ★화면에 계정 번호가 없어서 구분이 안 됐다★ 는 것이다.
    //   실측 2026-09-05: 구독 22개가 전부 접미사 없는 카드(계정1)라 한 PC 에 두 줄이
    //   나올 일이 없었고, 그래서 이 결함이 안 보였다. 부계정 구독이 켜지는 순간
    //   같은 PC 가 두 줄이 되는데 ★둘이 똑같아 보인다.★
    out.push({pid, base: baseId(pid), srv, no: n,
              kina: Number(p._total_kina) || 0,
              seen: p._total_kina != null});
  });
  return out;
}

// ★정렬·검색 상태 — 화면을 닫았다 열어도 보던 대로 (2026-08-31)★
let aiKinaSort = {key: 'pid', dir: 'asc'};
try {
  const _sv = JSON.parse(localStorage.getItem('aiKinaSort') || 'null');
  if (_sv && _sv.key) aiKinaSort = _sv;
} catch (e) {}
let aiKinaQ = '';

function kinaSorted(){
  const q = (aiKinaQ || '').trim().toLowerCase();
  let rows = aiBuildKina();
  // ★서버로 검색★ (주인님 지시). ★PC·계정번호로도 걸리게 넓혔다★ —
  //   'pc-04' 나 '계정2' 로도 찾을 수 있다. 서버만 되던 때는 못 읽은 서버가
  //   검색어만 넣으면 통째로 사라져 「없다」로 보였다.
  if (q) rows = rows.filter(r =>
      String(r.srv || '').toLowerCase().includes(q) ||
      String(r.pid || '').toLowerCase().includes(q) ||
      String(r.base || '').toLowerCase().includes(q) ||
      ('계정' + r.no).includes(q) || ('tk' + r.no).includes(q));
  const k = aiKinaSort.key, sgn = (aiKinaSort.dir === 'desc') ? -1 : 1;
  rows.sort((x, y) => {
    let d;
    if (k === 'kina') {
      // ★못 읽은 값은 언제나 맨 뒤★ — 0 으로 취급해 섞어버리면 '비었다'로 오해한다
      if (x.seen !== y.seen) return x.seen ? -1 : 1;
      d = x.kina - y.kina;
    } else if (k === 'srv') {
      d = String(x.srv).localeCompare(String(y.srv), 'ko');
    } else if (k === 'acct') {
      // ★계정 번호로 정렬해도 같은 PC 는 붙어 있게★ — 번호 먼저, 그다음 PC
      d = (x.no - y.no) || String(x.base).localeCompare(String(y.base));
    } else {
      d = String(x.base).localeCompare(String(y.base)) ||
          String(x.pid).localeCompare(String(y.pid));
    }
    // 같으면 PC 순으로 고정 — 다시 그릴 때마다 줄이 움직이면 눈이 자리를 잃는다
    return d ? d * sgn : String(x.pid).localeCompare(String(y.pid));
  });
  return rows;
}

function kinaArrow(key){
  if (aiKinaSort.key !== key) return '<span class="opacity-30">⇅</span>';
  return aiKinaSort.dir === 'asc'
    ? '<span class="text-amber-300">▲</span>' : '<span class="text-amber-300">▼</span>';
}

function kinaSortBy(key){
  // 같은 열을 다시 누르면 오름↔내림, 다른 열이면 오름부터
  if (aiKinaSort.key === key) aiKinaSort.dir = (aiKinaSort.dir === 'asc') ? 'desc' : 'asc';
  else aiKinaSort = {key, dir: 'asc'};
  try { localStorage.setItem('aiKinaSort', JSON.stringify(aiKinaSort)); } catch (e) {}
  kinaPaint();
}

function kinaSearch(v){
  aiKinaQ = v || '';
  kinaPaint();                       // ★tbody 만 갈아끼운다 — 입력 커서를 안 건드린다★
}

function kinaPaint(){
  const T = AI_T[aiLang] || AI_T.vi;
  const tb = document.getElementById('ai-kina-tbody');
  if (!tb) { renderAiKina(); return; }
  const rows = kinaSorted();
  const tot = rows.reduce((a, r) => a + (r.seen ? r.kina : 0), 0);
  const sum = document.getElementById('ai-summary');
  if (sum) sum.textContent = T.kinaSummary(rows.length, fmtKinaShort(tot))
                             + (aiKinaQ ? `  ·  🔍 ${aiKinaQ}` : '');
  ['pid', 'acct', 'srv', 'kina'].forEach(k => {
    const el = document.getElementById('ai-kina-h-' + k);
    if (el) el.innerHTML = el.dataset.label + ' ' + kinaArrow(k);
  });
  if (!rows.length) {
    // ★「검색에 안 걸렸다」와 「읽은 게 없다」는 다르다 (2026-08-31)★
    //   검색 중인데 "창고키나를 읽은 구독 계정이 아직 없습니다" 라고 적으면
    //   사람이 정보수집을 의심하러 간다 — 오늘 하루 내내 잡은 그 부류다.
    const msg = aiKinaQ ? T.kinaNoHit(aiKinaQ) : T.kinaNone;
    tb.innerHTML = `<tr><td colspan="5" class="px-3 py-8 text-center text-gray-400">${esc(msg)}</td></tr>`;
    return;
  }
  let h = '';
  rows.forEach(r => {
    // ★못 읽은 것을 0 으로 적지 않는다★ — 0 은 '비었다'로 읽히는데 사실은 '모른다'다.
    //   ★줄임 표기를 써도 원본은 title 에 남긴다★ (6.8억 뒤의 실제 자릿수)
    const kv = r.seen
      ? `<span class="font-extrabold text-amber-300 text-lg" style="font-variant-numeric:tabular-nums"
               title="${esc(fmtKina(r.kina))}">${esc(fmtKinaShort(r.kina))}</span>`
      : `<span class="text-gray-500 text-base" title="아직 안 읽었습니다 — 정보수집을 돌리면 채워집니다">–</span>`;
    // ★체크해도 자리는 그대로★ — 흐리게만 한다(정렬 키에 done 을 안 넣는다)
    const sold = aiKinaSold.keys.includes(r.pid);
    h += `<tr class="border-b border-gray-800/60 hover:bg-gray-800/50" style="${sold?'opacity:.45':''}">
      <td class="px-3 py-2 font-extrabold text-white text-base">${esc(r.base)}</td>
      <td class="px-3 py-2"><span class="px-2 py-0.5 rounded bg-indigo-900/70 text-indigo-200
            font-bold text-sm whitespace-nowrap"
            title="${esc(r.pid)}">${esc(T.kinaAcct(r.no))}</span></td>
      <td class="px-3 py-2"><span class="text-sky-300 font-bold text-base">${esc(r.srv || '–')}</span></td>
      <td class="px-3 py-2 text-right">${kv}</td>
      <td class="px-3 py-2 text-center">
        <input type="checkbox" ${sold?'checked':''}
               onchange="aiToggleKinaSold('${esc(r.pid)}', this)"
               class="w-5 h-5 accent-emerald-500 cursor-pointer" title="${esc(T.kinaHSold)}">
      </td>
    </tr>`;
  });
  tb.innerHTML = h;
  const c = document.getElementById('ai-kina-cnt');
  if (c) c.textContent = kinaSoldLabel();
}

function renderAiKina(){
  const T = AI_T[aiLang] || AI_T.vi;
  document.getElementById('ai-title').textContent = T.kinaTitle;
  document.getElementById('ai-foot').textContent = T.kinaFoot;
  const body = document.getElementById('ai-body');
  const th = 'px-3 py-2 cursor-pointer select-none hover:text-white text-gray-300 font-bold';
  body.innerHTML = `
    <div class="mb-3 flex items-center gap-2">
      <input id="ai-kina-q" type="search" value="${esc(aiKinaQ)}"
             oninput="kinaSearch(this.value)" placeholder="${esc(T.kinaSearch)}"
             class="flex-1 px-3 py-2 rounded-lg bg-gray-800 border border-gray-700 text-base
                    text-gray-100 placeholder-gray-500 focus:outline-none focus:border-amber-500">
      <span id="ai-kina-cnt" class="text-sm text-gray-400 whitespace-nowrap"></span>
      <button onclick="aiResetKinaSold()"
              class="px-3 py-2 rounded-lg bg-gray-700 hover:bg-rose-700 text-gray-200 text-sm
                     font-bold whitespace-nowrap transition-colors"
              title="${esc(T.kinaResetTip)}">${esc(T.kinaReset)}</button>
    </div>
    <table class="w-full text-base">
      <thead><tr class="text-left border-b border-gray-700">
        <th id="ai-kina-h-pid"  data-label="${esc(T.kinaHPc)}"   onclick="kinaSortBy('pid')"  class="${th}"></th>
        <th id="ai-kina-h-acct" data-label="${esc(T.kinaHAcct)}" onclick="kinaSortBy('acct')" class="${th}"></th>
        <th id="ai-kina-h-srv"  data-label="${esc(T.kinaHSrv)}"  onclick="kinaSortBy('srv')"  class="${th}"></th>
        <th id="ai-kina-h-kina" data-label="${esc(T.kinaHKina)}" onclick="kinaSortBy('kina')" class="${th} text-right"></th>
        <th class="px-3 py-2 text-center text-gray-300 font-bold whitespace-nowrap">${esc(T.kinaHSold)}</th>
      </tr></thead>
      <tbody id="ai-kina-tbody"></tbody>
    </table>`;
  kinaPaint();
}

function renderAiHunt(){
  const T = AI_T[aiLang] || AI_T.vi;
  document.getElementById('ai-title').textContent = T.huntTitle;
  document.getElementById('ai-foot').textContent = T.huntFoot;
  const rows = aiBuildHunt();
  const body = document.getElementById('ai-body');
  const sum = document.getElementById('ai-summary');
  if (!rows.length) {
    body.innerHTML = `<div class="text-emerald-400 text-sm py-8 text-center font-bold">${T.huntNone}</div>`;
    sum.textContent = '';
    return;
  }
  // ★남은 수는 '오늘 도는 계정' 만★ — 며칠째 안 뜬 카드까지 더하면 숫자가 부푼다
  const nAcct = rows.reduce((s, r) => s + r.accts.filter(a => !a.stale).length, 0);
  const nLeft = rows.reduce((s, r) => s + r.left, 0);
  const nStale = rows.reduce((s, r) => s + r.accts.filter(a => a.stale).length, 0);
  sum.textContent = T.huntSummary(rows.length, nAcct, nLeft) +
                    (nStale ? `  ·  ${esc(T.huntStaleNote)} ${nStale}` : '');
  let h = '';
  rows.forEach(r => {
    // ★사람이 가야 하는 PC 가 먼저다★ — 「사냥중」은 기다리면 되지만 이건 지금 가야 한다
    const heldBadge = r.held
      ? `<span style="background:rgba(244,63,94,.22);color:#fda4af;border:1px solid #fb7185"
               class="px-2 py-0.5 rounded text-xs font-extrabold">${esc(T.huntHuman)}</span>
         <span class="text-[11px] text-rose-300/90">${esc(T.huntHumanWhy(r.heldAcct, aiStLabel(r.heldSt)))}${
             r.heldWho ? esc(T.huntHumanWho(r.heldWho.name, r.heldWho.slot)) : ''}</span>`
      : '';
    // ★사고 380★ 계정 순회가 걸려 있으면 그걸 보여준다 — 「걸었다」를 화면에서 확인한다
    const tourBadge = r.tour
      ? `<span style="background:rgba(129,140,248,.2);color:#c7d2fe;border:1px solid #818cf8"
               class="px-2 py-0.5 rounded text-xs font-bold" title="계정 순회 ${esc(r.tourSeq)}">🔁 ${esc(r.tour)}</span>`
      : '';
    const busyBadge = (!r.held && r.busy)
      ? `<span style="background:rgba(16,185,129,.2);color:#6ee7b7;border:1px solid #34d399"
               class="px-2 py-0.5 rounded text-xs font-bold">${esc(aiStLabel(r.busySt) || T.huntBusy)}</span>
         <span class="text-[11px] text-emerald-300/80">${esc(T.huntBusyOther(r.busyAcct))}</span>`
      : '';
    h += `<div class="mb-3 rounded-lg border ${r.held ? 'border-rose-700' : (r.busy ? 'border-emerald-800/70' : 'border-amber-800/70')} bg-gray-800/40">
      <div class="flex items-center gap-2 px-3 py-2 border-b border-gray-700/60 flex-wrap">
        <span style="font-size:16px;font-weight:800;color:#fff">${esc(r.base)}</span>
        ${tourBadge}
        ${heldBadge}${busyBadge}
        ${(r.left || !r.minStale)
          ? `<span class="ml-auto text-xs ${r.busy ? 'text-gray-400' : 'text-amber-300 font-bold'}">${r.left} ${esc(T.huntLeft)}</span>`
          : `<span class="ml-auto text-xs text-gray-500 font-bold" title="${esc(T.huntStaleNote)}">${esc(T.huntStale(r.minStale))}</span>`}
        ${r.staleLeft ? `<span class="text-[11px] text-gray-500">(+${r.staleLeft} ${esc(T.huntStaleNote)})</span>` : ''}
      </div>`;
    r.accts.forEach(a => {
      const dot = a.busy ? '#34d399' : (a.online ? '#9ca3af' : '#6b7280');
      // ★며칠째 안 뜬 계정은 흐리게 + 이유를 적는다★ — 지우지 않는 이유는 위 주석 참조
      const staleTag = a.stale
        ? `<span class="text-[11px] px-1.5 py-0.5 rounded"
                 style="background:rgba(148,163,184,.15);color:#94a3b8;border:1px solid #475569"
                 title="${esc(T.huntStaleNote)}">${esc(T.huntStale(a.stale))}</span>` : '';
      h += `<div class="flex items-center gap-2 px-3 py-1.5" style="${a.stale ? 'opacity:.5' : ''}">
        ${acctTagSpread(a.pc)}
        <span class="text-sm font-bold text-gray-100" style="min-width:5.5rem">${esc(a.pc)}</span>
        <span class="text-xs" style="color:${dot}">${esc(a.online ? a.label : T.huntOff)}</span>
        ${staleTag}
        <span class="ml-auto text-xs text-gray-400">${esc(T.huntSlot)}
          <b class="${a.done ? 'text-cyan-300' : 'text-gray-500'}">${a.done}</b>/${a.total}
          <b class="${a.stale ? 'text-gray-500' : 'text-amber-300'}">(${a.left} ${esc(T.huntLeft)})</b></span>
      </div>`;
    });
    h += `</div>`;
  });
  body.innerHTML = h;
}

function renderAiPlan(){
  const T = AI_T[aiLang] || AI_T.vi;
  document.getElementById('ai-title').textContent = T.title;
  document.getElementById('ai-foot').textContent = T.foot;
  // 탭 버튼 라벨 + 활성 표시 (2026-09-09: 던전 하나 · 사냥 · 키나 = 셋)
  const bDg = document.getElementById('ai-f-dg');
  const bHt = document.getElementById('ai-f-hunt');
  if (bDg) {
    bDg.textContent = T.fDg;
    const on = 'text-xs px-2.5 py-1 rounded font-bold bg-fuchsia-700 text-white';
    const off = 'text-xs px-2.5 py-1 rounded font-bold bg-gray-700 text-gray-300 hover:bg-gray-600';
    // ★사냥 탭은 초록 계열★ — 던전(자홍)과 데이터도 목적도 달라서 색으로 갈라 둔다
    const onHunt = 'text-xs px-2.5 py-1 rounded font-bold bg-emerald-700 text-white';
    bDg.className = (aiFilter === 'dg') ? on : off;
    if (bHt) { bHt.textContent = T.fHunt || '🏹'; bHt.className = (aiFilter === 'hunt') ? onHunt : off; }
    // ★키나 탭은 호박색★ — 던전(자홍)·사냥(초록)과 목적이 또 다르다
    const bKi = document.getElementById('ai-f-kina');
    const onKina = 'text-xs px-2.5 py-1 rounded font-bold bg-amber-600 text-white';
    if (bKi) { bKi.textContent = T.fKina || '💰'; bKi.className = (aiFilter === 'kina') ? onKina : off; }
  }
  // ★사냥 탭은 state(실시간 카드)를 보므로 여기서 갈라 나간다★
  if (aiFilter === 'hunt') { renderAiHunt(); return; }
  if (aiFilter === 'kina') { renderAiKina(); return; }
  const plan = aiBuildPlan();
  const body = document.getElementById('ai-body');
  if (!plan.length) { body.innerHTML = `<div class="text-gray-400 text-sm py-8 text-center">${T.empty}</div>`;
                      document.getElementById('ai-summary').textContent = ''; return; }
  const nChar = plan.reduce((s,a)=>s+a.chars.length,0);
  const nDone = plan.reduce((s,a)=>s+a.chars.filter(c=>aiDone.keys.includes(c.key)).length,0);
  document.getElementById('ai-summary').textContent = T.summary(plan.length, nChar, `${nDone}/${nChar}`);
  let h = '';
  plan.forEach(a => {
    const badge = a.sub
      ? `<span style="background:rgba(16,185,129,.2);color:#6ee7b7;border:1px solid #34d399" class="px-2 py-0.5 rounded text-xs font-bold">${T.sub}</span>`
      : `<span style="background:rgba(239,68,68,.2);color:#fca5a5;border:1px solid #f87171" class="px-2 py-0.5 rounded text-xs font-bold">${T.nosub}</span>`;
    const aDone = a.chars.filter(c=>aiDone.keys.includes(c.key)).length;
    h += `<div class="mb-3 rounded-lg border ${a.sub?'border-gray-700':'border-red-900/60'} bg-gray-800/40">
      <div class="flex items-center gap-2 px-3 py-2 border-b border-gray-700/60 flex-wrap">
        <span style="font-size:16px;font-weight:800;color:#fff">${esc(baseId(a.pc))}</span>
        ${acctTagSpread(a.pc)}
        ${badge}
        ${aiAcctInfo(a.pc)}
        <span class="ml-auto text-xs ${aDone===a.chars.length?'text-emerald-400 font-bold':'text-gray-400'}">${aDone}/${a.chars.length}</span>
      </div>`;
    if (!a.sub) h += `<div class="px-3 py-1 text-[11px] text-red-300">${T.warn}</div>`;
    a.chars.forEach(c => {
      const done = aiDone.keys.includes(c.key);
      // ★체크해도 자리는 그대로★ — 흐리게 + 취소선으로만 표시한다(정렬은 위에서 고정).
      // ★키는 data-k 로 (2026-09-23)★ — slot 은 PC 가 보낸 값이라 onchange 문자열에 넣으면 XSS 였다.
      h += `<div class="flex items-center gap-2 px-3 py-1.5" style="${done?'opacity:.45':''}">
        <input type="checkbox" ${done?'checked':''} data-k="${esc(c.key)}" onchange="aiToggleDone(this.dataset.k, this)"
               style="width:18px;height:18px;accent-color:#22c55e;cursor:pointer;flex:none">
        <span class="text-xs text-gray-500 w-10">${T.slot}${esc(c.slot)}</span>
        <span class="text-sm font-bold text-gray-100 truncate" style="min-width:7rem;${done?'text-decoration:line-through':''}">${esc(c.name)}</span>
        <span class="text-xs text-gray-400">${T.power} <b class="text-amber-300">${c.pw.toLocaleString()}</b></span>
        <span class="text-xs text-gray-400">${T.energy} <b class="text-cyan-300">${c.daily}</b>/${a.max}</span>
        <span class="text-xs text-gray-500">(${T.bonus} +${c.bonus.toLocaleString()})</span>
      </div>`;
    });
    h += `</div>`;
  });
  body.innerHTML = h;
}

// ─── 음성 알림 (TTS) ─────────────────────────────────────────────────────────
// 브라우저 내장 speechSynthesis. 서버는 텍스트만 보내고 발화는 전부 여기서 한다.
// ★자동재생 정책: 사용자 제스처 없이 speak()를 처음 부르면 크롬이 무시한다.
//   그래서 반드시 '🔊 토글 클릭' 안에서 첫 발화를 태워 잠금을 푼다.★
let ttsOn = localStorage.getItem('ttsOn')==='1';
let _ttsVoice = null;                  // 폴백용 브라우저 음성
let _koVoices = [];
let _audio = null;                     // 현재 재생 중인 서버 음성
const _alertSeen = new Map();          // "kind|pc" → 마지막 발화 시각 (중복 억제)

// ratePct/pitchHz 는 서버(edge-tts) 단위. 브라우저 폴백에서는 배수로 환산해 쓴다.
// 기본값은 낮고 느리게 — 윈도우 기본음성 특유의 기계적인 느낌을 피하는 방향.
// ★최상위 JSON.parse 는 감싼다 (2026-09-11)★ — 여기서 던지면 아래 1,200여 줄이
//   정의조차 안 돼 화면이 백지가 된다(2026-07-27 사고와 같은 증상, 다른 원인).
//   이 파일의 다른 localStorage 파싱은 전부 try 로 감싸져 있는데 여기만 빠져 있었다.
function _ttsSaved(){
  try { return JSON.parse(localStorage.getItem('ttsCfg') || '{}') || {}; }
  catch (e) { return {}; }
}
const ttsCfg = Object.assign(
  { engine: 'server', name: '', ratePct: 25, pitchHz: 18 },
  _ttsSaved()
);
function saveTtsCfg(){ localStorage.setItem('ttsCfg', JSON.stringify(ttsCfg)); }
function _sgn(n){ return (n >= 0 ? '+' : '') + n; }

// 브라우저 음성 품질 점수(폴백 전용). 같은 한국어라도 엔진 차이가 크다.
function voiceScore(v){
  const n = (v.name || ''), low = n.toLowerCase();
  let s = 0;
  if(/^ko/i.test(v.lang || '')) s += 100;
  if(low.indexOf('natural') >= 0 || low.indexOf('neural') >= 0) s += 80;
  if(low.indexOf('google') >= 0) s += 50;
  if(n.indexOf('SunHi') >= 0) s += 30;
  if(n.indexOf('InJoon') >= 0) s -= 30;         // 남성
  if(n.indexOf('Heami') >= 0) s -= 10;          // 윈도우 로컬, 기계적
  if(!v.localService) s += 10;
  return s;
}

function refreshVoices(){
  if(!window.speechSynthesis) return;
  const all = speechSynthesis.getVoices() || [];
  _koVoices = all.filter(v => /^ko/i.test(v.lang || '')).sort((a,b) => voiceScore(b) - voiceScore(a));
  const pool = _koVoices.length ? _koVoices : all;
  _ttsVoice = pool.find(v => v.name === ttsCfg.name) || pool[0] || null;
  renderVoiceOptions();
}
if(window.speechSynthesis){
  refreshVoices();
  speechSynthesis.onvoiceschanged = refreshVoices;   // 크롬은 음성 목록을 비동기로 채운다
  // 크롬이 장시간 유휴 후 큐를 멈춰 세우는 버그 회피
  setInterval(()=>{ try{ if(speechSynthesis.paused) speechSynthesis.resume(); }catch(e){} }, 5000);
}

// ★소리로 나가는 숫자는 한자어로★ (#172, 주인님 2026-09-24 «2번은 두번·9번은 아홉번 — 이 번 구 번 이렇게») — 말하기 직전 한 곳.
//   speak(서버 /tts)·speakLocal(브라우저 음성) 맨 앞에서 부른다. 화면 글자는 그대로.
//   ★팜뷰 ui/index.html ttsSino·ttsText 를 글자 그대로 옮겼다★ = farmview/alarmvoice.speak_text —
//   tests/test_integration_contracts.py I8 이 세 구현(이 JS·팜뷰 JS·팜뷰 파이썬)을 같은 표로 묶는다.
function ttsSino(n){ n=Math.floor(+n)||0; if(n<=0) return '영';
  const D=['','일','이','삼','사','오','육','칠','팔','구'], U=['천','백','십','']; const s=String(n).padStart(4,'0'); let o='';
  for(let k=0;k<4;k++){ const v=+s[k]; if(v) o+=((v===1&&U[k])?'':D[v])+U[k]; } return o; }
//   곁가지: 한글 바로 뒤면 띄움 · 쉼표 든 수·번 뒤 조사 아닌 한글(«2번호»·«3 번개»)은 그대로 · PC_09 도.
function ttsText(t){ t=((t==null)?'':String(t)).replace(/[０-９]/g,c=>String.fromCharCode(c.charCodeAt(0)-0xFEE0)); const SFX={a:'에이',b:'비',c:'씨',d:'디'}; const gap=p=>/[가-힣]/.test(p)?p+' ':p;
  t=t.replace(/(^|[^A-Za-z0-9])[Pp][Cc][-_]?0*(\d{1,3})([a-dA-D])?(?![A-Za-z0-9])(?:\s*번(?![호째개역거갈쩍]))?/g,(m,pre,n,x)=>gap(pre)+ttsSino(n)+' 번'+(x?' '+SFX[x.toLowerCase()]:''));
  return t.replace(/(^|[^\d.,]|,(?!\d{3}(?!\d)))(\d{1,4})\s*번(?![호째개역거갈쩍])/g,(m,pre,n)=>gap(pre)+ttsSino(n)+' 번'); }

// ★1순위는 서버 신경망 음성(사람 목소리). 서버가 못 만들면 브라우저 내장 음성으로
//   자동 폴백한다 — 목소리는 아쉬워도 알림 자체가 끊기면 안 되기 때문.★
function speak(text, force){
  text=ttsText(text);
  if((!ttsOn && !force) || !text) return;
  if(ttsCfg.engine !== 'server'){ speakLocal(text); return; }
  try{
    if(_audio){ try{ _audio.pause(); }catch(e){} }
    const url = '/tts?text=' + encodeURIComponent(text)
              + '&rate=' + encodeURIComponent(_sgn(ttsCfg.ratePct) + '%')
              + '&pitch=' + encodeURIComponent(_sgn(ttsCfg.pitchHz) + 'Hz');
    const a = new Audio(url);
    _audio = a;
    a.onerror = () => speakLocal(text);
    a.play().catch(() => speakLocal(text));
  }catch(e){ speakLocal(text); }
}

function speakLocal(text){
  text=ttsText(text);
  if(!window.speechSynthesis || !text) return;
  try{
    const u = new SpeechSynthesisUtterance(text);
    if(_ttsVoice) u.voice = _ttsVoice;
    u.lang = 'ko-KR';
    u.rate  = Math.min(2, Math.max(0.5, 1 + ttsCfg.ratePct / 100));
    u.pitch = Math.min(2, Math.max(0.1, 1 + ttsCfg.pitchHz / 50));
    u.volume = 1.0;
    speechSynthesis.speak(u);
  }catch(e){}
}

// ─── 목소리 고르기 패널 ──────────────────────────────────────────────────────
function toggleVoicePanel(){
  const p = document.getElementById('voice-panel');
  if(!p) return;
  p.classList.toggle('hidden');
  if(!p.classList.contains('hidden')){ refreshVoices(); }
}

function renderVoiceOptions(){
  const sel = document.getElementById('tts-voice');
  if(!sel) return;
  let html = '<option value="server"' + (ttsCfg.engine === 'server' ? ' selected' : '') + '>'
           + '⭐ 사람 목소리 (서버 · SunHi)</option>';
  const list = _koVoices.length ? _koVoices : (window.speechSynthesis ? speechSynthesis.getVoices() : []);
  html += list.map(v =>
    '<option value="' + escAttr(v.name) + '"'
    + (ttsCfg.engine !== 'server' && v.name === ttsCfg.name ? ' selected' : '') + '>'
    + esc(v.name) + ' (브라우저)</option>').join('');
  sel.innerHTML = html;
  const r = document.getElementById('tts-rate'), p = document.getElementById('tts-pitch');
  if(r) r.value = ttsCfg.ratePct;
  if(p) p.value = ttsCfg.pitchHz;
}

function onVoiceChange(){
  const sel = document.getElementById('tts-voice');
  if(!sel) return;
  if(sel.value === 'server'){ ttsCfg.engine = 'server'; }
  else { ttsCfg.engine = 'browser'; ttsCfg.name = sel.value; }
  saveTtsCfg(); refreshVoices();
  speak('안녕하세요, 이 목소리로 알려드릴게요', true);
}
function onVoiceTune(){
  const r = document.getElementById('tts-rate'), p = document.getElementById('tts-pitch');
  if(r) ttsCfg.ratePct = parseInt(r.value, 10);
  if(p) ttsCfg.pitchHz = parseInt(p.value, 10);
  saveTtsCfg();
}
function previewVoice(){
  speak('칠번, 캡챠! 캡챠!', true);
}

// "PC-07" → "칠번". 그냥 넘기면 TTS가 "피 씨 공 칠"처럼 읽고,
// 숫자로 넘겨도 엔진에 따라 "칠"/"일곱"이 갈려서 한글로 못박는다.
const _SINO = ['','일','이','삼','사','오','육','칠','팔','구'];
function koNum(n){
  if(n < 10) return _SINO[n];
  if(n < 20) return '십' + (n % 10 ? _SINO[n % 10] : '');
  return _SINO[Math.floor(n / 10)] + '십' + (n % 10 ? _SINO[n % 10] : '');
}
function spokenPcName(pc){
  const m = /^PC-?0*(\d+)$/i.exec(pc || '');
  if(!m) return pc || '';
  const n = parseInt(m[1], 10);
  return (n > 0 && n < 100 ? koNum(n) : m[1]) + '번';
}

function renderTtsBtn(){
  const b = document.getElementById('tts-btn');
  if(!b) return;
  b.textContent = ttsOn ? '🔊 음성 켜짐' : '🔇 음성 꺼짐';
  b.className = 'px-3 py-1 rounded-lg text-xs font-semibold transition-colors whitespace-nowrap '
    + (ttsOn ? 'bg-emerald-700/80 hover:bg-emerald-600 text-emerald-50'
             : 'bg-gray-700/70 hover:bg-gray-600 text-gray-300');
  b.title = ttsOn ? '알림이 오면 소리내어 읽습니다 (끄려면 클릭)'
                  : '켜두면 캡차 실패 같은 알림을 음성으로 읽어줍니다';
}

// ★첫 재생 지연 대비: 서버가 처음 만드는 문구는 3초 넘게 걸린다(신경망 합성).
//   알림이 3초 늦게 울리면 의미가 반감되므로, 음성을 켜는 순간 각 PC의 알림 문구를
//   미리 한 번씩 요청해 서버·브라우저 캐시에 올려둔다. 소리는 나지 않는다.★
let _prewarmed = false;
async function prewarmTts(){
  if(_prewarmed || ttsCfg.engine !== 'server') return;
  _prewarmed = true;
  const pcs = Object.keys(state || {}).slice(0, 24);
  for(const pc of pcs){
    const say = spokenPcName(pc) + ', 캡챠! 캡챠!';
    const url = '/tts?text=' + encodeURIComponent(say)
              + '&rate=' + encodeURIComponent(_sgn(ttsCfg.ratePct) + '%')
              + '&pitch=' + encodeURIComponent(_sgn(ttsCfg.pitchHz) + 'Hz');
    try{ await fetch(url, {cache:'force-cache'}); }catch(e){}
    await new Promise(s => setTimeout(s, 150));   // 합성 서버 과부하 방지
  }
}

function toggleTts(){
  ttsOn = !ttsOn;
  localStorage.setItem('ttsOn', ttsOn ? '1' : '0');
  renderTtsBtn();
  if(ttsOn){ refreshVoices(); speak('음성 알림을 켰습니다'); prewarmTts(); }
  else { try{ speechSynthesis.cancel(); }catch(e){} showToast('🔇 음성 알림 꺼짐'); }
}

function handleAlert(msg){
  const pc = msg.pc_id || '';
  const key = (msg.kind || '') + '|' + pc;
  const now = Date.now();
  if(now - (_alertSeen.get(key) || 0) < 60000) return;   // 같은 알림 1분 내 재발화 억제
  _alertSeen.set(key, now);
  showAlertBanner(pc, msg.message || '');
  // ★읽는 문구(say)와 화면 문구(message)를 분리한다 — 화면은 자세히, 귀에는 짧게.
  //   긴 문장을 그대로 읽으면 다 듣기 전에 놓친다(사용자: "말이 길면 별로야").★
  //   ★쉼표로 잇고 통짜로 합성한다. '|'로 쪼개 이어붙이면 조각마다 앞뒤 무음이 붙어
  //     단어 사이 텀이 길어진다(사용자 지적). 억양은 속도·느낌표로 만든다.★
  if(msg.speak !== false) speak(spokenPcName(pc) + ', ' + (msg.say || msg.message || ''));
}

function showAlertBanner(pc, message){
  const wrap = document.getElementById('alert-stack');
  if(!wrap) return;
  const t = new Date().toTimeString().slice(0,8);
  const el = document.createElement('div');
  el.className = 'flex items-start gap-2 bg-rose-900/90 border border-rose-500/60 text-rose-50 '
               + 'text-xs px-3 py-2 rounded-lg shadow-xl cursor-pointer max-w-md';
  // ★esc() 필수 — 매크로가 보낸 문자열이다 (저장형 XSS 전례)★
  el.innerHTML = '<span class="text-base leading-none">🔔</span>'
    + '<span class="flex-1"><b>' + esc(pc) + '</b> · ' + esc(message)
    + '<span class="block text-[10px] text-rose-200/70 mt-0.5">' + t + ' · 클릭하면 닫힘</span></span>';
  el.onclick = () => el.remove();
  wrap.prepend(el);
  while(wrap.children.length > 5) wrap.lastChild.remove();
  setTimeout(()=>el.remove(), 120000);
}

let _toastTimer;
function showToast(msg) {
  const t=document.getElementById('toast');
  t.textContent=msg; t.classList.remove('hidden'); t.style.opacity='1';
  clearTimeout(_toastTimer);
  _toastTimer=setTimeout(()=>{t.style.opacity='0';setTimeout(()=>t.classList.add('hidden'),300);},2500);
}

// ─── 전역 클릭 → 메뉴 닫기 ──────────────────────────────────────────────────
document.addEventListener('click',()=>{
  if(!document.getElementById('card-menu').classList.contains('hidden')) closeCardMenu();
});

// ─── 업데이터 명령 ────────────────────────────────────────────────────────────
async function sendUpdaterCmd(pc_id, command, args={}) {
  // ★★적대검증 중간5 — ↺껐다켜기·업데이트가 표시를 아예 안 걸었다★★
  //   가장 오래 걸리는 조작(재시작 ~40초, 업데이트 수 분)이 여전히 「대기」로 남아
  //   주인님이 또 누르시게 만든다 = 원 요구의 절반이 비어 있었다.
  //   ★업데이터 큐는 다른 엔드포인트(§B3)★ 라 cmd_history ack 이 안 온다 →
  //   해제는 ④ttl 과 ①상태 변화에만 기댄다. 그래서 ttl 을 넉넉히 준다.
  const _pendU = pendRegister(pc_id, command, args);
  if (_pendU) scheduleRenderNow();
  const _base = baseId(pc_id);
  try {
    const res = await fetch(`/updater/command/${_base}`, {   // 업데이터=base id (멀티계정)
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command, args})
    });
    if (_pendU && !res.ok && pendingCmds[_pendU.base] === _pendU) {
      delete pendingCmds[_pendU.base]; scheduleRenderNow();
    }
    // ★결과만(2026-09-22)★ — update/restart 만 추적. update_only 는 대상 밖(사람이 안 부른다).
    if (res.ok && (command === 'update' || command === 'restart')) {
      updResultStart(_base, (state[_base]||{})._updater_version || '', command);
    }
    return res.ok;
  } catch (e) {          // ★네트워크 예외도 실패다 (2026-08-22)★ 안 잡으면 호출부가 통째로 죽는다
    console.error('sendUpdaterCmd 실패', pc_id, command, e);
    if (_pendU && pendingCmds[_pendU.base] === _pendU) {
      delete pendingCmds[_pendU.base]; scheduleRenderNow();
    }
    return false;
  }
}

// ★전체 대상 업데이터 명령★ — 지금 화면에 호출부가 없다. 생기면 반드시 확인창을 붙일 것.
//   (2026-08-28: selUpdaterCmd 가 확인 없이 23대에 나간 사고가 있었다)
async function bulkUpdaterCmd(command, args={}) {
  const ids = [...new Set(Object.keys(state).map(baseId))];   // 계정 카드 중복 → base로 접어 1대당 1회
  if (!ids.length) { showToast('연결된 PC 없음'); return; }
  // ★결과를 세어서 말한다 (2026-08-22 사고 146)★ — 옛 코드는 Promise.all 반환값을 버렸다.
  const oks = await Promise.all(ids.map(id => sendUpdaterCmd(id, command, args)));
  const bad = oks.filter(v => !v).length;
  if (bad) showToast(`⛔ 업데이터 ${command} — ${ids.length}대 중 ${bad}대 전송 실패`);
  else showToast(`✓ 업데이터 ${command} → 전체 ${ids.length}대 전송됨`);
}

async function updaterCmd(command, args={}) {
  if (!menuPcId) return;
  const ok = await sendUpdaterCmd(menuPcId, command, args);
  showToast(ok ? `✓ 업데이터 ${command} → ${menuPcId} 전송됨`
               : `⛔ 업데이터 ${command} → ${menuPcId} ★전송 실패★`);
  closeCardMenu();
}

// ─── 버그 모달 ────────────────────────────────────────────────────────────────
let bugModalPc = null;

async function openBugsModal(pc_id) {
  // ══════════════════════════════════════════════════════════
  // ★★스샷은 ★계정 스택 공통★ 이다 (2026-08-23 주인님 지적)★★
  //   원문: "지금 계정2가 돌가잇는데 카드에다가 스샷을 요청했는데 이게 꼭 계정1에만
  //          스샷 관련이떠잇네? 이건 공통으로 뜨게해야하는데"
  //
  //   ★왜 그랬나★ 매크로는 스샷을 ★base pc_id★ 로 올린다(파일명 실측:
  //   `PC-24_20260822_222222_PC-24b_...` — 앞이 base, 뒤가 그때 슬롯).
  //   그런데 조회는 `/bugs/${pc_id}` 라 계정2 카드는 `PC-20b` 로 물어보고
  //   ★빈 목록★ 을 받았다. 스샷은 그 PC 한 대의 것이지 계정별로 나뉘지 않는다.
  //   → 조회를 base 로 통일한다. 어느 계정 카드에서 눌러도 같은 목록이 나온다.
  // ══════════════════════════════════════════════════════════
  const _base = (typeof baseId === 'function') ? baseId(pc_id) : String(pc_id).replace(ACCT_SUF_RE, '');
  bugModalPc = _base;
  document.getElementById('bug-modal-title').textContent =
    `버그 스크린샷 — ${_base}` + (_base !== pc_id ? ` (${pc_id} 에서 열음 · 스택 공통)` : '');
  // href 대신 onclick으로 교체 (다운로드 후 모달 갱신)
  const dlBtn = document.getElementById('bug-download-link');
  dlBtn.onclick = (e) => { e.preventDefault(); downloadAndClearBugs(_base); };
  document.getElementById('bug-clear-btn').onclick = () => clearBugsOf(_base);
  document.getElementById('bug-modal').classList.remove('hidden');
  const el = document.getElementById('bug-list');
  el.innerHTML = '<div class="text-gray-600 text-sm">로딩 중...</div>';
  const res = await fetch(`/bugs/${_base}`);
  if (!res.ok) { el.innerHTML = '<div class="text-red-400 text-sm">로드 실패</div>'; return; }
  const data = await res.json();
  const bugs = data.bugs || [];
  if (!bugs.length) { el.innerHTML = '<div class="text-gray-600 text-sm py-6 text-center">버그 없음</div>'; return; }
  el.innerHTML = bugs.map(b => `
    <div class="bg-gray-800 rounded-lg p-3 border border-gray-700">
      <div class="flex items-center justify-between mb-2">
        <span class="text-xs text-gray-400 font-mono truncate mr-2">${esc(b.filename)}</span>
        <div class="flex items-center gap-2 shrink-0">
          <span class="text-xs text-gray-600">${(b.size/1024).toFixed(1)}KB</span>
          <button data-f="${esc(b.filename)}" onclick="deleteBug(this.dataset.f)" class="text-xs text-red-500 hover:text-red-400 transition-colors">🗑</button>
        </div>
      </div>
      <img src="/bugs/image/${esc(encodeURIComponent(b.filename))}" class="w-full rounded border border-gray-700 cursor-pointer hover:opacity-90 transition-opacity" onclick="window.open(this.src,'_blank')" alt="${esc(b.filename)}" loading="lazy">
    </div>
  `).join('');
}

function closeBugsModal() {
  bugModalPc = null;
  document.getElementById('bug-modal').classList.add('hidden');
}

async function downloadAndClearBugs(pc_id) {
  const url = `/bugs/download?pc_id=${encodeURIComponent(pc_id)}`;
  try {
    const res = await fetch(url);
    if (res.status === 404) { showToast('다운로드할 이미지 없음'); return; }
    if (!res.ok) { showToast('다운로드 실패'); return; }
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `bugs_${pc_id}_${Date.now()}.zip`;
    a.click();
    URL.revokeObjectURL(a.href);
    // 서버에서 이미 삭제됨 → 모달 내용 갱신
    showToast(`⬇ 다운로드 완료 · 서버 이미지 삭제됨`);
    if (bugModalPc) openBugsModal(bugModalPc);
  } catch(e) { showToast('다운로드 오류'); }
}

async function deleteBug(filename) {
  if (!confirm(`${filename}\n삭제하시겠습니까?`)) return;
  const res = await fetch(`/bugs/image/${encodeURIComponent(filename)}`, {method:'DELETE'});
  if (res.ok) { showToast('🗑 버그 삭제됨'); if (bugModalPc) openBugsModal(bugModalPc); }
  else showToast('삭제 실패');
}

// 버그스샷 일괄 삭제 (2026-07-27 사용자 요청 — 하나씩 지우기 번거로움)
async function clearBugsOf(pc_id) {
  if (!confirm(`${pc_id}의 버그 스크린샷을 전부 삭제할까요?`)) return;
  const res = await fetch(`/bugs?pc_id=${encodeURIComponent(pc_id)}`, {method:'DELETE'});
  if (!res.ok) { showToast('삭제 실패'); return; }
  const d = await res.json();
  showToast(`🧹 ${pc_id} 스샷 ${d.removed||0}장 삭제`);
  if (bugModalPc) openBugsModal(bugModalPc);
}

async function clearAllBugs() {
  if (!confirm('모든 PC의 버그 스크린샷을 전부 삭제할까요?')) return;
  const res = await fetch('/bugs', {method:'DELETE'});
  if (!res.ok) { showToast('삭제 실패'); return; }
  const d = await res.json();
  showToast(`🧹 스샷 ${d.removed||0}장 전부 삭제`);
  if (bugModalPc) openBugsModal(bugModalPc);
}

// ─── 캐릭터 세부정보 모달 ────────────────────────────────────────────────────
let infoModalPc = null;
let charInfoCache = {};  // pc_id → {total_kina, chars, collected_at}

function fmtNum(n) { return (n==null||n==='')?'–':Number(n).toLocaleString('en-US'); }
function fmtPower(n) {
  if (n==null||n===''||n===0) return '–';
  const v = Number(n);
  if (!v) return '–';
  const k = v / 1000;
  return (Number.isInteger(k) ? k : k.toFixed(1)) + ' K';
}
function fmtSlotUptime(slotUptime, activeSlot, fallback) {
  let hours = null;
  if (slotUptime && activeSlot) {
    const h = slotUptime[String(activeSlot)];
    if (h != null) hours = h;
  }
  if (hours == null && fallback) hours = Number(fallback);
  if (hours == null) return '–';
  const totalMin = Math.round(hours * 60);
  if (totalMin < 60) return totalMin + 'm';
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return m > 0 ? h + 'h ' + m + 'm' : h + 'h';
}
function fmtAt(iso) {
  if (!iso) return '–';
  return fmtLocalAt(iso, 0, 16);   // ★B-JS6 (2026-09-23)★ UTC 원문을 로컬처럼 보이던 것 → 보는 기기 로컬 시각
}

// ─── 전체 캐릭터 테이블 ────────────────────────────────────────────────────
let charTableData = [];
let charTableSort = {key:'pc_id', asc:true};
// ★악몽 도전 티켓 상한 — 한 곳(2026-09-23)★ 예전엔 「N/14」 와 「>=14 빨강」 이 세 곳에 박혀 있었다.
//   ★주인님 결정 #82 (2026-09-24)★ «하루 2장, 최대 14까지 쌓임» → 상한 14 · 12 이상 경고색(곧 가득 — 이틀 안 쓰면 넘친다)
//   · 14 이상 빨강(가득 — 새 티켓이 버려진다). ★상한을 넘는 실측(16 등)은 오류로 버리지 않고 그대로 「16/14」★(빨강).
//   리셋 시각(5시)은 미확인 가정 — 이 표시는 리셋 시각을 쓰지 않는다. null 로 되돌리면 옛 동작(「N」 만·색 없음).
const NIGHTMARE_TICKET_MAX = 14, NIGHTMARE_TICKET_WARN = 12;
function nmTicketText(n){
  if (n == null || n === '') return '–';
  return NIGHTMARE_TICKET_MAX != null ? `${n}/${NIGHTMARE_TICKET_MAX}` : String(n);
}
function nmTicketFull(n){
  return NIGHTMARE_TICKET_MAX != null && n != null && n !== '' && Number(n) >= NIGHTMARE_TICKET_MAX;
}
function nmTicketWarn(n){   // 경고색 = 12·13 (14 이상은 nmTicketFull 빨강이 이긴다). 숫자가 아니면(«?») 색 없음
  return NIGHTMARE_TICKET_WARN != null && n != null && n !== '' && !nmTicketFull(n) && Number(n) >= NIGHTMARE_TICKET_WARN;
}
let charTableVisible = false;
// ★은퇴·계정없음 PC 는 여기서 한 번에 뺀다(2026-09-23 주인님)★ — 「24번이 구독으로
//   표시돼서 구독 자료 자체를 흐린다」. char_info 옛 행은 no_account 로 바뀌어도 안 지운다
//   (업데이터 생존은 살려야 하니까) — 그래서 /characters 는 여전히 옛 캐릭을 준다.
//   ★소스에서 뺀다★ — subState·dkSubCount·isAwakenDone·surfaceZeroSlots·전광판 오드에너지
//   합계까지 전부 charTableData 하나만 보므로, 여기 한 곳만 고치면 전부 같이 빠진다(§A12).
//   은퇴 id 는 사실 char_info 자체가 삭제돼 애초에 안 실려 오지만, 타이밍 방어로 같이 거른다.
function isExcludedPc(pc_id){
  if (!pc_id) return true;
  if (RETIRED.has(pc_id)) return true;
  // ★가짜 PC(PC-TEST·PC-DEMO)도 같은 한 곳에서 뺀다(2026-09-23 반증 2바퀴)★ — 서버
  //   _fv_pc_excluded 와 같은 규칙. 빠져 있으면 /summary 가 낡아 폴백으로 떨어질 때마다
  //   거래키나·각성전·회랑 타일이 가짜 PC 몫만큼 튀었다(서버 K10 vs 폴백 K5010).
  if (isFakePc(pc_id)) return true;
  return (state[pc_id]||{}).status === 'no_account';
}

function toggleCharTable() {
  charTableVisible = !charTableVisible;
  document.getElementById('char-table-wrap').classList.toggle('hidden', !charTableVisible);
  document.getElementById('char-table-arrow').textContent = charTableVisible ? '▼' : '▶';
  if (charTableVisible && charTableData.length === 0) loadCharTable();
}

async function loadCharTable() {
  try {
    // ★캐시버스터+no-store: 브라우저가 GET /characters를 캐시해 새로고침 눌러도 옛 데이터
    //   보여주던 문제 수정(수집 직후 갱신 안 되던 원인).
    const r = await fetch('/characters?t=' + Date.now(), {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    CHAR_TABLE_AT = sumClock();
    // ★각성전(3)·일일던전(14) 티켓도 리셋 이전 값이면 가득 찬 값으로 보정(2026-09-23)★
    //   ★한 곳에서 바꾼다★ — 테이블 칸·isAwakenDone·전광판 각성 합계·베트남 표가 전부
    //   이 charTableData 하나만 보므로, 여기서 고치면 전부 같이 고쳐진다(§A12).
    charTableData = (d.characters || []).filter(r => !isExcludedPc(r.pc_id)).map(r => ({
      ...r,
      daily_ticket: resetAwareTicket(r.collected_at, r.daily_ticket, 14),
      awakening_ticket: resetAwareTicket(r.collected_at, r.awakening_ticket, 3),
      sanctuary: resetAwareSanctuary(r.collected_at, r.sanctuary),
    }));
    document.getElementById('char-table-count').textContent = `(${charTableData.length})`;
    renderCharTable();
    renderCards();   // 각성완료 뱃지가 charTableData 기반 — 로드 후 카드 재렌더(내부에서 refreshSummary 호출)
    // ★★AI 던전 추천도 같이 다시 그린다 (2026-08-24 주인님 지적)★★
    //   aiBuildPlan() 은 charTableData 를 캐시 없이 매번 다시 읽는다. 그런데 이 함수가
    //   renderCharTable/renderCards 만 부르고 renderAiPlan 은 안 불러서, ★모달을 열어둔 채★
    //   정보수집이 끝나면 화면이 옛 오드에너지 그대로였다(직원분들은 이 모달을 띄워놓고 일한다).
    //   모달이 닫혀 있으면 아무 일도 하지 않는다 — 열려 있을 때만 갱신한다.
    try {
      const _m = document.getElementById("ai-modal");
      if (_m && !_m.classList.contains("hidden")) renderAiPlan();
    } catch(e) { console.error("AI 추천 갱신 실패", e); }
  } catch(e) { console.error('캐릭터 테이블 로드 실패', e); }
}

// 캐릭터 1명(슬롯)만 정보수집 — 스프레드 각 행의 📡 버튼. 나머지 슬롯은 서버가 병합 보존.
async function collectSlot(pc, slot) {
  const ok = await sendCmd(pc, 'collect_info', {slot});
  showToast(ok ? `📡 ${pc} 슬롯 ${slot} 단일 정보수집 요청` : `${pc} 요청 실패`);
  if (typeof loadCmdHistory === 'function') loadCmdHistory();
}

// ─── 베트남어 캐릭터 뷰 (모바일 가시성 + VI/KO 토글 + 행별 자가체크 localStorage) ──────
let vietnamData = [];
let vietnamSort = {key:'pc_id', asc:true};
let vietnamLang = 'vi';   // 기본 베트남어
const VN_T = {
  vi:{title:'🇻🇳 Nhân vật', reset:'Đặt lại tất cả', done:'Xong', nodata:'Không có dữ liệu'},
  ko:{title:'🇰🇷 캐릭터',    reset:'전체 초기화',      done:'완료', nodata:'데이터 없음'},
};
// red: 스프레드와 동일 기준 — 오드 현재≥840(만충), 일일 14/14, 각성 3/3 → 빨간 굵게.
const VIETNAM_COLS = [
  {key:'pc_id',            vi:'PC',        ko:'PC',    align:'left',   fmt:r=>r.pc_id||'–'},
  {key:'slot',             vi:'NV',        ko:'캐릭',   align:'left',   fmt:r=>r.slot||'–'},
  {key:'gear_power',       vi:'Trang bị',  ko:'장비',   align:'right',  fmt:r=>r.gear_power?Number(r.gear_power).toLocaleString():'–', red:r=>{const n=parseInt(r.gear_power)||0;return n>0&&n<3000;}},
  {key:'power_power',      vi:'Power',     ko:'파워',   align:'right',  fmt:r=>r.power_power?Number(r.power_power).toLocaleString():'–', red:r=>{const n=parseInt(r.power_power)||0;return n>0&&n<200000;}},
  {key:'odd_energy',       vi:'Odd',       ko:'오드',   align:'left',   fmt:r=>r.odd_energy||'–', red:r=>{const n=parseInt(r.odd_energy);return !isNaN(n)&&n>=840;}},
  {key:'daily_ticket',     vi:'Ngày',      ko:'일일',   align:'center', fmt:r=>r.daily_ticket||'–', red:r=>{const n=parseInt(r.daily_ticket);return !isNaN(n)&&n>=14;}},
  {key:'awakening_ticket', vi:'Thức tỉnh', ko:'각성',   align:'center', fmt:r=>r.awakening_ticket!=null?r.awakening_ticket+'/3':'–', red:r=>r.awakening_ticket!=null&&r.awakening_ticket>=3},
];
function _ta(a){ return a==='right'?'text-right':a==='center'?'text-center':'text-left'; }
// 자가체크(작업완료) — 기기(휴대폰) localStorage에 저장. pc+slot 키라 데이터 갱신돼도 유지.
function vnKey(pc, slot){ return 'vn_done_'+pc+'_'+slot; }
function vnDone(pc, slot){ return localStorage.getItem(vnKey(pc,slot))==='1'; }
function vnToggle(pc, slot, on){ on?localStorage.setItem(vnKey(pc,slot),'1'):localStorage.removeItem(vnKey(pc,slot)); renderVietnam(); }
function vnResetAll(){ Object.keys(localStorage).filter(k=>k.indexOf('vn_done_')===0).forEach(k=>localStorage.removeItem(k)); renderVietnam(); }
function vnSetLang(l){ vietnamLang=l; renderVietnam(); }
async function openVietnamModal(){
  document.getElementById('vietnam-modal').classList.remove('hidden');
  await loadVietnam();
}
function closeVietnamModal(){ document.getElementById('vietnam-modal').classList.add('hidden'); }
async function loadVietnam(){
  try{
    const r = await fetch('/characters?t='+Date.now(), {cache:'no-store'});
    // ★loadCharTable 과 같은 필터·보정(2026-09-23)★ — 이 뷰가 /characters 를 따로
    //   불러서 은퇴·계정없음 제외와 각성전/일일던전 리셋 보정을 안 받고 있었다(§A12).
    if(r.ok) vietnamData = ((await r.json()).characters || []).filter(x => !isExcludedPc(x.pc_id)).map(x => ({
      ...x,
      daily_ticket: resetAwareTicket(x.collected_at, x.daily_ticket, 14),
      awakening_ticket: resetAwareTicket(x.collected_at, x.awakening_ticket, 3),
      sanctuary: resetAwareSanctuary(x.collected_at, x.sanctuary),
    }));
  }catch(e){ console.error('vietnam load', e); }
  renderVietnam();
}
function sortVietnam(key){
  if(vietnamSort.key===key) vietnamSort.asc = !vietnamSort.asc;
  else vietnamSort = {key, asc:true};
  renderVietnam();
}
// ★B-JS7 (2026-09-23)★ 'PC-03' 이 숫자만 남겨 -3 이 돼 PC 순서가 거꾸로 섰다 → pc_id 는 문자열로 두고
//   자연 정렬(numeric localeCompare). 빈 값(''·'–'·null)은 null — 정렬에서 방향과 무관하게 맨 뒤.
function _vietnamVal(r, key){
  if(key==='odd_energy') return parseOddEnergy(r.odd_energy);
  const v = r[key];
  const t = String(v==null?'':v).trim();
  if(key==='pc_id') return t || null;
  if(!t || t==='–' || t==='-') return null;
  const n = Number(t.replace(/[^\d.-]/g,''));
  return isNaN(n) ? t : n;
}
function _vietnamCmp(a, b, key, asc){
  const va=_vietnamVal(a,key), vb=_vietnamVal(b,key);
  if(va==null || vb==null) return (va==null) - (vb==null);   // 빈 값은 늘 맨 뒤
  if(typeof va==='number' && typeof vb==='number') return asc?va-vb:vb-va;
  const c = String(va).localeCompare(String(vb), undefined, {numeric:true, sensitivity:'base'});
  return asc?c:-c;
}
function renderVietnam(){
  const L = vietnamLang, T = VN_T[L];
  document.getElementById('vn-title').textContent = T.title;
  document.getElementById('vn-reset').textContent = T.reset;
  const on='px-2 py-1 bg-red-700 text-white', off='px-2 py-1 text-gray-400 hover:text-gray-200';
  document.getElementById('vn-lang-vi').className = L==='vi'?on:off;
  document.getElementById('vn-lang-ko').className = L==='ko'?on:off;
  const {key, asc} = vietnamSort;
  const rows = [...vietnamData].sort((a,b)=>_vietnamCmp(a,b,key,asc));   // ★B-JS7★
  document.getElementById('vietnam-head').innerHTML =
    `<th class="px-2 py-2 text-center">${T.done}</th>` +
    VIETNAM_COLS.map(c=>{
      const arrow = vietnamSort.key===c.key ? (vietnamSort.asc?' ▲':' ▼') : ' ⇅';
      return `<th class="px-3 py-2 cursor-pointer hover:text-white ${_ta(c.align)}" onclick="sortVietnam('${c.key}')">${c[L]}${arrow}</th>`;
    }).join('');
  document.getElementById('vietnam-body').innerHTML = rows.length
    ? rows.map(r=>{
        const d = vnDone(r.pc_id, r.slot);
        return `<tr class="${d?'bg-green-900/40':'bg-gray-900'}">`+
          `<td class="px-2 py-1.5 text-center"><input type="checkbox" ${d?'checked':''} onchange="vnToggle('${r.pc_id}',${Number(r.slot)|0},this.checked)" class="w-5 h-5 cursor-pointer accent-green-500 align-middle"></td>`+
          VIETNAM_COLS.map(c=>{
            const cls = (c.red && c.red(r)) ? 'text-red-400 font-bold' : 'text-gray-200';
            return `<td class="px-3 py-1.5 ${_ta(c.align)} ${cls}">${esc(c.fmt(r))}</td>`;
          }).join('')+
          `</tr>`;
      }).join('')
    : `<tr><td colspan="${VIETNAM_COLS.length+1}" class="text-center text-gray-600 py-8">${T.nodata}</td></tr>`;
}

// ★B-JS10 (2026-09-23) 빨간 줄 판정 한 곳★ — 행 배경(renderRow)과 그룹 헤더 (N) 뱃지가 각자 계산해서
//   성역은 행이 「첫값>=분모」, 뱃지가 「첫값>=2」 로 갈렸다(2/5 → 뱃지만 빨강, 1/1 → 행만 빨강). §A12.
function isRowRed(r){
  const odd = r.odd_energy || '–', daily = r.daily_ticket || '–', sanc = r.sanctuary || '–', ext = r.extract_level || '–';
  const oddFull = (odd !== '–' ? parseInt(odd) : 0) >= 840;
  const dailyFull = (daily !== '–' ? parseInt(daily) : 0) >= 14;
  const sp = sanc !== '–' ? String(sanc).match(/(\d+).*\/(\d+)/) : null;
  const sFirst = sp ? parseInt(sp[1]) : 0, sMax = sp ? parseInt(sp[2]) : 0;
  const sancFull = r.gear_power >= 2700 && sMax > 0 && sFirst >= sMax;
  const extFull = String(ext).includes('입문') && String(ext).includes('50');
  return oddFull || dailyFull || nmTicketFull(r.nightmare_ticket) || r.awakening_ticket >= 3 || sancFull || extFull;
}
function renderCharTable() {
  const filter = (document.getElementById('char-filter')?.value || '').toLowerCase();
  let rows = charTableData;
  if (filter) {
    rows = rows.filter(r => (r.pc_id||'').toLowerCase().includes(filter) || (r.name||'').toLowerCase().includes(filter));
  }
  const {key, asc} = charTableSort;
  rows.sort((a, b) => {
    let va = a[key] ?? '', vb = b[key] ?? '';
    if (typeof va === 'number' && typeof vb === 'number') return asc ? va - vb : vb - va;
    va = String(va).toLowerCase(); vb = String(vb).toLowerCase();
    return asc ? va.localeCompare(vb) : vb.localeCompare(va);
  });
  const tbody = document.getElementById('char-tbody');

  function renderRow(r, i) {
    const pcFilters = (state[r.pc_id] || {}).slot_filters || {};
    const slotEnabled = pcFilters[String(r.slot)] !== false;
    const gp = r.gear_power ? Number(r.gear_power).toLocaleString() : '–';
    const pp = r.power_power ? Number(r.power_power).toLocaleString() : '–';
    // 저전투력 경고(사용자 기준): 장비 <3,000 / 파워 <200,000 → 빨간 글씨 (0/누락은 '–'라 제외)
    const gpNum = parseInt(r.gear_power) || 0, ppNum = parseInt(r.power_power) || 0;
    const gpLow = gpNum > 0 && gpNum < 3000;
    const ppLow = ppNum > 0 && ppNum < 200000;
    const classColors = {'궁성':'text-green-400','검성':'text-orange-400','치유성':'text-pink-400','호법성':'text-purple-400','정령성':'text-blue-400','살성':'text-red-400','마도성':'text-cyan-400'};
    const cls = r.char_class || '–';
    const clsColor = classColors[cls] || 'text-gray-400';
    const kina = r.total_kina ? '₭' + Number(r.total_kina).toLocaleString() : '–';
    const odd = r.odd_energy || '–';
    const daily = r.daily_ticket || '–';
    const nmTicket = nmTicketText(r.nightmare_ticket);
    const nmProg = r.nightmare_progress || '';
    // ★nm 만 일부러 HTML 을 품는다★ — 조각을 여기서 감싸고, 쓰는 자리는 그대로 둔다 (2026-09-11)
    const nm = nmProg ? `${esc(nmTicket)} <span class="text-pink-400 text-[10px]">${esc(nmProg)}</span>` : esc(nmTicket);
    const aw = r.awakening_ticket != null ? `${r.awakening_ticket}/3` : '–';
    const sanc = r.sanctuary || '–';
    const mail = r.mail_count != null ? r.mail_count : '–';
    // 물약 열 제거(2026-07-30 사용자 지시) — v1.1.343부터 매크로가 판독하지 않는다(항상 0)
    const scroll = r.return_scroll_count != null ? r.return_scroll_count : '–';
    const scrollLow = typeof r.return_scroll_count === 'number' && r.return_scroll_count <= 50;
    const ext = r.extract_level || '–';
    const arcanaLink = r.arcana_image ? `<a href="#" onclick="showScreenshot('arcana','${r.pc_id}',${Number(r.slot)|0});return false" class="text-purple-400 hover:text-purple-300 underline">보기</a>` : '–';
    const equipLink = r.equip_image ? `<a href="#" onclick="showScreenshot('equip','${r.pc_id}',${Number(r.slot)|0});return false" class="text-blue-400 hover:text-blue-300 underline">보기</a>` : '–';
    const gakin = r.gakin_kina ? Number(r.gakin_kina).toLocaleString() : '–';
    const trade = r.trade_kina ? Number(r.trade_kina).toLocaleString() : '–';
    const rc = (s) => `<span class="text-red-400 font-bold">${s}</span>`;
    const oddFirst = odd !== '–' ? parseInt(odd) : 0;
    const oddFull = oddFirst >= 840;
    const dailyNum = daily !== '–' ? parseInt(daily) : 0;
    const dailyFull = dailyNum >= 14;
    const nmFull = nmTicketFull(r.nightmare_ticket), nmWarn = nmTicketWarn(r.nightmare_ticket);
    const awFull = r.awakening_ticket >= 3;
    const sancParts = sanc !== '–' ? sanc.match(/(\d+).*\/(\d+)/) : null;
    const sancFirst = sancParts ? parseInt(sancParts[1]) : 0;
    const sancMax = sancParts ? parseInt(sancParts[2]) : 0;
    const sancFull = r.gear_power >= 2700 && sancMax > 0 && sancFirst >= sancMax;
    const extFull = ext.includes('입문') && ext.includes('50');
    const hasRed = isRowRed(r);   // ★B-JS10★ 그룹 뱃지와 같은 판정
    const bg = hasRed ? 'bg-red-950/40' : (i % 2 === 0 ? 'bg-gray-900' : 'bg-gray-800/50');
    return `<tr class="${bg} hover:bg-gray-700/50 transition-colors">
      <td class="px-3 py-1.5 text-center">
        <input type="checkbox" ${slotEnabled ? 'checked' : ''}
          onchange="toggleSlotFilter('${r.pc_id}',${Number(r.slot)|0},this.checked)"
          onclick="event.stopPropagation()" class="cursor-pointer accent-green-500"></td>
      <td class="px-3 py-1.5 text-gray-400">${esc(r.slot||'–')}</td>
      <td class="px-3 py-1.5 text-white">${esc(r.name||'–')}</td>
      <td class="px-3 py-1.5 text-xs font-medium ${clsColor}">${esc(cls)}</td>
      <td class="px-3 py-1.5 text-center"><button onclick="collectSlot('${r.pc_id}',${Number(r.slot)|0})" class="px-2 py-0.5 text-xs rounded bg-sky-900/60 hover:bg-sky-700 text-sky-300 whitespace-nowrap" title="이 캐릭터만 정보수집">📡</button></td>
      <td class="px-3 py-1.5 text-right ${gpLow?'':'text-gray-200'}">${gpLow?rc(gp):gp}</td>
      <td class="px-3 py-1.5 text-right font-medium ${ppLow?'':'text-cyan-400'}">${ppLow?rc(pp):pp}</td>
      <td class="px-3 py-1.5 ${oddFull?'':'text-yellow-400'}">${oddFull?rc(esc(odd)):esc(odd)}</td>
      <td class="px-3 py-1.5 text-center">${dailyFull?rc(esc(daily)):esc(daily)}</td>
      <td class="px-3 py-1.5 text-center">${nmFull?rc(nm):(nmWarn?`<span class="text-amber-400 font-bold">${nm}</span>`:nm)}</td>
      <td class="px-3 py-1.5 text-center">${awFull?rc(esc(aw)):esc(aw)}</td>
      <td class="px-3 py-1.5">${sancFull?rc(esc(sanc)):esc(sanc)}</td>
      <td class="px-3 py-1.5 text-center">${esc(mail)}</td>
      <td class="px-3 py-1.5 text-center">${scrollLow?rc(esc(scroll)):esc(scroll)}</td>
      <td class="px-3 py-1.5">${extFull?rc(esc(ext)):esc(ext)}</td>
      <td class="px-3 py-1.5 text-center">${arcanaLink}</td>
      <td class="px-3 py-1.5 text-center">${equipLink}</td>
      <td class="px-3 py-1.5 text-right text-emerald-400">${gakin}</td>
      <td class="px-3 py-1.5 text-right text-orange-400">${trade}</td>
      <td class="px-3 py-1.5 text-right text-yellow-300 font-medium">${kina}</td>
      <td class="px-3 py-1.5 text-center text-fuchsia-300">${esc(displayAbyssTime(r))}</td>
      <td class="px-3 py-1.5 text-right text-fuchsia-200">${r.abyss_point ? Number(r.abyss_point).toLocaleString() : '–'}</td>
      <td class="px-3 py-1.5 text-center ${r.corridor_full ? 'text-green-400 font-medium' : 'text-sky-300'}">${esc(r.corridor_progress || '–')}</td>
    </tr>`;
  }

  // PC별 그룹핑
  const groups = {};
  rows.forEach(r => {
    const pc = r.pc_id || '?';
    if (!groups[pc]) groups[pc] = [];
    groups[pc].push(r);
  });

  let html = '';
  let idx = 0;
  Object.keys(groups).sort().forEach(pc => {
    const pcRows = groups[pc];
    const redCount = pcRows.filter(isRowRed).length;   // ★B-JS10★ 행 배경과 같은 판정
    const redBadge = redCount > 0 ? ` <span class="text-red-400 text-xs">(${redCount})</span>` : '';
    // ★서버는 계정별 우선(v1.1.424, 사용자: "2계정 서버를 못 읽는 것 같네")★ —
    //   info.txt 계정N_서버(지도) > 그 카드의 acct_server > 게임 감지 공통 서버 순.
    const pcServer = groupAcctMaps(baseId(pc)).servers[acctNumOf(pc)]
                  || (state[pc] || {}).acct_server || (state[pc] || {}).server || '';
    const serverTag = pcServer ? ` <span class="text-cyan-400 text-xs font-normal ml-1">[${esc(pcServer)}]</span>` : '';
    const pcKinaRaw = pcRows[0]?.total_kina;
    const kinaTag = pcKinaRaw ? ` <span class="text-yellow-300 text-xs font-normal ml-1">₭${Number(pcKinaRaw).toLocaleString()}</span>` : '';
    html += `<tr class="bg-gray-700/80 cursor-pointer" onclick="togglePcGroup('${pc}')">
      <td colspan="23" class="px-3 py-2 font-bold text-gray-100"><!-- ★colspan=컬럼 수와 동기★ 회랑 열 추가 때 22 그대로라 마지막 열 위가 빈칸(사용자: "회랑 위에 아무것도 없고 짤려있다") -->
        <div class="flex items-center gap-2">
          <span id="pc-arrow-${pc}">▶</span>
          <!-- ★PC 이름도 같이 키운다 (2026-08-22 주인님 지시)★
               "숫자동그라미는 잘나왔는데 이제 PC가 잘안보인다 저것도 글자 키우고 가시성이 좋게해"
               ★뱃지만 키우면 옆 글자가 상대적으로 작아 보인다★ — 한쪽을 키우면 짝도 같이 봐야 한다.
               26px 뱃지에 맞춰 17px/800 + 흰색으로. 접미사(PC-20b)는 계속 감춘다(계정은 뱃지가 말한다). -->
          <span style="font-size:17px;font-weight:800;color:#ffffff;letter-spacing:.01em;">${baseId(pc)}</span>
          ${acctTagSpread(pc)}
          ${serverTag}${kinaTag}${acctIdTag(pc)}
          <span class="text-gray-500 text-xs font-normal">${pcRows.length}캐릭</span>${redBadge}
          <div class="flex items-center gap-1 ml-auto flex-wrap justify-end" onclick="event.stopPropagation()">
            <button onclick="selectAllSlots('${pc}', ${JSON.stringify(pcRows.map(r=>Number(r.slot)|0))}, true)" class="px-1.5 py-0.5 text-xs rounded bg-gray-600/60 hover:bg-gray-500 text-gray-200 whitespace-nowrap">전체선택</button>
            <button onclick="selectAllSlots('${pc}', ${JSON.stringify(pcRows.map(r=>Number(r.slot)|0))}, false)" class="px-1.5 py-0.5 text-xs rounded bg-gray-600/60 hover:bg-gray-500 text-gray-400 whitespace-nowrap">전체해제</button>
            <span class="text-gray-600">|</span>
            <button onclick="sendCmd('${pc}','start')" class="px-1.5 py-0.5 text-xs rounded bg-green-900/60 hover:bg-green-700 text-green-300 whitespace-nowrap">▶ 시작</button>
            <button onclick="sendCmd('${pc}','exit')" class="px-1.5 py-0.5 text-xs rounded bg-red-900/60 hover:bg-red-700 text-red-300 whitespace-nowrap">✕ 종료</button>
            <button onclick="sendUpdaterCmd('${pc}','restart')" class="px-1.5 py-0.5 text-xs rounded bg-yellow-900/60 hover:bg-yellow-700 text-yellow-300 whitespace-nowrap">↺ 재시작</button>
            <button onclick="sendCmd('${pc}','daily_dungeon')" class="px-1.5 py-0.5 text-xs rounded bg-purple-900/60 hover:bg-purple-700 text-purple-300 whitespace-nowrap">일일던전</button>
            <button onclick="sendCmd('${pc}','nightmare')" class="px-1.5 py-0.5 text-xs rounded bg-pink-900/60 hover:bg-pink-700 text-pink-300 whitespace-nowrap">악몽</button>
            <button onclick="sendCmd('${pc}','abyss')" class="px-1.5 py-0.5 text-xs rounded bg-blue-900/60 hover:bg-blue-700 text-blue-300 whitespace-nowrap">어비스</button>
            <button onclick="sendCmd('${pc}','corridor')" class="px-1.5 py-0.5 text-xs rounded bg-indigo-900/60 hover:bg-indigo-700 text-indigo-300 whitespace-nowrap">회랑</button>
            <button onclick="sendCmd('${pc}','surface')" class="px-1.5 py-0.5 text-xs rounded bg-indigo-900/60 hover:bg-indigo-700 text-indigo-300 whitespace-nowrap" title="표층 준비 (사고 574)">표층</button>
            <button onclick="sendCmd('${pc}','netprobe')" class="px-1.5 py-0.5 text-xs rounded bg-slate-800/60 hover:bg-slate-600 text-slate-300 whitespace-nowrap" title="넷프로브 — 이 PC 에서 캡차 업체·관제 서버로 작은/큰(200KB) 요청을 재서 로그 [넷프로브] 4줄 (사냥 안 세움, 사고 579, 1.1.965+)">넷프로브</button>
            <button onclick="sendCmd('${pc}','awakening')" class="px-1.5 py-0.5 text-xs rounded bg-violet-900/60 hover:bg-violet-700 text-violet-300 whitespace-nowrap">각성전</button>
            <button onclick="sendCmd('${pc}','prepare')" class="px-1.5 py-0.5 text-xs rounded bg-amber-900/60 hover:bg-amber-700 text-amber-300 whitespace-nowrap" title="정산→추출→창고→정렬→귀환주문서">준비</button>
            <button onclick="sendCmd('${pc}','collect_info')" class="px-1.5 py-0.5 text-xs rounded bg-sky-900/60 hover:bg-sky-700 text-sky-300 whitespace-nowrap">정보수집</button>
            <button onclick="sellAllCard('${pc}')" class="px-1.5 py-0.5 text-xs rounded bg-yellow-900/60 hover:bg-yellow-700 text-yellow-300 whitespace-nowrap">판매</button>
            <button onclick="openLive('${pc}')" class="px-1.5 py-0.5 text-xs rounded bg-emerald-900/60 hover:bg-emerald-700 text-emerald-300 whitespace-nowrap" title="이 PC의 게임 화면을 실시간으로 봅니다 (열려 있는 동안만 전송)">🖵 화면</button>
            ${(/^http:\/\/[\d.]+:\d+\/(\?k=[\w-]+)?$/.test(((state[pc]||{}).lan_url)||'')) ? `<button onclick="window.open('${(state[pc]||{}).lan_url}','_blank')" class="px-1.5 py-0.5 text-xs rounded bg-teal-900/60 hover:bg-teal-700 text-teal-300 whitespace-nowrap" title="내부망 직결 — 원본 해상도·고프레임, 새 탭으로 열립니다 (같은 내부망에 있어야 열림)">⚡ 내부망</button>` : ''}
          </div>
        </div>
      </td>
    </tr>`;
    html += `<tr data-pc="${pc}" class="bg-gray-800/60 text-xs text-gray-500 uppercase" style="display:none">
      <th class="px-3 py-1 text-center w-8">✓</th>
      <th class="px-3 py-1">#</th>
      <th class="px-3 py-1">이름</th>
      <th class="px-3 py-1">직업</th>
      <th class="px-3 py-1 text-center">수집</th>
      <th class="px-3 py-1 text-right">장비전투력</th>
      <th class="px-3 py-1 text-right">파워전투력</th>
      <th class="px-3 py-1">오드에너지</th>
      <th class="px-3 py-1 text-center">일일던전</th>
      <th class="px-3 py-1 text-center">악몽</th>
      <th class="px-3 py-1 text-center">각성</th>
      <th class="px-3 py-1">성역</th>
      <th class="px-3 py-1 text-center">우편</th>
      <th class="px-3 py-1 text-center">귀환</th>
      <th class="px-3 py-1">정기추출</th>
      <th class="px-3 py-1 text-center">아르카나</th>
      <th class="px-3 py-1 text-center">장비</th>
      <th class="px-3 py-1 text-right">각인키나</th>
      <th class="px-3 py-1 text-right">거래키나</th>
      <th class="px-3 py-1 text-right">창고키나</th>
      <th class="px-3 py-1 text-center">어비스</th>
      <th class="px-3 py-1 text-right">어비스P</th>
      <th class="px-3 py-1 text-center">회랑</th>
    </tr>`;
    pcRows.forEach(r => {
      html += renderRow(r, idx).replace('<tr ', `<tr data-pc="${pc}" style="display:none" `);
      idx++;
    });
  });
  // 현재 열린 그룹 상태 저장 → innerHTML 후 복구
  const openGroups = new Set();
  document.querySelectorAll('[id^="pc-arrow-"]').forEach(a => {
    if (a.textContent === '▼') openGroups.add(a.id.replace('pc-arrow-', ''));
  });
  tbody.innerHTML = html;
  openGroups.forEach(pc => {
    document.querySelectorAll(`tr[data-pc="${pc}"]`).forEach(r => r.style.display = '');
    const arrow = document.getElementById(`pc-arrow-${pc}`);
    if (arrow) arrow.textContent = '▼';
  });
}

function sortCharTable(key) {
  if (charTableSort.key === key) charTableSort.asc = !charTableSort.asc;
  else { charTableSort.key = key; charTableSort.asc = true; }
  renderCharTable();
}

function filterCharTable() { renderCharTable(); }

function togglePcGroup(pc) {
  const rows = document.querySelectorAll(`tr[data-pc="${pc}"]`);
  const arrow = document.getElementById(`pc-arrow-${pc}`);
  const visible = rows[0]?.style.display !== 'none';
  rows.forEach(r => r.style.display = visible ? 'none' : '');
  if (arrow) arrow.textContent = visible ? '▶' : '▼';
}

function toggleAllPcGroups(open) {
  const arrows = document.querySelectorAll('[id^="pc-arrow-"]');
  arrows.forEach(a => {
    const pc = a.id.replace('pc-arrow-', '');
    const rows = document.querySelectorAll(`tr[data-pc="${pc}"]`);
    rows.forEach(r => r.style.display = open ? '' : 'none');
    a.textContent = open ? '▼' : '▶';
  });
}

function printCharTable() {
  const table = document.getElementById('char-table-wrap');
  if (!table) return;
  const win = window.open('', '_blank');
  win.document.write(`<html><head><title>캐릭터 현황</title>
<style>
  body { font-family: sans-serif; font-size: 11px; margin: 10px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #ccc; padding: 4px 6px; text-align: left; white-space: nowrap; }
  th { background: #333; color: #fff; font-size: 10px; }
  tr.red-row { background: #ffe0e0 !important; }
  .text-red-400 { color: #e53e3e; font-weight: bold; }
  @media print { body { margin: 0; } }
</style></head><body>`);
  const clone = table.querySelector('table').cloneNode(true);
  // 빨간 행 표시
  clone.querySelectorAll('tr').forEach(tr => {
    if (tr.innerHTML.includes('text-red-400')) tr.classList.add('red-row');
  });
  // 인쇄에서 링크 제거
  clone.querySelectorAll('a').forEach(a => { a.replaceWith(a.textContent); });
  win.document.write(clone.outerHTML);
  win.document.write('</body></html>');
  win.document.close();
  win.print();
}

function showScreenshot(category, pcId, slot) {
  const url = `/screenshot/${category}/${pcId}/${slot}?t=${Date.now()}`;
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9999;display:flex;align-items:center;justify-content:center;cursor:pointer';
  overlay.onclick = () => overlay.remove();
  const img = document.createElement('img');
  img.src = url;
  img.style.cssText = 'max-width:90vw;max-height:90vh;border:2px solid #555;border-radius:8px';
  img.onerror = () => { overlay.remove(); alert('이미지 없음'); };
  overlay.appendChild(img);
  document.body.appendChild(overlay);
}

function renderInfoContent(info) {
  const el = document.getElementById('info-content');
  if (!info || (!info.chars?.length && !info.total_kina)) {
    el.innerHTML = '<div class="text-gray-600 text-sm text-center py-10">수집된 데이터 없음<br><span class="text-xs text-gray-700">📡 정보수집 버튼을 눌러주세요</span></div>';
    document.getElementById('info-collected-at').textContent = '수집 시각: –';
    return;
  }
  const kinaHtml = info.total_kina
    ? `<div class="bg-gray-800 rounded-xl p-4 border border-gray-700">
        <div class="text-xs text-gray-500 mb-1">창고키나</div>
        <div class="text-xl font-bold text-yellow-300">₭${Number(info.total_kina).toLocaleString('en-US')}</div>
       </div>` : '';
  const LABELS = {
    gear_power:       '장비전투력',
    power_power:      '파워전투력',
    odd_energy:       '오드 에너지',
    nightmare_ticket: '악몽 도전횟수',
    awakening_ticket: '각성전 도전횟수',
    daily_ticket:     '일일던전 티켓',
    sanctuary:        '성역',
    mail_count:       '우편',
    extract_level:    '정기추출',
    gakin_kina:       '각인키나',
    trade_kina:       '거래키나',
  };
  const RAW_FIELDS   = new Set(['odd_energy', 'sanctuary', 'extract_level']);
  const POWER_FIELDS = new Set(['power_power']);
  const charsHtml = (info.chars||[]).map((c,i) => {
    const rows = Object.entries(LABELS).map(([k,lbl]) => {
      const v = c[k];
      if (v == null || v === '') return '';
      const display = RAW_FIELDS.has(k) ? v : POWER_FIELDS.has(k) ? fmtPower(v) : fmtNum(v);
      return `<div class="flex justify-between text-xs py-0.5 border-b border-gray-800/60">
        <span class="text-gray-500">${lbl}</span>
        <span class="text-gray-200 font-medium">${esc(display)}</span>
      </div>`;
    }).join('');
    return `<div class="bg-gray-800 rounded-xl border border-gray-700 overflow-hidden">
      <div class="px-4 py-2.5 bg-gray-750 border-b border-gray-700 flex items-center gap-2">
        <span class="text-xs font-bold text-indigo-400">${i+1}.</span>
        <span class="text-sm font-bold text-gray-100">${esc(c.name||c.char_name||`캐릭${i+1}`)}</span>
        ${c.class?`<span class="text-xs text-gray-500 ml-auto">${esc(c.class)}</span>`:''}
      </div>
      <div class="px-4 py-2">${rows||'<div class="text-xs text-gray-600 py-2">데이터 없음</div>'}</div>
    </div>`;
  }).join('');
  el.innerHTML = kinaHtml + charsHtml;
  document.getElementById('info-collected-at').textContent = `수집 시각: ${fmtAt(info.collected_at)}`;
  document.getElementById('info-collected-at').title = '보는 기기의 로컬 시각';   // ★B-JS6★
}

async function openInfoModal(pc_id) {
  infoModalPc = pc_id;
  document.getElementById('info-modal-title').textContent = `세부정보 — ${pc_id}`;
  document.getElementById('info-modal').classList.remove('hidden');
  // 캐시 있으면 즉시 표시
  if (charInfoCache[pc_id]) {
    renderInfoContent(charInfoCache[pc_id]);
  } else {
    document.getElementById('info-content').innerHTML = '<div class="text-gray-600 text-sm text-center py-10">로딩 중...</div>';
  }
  // 서버에서 최신 데이터 가져오기
  const res = await fetch(`/char_info/${pc_id}`);
  if (res.ok) {
    const data = await res.json();
    charInfoCache[pc_id] = data;
    if (infoModalPc === pc_id) renderInfoContent(data);
  }
}

function closeInfoModal() {
  infoModalPc = null;
  document.getElementById('info-modal').classList.add('hidden');
}

function openInfoFromMenu() { const id=menuPcId; closeCardMenu(); openInfoModal(id); }

async function collectInfoFromMenu() {
  const id = menuPcId;
  closeCardMenu();
  if (!id) return;
  await sendCmd(id, 'collect_info', {});
  showToast(`📡 ${id} 정보수집 시작`);
  loadCmdHistory();
}
// ─── 멀티계정(v1.1.412): 계정 전환 '선언' ───────────────────────────────────
// 게임 계정 전환은 사람이 수동으로 한다. 전환한 뒤 '지금 온라인인 카드'에서 이 버튼을
// 누르면 매크로가 account.txt를 바꾸고 재시작 → PC-03b 같은 계정 카드로 다시 접속한다.
// (기존 카드는 오프라인으로 남는 게 정상 — 어느 계정이 켜져 있는지 그대로 보인다)
async function setAccountFromMenu() {
  const id = menuPcId;
  closeCardMenu();
  if (!id) return;
  const v = normAcct(prompt(`${id} — 지금 게임에 로그인된 계정 번호를 입력하세요\n` +
                    `1 = 본계정 / 2, 3, 4 = 부계정\n` +
                    `(매크로가 재시작하며 해당 계정 카드로 갈아탑니다)`));
  if (!v) return;
  if (!confirm(`${id} → 계정 ${acctNum(v)} 선언 (매크로 재시작됨)`)) return;
  const ok = await sendCmd(id, 'set_account', {label: v});
  showToast(ok ? `👥 ${id} 계정 ${acctNum(v)} 선언 — 재시작 후 새 카드로 접속` : '✗ 전송 실패');
  loadCmdHistory();
}

// ─── 멀티계정(v1.1.412): 계정 '자동 전환' ───────────────────────────────────
// 선언(setAccount)과 달리 매크로가 크롬에서 로그아웃→해당 계정 로그인→AION2까지 자동으로
// 한다(info.txt 계정N_아이디/비번 필요). ★크롬 CDP 기반이 깔린 뒤 실동작★ — 그 전엔
// 매크로가 무해하게 무동작(게임 안 끊김) + 로그로 알린다.
// ─── 오른클릭 메뉴 계정 직행 (2026-08-15 사용자: "계정 1~4 버튼, 있는 것만 활성화") ───
// 존재 판정: 그 계정 카드가 이미 있거나, 매크로 보고 acct_total(자격증명 수, 1..N 연속 가정)
// 범위 안. 현재 접속 계정은 ✓ 표시 + 비활성(자기 자신으로 전환 방지).
function acctAvail(base){
  const avail = new Set();
  let total = 1;
  Object.values(state).forEach(p=>{
    const pid = p.pc_id||'';
    if (baseId(pid) !== base) return;
    total = Math.max(total, p.acct_total||1);
    const c = pid.slice(-1);
    avail.add(acctNoOfSuf(c) || 1);
  });
  for (let n=1; n<=Math.min(MAX_ACCT,total); n++) avail.add(n);
  return avail;
}
// ★★사고 308-b (2026-08-28) — `cmTarget` 은 ★어디에도 없었다★★★
//   `findHostFromMenu()` 가 `const live = cmTarget(); if(!live) return;` 를 부른다.
//   정의가 없으니 ★ReferenceError★ → 「본컴 계정 찾기」 메뉴가 눌러도 아무 일도 안 났다.
//   `toast` 오타(사고 308)와 ★같은 부류★ 이고, 둘 다 `node --check` 를 통과했다.
//   → check_js.py 에 ★미정의 이름 검사★ 를 넣었더니 이게 걸려 나왔다.
//   이름 그대로 '지금 열린 카드메뉴의 실제 대상' 을 준다.
function cmTarget(){
  const live = liveCardOf(baseId(menuPcId || ''));
  if (!live) {
    showToast(`⚠️ ${baseId(menuPcId || '')} 에 도는 매크로가 없습니다 — 먼저 켜 주세요`);
    return null;
  }
  return live;
}
function liveCardOf(base){
  const isOn = p => (STATUS_CFG[p.status||'offline']||STATUS_CFG.offline).online;
  return Object.values(state).find(p => baseId(p.pc_id||'')===base && isOn(p)) || null;
}
function currentAcctNum(base){
  const live = liveCardOf(base);
  if (!live) return 0;
  const c = (live.pc_id||'').slice(-1);
  return live.acct_num || (acctNoOfSuf(c) || 1);
}
function refreshAcctButtons(pc_id){
  const base = baseId(pc_id);
  const avail = acctAvail(base);
  const cur = currentAcctNum(base);
  // ★버튼을 MAX_ACCT 에서 만든다 — 계정을 늘려도 손으로 HTML 을 안 고치게★
  const box = document.getElementById('cm-acct-box');
  if (box && box.childElementCount !== MAX_ACCT) {
    box.innerHTML = '';
    for (let k=1; k<=MAX_ACCT; k++){
      const nb = document.createElement('button');
      // 계정 수가 홀수면 마지막 버튼을 2칸으로 — 안 그러면 왼쪽에 혼자 서고 오른쪽이 빈다
      nb.className = 'cm-btn chip-purple'
                   + ((MAX_ACCT % 2 === 1 && k === MAX_ACCT) ? ' cm-span2' : '');
      nb.id = 'cm-acct-' + k;
      nb.textContent = '계정 ' + k;
      nb.title = '★본컴 런처와 원격컴 크롬을 함께★ 계정 ' + k + ' 로 바꿉니다 (파섹 경유 → '
               + '게임 종료 → 계정 전환 → 게임 실행, 3~4분). ★이미 계정 ' + k + ' 로 보이는 '
               + '카드에도 누를 수 있습니다★ — 본컴 런처를 직접 읽어 어긋난 짝을 맞춥니다';
      nb.onclick = (function(v){ return function(){ switchAccountDirect(v); }; })(k);
      box.appendChild(nb);
    }
  }
  for (let n=1; n<=MAX_ACCT; n++){
    const b = document.getElementById('cm-acct-'+n);
    if (!b) continue;
    const has = avail.has(n);
    // ★★현재 계정 버튼도 ★누를 수 있게★ 둔다 (2026-08-23 주인님 지시)★★
    //   원문: "카드는 계정1로 되어잇는데 직원들이 작업하고 계정이 어딧는지 모른단말이지.
    //          근데 난 이거 계정1을 틀고 싶거든 … 계정1도 전환할수있게 하긴해야돼"
    //   ★막는 자리가 두 곳이었다★ — fullAccountSwitch 의 조기 return 만 풀었더니
    //   버튼이 애초에 disabled 라 거기까지 가지도 못했다(주인님 스샷: '✓ 계정 1' 회색).
    //   ★없는 계정만★ 막는다(info.txt 에 아이디/비번이 없는 칸). 현재 계정은 '강제 재정렬'
    //   손잡이로 살려둔다 — 카드의 계정은 매크로의 자칭값이라 본컴과 어긋날 수 있다.
    b.disabled = !has;
    b.style.opacity = b.disabled ? '0.35' : (n===cur ? '0.8' : '');
    b.style.cursor = b.disabled ? 'not-allowed' : '';
    b.textContent = (n===cur ? '✓ 계정 ' : '계정 ') + n;
    b.title = !has ? 'info.txt에 이 계정의 아이디/비번이 없습니다'
            : (n===cur ? `카드상 현재 계정입니다. 눌러도 됩니다 — ★강제 재정렬★ (본컴 런처를 직접 읽어 계정 ${n} 이 아니면 바꾸고, 맞으면 게임만 다시 켭니다. 매크로 재시작 없음)`
                       : `계정 ${n}로 통짜 전환 (본컴 런처 → 원격컴 크롬 → 재시작)`);
  }
}
// ★★카드만 강제 변경 (2026-08-27 주인님 지시)★★
//   set_account = lc/loot.py:821 — account.txt 를 쓰고 매크로가 ★자기만★ 재시작한다.
//   크롬도 본컴도 안 건드린다(loot.py 주석: '게임 계정 전환 자체는 사람이 수동으로 한다').
function refreshCardOnlyButtons(pc_id){
  const base = baseId(pc_id);
  const cur  = currentAcctNum(base);
  const box  = document.getElementById('cm-cardonly-box');
  if (!box) return;
  if (box.childElementCount !== MAX_ACCT) {
    box.innerHTML = '';
    for (let k=1; k<=MAX_ACCT; k++){
      const nb = document.createElement('button');
      nb.className = 'cm-btn chip-gray'
                   + ((MAX_ACCT % 2 === 1 && k === MAX_ACCT) ? ' cm-span2' : '');
      nb.id = 'cm-cardonly-' + k;
      nb.textContent = '카드 ' + k;
      nb.onclick = (function(v){ return function(){ setCardOnly(v); }; })(k);
      box.appendChild(nb);
    }
  }
  for (let n=1; n<=MAX_ACCT; n++){
    const b = document.getElementById('cm-cardonly-' + n);
    if (!b) continue;
    // ★없는 계정도 막지 않는다★ — 이건 자격증명을 안 쓴다(account.txt 한 글자).
    //   본컴을 사람이 바꿔놓은 상황이 바로 이 버튼이 필요한 상황이라, info.txt 가
    //   아직 안 채워졌을 수 있다. 막으면 정작 필요할 때 못 쓴다.
    b.style.opacity = (n === cur ? '0.55' : '');
    b.textContent = (n === cur ? '✓ 카드 ' : '카드 ') + n;
    b.title = (n === cur
      ? '카드가 이미 계정 ' + n + ' 입니다'
      : '★카드(매크로 정체성)만★ 계정 ' + n + ' 로 바꿉니다 — account.txt + 매크로 자기 재시작(약 10초). '
        + '본컴 런처와 원격컴 크롬은 건드리지 않습니다. 본컴을 사람이 이미 바꿔둔 경우에 쓰십시오.');
  }
}
async function setCardOnly(n){
  const id = menuPcId;
  const base = baseId(id);
  const cur = currentAcctNum(base);
  const label = ACCT_LABELS[n-1];
  // ★확인을 받는다★ — 본컴과 안 맞는 계정을 고르면 짝이 어긋난 채로 돌아간다.
  //   그 상태는 '매크로는 계정N 이라 보고하는데 실제 게임은 딴 계정' 이라
  //   화면만 봐서는 아무도 모른다. 그래서 누를 때 한 번 묻는다.
  const msg = base + ' 카드를 ★계정 ' + n + '★ 로 바꿉니다.' + String.fromCharCode(10)
            + String.fromCharCode(10)
            + '· 본컴 런처와 원격컴 크롬은 건드리지 않습니다' + String.fromCharCode(10)
            + '· 매크로가 자기만 재시작합니다 (★보통 15~30초★, 판매 중이면 최대 3분)'
            + String.fromCharCode(10)
            + (cur ? ('· 지금 카드: 계정 ' + cur + String.fromCharCode(10)) : '')
            + String.fromCharCode(10)
            + '본컴이 실제로 계정 ' + n + ' 인지 확인하셨습니까?';
  if (!confirm(msg)) return;
  closeCardMenu();
  // ★★사고 308 (2026-08-28) — 이 버튼은 느린 게 아니라 ★대답을 안 했다★★★
  //   주인님: "대시보드에서 내가 임의로 카드 바꾸는게 너무느리다"
  //   ★정의된 것은 `showToast` 뿐이고 `toast` 는 어디에도 없다★ →
  //   명령은 정상으로 나가는데 그 다음 줄이 ★ReferenceError★ 로 죽었다.
  //   그래서 토스트도, 카드 변화도, 에러도 ★아무것도 안 보였다.★
  //   실제로 카드가 바뀌기까지 15~30초 걸리는데 그 동안 화면이 완전히 죽어 있었다.
  //   ★`node --check` 는 이걸 못 잡는다★ — 문법은 멀쩡하기 때문이다(게이트 구멍).
  //   → check_js.py 에 ★미정의 식별자 검사★ 를 같이 넣었다.
  //
  //   ★낙관적 갱신★ — 사람이 누른 즉시 무슨 일이 일어나는지 보여준다.
  //   체감 30초를 0.3초로 줄이는 것은 서버가 아니라 이 한 줄이다.
  showToast(`⏳ ${base} 카드 → 계정 ${n} 전환 중… (보통 15~30초, 판매 중이면 최대 3분)`);
  const ok = await sendCmd(id, 'set_account', {label: label});
  showToast(ok ? (base + ' → 계정 ' + n + ' 명령 전달됨 · 매크로 재시작을 기다립니다')
               : (base + ' 카드 변경 ★실패★ — 명령이 안 나갔습니다'));
}

async function switchAccountDirect(n){
  const id = menuPcId;
  closeCardMenu();
  await fullAccountSwitch(id, n);
}
// ★계정전환의 유일한 경로 (2026-08-18 사용자 지시)★
//   "이제 계정전환하면 본컴도 바뀌게" — 예전엔 진입점이 둘로 갈려 있었다:
//     · 카드메뉴 [계정 N]      → switch_launcher (본컴+원격컴) ✅
//     · 카드메뉴 [계정 자동전환] → switch_account  (원격컴만)  ❌
//     · 다중선택 [계정전환]     → switch_account  (원격컴만)  ❌
//   본컴 런처가 계정1인데 원격컴 크롬만 계정2가 되면 ★짝이 안 맞아 스트림이 영영 안 뜬다★.
//   → 세 진입점 전부 여기로 모은다. 명령은 switch_launcher 하나.
async function fullAccountSwitch(id, n){
  if (!id) return false;
  const base = baseId(id);
  // ★계정을 늘려도 따라오게 (2026-08-24 사고 193)★ — 옛 맵은 n=5 에서 undefined 를
  //   내보내 chrome_label 이 비어 나갔다. 라벨은 ACCT_LABELS 가 정본.
  const lab = (n >= 1 && n <= MAX_ACCT) ? ACCT_LABELS[n-1] : '';
  if (!lab) return false;
  // 명령은 '지금 온라인인 카드'로 — 매크로는 현재 정체성의 pc_id로만 수신한다
  const live = liveCardOf(base);
  const target = live ? live.pc_id : id;
  const curAcct = ((target.match(ACCT_SUF_RE)||[])[1]) || 'a';
  const same = (curAcct === lab);
  // ══════════════════════════════════════════════════════════════════════════
  // ★★'이미 그 계정' 이어도 막지 않는다 (2026-08-23 주인님 지시)★★
  //   원문: "카드는 계정1로 되어잇는데 직원들이 작업하고 계정이 어딧는지 모른단말이지.
  //          근데 난 이거 계정1을 틀고 싶거든 이런경우도있으니까,
  //          카드 오른쪽 클릭해서 계정1도 전환할수있게 하긴해야돼"
  //
  //   ★왜 옛 가드가 틀렸나★ 카드의 계정은 ★매크로가 자기 info.txt 로 자칭하는 값★ 이다.
  //   본컴 런처를 보고 정한 값이 아니다. 직원이 본컴에서 런처를 갈아놓으면
  //   카드는 옛 계정 그대로고 ★짝이 어긋난 채로 굳는다.★ 그때 필요한 것이 바로
  //   '같은 번호로 한 번 더' = 강제 재정렬인데, 그걸 대시보드가 막고 있었다.
  //
  //   ★풀어도 안전한 이유 — 매크로 코드 실측 (2026-08-23)★
  //     · 본컴 : launcher_ctl._run_switch_locked 가 ★런처 드롭다운★ 으로 진짜 현재 계정을
  //              읽는다(detect_host_acct). 다르면 바꾸고, 같으면 런처를 안 건드리고
  //              [게임 실행]만 확인한다 → "skip · 이미 목표 계정(런처 무변경)"
  //     · 원격컴: loot.py 가 chrome_label == config.ACCOUNT 면 "전환 생략"
  //              → ★매크로 재시작이 아예 안 일어난다★
  //   즉 정말 계정 N 이면 게임만 다시 확실히 켜지고, 아니면 제대로 교정된다.
  //   ※ 상단 다중선택 [🔁 계정전환] 의 '이미 그 계정 제외' 는 ★그대로 둔다★ —
  //     거기서 풀면 한 번에 수십 대의 게임을 재정렬한다. 이건 카드 한 장짜리 손잡이다.
  if (same && !confirm(
      `${base} 카드는 지금 ★계정 ${n}★ 으로 표시돼 있습니다.\n\n` +
      `그래도 계정 ${n} 으로 ★강제 재정렬★ 할까요?\n\n` +
      `· 본컴 런처를 ★직접 읽어서★ 계정 ${n} 이 아니면 바꿉니다\n` +
      `· 맞으면 런처는 안 건드리고 게임만 다시 켭니다\n` +
      `· 원격컴 크롬이 이미 계정 ${n} 이면 매크로 재시작은 하지 않습니다\n\n` +
      `(카드 표시가 실제와 어긋났을 때 쓰는 손잡이입니다)`)) return false;
  const st = ((state[target]||{}).status)||'';
  if (st === 'hunting' && !confirm(`${target} 는 지금 사냥 중입니다.\n★게임을 먼저 끄는 게 맞습니다★ (웹플레이 Quit Game).\n그래도 보낼까요?`)) return false;
  if (!same && !confirm(`${base} → 계정 ${n} 전환\n\n① 본컴 런처 계정 교체 + 게임 실행 (파섹 경유)\n② 원격컴 크롬 로그인 교체\n③ 매크로 재시작\n\n1~2분 걸립니다. 진행할까요?`)) return false;
  // ★한 방에★ — 본컴(런처) 먼저, 성공하면 매크로가 이어서 원격컴 크롬까지 바꾼다.
  //   peer_id·파섹 비번은 서버가 배달 직전에 채운다(enrich_cmd_args).
  //   acct_index=1 : 런처 드롭다운의 '다른 계정' 첫 줄. 계정 2개면 항상 맞다.
  //   ★3개 이상은 줄 간격 미실측★ — 빗나가면 매크로가 '계정 칩 안 바뀜'으로 잡아 실패 처리.
  const ok = await sendCmd(target, 'switch_launcher',
                           {acct_no: n, acct_index: 1, acct_label: `계정${n}`, chrome_label: lab});
  showToast(ok ? (same ? `🔁 ${base} 계정 ${n} ★강제 재정렬★ 시작 (본컴 런처 확인 → 게임 실행)`
                       : `🔁 ${base} → 계정 ${n} 통짜 전환 시작 (본컴→원격컴, 결과는 텔레그램)`)
               : '✗ 전송 실패');
  loadCmdHistory();
  return ok;
}

// ★계정 순회(2026-08-17)★ — 있는 계정 전부를 1→2→3→4 순으로 돌며 작업 1개씩.
//   ★현재 계정도 순서에 포함★한다: 매크로는 '목표 == 현재'면 전환을 건너뛰고 바로 작업한다.
//   peer_id 는 여기서 안 붙인다 — 서버가 배달 직전에 채운다(enrich_cmd_args).
//   진행 상황은 텔레그램으로만 온다(계정마다 매크로가 재시작돼 WS 가 끊기므로 화면 추적 불가).
async function findHostFromMenu(){
  // ★기본은 '찾기만'★ — 정체성 전환은 매크로 재시작을 부르는 별개의 일이라 따로 묻는다.
  //   (사용자 요구 원문: "스트리밍 하기전 상태까지 갖다놓는게 필요하긴할듯")
  const live = cmTarget(); if(!live) return;
  const adopt = confirm(
    "본PC가 어느 계정으로 켜져 있는지 찾습니다 (1~2분, 본PC는 건드리지 않음).\n\n" +
    "[확인] 찾은 계정으로 ★전환까지★ (본컴 런처 + 매크로 재시작 포함, 추가 1~2분)\n" +
    "[취소] 찾아서 ★스트리밍 직전★ 상태로 세워두기만");
  const ok = await sendCmd(live.pc_id, 'find_host', adopt ? {adopt: true} : {});
  if(ok) showToast(adopt ? '본컴 계정 찾기 → 전환까지 진행합니다'
                     : '본컴 계정 찾기 시작 — 결과는 텔레그램/로그로 옵니다');
}

async function acctTourFromMenu(){
  const id = menuPcId;
  closeCardMenu();
  if (!id) return;
  const base = baseId(id);
  let nums = [...acctAvail(base)].sort((a,b)=>a-b);
  if (nums.length < 2) { showToast(`${base} 는 순회할 계정이 1개뿐입니다 (info.txt 확인)`); return; }
  // ★역순 순회 (2026-08-18 사용자 지시: "계정 역순으로 이동해도 가능하게")★
  //   매크로는 원래부터 역순을 지원한다 — acct_tour._norm 이 ★입력 순서를 그대로 보존★한다.
  //   막고 있던 건 여기서 오름차순으로 못박아 보낸 것 하나뿐이었다.
  const dir = prompt(`${base} 순회 방향\n\n1 = 정순 (${nums.join('→')})\n2 = 역순 (${[...nums].reverse().join('→')})`, '1');
  if (dir === null) return;
  if (String(dir).trim() === '2') nums = [...nums].reverse();
  const live = liveCardOf(base);
  if (!live) { showToast(`${base} 매크로가 오프라인입니다 — 순회는 매크로가 받아야 시작됩니다`); return; }
  const st = live.status||'';
  if (st === 'hunting' && !confirm(`${live.pc_id} 는 지금 사냥 중입니다.\n순회는 계정마다 게임을 껐다 켭니다.\n그래도 시작할까요?`)) return;
  if (!confirm(`${base} 계정 순회 — ${nums.join('→')}\n\n각 계정에서 정보수집을 1회씩 합니다.\n계정마다 본컴 런처 전환 + 원격컴 크롬 전환 + 매크로 재시작이 들어갑니다.\n\n★${nums.length*8}~${nums.length*12}분쯤 걸립니다★ (중단은 ■정지)\n결과는 텔레그램으로 옵니다. 시작할까요?`)) return;
  const ok = await sendCmd(live.pc_id, 'acct_tour', {accounts: nums, task: 'collect'});
  showToast(ok ? `🔄 ${base} 계정 순회 시작 (${nums.join('→')}, 결과는 텔레그램)` : '✗ 전송 실패');
  loadCmdHistory();
}

// (옛 switchAccountFromMenu 는 2026-08-18 제거 — 카드메뉴에서 이미 [계정 1~4] 버튼으로
//  대체돼 어디서도 안 불리는 죽은 코드였고, 이름 때문에 '계정전환의 정본' 으로 오해를 샀다.
//  계정전환의 유일한 경로는 위 fullAccountSwitch 다.)

// 크롬 CDP 전환(v1.1.413) — 실측·전환 기반. 게임 1회 끊김을 confirm으로 고지.
async function chromeCdpFromMenu() {
  const id = menuPcId;
  closeCardMenu();
  if (!id) return;
  if (!confirm(`${id} — 크롬을 제어 모드(CDP)로 재기동합니다.\n` +
               `★게임이 1회 끊겼다가 자동 재접속됩니다★\n계속할까요?`)) return;
  // ★★게이트 우회(force) 탈출구 (2026-08-20 적대검증 중3)★★
  //   매크로(v1.1.573+)는 크롬을 죽이기 전에 "다시 로그인할 수단이 있나" 를 확인하고,
  //   없으면 거부한다(2026-08-18 PC-17·19 로그아웃 사고 방지).
  //   그런데 ★구글 계정 PC(07·14·17)는 닭-달걀★ 이다 — CDP 프로필에 쿠키를 넣는 코드가
  //   게이트 뒤에 있어서 첫 1회를 스스로 못 넘는다. 그 3대는 사람이 1회 로그인해야 한다.
  //   여기서 우회 경로를 주지 않으면 API 를 직접 두드리는 수밖에 없다 = 사실상 영구 차단.
  //   ★두 번째 confirm 을 요구한다★ — 실수로 누를 수 없게.
  let force = false;
  if (confirm(`${id} — 로그인 복구수단 확인(게이트)을 ★건너뛸까요?★` +
              `

[취소] = 게이트 켬 (권장). 복구수단이 없으면 크롬을 안 죽입니다.` +
              `
[확인] = 게이트 끔. 로그아웃된 채 멈출 수 있습니다.` +
              `

구글 계정 PC 의 첫 전환일 때만 [확인] 을 누르십시오.`)) force = true;
  const ok = await sendCmd(id, 'chrome_cdp', force ? {force: true} : {});
  showToast(ok ? `🌐 ${id} 크롬 제어모드 전환 시작${force ? ' (게이트 우회)' : ''} (재접속까지 1~3분)` : '✗ 전송 실패');
  loadCmdHistory();
}

function openLogFromInfo() {
  const id = infoModalPc;
  closeInfoModal();
  openLogModal(id);
}

async function collectInfo() {
  if (!infoModalPc) return;
  await sendCmd(infoModalPc, 'collect_info', {});
  showToast(`📡 정보수집 명령 전송 → ${infoModalPc}`);
  loadCmdHistory();
  // 15초 후 자동 새로고침
  setTimeout(async () => {
    if (infoModalPc) {
      const res = await fetch(`/char_info/${infoModalPc}`);
      if (res.ok) { const d=await res.json(); charInfoCache[infoModalPc]=d; if(infoModalPc) renderInfoContent(d); }
    }
  }, 15000);
}

// WebSocket에서 char_info 메시지 수신 시 캐시 갱신 + 모달 갱신
function handleCharInfoMsg(msg) {
  charInfoCache[msg.pc_id] = {
    total_kina: msg.total_kina,
    chars: msg.chars,
    collected_at: msg.collected_at,
  };
  // 카드에 캐릭터 이름 즉시 반영
  if (state[msg.pc_id]) {
    // 인덱스 = slot-1 유지를 위해 filter 없이 빈 문자열로 보존
    state[msg.pc_id].chars = (msg.chars||[]).map(c => c.name||c.char_name||'');
    renderCards();
  }
  if (infoModalPc === msg.pc_id) renderInfoContent(charInfoCache[msg.pc_id]);
  // ★캐릭터 데이터 항상 리로드(2026-07-21 사용자: "각성전 완료돼도 갯수/뱃지 갱신 안 됨") —
  //   전광판 각성전 수치·⚔뱃지가 charTableData 기반인데 기존엔 스프레드 열려있을 때만
  //   리로드해서 새로고침 전까지 안 바뀜. loadCharTable이 내부에서 renderCards까지 해줌.★
  loadCharTable();
  showToast(`✓ ${msg.pc_id} 정보수집 완료`);
}

// ─── 초기화 ──────────────────────────────────────────────────────────────────
(async()=>{
  // ★B-JS9 (2026-09-23)★ 첫 /status 가 던지면(서버 재시작 중·망 끊김) 이 IIFE 가 통째로 죽어
  //   connectWS·setInterval·checkServerBoot 가 한 번도 안 돌았다 = 새로고침 전까지 죽은 화면.
  //   → 실패해도 아래는 전부 돈다. /status 는 5초마다 성공할 때까지 다시 받는다(은퇴 목록은 여기에만 온다).
  const _initStatus = async () => {
    try {
      const res=await fetch('/status', {cache:'no-store'});
      if(!res.ok) return false;
      const j=await res.json();
      // ★WS 전량이 먼저 왔으면 덮지 않는다(2026-09-23 반증 B2-5)★ — 이 /status 는 그보다 낡은 판일 수
      //   있고, 그 사이 지워진 카드를 되살리면 조각(state_diff)은 다시 그 카드를 지우지 않는다.
      if (STATE_VER !== -1) return true;
      j.pcs?.forEach(p=>{state[p.pc_id]=p;});RETIRED=new Set(j.retired||[]);
      return true;
    } catch(e) { console.error('초기 /status 실패 — 5초 뒤 다시', e); return false; }
  };
  if(!(await _initStatus())){
    const _t=setInterval(async()=>{ if(await _initStatus()){ clearInterval(_t); try{renderCards();}catch(e){console.error(e);} } },5000);
  }
  try { renderCards(); } catch(e) { console.error('초기 renderCards 실패', e); }
  loadCmdHistory(); loadCharTable(); connectWS(); loadSalePrice(); loadAwakenPreset();
  loadServerSummary();   // ★전광판 숫자를 팜뷰와 같은 서버 계산으로(2026-09-23)★
  setInterval(loadServerSummary, 20000);   // 정보수집·은퇴 등록은 즉시 안 보여도 20초면 따라잡는다
  setInterval(()=>{ updResultSweep(); renderCards(); },60000);   // ★3분 데드라인의 최소 보장 틱★ — WS state 가 안 와도 1분마다 판정
  // ★★사고 395 — ★꺼져 있는 걸 아무도 모른다★★ (주인님 2026-09-01)
  //   `rot_allow` 가 빈 채로 얼마나 오래 있었는지 아무도 몰랐다. ▶시작은 눌리는데
  //   무장은 매번 조용히 거부됐고(토스트는 몇 초 뒤 사라진다), 주인님은
  //   「정보수집하는것도 없어진거같고」로 ★증상으로만★ 알아채셨다.
  //   → 버튼 라벨에 ★지금 상태★ 를 상시로 박는다. 꺼져 있으면 붉게 보인다.
  rotAllowBadge(); setInterval(rotAllowBadge, 60000);
  setInterval(loadCharTable,120000);   // 각성티켓/뱃지 폴백 갱신(WS char_info 놓쳐도 2분 내 반영)
  checkServerBoot(); setInterval(checkServerBoot,5000);   // 서버 재시작 감지 → 자동 새로고침
  // 탭이 백그라운드면 배경 이펙트(별밭/오로라/혜성) 애니메이션 정지 — GPU 낭비 방지
  // + 복귀 시 즉시 최신화(2026-07-25): 백그라운드 절전으로 밀린 화면/죽은 WS를 그 자리에서 복구
  document.addEventListener('visibilitychange',()=>{
    document.documentElement.classList.toggle('fx-off',document.hidden);
    if(!document.hidden){
      renderCards(); loadCharTable(); loadCmdHistory();
      if(_ws && _ws.readyState===1 && Date.now()-_wsLastMsg>90000){ try{_ws.close();}catch(err){} }
    }
  });
})();
