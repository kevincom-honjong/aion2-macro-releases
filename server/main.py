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
import os, json, uuid, re, io, zipfile, time, hashlib, hmac, base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, StreamingResponse

from database import (
    init_db, upsert_status, get_all_statuses, get_status, delete_status,
    delete_pc_all_data, get_death_counts_since, get_all_death_events,
    insert_command, get_pending_command, ack_command, cancel_command, get_logs,
    insert_log, get_recent_commands,
    upsert_updater_status, get_all_updater_statuses,
    insert_updater_command, get_pending_updater_command, ack_updater_command,
    upsert_char_info, get_char_info, get_all_char_info,
    upsert_nightmare_progress, get_nightmare_progress, get_all_nightmare_progress,
    upsert_slot_filters, get_slot_filters, get_all_slot_filters,
)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")
API_KEY            = os.getenv("API_KEY", "macro_key_change_me")
SESSION_TTL        = timedelta(days=7)

# 프로세스 시작마다 고유 — 대시보드가 /ping으로 폴링해 값이 바뀌면 "서버 재시작"으로 보고 자동 새로고침.
SERVER_BOOT_ID     = uuid.uuid4().hex
# 세션 서명키.
#   Railway env SESSION_SECRET(랜덤 문자열)을 설정하면 재배포 후에도 쿠키 유효 → 재로그인 불필요.
#   미설정 시 부팅마다 랜덤 → 자동 새로고침은 동작하되 재시작 후 1회 재로그인.
#   ★DASHBOARD_PASSWORD/API_KEY에서 파생하지 않음: 파생하면 토큰 1개 캡처로 비번을 오프라인
#     브루트포스할 수 있고(API_KEY는 클라 PC에 배포돼 노출↑), 이는 랜덤 토큰 대비 보안 회귀.★
_SESSION_SECRET_ENV = os.getenv("SESSION_SECRET", "").strip()
SESSION_SECRET      = (hashlib.sha256(("aion2-session-v1:" + _SESSION_SECRET_ENV).encode()).digest()
                       if _SESSION_SECRET_ENV else os.urandom(32))
BUGS_DIR           = os.getenv("BUGS_DIR", "/data/bugs")
SCREENSHOTS_DIR    = os.getenv("SCREENSHOTS_DIR", "/data/screenshots")
os.makedirs(BUGS_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Session (stateless HMAC 서명 토큰 — 서버 재시작에도 유지됨, 인메모리 저장 X)
#   토큰 형식: base64url(만료ts) "." base64url(HMAC-SHA256(secret, 만료ts))
#   재배포 후에도 SESSION_SECRET이 동일해 쿠키가 계속 유효 → 재로그인 불필요.
# ─────────────────────────────────────────────────────────────────────────────
def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_session() -> str:
    exp = str(int((datetime.now(timezone.utc) + SESSION_TTL).timestamp())).encode()
    sig = hmac.new(SESSION_SECRET, exp, hashlib.sha256).digest()
    return f"{_b64u(exp)}.{_b64u(sig)}"


def valid_session(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    try:
        p_b64, s_b64 = token.split(".", 1)
        payload = _b64u_dec(p_b64)
        sig = _b64u_dec(s_b64)
    except Exception:
        return False
    expected = hmac.new(SESSION_SECRET, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        exp = int(payload.decode())
    except Exception:
        return False
    return datetime.now(timezone.utc).timestamp() <= exp


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

# 매크로 WebSocket 연결 관리
macro_ws_connections: dict[str, WebSocket] = {}   # pc_id → WebSocket


async def send_command_to_macro(pc_id: str, command: str, args: dict, cmd_id: int) -> bool:
    """매크로에 WS로 명령 즉시 전송. 실패 시 False (HTTP fallback 필요)."""
    ws = macro_ws_connections.get(pc_id)
    if ws:
        try:
            await ws.send_text(json.dumps({"type": "command", "id": cmd_id, "command": command, "args": args}))
            return True
        except Exception:
            macro_ws_connections.pop(pc_id, None)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(lifespan=lifespan, title="혼종 사령부 — AION2 관제")


# ─────────────────────────────────────────────────────────────────────────────
# Helper: broadcast current state to all WS clients
# ─────────────────────────────────────────────────────────────────────────────
OFFLINE_TIMEOUT = timedelta(seconds=90)

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
    """pc_status + updater_status + bug_count + char_info + slot_filters 병합 목록 반환"""
    statuses = await get_all_statuses()
    updater_statuses = await get_all_updater_statuses()
    all_filters = await get_all_slot_filters()

    # 최근 30분 사망 횟수 (pc_id별)
    _death_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    death_counts = await get_death_counts_since(_death_cutoff)

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
        pc["deaths_30m"] = death_counts.get(pid, 0)
        pc["slot_filters"] = all_filters.get(pid, {})
        # char_info 이름 항상 로드 (OCR 수집값 우선)
        if pid:
            ci = await get_char_info(pid)
            if ci:
                if ci.get("chars"):
                    pc["chars"] = [
                        c.get("name") or c.get("char_name") or ""
                        for c in ci["chars"]
                    ]
                if ci.get("total_kina"):
                    pc["_total_kina"] = ci["total_kina"]

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
                "deaths_30m":       death_counts.get(pid, 0),
            })
    return statuses


async def push_state():
    statuses = await _build_full_state()
    ver = _load_version_json()
    latest = {
        "macro": ver.get("exe", {}).get("version", ""),
        "updater": ver.get("updater", {}).get("version", ""),
    }
    await manager.broadcast({"type": "state", "pcs": statuses, "latest": latest})


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
<title>⚔ 혼종 사령부 — Login</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={darkMode:'class'}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;800&display=swap" rel="stylesheet">
<style>
  body{background:
    radial-gradient(900px 600px at 20% 0%,rgba(99,102,241,.22),transparent 60%),
    radial-gradient(800px 600px at 80% 100%,rgba(34,211,238,.14),transparent 55%),
    #070b17!important}
  @keyframes aurora{0%,100%{transform:translate3d(-4%,-2%,0) scale(1)}50%{transform:translate3d(4%,3%,0) scale(1.12)}}
  body::after{content:'';position:fixed;top:-20vh;left:50%;width:90vw;height:60vh;margin-left:-45vw;pointer-events:none;z-index:-1;
    background:radial-gradient(closest-side,rgba(99,102,241,.25),transparent 70%);filter:blur(60px);animation:aurora 14s ease-in-out infinite}
  @keyframes shine{to{background-position:200% center}}
  .brand-title{background:linear-gradient(90deg,#c7d2fe,#67e8f9 35%,#f0abfc 70%,#c7d2fe);background-size:200% auto;
    -webkit-background-clip:text;background-clip:text;color:transparent;-webkit-text-fill-color:transparent;animation:shine 7s linear infinite}
  .brand-sub{font-family:'Orbitron',sans-serif;font-size:.62rem;letter-spacing:.34em;color:#818cf8}
  @keyframes floaty{0%,100%{transform:translateY(0)}50%{transform:translateY(-3px)}}
  .brand-emblem{filter:drop-shadow(0 0 12px rgba(129,140,248,.95));animation:floaty 3s ease-in-out infinite;display:inline-block}
  .login-card{background:linear-gradient(165deg,rgba(17,24,39,.88),rgba(10,14,28,.94));backdrop-filter:blur(12px);
    border:1px solid rgba(99,102,241,.3);box-shadow:0 20px 60px -20px rgba(79,70,229,.45)}
  .login-btn{background:linear-gradient(90deg,#4f46e5,#7c3aed 50%,#06b6d4);background-size:200% auto;transition:background-position .3s,transform .15s}
  .login-btn:hover{background-position:right center;transform:translateY(-1px)}
  /* 별밭 + 신스웨이브 지평선 (대시보드와 동일 테마) */
  #bg-fx{position:fixed;inset:0;pointer-events:none;z-index:-1;overflow:hidden}
  #bg-fx .stars{position:absolute;left:0;right:0;top:-100%;height:200%;
    background-image:radial-gradient(1px 1px at 25px 35px,rgba(255,255,255,.9),transparent 45%),
      radial-gradient(1.5px 1.5px at 210px 160px,rgba(165,243,252,.9),transparent 45%),
      radial-gradient(1px 1px at 125px 90px,rgba(240,171,252,.7),transparent 45%),
      radial-gradient(1px 1px at 80px 225px,rgba(255,255,255,.5),transparent 45%);
    background-size:340px 290px;animation:star-drift 160s linear infinite}
  @keyframes star-drift{to{transform:translateY(50%)}}
  #bg-fx .horizon{position:absolute;left:-25%;right:-25%;bottom:-2px;height:32vh;opacity:.5;
    transform:perspective(430px) rotateX(63deg);transform-origin:50% 100%;overflow:hidden;
    -webkit-mask-image:linear-gradient(to top,#000 25%,transparent 96%);mask-image:linear-gradient(to top,#000 25%,transparent 96%)}
  #bg-fx .horizon::before{content:'';position:absolute;left:0;right:0;top:-88px;bottom:-88px;
    background:repeating-linear-gradient(90deg,rgba(129,140,248,.42) 0 1px,transparent 1px 64px),
      repeating-linear-gradient(0deg,rgba(232,121,249,.36) 0 1px,transparent 1px 44px);
    animation:grid-run 3s linear infinite}
  @keyframes grid-run{to{transform:translateY(44px)}}
</style>
</head>
<body class="text-gray-100 flex items-center justify-center min-h-screen">
<div id="bg-fx" aria-hidden="true"><div class="stars"></div><div class="horizon"></div></div>
<div class="w-full max-w-sm">
  <div class="login-card rounded-2xl p-8">
    <div class="text-center mb-8">
      <div class="brand-emblem text-5xl mb-3">⚔</div>
      <h1 class="brand-title text-2xl font-extrabold tracking-wide">혼종 사령부</h1>
      <p class="brand-sub mt-1">HONJONG COMMAND</p>
      <p class="text-gray-500 text-sm mt-3">AION2 매크로 관제 시스템</p>
    </div>
    <div id="err" class="hidden bg-red-900/50 border border-red-700 text-red-300 rounded-lg px-4 py-2 text-sm mb-4"></div>
    <input id="pw" type="password" placeholder="비밀번호"
      class="w-full bg-gray-800/70 border border-gray-700 rounded-lg px-4 py-3 text-sm focus:outline-none focus:border-indigo-500 mb-4"
      onkeydown="if(event.key==='Enter')login()">
    <button onclick="login()"
      class="login-btn w-full rounded-lg py-3 font-bold text-sm text-white">
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


@app.get("/ping")
async def ping():
    """재시작 감지용 — 대시보드가 폴링해서 boot 값이 바뀌면 자동 새로고침. (인증 불필요, 랜덤 id만 노출)"""
    return JSONResponse({"boot": SERVER_BOOT_ID})


@app.get("/debug/deaths")
async def debug_deaths(request: Request):
    """[진단용] death_events 원본 + 현재시각/컷오프/집계. 사망수 안 줄어드는 원인 추적."""
    if not check_session(request):
        raise HTTPException(status_code=401)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    events = await get_all_death_events()
    counts = await get_death_counts_since(cutoff)
    # pc별 이벤트 타임스탬프 나열
    by_pc: dict[str, list] = {}
    for e in events:
        by_pc.setdefault(e["pc_id"], []).append(e["created_at"])
    return JSONResponse({
        "server_now_utc": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "cutoff_30m_utc": cutoff,
        "total_events": len(events),
        "counts_30m": counts,
        "events_by_pc": by_pc,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard HTML
# ─────────────────────────────────────────────────────────────────────────────
HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="ko" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>⚔ 혼종 사령부 — AION2 관제</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={darkMode:'class'}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;800&display=swap" rel="stylesheet">
<style>
  @keyframes pulse-badge{0%,100%{opacity:1}50%{opacity:.5}}
  .pulse{animation:pulse-badge 1.5s infinite;box-shadow:0 0 9px 1px currentColor}
  .log-box{font-family:'Consolas','D2Coding',monospace}
  .scrollbar-thin::-webkit-scrollbar{width:4px}
  .scrollbar-thin::-webkit-scrollbar-track{background:transparent}
  .scrollbar-thin::-webkit-scrollbar-thumb{background:linear-gradient(#6366f1,#22d3ee);border-radius:4px}
  /* ── 카드 선택 표시 v2: 상태 글로우(초록 등)와 확실히 구분 ──
     ① 시안 이중 링 + 배경 틴트 ② 우상단 "✔ 선택됨" 뱃지 ③ ★선택 중엔 미선택 카드 디밍★ */
  .card-sel{outline:2.5px solid #22d3ee!important;outline-offset:2px;
    box-shadow:0 0 0 6px rgba(34,211,238,.16),0 0 28px rgba(34,211,238,.55)!important;
    background-image:linear-gradient(160deg,rgba(34,211,238,.10),transparent 55%)!important}
  .card-sel::before{content:'✔ 선택됨';position:absolute;top:-9px;right:10px;z-index:2;
    padding:1px 8px;border-radius:999px;font-size:10px;font-weight:800;color:#04202a;letter-spacing:.02em;
    background:linear-gradient(90deg,#22d3ee,#67e8f9);box-shadow:0 0 12px rgba(34,211,238,.85)}
  main:has(.card-sel) [id^="card-"]:not(.card-sel){opacity:.45;filter:saturate(.5) brightness(.75)}
  main:has(.card-sel) [id^="card-"]:not(.card-sel):hover{opacity:.95;filter:none}
  .card-dragging{opacity:.4;outline:2px dashed #6366f1!important}
  .card-dragover{outline:2px solid #818cf8!important;outline-offset:2px}
  .menu-item{display:block;width:100%;text-align:left;padding:5px 14px;font-size:.75rem;font-weight:600;transition:background .1s}
  .menu-item:hover{background:rgba(255,255,255,.08)}

  /* ══ FABULOUS 스킨 v2 — 사이버펑크 사령부 ══
     별밭 2겹+혜성+신스웨이브 지평선+오로라. 애니메이션은 전부 transform/opacity(GPU)라
     카드 17장+WS 실시간 갱신에서도 부하 없음. 기능/DOM 무관 — 시각 전용. */
  body{background:
    radial-gradient(1400px 900px at 12% -12%,rgba(99,102,241,.30),transparent 55%),
    radial-gradient(1100px 800px at 88% 5%,rgba(232,121,249,.18),transparent 55%),
    radial-gradient(1000px 1000px at 50% 115%,rgba(34,211,238,.15),transparent 55%),
    #04060f!important;background-attachment:fixed}
  body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:-1;
    background-image:linear-gradient(rgba(129,140,248,.06) 1px,transparent 1px),linear-gradient(90deg,rgba(129,140,248,.06) 1px,transparent 1px);
    background-size:44px 44px;
    -webkit-mask-image:radial-gradient(1100px 650px at 50% 0%,#000 25%,transparent 75%);
    mask-image:radial-gradient(1100px 650px at 50% 0%,#000 25%,transparent 75%)}
  @keyframes aurora{0%,100%{transform:translate3d(-4%,-2%,0) scale(1)}50%{transform:translate3d(4%,3%,0) scale(1.14)}}
  body::after{content:'';position:fixed;top:-28vh;left:50%;width:92vw;height:58vh;margin-left:-46vw;pointer-events:none;z-index:-1;
    background:radial-gradient(closest-side,rgba(99,102,241,.30),rgba(232,121,249,.12) 45%,rgba(34,211,238,.12) 60%,transparent 74%);
    filter:blur(56px);animation:aurora 14s ease-in-out infinite}

  /* ── 배경 FX: 별밭 2겹(드리프트+트윙클) / 혜성 / 신스웨이브 지평선 그리드 ── */
  #bg-fx{position:fixed;inset:0;pointer-events:none;z-index:-1;overflow:hidden}
  #bg-fx .stars{position:absolute;left:0;right:0;top:-100%;height:200%;
    background-image:
      radial-gradient(1px 1px at 25px 35px,rgba(255,255,255,.9),transparent 45%),
      radial-gradient(1px 1px at 125px 90px,rgba(199,210,254,.8),transparent 45%),
      radial-gradient(1.6px 1.6px at 210px 160px,rgba(165,243,252,.9),transparent 45%),
      radial-gradient(1px 1px at 80px 225px,rgba(255,255,255,.55),transparent 45%),
      radial-gradient(1.3px 1.3px at 305px 60px,rgba(240,171,252,.7),transparent 45%),
      radial-gradient(1px 1px at 170px 250px,rgba(255,255,255,.4),transparent 45%);
    background-size:340px 290px;animation:star-drift 160s linear infinite}
  #bg-fx .stars.s2{background-size:560px 470px;opacity:.55;
    animation:star-drift 260s linear infinite reverse,twinkle 6s ease-in-out infinite}
  @keyframes star-drift{to{transform:translateY(50%)}}
  .fx-off #bg-fx *{animation-play-state:paused!important}  /* 탭 백그라운드 시 배경 애니 정지 */
  @keyframes twinkle{0%,100%{opacity:.55}50%{opacity:.2}}
  #bg-fx .comet{position:absolute;top:6%;left:-14%;width:190px;height:2px;border-radius:2px;
    background:linear-gradient(90deg,transparent,rgba(165,243,252,.85) 65%,#fff);
    filter:drop-shadow(0 0 8px rgba(103,232,249,.95));opacity:0;
    animation:comet 12s ease-in 3s infinite}
  @keyframes comet{0%{transform:translate3d(0,0,0) rotate(16deg);opacity:0}
    2%{opacity:1}11%{transform:translate3d(135vw,45vh,0) rotate(16deg);opacity:0}100%{opacity:0}}
  #bg-fx .horizon{position:absolute;left:-25%;right:-25%;bottom:-2px;height:30vh;opacity:.5;
    transform:perspective(430px) rotateX(63deg);transform-origin:50% 100%;overflow:hidden;
    -webkit-mask-image:linear-gradient(to top,#000 25%,transparent 96%);
    mask-image:linear-gradient(to top,#000 25%,transparent 96%)}
  #bg-fx .horizon::before{content:'';position:absolute;left:0;right:0;top:-88px;bottom:-88px;
    background:
      repeating-linear-gradient(90deg,rgba(129,140,248,.42) 0 1px,transparent 1px 64px),
      repeating-linear-gradient(0deg,rgba(232,121,249,.36) 0 1px,transparent 1px 44px);
    animation:grid-run 3s linear infinite}
  @keyframes grid-run{to{transform:translateY(44px)}}
  #bg-fx .hglow{position:absolute;left:6%;right:6%;bottom:20vh;height:2px;border-radius:2px;
    background:linear-gradient(90deg,transparent,rgba(232,121,249,.55) 30%,rgba(34,211,238,.55) 70%,transparent);
    filter:blur(1.5px);opacity:.55}

  /* ── 헤더/명령바: 유리 패널 + 네온 언더라인 스윕 ── */
  /* ★성능(2026-07-21): sticky 요소의 backdrop-filter 제거 — 뒤 배경(별밭/오로라)이 상시
     애니메이션이라 매 프레임 블러 재계산 = 사이트 느려짐의 주범. 불투명도만 올려 동일 룩. */
  .glass-header{background:rgba(6,9,20,.94)!important;
    border-bottom:1px solid rgba(99,102,241,.4);box-shadow:0 2px 28px rgba(79,70,229,.22)}
  .glass-header::after{content:'';position:absolute;left:0;right:0;bottom:-1px;height:2px;pointer-events:none;
    background:linear-gradient(90deg,transparent,#6366f1 25%,#e879f9 50%,#22d3ee 75%,transparent);
    background-size:220% 100%;animation:shine 6s linear infinite;opacity:.9}
  .cmd-bar{background:rgba(8,11,24,.92)!important;border-bottom:1px solid rgba(99,102,241,.2);box-shadow:0 6px 24px -12px rgba(0,0,0,.6)}

  /* ── 명령바 콘솔 그룹 + 네온 칩 ── */
  .cmd-group{position:relative;display:flex;flex-wrap:wrap;align-items:center;gap:6px;
    padding:8px 10px 6px;border:1px solid rgba(99,102,241,.22);border-radius:12px;
    background:linear-gradient(160deg,rgba(20,26,48,.55),rgba(10,14,28,.6))}
  .cmd-group:hover{border-color:rgba(129,140,248,.45)}
  .cmd-legend{position:absolute;top:-8px;left:10px;padding:1px 7px;border-radius:5px;
    font-family:'Orbitron',ui-sans-serif,sans-serif;font-size:8px;font-weight:600;letter-spacing:.24em;
    color:#a5b4fc;background:#0b0f1f;border:1px solid rgba(99,102,241,.35);pointer-events:none}
  .chip{--c:148,163,184;padding:4px 11px;border-radius:9px;font-size:12px;font-weight:700;line-height:1.25;
    color:rgb(var(--c));border:1px solid rgba(var(--c),.45);background:rgba(var(--c),.09);
    box-shadow:inset 0 0 10px -6px rgba(var(--c),.8);white-space:nowrap}
  .chip:hover{background:rgba(var(--c),.24);color:#fff;border-color:rgba(var(--c),.95);
    box-shadow:0 0 16px -3px rgba(var(--c),.65),inset 0 0 10px -6px rgba(var(--c),.8)}
  .chip-indigo{--c:129,140,248} .chip-gray{--c:148,163,184} .chip-green{--c:74,222,128}
  .chip-red{--c:248,113,113}   .chip-cyan{--c:34,211,238}   .chip-purple{--c:192,132,252}
  .chip-pink{--c:244,114,182}  .chip-orange{--c:251,146,60} .chip-blue{--c:96,165,250}
  .chip-sky{--c:56,189,248}    .chip-yellow{--c:250,204,21} .chip-amber{--c:251,191,36}
  .sel-badge{font-size:11px;font-weight:800;color:#c7d2fe;padding:3px 11px;border-radius:999px;white-space:nowrap;
    background:linear-gradient(90deg,rgba(99,102,241,.35),rgba(34,211,238,.22));
    border:1px solid rgba(129,140,248,.55);box-shadow:0 0 12px -3px rgba(99,102,241,.7)}
  /* ── 카드 컨텍스트 메뉴 v2 ── */
  .cm-panel{width:238px;padding:9px;border-radius:14px;
    background:linear-gradient(165deg,rgba(17,23,45,.97),rgba(8,12,26,.98));
    border:1px solid rgba(99,102,241,.4);
    box-shadow:0 18px 50px -12px rgba(0,0,0,.85),0 0 30px -10px rgba(99,102,241,.5);
    backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
  .cm-head{display:flex;align-items:center;gap:7px;padding:2px 3px 8px;margin-bottom:2px;
    border-bottom:1px solid rgba(99,102,241,.28);font-size:13px}
  .cm-sec{font-family:'Orbitron',ui-sans-serif,sans-serif;font-size:8px;font-weight:600;letter-spacing:.24em;
    color:#818cf8;opacity:.95;margin:8px 2px 4px}
  .cm-grid2{display:grid;grid-template-columns:1fr 1fr;gap:5px}
  .cm-grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px}
  .cm-grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin-bottom:5px}
  .cm-span2{grid-column:span 2}
  .cm-btn{--c:148,163,184;padding:5px 6px;border-radius:8px;font-size:11.5px;font-weight:700;text-align:center;
    color:rgb(var(--c));border:1px solid rgba(var(--c),.4);background:rgba(var(--c),.08);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .cm-btn:hover{background:rgba(var(--c),.24);color:#fff;border-color:rgba(var(--c),.95);
    box-shadow:0 0 12px -3px rgba(var(--c),.65)}
  .cm-danger{width:100%;margin-top:9px;padding:5px;border-radius:8px;font-size:11px;font-weight:700;color:#f87171;
    border:1px dashed rgba(248,113,113,.45);background:rgba(248,113,113,.05)}
  .cm-danger:hover{background:rgba(239,68,68,.22);color:#fecaca;border-style:solid;box-shadow:0 0 14px -4px rgba(239,68,68,.6)}

  .price-wrap{position:relative;display:inline-flex;align-items:center}
  .price-wrap .price-k{position:absolute;left:9px;font-size:11px;font-weight:800;color:#fde047;opacity:.85;pointer-events:none}
  #sale-price{width:104px;padding:4px 8px 4px 22px;border-radius:9px;font-size:12px;font-weight:700;color:#fde047;
    font-family:'Orbitron',ui-sans-serif,sans-serif;background:rgba(250,204,21,.07);
    border:1px solid rgba(250,204,21,.4);outline:none;transition:border-color .15s,box-shadow .15s}
  #sale-price:focus{border-color:#facc15;box-shadow:0 0 14px -4px rgba(250,204,21,.75)}
  #sale-price::placeholder{color:rgba(250,204,21,.45);font-family:ui-sans-serif,system-ui,sans-serif;font-weight:500}
  #sale-price::-webkit-outer-spin-button,#sale-price::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}

  /* ── 브랜드 ── */
  @keyframes shine{to{background-position:200% center}}
  .brand-title{background:linear-gradient(90deg,#c7d2fe,#67e8f9 30%,#f0abfc 60%,#fde047 80%,#c7d2fe);background-size:200% auto;
    -webkit-background-clip:text;background-clip:text;color:transparent;-webkit-text-fill-color:transparent;
    animation:shine 6s linear infinite}
  .brand-sub{font-family:'Orbitron',ui-sans-serif,sans-serif;font-size:.6rem;letter-spacing:.3em;color:#818cf8;opacity:.9;transform:translateY(1px)}
  @keyframes floaty{0%,100%{transform:translateY(0) rotate(-4deg)}50%{transform:translateY(-3px) rotate(4deg)}}
  .brand-emblem{display:inline-block;filter:drop-shadow(0 0 10px rgba(129,140,248,1)) drop-shadow(0 0 24px rgba(232,121,249,.7));
    animation:floaty 3s ease-in-out infinite}

  /* ── 전광판 타일: 흐르는 네온 라인 + 그라데이션 발광 숫자 + 호버 리프트 ── */
  .stat-tile{position:relative;background:linear-gradient(160deg,rgba(23,29,52,.88),rgba(9,13,28,.94));
    border:1px solid rgba(99,102,241,.28);border-radius:1rem;padding:1rem;overflow:hidden;
    transition:transform .2s ease,box-shadow .2s ease,border-color .2s ease}
  .stat-tile::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
    background:linear-gradient(90deg,transparent,var(--tile) 40%,#fff 50%,var(--tile) 60%,transparent);
    background-size:220% 100%;animation:shine 4.5s linear infinite;opacity:.95}
  .stat-tile::after{content:'';position:absolute;top:-42%;left:50%;width:130%;height:85%;transform:translateX(-50%);
    background:radial-gradient(closest-side,var(--tile-glow),transparent 70%);opacity:.26;pointer-events:none}
  .stat-tile:hover{transform:translateY(-4px) scale(1.02);border-color:var(--tile);box-shadow:0 14px 40px -10px var(--tile-glow)}
  .tile-green {--tile:#4ade80;--tile-glow:rgba(74,222,128,.55)}
  .tile-blue  {--tile:#60a5fa;--tile-glow:rgba(96,165,250,.55)}
  .tile-yellow{--tile:#facc15;--tile-glow:rgba(250,204,21,.5)}
  .tile-indigo{--tile:#818cf8;--tile-glow:rgba(129,140,248,.6)}
  .tile-gold  {--tile:#fde047;--tile-glow:rgba(253,224,71,.5)}
  .stat-tile .stat-num{font-family:'Orbitron',ui-sans-serif,sans-serif;font-size:1.5rem;font-weight:800;line-height:1.25;
    white-space:nowrap;background:linear-gradient(180deg,#fff 15%,var(--tile) 90%);
    -webkit-background-clip:text;background-clip:text;color:transparent;-webkit-text-fill-color:transparent;
    filter:drop-shadow(0 0 14px var(--tile-glow))}
  .stat-tile .stat-label{font-size:.8rem;color:#a5b4c8;margin-top:.3rem;letter-spacing:.08em}
  .stat-tile .stat-icon{position:absolute;right:.8rem;top:.7rem;font-size:1.2rem;opacity:.5;
    filter:drop-shadow(0 0 6px var(--tile-glow))}

  /* ── PC 카드: 상태색 글로우 + 호버 리프트 + 회전 네온 보더(호버) ── */
  div[id^="card-"]{box-shadow:0 6px 22px -10px rgba(0,0,0,.6);
    transition:transform .18s ease,box-shadow .18s ease,opacity .2s ease,filter .2s ease}
  div[id^="card-"]:hover{transform:translateY(-3px) scale(1.008)}
  /* 상태별 은은한 외곽광 (STATUS_CFG border 클래스 기준 — 기능 신호 강화) */
  div[id^="card-"].border-green-700  {box-shadow:0 0 0 1px rgba(34,197,94,.22), 0 6px 26px -8px rgba(34,197,94,.3)}
  div[id^="card-"].border-blue-700   {box-shadow:0 0 0 1px rgba(59,130,246,.25),0 6px 26px -8px rgba(59,130,246,.32)}
  div[id^="card-"].border-indigo-700 {box-shadow:0 0 0 1px rgba(99,102,241,.28),0 6px 26px -8px rgba(99,102,241,.36)}
  div[id^="card-"].border-fuchsia-700{box-shadow:0 0 0 1px rgba(217,70,239,.25),0 6px 26px -8px rgba(217,70,239,.32)}
  div[id^="card-"].border-purple-700 {box-shadow:0 0 0 1px rgba(168,85,247,.25),0 6px 26px -8px rgba(168,85,247,.3)}
  div[id^="card-"].border-orange-700 {box-shadow:0 0 0 1px rgba(249,115,22,.28),0 6px 26px -8px rgba(249,115,22,.34)}
  div[id^="card-"].border-pink-700   {box-shadow:0 0 0 1px rgba(236,72,153,.28),0 6px 26px -8px rgba(236,72,153,.34)}
  div[id^="card-"].border-red-700    {box-shadow:0 0 0 1px rgba(239,68,68,.4),  0 6px 30px -8px rgba(239,68,68,.45)}
  div[id^="card-"].border-cyan-700   {box-shadow:0 0 0 1px rgba(34,211,238,.25),0 6px 26px -8px rgba(34,211,238,.32)}
  div[id^="card-"].border-amber-700  {box-shadow:0 0 0 1px rgba(245,158,11,.28),0 6px 26px -8px rgba(245,158,11,.34)}
  div[id^="card-"].border-lime-700   {box-shadow:0 0 0 1px rgba(132,204,22,.25),0 6px 26px -8px rgba(132,204,22,.3)}
  /* 호버 시 회전하는 네온 테두리 (호버에만 애니 → 평시 부하 0) */
  @property --spin{syntax:'<angle>';inherits:false;initial-value:0deg}
  div[id^="card-"]::after{content:'';position:absolute;inset:-1px;border-radius:.85rem;padding:1.5px;
    background:conic-gradient(from var(--spin),transparent 0deg 120deg,#818cf8 160deg,#e879f9 190deg,#22d3ee 220deg,transparent 260deg 360deg);
    -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
    -webkit-mask-composite:xor;mask-composite:exclude;
    opacity:0;transition:opacity .25s;pointer-events:none}
  div[id^="card-"]:hover::after{opacity:1;animation:spin-border 2.4s linear infinite}
  @keyframes spin-border{to{--spin:360deg}}

  /* ── 완료 스탬프 (카드 이름 옆, 이모지만 — 색깔 필이 신호) ── */
  .done-badge{padding:1px 5px;border-radius:999px;font-size:11px;line-height:1.4;
    white-space:nowrap;margin-left:4px}
  .done-hunt{color:#bbf7d0;background:rgba(34,197,94,.18);border:1px solid rgba(74,222,128,.55);
    box-shadow:0 0 8px -2px rgba(74,222,128,.6)}
  .done-awaken{color:#c7d2fe;background:rgba(99,102,241,.22);border:1px solid rgba(129,140,248,.6);
    box-shadow:0 0 8px -2px rgba(129,140,248,.65)}

  /* ── 섹션 헤더 네온 라인 / 토스트 ── */
  main section h2{position:relative}
  main section h2::after{content:'';position:absolute;left:0;bottom:-5px;width:170px;height:2px;border-radius:2px;
    background:linear-gradient(90deg,#6366f1,#22d3ee 55%,transparent);opacity:.75}
  #toast{background:rgba(13,18,38,.92)!important;border:1px solid rgba(129,140,248,.55)!important;
    box-shadow:0 0 26px rgba(99,102,241,.4);backdrop-filter:blur(8px)}

  /* ── 버튼: 떠오르는 손맛 ── */
  button{transition:transform .15s ease,box-shadow .15s ease,background-color .15s,color .15s}
  button:hover{transform:translateY(-1px)}
  button:active{transform:translateY(0) scale(.97)}
</style>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen">

<!-- 배경 FX (별밭 2겹 + 혜성 + 신스웨이브 지평선) — 시각 전용, pointer-events 없음 -->
<div id="bg-fx" aria-hidden="true">
  <div class="stars"></div>
  <div class="stars s2"></div>
  <div class="comet"></div>
  <div class="horizon"></div>
  <div class="hglow"></div>
</div>

<!-- HEADER -->
<header class="glass-header px-4 sm:px-6 py-3 flex items-center gap-3 sticky top-0 z-30">
  <span class="brand-emblem text-2xl">⚔</span>
  <h1 class="brand-title font-extrabold tracking-wide text-base sm:text-lg whitespace-nowrap">혼종 사령부</h1>
  <span class="brand-sub hidden md:inline">HONJONG COMMAND</span>
  <button onclick="openVietnamModal()" class="px-3 py-1 rounded-lg text-sm font-semibold bg-red-700/70 hover:bg-red-600 text-white transition-colors whitespace-nowrap">Việt Nam</button>
  <div class="ml-auto flex items-center gap-3">
    <span id="ws-dot" class="w-2.5 h-2.5 rounded-full bg-red-500 transition-colors" title="WebSocket"></span>
    <span id="pc-count" class="text-xs text-gray-500">PC 0대</span>
    <a href="/auth/logout" class="text-xs text-gray-500 hover:text-gray-300 transition-colors">로그아웃</a>
  </div>
</header>

<!-- 명령 바 (콘솔 그룹 패널 — 기능/핸들러 무변경, 스킨만) -->
<div class="cmd-bar px-4 pt-3 pb-2 flex flex-wrap gap-x-3 gap-y-2 items-stretch sticky top-[52px] z-20">
  <!-- 그룹 1: 선택 -->
  <div class="cmd-group">
    <span class="cmd-legend">SELECT</span>
    <span id="sel-label" class="sel-badge shrink-0">0개 선택</span>
    <button onclick="selectAllPcs()" class="chip chip-indigo">전체선택</button>
    <button onclick="clearSelection()" class="chip chip-gray">전체해제</button>
  </div>

  <!-- 그룹 2: 매크로 제어 -->
  <div class="cmd-group">
    <span class="cmd-legend">MACRO</span>
    <button onclick="selCmd('start')" class="chip chip-green">▶ 시작</button>
    <button onclick="selCmd('exit')" class="chip chip-red">✕ 종료</button>
    <button onclick="selUpdaterCmd('update')" class="chip chip-cyan">↑ 업데이트+재시작</button>
  </div>

  <!-- 그룹 3: 콘텐츠 -->
  <div class="cmd-group">
    <span class="cmd-legend">CONTENT</span>
    <button onclick="selCmd('daily_dungeon')" class="chip chip-purple">일일던전</button>
    <button onclick="selCmd('nightmare')" class="chip chip-pink">악몽</button>
    <button onclick="selCmd('awakening')" class="chip chip-orange">각성</button>
    <button onclick="selCmd('abyss')" class="chip chip-blue">어비스</button>
    <button onclick="selCmd('collect_info')" class="chip chip-sky">정보수집</button>
  </div>

  <!-- 그룹 4: 거래 -->
  <div class="cmd-group">
    <span class="cmd-legend">TRADE</span>
    <span class="price-wrap"><span class="price-k">₭</span><input id="sale-price" type="number" min="0" placeholder="거래소가" title="거래소 등록 가격 (전체 공통) — 확정하면 사이트 닫았다 열어도 유지"></span>
    <button id="sale-price-btn" onclick="toggleSalePrice()" class="chip chip-gray">확정</button>
    <button onclick="sellAllSel()" class="chip chip-yellow">판매</button>
    <button onclick="settleSel()" class="chip chip-amber" title="판매대금 수령 — 계정 단위, 1캐릭만 접속해 걷음">정산</button>
  </div>
</div>

<main class="p-4 sm:p-6 space-y-6">

  <!-- 전광판 (순서: 온라인 → 완료 → 오드에너지 → 각성전 → 창고키나) -->
  <div class="grid grid-cols-2 sm:grid-cols-5 gap-3">
    <div class="stat-tile tile-green">
      <div class="stat-icon">🖥️</div>
      <div class="stat-num text-green-400" id="cnt-online">0</div>
      <div class="stat-label">온라인</div>
    </div>
    <div class="stat-tile tile-blue">
      <div class="stat-icon">✅</div>
      <div class="stat-num text-blue-400" id="cnt-completed">0</div>
      <div class="stat-label">완료</div>
    </div>
    <div class="stat-tile tile-yellow">
      <div class="stat-icon">⚡</div>
      <div class="stat-num text-yellow-400" id="cnt-odd-energy">–</div>
      <div class="stat-label">오드에너지</div>
    </div>
    <div class="stat-tile tile-indigo">
      <div class="stat-icon">⚔️</div>
      <div class="stat-num text-indigo-400" id="cnt-awakening">–</div>
      <div class="stat-label">각성전</div>
    </div>
    <div class="stat-tile tile-gold">
      <div class="stat-icon">💰</div>
      <div class="stat-num text-yellow-300" id="cnt-total-kina">–</div>
      <div class="stat-label">창고키나</div>
    </div>
  </div>

  <!-- 온라인 섹션 -->
  <section id="online-section">
    <h2 class="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
      <span class="w-2 h-2 rounded-full bg-green-500 pulse inline-block"></span>
      온라인 <span id="online-count" class="text-gray-600 normal-case">(0)</span>
    </h2>
    <div id="grid-online" class="gap-3" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr))">
      <div class="text-gray-600 text-sm col-span-full text-center py-10">대기 중... (매크로 연결 없음)</div>
    </div>
  </section>

  <!-- 오프라인 섹션 -->
  <section id="offline-section" class="hidden">
    <h2 class="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
      <span class="w-2 h-2 rounded-full bg-gray-600 inline-block"></span>
      오프라인 <span id="offline-count" class="text-gray-600 normal-case">(0)</span>
    </h2>
    <div id="grid-offline" class="gap-3" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr))"></div>
  </section>

  <!-- 전체 캐릭터 현황 테이블 -->
  <section class="bg-gray-900 rounded-xl p-5 border border-gray-800">
    <div class="flex items-center justify-between mb-3">
      <h2 class="text-sm font-semibold text-gray-400 uppercase tracking-widest flex items-center gap-2 cursor-pointer" onclick="toggleCharTable()">
        <span id="char-table-arrow">▶</span> 전체 캐릭터 현황
        <span id="char-table-count" class="text-gray-600 normal-case">(0)</span>
      </h2>
      <button onclick="loadCharTable()" class="text-xs text-gray-600 hover:text-gray-300 px-2 py-1 bg-gray-800 rounded">↻ 새로고침</button>
      <button onclick="toggleAllPcGroups(true)" class="text-xs text-gray-600 hover:text-gray-300 px-2 py-1 bg-gray-800 rounded ml-2">▼ 전체 열기</button>
      <button onclick="toggleAllPcGroups(false)" class="text-xs text-gray-600 hover:text-gray-300 px-2 py-1 bg-gray-800 rounded ml-1">▲ 전체 닫기</button>
      <button onclick="printCharTable()" class="text-xs text-gray-600 hover:text-gray-300 px-2 py-1 bg-gray-800 rounded ml-2">🖨 인쇄</button>
    </div>
    <div id="char-table-wrap" class="hidden">
      <div class="flex gap-2 mb-3">
        <input id="char-filter" type="text" placeholder="PC번호 / 이름 검색..."
          class="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm text-gray-200 w-64 focus:outline-none focus:border-indigo-500"
          oninput="filterCharTable()">
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm text-left whitespace-nowrap">
          <thead class="text-xs text-gray-400 uppercase bg-gray-800/80 sticky top-0">
            <tr>
              <th class="px-3 py-2 text-center w-8">✓</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white" onclick="sortCharTable('slot')"># ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white" onclick="sortCharTable('name')">이름 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white" onclick="sortCharTable('char_class')">직업 ⇅</th>
              <th class="px-3 py-2 text-center">수집</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-right" onclick="sortCharTable('gear_power')">장비전투력 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-right" onclick="sortCharTable('power_power')">파워전투력 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white" onclick="sortCharTable('odd_energy')">오드에너지 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center" onclick="sortCharTable('daily_ticket')">일일던전 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center" onclick="sortCharTable('nightmare_ticket')">악몽 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center" onclick="sortCharTable('awakening_ticket')">각성 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white" onclick="sortCharTable('sanctuary')">성역 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center" onclick="sortCharTable('mail_count')">우편 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center" onclick="sortCharTable('potion_count')">물약 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center" onclick="sortCharTable('return_scroll_count')">귀환 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white" onclick="sortCharTable('extract_level')">정기추출 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center">아르카나</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center">장비</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-right" onclick="sortCharTable('gakin_kina')">각인키나 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-right" onclick="sortCharTable('trade_kina')">거래키나 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-right" onclick="sortCharTable('total_kina')">창고키나 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center" onclick="sortCharTable('abyss_time')">어비스 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-right" onclick="sortCharTable('abyss_point')">어비스P ⇅</th>
            </tr>
          </thead>
          <tbody id="char-tbody" class="divide-y divide-gray-800"></tbody>
        </table>
      </div>
    </div>
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

<!-- 베트남어 캐릭터 뷰 모달 (모바일 가시성 + 언어토글 + 자가체크) -->
<div id="vietnam-modal" class="hidden fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-2 sm:p-4">
  <div class="bg-gray-900 w-full max-w-3xl max-h-[93vh] flex flex-col rounded-xl border border-gray-700 shadow-2xl">
    <div class="flex items-center justify-between px-3 py-2.5 border-b border-gray-800 gap-2">
      <h2 class="font-bold text-red-400 text-base shrink-0" id="vn-title">🇻🇳 Nhân vật</h2>
      <div class="flex items-center gap-1.5 flex-wrap justify-end">
        <div class="flex rounded-lg overflow-hidden border border-gray-700 text-xs font-semibold">
          <button id="vn-lang-vi" onclick="vnSetLang('vi')">VI</button>
          <button id="vn-lang-ko" onclick="vnSetLang('ko')">KO</button>
        </div>
        <button id="vn-reset" onclick="vnResetAll()" class="text-xs px-2 py-1 bg-gray-700/70 hover:bg-gray-600 text-gray-200 rounded whitespace-nowrap">Đặt lại</button>
        <button onclick="loadVietnam()" class="text-xs px-2 py-1 bg-red-800/60 hover:bg-red-700 text-red-200 rounded">↻</button>
        <button onclick="closeVietnamModal()" class="text-gray-400 hover:text-gray-200 text-xl leading-none px-1">✕</button>
      </div>
    </div>
    <div class="flex-1 overflow-auto p-2 scrollbar-thin">
      <table class="w-full text-sm text-left whitespace-nowrap">
        <thead class="text-xs text-gray-400 uppercase bg-gray-800/90 sticky top-0"><tr id="vietnam-head"></tr></thead>
        <tbody id="vietnam-body" class="divide-y divide-gray-800"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- 카드 컨텍스트 메뉴 v2 — 2열 콘솔 그리드 (핸들러 전부 보존, 구조만 재편)
     기존 세로 19줄(~615px)이 하단서 잘리고 스캔 느리던 것 → 그룹 그리드 ~440px.
     '재시작'은 updaterCmd(프로세스 kill+재기동)=진짜 재시작. 업데이터 줄의 ▶■는
     매크로 '프로세스' 제어(크래시된 PC 살리기/강제종료), ✕는 업데이터 자체 종료. -->
<div id="card-menu" class="hidden fixed z-50 cm-panel" onclick="event.stopPropagation()">
  <div class="cm-head" id="menu-pc-label">PC-??</div>
  <div class="cm-sec">MACRO</div>
  <div class="cm-grid2">
    <button class="cm-btn chip-green"  onclick="cardCmd('start')">▶ 시작</button>
    <button class="cm-btn chip-gray"   onclick="cardCmd('stop')">■ 정지</button>
    <button class="cm-btn chip-red"    onclick="cardCmd('exit')">✕ 종료</button>
    <button class="cm-btn chip-yellow" onclick="updaterCmd('restart')" title="매크로 프로세스 재시작 (업데이터 경유 — 진짜 재시작)">↺ 재시작</button>
  </div>
  <div class="cm-sec">ACTION</div>
  <div class="cm-grid2">
    <button class="cm-btn chip-purple cm-span2" onclick="cardCmdSwitch()">⇄ 캐릭 전환...</button>
    <button class="cm-btn chip-yellow" onclick="sellAllFromMenu()" title="전 캐릭 순회 판매 (상단 거래소가 확정 필요)">$ 판매</button>
    <button class="cm-btn chip-amber"  onclick="settleFromMenu()" title="판매대금 수령 — 계정 단위, 1캐릭만 접속해 걷음">₭ 정산</button>
    <button class="cm-btn chip-cyan"   onclick="collectInfoFromMenu()">📡 정보수집</button>
    <button class="cm-btn chip-gray"   onclick="cardCmd('go_home')">⌂ 귀환</button>
  </div>
  <div class="cm-sec">VIEW</div>
  <div class="cm-grid3">
    <button class="cm-btn chip-indigo" onclick="openLogFromMenu()">📋 로그</button>
    <button class="cm-btn chip-sky"    onclick="openInfoFromMenu()">📊 정보</button>
    <button class="cm-btn chip-pink"   onclick="screenshotFromMenu()">📸 스샷</button>
  </div>
  <div class="cm-sec">UPDATER · 프로세스</div>
  <div class="cm-grid4">
    <button class="cm-btn chip-green"  onclick="updaterCmd('start')" title="매크로 프로세스 시작 (크래시된 PC 살리기)">▶</button>
    <button class="cm-btn chip-gray"   onclick="updaterCmd('stop')" title="매크로 프로세스 강제종료">■</button>
    <button class="cm-btn chip-red"    onclick="updaterCmd('exit')" title="업데이터 자체 종료 (원격제어 끊김 — 주의)">✕</button>
    <button class="cm-btn chip-yellow" onclick="updaterCmd('restart')" title="매크로 프로세스 재시작">↺</button>
  </div>
  <div class="cm-grid2">
    <button class="cm-btn chip-cyan cm-span2"   onclick="updaterCmd('update')">↑ 업데이트+재시작</button>
    <button class="cm-btn chip-purple cm-span2" onclick="updaterCmd('update_only')">⬆ 업데이트만</button>
  </div>
  <button class="cm-danger" onclick="deletePCFromMenu()">🗑 이 PC를 목록에서 삭제</button>
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
let latestVersions = {macro:'', updater:''};
let selectedPcs = new Set();
let logModalPc = null;
let menuPcId = null;

const STATUS_CFG = {
  hunting:      {label:'사냥 중',   bg:'bg-green-500/20',  border:'border-green-700',  badge:'bg-green-500',  text:'text-green-400',  online:true},
  selling:      {label:'판매 중',   bg:'bg-blue-500/20',   border:'border-blue-700',   badge:'bg-blue-500',   text:'text-blue-400',   online:true},
  abyss:        {label:'어비스',   bg:'bg-fuchsia-500/20', border:'border-fuchsia-700', badge:'bg-fuchsia-500', text:'text-fuchsia-400', online:true},
  moving:       {label:'사냥 중',   bg:'bg-green-500/20',  border:'border-green-700',  badge:'bg-green-500',  text:'text-green-400',  online:true},
  switching:    {label:'캐릭 전환', bg:'bg-purple-500/20', border:'border-purple-700', badge:'bg-purple-400', text:'text-purple-400', online:true},
  reconnecting: {label:'재연결 중', bg:'bg-orange-500/20', border:'border-orange-700', badge:'bg-orange-400', text:'text-orange-400', online:true},
  captcha:      {label:'캡차',      bg:'bg-pink-500/20',   border:'border-pink-700',   badge:'bg-pink-500',   text:'text-pink-400',   online:true},
  dead:         {label:'사망',      bg:'bg-red-500/20',    border:'border-red-700',    badge:'bg-red-500',    text:'text-red-400',    online:true},
  idle:         {label:'대기',      bg:'bg-gray-700/20',   border:'border-gray-600',   badge:'bg-gray-500',   text:'text-gray-400',   online:true},
  subquest:     {label:'서브퀘',   bg:'bg-lime-500/20',   border:'border-lime-700',   badge:'bg-lime-500',   text:'text-lime-400',   online:true},
  dungeon:      {label:'던전',    bg:'bg-purple-500/20', border:'border-purple-700', badge:'bg-purple-500', text:'text-purple-400', online:true},
  awakening:    {label:'각성전',  bg:'bg-indigo-500/20', border:'border-indigo-700', badge:'bg-indigo-500', text:'text-indigo-400', online:true},
  awakening_wait:{label:'각성전 대기', bg:'bg-red-500/20', border:'border-red-700', badge:'bg-red-500', text:'text-red-400', online:true},
  collecting:   {label:'정보수집', bg:'bg-cyan-500/20',   border:'border-cyan-700',   badge:'bg-cyan-500',   text:'text-cyan-400',   online:true},
  paused:       {label:'일시정지', bg:'bg-amber-500/20',  border:'border-amber-700',  badge:'bg-amber-500',  text:'text-amber-400',  online:true},
  error:        {label:'에러',      bg:'bg-red-500/20',    border:'border-red-700',    badge:'bg-red-500',    text:'text-red-400',    online:true},
  offline:      {label:'오프라인',  bg:'bg-gray-900/40',   border:'border-gray-800',   badge:'bg-gray-700',   text:'text-gray-600',   online:false},
};
const LOG_COLOR = {error:'text-red-400', warn:'text-yellow-400', info:'text-gray-300', debug:'text-gray-600'};

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
function relTime(iso) {
  if (!iso) return '–';
  const d = Math.floor((Date.now()-new Date(iso+'Z').getTime())/1000);
  if (d<5) return '방금'; if (d<60) return d+'초 전';
  if (d<3600) return Math.floor(d/60)+'분 전'; return Math.floor(d/3600)+'시간 전';
}

const CLASS_LABEL = {gungsung:'궁성',spirit:'정령성',kumsung:'검성',chiyousung:'치유성'};

// ─── 완료 뱃지 (사냥=매일 05:00 초기화 / 각성전=매주 수요일 05:00 초기화) ─────────
// 완료 판정에 '초기화 경계 이후 데이터'만 인정 — PC가 밤새 꺼져 있어도 어제 완료가
// 오늘 완료로 둔갑하지 않게 시각 게이트.
function fmtTs(d){const p=n=>String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;}
function lastDailyReset(){const d=new Date();if(d.getHours()<5)d.setDate(d.getDate()-1);d.setHours(5,0,0,0);return d;}
function lastWeeklyReset(){const d=lastDailyReset();while(d.getDay()!==3)d.setDate(d.getDate()-1);return d;}  // 3=수요일
function isHuntDone(dp){
  if(!dp||!dp.length) return false;
  const cut=fmtTs(lastDailyReset());
  return dp.every(c=>c.completed && ((c.completed_time||'').replace('T',' ')>=cut));
}
function isAwakenDone(pc_id){
  // 규칙(사용자 확정): 전 캐릭터 각성 티켓 0/3이면 스탬프 — 단순 판정.
  // (수요일 05시 초기화 후엔 게임이 3/3으로 돌아가고 다음 정보수집 때 DB 갱신돼 자연 소멸)
  const rows=charTableData.filter(r=>r.pc_id===pc_id);
  return rows.length>0 && rows.every(r=>parseInt(r.awakening_ticket)===0);
}

// ─── 오늘 진행 현황 ──────────────────────────────────────────────────────────
function buildDailyProgress(dp, activeSlot, charNames, pc) {
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
    const classLabel = isActive && pc.map ? (CLASS_LABEL[pc.map]||'') : '';
    return `<div class="flex flex-col items-center ${cls} border rounded-md px-1 py-0.5 text-center cursor-default"
      style="min-width:0" title="${name}${done?' ✓ '+time:isActive?' 진행 중':''}">
      <span class="font-bold text-xs leading-none">${icon}</span>
      <span style="font-size:9px;line-height:1.2;max-width:100%;overflow:hidden;white-space:nowrap">${short}</span>
      ${classLabel?`<span style="font-size:8px;line-height:1;color:#9ca3af">${classLabel}</span>`:''}
    </div>`;
  }).join('');
  return `<div class="mt-2 pt-2 border-t border-gray-800/60">
    <div class="flex items-center justify-between mb-1">
      <span class="text-gray-400" style="font-size:10px">오늘 완료 <span class="${completed===total?'text-green-500':'text-gray-500'}">${completed}/${total}</span></span>
      ${pc._total_kina?`<span class="text-yellow-400 font-semibold whitespace-nowrap" style="font-size:12px">창고키나 ${fmtKinaShort(pc._total_kina)}</span>`:''}
    </div>
    <div class="grid gap-1" style="grid-template-columns:repeat(${total},minmax(0,1fr))">${slots}</div>
  </div>`;
}

// ─── 카드 렌더링 ──────────────────────────────────────────────────────────────
function buildCard(pc) {
  const st = pc.status||'offline';
  const cfg = STATUS_CFG[st]||STATUS_CFG.offline;
  const pulse = (st==='hunting'||st==='selling'||st==='abyss'||st==='awakening_wait')?' pulse':'';   // 각성전 대기 = 깜빡여서 눈에 띄게
  const sel = selectedPcs.has(pc.pc_id)?' card-sel':'';
  const errHtml = (pc.errors||[]).slice(0,3).map(e=>
    `<div class="text-xs text-red-400 bg-red-900/30 rounded px-2 py-0.5">⚠ ${e}</div>`).join('');
  const bugBadge = (pc._bug_count||0)>0
    ? `<span class="ml-1.5 px-1.5 py-0.5 bg-red-700/80 text-red-200 rounded text-xs font-bold leading-none cursor-pointer" onclick="event.stopPropagation();openBugsModal('${pc.pc_id}')">🐛 ${pc._bug_count}</span>`
    : '';
  const ucls = {'running':'text-green-400','stopped':'text-gray-500','updating':'text-cyan-400','crashed':'text-red-400'}[pc._updater_state]||'text-gray-600';
  const mvcls = (pc.macro_version && latestVersions.macro && pc.macro_version !== latestVersions.macro) ? 'text-red-400' : 'text-gray-700';
  const uvcls = (pc._updater_version && latestVersions.updater && pc._updater_version !== latestVersions.updater) ? 'text-red-400' : 'text-gray-700';
  const macroVer = pc.macro_version ? `<span class="${mvcls}">매크로 v${pc.macro_version}</span>` : '';
  const updaterRow = (pc._updater_state&&pc._updater_state!=='unknown')
    ? `<div class="mt-1 flex items-center gap-1 text-gray-600 whitespace-nowrap overflow-hidden" style="font-size:10px">${macroVer}${macroVer?'<span class="text-gray-800">|</span>':''}<span>업데이터</span><span class="${ucls}">${pc._updater_state}</span>${pc._updater_version?`<span class="${uvcls}">v${pc._updater_version}</span>`:''}</div>`
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
  // 완료 스탬프(이모지만 — 색이 신호): 🏹초록=오늘 사냥 완료 / ⚔인디고=전 캐릭 각성 0/3
  const doneBadges =
    (isHuntDone(pc.daily_progress)?`<span class="done-badge done-hunt" title="오늘 사냥 완료 — 매일 새벽 5시 초기화">🏹</span>`:'') +
    (isAwakenDone(pc.pc_id)?`<span class="done-badge done-awaken" title="각성전 완료 — 전 캐릭 0/3 (수요일 새벽 5시 초기화)">⚔</span>`:'');
  return `<div id="card-${pc.pc_id}"
    class="relative bg-gray-900 rounded-xl p-3 border ${cfg.border} ${cfg.bg}${sel} transition-all group cursor-pointer select-none"
    onclick="toggleSelect('${pc.pc_id}',event)"
    oncontextmenu="openCardMenu('${pc.pc_id}',event);return false">
    <div class="flex items-start justify-between mb-2">
      <div class="flex items-center gap-2 min-w-0">
        <span class="drag-handle shrink-0 cursor-grab active:cursor-grabbing text-gray-700 hover:text-gray-400 select-none" style="font-size:14px;line-height:1" title="드래그로 순서 변경">⠿</span>
        <div class="min-w-0">
          <div class="font-bold text-base flex items-center gap-0 min-w-0 flex-wrap"><span class="truncate">${pc.pc_id||'?'}</span>${doneBadges}${bugBadge}${activeTag}</div>
        </div>
      </div>
      <div class="flex items-center gap-1 shrink-0">
        <span class="inline-flex items-center gap-1.5 text-base font-bold ${cfg.text}">
          <span class="w-3 h-3 rounded-full ${cfg.badge}${pulse}"></span>
          ${cfg.label}
        </span>
      </div>
    </div>
    <div class="grid grid-cols-2 gap-x-4 gap-y-1 text-sm mt-2">
      <div><span class="text-gray-400">진행도</span> <span class="text-white font-medium">${pc.hunt_progress!=null ? Math.round(pc.hunt_progress)+' %' : '–'}</span></div>
      <div class="whitespace-nowrap"><span class="text-gray-400">효율</span> <span class="text-white font-medium">${pc.efficiency!=null ? pc.efficiency.toFixed(1)+'%/h' : '–'}</span></div>
      <div class="col-span-2"><span class="text-gray-400">맵</span> <span class="text-white font-medium">${pc.map_name||'–'}</span></div>
      <div><span class="text-gray-400">업타임</span> <span class="text-white font-medium">${fmtSlotUptime(pc.slot_uptime, pc.slot||0, pc.uptime_hours)}</span></div>
      ${pc.server?`<div><span class="text-gray-400">서버</span> <span class="text-white font-medium">${pc.server}</span></div>`:''}
      <div><span class="text-gray-400">최근</span> <span class="text-white font-medium">${relTime(pc.last_active)}</span></div>
      <div><span class="text-gray-400">사망(30분)</span> <span class="${(pc.deaths_30m||0)>0?'text-red-400 font-bold':'text-white font-medium'}">${pc.deaths_30m||0}회</span></div>
    </div>
    ${errHtml?`<div class="mt-2 space-y-0.5">${errHtml}</div>`:''}
    ${buildDailyProgress(pc.daily_progress, activeSlot, pc.chars, pc)}
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
  const c={online:0,offline:0,completed:0,totalKina:0};
  const seenPc = new Set();
  pcs.forEach(p=>{
    const s=p.status||'offline';
    const isOnline = (STATUS_CFG[s]||STATUS_CFG.offline).online;
    if(isOnline) c.online++; else c.offline++;
    const dp = p.daily_progress||[];
    if(dp.length>0 && dp.every(d=>d.completed)) c.completed++;
    // 창고키나: PC별 1회만 합산 (창고 공유 → 중복 방지)
    if(p._total_kina && !seenPc.has(p.pc_id)) {
      seenPc.add(p.pc_id);
      c.totalKina += p._total_kina;
    }
  });
  // 오드에너지 + 각성전 티켓 합산 (charTableData 기준)
  let totalOdd = 0, totalAwaken = 0, awakenSeen = false;
  charTableData.forEach(r => {
    totalOdd += parseOddEnergy(r.odd_energy);
    if (r.awakening_ticket != null) { awakenSeen = true; totalAwaken += (parseInt(r.awakening_ticket) || 0); }
  });
  document.getElementById('cnt-online').textContent=c.online;
  document.getElementById('cnt-odd-energy').textContent=totalOdd > 0 ? totalOdd.toLocaleString() : '–';
  document.getElementById('cnt-awakening').textContent=awakenSeen ? totalAwaken.toLocaleString() : '–';
  document.getElementById('cnt-completed').textContent=c.completed;
  document.getElementById('cnt-total-kina').textContent=fmtKinaKor(c.totalKina);
}

// ─── 선택 ─────────────────────────────────────────────────────────────────────
function toggleSelect(pc_id, e) {
  if (e && (e.target.tagName === 'BUTTON' || e.target.closest('.drag-handle'))) return;
  selectedPcs.has(pc_id) ? selectedPcs.delete(pc_id) : selectedPcs.add(pc_id);
  const card = document.getElementById(`card-${pc_id}`);
  if (card) card.classList.toggle('card-sel', selectedPcs.has(pc_id));
  updateSelBar();
}

function clearSelection() {
  selectedPcs.clear();
  document.querySelectorAll('.card-sel').forEach(el=>el.classList.remove('card-sel'));
  updateSelBar();
}

function updateSelBar() {
  const n=selectedPcs.size;
  document.getElementById('sel-label').textContent=n>0?`${n}개 선택`:'선택 없음';
}

function selectAllPcs() {
  Object.keys(state).forEach(id=>selectedPcs.add(id));
  document.querySelectorAll('[id^="card-"]').forEach(el=>el.classList.add('card-sel'));
  updateSelBar();
}

async function selUpdaterCmd(command, args={}) {
  if(selectedPcs.size===0){alert('PC를 선택하세요');return;}
  const n=selectedPcs.size;
  for(const id of selectedPcs) {
    await fetch(`/updater/command/${id}`, {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({command,...args})});
  }
  showToast(`${n}대 업데이터 ${command} (선택 해제됨)`);
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
  if(!selectedPcs.size){alert('PC를 선택하세요');return;}
  const n=selectedPcs.size;
  await Promise.all([...selectedPcs].map(id=>sendCmd(id,command,args)));
  showToast(`✓ ${command} → 선택 ${n}대 (선택 해제됨)`);
  loadCmdHistory();
  clearSelection();   // ★명령 전송 완료 = 선택 자동 해제 — 같은 세트에 실수로 중복 명령 방지★
}

// ─── 판매(sell_all) — 거래소 지정가를 args.price로 전송 ─────────────────────────
// 거래소 가격은 localStorage에 저장(확정) → 사이트 닫았다 열어도·서버 재배포에도 유지.
function getSalePrice() {
  const el=document.getElementById('sale-price');
  const v=parseInt((el&&el.value)||'0',10);
  return isNaN(v)?0:v;
}
function isSalePriceConfirmed(){ return localStorage.getItem('sale_price_confirmed')==='1'; }
function loadSalePrice() {
  const el=document.getElementById('sale-price'), btn=document.getElementById('sale-price-btn');
  if(!el||!btn) return;
  const v=localStorage.getItem('sale_price');
  if(v) el.value=v;
  if(isSalePriceConfirmed()){ el.readOnly=true; el.classList.add('opacity-60'); btn.textContent='수정'; }
  else { el.readOnly=false; el.classList.remove('opacity-60'); btn.textContent='확정'; }
}
function toggleSalePrice() {
  const el=document.getElementById('sale-price'), btn=document.getElementById('sale-price-btn');
  if(isSalePriceConfirmed()){
    localStorage.setItem('sale_price_confirmed','0');
    el.readOnly=false; el.classList.remove('opacity-60'); btn.textContent='확정'; el.focus();
  } else {
    const p=parseInt(el.value||'0',10);
    if(!p||p<=0){alert('거래소 가격을 입력하세요');return;}
    localStorage.setItem('sale_price', String(p));
    localStorage.setItem('sale_price_confirmed','1');
    el.readOnly=true; el.classList.add('opacity-60'); btn.textContent='수정';
    showToast(`거래소 가격 확정: ${p.toLocaleString()} (유지됨)`);
  }
}
async function sellAllSel() {
  const p=getSalePrice();
  if(p<=0||!isSalePriceConfirmed()){alert('먼저 거래소 가격을 입력하고 [확정] 하세요');return;}
  if(!selectedPcs.size){alert('PC를 선택하세요');return;}
  if(!confirm(`선택 ${selectedPcs.size}대 판매 실행\n거래소 지정가: ${p.toLocaleString()}`))return;
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
  const ver=pc.macro_version?`v${pc.macro_version}`:'';
  document.getElementById('menu-pc-label').innerHTML=
    `<span class="font-bold text-gray-100">${pc_id}</span>`+
    `<span class="inline-flex items-center gap-1 ${cfg.text}" style="font-size:11px"><span class="w-2 h-2 rounded-full ${cfg.badge}"></span>${cfg.label}</span>`+
    (ver?`<span class="text-gray-500 ml-auto" style="font-size:10px">${ver}</span>`:'');
  menu.classList.remove('hidden');
  let top=e.clientY+4, left=e.clientX;
  if(left+246>window.innerWidth) left=window.innerWidth-250;   // 메뉴 v2 폭 238px
  if(top+480>window.innerHeight) top=e.clientY-484;            // 메뉴 v2 실측 높이 472px
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

// ─── 정산(settle) — 판매대금 수령. 계정 단위라 1캐릭만 접속해 걷고 종료 (가격 불필요) ───
async function settleSel() {
  if(selectedPcs.size===0){showToast('PC를 먼저 선택하세요');return;}
  if(!confirm(`선택 ${selectedPcs.size}대 정산 실행\n(계정 단위 — 1캐릭만 접속해 판매대금 수령, ~2분)`))return;
  await selCmd('settle');
}

async function settleFromMenu() {
  if(!menuPcId) return;
  const pc=menuPcId;
  if(!confirm(`${pc} 정산 실행\n(계정 단위 — 1캐릭만 접속해 판매대금 수령, ~2분)`))return;
  closeCardMenu();
  const ok=await sendCmd(pc,'settle',{});
  showToast(ok?`✓ 정산 → ${pc}`:`✗ 정산 전송 실패`);
  loadCmdHistory();
}

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
// ★WS state 렌더 디바운스(성능, 2026-07-21): 17대가 30초 주기 보고 = state 브로드캐스트가
//   ~2초에 한 번인데 그때마다 카드 전체 innerHTML 재구축은 낭비(장시간 열어두면 체감 느려짐).
//   700ms로 모아서 1회만 렌더.★
let _renderTimer=null;
function scheduleRender(){ if(_renderTimer) return; _renderTimer=setTimeout(()=>{_renderTimer=null; renderCards();},700); }

function connectWS() {
  const proto=location.protocol==='https:'?'wss':'ws';
  const ws=new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen=()=>{document.getElementById('ws-dot').className='w-2.5 h-2.5 rounded-full bg-green-500 transition-colors';};
  ws.onmessage=(e)=>{
    const msg=JSON.parse(e.data);
    if(msg.type==='state'){state={};(msg.pcs||[]).forEach(p=>{state[p.pc_id]=p;});if(msg.latest)latestVersions=msg.latest;scheduleRender();}
    else if(msg.type==='log'&&logModalPc===msg.pc_id){appendLogLine(msg.level,msg.message);}
    else if(msg.type==='cmd_history'){renderCmdHistory(msg.commands||[]);}
    else if(msg.type==='char_info'){handleCharInfoMsg(msg);}
  };
  ws.onclose=(e)=>{
    document.getElementById('ws-dot').className='w-2.5 h-2.5 rounded-full bg-red-500 transition-colors';
    if(e&&e.code===1008){location.reload();return;}   // 세션 무효(만료 등) → 새로고침으로 로그인 이동
    setTimeout(connectWS,3000);
  };
}

// ─── 서버 재시작 감지 → 자동 새로고침 ────────────────────────────────────────
let serverBoot=null;
async function checkServerBoot(){
  try{
    const r=await fetch('/ping',{cache:'no-store'});
    if(!r.ok)return;
    const b=(await r.json()).boot;
    if(serverBoot===null){serverBoot=b;return;}   // 최초 폴링 = 기준값 저장
    if(b!==serverBoot)location.reload();            // boot 바뀜 = 서버 재시작 → 새로고침
  }catch(e){/* 재시작 중이라 연결 실패 = 무시, 다음 폴링에서 감지 */}
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
  if (totalMin < 60) return totalMin + 'm';
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return m > 0 ? h + 'h ' + m + 'm' : h + 'h';
}
function fmtAt(iso) {
  if (!iso) return '–';
  return iso.replace('T',' ').slice(0,16);
}

// ─── 전체 캐릭터 테이블 ────────────────────────────────────────────────────
let charTableData = [];
let charTableSort = {key:'pc_id', asc:true};
let charTableVisible = false;

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
    charTableData = d.characters || [];
    document.getElementById('char-table-count').textContent = `(${charTableData.length})`;
    renderCharTable();
    renderCards();   // 각성완료 뱃지가 charTableData 기반 — 로드 후 카드 재렌더(내부에서 refreshSummary 호출)
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
    if(r.ok) vietnamData = (await r.json()).characters || [];
  }catch(e){ console.error('vietnam load', e); }
  renderVietnam();
}
function sortVietnam(key){
  if(vietnamSort.key===key) vietnamSort.asc = !vietnamSort.asc;
  else vietnamSort = {key, asc:true};
  renderVietnam();
}
function _vietnamVal(r, key){
  if(key==='odd_energy') return parseOddEnergy(r.odd_energy);
  const v = r[key];
  const n = Number(String(v==null?'':v).replace(/[^\d.-]/g,''));
  return isNaN(n) ? String(v==null?'':v) : n;
}
function renderVietnam(){
  const L = vietnamLang, T = VN_T[L];
  document.getElementById('vn-title').textContent = T.title;
  document.getElementById('vn-reset').textContent = T.reset;
  const on='px-2 py-1 bg-red-700 text-white', off='px-2 py-1 text-gray-400 hover:text-gray-200';
  document.getElementById('vn-lang-vi').className = L==='vi'?on:off;
  document.getElementById('vn-lang-ko').className = L==='ko'?on:off;
  const {key, asc} = vietnamSort;
  const rows = [...vietnamData].sort((a,b)=>{
    let va=_vietnamVal(a,key), vb=_vietnamVal(b,key);
    if(typeof va==='number' && typeof vb==='number') return asc?va-vb:vb-va;
    va=String(va).toLowerCase(); vb=String(vb).toLowerCase();
    return asc?va.localeCompare(vb):vb.localeCompare(va);
  });
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
          `<td class="px-2 py-1.5 text-center"><input type="checkbox" ${d?'checked':''} onchange="vnToggle('${r.pc_id}',${r.slot},this.checked)" class="w-5 h-5 cursor-pointer accent-green-500 align-middle"></td>`+
          VIETNAM_COLS.map(c=>{
            const cls = (c.red && c.red(r)) ? 'text-red-400 font-bold' : 'text-gray-200';
            return `<td class="px-3 py-1.5 ${_ta(c.align)} ${cls}">${c.fmt(r)}</td>`;
          }).join('')+
          `</tr>`;
      }).join('')
    : `<tr><td colspan="${VIETNAM_COLS.length+1}" class="text-center text-gray-600 py-8">${T.nodata}</td></tr>`;
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
    const nmTicket = r.nightmare_ticket != null ? `${r.nightmare_ticket}/14` : '–';
    const nmProg = r.nightmare_progress || '';
    const nm = nmProg ? `${nmTicket} <span class="text-pink-400 text-[10px]">${nmProg}</span>` : nmTicket;
    const aw = r.awakening_ticket != null ? `${r.awakening_ticket}/3` : '–';
    const sanc = r.sanctuary || '–';
    const mail = r.mail_count != null ? r.mail_count : '–';
    const potion = r.potion_count != null ? r.potion_count : '–';
    const scroll = r.return_scroll_count != null ? r.return_scroll_count : '–';
    const potionLow = typeof r.potion_count === 'number' && r.potion_count <= 50;
    const scrollLow = typeof r.return_scroll_count === 'number' && r.return_scroll_count <= 50;
    const ext = r.extract_level || '–';
    const arcanaLink = r.arcana_image ? `<a href="#" onclick="showScreenshot('arcana','${r.pc_id}',${r.slot});return false" class="text-purple-400 hover:text-purple-300 underline">보기</a>` : '–';
    const equipLink = r.equip_image ? `<a href="#" onclick="showScreenshot('equip','${r.pc_id}',${r.slot});return false" class="text-blue-400 hover:text-blue-300 underline">보기</a>` : '–';
    const gakin = r.gakin_kina ? Number(r.gakin_kina).toLocaleString() : '–';
    const trade = r.trade_kina ? Number(r.trade_kina).toLocaleString() : '–';
    const rc = (s) => `<span class="text-red-400 font-bold">${s}</span>`;
    const oddFirst = odd !== '–' ? parseInt(odd) : 0;
    const oddFull = oddFirst >= 840;
    const dailyNum = daily !== '–' ? parseInt(daily) : 0;
    const dailyFull = dailyNum >= 14;
    const nmFull = r.nightmare_ticket >= 14;
    const awFull = r.awakening_ticket >= 3;
    const sancParts = sanc !== '–' ? sanc.match(/(\d+).*\/(\d+)/) : null;
    const sancFirst = sancParts ? parseInt(sancParts[1]) : 0;
    const sancMax = sancParts ? parseInt(sancParts[2]) : 0;
    const sancFull = r.gear_power >= 2700 && sancMax > 0 && sancFirst >= sancMax;
    const extFull = ext.includes('입문') && ext.includes('50');
    const hasRed = oddFull || dailyFull || nmFull || awFull || sancFull || extFull;
    const bg = hasRed ? 'bg-red-950/40' : (i % 2 === 0 ? 'bg-gray-900' : 'bg-gray-800/50');
    return `<tr class="${bg} hover:bg-gray-700/50 transition-colors">
      <td class="px-3 py-1.5 text-center">
        <input type="checkbox" ${slotEnabled ? 'checked' : ''}
          onchange="toggleSlotFilter('${r.pc_id}',${r.slot},this.checked)"
          onclick="event.stopPropagation()" class="cursor-pointer accent-green-500"></td>
      <td class="px-3 py-1.5 text-gray-400">${r.slot||'–'}</td>
      <td class="px-3 py-1.5 text-white">${r.name||'–'}</td>
      <td class="px-3 py-1.5 text-xs font-medium ${clsColor}">${cls}</td>
      <td class="px-3 py-1.5 text-center"><button onclick="collectSlot('${r.pc_id}',${r.slot})" class="px-2 py-0.5 text-xs rounded bg-sky-900/60 hover:bg-sky-700 text-sky-300 whitespace-nowrap" title="이 캐릭터만 정보수집">📡</button></td>
      <td class="px-3 py-1.5 text-right ${gpLow?'':'text-gray-200'}">${gpLow?rc(gp):gp}</td>
      <td class="px-3 py-1.5 text-right font-medium ${ppLow?'':'text-cyan-400'}">${ppLow?rc(pp):pp}</td>
      <td class="px-3 py-1.5 ${oddFull?'':'text-yellow-400'}">${oddFull?rc(odd):odd}</td>
      <td class="px-3 py-1.5 text-center">${dailyFull?rc(daily):daily}</td>
      <td class="px-3 py-1.5 text-center">${nmFull?rc(nm):nm}</td>
      <td class="px-3 py-1.5 text-center">${awFull?rc(aw):aw}</td>
      <td class="px-3 py-1.5">${sancFull?rc(sanc):sanc}</td>
      <td class="px-3 py-1.5 text-center">${mail}</td>
      <td class="px-3 py-1.5 text-center">${potionLow?rc(potion):potion}</td>
      <td class="px-3 py-1.5 text-center">${scrollLow?rc(scroll):scroll}</td>
      <td class="px-3 py-1.5">${extFull?rc(ext):ext}</td>
      <td class="px-3 py-1.5 text-center">${arcanaLink}</td>
      <td class="px-3 py-1.5 text-center">${equipLink}</td>
      <td class="px-3 py-1.5 text-right text-emerald-400">${gakin}</td>
      <td class="px-3 py-1.5 text-right text-orange-400">${trade}</td>
      <td class="px-3 py-1.5 text-right text-yellow-300 font-medium">${kina}</td>
      <td class="px-3 py-1.5 text-center text-fuchsia-300">${r.abyss_time || '–'}</td>
      <td class="px-3 py-1.5 text-right text-fuchsia-200">${r.abyss_point ? Number(r.abyss_point).toLocaleString() : '–'}</td>
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
    const redCount = pcRows.filter(r => {
      const odd = r.odd_energy||''; const sanc = r.sanctuary||''; const ext = r.extract_level||'';
      return parseInt(odd)>=840 ||
             parseInt(r.daily_ticket)>=14 || r.nightmare_ticket>=14 || r.awakening_ticket>=3 ||
             (r.gear_power>=2700 && parseInt(sanc)>=2) || (ext.includes('입문')&&ext.includes('50'));
    }).length;
    const redBadge = redCount > 0 ? ` <span class="text-red-400 text-xs">(${redCount})</span>` : '';
    const pcServer = (state[pc] || {}).server || '';
    const serverTag = pcServer ? ` <span class="text-cyan-400 text-xs font-normal ml-1">[${pcServer}]</span>` : '';
    const pcKinaRaw = pcRows[0]?.total_kina;
    const kinaTag = pcKinaRaw ? ` <span class="text-yellow-300 text-xs font-normal ml-1">₭${Number(pcKinaRaw).toLocaleString()}</span>` : '';
    html += `<tr class="bg-gray-700/80 cursor-pointer" onclick="togglePcGroup('${pc}')">
      <td colspan="23" class="px-3 py-2 font-bold text-gray-100">
        <div class="flex items-center gap-2">
          <span id="pc-arrow-${pc}">▶</span>
          <span>${pc}</span>
          <span class="text-gray-500 text-xs font-normal">${pcRows.length}캐릭</span>
          ${serverTag}${kinaTag}${redBadge}
          <div class="flex items-center gap-1 ml-auto flex-wrap justify-end" onclick="event.stopPropagation()">
            <button onclick="selectAllSlots('${pc}', ${JSON.stringify(pcRows.map(r=>r.slot))}, true)" class="px-1.5 py-0.5 text-xs rounded bg-gray-600/60 hover:bg-gray-500 text-gray-200 whitespace-nowrap">전체선택</button>
            <button onclick="selectAllSlots('${pc}', ${JSON.stringify(pcRows.map(r=>r.slot))}, false)" class="px-1.5 py-0.5 text-xs rounded bg-gray-600/60 hover:bg-gray-500 text-gray-400 whitespace-nowrap">전체해제</button>
            <span class="text-gray-600">|</span>
            <button onclick="sendCmd('${pc}','start')" class="px-1.5 py-0.5 text-xs rounded bg-green-900/60 hover:bg-green-700 text-green-300 whitespace-nowrap">▶ 시작</button>
            <button onclick="sendCmd('${pc}','exit')" class="px-1.5 py-0.5 text-xs rounded bg-red-900/60 hover:bg-red-700 text-red-300 whitespace-nowrap">✕ 종료</button>
            <button onclick="sendUpdaterCmd('${pc}','update')" class="px-1.5 py-0.5 text-xs rounded bg-yellow-900/60 hover:bg-yellow-700 text-yellow-300 whitespace-nowrap">↺ 재시작</button>
            <button onclick="sendCmd('${pc}','daily_dungeon')" class="px-1.5 py-0.5 text-xs rounded bg-purple-900/60 hover:bg-purple-700 text-purple-300 whitespace-nowrap">일일던전</button>
            <button onclick="sendCmd('${pc}','nightmare')" class="px-1.5 py-0.5 text-xs rounded bg-pink-900/60 hover:bg-pink-700 text-pink-300 whitespace-nowrap">악몽</button>
            <button onclick="sendCmd('${pc}','abyss')" class="px-1.5 py-0.5 text-xs rounded bg-blue-900/60 hover:bg-blue-700 text-blue-300 whitespace-nowrap">어비스</button>
            <button onclick="sendCmd('${pc}','collect_info')" class="px-1.5 py-0.5 text-xs rounded bg-sky-900/60 hover:bg-sky-700 text-sky-300 whitespace-nowrap">정보수집</button>
            <button onclick="sellAllCard('${pc}')" class="px-1.5 py-0.5 text-xs rounded bg-yellow-900/60 hover:bg-yellow-700 text-yellow-300 whitespace-nowrap">판매</button>
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
      <th class="px-3 py-1 text-center">물약</th>
      <th class="px-3 py-1 text-center">귀환</th>
      <th class="px-3 py-1">정기추출</th>
      <th class="px-3 py-1 text-center">아르카나</th>
      <th class="px-3 py-1 text-center">장비</th>
      <th class="px-3 py-1 text-right">각인키나</th>
      <th class="px-3 py-1 text-right">거래키나</th>
      <th class="px-3 py-1 text-right">창고키나</th>
      <th class="px-3 py-1 text-center">어비스</th>
      <th class="px-3 py-1 text-right">어비스P</th>
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
  // ★캐릭터 데이터 항상 리로드(2026-07-21 사용자: "각성전 완료돼도 갯수/뱃지 갱신 안 됨") —
  //   전광판 각성전 수치·⚔뱃지가 charTableData 기반인데 기존엔 스프레드 열려있을 때만
  //   리로드해서 새로고침 전까지 안 바뀜. loadCharTable이 내부에서 renderCards까지 해줌.★
  loadCharTable();
  showToast(`✓ ${msg.pc_id} 정보수집 완료`);
}

// ─── 초기화 ──────────────────────────────────────────────────────────────────
(async()=>{
  const res=await fetch('/status');
  if(res.ok)(await res.json()).pcs?.forEach(p=>{state[p.pc_id]=p;});
  renderCards(); loadCmdHistory(); loadCharTable(); connectWS(); loadSalePrice();
  setInterval(renderCards,60000);
  setInterval(loadCharTable,120000);   // 각성티켓/뱃지 폴백 갱신(WS char_info 놓쳐도 2분 내 반영)
  checkServerBoot(); setInterval(checkServerBoot,5000);   // 서버 재시작 감지 → 자동 새로고침
  // 탭이 백그라운드면 배경 이펙트(별밭/오로라/혜성) 애니메이션 정지 — GPU 낭비 방지
  document.addEventListener('visibilitychange',()=>{document.documentElement.classList.toggle('fx-off',document.hidden);});
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
    # ★no-store: 대시보드 HTML을 브라우저가 캐시해 옛 버전(옛 컬럼/JS)을 보여주던 문제 방지.
    #   배포 때마다 새 대시보드가 바로 뜨게 함(컬럼 어긋남 등 stale 렌더 방지).
    return HTMLResponse(HTML_DASHBOARD, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache", "Expires": "0",
    })


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
    logs = await get_logs(pc_id, limit=2000)
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
    # 매크로 WS 연결되어 있으면 즉시 전달
    ws_sent = await send_command_to_macro(pc_id, command, args, cmd_id)
    # 브로드캐스트 (명령 내역 갱신용)
    cmds = await get_recent_commands(20)
    await manager.broadcast({"type": "cmd_history", "commands": cmds})
    return JSONResponse({"ok": True, "id": cmd_id, "ws": ws_sent})


@app.delete("/status/{pc_id}")
async def remove_pc(pc_id: str, request: Request):
    if not check_session(request):
        raise HTTPException(status_code=401)
    await delete_pc_all_data(pc_id)
    await push_state()
    return JSONResponse({"ok": True})


@app.websocket("/ws/macro/{pc_id}")
async def macro_websocket(websocket: WebSocket, pc_id: str):
    """매크로 클라이언트 WebSocket — 상태 수신 + 명령 송신"""
    # API 키 인증 (쿼리 파라미터)
    api_key = websocket.query_params.get("key", "")
    if api_key != API_KEY:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    macro_ws_connections[pc_id] = websocket
    try:
        # 대기 중인 명령 즉시 전달
        pending = await get_pending_command(pc_id)
        if pending:
            await websocket.send_text(json.dumps({
                "type": "command", "id": pending["id"],
                "command": pending["command"], "args": pending.get("args", {})
            }))
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            msg_type = msg.get("type", "")
            if msg_type == "status":
                payload = msg.get("payload", {})
                payload["pc_id"] = pc_id
                await upsert_status(pc_id, payload)
                errors = payload.get("errors") or []
                for e in errors[:3]:
                    await insert_log(pc_id, "warn", str(e))
                await push_state()
            elif msg_type == "log":
                logs = msg.get("logs", [])
                for entry in logs:
                    await insert_log(pc_id, entry.get("level", "info"), entry.get("message", ""))
            elif msg_type == "ack":
                cmd_id = msg.get("command_id")
                if cmd_id:
                    await ack_command(cmd_id)
            elif msg_type == "pong":
                pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        macro_ws_connections.pop(pc_id, None)


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
    merge = bool(data.get("merge", False))   # 단일 캐릭 수집: slot 기준 병합(나머지 보존)
    merged = await upsert_char_info(pc_id, total_kina, chars, merge=merge)
    # 병합 시 최종 total_kina를 다시 읽어 브로드캐스트(0으로 보냈으면 기존값 유지됐으므로)
    final_kina = total_kina
    if merge:
        info = await get_char_info(pc_id)
        if info:
            final_kina = info.get("total_kina", total_kina)
    await manager.broadcast({"type": "char_info", "pc_id": pc_id,
                              "total_kina": final_kina, "chars": merged,
                              "collected_at": data.get("collected_at", "")})
    return JSONResponse({"ok": True})


@app.post("/slot_filter/{pc_id}")
async def set_slot_filter(pc_id: str, request: Request):
    """대시보드 → 슬롯 활성화/비활성화 저장 + 매크로에 명령 전달"""
    if not check_session(request):
        raise HTTPException(status_code=401)
    body = await request.json()
    filters = body.get("filters", {})
    # int 키로 정규화
    filters = {int(k): bool(v) for k, v in filters.items()}
    await upsert_slot_filters(pc_id, filters)
    # 매크로에 set_slot_filter 명령 전달
    cmd_id = await insert_command(pc_id, "set_slot_filter", {"filters": filters})
    await send_command_to_macro(pc_id, cmd_id, "set_slot_filter", {"filters": filters})
    await push_state()
    return {"ok": True}


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
# 악몽 진행 상태
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/nightmare/progress/{pc_id}")
async def save_nightmare_progress(pc_id: str, request: Request):
    """매크로가 악몽 진행 상태 전송"""
    if not check_api_key(request):
        raise HTTPException(status_code=403)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    slot = data.get("slot", 1)
    tab = data.get("tab", "몽충I")
    bosses = data.get("bosses", {})
    await upsert_nightmare_progress(pc_id, slot, tab, bosses)
    await manager.broadcast({"type": "nightmare_progress", "pc_id": pc_id,
                              "slot": slot, "tab": tab, "bosses": bosses})
    return JSONResponse({"ok": True})


@app.get("/nightmare/progress/{pc_id}")
async def query_nightmare_progress(pc_id: str, request: Request):
    """대시보드가 악몽 진행 상태 조회"""
    if not check_session(request):
        raise HTTPException(status_code=401)
    progress = await get_nightmare_progress(pc_id)
    return JSONResponse({"pc_id": pc_id, "slots": progress})


# ─────────────────────────────────────────────────────────────────────────────
# 스크린샷 업로드/조회 (아르카나, 장비 등)
# ─────────────────────────────────────────────────────────────────────────────
import base64 as _b64

@app.post("/screenshot/{category}/{pc_id}/{slot}")
async def upload_screenshot(category: str, pc_id: str, slot: int, request: Request):
    """매크로가 스크린샷 업로드 (arcana, equip 등)"""
    if not check_api_key(request):
        raise HTTPException(status_code=403)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400)
    img_b64 = data.get("image", "")
    if not img_b64:
        raise HTTPException(status_code=400, detail="No image")
    img_bytes = _b64.b64decode(img_b64)
    cat_dir = os.path.join(SCREENSHOTS_DIR, category)
    os.makedirs(cat_dir, exist_ok=True)
    fpath = os.path.join(cat_dir, f"{pc_id}_s{slot}.png")
    with open(fpath, "wb") as f:
        f.write(img_bytes)
    return JSONResponse({"ok": True, "path": f"/screenshot/{category}/{pc_id}/{slot}"})


@app.get("/screenshot/{category}/{pc_id}/{slot}")
async def get_screenshot(category: str, pc_id: str, slot: int, request: Request):
    """대시보드가 스크린샷 조회"""
    if not check_session(request):
        raise HTTPException(status_code=401)
    fpath = os.path.join(SCREENSHOTS_DIR, category, f"{pc_id}_s{slot}.png")
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(fpath, media_type="image/png")


# ─────────────────────────────────────────────────────────────────────────────
# 전체 캐릭터 테이블 API
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/characters")
async def get_all_characters(request: Request):
    """전체 PC의 모든 캐릭터 정보를 플랫 테이블로 반환"""
    if not check_session(request):
        raise HTTPException(status_code=401)
    all_info = await get_all_char_info()
    # 악몽 진행 상태도 같이 조회
    all_nm = await get_all_nightmare_progress()
    nm_map = {}  # (pc_id, slot) → nightmare summary
    for nm in all_nm:
        bosses = nm.get("bosses", {})
        cleared = sum(1 for b in bosses.values() if b.get("cleared"))
        total = len(bosses) if bosses else 7
        best_stage = max((b.get("stage", 0) for b in bosses.values()), default=0) if bosses else 0
        nm_map[(nm["pc_id"], nm["slot"])] = f"{nm.get('tab','몽충I')} {cleared}/{total}" if bosses else ""
    rows = []
    for info in all_info:
        pc_id = info["pc_id"]
        total_kina = info.get("total_kina", 0)
        collected_at = info.get("collected_at", "")
        for ch in info.get("chars", []):
            slot = ch.get("slot", 0)
            rows.append({
                "pc_id": pc_id,
                "slot": slot,
                "name": ch.get("name", ""),
                "char_class": ch.get("char_class", ""),
                "gear_power": ch.get("gear_power", 0),
                "power_power": ch.get("power_power", 0),
                "odd_energy": ch.get("odd_energy", ""),
                "daily_ticket": ch.get("daily_ticket", ""),
                "nightmare_ticket": ch.get("nightmare_ticket", 0),
                "awakening_ticket": ch.get("awakening_ticket", 0),
                "sanctuary": ch.get("sanctuary", ""),
                "mail_count": ch.get("mail_count", 0),
                "extract_level": ch.get("extract_level", ""),
                "potion_count": ch.get("potion_count"),
                "return_scroll_count": ch.get("return_scroll_count"),
                "abyss_time": ch.get("abyss_time", ""),
                "abyss_point": ch.get("abyss_point", 0),
                "arcana_image": ch.get("arcana_image", False),
                "equip_image": ch.get("equip_image", False),
                "gakin_kina": ch.get("gakin_kina", 0),
                "trade_kina": ch.get("trade_kina", 0),
                "total_kina": total_kina,
                "collected_at": collected_at,
                "nightmare_progress": nm_map.get((pc_id, slot), ""),
            })
    return JSONResponse({"characters": rows})


# ─────────────────────────────────────────────────────────────────────────────
# 업데이터 버전 체크 (POST /check)
# ─────────────────────────────────────────────────────────────────────────────
import urllib.parse as _urlparse

# GitHub raw URL 베이스
_GH_RAW = "https://raw.githubusercontent.com/kevincom-honjong/aion2-macro-releases/main"
# jsDelivr CDN 베이스 (raw rate limit(429) 우회용 — 작은 파일=이미지 배포에 사용)
# raw는 IP당 요청수 제한이 빡세서 13PC×이미지150개 동시 다운로드 시 429남.
# jsDelivr는 GitHub 미러 CDN이라 rate limit 사실상 없음. (exe는 용량때문에 jsDelivr 불가 → Releases)
_GH_CDN = "https://cdn.jsdelivr.net/gh/kevincom-honjong/aion2-macro-releases@main"

_version_cache = {"data": {}, "ts": 0}

def _load_version_json() -> dict:
    """version.json 로드 — GitHub raw에서 가져옴 (5분 캐시)"""
    import time as _time
    now = _time.time()
    if _version_cache["data"] and now - _version_cache["ts"] < 300:
        return _version_cache["data"]
    # 1차: 로컬 파일
    for vpath in [
        os.path.join(os.path.dirname(__file__), "version.json"),
        "/app/version.json",
    ]:
        try:
            with open(vpath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get("exe", {}).get("version"):
                    _version_cache["data"] = data
                    _version_cache["ts"] = now
                    return data
        except Exception:
            pass
    # 2차: GitHub raw
    try:
        import requests as _req
        r = _req.get("https://raw.githubusercontent.com/kevincom-honjong/aion2-macro-releases/main/server/version.json",
                     timeout=10)
        if r.status_code == 200:
            data = r.json()
            _version_cache["data"] = data
            _version_cache["ts"] = now
            return data
    except Exception:
        pass
    return _version_cache.get("data", {})

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
            # exe(71MB)는 GitHub Releases(CDN)에서 배포 — raw 429 우회. jsDelivr는 용량초과라 불가.
            # 규칙: 릴리스 태그 v<버전>, 에셋 이름 macro-<버전>.exe (릴리스 미리 만들어둬야 함)
            "download_url": f"https://github.com/kevincom-honjong/aion2-macro-releases/releases/download/v{server_exe_ver}/macro-{server_exe_ver}.exe",
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
                "download_url": f"{_GH_CDN}/images2/{_urlparse.quote(fname)}",
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
    print(f"혼종 사령부: http://localhost:{port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
# Tue Apr  7 08:52:44     2026
