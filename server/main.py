"""
매크로 관제 서버
FastAPI + SQLite + WebSocket
Railway 배포용

환경변수:
  DASHBOARD_PASSWORD  웹 대시보드 비밀번호
  API_KEY             매크로 클라이언트 인증 키 (기본: macro_key_change_me)
  DB_PATH             SQLite 파일 경로 (기본: /tmp/macro_control.db)
  PORT                uvicorn 포트 (Railway 자동 설정)
"""
import os, json, uuid, re, io, zipfile, time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, StreamingResponse

from database import (
    init_db, upsert_status, get_all_statuses, get_status, delete_status,
    delete_pc_all_data,
    insert_command, get_pending_command, ack_command, cancel_command, get_logs,
    insert_log, get_recent_commands,
    upsert_updater_status, get_all_updater_statuses,
    insert_updater_command, get_pending_updater_command, ack_updater_command,
    upsert_char_info, get_char_info,
)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")
API_KEY            = os.getenv("API_KEY", "macro_key_change_me")
SESSION_TTL        = timedelta(days=7)
BUGS_DIR           = os.getenv("BUGS_DIR", "/data/bugs")
os.makedirs(BUGS_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Session store
# ─────────────────────────────────────────────────────────────────────────────
sessions: dict[str, datetime] = {}   # token → expiry


def new_session() -> str:
    token = str(uuid.uuid4())
    sessions[token] = datetime.now(timezone.utc) + SESSION_TTL
    return token


def valid_session(token: Optional[str]) -> bool:
    if not token or token not in sessions:
        return False
    if datetime.now(timezone.utc) > sessions[token]:
        sessions.pop(token, None)
        return False
    # 사용할 때마다 TTL 갱신 (슬라이딩 세션)
    sessions[token] = datetime.now(timezone.utc) + SESSION_TTL
    return True


def check_session(request: Request) -> bool:
    return valid_session(request.cookies.get("session"))


def check_api_key(request: Request) -> bool:
    return request.headers.get("X-Api-Key") == API_KEY


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket manager
# ─────────────────────────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active = [c for c in self.active if c is not ws]

    async def broadcast(self, data: dict):
        msg = json.dumps(data, ensure_ascii=False)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(lifespan=lifespan, title="Macro Control Panel")


# ─────────────────────────────────────────────────────────────────────────────
# Helper: broadcast current state to all WS clients
# ─────────────────────────────────────────────────────────────────────────────
OFFLINE_TIMEOUT = timedelta(seconds=30)

def _is_stale(updated_at_str: str | None) -> bool:
    """updated_at 타임스탬프가 30초 이상 지났으면 True"""
    if not updated_at_str:
        return True
    try:
        ts = datetime.fromisoformat(updated_at_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ts > OFFLINE_TIMEOUT
    except Exception:
        return True

async def _build_full_state() -> list[dict]:
    """pc_status + updater_status + bug_count + char_info 병합 목록 반환"""
    statuses = await get_all_statuses()
    updater_statuses = await get_all_updater_statuses()

    updater_map: dict[str, dict] = {}
    for u in updater_statuses:
        pid = u.get("pc_id")
        if pid:
            updater_map[pid] = u

    bug_counts: dict[str, int] = {}
    try:
        if os.path.isdir(BUGS_DIR):
            for fname in os.listdir(BUGS_DIR):
                if fname.endswith(".png"):
                    m = re.match(r"^(.+?)_\d{8}_\d{6}_", fname)
                    if m:
                        pid = m.group(1)
                        bug_counts[pid] = bug_counts.get(pid, 0) + 1
    except Exception:
        pass

    seen: set[str] = set()
    for pc in statuses:
        pid = pc.get("pc_id"); seen.add(pid)
        if pid in updater_map:
            u = updater_map[pid]
            pc["_updater_state"]   = u.get("macro_state", "unknown")
            pc["_updater_version"] = u.get("updater_version", "")
            # updater 30초 타임아웃 → offline
            if _is_stale(u.get("_updated_at")):
                pc["status"] = "offline"
        else:
            # updater 기록 자체가 없으면 offline
            pc["status"] = "offline"
        pc["_bug_count"] = bug_counts.get(pid, 0)
        # char_info 이름 항상 로드 (OCR 수집값 우선)
        if pid:
            ci = await get_char_info(pid)
            if ci and ci.get("chars"):
                pc["chars"] = [
                    c.get("name") or c.get("char_name") or ""
                    for c in ci["chars"]
                ]

    for pid, u in updater_map.items():
        if pid not in seen:
            # setup_complete=False (pc_id 미설정 or token 없음)이면 카드 표시 안 함
            if not u.get("setup_complete", True):
                continue
            statuses.append({
                "pc_id":            pid,
                "status":           "offline",
                "_updater_state":   u.get("macro_state", "unknown"),
                "_updater_version": u.get("updater_version", ""),
                "_bug_count":       bug_counts.get(pid, 0),
            })
    return statuses


async def push_state():
    statuses = await _build_full_state()
    await manager.broadcast({"type": "state", "pcs": statuses})


async def push_log(pc_id: str, message: str, level: str = "info"):
    await manager.broadcast({"type": "log", "pc_id": pc_id, "level": level, "message": message})


# ─────────────────────────────────────────────────────────────────────────────
# Auth routes
# ─────────────────────────────────────────────────────────────────────────────
HTML_LOGIN = """<!DOCTYPE html>
<html lang="ko" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Macro Control — Login</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={darkMode:'class'}</script>
</head>
<body class="bg-gray-950 text-gray-100 flex items-center justify-center min-h-screen">
<div class="w-full max-w-sm">
  <div class="bg-gray-900 rounded-2xl shadow-2xl p-8 border border-gray-800">
    <div class="text-center mb-8">
      <div class="text-4xl mb-2">⚔</div>
      <h1 class="text-xl font-bold text-indigo-400 tracking-wide">Macro Control Panel</h1>
      <p class="text-gray-500 text-sm mt-1">매크로 관제 시스템</p>
    </div>
    <div id="err" class="hidden bg-red-900/50 border border-red-700 text-red-300 rounded-lg px-4 py-2 text-sm mb-4"></div>
    <input id="pw" type="password" placeholder="비밀번호"
      class="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-3 text-sm focus:outline-none focus:border-indigo-500 mb-4"
      onkeydown="if(event.key==='Enter')login()">
    <button onclick="login()"
      class="w-full bg-indigo-600 hover:bg-indigo-500 rounded-lg py-3 font-bold text-sm transition-colors">
      로그인
    </button>
  </div>
</div>
<script>
async function login() {
  const pw = document.getElementById('pw').value;
  const r = await fetch('/auth/login', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: pw})
  });
  if (r.ok) { location.href = '/'; }
  else { const e=document.getElementById('err'); e.textContent='비밀번호가 틀렸습니다.'; e.classList.remove('hidden'); }
}
</script>
</body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTML_LOGIN


@app.post("/auth/login")
async def do_login(request: Request, response: Response):
    body = await request.json()
    if body.get("password") != DASHBOARD_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong password")
    token = new_session()
    secure = request.headers.get("x-forwarded-proto", "http") == "https"
    response.set_cookie("session", token, httponly=True, samesite="lax",
                        secure=secure, max_age=604800)
    return {"ok": True}


@app.get("/auth/logout")
async def do_logout(response: Response):
    response.delete_cookie("session")
    return RedirectResponse("/login")


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard HTML
# ─────────────────────────────────────────────────────────────────────────────
HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="ko" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Macro Control Panel</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={darkMode:'class'}</script>
<style>
  @keyframes pulse-badge{0%,100%{opacity:1}50%{opacity:.5}}
  .pulse{animation:pulse-badge 1.5s infinite}
  .log-box{font-family:'Consolas','D2Coding',monospace}
  .scrollbar-thin::-webkit-scrollbar{width:4px}
  .scrollbar-thin::-webkit-scrollbar-track{background:transparent}
  .scrollbar-thin::-webkit-scrollbar-thumb{background:#374151;border-radius:4px}
  .card-sel{outline:2px solid #6366f1!important;outline-offset:1px}
  .card-dragging{opacity:.4;outline:2px dashed #6366f1!important}
  .card-dragover{outline:2px solid #818cf8!important;outline-offset:2px}
  .menu-item{display:block;width:100%;text-align:left;padding:5px 14px;font-size:.75rem;font-weight:600;transition:background .1s}
  .menu-item:hover{background:rgba(255,255,255,.08)}
</style>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen">

<!-- HEADER -->
<header class="bg-gray-900 border-b border-gray-800 px-4 sm:px-6 py-3 flex items-center gap-3 sticky top-0 z-30">
  <span class="text-2xl">⚔</span>
  <h1 class="font-bold text-indigo-400 tracking-wide text-base sm:text-lg">Macro Control Panel</h1>
  <div class="ml-auto flex items-center gap-3">
    <span id="ws-dot" class="w-2.5 h-2.5 rounded-full bg-red-500 transition-colors" title="WebSocket"></span>
    <span id="pc-count" class="text-xs text-gray-500">PC 0대</span>
    <a href="/auth/logout" class="text-xs text-gray-500 hover:text-gray-300 transition-colors">로그아웃</a>
  </div>
</header>

<!-- 전체 명령 바 -->
<div class="bg-gray-900/90 border-b border-gray-800 px-4 py-2 flex flex-wrap gap-2 items-center sticky top-[52px] z-20 backdrop-blur">
  <span class="text-xs text-gray-600 shrink-0 mr-1">전체:</span>
  <button onclick="bulkCmd('start')"   class="px-2.5 py-1 rounded-lg text-xs font-semibold transition-colors bg-green-900/60 hover:bg-green-700 text-green-300">▶ 시작</button>
  <button onclick="bulkCmd('stop')"    class="px-2.5 py-1 rounded-lg text-xs font-semibold transition-colors bg-red-900/60 hover:bg-red-700 text-red-300">■ 정지</button>
  <button onclick="bulkCmd('restart')" class="px-2.5 py-1 rounded-lg text-xs font-semibold transition-colors bg-yellow-900/60 hover:bg-yellow-700 text-yellow-300">↺ 재시작</button>
  <button onclick="bulkCmd('sell')"    class="px-2.5 py-1 rounded-lg text-xs font-semibold transition-colors bg-blue-900/60 hover:bg-blue-700 text-blue-300">$ 판매</button>
  <button onclick="bulkCmd('go_home')" class="px-2.5 py-1 rounded-lg text-xs font-semibold transition-colors bg-gray-700/60 hover:bg-gray-600 text-gray-300">⌂ 귀환</button>
  <div class="h-4 w-px bg-gray-700 mx-1 shrink-0"></div>
  <span class="text-xs text-gray-600 shrink-0 mr-1">업데이터:</span>
  <button onclick="bulkUpdaterCmd('start')"   class="px-2.5 py-1 rounded-lg text-xs font-semibold transition-colors bg-green-900/40 hover:bg-green-700 text-green-400">▶ 시작</button>
  <button onclick="bulkUpdaterCmd('stop')"    class="px-2.5 py-1 rounded-lg text-xs font-semibold transition-colors bg-red-900/40 hover:bg-red-700 text-red-400">■ 정지</button>
  <button onclick="bulkUpdaterCmd('update')"  class="px-2.5 py-1 rounded-lg text-xs font-semibold transition-colors bg-cyan-900/40 hover:bg-cyan-700 text-cyan-400">↑ 업데이트</button>
  <!-- 선택 명령 바 (선택된 PC 있을 때만 표시) -->
  <div id="sel-bar" class="hidden ml-auto flex items-center gap-2 pl-3 border-l border-gray-700">
    <span id="sel-label" class="text-xs text-indigo-400 shrink-0 font-semibold">0개 선택</span>
    <button onclick="selCmd('start')"   class="px-2 py-1 rounded-lg text-xs font-semibold bg-green-800/80 hover:bg-green-700 text-green-200 transition-colors">▶</button>
    <button onclick="selCmd('stop')"    class="px-2 py-1 rounded-lg text-xs font-semibold bg-red-800/80 hover:bg-red-700 text-red-200 transition-colors">■</button>
    <button onclick="selCmd('restart')" class="px-2 py-1 rounded-lg text-xs font-semibold bg-yellow-800/80 hover:bg-yellow-700 text-yellow-200 transition-colors">↺</button>
    <button onclick="selCmd('sell')"    class="px-2 py-1 rounded-lg text-xs font-semibold bg-blue-800/80 hover:bg-blue-700 text-blue-200 transition-colors">$</button>
    <button onclick="clearSelection()" class="px-2 py-1 rounded-lg text-xs font-semibold bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors">✕ 해제</button>
  </div>
</div>

<main class="p-4 sm:p-6 space-y-6">

  <!-- 요약 바 -->
  <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
    <div class="bg-gray-900 rounded-xl p-4 border border-gray-800">
      <div class="text-2xl font-bold text-green-400" id="cnt-hunting">0</div>
      <div class="text-xs text-gray-500 mt-1">사냥 중</div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4 border border-gray-800">
      <div class="text-2xl font-bold text-blue-400" id="cnt-selling">0</div>
      <div class="text-xs text-gray-500 mt-1">판매 중</div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4 border border-gray-800">
      <div class="text-2xl font-bold text-red-400" id="cnt-error">0</div>
      <div class="text-xs text-gray-500 mt-1">에러</div>
    </div>
    <div class="bg-gray-900 rounded-xl p-4 border border-gray-800">
      <div class="text-2xl font-bold text-gray-400" id="cnt-offline">0</div>
      <div class="text-xs text-gray-500 mt-1">오프라인</div>
    </div>
  </div>

  <!-- 온라인 섹션 -->
  <section id="online-section">
    <h2 class="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
      <span class="w-2 h-2 rounded-full bg-green-500 pulse inline-block"></span>
      온라인 <span id="online-count" class="text-gray-600 normal-case">(0)</span>
    </h2>
    <div id="grid-online" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
      <div class="text-gray-600 text-sm col-span-full text-center py-10">대기 중... (매크로 연결 없음)</div>
    </div>
  </section>

  <!-- 오프라인 섹션 -->
  <section id="offline-section" class="hidden">
    <h2 class="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
      <span class="w-2 h-2 rounded-full bg-gray-600 inline-block"></span>
      오프라인 <span id="offline-count" class="text-gray-600 normal-case">(0)</span>
    </h2>
    <div id="grid-offline" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3"></div>
  </section>

  <!-- 최근 명령 내역 -->
  <section class="bg-gray-900 rounded-xl p-5 border border-gray-800">
    <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-widest mb-3">최근 명령 내역</h2>
    <div id="cmd-history" class="space-y-1 text-xs log-box max-h-40 overflow-y-auto scrollbar-thin text-gray-400">
      <div class="text-gray-600">없음</div>
    </div>
  </section>

</main>

<!-- 로그 모달 -->
<div id="log-modal" class="hidden fixed inset-0 bg-black/70 z-50 flex justify-end">
  <div class="bg-gray-900 w-full max-w-lg h-full flex flex-col border-l border-gray-800 shadow-2xl">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-800">
      <h2 class="font-bold text-indigo-400" id="log-modal-title">로그</h2>
      <div class="flex items-center gap-2">
        <button onclick="requestLogs()" class="text-xs px-2 py-1 bg-cyan-700/60 hover:bg-cyan-600/80 text-cyan-200 rounded">📥 로그 요청</button>
        <button onclick="closeLogModal()" class="text-gray-500 hover:text-gray-200 text-xl leading-none">✕</button>
      </div>
    </div>
    <div id="log-entries" class="flex-1 overflow-y-auto p-4 log-box text-xs space-y-0.5 scrollbar-thin"></div>
  </div>
</div>

<!-- 카드 드롭다운 메뉴 -->
<div id="card-menu" class="hidden fixed z-50 bg-gray-800 border border-gray-700 rounded-xl shadow-2xl py-1.5 min-w-[168px]"
     onclick="event.stopPropagation()">
  <div class="px-3 py-1 text-xs text-gray-500 border-b border-gray-700 mb-1" id="menu-pc-label">PC-??</div>
  <button class="menu-item text-green-400"  onclick="cardCmd('start')">▶ 매크로 시작</button>
  <button class="menu-item text-red-400"    onclick="cardCmd('stop')">■ 매크로 정지</button>
  <button class="menu-item text-orange-400" onclick="cardCmd('exit')">✕ 매크로 종료</button>
  <button class="menu-item text-yellow-400" onclick="cardCmd('restart')">↺ 재시작</button>
  <button class="menu-item text-purple-400" onclick="cardCmdSwitch()">⇄ 캐릭 전환...</button>
  <button class="menu-item text-blue-400"   onclick="cardCmd('sell')">$ 판매</button>
  <button class="menu-item text-cyan-400"   onclick="collectInfoFromMenu()">📡 정보수집</button>
  <button class="menu-item text-gray-300"   onclick="cardCmd('go_home')">⌂ 귀환</button>
  <div class="border-t border-gray-700 my-1"></div>
  <button class="menu-item text-indigo-400" onclick="openLogFromMenu()">📋 로그 보기</button>
  <button class="menu-item text-cyan-400"   onclick="openInfoFromMenu()">📊 세부정보</button>
  <button class="menu-item text-yellow-400" onclick="screenshotFromMenu()">📸 스크린샷</button>
  <button class="menu-item text-red-500"    onclick="deletePCFromMenu()">🗑 삭제</button>
  <div class="border-t border-gray-700 my-1"></div>
  <div class="px-3 py-1 text-xs text-gray-500 font-semibold">업데이터 제어</div>
  <button class="menu-item text-green-300"  onclick="updaterCmd('start')">▶ 시작</button>
  <button class="menu-item text-red-300"    onclick="updaterCmd('stop')">■ 정지</button>
  <button class="menu-item text-orange-300" onclick="updaterCmd('exit')">✕ 종료</button>
  <button class="menu-item text-yellow-300" onclick="updaterCmd('restart')">↺ 재시작</button>
  <button class="menu-item text-cyan-300"   onclick="updaterCmd('update')">↑ 업데이트+재시작</button>
  <button class="menu-item text-purple-300" onclick="updaterCmd('update_only')">⬆ 업데이트만</button>
</div>

<!-- 버그 모달 -->
<div id="bug-modal" class="hidden fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4">
  <div class="bg-gray-900 rounded-2xl shadow-2xl border border-gray-800 w-full max-w-2xl max-h-[90vh] flex flex-col">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-800 shrink-0">
      <h2 class="font-bold text-red-400" id="bug-modal-title">버그 스크린샷</h2>
      <div class="flex items-center gap-2">
        <a id="bug-download-link" href="#" class="text-xs text-gray-400 hover:text-gray-200 px-2 py-1 bg-gray-700 rounded transition-colors">⬇ ZIP</a>
        <button onclick="closeBugsModal()" class="text-gray-500 hover:text-gray-200 text-xl leading-none">✕</button>
      </div>
    </div>
    <div id="bug-list" class="flex-1 overflow-y-auto p-4 space-y-3 scrollbar-thin"></div>
  </div>
</div>

<!-- 캐릭터 세부정보 모달 -->
<div id="info-modal" class="hidden fixed inset-0 bg-black/70 z-50 flex justify-end">
  <div class="bg-gray-900 w-full max-w-md h-full flex flex-col border-l border-gray-800 shadow-2xl">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-800 shrink-0">
      <h2 class="font-bold text-cyan-400" id="info-modal-title">세부정보</h2>
      <div class="flex items-center gap-2">
        <button id="info-collect-btn" onclick="collectInfo()" class="text-xs px-3 py-1.5 bg-cyan-800/60 hover:bg-cyan-700 text-cyan-200 rounded-lg font-semibold transition-colors">📡 정보수집</button>
        <button onclick="openLogFromInfo()" class="text-xs px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded-lg font-semibold transition-colors">📋 로그</button>
        <button onclick="closeInfoModal()" class="text-gray-500 hover:text-gray-200 text-xl leading-none ml-1">✕</button>
      </div>
    </div>
    <div id="info-content" class="flex-1 overflow-y-auto p-4 scrollbar-thin space-y-4"></div>
    <div class="px-5 py-3 border-t border-gray-800 shrink-0">
      <span id="info-collected-at" class="text-xs text-gray-600">수집 시각: –</span>
    </div>
  </div>
</div>

<!-- 토스트 -->
<div id="toast" class="hidden fixed bottom-6 left-1/2 -translate-x-1/2 bg-gray-800 border border-gray-700 text-gray-200 text-xs font-semibold px-4 py-2 rounded-full shadow-xl z-50 transition-opacity duration-300"></div>

<script>
// ─── 상태 ────────────────────────────────────────────────────────────────────
let state = {};
let selectedPcs = new Set();
let logModalPc = null;
let menuPcId = null;

const STATUS_CFG = {
  hunting:      {label:'사냥 중',   bg:'bg-green-500/20',  border:'border-green-700',  badge:'bg-green-500',  text:'text-green-400',  online:true},
  selling:      {label:'판매 중',   bg:'bg-blue-500/20',   border:'border-blue-700',   badge:'bg-blue-500',   text:'text-blue-400',   online:true},
  moving:       {label:'이동 중',   bg:'bg-yellow-500/20', border:'border-yellow-700', badge:'bg-yellow-400', text:'text-yellow-400', online:true},
  switching:    {label:'캐릭 전환', bg:'bg-purple-500/20', border:'border-purple-700', badge:'bg-purple-400', text:'text-purple-400', online:true},
  reconnecting: {label:'재연결 중', bg:'bg-orange-500/20', border:'border-orange-700', badge:'bg-orange-400', text:'text-orange-400', online:true},
  captcha:      {label:'캡차',      bg:'bg-pink-500/20',   border:'border-pink-700',   badge:'bg-pink-500',   text:'text-pink-400',   online:true},
  dead:         {label:'사망',      bg:'bg-red-500/20',    border:'border-red-700',    badge:'bg-red-500',    text:'text-red-400',    online:true},
  idle:         {label:'대기',      bg:'bg-gray-700/20',   border:'border-gray-600',   badge:'bg-gray-500',   text:'text-gray-400',   online:true},
  error:        {label:'에러',      bg:'bg-red-500/20',    border:'border-red-700',    badge:'bg-red-500',    text:'text-red-400',    online:true},
  offline:      {label:'오프라인',  bg:'bg-gray-900/40',   border:'border-gray-800',   badge:'bg-gray-700',   text:'text-gray-600',   online:false},
};
const LOG_COLOR = {error:'text-red-400', warn:'text-yellow-400', info:'text-gray-300', debug:'text-gray-600'};

function fmtKina(n) { return (!n&&n!==0)?'–':'₭'+Number(n).toLocaleString('en-US'); }
function fmtRate(n) { return (!n&&n!==0)?'–':'₭'+Number(n).toLocaleString('en-US')+'/hr'; }
function relTime(iso) {
  if (!iso) return '–';
  const d = Math.floor((Date.now()-new Date(iso+'Z').getTime())/1000);
  if (d<5) return '방금'; if (d<60) return d+'초 전';
  if (d<3600) return Math.floor(d/60)+'분 전'; return Math.floor(d/3600)+'시간 전';
}

// ─── 오늘 진행 현황 ──────────────────────────────────────────────────────────
function buildDailyProgress(dp, activeSlot, charNames) {
  if (!dp || !dp.length) return '';
  const completed = dp.filter(c=>c.completed).length;
  const total = dp.length;
  const slots = dp.map(c => {
    const done = c.completed;
    const isActive = !done && c.slot === activeSlot;
    // char_info OCR 이름 우선, 없으면 daily_progress 이름, 없으면 슬롯 번호
    const name = (charNames && charNames[c.slot-1]) || c.name || `${c.slot}`;
    const short = name.length > 3 ? name.slice(0,3) : name;
    const time = (c.completed_time||'').slice(11,16);
    const cls = done
      ? 'bg-green-900/70 border-green-700 text-green-400'
      : isActive
        ? 'bg-yellow-900/70 border-yellow-600 text-yellow-300'
        : 'bg-gray-800/60 border-gray-700 text-gray-600';
    const icon = done ? '✓' : isActive ? '▶' : String(c.slot);
    return `<div class="flex flex-col items-center ${cls} border rounded-md px-1 py-0.5 text-center cursor-default"
      style="min-width:34px" title="${name}${done?' ✓ '+time:isActive?' 진행 중':''}">
      <span class="font-bold text-xs leading-none">${icon}</span>
      <span style="font-size:9px;line-height:1.2;max-width:34px;overflow:hidden;white-space:nowrap">${short}</span>
    </div>`;
  }).join('');
  return `<div class="mt-2 pt-2 border-t border-gray-800/60">
    <div class="flex items-center justify-between mb-1">
      <span class="text-gray-400" style="font-size:10px">오늘 완료</span>
      <span class="${completed===total?'text-green-500':'text-gray-500'}" style="font-size:10px">${completed}/${total}</span>
    </div>
    <div class="flex gap-1">${slots}</div>
  </div>`;
}

// ─── 카드 렌더링 ──────────────────────────────────────────────────────────────
function buildCard(pc) {
  const st = pc.status||'offline';
  const cfg = STATUS_CFG[st]||STATUS_CFG.offline;
  const pulse = (st==='hunting'||st==='selling')?' pulse':'';
  const sel = selectedPcs.has(pc.pc_id)?' card-sel':'';
  const errHtml = (pc.errors||[]).slice(0,3).map(e=>
    `<div class="text-xs text-red-400 bg-red-900/30 rounded px-2 py-0.5">⚠ ${e}</div>`).join('');
  const bugBadge = (pc._bug_count||0)>0
    ? `<span class="ml-1.5 px-1.5 py-0.5 bg-red-700/80 text-red-200 rounded text-xs font-bold leading-none cursor-pointer" onclick="event.stopPropagation();openBugsModal('${pc.pc_id}')">🐛 ${pc._bug_count}</span>`
    : '';
  const ucls = {'running':'text-green-400','stopped':'text-gray-500','updating':'text-cyan-400','crashed':'text-red-400'}[pc._updater_state]||'text-gray-600';
  const updaterRow = (pc._updater_state&&pc._updater_state!=='unknown')
    ? `<div class="mt-1 flex items-center gap-1.5 text-xs text-gray-600"><span>업데이터</span><span class="${ucls}">${pc._updater_state}</span>${pc._updater_version?`<span class="text-gray-700 ml-0.5">v${pc._updater_version}</span>`:''}</div>`
    : '';
  const activeSlot = pc.slot||0;
  const activeDp = (pc.daily_progress||[]).find(c=>c.slot===activeSlot&&!c.completed);
  const activeName = activeDp
    ? ((pc.chars&&pc.chars[activeSlot-1]) || activeDp.name || String(activeSlot))
    : '';
  const isOnline = (STATUS_CFG[st]||STATUS_CFG.offline).online;
  const activeTag = (activeName && isOnline)
    ? `<span class="ml-1 px-1 py-0 bg-yellow-700/60 text-yellow-200 border border-yellow-700/80 rounded text-xs leading-none whitespace-nowrap" style="font-size:10px">${activeSlot} ${activeName}</span>`
    : '';
  return `<div id="card-${pc.pc_id}"
    class="relative bg-gray-900 rounded-xl p-4 border ${cfg.border} ${cfg.bg}${sel} transition-all group cursor-pointer"
    onclick="openCardMenu('${pc.pc_id}',event)">
    <div class="flex items-start justify-between mb-2">
      <div class="flex items-center gap-2 min-w-0">
        <span class="drag-handle shrink-0 cursor-grab active:cursor-grabbing text-gray-700 hover:text-gray-400 select-none" style="font-size:14px;line-height:1" title="드래그로 순서 변경">⠿</span>
        <input type="checkbox" class="rounded accent-indigo-500 shrink-0 cursor-pointer mt-0.5"
          ${selectedPcs.has(pc.pc_id)?'checked':''}
          onclick="event.stopPropagation();toggleSelect('${pc.pc_id}',event)">
        <div class="min-w-0">
          <div class="font-bold text-sm flex items-center gap-0 min-w-0 flex-wrap"><span class="truncate">${pc.pc_id||'?'}</span>${bugBadge}${activeTag}</div>
          ${pc.chars&&pc.chars.length?`<div class="text-xs text-gray-400 truncate">${pc.chars.join(' · ')}</div>`:''}
        </div>
      </div>
      <div class="flex items-center gap-1 shrink-0">
        <span class="inline-flex items-center gap-1.5 text-sm font-bold ${cfg.text}">
          <span class="w-2.5 h-2.5 rounded-full ${cfg.badge}${pulse}"></span>
          ${cfg.label}
        </span>
      </div>
    </div>
    <div class="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs mt-2">
      <div><span class="text-gray-400">진행도</span> <span class="text-white font-medium">${pc.hunt_progress!=null ? pc.hunt_progress.toFixed(1)+' %' : '–'}</span></div>
      <div><span class="text-gray-400">효율</span> <span class="text-white font-medium">${pc.efficiency!=null ? pc.efficiency.toFixed(1)+' % / h' : '–'}</span></div>
      <div><span class="text-gray-400">맵</span> <span class="text-white font-medium">${pc.map_name||'–'}</span></div>
      <div><span class="text-gray-400">업타임</span> <span class="text-white font-medium">${fmtSlotUptime(pc.slot_uptime, pc.slot||0, pc.uptime_hours)}</span></div>
    </div>
    <div class="mt-2 text-xs text-gray-400">최근: ${relTime(pc.last_active)}</div>
    ${errHtml?`<div class="mt-2 space-y-0.5">${errHtml}</div>`:''}
    ${buildDailyProgress(pc.daily_progress, activeSlot, pc.chars)}
    ${updaterRow}
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
function sortByOrder(pcs, key) {
  const order = loadOrder(key);
  const idx = {};
  order.forEach((id,i) => idx[id] = i);
  const known = pcs.filter(p => idx[p.pc_id] !== undefined).sort((a,b) => idx[a.pc_id] - idx[b.pc_id]);
  const fresh = pcs.filter(p => idx[p.pc_id] === undefined).sort((a,b) => (a.pc_id||'').localeCompare(b.pc_id||''));
  return [...known, ...fresh];
}
function saveCurrentOrder(gridId, key) {
  const ids = [...document.getElementById(gridId).children]
    .map(el => el.id?.replace('card-',''))
    .filter(Boolean);
  saveOrder(key, ids);
}

function setupDrag(gridId, orderKey) {
  const grid = document.getElementById(gridId);
  if (!grid) return;
  grid.querySelectorAll('[id^="card-"]').forEach(card => {
    const handle = card.querySelector('.drag-handle');
    if (!handle) return;
    card.setAttribute('draggable','false');
    // 핸들에서만 드래그 시작
    handle.addEventListener('mousedown', e => {
      e.stopPropagation();
      card.setAttribute('draggable','true');
      dragSrcId = card.id.replace('card-','');
      dragSection = orderKey;
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
      const toId = card.id.replace('card-','');
      if (fromId===toId) return;
      const fromEl = document.getElementById('card-'+fromId);
      if (!fromEl) return;
      const rect = card.getBoundingClientRect();
      const after = e.clientY > rect.top + rect.height/2;
      if (after) { card.after(fromEl); } else { card.before(fromEl); }
      saveCurrentOrder(gridId, orderKey);
    });
  });
}

function renderCards() {
  const pcs = Object.values(state).sort((a,b)=>(a.pc_id||'').localeCompare(b.pc_id||''));
  const onlineAll  = pcs.filter(p=>(STATUS_CFG[p.status||'offline']||STATUS_CFG.offline).online);
  const offlineAll = pcs.filter(p=>!(STATUS_CFG[p.status||'offline']||STATUS_CFG.offline).online);
  const online  = sortByOrder(onlineAll,  DRAG_ORDER_KEY_ON);
  const offline = sortByOrder(offlineAll, DRAG_ORDER_KEY_OFF);
  const go  = document.getElementById('grid-online');
  const gof = document.getElementById('grid-offline');
  go.innerHTML  = online.length  ? online.map(buildCard).join('')  : '<div class="text-gray-700 text-sm col-span-full text-center py-10">매크로 연결 없음</div>';
  gof.innerHTML = offline.length ? offline.map(buildCard).join('') : '';
  document.getElementById('online-count').textContent  = `(${online.length})`;
  document.getElementById('offline-count').textContent = `(${offline.length})`;
  document.getElementById('offline-section').classList.toggle('hidden', offline.length===0);
  refreshSummary(pcs);
  document.getElementById('pc-count').textContent = `PC ${pcs.length}대`;
  setupDrag('grid-online',  DRAG_ORDER_KEY_ON);
  setupDrag('grid-offline', DRAG_ORDER_KEY_OFF);
}

function refreshSummary(pcs) {
  const c={hunting:0,selling:0,error:0,offline:0};
  pcs.forEach(p=>{
    const s=p.status||'offline';
    if(s==='hunting'||s==='moving') c.hunting++;
    else if(s==='selling') c.selling++;
    else if(s==='error'||s==='dead'||s==='captcha'||s==='reconnecting') c.error++;
    else if(!(STATUS_CFG[s]||STATUS_CFG.offline).online) c.offline++;
  });
  document.getElementById('cnt-hunting').textContent=c.hunting;
  document.getElementById('cnt-selling').textContent=c.selling;
  document.getElementById('cnt-error').textContent=c.error;
  document.getElementById('cnt-offline').textContent=c.offline;
}

// ─── 선택 ─────────────────────────────────────────────────────────────────────
function toggleSelect(pc_id, e) {
  if (e&&(e.target.tagName==='BUTTON')) return;
  selectedPcs.has(pc_id)?selectedPcs.delete(pc_id):selectedPcs.add(pc_id);
  const card=document.getElementById(`card-${pc_id}`);
  if(card){
    card.classList.toggle('card-sel',selectedPcs.has(pc_id));
    const cb=card.querySelector('input[type=checkbox]');
    if(cb) cb.checked=selectedPcs.has(pc_id);
  }
  updateSelBar();
}

function clearSelection() {
  selectedPcs.clear();
  document.querySelectorAll('.card-sel').forEach(el=>el.classList.remove('card-sel'));
  document.querySelectorAll('#grid-online input[type=checkbox],#grid-offline input[type=checkbox]').forEach(cb=>cb.checked=false);
  updateSelBar();
}

function updateSelBar() {
  const n=selectedPcs.size;
  const bar=document.getElementById('sel-bar');
  bar.classList.toggle('hidden',n===0);
  if(n>0) document.getElementById('sel-label').textContent=`${n}개 선택`;
}

// ─── 명령 전송 ────────────────────────────────────────────────────────────────
async function sendCmd(pc_id, command, args={}) {
  const res=await fetch(`/command/${pc_id}`,{
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({command,args})});
  return res.ok;
}

async function bulkCmd(command, args={}) {
  const ids=Object.keys(state);
  if(!ids.length){showToast('연결된 PC 없음');return;}
  await Promise.all(ids.map(id=>sendCmd(id,command,args)));
  showToast(`✓ ${command} → 전체 ${ids.length}대`);
  loadCmdHistory();
}

async function selCmd(command, args={}) {
  if(!selectedPcs.size) return;
  await Promise.all([...selectedPcs].map(id=>sendCmd(id,command,args)));
  showToast(`✓ ${command} → 선택 ${selectedPcs.size}대`);
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
  document.getElementById('menu-pc-label').textContent=pc_id;
  menu.classList.remove('hidden');
  let top=e.clientY+4, left=e.clientX;
  if(left+174>window.innerWidth) left=window.innerWidth-178;
  if(top+440>window.innerHeight) top=e.clientY-444;
  if(top<4) top=4;
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
  const slot=prompt(`${menuPcId} — 전환할 슬롯 번호 (1~5):`, '1');
  if(slot===null){closeCardMenu();return;}
  const n=parseInt(slot);
  if(isNaN(n)||n<1||n>5){alert('1~5 사이 숫자를 입력하세요');return;}
  cardCmd('switch_char',{slot:n});
}

function openLogFromMenu(){const id=menuPcId; closeCardMenu(); openLogModal(id);}

async function screenshotFromMenu() {
  if(!menuPcId) return;
  const id=menuPcId; closeCardMenu();
  const res = await fetch(`/updater/command/${id}`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({command:'screenshot'})
  });
  if(res.ok) showToast(`📸 ${id} 스크린샷 명령 전송`);
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
function connectWS() {
  const proto=location.protocol==='https:'?'wss':'ws';
  const ws=new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen=()=>{document.getElementById('ws-dot').className='w-2.5 h-2.5 rounded-full bg-green-500 transition-colors';};
  ws.onmessage=(e)=>{
    const msg=JSON.parse(e.data);
    if(msg.type==='state'){state={};(msg.pcs||[]).forEach(p=>{state[p.pc_id]=p;});renderCards();}
    else if(msg.type==='log'&&logModalPc===msg.pc_id){appendLogLine(msg.level,msg.message);}
    else if(msg.type==='cmd_history'){renderCmdHistory(msg.commands||[]);}
    else if(msg.type==='char_info'){handleCharInfoMsg(msg);}
  };
  ws.onclose=()=>{
    document.getElementById('ws-dot').className='w-2.5 h-2.5 rounded-full bg-red-500 transition-colors';
    setTimeout(connectWS,3000);
  };
}

// ─── 명령 내역 ────────────────────────────────────────────────────────────────
async function loadCmdHistory() {
  const res=await fetch('/commands/recent'); if(!res.ok) return;
  renderCmdHistory((await res.json()).commands||[]);
}
function renderCmdHistory(cmds) {
  const el=document.getElementById('cmd-history');
  if(!cmds.length){el.innerHTML='<div class="text-gray-600">없음</div>';return;}
  el.innerHTML=cmds.map(c=>{
    const sc=c.status==='acked'?'text-green-500':(c.status==='pending'?'text-yellow-500':(c.status==='cancelled'?'text-red-400 line-through':'text-gray-500'));
    const cancelBtn = c.status==='pending'
      ? `<button onclick="cancelCmd(${c.id})" class="ml-1 text-gray-600 hover:text-red-400 transition-colors leading-none" title="취소">✕</button>`
      : '';
    return `<div class="flex gap-2 items-center py-0.5">
      <span class="text-gray-600 shrink-0">${(c.created_at||'').slice(11,19)}</span>
      <span class="text-indigo-400 shrink-0">${c.pc_id}</span>
      <span class="text-gray-200">${c.command}</span>
      <span class="${sc} ml-auto shrink-0">${c.status}</span>${cancelBtn}
    </div>`;
  }).join('');
}
async function cancelCmd(cmd_id) {
  const res=await fetch(`/commands/${cmd_id}`,{method:'DELETE'});
  if(res.ok) showToast('✕ 명령 취소됨');
  else showToast('✗ 취소 실패');
}

// ─── 로그 모달 ────────────────────────────────────────────────────────────────
async function openLogModal(pc_id) {
  logModalPc=pc_id;
  document.getElementById('log-modal-title').textContent=`로그 — ${pc_id}`;
  document.getElementById('log-modal').classList.remove('hidden');
  const el=document.getElementById('log-entries');
  el.innerHTML='<div class="text-gray-600">로딩 중...</div>';
  const res=await fetch(`/logs/${pc_id}`);
  if(!res.ok){el.innerHTML='<div class="text-red-400">로드 실패</div>';return;}
  el.innerHTML='';
  (await res.json()).logs?.forEach(l=>appendLogLine(l.level,`${l.created_at.slice(11,19)} ${l.message}`));
  el.scrollTop=el.scrollHeight;
}
function appendLogLine(level, msg) {
  const el=document.getElementById('log-entries');
  const d=document.createElement('div');
  d.className=`${LOG_COLOR[level]||'text-gray-400'} whitespace-pre-wrap break-all leading-5`;
  d.textContent=msg; el.appendChild(d); el.scrollTop=el.scrollHeight;
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
  const res = await fetch(`/updater/command/${pc_id}`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({command, args})
  });
  return res.ok;
}

async function bulkUpdaterCmd(command, args={}) {
  const ids = Object.keys(state);
  if (!ids.length) { showToast('연결된 PC 없음'); return; }
  await Promise.all(ids.map(id => sendUpdaterCmd(id, command, args)));
  showToast(`✓ 업데이터 ${command} → 전체 ${ids.length}대`);
}

async function updaterCmd(command, args={}) {
  if (!menuPcId) return;
  await sendUpdaterCmd(menuPcId, command, args);
  showToast(`✓ 업데이터 ${command} → ${menuPcId}`);
  closeCardMenu();
}

// ─── 버그 모달 ────────────────────────────────────────────────────────────────
let bugModalPc = null;

async function openBugsModal(pc_id) {
  bugModalPc = pc_id;
  document.getElementById('bug-modal-title').textContent = `버그 스크린샷 — ${pc_id}`;
  // href 대신 onclick으로 교체 (다운로드 후 모달 갱신)
  const dlBtn = document.getElementById('bug-download-link');
  dlBtn.onclick = (e) => { e.preventDefault(); downloadAndClearBugs(pc_id); };
  document.getElementById('bug-modal').classList.remove('hidden');
  const el = document.getElementById('bug-list');
  el.innerHTML = '<div class="text-gray-600 text-sm">로딩 중...</div>';
  const res = await fetch(`/bugs/${pc_id}`);
  if (!res.ok) { el.innerHTML = '<div class="text-red-400 text-sm">로드 실패</div>'; return; }
  const data = await res.json();
  const bugs = data.bugs || [];
  if (!bugs.length) { el.innerHTML = '<div class="text-gray-600 text-sm py-6 text-center">버그 없음</div>'; return; }
  el.innerHTML = bugs.map(b => `
    <div class="bg-gray-800 rounded-lg p-3 border border-gray-700">
      <div class="flex items-center justify-between mb-2">
        <span class="text-xs text-gray-400 font-mono truncate mr-2">${b.filename}</span>
        <div class="flex items-center gap-2 shrink-0">
          <span class="text-xs text-gray-600">${(b.size/1024).toFixed(1)}KB</span>
          <button onclick="deleteBug('${b.filename}')" class="text-xs text-red-500 hover:text-red-400 transition-colors">🗑</button>
        </div>
      </div>
      <img src="/bugs/image/${b.filename}" class="w-full rounded border border-gray-700 cursor-pointer hover:opacity-90 transition-opacity" onclick="window.open(this.src,'_blank')" alt="${b.filename}" loading="lazy">
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
  if (totalMin < 60) return totalMin + ' 분';
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return m > 0 ? h + '시간 ' + m + '분' : h + '시간';
}
function fmtAt(iso) {
  if (!iso) return '–';
  return iso.replace('T',' ').slice(0,16);
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
        <div class="text-xs text-gray-500 mb-1">총 키나</div>
        <div class="text-xl font-bold text-yellow-300">₭${Number(info.total_kina).toLocaleString('en-US')}</div>
       </div>` : '';
  const LABELS = {
    gear_power:       '장비전투력',
    power_power:      '파워전투력',
    odd_energy:       '오드 에너지',
    nightmare_ticket: '악몽 도전횟수',
    awakening_ticket: '각성전 도전횟수',
    daily_ticket:     '일일던전 티켓',
  };
  // odd_energy는 "195(+1,985)/840" 형식 문자열 → fmtNum 제외
  const RAW_FIELDS   = new Set(['odd_energy']);
  // power_power만 "2.4 K" 형식 표기 (gear_power는 일반 숫자)
  const POWER_FIELDS = new Set(['power_power']);
  const charsHtml = (info.chars||[]).map((c,i) => {
    const rows = Object.entries(LABELS).map(([k,lbl]) => {
      const v = c[k];
      if (v == null || v === '') return '';
      const display = RAW_FIELDS.has(k) ? v : POWER_FIELDS.has(k) ? fmtPower(v) : fmtNum(v);
      return `<div class="flex justify-between text-xs py-0.5 border-b border-gray-800/60">
        <span class="text-gray-500">${lbl}</span>
        <span class="text-gray-200 font-medium">${display}</span>
      </div>`;
    }).join('');
    return `<div class="bg-gray-800 rounded-xl border border-gray-700 overflow-hidden">
      <div class="px-4 py-2.5 bg-gray-750 border-b border-gray-700 flex items-center gap-2">
        <span class="text-xs font-bold text-indigo-400">${i+1}.</span>
        <span class="text-sm font-bold text-gray-100">${c.name||c.char_name||`캐릭${i+1}`}</span>
        ${c.class?`<span class="text-xs text-gray-500 ml-auto">${c.class}</span>`:''}
      </div>
      <div class="px-4 py-2">${rows||'<div class="text-xs text-gray-600 py-2">데이터 없음</div>'}</div>
    </div>`;
  }).join('');
  el.innerHTML = kinaHtml + charsHtml;
  document.getElementById('info-collected-at').textContent = `수집 시각: ${fmtAt(info.collected_at)}`;
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
  showToast(`✓ ${msg.pc_id} 정보수집 완료`);
}

// ─── 초기화 ──────────────────────────────────────────────────────────────────
(async()=>{
  const res=await fetch('/status');
  if(res.ok)(await res.json()).pcs?.forEach(p=>{state[p.pc_id]=p;});
  renderCards(); loadCmdHistory(); connectWS();
  setInterval(renderCards,60000);
})();
</script>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Web routes (session auth)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not check_session(request):
        return RedirectResponse("/login")
    return HTML_DASHBOARD


@app.get("/status")
async def all_statuses(request: Request):
    if not check_session(request):
        raise HTTPException(status_code=401)
    pcs = await _build_full_state()
    return JSONResponse({"pcs": pcs})


@app.get("/logs/{pc_id}")
async def pc_logs(pc_id: str, request: Request):
    if not check_session(request):
        raise HTTPException(status_code=401)
    logs = await get_logs(pc_id, limit=1000)
    return JSONResponse({"logs": logs})


@app.post("/log/{pc_id}")
async def receive_logs(pc_id: str, request: Request):
    """매크로가 보내는 로그 배치 수신"""
    if not check_api_key(request):
        raise HTTPException(status_code=403)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400)
    logs = data.get("logs", [])
    for entry in logs[:50]:   # 배치당 최대 50개
        level   = str(entry.get("level", "info"))[:10]
        message = str(entry.get("message", ""))[:500]
        if message:
            await insert_log(pc_id, level, message)
    return JSONResponse({"ok": True, "count": len(logs)})


@app.get("/commands/recent")
async def recent_commands(request: Request):
    if not check_session(request):
        raise HTTPException(status_code=401)
    cmds = await get_recent_commands(20)
    return JSONResponse({"commands": cmds})


@app.post("/command/{pc_id}")
async def send_command(pc_id: str, request: Request):
    # 웹 대시보드: session 인증 / 매크로 ack: API key 인증 — 양쪽 모두 허용
    is_web = check_session(request)
    is_mac = check_api_key(request)
    if not is_web and not is_mac:
        raise HTTPException(status_code=401)
    body = await request.json()
    command = body.get("command")
    if not command:
        raise HTTPException(status_code=400, detail="command 필드 필요")
    args = body.get("args", {})
    cmd_id = await insert_command(pc_id, command, args)
    # 브로드캐스트 (명령 내역 갱신용)
    cmds = await get_recent_commands(20)
    await manager.broadcast({"type": "cmd_history", "commands": cmds})
    return JSONResponse({"ok": True, "id": cmd_id})


@app.delete("/status/{pc_id}")
async def remove_pc(pc_id: str, request: Request):
    if not check_session(request):
        raise HTTPException(status_code=401)
    await delete_pc_all_data(pc_id)
    await push_state()
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # session 쿠키로 인증
    session_token = websocket.cookies.get("session")
    if not valid_session(session_token):
        await websocket.close(code=1008)
        return
    await manager.connect(websocket)
    # 초기 상태 전송 (updater 정보 포함)
    pcs = await _build_full_state()
    await websocket.send_text(json.dumps({"type": "state", "pcs": pcs}))
    try:
        while True:
            await websocket.receive_text()   # keep alive; client doesn't send
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ─────────────────────────────────────────────────────────────────────────────
# Macro API routes (API key auth)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/report/{pc_id}")
async def receive_report(pc_id: str, request: Request):
    if not check_api_key(request):
        raise HTTPException(status_code=403)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    data["pc_id"] = pc_id
    await upsert_status(pc_id, data)
    # 중요 이벤트는 로그 테이블에 저장
    errors = data.get("errors") or []
    if errors:
        for e in errors[:3]:
            await insert_log(pc_id, "warn", str(e))
    # WS 브로드캐스트 (updater 정보 포함)
    await push_state()
    return JSONResponse({"ok": True})


@app.get("/command/{pc_id}")
async def poll_command(pc_id: str, request: Request):
    if not check_api_key(request):
        raise HTTPException(status_code=403)
    cmd = await get_pending_command(pc_id)
    if cmd:
        return JSONResponse({"command": cmd["command"], "args": cmd["args"], "id": cmd["id"]})
    return JSONResponse({"command": None})


@app.post("/command/{pc_id}/ack/{cmd_id}")
async def ack_cmd(pc_id: str, cmd_id: int, request: Request):
    if not check_api_key(request):
        raise HTTPException(status_code=403)
    ok = await ack_command(cmd_id)
    # 내역 브로드캐스트
    cmds = await get_recent_commands(20)
    await manager.broadcast({"type": "cmd_history", "commands": cmds})
    return JSONResponse({"ok": ok})


@app.delete("/commands/{cmd_id}")
async def cancel_cmd(cmd_id: int, request: Request):
    """pending 명령 취소 (dashboard용)"""
    if not check_session(request):
        raise HTTPException(status_code=401)
    ok = await cancel_command(cmd_id)
    cmds = await get_recent_commands(20)
    await manager.broadcast({"type": "cmd_history", "commands": cmds})
    return JSONResponse({"ok": ok})


# ─────────────────────────────────────────────────────────────────────────────
# Updater API (API key auth) — 업데이터 데몬이 호출
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/updater/status/{pc_id}")
async def updater_report_status(pc_id: str, request: Request):
    if not check_api_key(request):
        raise HTTPException(status_code=403)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    data["pc_id"] = pc_id
    await upsert_updater_status(pc_id, data)
    await push_state()
    return JSONResponse({"ok": True})


@app.get("/updater/command/{pc_id}")
async def updater_poll_command(pc_id: str, request: Request):
    if not check_api_key(request):
        raise HTTPException(status_code=403)
    cmd = await get_pending_updater_command(pc_id)
    if cmd:
        return JSONResponse({"command": cmd["command"], "args": cmd.get("args", {}), "id": cmd["id"]})
    return JSONResponse({"command": None})


@app.post("/updater/command/{pc_id}")
async def dashboard_send_updater_command(pc_id: str, request: Request):
    if not check_session(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    command = body.get("command")
    if not command:
        raise HTTPException(status_code=400, detail="command 필드 필요")
    cmd_id = await insert_updater_command(pc_id, command, body.get("args", {}))
    return JSONResponse({"ok": True, "id": cmd_id})


@app.post("/updater/command/{pc_id}/ack/{cmd_id}")
async def updater_ack_command(pc_id: str, cmd_id: int, request: Request):
    if not check_api_key(request):
        raise HTTPException(status_code=403)
    ok = await ack_updater_command(cmd_id)
    return JSONResponse({"ok": ok})


# ─────────────────────────────────────────────────────────────────────────────
# Bug API — 스크린샷 업로드/조회/삭제
# ─────────────────────────────────────────────────────────────────────────────

def _list_bug_files(pc_id: Optional[str] = None) -> list[dict]:
    result = []
    if not os.path.isdir(BUGS_DIR):
        return result
    for fname in sorted(os.listdir(BUGS_DIR), reverse=True):
        if not fname.endswith('.png'):
            continue
        if pc_id:
            m = re.match(r'^(.+?)_\d{8}_\d{6}_', fname)
            if not m or m.group(1) != pc_id:
                continue
        path = os.path.join(BUGS_DIR, fname)
        try:
            size = os.path.getsize(path)
        except Exception:
            size = 0
        result.append({"filename": fname, "size": size})
    return result


@app.post("/bugs/{pc_id}")
async def upload_bug(pc_id: str, request: Request, file: UploadFile = File(...)):
    if not check_api_key(request):
        raise HTTPException(status_code=403)
    os.makedirs(BUGS_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    orig = os.path.basename(file.filename or "bug.png")
    # 반드시 {pc_id}_{YYYYMMDD}_{HHMMSS}_{orig} 형태로 저장해야 배지/목록이 작동함
    filename = f"{pc_id}_{ts}_{orig}"
    dest = os.path.join(BUGS_DIR, filename)
    content = await file.read()
    with open(dest, 'wb') as f:
        f.write(content)
    await push_state()
    return JSONResponse({"ok": True, "filename": filename})


@app.get("/bugs/download")
async def download_bugs_zip(request: Request, pc_id: Optional[str] = None):
    if not check_session(request):
        raise HTTPException(status_code=401)
    bugs = _list_bug_files(pc_id)
    if not bugs:
        raise HTTPException(status_code=404, detail="다운로드할 버그 이미지 없음")
    buf = io.BytesIO()
    downloaded_paths = []
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for bug in bugs:
            path = os.path.join(BUGS_DIR, bug["filename"])
            if os.path.exists(path):
                zf.write(path, bug["filename"])
                downloaded_paths.append(path)
    buf.seek(0)
    # ZIP 빌드 완료 후 파일 삭제
    for path in downloaded_paths:
        try:
            os.remove(path)
        except Exception:
            pass
    # 상태 브로드캐스트 (뱃지 갱신)
    await push_state()
    zip_name = f"bugs_{pc_id or 'all'}_{int(time.time())}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={zip_name}"},
    )


@app.get("/bugs")
async def list_all_bugs(request: Request):
    if not check_session(request):
        raise HTTPException(status_code=401)
    return JSONResponse({"bugs": _list_bug_files()})


@app.get("/bugs/{pc_id}")
async def list_pc_bugs(pc_id: str, request: Request):
    if not check_session(request):
        raise HTTPException(status_code=401)
    return JSONResponse({"bugs": _list_bug_files(pc_id)})


@app.get("/bugs/image/{filename:path}")
async def serve_bug_image(filename: str, request: Request):
    if not check_session(request):
        raise HTTPException(status_code=401)
    filename = os.path.basename(filename)
    path = os.path.join(BUGS_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png")


@app.delete("/bugs/image/{filename:path}")
async def delete_bug_image(filename: str, request: Request):
    if not check_session(request):
        raise HTTPException(status_code=401)
    filename = os.path.basename(filename)
    path = os.path.join(BUGS_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    os.remove(path)
    await push_state()
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# Char Info API (macro → server, server → dashboard)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/char_info/{pc_id}")
async def receive_char_info(pc_id: str, request: Request):
    """매크로가 수집한 캐릭터 세부정보 저장"""
    if not check_api_key(request):
        raise HTTPException(status_code=403)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    total_kina = data.get("total_kina", 0)
    chars = data.get("characters", [])
    await upsert_char_info(pc_id, total_kina, chars)
    await manager.broadcast({"type": "char_info", "pc_id": pc_id,
                              "total_kina": total_kina, "chars": chars,
                              "collected_at": data.get("collected_at", "")})
    return JSONResponse({"ok": True})


@app.get("/char_info/{pc_id}")
async def query_char_info(pc_id: str, request: Request):
    """대시보드가 캐릭터 세부정보 조회"""
    if not check_session(request):
        raise HTTPException(status_code=401)
    info = await get_char_info(pc_id)
    if not info:
        return JSONResponse({"pc_id": pc_id, "total_kina": 0, "chars": [], "collected_at": None})
    return JSONResponse(info)


# ─────────────────────────────────────────────────────────────────────────────
# 업데이터 버전 체크 (POST /check)
# ─────────────────────────────────────────────────────────────────────────────
import urllib.parse as _urlparse

# GitHub raw URL 베이스
_GH_RAW = "https://raw.githubusercontent.com/kevincom-honjong/aion2-macro-releases/main"

def _load_version_json() -> dict:
    """version.json 로드 (GitHub Actions가 관리)"""
    vpath = os.path.join(os.path.dirname(__file__), "version.json")
    if not os.path.exists(vpath):
        # Railway 배포 시 /app/version.json
        vpath = "/app/version.json"
    try:
        with open(vpath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

@app.post("/check")
async def updater_check(request: Request):
    """updater.exe가 호출 — exe/이미지/updater 업데이트 필요 여부 응답"""
    body = await request.json()
    client_exe_ver     = body.get("exe_version", "0.0.0")
    client_img_hashes  = body.get("image_hashes", {})
    client_updater_ver = body.get("updater_version", "0.0.0")

    ver = _load_version_json()
    result: dict = {}

    # exe 업데이트 체크
    exe_info = ver.get("exe", {})
    server_exe_ver = exe_info.get("version", "0.0.0")
    if server_exe_ver != client_exe_ver:
        result["exe_update"] = {
            "version":      server_exe_ver,
            "sha256":       exe_info.get("sha256"),
            "download_url": exe_info.get("download_url",
                f"{_GH_RAW}/exe/{_urlparse.quote(exe_info.get('filename', ''))}"),
        }

    # 이미지 업데이트 체크
    server_images = ver.get("images", {})
    images_to_update = []
    for fname, server_hash in server_images.items():
        if fname.startswith("."):
            continue
        client_hash = client_img_hashes.get(fname)
        if client_hash != server_hash:
            images_to_update.append({
                "filename":     fname,
                "sha256":       server_hash,
                "download_url": f"{_GH_RAW}/images2/{_urlparse.quote(fname)}",
            })
    if images_to_update:
        result["images_update"] = images_to_update

    # updater 자가 업데이트 체크
    updater_info = ver.get("updater", {})
    server_updater_ver = updater_info.get("version", "0.0.0")
    if server_updater_ver != client_updater_ver:
        result["updater_update"] = {
            "version":      server_updater_ver,
            "sha256":       updater_info.get("sha256"),
            "download_url": updater_info.get("download_url",
                f"{_GH_RAW}/exe/updater.exe"),
        }

    return JSONResponse(result)


# ─────────────────────────────────────────────────────────────────────────────
# 직접 실행 시 (개발용)
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"Macro Control Panel: http://localhost:{port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
# Tue Apr  7 08:52:44     2026
