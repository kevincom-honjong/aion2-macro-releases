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
import os, json, uuid, re, io, zipfile, time, hashlib, hmac, base64, asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, StreamingResponse

from database import (
    init_db, upsert_status, get_all_statuses, get_status, delete_status,
    delete_pc_all_data, get_death_counts_since, get_all_death_events,
    insert_command, get_pending_command, ack_command, cancel_command, get_logs,
    insert_log, get_recent_commands, get_command_pc, get_updater_command_pc,
    set_setting, get_setting,
    upsert_updater_status, get_all_updater_statuses,
    insert_updater_command, get_pending_updater_command, ack_updater_command,
    recent_updater_commands,
    upsert_char_info, get_char_info, get_all_char_info,
    upsert_nightmare_progress, get_nightmare_progress, get_all_nightmare_progress,
    upsert_slot_filters, get_slot_filters, get_all_slot_filters,
    tg_map_put, tg_map_get, tg_map_recent, tg_map_delete_pc,
)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")
API_KEY            = os.getenv("API_KEY", "macro_key_change_me")
SESSION_TTL        = timedelta(days=7)

# ─── 테넌트(2026-07-25): 비밀번호별 독립 대시보드 ────────────────────────────
# 사용자 설계: "특정 비밀번호로 로그인하면 그 비밀번호 전용 대시보드가 나오게".
#   - 로그인 비밀번호 → 테넌트 결정(세션에 탑재) / 매크로·업데이터 X-Api-Key → 테넌트 결정
#   - 내부 저장 키는 "테넌트::PC-ID" 네임스페이스, 출력 시 벗겨서 노출. DB 스키마 무변경.
#   - "main" = 기존 DASHBOARD_PASSWORD/API_KEY, 접두사 없이 저장 → 기존 함대·데이터 완전 호환.
# 테넌트 추가: Railway env TENANTS_JSON = {"friend1": {"password": "지인비번", "api_key": "지인매크로키"}}
def _load_tenants() -> dict:
    t = {"main": {"password": DASHBOARD_PASSWORD, "api_key": API_KEY, "expires": "",
                  "chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip()}}
    try:
        extra = json.loads(os.getenv("TENANTS_JSON", "") or "{}")
        for name, v in extra.items():
            name = str(name)
            if not isinstance(v, dict) or name == "main" or NS_SEP in name:
                continue
            t[name] = {"password": str(v.get("password", "")),
                       "api_key": str(v.get("api_key", "")),
                       # 기간제(2026-07-26): "expires": "2026-08-31" (KST 그날 23:59까지 유효, 빈값=무기한)
                       "expires": str(v.get("expires", "")),
                       # 텔레그램 단일봇 중계(2026-07-28): 지인마다 봇을 새로 파지 않고
                       # 봇 1개 + 각자의 chat_id로 분리한다. 빈값이면 그 테넌트는 텔레그램 미사용.
                       "chat_id": str(v.get("chat_id", "")).strip()}
    except Exception as e:
        print(f"[TENANTS] TENANTS_JSON 파싱 실패(무시): {e}")
    return t


def tenant_expired(tenant: str) -> bool:
    """기간제 만료 여부 — expires(YYYY-MM-DD, KST 기준 당일 23:59까지 유효). main/빈값은 무기한."""
    exp = (TENANTS.get(tenant) or {}).get("expires") or ""
    if not exp:
        return False
    try:
        # KST(UTC+9) 자정 경계: 만료일 다음날 00:00 KST = 만료일 15:00 UTC
        end = datetime.strptime(exp, "%Y-%m-%d") + timedelta(hours=15)
        return datetime.now(timezone.utc).replace(tzinfo=None) >= end
    except Exception:
        return False   # 형식 오류 시 잠그지 않음 (오타로 지인 전체 정지 방지)


# ─── 렌탈 킬스위치 (2026-08-06) — 기간제 대신 스위치로 즉시 차단 ─────────────
# 저장: main 테넌트 설정 "rental_kill" (테넌트명 콤마/공백 구분 나열, 빈값=전원 해제).
# 반영: lifespan 로드 + POST /setting/rental_kill 즉시 갱신. 볼륨 DB라 재배포에도 유지.
# 온오프(main 세션): POST /setting/rental_kill {"value":"친구A"} / 해제 {"value":""}
KILLED_TENANTS: set = set()
# 차단 테넌트의 '정지 안내' 텔레그램 예외 사용량 (테넌트 → {n, since}) — 시간당 3건 상한
_KILL_TG: dict = {}


def _parse_killed(raw: str) -> set:
    """"친구A, 친구B" → {"친구A","친구B"}. ★콤마로만 구분★ — 테넌트명에 공백이 있어도 통짜로
    취급(리뷰 2026-08-06: split()이 "홍 길동"을 쪼개 영구 킬 불가였음). main은 어떤 입력이
    와도 킬 불가(자기 잠금 방지). 미등록 이름은 무해(아무와도 일치 안 함) — POST 응답이 에코."""
    return {t.strip() for t in (raw or "").split(",")
            if t.strip() and t.strip() != "main"}


def tenant_blocked(tenant: str) -> bool:
    """만료(기간제) 또는 킬스위치 — 세션·API키·WS·로그인 모든 게이트가 이걸 본다."""
    return tenant_expired(tenant) or (tenant in KILLED_TENANTS)


NS_SEP = "::"
TENANTS: dict = {}
PW_TO_TENANT: dict = {}
KEY_TO_TENANT: dict = {}
CHAT_TO_TENANT: dict = {}


def _init_tenants():
    global TENANTS, PW_TO_TENANT, KEY_TO_TENANT, CHAT_TO_TENANT
    TENANTS = _load_tenants()
    PW_TO_TENANT, KEY_TO_TENANT, CHAT_TO_TENANT = {}, {}, {}
    for name, v in TENANTS.items():
        if v.get("password") and v["password"] not in PW_TO_TENANT:
            PW_TO_TENANT[v["password"]] = name
        if v.get("api_key") and v["api_key"] not in KEY_TO_TENANT:
            KEY_TO_TENANT[v["api_key"]] = name
        if v.get("chat_id") and v["chat_id"] not in CHAT_TO_TENANT:
            CHAT_TO_TENANT[v["chat_id"]] = name


def tenant_chat_id(tenant: str) -> str:
    return (TENANTS.get(tenant) or {}).get("chat_id") or ""


_init_tenants()


_PC_ID_SAFE = re.compile(r"[^A-Za-z0-9가-힣._\- ]")


def clean_pc_id(pc_id: str) -> str:
    """★pc_id 화이트리스트 소독(2026-07-27 보안감사).
    pc_id는 화면 텍스트뿐 아니라 onclick="fn('${pc_id}')" 속성과 버그스샷 파일명에까지 들어간다.
    따옴표·꺾쇠·경로문자가 통과하면 XSS와 경로 순회가 동시에 열린다 → 안전 문자만 허용."""
    return _PC_ID_SAFE.sub("_", (pc_id or ""))[:64]


def ns(tenant: str, pc_id: str) -> str:
    """테넌트 네임스페이스 키. main은 접두사 없음(기존 데이터 호환). pc_id는 화이트리스트 소독."""
    pc_id = clean_pc_id(pc_id).replace(NS_SEP, "_")
    return pc_id if tenant == "main" else f"{tenant}{NS_SEP}{pc_id}"


def split_ns(key: str) -> "tuple[str, str]":
    """저장 키 → (테넌트, 원래 pc_id). 접두사 없으면 main."""
    if key and NS_SEP in key:
        t, p = key.split(NS_SEP, 1)
        return t, p
    return "main", (key or "")


def ns_of(key: str) -> str:
    return split_ns(key)[0]

# 프로세스 시작마다 고유 — 대시보드가 /ping으로 폴링해 값이 바뀌면 "서버 재시작"으로 보고 자동 새로고침.
SERVER_BOOT_ID     = uuid.uuid4().hex
SERVER_BOOT_TS     = time.time()   # /health 업타임 계산용 (자발 재시작 원인 추적, 2026-07-25)
# ★★어느 '코드'가 떠 있는지 (2026-08-18)★★ — boot/uptime 으로는 알 수 없다.
#   실사고: 서버 커밋 2개(스프레드 헤더·계정전환 통합)를 푸시하고 재배포를 기다렸는데
#   Railway 가 안 받았다. uptime 은 계속 흘러서 '살아 있음' 으로만 보였고, 결국
#   ★서빙되는 HTML 에서 함수 이름을 grep★ 해서야 옛 빌드인 걸 알았다.
#   → 자기 소스의 sha256 앞 8자리를 내보낸다. 로컬에서 같은 값을 계산해 대조하면
#     "내 코드가 떠 있나?" 가 curl 한 방으로 끝난다. 수동 관리가 필요 없다(자동 계산).
#   로컬 대조:  python -c "import hashlib;print(hashlib.sha256(open(r'server/main.py','rb').read()).hexdigest()[:8])"
try:
    with open(os.path.abspath(__file__), "rb") as _cf:
        SERVER_CODE_ID = hashlib.sha256(_cf.read()).hexdigest()[:8]
except Exception:
    SERVER_CODE_ID = "unknown"
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

# ─── 음성 알림(TTS) 합성 설정 ────────────────────────────────────────────────
# 브라우저 내장 음성이 기계음이라 서버에서 신경망 음성을 만들어 내려준다(/tts 참조).
# 기본 톤은 낮고 느리게 — 사용자가 고른 값. 대시보드 ⚙ 슬라이더로 그때그때 덮어쓴다.
TTS_DIR   = os.getenv("TTS_DIR", "/data/tts")
TTS_VOICE = os.getenv("TTS_VOICE", "ko-KR-SunHiNeural")   # edge-tts 한국어 여성 신경망
TTS_RATE  = os.getenv("TTS_RATE", "+25%")    # 빠르게 — 텀이 길면 답답하다는 사용자 지적
TTS_PITCH = os.getenv("TTS_PITCH", "+18Hz")  # 귀엽되 콧소리 안 나는 선
os.makedirs(TTS_DIR, exist_ok=True)

# ─── 텔레그램 단일 봇 중계(2026-07-28) ──────────────────────────────────────
# 왜: BotFather는 계정당 봇 20개 제한인데 PC마다 봇을 팠다. 근본 원인은 getUpdates가
#     '먼저 가져간 쪽만' 메시지를 받는 배타 소비라, 19대가 한 봇을 폴링하면 PC-03이
#     요청한 코드를 PC-11이 낚아채기 때문. → 서버 한 곳만 폴링하고, 받은 코드를
#     이미 있는 명령 큐(captcha_code)로 해당 PC에 꽂아준다. 봇은 영원히 1개면 된다.
# ★토큰은 Railway 환경변수로만 둔다(코드·저장소 금지 — 2026-07-27 보안감사).★
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_REPLY_WINDOW      = int(os.getenv("TELEGRAM_REPLY_WINDOW", "1800"))  # 답장 없이 코드만 왔을 때 추론 유효시간(초)
TG_CODE_RE           = re.compile(r"^[A-Za-z0-9]{3,16}$")
TG_API               = "https://api.telegram.org/bot"
# 폴러 리스: 인스턴스가 둘 이상 떠도 한 놈만 getUpdates를 잡게(중복 소비 시 메시지 유실)
TG_LEASE_KEY         = "tg_poller_lease"
TG_LEASE_TTL         = 90     # 초 — 소유자가 이 시간 갱신 없으면 다른 인스턴스가 인수
TG_OFFSET_KEY        = "tg_offset"

# ─────────────────────────────────────────────────────────────────────────────
# Session (stateless HMAC 서명 토큰 — 서버 재시작에도 유지됨, 인메모리 저장 X)
#   토큰 형식: base64url(만료ts) "." base64url(HMAC-SHA256(secret, 만료ts))
#   재배포 후에도 SESSION_SECRET이 동일해 쿠키가 계속 유효 → 재로그인 불필요.
# ─────────────────────────────────────────────────────────────────────────────
def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_session(tenant: str = "main") -> str:
    exp = int((datetime.now(timezone.utc) + SESSION_TTL).timestamp())
    payload = f"{exp}:{tenant}".encode()   # 테넌트를 서명 페이로드에 탑재(위조 불가)
    sig = hmac.new(SESSION_SECRET, payload, hashlib.sha256).digest()
    return f"{_b64u(payload)}.{_b64u(sig)}"


def valid_session(token: Optional[str]) -> Optional[str]:
    """유효하면 테넌트명("main" 등) 반환, 아니면 None. 구버전 토큰(만료ts만)은 main으로 인정."""
    if not token or "." not in token:
        return None
    try:
        p_b64, s_b64 = token.split(".", 1)
        payload = _b64u_dec(p_b64)
        sig = _b64u_dec(s_b64)
    except Exception:
        return None
    expected = hmac.new(SESSION_SECRET, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        text = payload.decode()
        if ":" in text:
            exp_s, tenant = text.split(":", 1)
        else:
            exp_s, tenant = text, "main"
        exp = int(exp_s)
    except Exception:
        return None
    if datetime.now(timezone.utc).timestamp() > exp:
        return None
    return tenant if tenant in TENANTS else None


def check_session(request: Request) -> Optional[str]:
    """유효 세션이면 테넌트명 반환(truthy), 아니면 None — 기존 `if not check_session(...)` 호환.
    기간제 만료 테넌트는 기존 세션도 즉시 무효(2026-07-26)."""
    tenant = valid_session(request.cookies.get("session"))
    if tenant and tenant_blocked(tenant):
        return None
    return tenant


_KEY_FAILS: dict = {}          # ip -> {"n": int, "since": float}
KEY_MAX_FAILS = 30             # 창 안 API키 실패 상한 (매크로는 정상이면 거의 실패하지 않음)
KEY_WINDOW = 300.0


def _key_probe_blocked(ip: str) -> bool:
    """API키 추측 시도 차단 여부 — 헤더/바디/WS 어디로 오든 같은 카운터를 공유한다."""
    rec = _KEY_FAILS.get(ip)
    return bool(rec and time.time() - rec["since"] <= KEY_WINDOW and rec["n"] >= KEY_MAX_FAILS)


def _key_probe_failed(ip: str):
    now = time.time()
    rec = _KEY_FAILS.get(ip)
    if not rec or now - rec["since"] > KEY_WINDOW:
        rec = {"n": 0, "since": now}
        _KEY_FAILS[ip] = rec
    rec["n"] += 1
    if rec["n"] == KEY_MAX_FAILS:
        print(f"[SECURITY] API키 추측 누적 → {ip} {int(KEY_WINDOW)}초 차단")
    if len(_KEY_FAILS) > 5000:
        for kk in [kk for kk, vv in _KEY_FAILS.items() if now - vv["since"] > KEY_WINDOW][:2000]:
            _KEY_FAILS.pop(kk, None)


def check_api_key(request: Request) -> Optional[str]:
    """키가 유효하면 소속 테넌트명 반환(truthy), 아니면 None. 만료 테넌트 키는 전면 차단.
    ★키 추측 시도 완화(2026-07-27): 상수시간 비교 + IP별 실패 상한. 이 키가 뚫리면
      남의 PC에 원격명령을 넣을 수 있으므로 로그인만큼 중요하다.★"""
    supplied = request.headers.get("X-Api-Key", "")
    ip = _client_ip(request)
    now = time.time()
    rec = _KEY_FAILS.get(ip)
    if rec and now - rec["since"] <= KEY_WINDOW and rec["n"] >= KEY_MAX_FAILS:
        return None            # 실패 폭주 IP는 정답 키여도 창이 끝날 때까지 거부
    tenant = None
    for k, tn in KEY_TO_TENANT.items():
        if hmac.compare_digest(supplied, k):
            tenant = tn
    if tenant and tenant_blocked(tenant):
        # ★킬스위치/만료(2026-08-06): 유효한 키의 '차단'은 추측 실패가 아니다 — 여기서
        #   카운터를 올리면 차단된 PC의 하트비트가 자기 IP를 잠가 /license 확인까지 429가
        #   되고, 클라가 '중지'를 network로 오분류해 48시간 유예를 탄다(즉시 정지 실패).★
        return None
    if not tenant:
        if not rec or now - rec["since"] > KEY_WINDOW:
            rec = {"n": 0, "since": now}
            _KEY_FAILS[ip] = rec
        rec["n"] += 1
        if rec["n"] == KEY_MAX_FAILS:
            print(f"[SECURITY] API키 실패 누적 → {ip} {int(KEY_WINDOW)}초 차단")
        if len(_KEY_FAILS) > 5000:
            for kk in [kk for kk, vv in _KEY_FAILS.items() if now - vv["since"] > KEY_WINDOW][:2000]:
                _KEY_FAILS.pop(kk, None)
        return None
    _KEY_FAILS.pop(ip, None)
    return tenant


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket manager
# ─────────────────────────────────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list["tuple[WebSocket, str]"] = []   # (대시보드 ws, 테넌트)

    async def connect(self, ws: WebSocket, tenant: str = "main"):
        await ws.accept()
        self.active.append((ws, tenant))

    def disconnect(self, ws: WebSocket):
        self.active = [(c, t) for c, t in self.active if c is not ws]

    async def broadcast(self, data: dict, tenant: str = "main"):
        """해당 테넌트의 대시보드에게만 전송 (테넌트 격리).
        ★차단(킬/만료) 재검사(2026-08-06 감사 major)★ — 예전엔 핸드셰이크 때 한 번만 봐서,
        탭을 열어둔 채 킬을 당하면 HTTP는 401인데 WS로는 실시간 데이터가 계속 흘렀다.
        전송 직전마다 확인하고, 차단이면 그 소켓을 닫는다(다음 재접속은 1008로 막힌다)."""
        if tenant_blocked(tenant):
            for ws, t in list(self.active):
                if t == tenant:
                    self.disconnect(ws)
                    try:
                        await ws.close(code=1008)
                    except Exception:
                        pass
            return
        msg = json.dumps(data, ensure_ascii=False)
        dead = []
        for ws, t in self.active:
            if t != tenant:
                continue
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()

# 매크로 WebSocket 연결 관리
macro_ws_connections: dict[str, WebSocket] = {}   # pc_id → WebSocket

# ★★★'살아있다는 증거' 는 상태보고 하나가 아니다 (2026-08-21 실사고)★★★
#   주인님: "사냥 잘하는데 왜 대시보드에는 오프라인 떠잇는경우는 뭐야?"
#           "지금 사냥돌아가고잇는데 오프라인 뜬다니까? 그걸 잘받게 만들던가"
#
#   ★무슨 일이었나★ 매크로의 상태 push 는 ★해시가 바뀔 때만★ 일어나고,
#   하트비트는 `if not _ws_connected:` 라 ★WS 가 붙었다고 믿으면 아무것도 안 보냈다.★
#   그런데 WS 가 ★반쯤 죽으면★(서버는 끊긴 걸로 보는데 클라는 모름) 그 둘이 겹쳐
#   서버는 몇 분씩 아무것도 못 받는다 → last_active 가 늙어 offline 으로 칠해진다.
#   실측 03:0x — PC-09b·10b·12b·20b 가 사냥 중인데 last_active 199~257초.
#
#   ★고친 방향★ 매크로만 고치면 ★업데이트를 받은 PC만★ 낫는다. 서버가
#   ★어떤 종류든 그 PC에서 온 요청★ 을 생존 신호로 받으면 전 버전이 즉시 낫는다.
#   로그 전송·명령 폴링·ack·리포트 — 전부 그 PC 프로세스가 살아있다는 증거다.
_last_seen: dict[str, datetime] = {}          # nspc → 마지막으로 그 PC에서 뭐라도 온 시각


def mark_seen(nspc: str) -> None:
    """그 PC에서 어떤 요청이든 왔다 = 살아있다. 오프라인 판정에 쓴다."""
    try:
        _last_seen[nspc] = datetime.now(timezone.utc)
        if len(_last_seen) > 4000:            # 무한 증식 방지(테넌트 오염 대비)
            _last_seen.clear()
    except Exception:
        pass


_lan_cache_last: dict[str, str] = {}   # nspc → 마지막으로 받은 내부망 주소


def seen_fresh(nspc: str, secs: int) -> bool:
    t = _last_seen.get(nspc)
    if not t:
        return False
    return (datetime.now(timezone.utc) - t).total_seconds() < secs


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
# 텔레그램 단일 봇 중계 — 서버가 유일한 폴러
#
#   ① 매크로가 POST /telegram/photo/{pc} (캡차 스샷, expect_reply=1)
#   ② 서버가 sendPhoto → 돌아온 message_id를 telegram_map에 (message_id → 테넌트::PC) 저장
#   ③ 사용자가 그 메시지에 '답장'으로 코드 입력
#   ④ 서버가 reply_to_message.message_id로 PC를 특정 → 명령 큐에 captcha_code 투입
#   ⑤ 매크로가 기존 WS/폴링으로 수신 → recovery.apply_captcha_code()
#
#   ★보내는 쪽까지 서버가 맡아야 ②의 매핑이 성립한다. 매크로가 직접 보내면
#     서버는 message_id를 모르고, 답장은 어느 PC 것인지 영원히 알 수 없다.★
# ─────────────────────────────────────────────────────────────────────────────
def tg_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)


async def _tg_call(method: str, data: dict | None = None,
                   files: dict | None = None, timeout: float = 20.0) -> dict | None:
    """Bot API 호출. 실패는 None (알림 실패로 본 기능이 막히면 안 된다)."""
    if not tg_enabled():
        return None
    import httpx
    url = f"{TG_API}{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.post(url, data=data or {}, files=files)
        if r.status_code != 200:
            print(f"[TG] {method} HTTP {r.status_code}: {r.text[:200]}")
            return None
        js = r.json()
        if not js.get("ok"):
            print(f"[TG] {method} not ok: {str(js)[:200]}")
            return None
        return js.get("result")
    except Exception as e:
        print(f"[TG] {method} 예외: {e.__class__.__name__}: {e}")
        return None


async def tg_send_text(chat_id: str, text: str) -> int | None:
    res = await _tg_call("sendMessage", {"chat_id": chat_id, "text": text[:3500]})
    return (res or {}).get("message_id")


async def tg_send_photo(chat_id: str, caption: str, photo: bytes, filename: str = "shot.png") -> int | None:
    res = await _tg_call("sendPhoto", {"chat_id": chat_id, "caption": caption[:900]},
                         files={"photo": (filename, photo, "image/png")}, timeout=40.0)
    return (res or {}).get("message_id")


async def _tg_route_code(nspc: str, code: str) -> bool:
    """명령 큐에 captcha_code 투입 + WS 연결돼 있으면 즉시 전달."""
    tenant = ns_of(nspc)
    cmd_id = await insert_command(nspc, "captcha_code", {"code": code})
    await send_command_to_macro(nspc, "captcha_code", {"code": code}, cmd_id)
    try:
        await _push_cmd_history(tenant)
    except Exception:
        pass
    return True


async def _tg_handle_update(u: dict) -> None:
    msg = u.get("message") or u.get("edited_message") or {}
    chat_id = str(((msg.get("chat") or {}).get("id")) or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    # ★모르는 채팅은 통째로 무시 — 봇 주소만 알면 누구나 코드를 꽂을 수 있게 되면 안 된다.
    #   등록 경로: main은 env TELEGRAM_CHAT_ID, 지인은 TENANTS_JSON의 chat_id.★
    tenant = CHAT_TO_TENANT.get(chat_id)
    if not tenant:
        print(f"[TG] 미등록 chat_id 무시: {chat_id}")
        return
    if tenant_blocked(tenant):
        return

    if text.startswith("/"):
        if text.split("@")[0] in ("/start", "/help"):
            await tg_send_text(chat_id,
                               "거탐(캡차) 코드 입력용 봇입니다.\n"
                               "캡차 사진이 올라오면 그 사진에 '답장'으로 코드만 보내주세요.\n"
                               "대기 중인 PC가 하나뿐이면 답장 없이 코드만 보내도 됩니다.")
        return

    # ① 답장이면 그 메시지의 주인 PC로
    target = None
    reply_id = ((msg.get("reply_to_message") or {}).get("message_id"))
    if reply_id:
        row = await tg_map_get(int(reply_id))
        # ★테넌트 교차 차단: 남의 PC 메시지 id를 알아내 답장해도 라우팅되면 안 된다.★
        if row and ns_of(row["pc_id"]) == tenant:
            target = row["pc_id"]

    code_ok = bool(TG_CODE_RE.match(text))
    pending = await tg_map_recent(chat_id, TG_REPLY_WINDOW)

    # ② 답장이 아니어도 대기 중인 'PC'가 하나뿐이면 그리로 (1대만 쓰는 지인용 편의)
    #    ★행 수가 아니라 PC 수로 센다 — 오답 재전송 때 같은 PC의 사진이 여러 장 쌓이는데,
    #      행 수로 세면 그 흔한 경우에 추론이 죽어버린다.★
    waiting_pcs = {p["pc_id"] for p in pending}
    if target is None and code_ok and len(waiting_pcs) == 1:
        target = next(iter(waiting_pcs))

    if target is None:
        if not pending:
            return          # 대기 중인 요청이 없으면 잡담으로 보고 조용히 넘긴다
        names = ", ".join(sorted({split_ns(p)[1] for p in waiting_pcs}))
        await tg_send_text(chat_id, f"어느 PC인지 알 수 없습니다. 캡차 사진에 '답장'으로 보내주세요.\n대기 중: {names}")
        return
    if not code_ok:
        await tg_send_text(chat_id, "코드는 영문/숫자 3~16자만 됩니다. 다시 보내주세요.")
        return

    pc_name = split_ns(target)[1]
    await _tg_route_code(target, text)
    await tg_map_delete_pc(target)      # 처리했으니 후보에서 제거 (다음 코드가 오라우팅되지 않게)
    await tg_send_text(chat_id, f"{pc_name} → 코드 '{text}' 전달했습니다")
    print(f"[TG] 코드 라우팅: {target} ← {text}")


async def _tg_lease_ok() -> bool:
    """폴러 단독 소유권. Railway가 인스턴스를 둘 띄워도 한 놈만 getUpdates를 잡는다
    (동시에 잡으면 텔레그램이 한쪽에만 주므로 메시지가 랜덤하게 사라진다)."""
    now = time.time()
    raw = await get_setting(TG_LEASE_KEY) or ""
    owner, _, ts = raw.partition(":")
    try:
        age = now - float(ts or 0)
    except ValueError:
        age = 1e9
    if owner and owner != SERVER_BOOT_ID and age < TG_LEASE_TTL:
        return False
    await set_setting(TG_LEASE_KEY, f"{SERVER_BOOT_ID}:{now:.0f}")
    return True


async def _tg_poller() -> None:
    if not tg_enabled():
        print("[TG] TELEGRAM_BOT_TOKEN 미설정 → 중계 비활성")
        return
    me = await _tg_call("getMe", timeout=15.0)
    print(f"[TG] 중계 시작 — bot=@{(me or {}).get('username', '?')} "
          f"chats={list(CHAT_TO_TENANT.keys())}")
    offset: int | None = None
    try:
        saved = await get_setting(TG_OFFSET_KEY)
        offset = int(saved) if saved else None
    except Exception:
        offset = None
    while True:
        try:
            if not await _tg_lease_ok():
                await asyncio.sleep(30)
                continue
            if offset is None:
                # 첫 기동: 밀려 있던 옛 메시지를 재생하지 않도록 커서만 끝으로 옮긴다
                res = await _tg_call("getUpdates", {"offset": -1, "timeout": 0}, timeout=15.0) or []
                offset = (res[-1]["update_id"] + 1) if res else 0
                await set_setting(TG_OFFSET_KEY, str(offset))
                continue
            res = await _tg_call("getUpdates",
                                 {"offset": offset, "timeout": 30, "allowed_updates": '["message"]'},
                                 timeout=45.0)
            if res is None:
                await asyncio.sleep(5)
                continue
            for u in res:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
                try:
                    await _tg_handle_update(u)
                except Exception as e:
                    print(f"[TG] update 처리 예외: {e.__class__.__name__}: {e}")
            if res:
                await set_setting(TG_OFFSET_KEY, str(offset))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[TG] 폴러 예외: {e.__class__.__name__}: {e}")
            await asyncio.sleep(10)


# ─────────────────────────────────────────────────────────────────────────────
# 디스크 영속 프로브 (2026-07-28 실사고)
#   증상: 재시작마다 창고키나 합계가 뚝 떨어지고 버그 스샷·로그·악몽진행도가 사라짐.
#   원인: /data 가 Railway 볼륨이 아니라 컨테이너 임시 디스크였다. DB도 스샷도 휘발.
#         살아 있는 매크로는 WS 재연결 때 char_info를 다시 올려 되살아나지만,
#         매크로가 죽은 PC는 되살릴 주체가 없어 그 PC 재산이 합계에서 통째로 빠진다.
#   ★경로만 보고 '/data니까 볼륨이겠지' 하고 판정하면 안 된다(그렇게 짰다가 틀렸다).
#     부팅마다 마커를 남기고, 다음 부팅에서 그 마커가 살아있는지로 '실제로' 확인한다.★
# ─────────────────────────────────────────────────────────────────────────────
VOLUME_DIR = os.path.dirname(os.getenv("DB_PATH", "") or "/data/macro_control.db") or "/data"
_VOL_PROBE = os.path.join(VOLUME_DIR, ".persist_probe.json")
VOLUME_PERSISTED: Optional[bool] = None      # None = 아직 판정 전
VOLUME_PREV: dict = {}


def _probe_volume() -> None:
    global VOLUME_PERSISTED, VOLUME_PREV
    prev = {}
    try:
        with open(_VOL_PROBE, encoding="utf-8") as f:
            prev = json.load(f) or {}
    except Exception:
        prev = {}
    VOLUME_PREV = prev
    VOLUME_PERSISTED = bool(prev.get("boot") and prev["boot"] != SERVER_BOOT_ID)
    try:
        os.makedirs(VOLUME_DIR, exist_ok=True)
        with open(_VOL_PROBE, "w", encoding="utf-8") as f:
            json.dump({"boot": SERVER_BOOT_ID,
                       "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")}, f)
    except Exception as e:
        print(f"[VOL] 프로브 기록 실패: {e}")
    if VOLUME_PERSISTED:
        print(f"[VOL] {VOLUME_DIR} 영속 확인 (이전 boot={prev.get('boot','?')[:8]} @ {prev.get('at')})")
    else:
        print(f"[VOL] ★{VOLUME_DIR} 가 재시작마다 초기화된다★ — Railway 볼륨을 {VOLUME_DIR}에 "
              f"마운트해야 DB·버그스샷·OCR 학습크롭이 살아남는다 "
              f"(첫 부팅이면 다음 재시작 때 확정된다)")


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _probe_volume()
    await init_db()
    # 렌탈 킬스위치 복원(2026-08-06) — 볼륨 DB의 설정을 부팅 시 메모리로
    try:
        KILLED_TENANTS.update(_parse_killed(await get_setting(ns("main", "rental_kill")) or ""))
        if KILLED_TENANTS:
            print(f"[KILL] 렌탈 킬스위치 복원: {sorted(KILLED_TENANTS)}")
    except Exception as e:
        print(f"[KILL] rental_kill 로드 실패(무시): {e}")
    await _corridor_restore()          # 회랑 진행 스냅샷 복원(2026-08-07)
    tg_task = asyncio.create_task(_tg_poller()) if tg_enabled() else None
    # ★계정 자동순환 엔진 (2026-08-20)★ — 무장된 PC 가 하나도 없으면 아무 일도 안 한다.
    rot_task = asyncio.create_task(_rot_engine())
    try:
        yield
    finally:
        for _t in (tg_task, rot_task):
            if _t:
                _t.cancel()
                try:
                    await _t
                except (asyncio.CancelledError, Exception):
                    pass


app = FastAPI(lifespan=lifespan, title="혼종 사령부 — AION2 관제")


# ─────────────────────────────────────────────────────────────────────────────
# Helper: broadcast current state to all WS clients
# ─────────────────────────────────────────────────────────────────────────────
OFFLINE_TIMEOUT = timedelta(seconds=90)

def _is_stale(updated_at_str: str | None) -> bool:
    """updated_at 타임스탬프가 ★90초★(OFFLINE_TIMEOUT) 이상 지났으면 True.

    ★주석이 거짓말하고 있었다 (2026-08-20 감사)★ — 여기와 아래 base 카드 분기가
    둘 다 '30초' 라고 적혀 있었지만 상수는 90초다. 업데이터 STATUS_INTERVAL 이
    30초이므로 실제 기준은 ★연속 3회 미보고★ 다. 이 숫자로 타이밍을 계산하면 틀린다.
    """
    if not updated_at_str:
        return True
    try:
        ts = datetime.fromisoformat(updated_at_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ts > OFFLINE_TIMEOUT
    except Exception:
        return True

def _fresh(ts_str: str | None, secs: int) -> bool:
    """pc_status 보고가 secs초 이내로 신선한가 (멀티계정 생사 판정 보조, 2026-08-15).
    ★근거★ idle 매크로는 변경-해시 게이트 때문에 수천 초씩 무보고다(실측 5,500초+).
    따라서 '아주 최근 보고'는 살아있다는 확실한 증거다(반대는 성립하지 않는다)."""
    if not ts_str:
        return False
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() <= secs
    except Exception:
        return False


def tenant_bugs_dir(tenant: str) -> str:
    """테넌트별 버그스샷 폴더. main = 기존 루트(호환), 그 외 = 하위 폴더."""
    return BUGS_DIR if tenant == "main" else os.path.join(BUGS_DIR, tenant)


async def _build_full_state(tenant: str = "main") -> list[dict]:
    """해당 테넌트의 pc_status + updater_status + bug_count + char_info + slot_filters 병합 목록.
    저장 키는 네임스페이스("t::PC-01")지만 반환 pc_id는 원래 이름으로 벗겨서 냄."""
    def _mine(rows: list) -> list:
        out = []
        for r in rows:
            t, raw = split_ns(r.get("pc_id") or "")
            if t == tenant:
                r = dict(r)
                r["pc_id"] = raw
                out.append(r)
        return out

    statuses = _mine(await get_all_statuses())
    updater_statuses = _mine(await get_all_updater_statuses())
    _filters_raw = await get_all_slot_filters()
    all_filters = {split_ns(k)[1]: v for k, v in _filters_raw.items() if ns_of(k) == tenant}

    # 최근 30분 사망 횟수 (pc_id별)
    _death_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    _deaths_raw = await get_death_counts_since(_death_cutoff)
    death_counts = {split_ns(k)[1]: v for k, v in _deaths_raw.items() if ns_of(k) == tenant}

    updater_map: dict[str, dict] = {}
    for u in updater_statuses:
        pid = u.get("pc_id")
        if pid:
            updater_map[pid] = u

    bug_counts: dict[str, int] = {}
    try:
        bdir = tenant_bugs_dir(tenant)
        if os.path.isdir(bdir):
            for fname in os.listdir(bdir):
                if fname.endswith(".png"):
                    m = re.match(r"^(.+?)_\d{8}_\d{6}_", fname)
                    if m:
                        # ★★버그 스샷은 ★PC 단위★ 다 (2026-08-23 주인님 지적)★★
                        #   주인님: "대시보드에 스크린샷 아직도 컴퓨터 단위아닌거같은데"
                        #   매크로는 파일을 ★베이스 pc_id★ 로 올린다(PC-24_...). 그런데
                        #   여기서 그대로 세면 그 수가 ★계정1 카드에만★ 붙는다.
                        #   스샷은 그 물리 PC 의 화면이지 계정의 것이 아니다 —
                        #   계정2 카드를 보고 있을 때도 같은 수가 보여야 한다.
                        #   → 베이스로 세고, 아래에서 모든 계정 카드에 같은 값을 준다.
                        pid = _base_pc(m.group(1))
                        bug_counts[pid] = bug_counts.get(pid, 0) + 1
    except Exception:
        pass

    seen: set[str] = set()
    for pc in statuses:
        pid = pc.get("pc_id"); seen.add(pid)
        # ★멀티계정(v1.1.412 리뷰 결함 1)★ 업데이터는 PC 단위라 base id(PC-03)로만 보고한다.
        #   부계정 카드(PC-03b)는 pc_status만 올리고 updater_map엔 'PC-03b'가 영영 없어
        #   무조건 offline으로 강등됐다. 접미사(b/c/d)를 벗긴 base id로 updater를 조인한다.
        is_sub = bool(pid and pid[-1] in ("b", "c", "d") and pid[:-1] in seen | set(updater_map))
        ukey = pid[:-1] if (pid and pid[-1] in ("b", "c", "d") and pid[:-1] in updater_map) else pid
        if ukey in updater_map:
            u = updater_map[ukey]
            pc["_updater_state"]   = u.get("macro_state", "unknown")
            pc["_updater_version"] = u.get("updater_version", "")
            # ★★업데이터 보고가 얼마나 낡았는지 같이 싣는다 (2026-08-20 PC-23)★★
            #   _updater_state 는 업데이터가 ★마지막으로 전송에 성공한★ 값이다.
            #   그런데 카드는 신선도를 전혀 안 보고 그대로 초록색으로 칠했다.
            #   → PC-23 이 14.6시간 죽어 있는 내내 "업데이터 running" 이 초록으로
            #     떠 있었고, 그래서 아무도 못 봤다. 함대 전 카드가 running 이었다
            #     (PC-17b 는 08-17 21:18 에 얼어붙은 값이 그대로 초록).
            #   ★HTTP 200 = 전달됨이지 적용됨이 아니다★ 의 표시판 버전이다(A2).
            try:
                _uat = u.get("_updated_at")
                if _uat:
                    _ud = datetime.fromisoformat(str(_uat).replace("Z", ""))
                    if _ud.tzinfo is None:
                        _ud = _ud.replace(tzinfo=timezone.utc)
                    pc["_updater_age_s"] = int(
                        (datetime.now(timezone.utc) - _ud).total_seconds())
            except Exception:
                pass
        if is_sub:
            # ★부계정 생사 = WS 접속 OR 아주 최근 보고(2026-08-15 실사고 수정)★
            #   WS 단독 판정은 WS가 순간 끊긴(재연결 중·HTTP 폴백) 매크로를 오프라인으로
            #   깔아뭉갰다 — PC-20b가 54초 전까지 사냥 로그를 찍는데 카드가 offline이었고,
            #   그 바람에 3.7시간 전 상태로 박제된 계정1 카드가 스택 맨 앞을 차지했다
            #   (사용자: "계정2 돌리고 있는데 왜 계정1이 카드 맨 앞이냐").
            # ★seen_fresh 추가★ — WS 가 반쯤 죽어도 로그/폴링/ack 가 오고 있으면 살아있다.
            if (ns(tenant, pid) not in macro_ws_connections
                    and not _fresh(pc.get("last_active"), 180)
                    and not seen_fresh(ns(tenant, pid), 180)):
                pc["status"] = "offline"
        elif ukey in updater_map:
            # base 카드: 업데이터 ★90초★ 타임아웃 → offline (상수 OFFLINE_TIMEOUT) (★기존 규칙 그대로 — 단일계정
            #   함대 회귀 0★). 계정 전환으로 버려진 base 카드의 '얼어붙은 사냥중' 문제는
            #   서버 추측이 아니라 ★매크로가 떠나며 마지막 offline 보고★로 푼다
            #   (config.switch_account_and_restart — 재리뷰 결함 2).
            # ★업데이터가 늦어도 매크로에서 뭐라도 오고 있으면 오프라인이 아니다★
            if _is_stale(u.get("_updated_at")) and not seen_fresh(ns(tenant, pid), 180):
                pc["status"] = "offline"
        else:
            # base 카드인데 updater 기록 자체가 없으면 offline (기존 규칙)
            pc["status"] = "offline"
        pc["_bug_count"] = bug_counts.get(_base_pc(pid), 0)   # ★PC 단위★ (2026-08-23)
        pc["deaths_30m"] = death_counts.get(pid, 0)
        pc["slot_filters"] = all_filters.get(pid, {})
        pc["_ws_live"] = ns(tenant, pid) in macro_ws_connections   # 2차 패스(한 PC=한 매크로)용
        # ★★내부망 주소는 마지막으로 받은 값을 유지한다 (2026-08-21 주인님 지적)★★
        #   주인님: "뭐만하면 내부망연결할수없다고 하는데 그것좀수정해"
        #   lan_url 은 상태 push 에만 실려온다. push 가 뜸해지면 카드에서 빈 값이 되고
        #   대시보드는 '내부망 주소 없음' 토스트를 띄운다 — ★주소가 없는 게 아니라
        #   최근에 안 받은 것뿐이다.★ 마지막으로 받은 값을 기억했다가 채워 준다.
        #   (토큰은 매크로 재기동 때 바뀌므로, 재기동하면 다음 push 가 새 값으로 덮는다)
        _lk = ns(tenant, pid)
        if pc.get("lan_url"):
            _lan_cache_last[_lk] = pc["lan_url"]
        elif _lan_cache_last.get(_lk):
            pc["lan_url"] = _lan_cache_last[_lk]
            pc["_lan_url_cached"] = True
        # char_info 이름 항상 로드 (OCR 수집값 우선)
        if pid:
            ci = await get_char_info(ns(tenant, pid))
            if ci:
                if ci.get("chars"):
                    pc["chars"] = [
                        c.get("name") or c.get("char_name") or ""
                        for c in ci["chars"]
                    ]
                if ci.get("total_kina"):
                    pc["_total_kina"] = ci["total_kina"]
                # 카드 "수집 X분 전" 표시용 (2026-07-25)
                if ci.get("collected_at"):
                    pc["_char_collected_at"] = ci["collected_at"]

    for pid, u in updater_map.items():
        if pid not in seen:
            # setup_complete=False (pc_id 미설정 or token 없음)이면 카드 표시 안 함
            if not u.get("setup_complete", True):
                continue
            row = {
                "pc_id":            pid,
                "status":           "offline",
                "_updater_state":   u.get("macro_state", "unknown"),
                "_updater_version": u.get("updater_version", ""),
                "_bug_count":       bug_counts.get(_base_pc(pid), 0),   # ★PC 단위★
                "deaths_30m":       death_counts.get(pid, 0),
            }
            # ★여기도 char_info를 붙인다(2026-07-28): 매크로가 죽어 pc_status 행이 없는 PC는
            #   이 분기로 오는데, 예전엔 char_info를 안 붙여 '창고키나 합계'에서 통째로 빠졌다.
            #   전광판 합계는 마지막으로 알던 값을 계속 세야 한다 — 꺼졌다고 재산이 준 게 아니다.★
            ci = await get_char_info(ns(tenant, pid))
            if ci:
                if ci.get("chars"):
                    row["chars"] = [c.get("name") or c.get("char_name") or "" for c in ci["chars"]]
                if ci.get("total_kina"):
                    row["_total_kina"] = ci["total_kina"]
                if ci.get("collected_at"):
                    row["_char_collected_at"] = ci["collected_at"]
            statuses.append(row)

    # ★2차 패스 — 한 PC = 한 매크로(2026-08-15 사용자: "컴퓨터가 같은데... 한 번에 한
    #   아이디밖에 못 들어가는데")★ 같은 base에 계정 카드가 2장 이상이면 ★반드시 하나만★
    #   현역이다. 나머지는 마지막 보고가 뭐였든(재연결중 등 박제) '다른 계정'으로 강등하고,
    #   exe는 PC당 하나뿐이라 macro_version도 현역 카드 것으로 통일한다.
    #   현역 선택: ①WS 접속 카드 ②없으면 ★마지막 보고가 가장 최신인 카드★.
    #   ②가 필수다(2026-08-15 실사고) — WS만 보면 PC-20b(WS 순간 끊김, 54초 전 보고)가
    #   현역으로 안 뽑혀 아무도 강등되지 않았고, 3.7시간 전 'reconnecting'으로 굳은 계정1이
    #   업데이터 생존 덕에 온라인 취급되어 스택 맨 앞을 차지했다.
    #   카드가 1장뿐인 PC(단일계정 함대 17대)는 건드리지 않는다 = 회귀 0.
    def _base_of(p: str) -> str:
        return p[:-1] if p and p[-1] in ("b", "c", "d") else p
    by_base: dict[str, list] = {}
    for pc in statuses:
        by_base.setdefault(_base_of(pc.get("pc_id") or ""), []).append(pc)
    for members in by_base.values():
        if len(members) < 2:
            continue
        live = next((m for m in members if m.get("_ws_live")), None)
        if live is None:
            live = max(members, key=lambda m: str(m.get("last_active") or ""))
        for m in members:
            if m is live:
                continue
            m["status"] = "other_account"
            if live.get("macro_version"):
                m["macro_version"] = live.get("macro_version")

    # ★파섹 주소록 주입(2026-08-15)★ — 카드에 parsec_peer_id 를 붙여 [🎮 파섹] 버튼을 만든다.
    #   ★매크로가 보고하지 않는다★. 처음엔 각 PC의 매크로가 자기 peer_id를 보고하게 만들었는데,
    #   사용자가 정확히 짚었다: "매크로는 파섹이랑 상관이 없어". 실제로 그 설계는 순환 의존이었다 —
    #   매크로가 죽으면 보고가 끊겨 파섹 버튼도 사라지는데, ★파섹으로 들어가야 할 상황이 바로
    #   그 상황★이다(PC-13: 게임 끊기고 멈춰 있어 원격으로 봐야 하는 상태). 그래서 주소록은
    #   관제컴이 파섹 계정 API에서 뽑아 POST /parsec/map 으로 밀어넣고, 서버가 들고 있는다.
    #   → 대상 PC가 꺼져 있든 매크로가 죽었든 버튼은 항상 살아 있다.
    pmap = await _get_parsec_map(tenant)
    if pmap:
        for pc in statuses:
            n = _pc_num(pc.get("pc_id"))
            if n is None:
                continue
            peer = pmap.get(str(n))
            if peer:
                pc["parsec_peer_id"] = peer
    # ★★'오늘 완주' 를 날짜로 검증해서 붙인다 (2026-08-20 PC-12 실측)★★
    #   ★무엇이 문제였나★ daily_progress 의 completed 플래그는 ★늙지 않는다.★
    #   리셋은 매크로가 ★자기 로컬 파일에만★ 한다(새벽 5시). 그래서 며칠째 안 뜬 계정
    #   카드는 옛날 completed=true 를 그대로 달고 있고, 서버는 그걸 오늘 것으로 내보냈다.
    #   실측 2026-08-20: 함대 계정2 카드 ★6장★ 이 08-18~19 완주를 '오늘 완주'로 표시 중.
    #     PC-12b 슬롯1·2 completed_time=2026-08-18  (이틀 전)
    #   → 대시보드 숫자·감시기 미완 판정·계정 순환이 전부 이 값을 보므로 여기서 한 번에 고친다.
    #   ★completed 자체는 지우지 않는다★ — 매크로가 보낸 사실은 보존하고, 판정용
    #     today 플래그만 얹는다(되돌릴 수 있고, 옛 소비자도 안 깨진다).
    try:
        for pc in statuses:
            for _e in (pc.get("daily_progress") or []):
                _e["today"] = bool(_e.get("completed")) and _rot_is_today(_e.get("completed_time"))
    except Exception:
        pass
    # ★순환 무장 상태를 카드에 싣는다 (2026-08-20 감사)★ — 무장됐는지 화면에서 볼 수
    #   없으면 "▶시작을 눌렀는데 계정이 안 넘어간다"를 아무도 진단할 수 없다.
    try:
        for pc in statuses:
            _rk = ns(tenant, _base_pc(str(pc.get("pc_id") or "")))
            _rs = _ROT.get(_rk)
            if _rs:
                pc["_rot"] = str(_rs.get("stage") or "")
                # ★작업 순환이면 무슨 작업인지도 싣는다 (2026-08-23)★ — 뱃지가
                #   "🔄 계정 전환중" 만 뜨면 사냥 순환인지 회랑 순환인지 구분이 안 된다.
                _tk = str(_rs.get("task") or "")
                if _tk:
                    pc["_rot_task"] = ROT_TASK_LABEL.get(_tk, _tk)
                # ★전환 목표도 싣는다 (2026-08-21 주인님 요청 "전환중이라는 표시")★
                #   stage 만으로는 '어디로' 가 안 보여서 화면에서 진단이 안 된다.
                # ★★2026-08-23 수리: target 은 'b' 같은 ★글자★ 인데 int() 로 읽고 있었다.★★
                #   ValueError 가 이 for 문을 감싼 except 에 먹혀서, ★전환 중인 PC 가
                #   한 대라도 있으면 그 뒤 카드들은 _rot 를 통째로 못 받았다.★
                #   증상: 순환은 도는데 대시보드에 순환 뱃지가 안 보인다(= 진단 불가).
                _tg = str(_rs.get("target") or "")
                if _tg:
                    pc["_rot_target"] = ("abcd".index(_tg) + 1) if _tg in "abcd" else 0
    except Exception:
        pass
    return statuses


# ─── 파섹 주소록 (관제컴 → 서버, 매크로 무관) ─────────────────────────────────
PARSEC_MAP_SETTING = "parsec_map"
# peer_id 는 27자 base62(+ - _) 였다(실측). 길이는 넉넉히 잡되 형식은 좁게 검증한다.
_PEER_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _pc_num(pc_id: str):
    """'PC-18' → 18, 'PC-18b'(부계정 카드) → 18. 번호를 못 뽑으면 None.

    ★부계정 접미사를 먼저 벗긴다★ — 파섹 호스트는 '물리 PC' 하나뿐이므로 같은 본체의
    계정 카드는 모두 같은 peer_id 를 가리켜야 한다.
    """
    if not pc_id:
        return None
    s = pc_id[:-1] if pc_id[-1] in ("b", "c", "d") else pc_id
    m = re.search(r"(\d{1,3})\s*$", s)
    return int(m.group(1)) if m else None


async def _get_parsec_map(tenant: str) -> dict:
    try:
        raw = await get_setting(ns(tenant, PARSEC_MAP_SETTING))
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


@app.post("/parsec/map")
async def set_parsec_map(request: Request):
    """관제컴의 updater/parsec_multi.py 가 밀어넣는 {"번호": "peer_id"} 주소록.

    파섹 세션 토큰은 관제컴 밖으로 나오지 않는다 — 여기 올라오는 건 peer_id 뿐이고,
    peer_id 만으로는 접속이 안 된다(호스트가 내 파섹 계정으로 로그인돼 있어야 한다).
    """
    tenant = check_api_key(request) or check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    src = body.get("map")
    if not isinstance(src, dict):
        raise HTTPException(status_code=400, detail="map 은 {\"번호\": \"peer_id\"} 객체여야 합니다")
    clean: dict = {}
    for k, v in list(src.items())[:200]:
        ks, vs = str(k).strip(), str(v).strip()
        if ks.isdigit() and _PEER_RE.match(vs):
            clean[str(int(ks))] = vs          # "08" 과 "8" 을 같은 칸으로 정규화
    await set_setting(ns(tenant, PARSEC_MAP_SETTING), json.dumps(clean, ensure_ascii=False))
    await push_state(tenant)                   # 대시보드 즉시 반영
    return JSONResponse({"ok": True, "count": len(clean), "skipped": len(src) - len(clean)})


@app.get("/parsec/map")
async def get_parsec_map(request: Request):
    tenant = check_api_key(request) or check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    return JSONResponse({"map": await _get_parsec_map(tenant)})


async def push_state(tenant: str = "main"):
    statuses = await _build_full_state(tenant)
    ver = _load_version_json()
    latest = {
        "macro": ver.get("exe", {}).get("version", ""),
        "updater": ver.get("updater", {}).get("version", ""),
    }
    await manager.broadcast({"type": "state", "pcs": statuses, "latest": latest}, tenant)


async def push_log(tenant: str, pc_id: str, message: str, level: str = "info"):
    """pc_id는 원래 이름(네임스페이스 벗긴 것)으로 호출할 것."""
    await manager.broadcast({"type": "log", "pc_id": pc_id, "level": level, "message": message}, tenant)


async def push_alert(tenant: str, pc_id: str, kind: str, message: str,
                     speak: bool = True, say: str = ""):
    """매크로가 올린 알림을 그 테넌트의 대시보드로 즉시 전달 (배너 + TTS 음성).
    message=화면에 띄울 자세한 문장, say=소리내어 읽을 짧은 문구.
    pc_id는 네임스페이스를 벗긴 원래 이름으로 호출할 것."""
    await manager.broadcast({"type": "alert", "pc_id": pc_id, "kind": kind,
                             "message": message, "speak": bool(speak),
                             "say": say}, tenant)


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

/* ══════════════════════════════════════════════════════════════════════════
   ★로그인 — 커맨드 덱 (2026-08-16)★  대시보드와 같은 언어로 맞춘다.
   차가운 잉크 바닥 + 따뜻한 앰버. 예전 보라/시안 그라데이션 글자는 걷어냈다
   (대시보드와 안 맞고, 흔한 AI 화면 인상).
   ★주의★ 이 블록 안에 절대 문자열 "</st"+"yle>" 를 쓰지 말 것 — HTML 파서가
   거기서 스타일을 끊어 뒤쪽 CSS가 전부 죽고 본문에 글자로 쏟아진다(실사고).
   ══════════════════════════════════════════════════════════════════════════ */
  :root{
    --lg-ink0:#05070d; --lg-ink1:#0c1120; --lg-line:#1a2237; --lg-line2:#26314b;
    --lg-t0:#eef2fa; --lg-t1:#9aa7c2; --lg-t2:#5c6b8a; --lg-t3:#3a4762;
    --lg-gold:#f2b53c; --lg-gold-s:#ffd479; --lg-coral:#ff5d6e; --lg-mint:#3ddc9a;
    --lg-disp:"Bahnschrift","DIN Alternate","Segoe UI Variable Display","Pretendard",system-ui,sans-serif;
  }
  body{background:
    radial-gradient(1000px 620px at 22% -10%, #16233f 0%, transparent 60%),
    radial-gradient(760px 520px at 82% 108%, #2a1e0c 0%, transparent 58%),
    var(--lg-ink0)!important}
  /* 위쪽에서 내려오는 광원 하나만 — 예전 aurora 는 끈다 */
  body::after{background:radial-gradient(closest-side,rgba(79,211,232,.16),transparent 70%)!important;
    filter:blur(70px)!important;animation:aurora 18s ease-in-out infinite}
  /* 지평선 격자는 살리되 색만 덱으로 */
  #bg-fx .horizon{opacity:.32}
  #bg-fx .horizon::before{
    background:repeating-linear-gradient(90deg,rgba(79,211,232,.30) 0 1px,transparent 1px 64px),
      repeating-linear-gradient(0deg,rgba(242,181,60,.22) 0 1px,transparent 1px 44px)!important}

  .login-card{
    position:relative;overflow:hidden;
    background:linear-gradient(168deg,#101827 0%,#0a0f1c 58%,#080c17 100%)!important;
    border:1px solid var(--lg-line)!important;
    box-shadow:0 34px 80px -34px rgba(0,0,0,.95),inset 0 1px 0 rgba(255,255,255,.055)!important;
    backdrop-filter:none!important}
  /* 카드 윗면을 훑는 앰버 실선 — '전원이 들어온 판' */
  .login-card::before{content:'';position:absolute;left:22px;right:22px;top:0;height:1px;
    background:linear-gradient(90deg,transparent,var(--lg-gold),transparent);opacity:.75}

  /* 인장(seal) — 예전 ⚔ 이모지 대신 금빛 젬 안에 넣는다 */
  .seal{width:54px;height:54px;margin:0 auto;border-radius:15px;display:flex;
    align-items:center;justify-content:center;font-size:25px;line-height:1;
    background:linear-gradient(145deg,var(--lg-gold-s),var(--lg-gold) 45%,#8a5f10);
    box-shadow:0 0 26px rgba(242,181,60,.34),inset 0 1px 0 rgba(255,255,255,.55);
    animation:floaty 4s ease-in-out infinite}
  .lg-title{font-family:var(--lg-disp);font-size:26px;font-weight:700;letter-spacing:.01em;
    color:var(--lg-t0);background:none!important;-webkit-text-fill-color:currentColor!important;
    animation:none!important}
  .lg-sub{font-family:'Orbitron',var(--lg-disp);font-size:9.5px;letter-spacing:.32em;
    color:var(--lg-gold);opacity:.78;margin-top:5px}
  .lg-note{color:var(--lg-t3);font-size:11.5px;margin-top:11px;letter-spacing:.01em}

  .lg-field{position:relative;margin-bottom:13px}
  .lg-lab{display:block;font-size:10px;letter-spacing:.14em;color:var(--lg-t3);
    font-weight:700;margin-bottom:7px}
  #pw{
    width:100%;background:#070b14!important;border:1px solid var(--lg-line2)!important;
    border-radius:10px!important;padding:12px 14px!important;font-size:13.5px!important;
    color:var(--lg-t0)!important;letter-spacing:.16em;
    box-shadow:inset 0 1px 3px rgba(0,0,0,.6);transition:border-color .16s,box-shadow .16s}
  #pw::placeholder{color:var(--lg-t3);letter-spacing:.02em}
  #pw:focus{border-color:rgba(242,181,60,.62)!important;
    box-shadow:inset 0 1px 3px rgba(0,0,0,.6),0 0 0 3px rgba(242,181,60,.14)!important}

  .login-btn{
    background:linear-gradient(180deg,var(--lg-gold-s),var(--lg-gold))!important;
    background-size:auto!important;border:1px solid #c9922a!important;border-radius:10px!important;
    color:#2a1c02!important;font-family:var(--lg-disp);font-size:14px!important;font-weight:700!important;
    letter-spacing:.06em;box-shadow:0 10px 24px -12px rgba(242,181,60,.7)}
  .login-btn:hover{background:linear-gradient(180deg,#ffdd8f,#f7bd46)!important;transform:translateY(-1px)}
  .login-btn:active{transform:translateY(0) scale(.985)}
  .login-btn[disabled]{opacity:.62;cursor:default;transform:none!important;
    background:linear-gradient(180deg,#4a5163,#3a3f4d)!important;
    border-color:#3a4250!important;color:#aab3c4!important;box-shadow:none}

  #err{background:rgba(255,93,110,.1)!important;border:1px solid rgba(255,93,110,.34)!important;
    color:#ffb3ba!important;border-radius:10px!important;font-size:12px!important;
    letter-spacing:.01em;animation:shake .3s}
  @keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-4px)}75%{transform:translateX(4px)}}
  @media (prefers-reduced-motion:reduce){
    .seal,body::after,#bg-fx .horizon::before,#err{animation:none!important}}
</style>
</head>
<body class="text-gray-100 flex items-center justify-center min-h-screen">
<div id="bg-fx" aria-hidden="true"><div class="stars"></div><div class="horizon"></div></div>
<div class="w-full max-w-sm">
  <div class="login-card rounded-2xl p-8">
    <div class="text-center mb-7">
      <div class="seal">⚔</div>
      <h1 class="lg-title mt-4">혼종 사령부</h1>
      <p class="lg-sub">HONJONG COMMAND</p>
      <p class="lg-note">AION2 함대 관제</p>
    </div>
    <div id="err" class="hidden px-4 py-2.5 mb-4"></div>
    <div class="lg-field">
      <label class="lg-lab" for="pw">비밀번호</label>
      <input id="pw" type="password" placeholder="••••••••" autofocus autocomplete="current-password"
        onkeydown="if(event.key==='Enter')login()">
    </div>
    <button id="lg-btn" onclick="login()" class="login-btn w-full py-3">
      들어가기
    </button>
  </div>
</div>
<script>
// ★상태 전부 구현(2026-08-16)★ — 예전엔 누르고 나서 응답이 올 때까지 아무 반응이
//   없어서 두 번 세 번 누르게 됐다(그만큼 잠금 카운터도 빨리 찬다). 누르는 즉시
//   버튼을 잠그고 '확인 중'으로 바꾼다. 서버가 잠금(429)을 주면 그 문구를 그대로 보여준다.
async function login() {
  const btn = document.getElementById('lg-btn');
  const e   = document.getElementById('err');
  const pw  = document.getElementById('pw').value;
  if (btn.disabled) return;
  e.classList.add('hidden');
  btn.disabled = true; btn.textContent = '확인 중…';
  try {
    const r = await fetch('/auth/login', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: pw})
    });
    if (r.ok) { btn.textContent = '들어가는 중…'; location.href = '/'; return; }
    let msg = '비밀번호가 틀렸습니다.';
    try { const j = await r.json(); if (j && j.detail) msg = j.detail; } catch(_) {}
    if (r.status === 429 && msg === '비밀번호가 틀렸습니다.') msg = '시도가 너무 많습니다. 잠시 후 다시 시도하세요.';
    e.textContent = msg; e.classList.remove('hidden');
  } catch(_) {
    e.textContent = '서버에 연결할 수 없습니다.'; e.classList.remove('hidden');
  } finally {
    if (btn.textContent !== '들어가는 중…') { btn.disabled = false; btn.textContent = '들어가기'; }
    const p = document.getElementById('pw'); p.value = ''; p.focus();
  }
}
</script>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# 로그인 무차별 대입(brute force) 방어 — 2026-07-27
# 배경: 프로그램이 외부로 배포되면서 대시보드 주소가 알려짐. 비밀번호가 짧으면
#       조합 수가 적어 자동화 도구로 수 초 내 전수 시도가 가능하다. 레이트 리밋이 없으면
#       그 시도를 막을 수단이 전혀 없으므로, IP별 실패 누적 + 점증 잠금으로 차단한다.
# 구현: Railway 단일 인스턴스라 인메모리로 충분(재시작 시 초기화되지만 공격도 처음부터).
# ─────────────────────────────────────────────────────────────────────────────
LOGIN_MAX_FAILS   = 5           # 이 횟수를 넘기면 잠금
LOGIN_WINDOW      = 300.0       # 실패 누적 관측 창(초)
LOGIN_LOCK_STEPS  = (60, 300, 900, 3600)   # 잠금 시간 점증(초) — 반복 공격일수록 길게
GLOBAL_MAX_FAILS  = 60          # 창 안 전체 실패 상한(여러 IP 분산 시도 완화)
_LOGIN_FAILS: dict = {}         # ip -> {"n": int, "since": float, "until": float, "step": int}
_LOGIN_GLOBAL = {"n": 0, "since": 0.0}


def _ip_from_xff(xff: str, peer: str) -> str:
    """★X-Forwarded-For 위조 방어(2026-07-27 보안감사 critical).
    XFF는 'client, proxy1, proxy2…' 순서로 각 프록시가 '자기가 본 주소'를 뒤에 붙인다.
    따라서 맨 앞 값은 클라이언트가 마음대로 적어 보낼 수 있다 — 그걸 신뢰하면
      ① 매 요청 다른 IP를 적어 레이트리밋을 무한 우회하고
      ② 남의 IP를 적어 그 사람을 잠가버리는 역공격(DoS)까지 된다.
    신뢰할 수 있는 건 '우리 앞단 프록시가 붙인' 맨 뒤 값이므로 오른쪽부터 채택한다."""
    parts = [p.strip() for p in (xff or "").split(",") if p.strip()]
    if parts:
        return parts[-1]
    return peer or "unknown"


def _client_ip(request: Request) -> str:
    """프록시(Railway) 뒤 실제 클라이언트 IP — 위조 불가한 최우측 항목 사용"""
    peer = request.client.host if request.client else ""
    return _ip_from_xff(request.headers.get("x-forwarded-for", ""), peer)


def _login_locked_for(ip: str) -> float:
    """남은 잠금 시간(초). 0이면 시도 허용. (전역 상한은 '잠금'이 아니라 '지연'으로만 쓴다)"""
    now = time.time()
    rec = _LOGIN_FAILS.get(ip)
    if rec and rec["until"] > now:
        return rec["until"] - now
    return 0.0


def _global_overloaded() -> bool:
    """★전역 실패 상한은 '차단'이 아니라 '지연'에만 쓴다(2026-07-27 보안감사).
    차단으로 쓰면 공격자가 일부러 실패를 60회 쌓아 주인님까지 못 들어오게 만드는
    값싼 서비스 거부 스위치가 된다. 분산 시도 속도만 떨어뜨리는 용도로 격하.★"""
    now = time.time()
    g = _LOGIN_GLOBAL
    if now - g["since"] > LOGIN_WINDOW:
        g["n"], g["since"] = 0, now
    return g["n"] >= GLOBAL_MAX_FAILS


def _login_failed(ip: str):
    now = time.time()
    rec = _LOGIN_FAILS.get(ip)
    if not rec or now - rec["since"] > LOGIN_WINDOW:
        rec = {"n": 0, "since": now, "until": 0.0, "step": 0}
        _LOGIN_FAILS[ip] = rec
    rec["n"] += 1
    g = _LOGIN_GLOBAL
    if now - g["since"] > LOGIN_WINDOW:
        g["n"], g["since"] = 0, now
    g["n"] += 1
    if rec["n"] >= LOGIN_MAX_FAILS:
        lock = LOGIN_LOCK_STEPS[min(rec["step"], len(LOGIN_LOCK_STEPS) - 1)]
        rec["until"] = now + lock
        rec["step"] += 1
        rec["n"] = 0
        rec["since"] = now
        print(f"[SECURITY] 로그인 실패 누적 → {ip} {lock}초 차단 (누적 잠금 {rec['step']}회)")
    if len(_LOGIN_FAILS) > 5000:        # 메모리 폭주 방지
        for k in [k for k, v in _LOGIN_FAILS.items() if v["until"] < now - 3600][:2000]:
            _LOGIN_FAILS.pop(k, None)


def _login_ok(ip: str):
    _LOGIN_FAILS.pop(ip, None)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTML_LOGIN


@app.post("/auth/login")
async def do_login(request: Request, response: Response):
    ip = _client_ip(request)
    wait = _login_locked_for(ip)
    if wait > 0:
        raise HTTPException(status_code=429,
                            detail=f"로그인 시도가 너무 많습니다. {int(wait) + 1}초 후 다시 시도하세요.")
    if _global_overloaded():
        await asyncio.sleep(1.5)      # 분산 시도 감속 — 차단이 아니라 지연(정상 로그인은 통과)
    try:
        body = await request.json()
    except Exception:
        _login_failed(ip)
        raise HTTPException(status_code=400, detail="잘못된 요청")
    # 비밀번호 → 테넌트 결정 (테넌트별 독립 대시보드, 2026-07-25)
    # 타이밍 차이로 비번을 좁히지 못하게 상수시간 비교로 전체 후보를 훑는다
    supplied = body.get("password") or ""
    tenant = None
    for pw, tn in PW_TO_TENANT.items():
        if hmac.compare_digest(supplied, pw):
            tenant = tn
    if tenant and tenant in KILLED_TENANTS:
        raise HTTPException(status_code=403, detail="이용이 중지되었습니다. 판매자에게 문의하세요")
    if tenant and tenant_expired(tenant):
        raise HTTPException(status_code=403, detail="이용 기간이 만료되었습니다")
    if not tenant:
        _login_failed(ip)
        await asyncio.sleep(0.5)        # 자동화 도구의 초당 시도 수를 떨어뜨림
        raise HTTPException(status_code=401, detail="Wrong password")
    _login_ok(ip)
    token = new_session(tenant)
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


# 렌탈 exe ↔ 서버 라이선스 응답 서명용 공유 비밀 (exe에도 동일 값 각인 — 가짜 서버로 우회 방지)
LICENSE_SECRET = os.getenv("LICENSE_SECRET", "aion2-license-v1-7f3a")


# ─── 전역 설정 KV (각성 난이도 프리셋 등, 2026-07-26) — 테넌트 스코프 ────────
# ★비밀 설정은 API 키로 못 읽는다 (2026-08-16)★
#   매크로 API 키는 ★공개 exe·공개 저장소에 각인★돼 있어(구조적) 유출을 전제해야 한다.
#   그런데 /setting/{key} 는 API 키 조회를 허용하므로, 여기에 비번을 넣으면
#   ★exe 받은 사람 누구나 꺼낼 수 있다★ — 내가 "대시보드 세션 전용이라 막힌다"고
#   설명했는데 이 엔드포인트를 못 봐서 틀린 말이었다(사용자 확인 중 발견).
#   매크로가 실제로 읽는 설정은 awakening_preset / sale_price 둘뿐이라(실측) 무해하다.
#   parsec_pw 는 명령 args 로만 배달된다(enrich_cmd_args) — 매크로는 조회할 필요가 없다.
SECRET_SETTINGS = {"parsec_pw", "parsec_id"}


@app.get("/setting/{key}")
async def get_setting_ep(key: str, request: Request):
    """매크로(X-Api-Key)와 대시보드(세션) 양쪽 조회 허용 — ★비밀 키는 세션만★."""
    if key in SECRET_SETTINGS:
        tenant = check_session(request)          # API 키로는 못 읽는다
    else:
        tenant = check_api_key(request) or check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    val = await get_setting(ns(tenant, key))
    return JSONResponse({"key": key, "value": val})


@app.post("/setting/{key}")
async def set_setting_ep(key: str, request: Request):
    """대시보드(세션)에서 설정 변경."""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400)
    val_raw = str(body.get("value", ""))
    # rental_kill은 테넌트 나열이라 100자 상한이 이름을 중간에서 잘라 '무경고 킬 누락/오차단'을
    # 만들 수 있다(리뷰 2026-08-06 major) — 이 키만 1000자. 나머지 설정은 기존 100자 유지.
    # ★★키마다 상한이 다르다 — 넘치면 ★조용히 잘려서★ 값이 깨진다★★
    #   rental_kill : 테넌트 나열이라 100자면 이름이 중간에서 잘려 '무경고 킬 누락/오차단'
    #                 (리뷰 2026-08-06 major)
    #   ai_dungeon_done : ★AI 던전 추천의 완료 체크★ (2026-08-22). {"day":"...","keys":[...]}
    #                 형태이고 캐릭이 150명대라 100자는 ★수십 배 부족★ 하다.
    #                 100자로 자르면 JSON 이 깨져 파싱 실패 → 체크가 매번 초기화된다
    #                 (직원분들이 "체크했는데 사라진다" 를 겪게 된다). 배포 전 게이트에서 잡음.
    _CAP = {"rental_kill": 1000, "ai_dungeon_done": 8000}
    val = val_raw[:_CAP.get(key, 100)]
    if len(val_raw) > len(val):
        print(f"[설정] ★{key} 값이 상한({_CAP.get(key,100)})을 넘어 잘렸다★ — {len(val_raw)}자")
    await set_setting(ns(tenant, key), val)
    # 렌탈 킬스위치(2026-08-06): main 세션의 rental_kill 저장 즉시 메모리 반영.
    # 지인 테넌트가 같은 키를 저장해도 자기 네임스페이스 설정일 뿐 여기 안 닿는다.
    if tenant == "main" and key == "rental_kill":
        KILLED_TENANTS.clear()
        KILLED_TENANTS.update(_parse_killed(val))
        # 오타·미등록 이름을 응답에 실어 무음 실패를 막는다(리뷰: ok:true만 주면 킬 실패를 모름)
        unknown = sorted(t for t in KILLED_TENANTS if t not in TENANTS)
        print(f"[KILL] 렌탈 킬스위치 갱신: {sorted(KILLED_TENANTS) or '(전원 해제)'}"
              + (f" / ★미등록 이름(효과 없음): {unknown}★" if unknown else ""))
        return JSONResponse({"ok": True, "killed": sorted(KILLED_TENANTS),
                             "unknown": unknown, "truncated": len(val_raw) > len(val)})
    return JSONResponse({"ok": True})


@app.post("/license")
async def license_check(request: Request):
    """렌탈 exe 기간제 검증(2026-07-26). 만료 키는 check_api_key에서 이미 None이므로
    여기서는 '키 자체는 등록돼 있으나 만료'까지 구분해 알려준다. 응답은 HMAC 서명."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400)
    key = body.get("api_key", "")
    nonce = str(body.get("nonce", ""))[:64]   # 리플레이 방지용 클라 난수(서명에 포함)
    # ★키 판별 오라클 차단(2026-07-27 보안감사 critical): 이 엔드포인트는 무인증이라
    #   check_api_key의 실패 카운터를 우회해 무제한으로 키 정오를 물어볼 수 있었다.
    #   → 같은 IP 실패 카운터를 공유하고, 상수시간 비교를 쓰고, 지연을 준다.★
    ip = _client_ip(request)
    if _key_probe_blocked(ip):
        raise HTTPException(status_code=429, detail="too many attempts")
    tenant = None
    for _k, _tn in KEY_TO_TENANT.items():
        if hmac.compare_digest(key, _k):
            tenant = _tn
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    if not tenant:
        _key_probe_failed(ip)
        await asyncio.sleep(0.4)
        # ★reason을 'unknown_key'로 구분해주면 '등록된 키인지'까지 알려주는 셈 → 단일화★
        payload = {"valid": False, "reason": "invalid", "expires": "", "now": now, "nonce": nonce}
    elif tenant in KILLED_TENANTS:
        # ★킬스위치(2026-08-06): 기간 만료가 아니라 판매자가 서버에서 내린 즉시 중지.
        #   클라(license_guard)는 이 reason을 받으면 재시도 없이 바로 정지한다.★
        payload = {"valid": False, "reason": "killed", "expires": "", "now": now, "nonce": nonce}
    elif tenant_expired(tenant):
        payload = {"valid": False, "reason": "expired", "expires": (TENANTS[tenant].get("expires") or ""), "now": now, "nonce": nonce}
    else:
        payload = {"valid": True, "reason": "", "expires": (TENANTS[tenant].get("expires") or ""), "now": now, "nonce": nonce}
    base = f"{payload['valid']}|{payload['reason']}|{payload['expires']}|{payload['now']}|{nonce}"
    payload["sig"] = hmac.new(LICENSE_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
    return JSONResponse(payload)


@app.get("/health")
async def health(request: Request):
    """[진단] 업타임+메모리 — Railway 자발 재시작(배포 무관 boot 변경) 원인 추적용(2026-07-25).
    uptime이 짧으면 최근 크래시/재시작, rss가 계속 오르면 메모리 누수→OOM 의심.
    ★상세(볼륨 경로·DB 크기·버그스샷 수)는 main 세션에만(2026-08-06 감사 minor)★ —
    무인증 응답은 헬스체크에 필요한 최소치만. Railway healthcheck는 /login을 쓰므로 무관."""
    _detail = check_session(request) == "main"
    rss_mb = None
    try:
        import resource
        rss_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)  # Linux: KB→MB
    except Exception:
        pass
    # ★DB 영속 여부(2026-07-28 실사고): 재시작마다 pc_status/char_info가 통째로 날아가
    #   '창고키나 합계'가 뚝 떨어졌다. 살아 있는 매크로는 WS 재연결 때 char_info.json을
    #   다시 올려 복구되지만, 매크로가 죽은 PC는 복구 주체가 없어 영영 빠진다.
    #   원인은 DB_PATH가 볼륨(/data) 밖을 가리키는 것 — 여기서 바로 보이게 노출한다.
    _dbp = os.getenv("DB_PATH") or "(기본값)"
    try:
        from database import DB_PATH as _DBP
        _dbp = _DBP
    except Exception:
        pass
    _bugs = 0
    try:
        _bugs = len([f for f in os.listdir(BUGS_DIR) if f.endswith(".png")])
    except Exception:
        pass
    out = {
        "boot": SERVER_BOOT_ID[:8],
        # ★떠 있는 코드의 지문★ — 로컬 server/main.py 의 sha256[:8] 과 같으면 내 빌드다.
        #   uptime 은 '살아 있나'만 알려주지 '무엇이 떠 있나'는 못 알려준다(2026-08-18 실사고).
        "code": SERVER_CODE_ID,
        "uptime_s": int(time.time() - SERVER_BOOT_TS),
        # ★경로 추측이 아니라 실측: 지난 부팅의 마커가 살아남았는지로 판정(_probe_volume)★
        #   false면 재시작마다 DB·스샷이 전부 사라진다 → Railway 볼륨을 마운트해야 한다.
        "disk_persisted": VOLUME_PERSISTED,
        # ★어느 빌드가 떠 있는지 밖에서 보이게 (2026-08-18)★
        #   이미지 배포처를 raw 로 바꿔 놓고 재배포를 30분 기다렸는데, 배포가 됐는지
        #   안 됐는지 확인할 방법이 없어 /check 응답만 계속 찔러 봤다. 값을 내보내면 끝난다.
        "img_base": _GH_CDN.split("//", 1)[-1][:28],
        # ★★지금 /check 가 ★무슨 버전을 광고하고 있는지★ 를 밖에서 보이게 (사고 146)★★
        #   2026-08-22 실사고: 605·606·607 을 11분 안에 몰아 올렸더니 _version_cache(300초)
        #   + raw.githubusercontent 엣지 캐시가 겹쳐, 릴리스 후 ~10분간 /check 가 옛 버전을
        #   광고했다. 그동안 [업데이트] 를 누르면 서버가 exe_update 키를 통째로 빼고 주고
        #   업데이터는 그걸 ★'최신'★ 으로 읽고 조용히 끝낸다 → "눌러도 안 한다".
        #   주인님이 확인할 수단이 없었다. 이제 이 두 줄이면 1초에 판정된다.
        "serving_exe": (((_version_cache.get("data") or {}).get("exe") or {}).get("version")
                        if isinstance(_version_cache.get("data"), dict) else None),
        "version_cache_age_s": (round(time.time() - float(_version_cache.get("ts") or 0), 1)
                                if _version_cache.get("ts") else None),
    }
    if _detail:
        out.update({
            "rss_max_mb": rss_mb,
            "db_path": _dbp,
            "db_size_kb": (round(os.path.getsize(_dbp) / 1024, 1) if os.path.exists(_dbp) else 0),
            "bug_files": _bugs,
            "prev_boot": (VOLUME_PREV.get("boot") or "")[:8] or None,
            "prev_boot_at": VOLUME_PREV.get("at"),
        })
    return JSONResponse(out)


@app.get("/debug/deaths")
async def debug_deaths(request: Request):
    """[진단용] death_events 원본 + 현재시각/컷오프/집계. 사망수 안 줄어드는 원인 추적."""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
    events = [e for e in await get_all_death_events() if ns_of(e.get("pc_id") or "") == tenant]
    counts = {split_ns(k)[1]: v for k, v in (await get_death_counts_since(cutoff)).items()
              if ns_of(k) == tenant}
    # pc별 이벤트 타임스탬프 나열
    by_pc: dict[str, list] = {}
    for e in events:
        by_pc.setdefault(split_ns(e["pc_id"])[1], []).append(e["created_at"])
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
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<!-- Orbitron=전광판 숫자 / Black Han Sans=오늘의 한마디(굵고 팍 치는 헤드라인체)
     / Nanum Myeongjo=인용 출처(명조로 대비를 준다) -->
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;800&family=Black+Han+Sans&family=Nanum+Myeongjo:wght@700;800&display=swap" rel="stylesheet">
<style>
  @keyframes pulse-badge{0%,100%{opacity:1}50%{opacity:.5}}
  .pulse{animation:pulse-badge 1.5s infinite;box-shadow:0 0 9px 1px currentColor}
  .log-box{font-family:'Consolas','D2Coding',monospace}
  .scrollbar-thin::-webkit-scrollbar{width:4px}
  .scrollbar-thin::-webkit-scrollbar-track{background:transparent}
  .scrollbar-thin::-webkit-scrollbar-thumb{background:linear-gradient(#6366f1,#22d3ee);border-radius:4px}
  /* ── 카드 선택 표시 v2: 상태 글로우(초록 등)와 확실히 구분 ──
     ① 시안 이중 링 + 배경 틴트 ② 우상단 "✔ 선택됨" 뱃지 ③ ★선택 중엔 미선택 카드 디밍★ */
  /* ── 멀티계정 카드 스택(2026-08-15 사용자: "포커 카드 여러장처럼") ── */
  .stack-layer{position:absolute;background:#0d1424;border:1px solid #374151;
    border-radius:0.75rem;cursor:pointer;color:#94a3b8;overflow:hidden;
    transition:background .15s,border-color .15s}
  .stack-layer:hover{background:#16203a;border-color:#6366f1;color:#c7d2fe}
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
  /* ★2026-08-23 주인님: "위쪽상단에 순환용이랑 선택카드만 하는거 두개로 나눠있는게 낫겟네"★
     같은 이름의 버튼이 두 줄로 늘어서므로 ★색 테두리로 갈라놔야★ 직원이 안 헷갈린다.
     순환 = 하늘색(전 계정을 돈다) / 선택 = 회색(그 카드 한 번). */
  /* 순환 = 하늘색 굵은 테두리(전 계정을 돈다) / 선택 = 회색 점선(그 카드 한 번) */
  .cmd-rot{border:2px solid rgba(56,189,248,.75);
    background:linear-gradient(160deg,rgba(8,47,73,.75),rgba(8,20,38,.8));
    box-shadow:inset 0 0 24px -14px rgba(56,189,248,.9)}
  .cmd-rot:hover{border-color:rgba(56,189,248,1)}
  .cmd-rot .cmd-legend{color:#0b0f1f;background:#7dd3fc;border-color:#7dd3fc}
  .cmd-one{border:1px dashed rgba(148,163,184,.45);background:rgba(255,255,255,.02)}
  .cmd-one .cmd-legend{color:#cbd5e1;border-color:rgba(148,163,184,.45)}
  /* ★버튼 자체에도 표식★ — 그룹 테두리를 안 봐도 버튼만으로 갈린다 */
  .cmd-rot .chip::before{content:'🔁 ';font-size:11px}
  /* ★★그룹 라벨은 ★읽히라고★ 있는 것이다 (2026-08-24 주인님 지적)★★
     주인님: "순환용이랑 비순환용 만들어놓으라햇잖아 근데 다 순환용 처럼 보이는데"
     8px + letter-spacing .24em 은 장식이지 글자가 아니었다. 버튼 이름·색까지 두 그룹이
     똑같으니 ★무엇으로도 구분이 안 됐다.★ 라벨을 키우고, 아래에서 버튼에도 표식을 단다. */
  .cmd-legend{position:absolute;top:-10px;left:10px;padding:2px 9px;border-radius:6px;
    font-size:12px;font-weight:800;letter-spacing:.02em;
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
  .chip-emerald{--c:52,211,153} .chip-teal{--c:45,212,191} .chip-violet{--c:167,139,250}
  .sel-badge{font-size:11px;font-weight:800;color:#c7d2fe;padding:3px 11px;border-radius:999px;white-space:nowrap;
    background:linear-gradient(90deg,rgba(99,102,241,.35),rgba(34,211,238,.22));
    border:1px solid rgba(129,140,248,.55);box-shadow:0 0 12px -3px rgba(99,102,241,.7)}
  /* ── 카드 컨텍스트 메뉴 v2 ── */
  .cm-panel{width:360px;padding:14px;border-radius:16px;
    background:linear-gradient(165deg,rgba(17,23,45,.97),rgba(8,12,26,.98));
    border:1px solid rgba(99,102,241,.4);
    box-shadow:0 18px 50px -12px rgba(0,0,0,.85),0 0 30px -10px rgba(99,102,241,.5);
    backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
  .cm-head{display:flex;align-items:center;gap:9px;padding:4px 3px 11px;margin-bottom:4px;
    border-bottom:1px solid rgba(99,102,241,.28);font-size:17px;font-weight:800}
  /* ★2026-08-23 주인님: "너무 작아보여 눈에도 잘안들어온다" → 전반 확대★ */
  .cm-sec{font-family:'Orbitron',ui-sans-serif,sans-serif;font-size:12px;font-weight:700;letter-spacing:.16em;
    color:#a5b4fc;opacity:1;margin:13px 2px 8px}
  .cm-grid2{display:grid;grid-template-columns:1fr 1fr;gap:7px}
  .cm-grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px}
  .cm-grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-bottom:7px}
  .cm-span2{grid-column:span 2}
  /* ★--c 를 여기서 선언하면 안 된다(2026-08-15 리뷰)★ — .cm-btn 이 .chip-* 팔레트보다 뒤에
     오는데 특정도가 같아서(둘 다 클래스 1개) 나중 것이 이긴다. 그래서 chip-teal/violet 등을
     붙여도 전부 회색으로 렌더됐다(파섹 버튼만이 아니라 카드 메뉴 전체가 그랬음).
     선언 대신 var() 폴백으로 두면 팔레트가 있으면 팔레트, 없으면 회색이 된다. */
  .cm-btn{padding:12px 6px;border-radius:10px;font-size:17px;font-weight:800;text-align:center;
    color:rgb(var(--c,148,163,184));border:1px solid rgba(var(--c,148,163,184),.4);
    background:rgba(var(--c,148,163,184),.08);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

  /* ── 계정 세부정보 표 (2026-08-16 재설계) ────────────────────────────────
     20대×최대4계정 = 80줄이 쌓여도 읽히게: 머리줄 고정 + PC 단위 덩어리 +
     빈 칸은 조용히. 값 칸은 ★클릭하면 그 값만 복사★(사용자 지시). */
  .acct-table{width:100%;border-collapse:separate;border-spacing:0;table-layout:fixed}
  .acct-table thead th{position:sticky;top:0;z-index:2;background:#111827;
    padding:9px 10px;text-align:left;font-size:11px;font-weight:600;color:#6b7280;
    letter-spacing:.04em;border-bottom:1px solid #374151}
  .acct-td{padding:7px 10px;font-size:12.5px;vertical-align:middle;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    border-bottom:1px solid rgba(31,41,55,.55)}
  /* PC가 바뀌는 첫 줄 위에만 선을 굵게 — 덩어리 경계 */
  .acct-row.acct-group td{border-top:1px solid #374151}
  .acct-row:hover td{background:rgba(31,41,55,.35)}
  .acct-pc{color:#a5b4fc;font-weight:700}
  .acct-n{text-align:center}
  .acct-chip{display:inline-block;min-width:19px;padding:1px 6px;border-radius:6px;
    background:rgba(167,139,250,.14);color:#c4b5fd;font-size:11px;font-weight:700}
  .acct-plat{color:#5eead4}
  .acct-id{color:#e5e7eb}
  .acct-em,.acct-ph{color:#9ca3af}
  .acct-ph{font-variant-numeric:tabular-nums}
  /* 클릭 복사 가능한 칸 — 평소엔 조용하고 hover 때만 드러난다 */
  .acct-cp{cursor:pointer;position:relative}
  .acct-cp:hover .acct-val{border-bottom:1px dashed rgba(196,181,253,.6)}
  .acct-cp:hover::after{content:'복사';position:absolute;right:6px;top:50%;
    transform:translateY(-50%);font-size:9.5px;color:#c4b5fd;
    background:rgba(17,24,39,.92);padding:1px 5px;border-radius:5px}
  .acct-cp.acct-hit{background:rgba(52,211,153,.16)!important}
  .acct-cp.acct-hit .acct-val{color:#6ee7b7}
  .cm-btn:hover{background:rgba(var(--c,148,163,184),.24);color:#fff;
    border-color:rgba(var(--c,148,163,184),.95);
    box-shadow:0 0 12px -3px rgba(var(--c,148,163,184),.65)}
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
  .tile-purple{--tile:#c084fc;--tile-glow:rgba(192,132,252,.55)}
  .tile-gold  {--tile:#fde047;--tile-glow:rgba(253,224,71,.5)}
  /* 전광판 그리드: 숫자 긴 타일(오드에너지/거래키나/창고키나)만 넓게, 카운트류는 좁게 */
  .stat-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.75rem}
  @media (min-width:640px){.stat-grid{grid-template-columns:2fr 2fr 3fr 2fr 2fr 2fr 3fr 3fr}}
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
  .done-dungeon{color:#e9d5ff;background:rgba(168,85,247,.2);border:1px solid rgba(192,132,252,.6);
    box-shadow:0 0 8px -2px rgba(192,132,252,.65)}
  .done-corridor{color:#bae6fd;background:rgba(14,165,233,.2);border:1px solid rgba(56,189,248,.6);
    box-shadow:0 0 8px -2px rgba(56,189,248,.65)}

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

/* ══════════════════════════════════════════════════════════════════════════
   ★커맨드 덱 테마 (2026-08-16)★ — 마지막에 오므로 위 규칙을 덮는다.
   ★구조는 하나도 안 건드렸다★ — 카드 HTML에는 주인님이 지적하셨던 사고 수정이
   여러 겹 들어 있다(이름 짤림·뱃지 두 줄·접미사 노출·드래그·우클릭). 마크업을 새로
   짜면 그게 다 날아간다. 그래서 ★기존 클래스에 CSS만 입힌다★.
   되돌리기: 이 블록만 지우면 예전 모습으로 완전 복귀한다.

   언어: 차가운 잉크 바닥 + 따뜻한 앰버 액센트(한난 대비) / 숫자는 Bahnschrift +
        tabular-nums / 판은 위 1px 하이라이트 + 색조 그림자 / 반경 한 체계.
   ══════════════════════════════════════════════════════════════════════════ */
  :root{
    --dk-ink0:#05070d; --dk-ink1:#0c1120; --dk-ink2:#111829;
    --dk-line:#1a2237; --dk-line2:#26314b;
    --dk-t0:#eef2fa; --dk-t1:#9aa7c2; --dk-t2:#5c6b8a; --dk-t3:#3a4762;
    --dk-gold:#f2b53c; --dk-gold-s:#ffd479;
    --dk-mint:#3ddc9a; --dk-coral:#ff5d6e; --dk-cyan:#4fd3e8;
    --dk-disp:"Bahnschrift","DIN Alternate","Segoe UI Variable Display","Pretendard",system-ui,sans-serif;
  }
  /* 바닥 — 기존 별밭 FX는 살리되 잉크 그라운드를 깐다 */
  body.bg-gray-950{
    background:
      radial-gradient(1200px 520px at 18% -6%, #16233f 0%, transparent 62%),
      radial-gradient(900px 420px at 92% -4%, #1d2033 0%, transparent 58%),
      var(--dk-ink0)!important;
  }
  /* 숫자에 성격을 준다 — 값이 바뀌어도 자리가 안 흔들린다 */
  .stat-num,.dk-num,#cnt-online,#cnt-completed{
    font-family:var(--dk-disp);font-variant-numeric:tabular-nums;letter-spacing:-.02em}

  /* ── 전광판: 타일 박스 → 하이라인 스트립 ───────────────────────────── */
  .stat-grid{
    background:linear-gradient(160deg,#0e1526,#0a0f1c 60%,#080c17);
    border:1px solid var(--dk-line);border-radius:18px;overflow:hidden;
    box-shadow:0 22px 56px -30px rgba(0,0,0,.9),inset 0 1px 0 rgba(255,255,255,.05);
    gap:0!important;position:relative;
  }
  .stat-grid::after{content:'';position:absolute;left:6%;top:-72%;width:48%;height:150%;
    pointer-events:none;background:radial-gradient(closest-side,rgba(79,211,232,.13),transparent)}
  .stat-tile{
    background:transparent!important;border:0!important;border-right:1px solid var(--dk-line)!important;
    border-radius:0!important;box-shadow:none!important;padding:14px 16px!important;position:relative;z-index:1}
  .stat-tile:last-child{border-right:0!important}
  .stat-tile::before,.stat-tile::after{display:none!important}
  .stat-icon{opacity:.34;font-size:13px!important}
  .stat-num{font-size:25px!important;font-weight:700!important;line-height:1.05!important}
  /* ★라벨 가독성(2026-08-16 사용자 지적: "숫자는 잘 보이는데 밑에 글자가 너무 안 보여")★
     내가 --dk-t3(#3a4762) 를 10px 에 씌운 게 원인이다. 이 바탕(#0b1120)에서 대비가
     2:1 남짓이라 사실상 안 읽힌다. --dk-t1 로 올리고 크기·굵기를 키운다(대비 ≈8:1).
     ★숫자는 건드리지 않는다★ — 잘 보인다고 하셨다. */
  .stat-label{color:var(--dk-t1)!important;font-size:11.5px!important;
    letter-spacing:.06em;font-weight:700;margin-top:.35rem!important}

  /* ── 명령 버튼줄: 묶음을 판으로 ─────────────────────────────────────── */
  .cmd-group{background:rgba(255,255,255,.022)!important;border:1px solid var(--dk-line)!important;
    border-radius:13px!important;box-shadow:none!important}
  .cmd-legend{color:var(--dk-t3)!important;letter-spacing:.15em!important;font-weight:700}
  .cmd-bar button{border-radius:9px!important}

  /* ── 카드: 납작한 사각형 → 빛이 위에서 오는 판 ─────────────────────── */
  #cards > div, .stack-wrap > div{border-radius:15px}
  [id^="card-"]{
    background:linear-gradient(178deg,var(--dk-ink1),#080b14)!important;
    border-color:var(--dk-line)!important;border-radius:15px!important;
    box-shadow:0 14px 34px -22px rgba(0,0,0,.95),inset 0 1px 0 rgba(255,255,255,.045)!important;
    transition:border-color .18s,box-shadow .18s,transform .18s;
  }
  [id^="card-"]:hover{border-color:var(--dk-line2)!important;transform:translateY(-1px)}
  /* ★이상 상태 = 테두리 대신 윗면에서 빛이 샌다★ — 20장이 액자처럼 안 보인다 */
  [id^="card-"]::before{content:'';position:absolute;left:0;right:0;top:0;height:86px;
    pointer-events:none;border-radius:15px 15px 0 0;opacity:0;transition:opacity .2s;
    background:linear-gradient(180deg,var(--dk-bleed,transparent),transparent)}
  [id^="card-"].bleed::before{opacity:1}
  [id^="card-"].bleed::after{content:'';position:absolute;left:15px;right:15px;top:0;height:1px;
    pointer-events:none;background:linear-gradient(90deg,transparent,var(--dk-edge),transparent);opacity:.9}
  /* 선택 = 앰버 링 (기존 card-sel 위에 덧씌움) */
  .card-sel{box-shadow:0 0 0 1px rgba(242,181,60,.55),0 14px 34px -22px rgba(0,0,0,.95),
    inset 0 1px 0 rgba(255,255,255,.05)!important;border-color:rgba(242,181,60,.45)!important}
  /* 카드 안 숫자·창고키나 */
  [id^="card-"] .text-yellow-400{color:var(--dk-gold)!important}
  /* 슬롯 버튼줄 — 캡슐 */
  [id^="card-"] .slot-btn,[id^="card-"] [class*="slot"]{border-radius:7px}

  /* ── 스프레드 / 모달 표 ─────────────────────────────────────────────── */
  table thead th{background:#0d1322!important;color:var(--dk-t3)!important;
    border-bottom:1px solid var(--dk-line2)!important;letter-spacing:.08em}
  .acct-table thead th{background:#0d1322!important}
  .acct-pc{color:var(--dk-t1)!important;font-family:var(--dk-disp)}
  .acct-chip{background:rgba(242,181,60,.14)!important;color:var(--dk-gold)!important;
    font-family:var(--dk-disp)}
  .acct-plat{color:var(--dk-cyan)!important}
  .acct-cp:hover .acct-val{border-bottom-color:rgba(242,181,60,.6)!important}
  .acct-cp:hover::after{color:var(--dk-gold)!important;background:rgba(10,15,28,.94)!important}
  .acct-cp.acct-hit{background:rgba(61,220,154,.16)!important}
  .acct-cp.acct-hit .acct-val{color:var(--dk-mint)!important}

  /* ── 섹션 헤더: 네온 밑줄 → 잉크 라인 ── */
  main section h2::after{background:linear-gradient(90deg,var(--dk-line2),transparent)!important;
    opacity:1!important;width:220px!important}

  /* ── 상단바 버튼 ── */
  header button,header a{border-radius:9px!important}

/* ══════════════════════════════════════════════════════════════════════════
   ★빛샘 3등급 (2026-08-16 사용자 요청 "사냥중도 초록색 느낌")★
   위 .bleed 규칙은 손대지 않고 변수 두 개만 덧씌운다(원복하기 쉽게).
   높이·모서리선 세기를 JS(DK_TIER)가 넣어 주고, 없으면 예전 값(86px/.9)으로 돈다.
   ★알파만 바꾸지 말 것★ — 정상(초록) 카드가 15장이라 높이까지 같이 줄여야
   빨강 카드가 여전히 먼저 눈에 들어온다.
   ══════════════════════════════════════════════════════════════════════════ */
  [id^="card-"].bleed::before{height:var(--dk-bleed-h,86px)}
  [id^="card-"].bleed::after{opacity:var(--dk-edge-o,.9)}

/* ══════════════════════════════════════════════════════════════════════════
   ★히어로(2026-08-16)★ — 사용자: "아무리봐도 아까 사진처럼 나온 것 같진 않은데"
   맞는 지적이었다. 앞 단계는 팔레트·재질만 옮겼고, 미리보기의 인상을 만들던
   ★큰 요약 한 덩어리★가 빠져 있었다. 전광판을 갈아엎지 않고 그 위에 얹는다.
   ══════════════════════════════════════════════════════════════════════════ */
  .dk-hero{
    position:relative;overflow:hidden;border-radius:18px;
    border:1px solid var(--dk-line);
    background:linear-gradient(160deg,#0e1526 0%,#0a0f1c 58%,#080c17 100%);
    box-shadow:0 24px 60px -30px rgba(0,0,0,.9),inset 0 1px 0 rgba(255,255,255,.055);
    padding:26px 24px 24px;display:flex;align-items:center;gap:30px;flex-wrap:wrap}
  .dk-hero::after{content:'';position:absolute;left:14%;top:-90%;width:56%;height:190%;
    pointer-events:none;background:radial-gradient(closest-side,rgba(79,211,232,.15),transparent)}
  /* ★2026-08-16 사용자 지시★ "오른쪽 요약을 제외하고 가운데쯤 위치하게 / 더 크게 /
     좌우로 길게 / 글씨체 간지나는 걸로 팍팍"
     → 왼쪽 세로선 인용 표시를 버리고, 남은 폭 전체를 차지하는 ★가운데 정렬 헤드라인★.
       flex:1 이라 오른쪽 요약(dk-hero-side)이 가져간 폭을 뺀 나머지의 정중앙에 선다. */
  .dk-hero-main{position:relative;z-index:1;flex:1 1 460px;min-width:280px;max-width:none;
    text-align:center;padding:2px 4px}
  .dk-eyebrow{font-size:9.5px;letter-spacing:.24em;color:var(--dk-t3);font-weight:700}
  /* 눈썹 양옆 실선 — 가운데 정렬이 허전하지 않게 잡아준다 */
  .dk-eyebrow{display:flex;align-items:center;justify-content:center;gap:12px}
  .dk-eyebrow::before,.dk-eyebrow::after{content:'';height:1px;width:min(90px,12%);
    background:linear-gradient(90deg,transparent,var(--dk-line-s,rgba(255,255,255,.16)),transparent)}
  /* ★오늘의 한마디★ — 이 칸의 주인공. Black Han Sans = 굵고 각진 헤드라인체. */
  .dk-quote{
    margin:14px auto 0;font-family:'Black Han Sans',var(--dk-disp),sans-serif;
    font-size:clamp(30px,3.7vw,56px);font-weight:400;
    line-height:1.18;letter-spacing:-.012em;color:var(--dk-t0);
    max-width:30ch;text-wrap:balance;word-break:keep-all;
    text-shadow:0 3px 30px rgba(79,211,232,.12)}
  /* 강조 단어 — ★nowrap★ 을 빼면 balance 가 "안 / 되고" 처럼 금색 구절을 반으로 쪼갠다 */
  .dk-quote em{font-style:normal;color:var(--dk-gold-s);white-space:nowrap;
    text-shadow:0 3px 26px rgba(242,181,60,.30)}
  .dk-quote-by{margin-top:13px;font-family:'Nanum Myeongjo',serif;font-size:13px;
    color:var(--dk-t3);letter-spacing:.03em}
  .dk-quote-by b{color:var(--dk-t2);font-weight:700}
  .dk-quote-by:empty{display:none}   /* 출처 없는 문장에서 빈 여백이 안 생기게 */
  @media(max-width:1100px){.dk-quote{max-width:22ch}}
  .dk-hero-side{position:relative;z-index:1;margin-left:auto;display:flex;gap:24px;align-items:flex-end}
  .dk-sm{text-align:right}
  .dk-sm .k{font-size:9px;letter-spacing:.15em;color:var(--dk-t3);font-weight:700}
  .dk-sm .v{font-family:var(--dk-disp);font-variant-numeric:tabular-nums;
    font-size:24px;font-weight:700;margin-top:3px;line-height:1;color:var(--dk-t0)}
  .dk-sm .v i{font-size:12px;color:var(--dk-t2);font-style:normal;margin-left:2px}
  .dk-sm.gold .v{color:var(--dk-gold)}
  @media(max-width:760px){.dk-hero{gap:16px}.dk-hero-side{margin-left:0;gap:16px}
    .dk-hero-n{font-size:44px}}

/* ══════════════════════════════════════════════════════════════════════════
   ★덱 테마 2단계 — 캐릭터 스프레드 (2026-08-16)★
   마크업·onclick·정렬 함수는 하나도 안 건드렸다. 전부 CSS 덮어쓰기다.
   근거가 되는 마크업(고치면 아래 선택자가 죽는다):
     · 판  : <section class="bg-gray-900 rounded-xl p-5 border border-gray-800">
     · 표틀: #char-table-wrap > div.overflow-x-auto > table
     · PC 그룹 헤더줄 : <tr class="bg-gray-700/80 cursor-pointer" onclick=togglePcGroup>
       → CSS 에선 슬래시를 이스케이프해서 .bg-gray-700\/80 으로 잡는다.
     · 그룹마다 반복되는 열이름줄 : <tr data-pc=".."> 안이 <th>
     · 실제 데이터줄             : <tr data-pc=".."> 안이 <td>
       (renderCharTable 이 renderRow 결과에 data-pc 를 끼워 넣는다)
     · 경고줄 = .bg-red-950\/40 / 줄무늬 = .bg-gray-900, .bg-gray-800\/50
   되돌리기: 이 주석부터 스타일 블록 끝까지만 지우면 어제 모습으로 돌아간다.
   ★주의★ CSS 주석 안에도 스타일 닫는 태그를 절대 쓰지 말 것 — HTML 파서가 그 자리에서
   스타일을 끝내 버려서 뒤쪽 CSS 전부가 본문에 글자로 쏟아진다(이 블록 작성 중 실제 발생).
   ══════════════════════════════════════════════════════════════════════════ */

  /* ── 표를 담은 판 (캐릭터 현황 + 최근 명령 내역 둘 다) ───────────────── */
  main section.bg-gray-900{
    background:linear-gradient(178deg,var(--dk-ink1),#080b14)!important;
    border-color:var(--dk-line)!important;border-radius:15px!important;
    box-shadow:0 18px 44px -30px rgba(0,0,0,.95),inset 0 1px 0 rgba(255,255,255,.045)!important}
  main section.bg-gray-900 h2{color:var(--dk-t2)!important;letter-spacing:.13em}
  main section.bg-gray-900 h2:hover{color:var(--dk-t1)!important}
  #char-table-arrow{display:inline-block;width:11px;color:var(--dk-gold);font-size:10px}
  #char-table-count{color:var(--dk-t3)!important;
    font-family:var(--dk-disp);font-variant-numeric:tabular-nums}
  /* 우상단 도구 버튼(새로고침·전체열기·인쇄) — 무채색, 손대면 앰버 */
  main section.bg-gray-900 button.bg-gray-800{
    background:rgba(255,255,255,.04)!important;border:1px solid var(--dk-line);
    border-radius:8px!important;color:var(--dk-t2)!important;font-weight:600}
  main section.bg-gray-900 button.bg-gray-800:hover{
    background:rgba(242,181,60,.10)!important;border-color:rgba(242,181,60,.42);
    color:var(--dk-gold-s)!important}
  /* 검색칸 */
  #char-filter{background:rgba(255,255,255,.035)!important;border:1px solid var(--dk-line)!important;
    border-radius:9px!important;color:var(--dk-t0)!important}
  #char-filter::placeholder{color:var(--dk-t3)}
  #char-filter:focus{border-color:rgba(242,181,60,.55)!important;
    box-shadow:0 0 0 3px rgba(242,181,60,.10)}
  #cmd-history{color:var(--dk-t2)!important}

  /* ── 표틀: 잉크 접시 하나로 묶고 세로 스크롤을 준다 ───────────────────
     ★행동 변화 1건★ — thead 에는 원래 sticky top-0 이 붙어 있었지만 감싼 div 에
     높이가 없어 실제로는 고정되지 않았다. max-height 를 줘서 표 안에서 스크롤되게
     하면 비로소 머리줄이 붙는다. 예전처럼 페이지 전체 스크롤로 돌리려면
     아래 max-height/overflow-y 두 줄만 지우면 된다. */
  #char-table-wrap > .overflow-x-auto{
    max-height:min(74vh,900px);overflow-y:auto;
    border:1px solid var(--dk-line);border-radius:12px;background:var(--dk-ink0);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.035)}
  #char-table-wrap > .overflow-x-auto::-webkit-scrollbar{width:9px;height:9px}
  #char-table-wrap > .overflow-x-auto::-webkit-scrollbar-track{background:transparent}
  #char-table-wrap > .overflow-x-auto::-webkit-scrollbar-thumb{
    background:var(--dk-line2);border-radius:6px;border:2px solid var(--dk-ink0)}
  #char-table-wrap > .overflow-x-auto::-webkit-scrollbar-thumb:hover{background:#46567a}

  /* ── 머리줄: 잉크 배경 + 실제로 붙는 sticky ── */
  #char-table-wrap thead th{
    position:sticky;top:0;z-index:4;
    background:#0b1018!important;color:var(--dk-t2)!important;
    font-weight:600;letter-spacing:.05em;
    border-bottom:1px solid var(--dk-line2)!important;
    box-shadow:0 1px 0 rgba(0,0,0,.6)}
  #char-table-wrap thead th[onclick]{cursor:pointer}
  #char-table-wrap thead th:hover{color:var(--dk-gold-s)!important}

  /* ── PC 그룹 헤더: 덩어리의 뚜껑 ───────────────────────────────────── */
  #char-tbody tr.bg-gray-700\/80 > td{
    background:linear-gradient(90deg,#17203a,#121a2c 55%,#0e1424)!important;
    border-top:1px solid var(--dk-line2)!important;
    border-bottom:1px solid var(--dk-line)!important;
    box-shadow:inset 3px 0 0 var(--dk-gold);
    padding-top:.5rem!important;padding-bottom:.5rem!important;
    color:var(--dk-t0)!important}
  #char-tbody tr.bg-gray-700\/80:hover > td{
    background:linear-gradient(90deg,#1c2748,#141d33 55%,#101728)!important}
  #char-tbody [id^="pc-arrow-"]{display:inline-block;width:11px;font-size:10px;color:var(--dk-gold)}
  /* 뚜껑에 달린 꼬리표들 — 버튼(중첩 div 안)은 안 건드리도록 > span 으로만 잡는다 */
  #char-tbody tr.bg-gray-700\/80 > td > div > span{
    font-family:var(--dk-disp);font-variant-numeric:tabular-nums;letter-spacing:.01em}
  #char-tbody tr.bg-gray-700\/80 > td > div > span.text-purple-300{color:var(--dk-t2)!important}
  #char-tbody tr.bg-gray-700\/80 > td > div > span.text-gray-500{color:var(--dk-t3)!important}
  #char-tbody tr.bg-gray-700\/80 > td > div > span.text-cyan-400{color:var(--dk-t2)!important}
  #char-tbody tr.bg-gray-700\/80 > td > div > span.text-yellow-300{color:var(--dk-gold)!important}
  #char-tbody tr.bg-gray-700\/80 > td > div > span.text-red-400{color:var(--dk-coral)!important;font-weight:700}
  /* 버튼 묶음 사이의 '|' 구분자 — 버튼 div 안에 있으므로 따로 잡는다 */
  #char-tbody tr.bg-gray-700\/80 > td > div > div > span.text-gray-600{color:var(--dk-line2)!important}
  /* ★뚜껑의 명령 버튼 14개 — 바탕만 무채색으로 눕히고 글자색(의미)은 그대로 둔다★
     20개 그룹이 펼쳐지면 색 버튼 300개가 표를 덮어서 정작 봐야 할 수치가 안 읽혔다. */
  #char-tbody tr.bg-gray-700\/80 button{
    background:rgba(255,255,255,.05)!important;border:1px solid var(--dk-line2);
    border-radius:7px!important;font-weight:600}
  #char-tbody tr.bg-gray-700\/80 button:hover{
    background:rgba(255,255,255,.13)!important;border-color:var(--dk-t3)}

  /* ── 그룹마다 반복되는 열이름줄 (tr[data-pc] 안의 th) ── */
  #char-tbody tr[data-pc] th{
    background:#0a0f1b!important;color:var(--dk-t3)!important;font-weight:600;
    letter-spacing:.05em;border-bottom:1px solid var(--dk-line)!important}

  /* ── 데이터줄 ──────────────────────────────────────────────────────── */
  #char-tbody tr{border-color:var(--dk-line)!important}
  #char-tbody tr.bg-gray-900{background:transparent!important}
  #char-tbody tr.bg-gray-800\/50{background:rgba(255,255,255,.022)!important}
  /* 개입 필요줄 — 빨간 판때기 대신 왼쪽에 코랄 한 줄 + 아주 옅은 틴트 */
  #char-tbody tr.bg-red-950\/40{background:rgba(255,93,110,.07)!important}
  #char-tbody tr.bg-red-950\/40 > td:first-child{box-shadow:inset 2px 0 0 var(--dk-coral)}
  #char-tbody tr[data-pc]:hover > td{background:rgba(242,181,60,.07)}
  #char-tbody tr[data-pc] > td{border-bottom:1px solid rgba(26,34,55,.5)}

  /* 숫자 열만 표시체 + 자릿수 고정 (이름·직업·정기추출·아르카나·장비는 제외) */
  #char-tbody tr[data-pc] > td:nth-child(1),#char-tbody tr[data-pc] > td:nth-child(2),
  #char-tbody tr[data-pc] > td:nth-child(6),#char-tbody tr[data-pc] > td:nth-child(7),
  #char-tbody tr[data-pc] > td:nth-child(8),#char-tbody tr[data-pc] > td:nth-child(9),
  #char-tbody tr[data-pc] > td:nth-child(10),#char-tbody tr[data-pc] > td:nth-child(11),
  #char-tbody tr[data-pc] > td:nth-child(12),#char-tbody tr[data-pc] > td:nth-child(13),
  #char-tbody tr[data-pc] > td:nth-child(14),#char-tbody tr[data-pc] > td:nth-child(18),
  #char-tbody tr[data-pc] > td:nth-child(19),#char-tbody tr[data-pc] > td:nth-child(20),
  #char-tbody tr[data-pc] > td:nth-child(21),#char-tbody tr[data-pc] > td:nth-child(22),
  #char-tbody tr[data-pc] > td:nth-child(23){
    font-family:var(--dk-disp);font-variant-numeric:tabular-nums;letter-spacing:-.01em}

  /* 평상시는 무채색 — 색이 정보를 나를 때만 남긴다.
     ★열 번호로 잡는 이유★ — 4열(직업)의 classColors 가 궁성=green-400 / 검성=orange-400 /
     마도성=cyan-400 … 로 수치 열과 ★같은 Tailwind 클래스★를 쓴다. .text-cyan-400 처럼
     클래스만 보고 칠하면 직업 색까지 같이 죽는다(작업 중 실제로 그랬다). 그래서 열 번호로
     한정하고 4열(직업)·15열(정기추출)은 손대지 않는다. */
  #char-tbody tr[data-pc] > td:nth-child(2){color:var(--dk-t2)!important}   /* # */
  #char-tbody tr[data-pc] > td:nth-child(3){color:var(--dk-t0)!important;font-weight:600} /* 이름 */
  #char-tbody tr[data-pc] > td:nth-child(6){color:var(--dk-t1)!important}   /* 장비전투력 */
  #char-tbody tr[data-pc] > td:nth-child(7){color:var(--dk-t0)!important}   /* 파워전투력 */
  #char-tbody tr[data-pc] > td:nth-child(8){color:var(--dk-t1)!important}   /* 오드에너지 */
  #char-tbody tr[data-pc] > td:nth-child(18),
  #char-tbody tr[data-pc] > td:nth-child(19){color:var(--dk-t1)!important}  /* 각인·거래키나 */
  #char-tbody tr[data-pc] > td:nth-child(21),
  #char-tbody tr[data-pc] > td:nth-child(22){color:var(--dk-t2)!important}  /* 어비스 */
  /* 돈의 총합 한 줄만 앰버 / 완료는 민트 / 개입은 코랄 */
  #char-tbody tr[data-pc] > td:nth-child(20){color:var(--dk-gold)!important}          /* 창고키나 */
  #char-tbody tr[data-pc] > td:nth-child(23).text-sky-300{color:var(--dk-t2)!important}   /* 회랑 미완 */
  #char-tbody tr[data-pc] > td:nth-child(23).text-green-400{color:var(--dk-mint)!important}/* 회랑 완주 */
  #char-tbody tr[data-pc] > td .text-red-400{color:var(--dk-coral)!important}
  #char-tbody tr[data-pc] > td .text-pink-400{color:var(--dk-t2)!important}
  #char-tbody input[type="checkbox"]{accent-color:var(--dk-mint)}
  /* 줄 안의 작은 것들: 📡 수집 버튼 / 보기 링크 */
  #char-tbody tr[data-pc] > td button{
    background:rgba(255,255,255,.05)!important;border:1px solid var(--dk-line2);
    border-radius:7px!important;color:var(--dk-t2)!important}
  #char-tbody tr[data-pc] > td button:hover{
    background:rgba(79,211,232,.16)!important;color:var(--dk-cyan)!important}
  #char-tbody tr[data-pc] > td a{color:var(--dk-t2)!important;
    text-decoration-color:var(--dk-t3)}
  #char-tbody tr[data-pc] > td a:hover{color:var(--dk-gold-s)!important;
    text-decoration-color:var(--dk-gold)}

/* ══════════════════════════════════════════════════════════════════════════
   ★덱 테마 3단계 — 모달 (2026-08-16)★
   겉모습만. id·onclick·열고닫는 JS(classList hidden) 전부 그대로다.
   ══════════════════════════════════════════════════════════════════════════ */
  /* 마스크: 검은 장막 → 짙은 잉크 + 블러 */
  #log-modal,#vietnam-modal,#bug-modal,#liveModal,#rental-modal,#acct-modal,#info-modal{
    background:rgba(4,6,12,.72)!important;
    -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
  /* 판: 위 1px 하이라이트 + 깊은 그림자 */
  #log-modal > div,#vietnam-modal > div,#bug-modal > div,#liveModal > div,
  #rental-modal > div,#acct-modal > div,#info-modal > div,#voice-panel{
    background:linear-gradient(178deg,var(--dk-ink1),#070a12)!important;
    border-color:var(--dk-line)!important;
    box-shadow:0 40px 90px -42px rgba(0,0,0,1),
               inset 0 1px 0 rgba(255,255,255,.05)!important}
  /* 가운데 뜨는 판은 카드와 같은 반경 / 옆에서 나오는 서랍은 각지게 둔다 */
  #vietnam-modal > div,#bug-modal > div,#liveModal > div,
  #rental-modal > div,#acct-modal > div{border-radius:15px!important}
  #voice-panel{border-radius:13px!important}
  /* 머리줄·바닥줄 구분선 */
  #log-modal .border-b,#vietnam-modal .border-b,#bug-modal .border-b,#liveModal .border-b,
  #rental-modal .border-b,#acct-modal .border-b,#info-modal .border-b{
    border-bottom-color:var(--dk-line)!important}
  #log-modal .border-t,#liveModal .border-t,#info-modal .border-t{
    border-top-color:var(--dk-line)!important}
  /* 제목: 알록달록한 색 이름표 → 무채색. 단 버그 모달만 코랄(개입 신호) */
  #log-modal h2,#vietnam-modal h2,#liveModal h2,#acct-modal h2,#info-modal h2{
    color:var(--dk-t0)!important;font-weight:700;letter-spacing:.01em}
  #bug-modal h2,#rental-modal h2{color:var(--dk-coral)!important;font-weight:700}
  #acct-count{color:var(--dk-t3)!important;font-family:var(--dk-disp)}
  /* 모달 안 버튼: 반경 한 체계 + 무채색 바탕(글자색=의미는 유지) */
  #log-modal button,#vietnam-modal button,#bug-modal button,#liveModal button,
  #rental-modal button,#acct-modal button,#info-modal button,#voice-panel button{
    border-radius:8px!important}
  #log-modal [class*="bg-gray-7"],#bug-modal [class*="bg-gray-7"],
  #acct-modal button[class*="bg-gray-7"],#info-modal button[class*="bg-gray-7"]{
    background:rgba(255,255,255,.05)!important;border:1px solid var(--dk-line2)}

  /* ── 계정 세부정보 표 ── */
  .acct-table thead th{color:var(--dk-t3)!important;letter-spacing:.06em;
    border-bottom:1px solid var(--dk-line2)!important}
  .acct-td{border-bottom:1px solid rgba(26,34,55,.6)!important}
  .acct-row.acct-group td{border-top:1px solid var(--dk-line2)!important}
  .acct-row:hover td{background:rgba(242,181,60,.055)!important}
  .acct-id{color:var(--dk-t0)!important}
  .acct-em,.acct-ph{color:var(--dk-t2)!important}
  .acct-ph,.acct-n{font-family:var(--dk-disp);font-variant-numeric:tabular-nums}

  /* ── 캐릭터 세부정보(서랍) ── */
  #info-content > div{
    background:linear-gradient(180deg,#101627,#0a0f1a)!important;
    border-color:var(--dk-line)!important;border-radius:13px!important;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.04)!important}
  #info-content .bg-gray-750{background:rgba(255,255,255,.035)!important}
  #info-content .border-gray-700,#info-content .border-gray-800\/60{
    border-color:var(--dk-line)!important}
  #info-content .text-indigo-400{color:var(--dk-gold)!important;
    font-family:var(--dk-disp)}
  #info-content .text-gray-500{color:var(--dk-t3)!important}
  #info-content .text-gray-100{color:var(--dk-t0)!important}
  #info-content .text-gray-200{color:var(--dk-t0)!important;
    font-family:var(--dk-disp);font-variant-numeric:tabular-nums}
  #info-content .text-yellow-300{color:var(--dk-gold)!important;
    font-family:var(--dk-disp);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
  #info-collected-at{color:var(--dk-t3)!important;font-family:var(--dk-disp)}

  /* ── 버그 스크린샷 목록 ── */
  #bug-list > div{background:rgba(255,255,255,.035)!important;
    border-color:var(--dk-line)!important;border-radius:11px!important}
  #bug-list .font-mono{color:var(--dk-t2)!important}
  #bug-list .text-gray-600{color:var(--dk-t3)!important}
  #bug-clear-btn{background:rgba(255,93,110,.12)!important;
    border:1px solid rgba(255,93,110,.35);color:var(--dk-coral)!important}
  #bug-clear-btn:hover{background:rgba(255,93,110,.22)!important}
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
    <!-- ★★AI 던전 추천 (2026-08-22 주인님 지시)★★
         원문: "직원들이 존나 헷갈려하고있어 오늘 어떤 캐릭터의 던전을 돌아야할지
                그래서 내가 그걸 일일이 얘기해주는건 너무 번잡하고 내가 부재일때가 있으니까"
         → 사람이 매일 불러주던 판단을 화면이 대신한다. 기준은 주인님이 준 그대로:
           ①일일 에너지(앞 숫자)를 먼저 태운다  ②구독 계정이 2배 효율  ③파워 30만+ 만 투입 -->
    <button id="ai-btn" onclick="openAiPlan()" class="px-3 py-1 rounded-lg text-xs font-extrabold bg-gradient-to-r from-fuchsia-600 to-indigo-600 hover:from-fuchsia-500 hover:to-indigo-500 text-white transition-colors whitespace-nowrap shadow" title="오늘 어느 계정의 어느 캐릭으로 던전을 돌면 좋은지 — 지금 정보수집 상태 기준">🤖 AI</button>
    <button id="tts-btn" onclick="toggleTts()" class="px-3 py-1 rounded-lg text-xs font-semibold bg-gray-700/70 hover:bg-gray-600 text-gray-300 transition-colors whitespace-nowrap">🔇 음성 꺼짐</button>
    <button onclick="toggleVoicePanel()" class="px-2 py-1 rounded-lg text-xs bg-gray-700/70 hover:bg-gray-600 text-gray-300 transition-colors" title="설정 — 파섹 계정 · 목소리">⚙ 설정</button>
    <a href="#" onclick="window.open('/manual?t='+Date.now(),'_blank');return false;" class="px-3 py-1 rounded-lg text-xs font-semibold bg-indigo-800/70 hover:bg-indigo-600 text-indigo-100 transition-colors whitespace-nowrap" title="이용 매뉴얼 PDF 열기 / 내려받기 (항상 최신본)">📘 매뉴얼</a>
    <a href="/updater.exe" class="px-3 py-1 rounded-lg text-xs font-semibold bg-teal-800/70 hover:bg-teal-600 text-teal-100 transition-colors whitespace-nowrap" title="설치용 업데이터 내려받기 — 로그인 계정에 맞는 파일명으로 받아집니다 (본판 updater.exe / 렌탈 rental_updater.exe)">⬇ 업데이터</a>
    <button onclick="openAcctModal()" class="px-3 py-1 rounded-lg text-xs font-semibold bg-violet-900/70 hover:bg-violet-700 text-violet-100 transition-colors whitespace-nowrap" title="계정 세부정보 — PC별 계정 아이디·이메일·휴대폰 (info.txt 에서 모아옵니다)">📇 계정정보</button>
<!-- ★[🛑 렌탈] 버튼 제거 (2026-08-16 → 2026-08-23 렌탈 사업 폐기)★
     ★킬스위치 자체는 살아 있다★ — 버튼(화면)만 뺀 것이지 차단을 푼 게 아니다.
     ★모달·서버 코드는 지우지 않는다★ (48h 유예 함정 — 지우면 휴면 테넌트가 되살아난다).
     되살리려면 이 자리에 버튼 한 줄만 다시 붙이면 된다
     (openRentalModal · #rental-modal · loadRentalTenants 전부 남아 있고,
      loadRentalTenants 는 btn 이 없으면 아무 것도 안 하는 가드가 있어 무해하다).

     ★★2026-08-24 수리 — 여기 주석이 ★중첩★ 돼 있었다★★
     HTML 주석은 중첩이 안 된다. 안쪽 주석의 닫는 태그가 바깥 주석까지 닫아버려
     그 뒤 문장이 ★대시보드 화면에 그대로 출력★ 됐다(주인님 발견). 게다가 새어나온 줄이
     하필 rental_kill 엔드포인트 사용법이라 ★운영 정보가 화면에 노출★ 됐다.
     → HTML 주석 안에 주석 기호를 절대 쓰지 않는다(이 문장을 쓰면서 또 그랬다).
     조작법은 파이썬 코드 주석(파일 상단 83~85행)에만 둔다. -->
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
    <button onclick="switchAccountSelected()" class="chip chip-purple"
            title="선택한 PC들을 한꺼번에 지정 계정(1~4)으로 통짜 전환 — 각 PC가 ★본컴 런처(파섹) → 원격컴 크롬 → 매크로 재시작★ 까지 (물리 PC당 1건, 이미 그 계정인 PC는 제외, 대당 1~2분)">🔁 계정전환</button>
  </div>

  <!-- ★그룹 3: 전 계정 순환 (2026-08-23 주인님 지시)★
       "일일던전 악몽 각성 회랑 정보수집은 다 피씨의 전체계정순환으로 되야할거야"
       한 계정에서 작업이 끝나면 서버가 스스로 다음 계정으로 통짜 전환해 또 시킨다.
       계정을 한 바퀴 다 돌면 자동 종료(텔레그램 ✅). ★물리 PC당 1건★ 으로 접어 보낸다. -->
  <div class="cmd-group cmd-rot">
    <span class="cmd-legend">🔁 전 계정 순환 (PC의 계정 전부)</span>
    <button onclick="rotCmd('daily_dungeon')" class="chip chip-purple" title="선택 PC의 ★모든 계정★ 을 돌며 일일던전. 한 계정이 끝나면 자동으로 다음 계정으로 전환합니다">일일던전</button>
    <button onclick="rotCmd('nightmare')" class="chip chip-pink" title="선택 PC의 ★모든 계정★ 을 돌며 악몽">악몽</button>
    <button onclick="rotCmd('awakening')" class="chip chip-orange" title="선택 PC의 ★모든 계정★ 을 돌며 각성전">각성</button>
    <button onclick="rotCmd('corridor')" class="chip chip-blue" title="선택 PC의 ★모든 계정★ 을 돌며 어비스 회랑">회랑</button>
    <button onclick="rotCmd('collect_info')" class="chip chip-sky" title="선택 PC의 ★모든 계정★ 을 돌며 캐릭터 정보수집">정보수집</button>
  </div>

  <!-- 그룹 4: 선택 카드만 (예전 CONTENT — 그 계정 한 번, 순환 없음) -->
  <div class="cmd-group cmd-one">
    <span class="cmd-legend">🎯 선택한 카드 1개만</span>
    <button onclick="selCmd('daily_dungeon')" class="chip chip-purple">일일던전</button>
    <button onclick="selCmd('nightmare')" class="chip chip-pink">악몽</button>
    <button onclick="selCmd('awakening')" class="chip chip-orange">각성</button>
    <select id="awaken-preset" onchange="setAwakenPreset(this.value)" class="chip chip-orange"
            title="각성전 난이도 프리셋 — 저장 즉시 전 PC 다음 입장부터 적용"
            style="appearance:auto;background:rgba(249,115,22,.12);cursor:pointer">
      <option value="default">난이도: 기본 (20/30/50/60만)</option>
      <option value="hard_up">난이도: 상향 (15/25/40/55만)</option>
    </select>
    <button onclick="selCmd('abyss')" class="chip chip-blue">어비스</button>
    <button onclick="selCmd('corridor')" class="chip chip-blue" title="어비스 회랑 순회 — 전 캐릭 (하층+중층 우리 진영 아티팩트), 진입=완료, 미완만 재개">회랑</button>
    <button onclick="selCmd('collect_info')" class="chip chip-sky">정보수집</button>
  </div>

  <!-- 그룹 4: 거래 -->
  <div class="cmd-group">
    <span class="cmd-legend">TRADE</span>
    <!-- 가격은 프리셋만(2026-07-30) — 매크로가 이 6종의 이미지 템플릿으로 입력을 검증한다.
         자유 입력을 열면 템플릿이 없는 가격이 들어와 검증이 OCR로 떨어진다. -->
    <span class="price-wrap"><span class="price-k">₭</span><select id="sale-price" title="거래소 등록 가격 (전체 공통) — 확정하면 사이트 닫았다 열어도 유지">
      <option value="">가격 선택</option>
      <option value="159999">159,999</option>
      <option value="169999">169,999</option>
      <option value="179999">179,999</option>
      <option value="189999">189,999</option>
      <option value="199999">199,999</option>
      <option value="209999">209,999</option>
    </select></span>
    <button id="sale-price-btn" onclick="toggleSalePrice()" class="chip chip-gray">확정</button>
    <button onclick="sellAllSel()" class="chip chip-yellow">판매</button>
    <button onclick="settleSel()" class="chip chip-amber" title="전 캐릭 준비 — 정산(계정 1회) → 추출 → 개인/서버창고 보관 → 인벤정렬 → 귀환주문서 보충">준비</button>
  </div>

  <!-- 그룹 5: 정리 -->
  <div class="cmd-group ml-auto">
    <span class="cmd-legend">CLEAN</span>
    <button onclick="clearAllBugs()" class="chip chip-gray" title="모든 PC의 버그 스크린샷을 서버에서 전부 삭제">🧹 스샷 비우기</button>
  </div>
</div>

<main class="p-4 sm:p-6 space-y-6">

  <!-- ★히어로(2026-08-16)★ — 전광판은 '숫자 7개'라 무엇부터 봐야 할지 안 알려준다.
       그 위에 ★지금 손봐야 할 게 몇 대인지★를 제일 크게 얹는다. 기존 전광판은
       그대로 두고(id 하나도 안 건드림) 위에 얹기만 하므로 되돌리기 쉽다.
       값은 renderCards 끝에서 dkHero()가 채운다 — 실패해도 화면은 멀쩡하다. -->
  <div class="dk-hero">
    <div class="dk-hero-main">
      <div class="dk-eyebrow" id="dk-q-day">오늘의 한마디</div>
      <blockquote class="dk-quote" id="dk-q-text">…</blockquote>
      <div class="dk-quote-by" id="dk-q-by"></div>
    </div>
    <div class="dk-hero-side">
      <div class="dk-sm"><div class="k">평균 효율</div><div class="v" id="dk-h-eff">–</div>
        <svg width="104" height="26" id="dk-spark" aria-hidden="true"></svg></div>
      <div class="dk-sm gold"><div class="k">창고 키나</div><div class="v" id="dk-h-kina">–</div></div>
      <div class="dk-sm"><div class="k">완료 캐릭</div><div class="v" id="dk-h-done">–</div></div>
    </div>
  </div>

  <!-- 전광판 (순서: 온라인 → 완료 → 오드에너지 → 각성전 → 일일던전 → 거래키나 → 창고키나)
       — 열 폭은 .stat-grid(숫자 긴 오드에너지/거래키나/창고키나=3fr, 나머지=2fr) -->
  <div class="stat-grid">
    <!-- ★캐릭터 수 기준(2026-08-07 사용자 요청)★ — PC 대수가 아니라 실제로 돌고 있는/끝낸
         캐릭터 수를 본다. 대수는 툴팁과 아래 '온라인' 섹션 헤더에 남아 있다. -->
    <div class="stat-tile tile-green">
      <div class="stat-icon">🖥️</div>
      <div class="stat-num text-green-400" id="cnt-online" title="함대가 굴리는 전체 캐릭터 수 (뒷카드·오프라인 PC 포함)">0</div>
      <div class="stat-label">캐릭터</div>
    </div>
    <div class="stat-tile tile-blue">
      <div class="stat-icon">✅</div>
      <div class="stat-num text-blue-400" id="cnt-completed" title="오늘 사냥을 끝낸 캐릭터 수 (오프라인 PC 포함, 새벽 5시 초기화)">0</div>
      <div class="stat-label">완료 캐릭</div>
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
    <div class="stat-tile tile-purple" title="일일던전(계정 티켓)이 남은 계정 수 — 🏰 뱃지 없는 PC">
      <div class="stat-icon">🏰</div>
      <div class="stat-num text-purple-400" id="cnt-dungeon-left">–</div>
      <div class="stat-label">일일던전 남음</div>
    </div>
    <div class="stat-tile tile-blue" title="회랑을 아직 다 못 돈 캐릭터 수 (적 진영 제외, 함대 합계 · 수·토 22시 리셋)">
      <div class="stat-icon">🌀</div>
      <div class="stat-num text-sky-400" id="cnt-corridor">–</div>
      <div class="stat-label">회랑 남음</div>
    </div>
    <div class="stat-tile tile-gold">
      <div class="stat-icon">🪙</div>
      <div class="stat-num text-yellow-400" id="cnt-trade-kina">–</div>
      <div class="stat-label">거래키나</div>
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
    <div id="grid-online" class="gap-3" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr))">
      <div class="text-gray-600 text-sm col-span-full text-center py-10">대기 중... (매크로 연결 없음)</div>
    </div>
  </section>

  <!-- 오프라인 섹션 -->
  <section id="offline-section" class="hidden">
    <h2 class="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-3 flex items-center gap-2">
      <span class="w-2 h-2 rounded-full bg-gray-600 inline-block"></span>
      오프라인 <span id="offline-count" class="text-gray-600 normal-case">(0)</span>
    </h2>
    <div id="grid-offline" class="gap-3" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr))"></div>
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
      <!-- ★스프레드 압축(2026-08-01 사용자: "회랑 열이 스크롤해야 보인다 — 좌우 안 넘치게")★
           23열이라 px-3(칸당 24px)만 552px를 먹는다. 셀 좌우 여백·글자를 CSS로 일괄 축소해
           1920 화면에 전 열이 들어가게 한다(td/th의 Tailwind px-3을 !important로 덮음). -->
      <style>
        #char-table-wrap table td, #char-table-wrap table th{
          padding-left:.3rem!important;padding-right:.3rem!important;font-size:.72rem}
      </style>
      <div class="overflow-x-auto">
        <table class="w-full text-left whitespace-nowrap">
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
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center" onclick="sortCharTable('return_scroll_count')">귀환 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white" onclick="sortCharTable('extract_level')">정기추출 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center">아르카나</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center">장비</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-right" onclick="sortCharTable('gakin_kina')">각인키나 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-right" onclick="sortCharTable('trade_kina')">거래키나 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-right" onclick="sortCharTable('total_kina')">창고키나 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center" onclick="sortCharTable('abyss_time')">어비스 ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-right" onclick="sortCharTable('abyss_point')">어비스P ⇅</th>
              <th class="px-3 py-2 cursor-pointer hover:text-white text-center" onclick="sortCharTable('corridor_progress')">회랑 ⇅</th>
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
    <!-- ★로그 출처 탭 (2026-08-20)★ 매크로가 죽으면 그 PC 가 실명이 된다 → 업데이터
         로그를 같이 본다. className 은 renderLogTabs() 가 통째로 덮으므로 여기 두지 않는다
         (마크업과 JS 두 군데에 색을 적으면 반드시 한쪽만 고쳐서 어긋난다). -->
    <div class="flex items-center gap-1 px-4 pt-3 pb-1 text-xs shrink-0">
      <button id="log-tab-both"  onclick="setLogSrc('both')">합침</button>
      <button id="log-tab-macro" onclick="setLogSrc('macro')">매크로</button>
      <button id="log-tab-upd"   onclick="setLogSrc('upd')">업데이터</button>
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
    <!-- ★★버튼 이름을 '실제로 하는 일' 로 바꿨다 (2026-08-23 주인님 지시)★★
         이름은 코드를 실측해서 붙였다 — 이름과 동작이 다르면 그게 거짓말이다.
           start   → loot.py: config.running = True            = 사냥을 시작한다
           stop    → loot.py: config.running = False (프로세스는 살아 있다) = 일시정지
           exit    → loot.py: should_exit + mark_no_restart    = 매크로만 종료
                     ★업데이터는 안 죽는다★ (그래서 원격으로 다시 켤 수 있다)
           restart → updater.py: stop_macro() → 2초 → start_macro()
                     꺼져 있으면 stop 은 무해하고 start 가 켠다 = ★주인님이 원한 그대로★ -->
    <button class="cm-btn chip-green"  onclick="cardCmd('start')"
            title="사냥을 시작합니다 (running=True). 프로그램이 이미 떠 있어야 합니다">▶ 사냥 시작</button>
    <button class="cm-btn chip-gray"   onclick="cardCmd('stop')"
            title="사냥만 멈춥니다 — 프로그램은 살아 있어서 ▶ 사냥 시작으로 바로 재개됩니다">⏸ 일시정지</button>
    <button class="cm-btn chip-red"    onclick="cardCmd('exit')"
            title="매크로 프로그램을 끕니다. ★업데이터는 안 끕니다★ — 그래서 ↺ 로 다시 켤 수 있습니다">✕ 프로그램 종료</button>
    <button class="cm-btn chip-yellow" onclick="updaterCmd('restart')"
            title="꺼져 있으면 켜고, 켜져 있으면 껐다 켭니다 (업데이터 경유 — 진짜 재기동)">↺ 껐다 켜기</button>
  </div>
  <div class="cm-sec">ACTION</div>
  <div class="cm-grid2">
    <!-- ★[⇄ 캐릭 전환...] 제거 (2026-08-23 주인님 지시 "안 쓸 것 같다")★
         함수 cardCmdSwitch() 는 남겨둔다 — 되살릴 때 버튼만 다시 붙이면 된다. -->
    <button class="cm-btn chip-yellow" onclick="sellAllFromMenu()" title="전 캐릭 순회 판매 (상단 거래소가 확정 필요)">$ 판매</button>
    <button class="cm-btn chip-amber"  onclick="settleFromMenu()" title="전 캐릭 준비 — 정산 → 추출 → 개인/서버창고 보관 → 인벤정렬 → 귀환주문서 보충">🧰 준비</button>
    <button class="cm-btn chip-cyan"   onclick="collectInfoFromMenu()">📡 정보수집</button>
    <button class="cm-btn chip-gray"   onclick="cardCmd('go_home')">⌂ 귀환</button>
    <!-- ★계정 버튼 = 본컴+원격컴 통짜 전환(2026-08-16 사용자 지시)★
         "계정1 2 3 이거는 없애고, 본컴 전환하면 원격컴에 원격 계정도 바꾸게끔"
         예전엔 원격컴 크롬만 바꾸는 switch_account 였는데, 본컴 런처가 그대로면
         ★짝이 안 맞아 스트림이 영영 안 뜬다★. 이제 한 번 누르면
         본컴 런처(파섹) → 원격컴 크롬 → 재시작 까지 이어서 간다.
         openCardMenu 가 열 때마다 있는 계정만 활성화 -->
    <button class="cm-btn chip-purple" id="cm-acct-1" onclick="switchAccountDirect(1)" title="★본컴 런처와 원격컴 크롬을 함께★ 계정 1 로 바꿉니다 (파섹 경유 → 게임 종료 → 계정 전환 → 게임 실행, 3~4분). ★이미 계정 1 로 보이는 카드에도 누를 수 있습니다★ — 본컴 런처를 직접 읽어 어긋난 짝을 맞춥니다">계정 1</button>
    <button class="cm-btn chip-purple" id="cm-acct-2" onclick="switchAccountDirect(2)" title="★본컴 런처와 원격컴 크롬을 함께★ 계정 2 로 바꿉니다 (파섹 경유 → 게임 종료 → 계정 전환 → 게임 실행, 3~4분). ★이미 계정 2 로 보이는 카드에도 누를 수 있습니다★ — 본컴 런처를 직접 읽어 어긋난 짝을 맞춥니다">계정 2</button>
    <button class="cm-btn chip-purple" id="cm-acct-3" onclick="switchAccountDirect(3)" title="★본컴 런처와 원격컴 크롬을 함께★ 계정 3 로 바꿉니다 (파섹 경유 → 게임 종료 → 계정 전환 → 게임 실행, 3~4분). ★이미 계정 3 로 보이는 카드에도 누를 수 있습니다★ — 본컴 런처를 직접 읽어 어긋난 짝을 맞춥니다">계정 3</button>
    <button class="cm-btn chip-purple" id="cm-acct-4" onclick="switchAccountDirect(4)" title="★본컴 런처와 원격컴 크롬을 함께★ 계정 4 로 바꿉니다 (파섹 경유 → 게임 종료 → 계정 전환 → 게임 실행, 3~4분). ★이미 계정 4 로 보이는 카드에도 누를 수 있습니다★ — 본컴 런처를 직접 읽어 어긋난 짝을 맞춥니다">계정 4</button>
    <!-- ★[🌐 크롬 제어모드 전환] 제거 (2026-08-23 주인님 지시)★
         함수 chromeCdpFromMenu() 와 chrome_cdp 원격명령은 남는다 —
         CDP 없는 PC 를 살릴 때 ops 스크립트로 여전히 쓴다. -->
    <!-- ★계정 순회(2026-08-17)★ — 계정을 바꾸면 매크로가 재시작되므로 순회는 한 프로세스
         안에 못 둔다. 매크로가 C:\auto\acct_tour.json 에 진행도를 남기고 부팅 때 이어받는다.
         그래서 이 버튼은 '시작 신호' 하나만 보내고, 진행은 텔레그램으로 온다. -->
    <!-- ★본컴 계정 찾기(2026-08-18)★ — 직원이 아무 계정이나 켜두면 원격컴은 어느 계정으로
         붙어야 할지 모른다. 이건 ★본컴을 건드리지 않고★ 원격컴 크롬 로비 계정 메뉴로만
         갈아타며 "호스트가 살아있는 계정"을 찾아 스트리밍 직전에서 멈춘다(계정당 20~30초).
         계정 순회(위)와 혼동 금지 — 저건 본컴 런처 교체+재시작이라 20~40분이다. -->
    <button class="cm-btn chip-amber cm-span2" onclick="findHostFromMenu()" title="본PC가 지금 어느 계정으로 켜져 있는지 원격컴 크롬만으로 찾습니다(1~2분). 찾으면 ★스트리밍 직전★ 상태로 세워두고 멈춥니다. 본PC 런처는 건드리지 않습니다.">🔎 본컴 계정 찾기</button>
    <!-- ★[🔄 계정 순회] 제거 (2026-08-23 주인님 지시)★ — 20~40분이 걸리고
         서버 자동순환(rotate)과 역할이 겹친다. 함수 acctTourFromMenu() 는 남는다. -->
  </div>
  <div class="cm-sec">VIEW</div>
  <div class="cm-grid3">
    <button class="cm-btn chip-indigo" onclick="openLogFromMenu()">📋 로그</button>
    <button class="cm-btn chip-sky"    onclick="openInfoFromMenu()">📊 정보</button>
    <button class="cm-btn chip-pink"   onclick="screenshotFromMenu()">📸 스샷</button>
  </div>
  <div class="cm-sec">화면 · 원격</div>
  <!-- ★한 줄 3칸 (2026-08-23 주인님 지시)★ — "화면 내부망원격 파섹웹 이거 보기 안좋아
       줄나눠줘서 … 화면, 원격, 파섹 이렇게 이름 바꾸고 한줄에 나오게해"
       긴 이름은 title 로 내렸다 — 버튼은 짧게, 설명은 마우스를 올리면 나온다. -->
  <div class="cm-grid3">
    <button class="cm-btn chip-emerald" onclick="liveFromMenu()" title="실시간 화면 — 어디서나 됨 (Railway 경유, 960x540 · 3fps)">🖵 화면</button>
    <button class="cm-btn chip-teal" id="cm-lan" onclick="lanFromMenu()" title="내부망 직결 원격 — 원본 해상도 + 마우스/키보드 조작 (같은 내부망에서만)">⚡ 원격</button>
    <button class="cm-btn chip-violet" id="cm-parsec-web" onclick="parsecWebFromMenu()" title="파섹 웹으로 ★본컴★ 보기 — 새 탭으로 열립니다. 탭을 여러 개 열면 여러 대를 동시에 볼 수 있습니다 (설치 불필요, 크롬 전용, H.264)">🌐 파섹</button>
    <!-- ★[🎮 파섹 앱] 제거 (2026-08-23 주인님 지시)★ — 창이 한 번에 하나뿐이라
         여러 대를 동시에 못 본다. 본컴 보기는 [🌐 파섹 웹] 하나로 통일. -->
  </div>
  <!-- ★[🖥 본컴 화면 받기] 제거 (2026-08-23 주인님 지시)★ — [🌐 파섹 웹] 이 같은
       화면을 더 빠르게 보여준다. 함수 bonComViewFromMenu() 와 chrome_view 원격명령은
       그대로 남는다(매크로가 자동화 안에서 쓴다). -->
  <!-- ★[🔄 본컴 계정 전환] 제거(2026-08-16)★ — 위 ACTION 의 [계정 1~4] 가 같은 일을
       ★더 정확하게★ 한다(목표 계정 번호를 알아 이메일 줄 템플릿으로 찾는다).
       이 버튼은 '몇 번째 줄'만 물어봐서 acct_no 가 안 실렸고, 실제로 계정1 을 원하는데
       계정2 로 가는 사고를 냈다(2026-08-16 로그: 대상=계정#1(계정None)). -->
  <div class="cm-sec">UPDATER · 프로세스</div>
  <!-- ★[▶ ■ ✕ ↺] 아이콘 4개 제거 (2026-08-23 주인님 지시)★
       · ↺ 는 위 MACRO 의 [↺ 재시작] 과 ★완전히 같은 함수★ updaterCmd('restart') 였다
       · ▶ ■ 는 MACRO 의 [▶시작] [■정지] 와 역할이 겹쳐 직원이 헷갈렸다
       · ✕ 는 ★업데이터 자체를 끄는★ 버튼이라 잘못 누르면 그 PC 원격제어가 끊긴다
         — 없애는 것이 사고 예방이다
       업데이트 두 줄([↑ 업데이트+재시작] [⬆ 업데이트만])은 그대로 남는다. -->
  <div class="cm-grid2">
    <button class="cm-btn chip-cyan cm-span2"   onclick="updaterCmd('update')">↑ 업데이트+재시작</button>
    <button class="cm-btn chip-purple cm-span2" onclick="updaterCmd('update_only')">⬆ 업데이트만</button>
  </div>
  <button class="cm-danger" onclick="deletePCFromMenu()">🗑 이 PC를 목록에서 삭제</button>
</div>

<!-- 버그 모달 -->
<!-- ★★AI 던전 추천 모달 (2026-08-22)★★ 기본 언어 = 베트남어(직원분들), 한국어 전환 가능 -->
<div id="ai-modal" class="hidden fixed inset-0 bg-black/75 z-50 flex items-center justify-center p-4">
  <div class="bg-gray-900 rounded-2xl shadow-2xl border border-gray-800 w-full max-w-4xl max-h-[92vh] flex flex-col">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-800 shrink-0">
      <div class="flex items-center gap-2 flex-wrap">
        <h2 class="font-extrabold text-lg text-fuchsia-300" id="ai-title">🤖 Hầm ngục hôm nay</h2>
        <!-- ★상위/하위 던전 필터 (2026-08-23 주인님 지시)★ 기준 파워 280,000 -->
        <button onclick="setAiFilter('hi')" id="ai-f-hi"
                class="text-xs px-2.5 py-1 rounded font-bold bg-fuchsia-700 text-white"></button>
        <button onclick="setAiFilter('lo')" id="ai-f-lo"
                class="text-xs px-2.5 py-1 rounded font-bold bg-gray-700 text-gray-300"></button>
      </div>
      <div class="flex items-center gap-2">
        <button onclick="setAiLang('vi')" id="ai-lang-vi" class="text-xs px-2 py-1 rounded bg-fuchsia-700 text-white font-bold">🇻🇳 VI</button>
        <button onclick="setAiLang('ko')" id="ai-lang-ko" class="text-xs px-2 py-1 rounded bg-gray-700 text-gray-300 font-bold">🇰🇷 KO</button>
        <button onclick="renderAiPlan()" id="ai-refresh" class="text-xs px-2 py-1 rounded bg-gray-700 hover:bg-gray-600 text-gray-200">↻</button>
        <button onclick="document.getElementById('ai-modal').classList.add('hidden')" class="text-gray-500 hover:text-gray-300 text-2xl leading-none px-1">&times;</button>
      </div>
    </div>
    <div class="px-5 py-2 border-b border-gray-800 shrink-0 text-xs text-gray-400" id="ai-summary"></div>
    <div class="overflow-y-auto px-5 py-3 grow" id="ai-body"></div>
    <div class="px-5 py-3 border-t border-gray-800 shrink-0 text-[11px] text-gray-500" id="ai-foot"></div>
  </div>
</div>

<div id="bug-modal" class="hidden fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4">
  <div class="bg-gray-900 rounded-2xl shadow-2xl border border-gray-800 w-full max-w-2xl max-h-[90vh] flex flex-col">
    <div class="flex items-center justify-between px-5 py-4 border-b border-gray-800 shrink-0">
      <h2 class="font-bold text-red-400" id="bug-modal-title">버그 스크린샷</h2>
      <div class="flex items-center gap-2">
        <a id="bug-download-link" href="#" class="text-xs text-gray-400 hover:text-gray-200 px-2 py-1 bg-gray-700 rounded transition-colors">⬇ ZIP</a>
        <button id="bug-clear-btn" class="text-xs text-red-300 hover:text-red-200 px-2 py-1 bg-red-900/50 rounded transition-colors" title="이 PC의 스샷 전부 삭제">🧹 전체삭제</button>
        <button onclick="closeBugsModal()" class="text-gray-500 hover:text-gray-200 text-xl leading-none">✕</button>
      </div>
    </div>
    <div id="bug-list" class="flex-1 overflow-y-auto p-4 space-y-3 scrollbar-thin"></div>
  </div>
</div>

<!-- 실시간 화면 보기 (2026-07-31) — 열려 있는 동안만 프레임이 흐른다 -->
<div id="liveModal" class="hidden fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4">
  <div class="bg-gray-900 rounded-2xl shadow-2xl border border-gray-800 w-full max-w-4xl flex flex-col">
    <div class="flex items-center justify-between px-5 py-3 border-b border-gray-800 shrink-0">
      <h2 class="font-bold text-emerald-400" id="liveTitle">실시간 화면</h2>
      <div class="flex items-center gap-3">
        <span class="text-[11px] text-gray-500">🔴 클릭 지점 · 창을 닫으면 전송이 멈춥니다</span>
        <button onclick="closeLive()" class="text-gray-500 hover:text-gray-200 text-xl leading-none">✕</button>
      </div>
    </div>
    <div class="relative bg-black">
      <img id="liveShot" class="w-full block" style="aspect-ratio:16/9;object-fit:contain" alt="">
      <canvas id="liveCanvas" class="absolute inset-0 w-full h-full pointer-events-none"></canvas>
    </div>
    <div class="px-5 py-2 border-t border-gray-800 text-xs text-gray-400 font-mono truncate" id="liveStep">—</div>
  </div>
</div>

<!-- 렌탈 관리(킬스위치) — main 계정에서만 버튼이 보인다 (2026-08-06) -->
<div id="rental-modal" class="hidden fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4">
  <div class="bg-gray-900 rounded-2xl shadow-2xl border border-gray-800 w-full max-w-lg flex flex-col">
    <div class="flex items-center justify-between px-5 py-3 border-b border-gray-800">
      <h2 class="font-bold text-rose-400">🛑 렌탈 계정 관리</h2>
      <button onclick="closeRentalModal()" class="text-gray-500 hover:text-gray-200 text-xl leading-none">✕</button>
    </div>
    <div class="px-5 py-3 text-xs text-gray-400 border-b border-gray-800">
      <b class="text-gray-300">이용 중지</b>를 켜면 그 계정은 <b>즉시</b> 대시보드 로그인이 막히고,
      대여 프로그램은 <b>10분 안에 자동 정지</b>됩니다. 끄면 재설치 없이 자동으로 다시 이용됩니다.
      <span class="text-gray-500">(내 함대 20대는 영향 없음)</span>
    </div>
    <div id="rental-list" class="px-5 py-3 space-y-2 max-h-[50vh] overflow-y-auto text-sm">불러오는 중…</div>
  </div>
</div>

<!-- 계정 세부정보(스프레드) — info.txt 계정N_* 를 PC×계정 표로 (2026-08-16) -->
<div id="acct-modal" class="hidden fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4">
  <div class="bg-gray-900 rounded-2xl shadow-2xl border border-gray-800 w-full max-w-5xl flex flex-col max-h-[85vh]">
    <div class="flex items-center justify-between px-5 py-3 border-b border-gray-800 shrink-0">
      <h2 class="font-bold text-violet-300">📇 계정 세부정보 <span id="acct-count" class="ml-2 text-xs font-normal text-gray-500"></span></h2>
      <div class="flex items-center gap-2">
        <button onclick="copyAcctTable()" class="text-xs px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-gray-200 rounded-lg font-semibold transition-colors" title="표 전체를 엑셀에 그대로 붙여넣기">📋 표 전체</button>
        <button onclick="closeAcctModal()" class="text-gray-500 hover:text-gray-200 text-xl leading-none ml-1">✕</button>
      </div>
    </div>
    <div class="px-5 py-2 text-xs text-gray-500 border-b border-gray-800 shrink-0 flex items-center justify-between gap-4">
      <span>각 PC의 <b class="text-gray-400">C:&#92;auto&#92;info.txt</b> 에 적힌 값입니다. 고치려면 그 PC의 info.txt 를 수정하세요.</span>
      <span class="shrink-0 text-violet-400/80">칸을 클릭하면 그 값만 복사됩니다</span>
    </div>
    <div id="acct-table" class="flex-1 overflow-auto text-sm"></div>
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
<div id="alert-stack" class="fixed top-4 right-4 z-50 flex flex-col gap-2 pointer-events-auto"></div>
<div id="voice-panel" class="hidden fixed top-14 right-4 z-50 w-72 bg-gray-900/95 border border-gray-700 rounded-xl shadow-2xl p-4 text-xs text-gray-300">
  <div class="flex items-center justify-between mb-3">
    <b class="text-sm text-gray-100">⚙ 설정</b>
    <button onclick="toggleVoicePanel()" class="text-gray-500 hover:text-gray-200">✕</button>
  </div>
  <!-- ★파섹 계정을 맨 위로(2026-08-16)★ — 사용자: "파섹 아이디 비번 넣는거 대시보드에 없잖아".
       실제로는 있었는데 ★'목소리 설정' 패널 맨 아래★ 였다. 파섹 비번을 목소리 설정에서
       찾을 사람은 없다 — 내 배치 실수. 제일 위로 올리고 패널 이름도 '설정'으로 바꾼다. -->
  <div class="mb-4 pb-3 border-b border-gray-700/70">
    <b class="block mb-2 text-gray-200">🔑 파섹 계정 <span class="text-gray-500 font-normal">(전 PC 공용 · 한 번만)</span></b>
    <input id="ps-id" type="text" autocomplete="off" placeholder="파섹 이메일"
           class="w-full bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 mb-2 text-gray-200">
    <input id="ps-pw" type="password" autocomplete="new-password" placeholder="파섹 비밀번호"
           class="w-full bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 mb-2 text-gray-200">
    <button onclick="saveParsecCreds()" class="w-full py-1.5 rounded-lg bg-indigo-700/80 hover:bg-indigo-600 text-indigo-50 font-semibold">저장</button>
    <p class="mt-2 text-[10px] leading-relaxed text-gray-500">카드 메뉴 <b>[🔄 본컴 계정 전환]</b>이 씁니다. 원격컴 크롬이 본컴 런처에 붙을 때 파섹 로그인 폼이 뜨면 이걸로 자동 입력합니다. 서버에만 저장되고 <b>화면으로 다시 불러오지 않습니다</b>. 명령 이력에도 <code>***</code> 로만 남습니다.</p>
  </div>
  <b class="block mb-2 text-gray-200">🎙 목소리</b>
  <label class="block mb-1 text-gray-400">목소리 (⭐ = 가장 자연스러움)</label>
  <select id="tts-voice" onchange="onVoiceChange()" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 mb-3 text-gray-200"></select>
  <label class="block mb-1 text-gray-400">속도 <span class="text-gray-600">느리게 ↔ 빠르게</span></label>
  <input id="tts-rate" type="range" min="-20" max="55" step="5" oninput="onVoiceTune()" class="w-full mb-3">
  <label class="block mb-1 text-gray-400">톤 <span class="text-gray-600">낮게 ↔ 높게</span></label>
  <input id="tts-pitch" type="range" min="-30" max="30" step="2" oninput="onVoiceTune()" class="w-full mb-3">
  <button onclick="previewVoice()" class="w-full py-1.5 rounded-lg bg-emerald-700/80 hover:bg-emerald-600 text-emerald-50 font-semibold">▶ 미리듣기</button>
  <p class="mt-3 text-[10px] leading-relaxed text-gray-500">기본은 <b>서버 사람 목소리</b>입니다. 서버가 음성을 못 만들면 브라우저 내장 음성으로 자동 전환되니 알림 자체는 끊기지 않습니다. 슬라이더를 내릴수록 낮고 느려집니다.</p>
</div>

<script>
// ─── 상태 ────────────────────────────────────────────────────────────────────
let state = {};
let latestVersions = {macro:'', updater:''};
let selectedPcs = new Set();
let logModalPc = null;
let logModalSrc = 'both';   // 'both' | 'macro' | 'upd' — 로그 모달이 지금 보고 있는 출처
let menuPcId = null;

// ★XSS 방어(2026-07-27 보안감사): 매크로가 보낸 값(PC이름/캐릭명/에러/맵/파일명)이
// innerHTML로 그대로 들어가고 있었다. API키를 가진 자(유출키·렌탈 고객)가 악성 문자열을
// 보고에 실어 보내면 대시보드를 여는 순간 실행되어 세션이 탈취된다(무클릭 저장형 XSS).
function esc(v){ return String(v==null?'':v).replace(/[&<>"'`=\/]/g, c => ({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','`':'&#96;','=':'&#61;','/':'&#47;'}[c])); }
function escAttr(v){ return esc(v); }   // esc가 따옴표까지 막으므로 속성값에 그대로 안전

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
  nightmare:    {label:'악몽',    bg:'bg-pink-500/20',   border:'border-pink-700',   badge:'bg-pink-500',   text:'text-pink-400',   online:true},
  awakening:    {label:'각성전',  bg:'bg-indigo-500/20', border:'border-indigo-700', badge:'bg-indigo-500', text:'text-indigo-400', online:true},
  awakening_wait:{label:'각성전 대기', bg:'bg-red-500/20', border:'border-red-700', badge:'bg-red-500', text:'text-red-400', online:true},
  nightmare_wait:{label:'악몽전 대기', bg:'bg-red-500/20', border:'border-red-700', badge:'bg-red-500', text:'text-red-400', online:true},
  corridor:     {label:'회랑',    bg:'bg-blue-500/20',   border:'border-blue-700',   badge:'bg-blue-500',   text:'text-blue-400',   online:true},
  collecting:   {label:'정보수집', bg:'bg-cyan-500/20',   border:'border-cyan-700',   badge:'bg-cyan-500',   text:'text-cyan-400',   online:true},
  paused:       {label:'일시정지', bg:'bg-amber-500/20',  border:'border-amber-700',  badge:'bg-amber-500',  text:'text-amber-400',  online:true},
  error:        {label:'에러',      bg:'bg-red-500/20',    border:'border-red-700',    badge:'bg-red-500',    text:'text-red-400',    online:true},
  // ★오프라인은 빨갛게 (2026-08-20 사용자 지시)★ — 카드가 자리를 안 옮기게 바꿨으니
  //   (아래로 안 내려간다) 죽었다는 걸 ★색으로★ 확실히 알려야 한다. 회색은 안 보인다.
  offline:      {label:'오프라인',  bg:'bg-red-950/40',    border:'border-red-800/70', badge:'bg-red-700',    text:'text-red-400',    online:false},
  other_account:{label:'다른 계정', bg:'bg-gray-900/40', border:'border-gray-800', badge:'bg-purple-900', text:'text-purple-400/70', online:false},
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

function dkHero(){
  const $ = id => document.getElementById(id);
  dkQuote();
  const pcs = Object.values(state||{});
  if(!pcs.length) return;
  const ef = pcs.filter(p=>p.efficiency);
  const avg = ef.length ? ef.reduce((a,p)=>a+p.efficiency,0)/ef.length : 0;
  $('dk-h-eff').innerHTML = avg.toFixed(1)+'<i>%/h</i>';
  $('dk-h-kina').textContent = fmtKinaShort(pcs.reduce((a,p)=>a+(p._total_kina||0),0));
  const done = pcs.reduce((a,p)=>a+((p.daily_progress||[]).filter(dpDone).length),0);
  const tot  = pcs.reduce((a,p)=>a+((p.chars||[]).length),0);
  $('dk-h-done').innerHTML = done+`<i>/${tot||'–'}</i>`;

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
function isCorridorDone(pc_id){
  // 어비스 회랑 완료 = 매크로가 보고한 '남은 캐릭 수'가 0 (적 진영 제외 기준, 수·토 22시 리셋).
  // corridorRemaining은 /corridor/progress + WS로 채워진다. 보고가 없으면 뱃지 없음.
  const v = corridorRemaining[pc_id];
  return !!v && typeof v.remaining === 'number' && v.remaining === 0;
}
function isDungeonDone(pc){
  // 일일던전(계정 티켓 14장) 소진 — 매크로가 소진 시각(dungeon_done_at)을 보고.
  // 각성전과 같은 주간 리셋(수요일 05시) 경계 이후 기록만 인정 → 경계 지나면 자연 소멸.
  const t=(pc.dungeon_done_at||'').replace('T',' ');
  return !!t && t>=fmtTs(lastWeeklyReset());
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
  const n = ({'a':1,'b':2,'c':3,'d':4})[String(pc.pc_id||'').slice(-1)] || 1;
  const info = Object.values((pc.acct_names||{})[String(n)] || {}).filter(Boolean);
  const card = (pc.chars||[]).filter(Boolean);
  if (!info.length || !card.length) return '';
  const a = info.slice().sort().join('|'), b = card.slice().sort().join('|');
  if (a === b) return '';
  return ` · <span class="text-orange-400" title="info.txt 에 적힌 이름: ${esc(info.join(', '))}
카드에 보이는 이름(정보수집 OCR): ${esc(card.join(', '))}

카드는 수집값이 이깁니다. info.txt 를 고쳐도 안 바뀝니다 — 정보수집을 다시 돌리십시오.">이름≠</span>`;
};
const cdpMark = pc => {
  const multi = Object.values(pc.acct_ids || {}).filter(v => String(v||'').trim()).length > 1;
  if (!multi) return '';
  if (pc.cdp === true)  return ` · <span class="text-emerald-500" title="크롬 CDP 붙음 — 계정전환·find_host·순환 가능">CDP</span>`;
  if (pc.cdp === false) return ` · <span class="text-amber-500" title="크롬 CDP 없음 — 계정전환·find_host·순환 불가 (chrome_cdp 명령 필요)">CDP✕</span>`;
  return ` · <span class="text-gray-600" title="CDP 보고 없음 — 매크로가 옛 버전(1.1.571 이하)이거나 죽어 있음">CDP?</span>`;
};

// ─── 오늘 진행 현황 ──────────────────────────────────────────────────────────
function buildDailyProgress(dp, activeSlot, charNames, pc) {
  if (!dp || !dp.length) return '';
  // ★오늘 완료만 센다★ — 서버가 붙인 today 플래그(=completed_time 이 오늘 게임일)를 쓴다.
  //   플래그가 없는 옛 응답이면 예전처럼 completed 만 본다(today!==false).
  const completed = dp.filter(dpDone).length;
  const total = dp.length;
  const slots = dp.map(c => {
    const done = dpDone(c);
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
      style="min-width:0" title="${escAttr(name)}${done?' ✓ '+time:isActive?' 진행 중':''}">
      <span class="font-bold text-xs leading-none">${icon}</span>
      <span style="font-size:9px;line-height:1.2;max-width:100%;overflow:hidden;white-space:nowrap">${esc(short)}</span>
      ${classLabel?`<span style="font-size:8px;line-height:1;color:#9ca3af">${classLabel}</span>`:''}
    </div>`;
  }).join('');
  return `<div class="mt-2 pt-2 border-t border-gray-800/60">
    <div class="flex items-center justify-between mb-1">
      <span class="text-gray-400" style="font-size:10px">오늘 완료 <span class="${completed===total?'text-green-500':'text-gray-500'}">${completed}/${total}</span>${pc._char_collected_at?` · <span class="text-cyan-600">수집 ${relTime(pc._char_collected_at)}</span>`:''}${pc._rot?` · <span class="text-purple-400" title="계정 자동순환 무장됨 — 완주하면 정보수집 후 다음 계정으로 넘어갑니다">🔁 ${esc((ROT_CHIP[pc._rot]||{}).t||pc._rot)}</span>`:''}${cdpMark(pc)}${nameMismatch(pc)}</span>
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
function acctNum(label){ return ({a:1,b:2,c:3,d:4})[label] || '?'; }
function normAcct(input){
  const v = (input||'').trim().toLowerCase();
  const m = {'1':'a','2':'b','3':'c','4':'d','a':'a','b':'b','c':'c','d':'d'}[v];
  if(!m) { if(v) alert('1~4 중 하나만 됩니다'); return null; }
  return m;
}
// 스프레드 그룹 헤더용 계정 태그 — "몇번 계정 · 어떤 아이디"(2026-08-15 사용자 지시).
// 부계정 카드(접미사) 또는 acct 필드 보고가 있는 카드만 표시. 없으면 기존 화면 불변.
function acctTagSpread(pcid){
  const st = state[pcid] || {};
  const isSub = 'bcd'.includes((pcid||'').slice(-1));
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
  //   그게 더 헷갈린다: 1=초록 2=보라 3=청록 4=주황.
  //   ★Tailwind 클래스 대신 인라인 스타일★ — 퍼지(purge)로 색 클래스가 빠져도
  //   이 뱃지는 반드시 보여야 한다(안 보이면 이 수정의 목적 자체가 사라진다).
  const AC = {
    1: {fg:'#6ee7b7', bg:'rgba(16,185,129,.20)', bd:'#34d399'},   // 초록
    2: {fg:'#c4b5fd', bg:'rgba(139,92,246,.20)', bd:'#a78bfa'},   // 보라
    3: {fg:'#5eead4', bg:'rgba(20,184,166,.20)', bd:'#2dd4bf'},   // 청록
    4: {fg:'#fcd34d', bg:'rgba(245,158,11,.20)', bd:'#fbbf24'},   // 주황
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
  const ids = {}, servers = {}, plats = {};
  Object.values(state).forEach(p=>{
    if (baseId(p.pc_id||'') !== base) return;
    Object.assign(ids, p.acct_ids||{});
    Object.assign(servers, p.acct_servers||{});
    Object.assign(plats, p.acct_platforms||{});   // ★카드 계정줄의 '구글' 표기용 (2026-08-21)★
  });
  return {ids, servers, plats};
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
  return 'bcd'.includes(c) ? ({b:2,c:3,d:4}[c]) : ((state[pcid]||{}).acct_num || 1);
}
// 이 PC가 멀티계정인가 — 형제 계정 카드가 있거나(접미사 카드 존재) 매크로가 acct_total>1 보고.
function isMultiAcct(pid){
  const b = baseId(pid||'');
  if (((state[pid]||{}).acct_total || 0) > 1) return true;
  if ('bcd'.includes((pid||'').slice(-1))) return true;
  return ['b','c','d'].some(s => state[b+s]);
}
function acctChip(pid){
  if(!pid) return '';
  const c = pid.slice(-1);
  if(!'bcd'.includes(c)){
    // ★본계정도 멀티계정 PC에선 '계정 1' 칩(2026-08-15 사용자: "계정2는 나오는데 계정1은
    //   안 나온다")★ — 단일 계정 PC(함대 17대)는 칩 없음 그대로.
    if (!isMultiAcct(pid)) return '';
    return `<span class="ml-1 shrink-0 px-1 py-0 rounded border text-xs leading-none bg-emerald-800/70 text-emerald-200 border-emerald-600" style="font-size:10px" title="같은 PC의 계정 1 (본계정)">계정 1</span>`;
  }
  const color = {b:'bg-purple-800/70 text-purple-200 border-purple-600',
                 c:'bg-teal-800/70 text-teal-200 border-teal-600',
                 d:'bg-amber-800/70 text-amber-200 border-amber-600'}[c];
  return `<span class="ml-1 shrink-0 px-1 py-0 rounded border text-xs leading-none ${color}" style="font-size:10px" title="같은 PC의 계정 ${acctNum(c)}">계정 ${acctNum(c)}</span>`;
}
// ★★순환 단계 칩 — '전환중' 이 한눈에 보이게 (2026-08-21 주인님 요청)★★
//   원문: "그리고 대시보드에 전환중이라는 표시도 보여야할거같아"
//   예전엔 카드 맨 아래 10px 회색으로 `🔁 switching` 만 찍혀서 ①영어고 ②안 보였다.
//   순환이 실제로 뭘 하는 중인지가 안 보이면 "왜 안 넘어가지" 를 아무도 진단 못 한다.
//   hunting 은 평상시라 조용히, 나머지 3단계는 ★상태 이름 옆에 크게★ 띄운다.
const ROT_CHIP = {
  collecting: {t:'📋 정보수집중',  c:'bg-cyan-800/80 text-cyan-100 border-cyan-500',    p:true},
  switching : {t:'🔄 계정 전환중', c:'bg-amber-700/85 text-amber-100 border-amber-400', p:true},
  starting  : {t:'▶ 사냥 시작중',  c:'bg-green-800/80 text-green-100 border-green-500', p:true},
  hunting   : {t:'🔁 순환 ON',     c:'bg-purple-900/60 text-purple-300 border-purple-700', p:false},
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
function buildCard(pc) {
  const st = pc.status||'offline';
  const cfg = STATUS_CFG[st]||STATUS_CFG.offline;
  const pulse = (st==='hunting'||st==='selling'||st==='abyss'||st==='awakening_wait')?' pulse':'';   // 각성전 대기 = 깜빡여서 눈에 띄게
  const sel = selectedPcs.has(pc.pc_id)?' card-sel':'';
  const errHtml = (pc.errors||[]).slice(0,3).map(e=>
    `<div class="text-xs text-red-400 bg-red-900/30 rounded px-2 py-0.5">⚠ ${esc(e)}</div>`).join('');
  const bugBadge = (pc._bug_count||0)>0
    ? `<span class="ml-1.5 px-1.5 py-0.5 bg-red-700/80 text-red-200 rounded text-xs font-bold leading-none cursor-pointer" onclick="event.stopPropagation();openBugsModal('${pc.pc_id}')">🐛 ${pc._bug_count}</span>`
    : '';
  // ★낡은 보고는 색칠하지 않는다 (2026-08-20 PC-23)★ — 업데이터가 마지막으로 전송에
  //   성공한 값을 신선도 검사 없이 초록으로 칠하는 바람에, 14.6시간 죽은 PC 가
  //   "업데이터 running" 으로 멀쩡해 보였다. 서버 OFFLINE_TIMEOUT 이 90초이므로
  //   그 3배(270초)를 넘으면 회색 + '(N분전)' 로 강등한다.
  const _uage = (typeof pc._updater_age_s === 'number') ? pc._updater_age_s : null;
  const _ustale = (_uage !== null && _uage > 270);
  const ucls = _ustale ? 'text-gray-600 line-through'
    : ({'running':'text-green-400','stopped':'text-gray-500','updating':'text-cyan-400','crashed':'text-red-400'}[pc._updater_state]||'text-gray-600');
  const uageTxt = _ustale ? `<span class="text-amber-600" title="업데이터 보고가 ${_uage}초째 없음 — 화면의 상태는 그때 값입니다">(${Math.floor(_uage/60)}분전)</span>` : '';
  const mvcls = (pc.macro_version && latestVersions.macro && pc.macro_version !== latestVersions.macro) ? 'text-red-400' : 'text-gray-700';
  const uvcls = (pc._updater_version && latestVersions.updater && pc._updater_version !== latestVersions.updater) ? 'text-red-400' : 'text-gray-700';
  const macroVer = pc.macro_version ? `<span class="${mvcls}">매크로 v${pc.macro_version}</span>` : '';
  const updaterRow = (pc._updater_state&&pc._updater_state!=='unknown')
    ? `<div class="mt-1 flex items-center gap-1 text-gray-600 whitespace-nowrap overflow-hidden" style="font-size:10px">${macroVer}${macroVer?'<span class="text-gray-800">|</span>':''}<span>업데이터</span><span class="${ucls}">${esc(pc._updater_state)}</span>${uageTxt}${pc._updater_version?`<span class="${uvcls}">v${esc(pc._updater_version)}</span>`:''}</div>`
    : '';
  const activeSlot = pc.slot||0;
  const activeDp = (pc.daily_progress||[]).find(c=>c.slot===activeSlot&&!dpDone(c));
  const activeName = activeDp
    ? ((pc.chars&&pc.chars[activeSlot-1]) || activeDp.name || String(activeSlot))
    : '';
  const isOnline = (STATUS_CFG[st]||STATUS_CFG.offline).online;
  const activeTag = (activeName && isOnline)
    ? `<span class="ml-1 px-1 py-0 bg-yellow-700/60 text-yellow-200 border border-yellow-700/80 rounded text-xs leading-none whitespace-nowrap" style="font-size:10px">${activeSlot} ${esc(activeName)}</span>`
    : '';
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
            <span class="shrink-0">${esc(baseId(pc.pc_id||'')||'?')}</span>
            <span class="shrink-0 flex items-center">${bugBadge}</span>
          </div>
          ${(acctChip(pc.pc_id)||doneBadges||activeTag)?`<div class="mt-0.5 flex items-center gap-1 whitespace-nowrap overflow-hidden min-w-0">${acctChip(pc.pc_id)}${doneBadges}${activeTag}</div>`:''}
        </div>
      </div>
      <!-- 상태 쪽이 양보한다(min-w-0+truncate) — PC명은 shrink-0라 절대 안 잘림 -->
      <div class="flex items-center gap-1 min-w-0">
        <span class="inline-flex items-center gap-1.5 text-base font-bold ${cfg.text} min-w-0">
          <span class="w-3 h-3 rounded-full ${cfg.badge}${pulse} shrink-0"></span>
          <span class="truncate">${cfg.label}</span>${rotChip(pc)}
        </span>
      </div>
    </div>
    <div class="grid grid-cols-2 gap-x-4 gap-y-1 text-sm mt-2">
      <div><span class="text-gray-400">진행도</span> <span class="text-white font-medium">${pc.hunt_progress!=null ? Math.round(pc.hunt_progress)+' %' : '–'}</span></div>
      <div class="whitespace-nowrap"><span class="text-gray-400">효율</span> <span class="text-white font-medium">${pc.efficiency!=null ? pc.efficiency.toFixed(1)+'%/h' : '–'}</span></div>
      <div class="col-span-2"><span class="text-gray-400">맵</span> <span class="text-white font-medium">${esc(pc.map_name||'–')}</span></div>
      <div><span class="text-gray-400">업타임</span> <span class="text-white font-medium">${fmtSlotUptime(pc.slot_uptime, pc.slot||0, pc.uptime_hours)}</span></div>
      ${pc.server?`<div><span class="text-gray-400">서버</span> <span class="text-white font-medium">${pc.server}</span></div>`:''}
      <div><span class="text-gray-400">최근</span> <span class="text-white font-medium">${relTime(pc.last_active)}</span></div>
      <div><span class="text-gray-400">사망(30분)</span> <span class="${(pc.deaths_30m||0)>0?'text-red-400 font-bold':'text-white font-medium'}">${pc.deaths_30m||0}회</span></div>
    </div>
    ${pc.abyss_kina?`<div class="mt-1.5 text-xs text-amber-300 bg-amber-900/20 border border-amber-800/40 rounded px-2 py-0.5 truncate" title="어비스(Delete) 세션 키나 정산 — 켤 때/끌 때 보유 키나 차액. 다음 세션 시작까지 유지">💰 어비스 ${esc(pc.abyss_kina)}</div>`:''}
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
  const shownCls = useId ? 'text-gray-400' : (pl ? pl.cls : '');
  return `<div class="mt-2 pt-1.5 border-t border-gray-800/80 flex items-center gap-1.5 text-gray-500 whitespace-nowrap overflow-hidden" style="font-size:11px">
      <span class="shrink-0 text-purple-300/90">🔑 계정 ${n}</span>
      ${isNC ? `<span class="shrink-0 text-sky-300/80" style="font-size:10px" title="플랫폼: NC">NC</span>` : ''}
      <span class="truncate ${shownCls}" title="${esc(id || plat)}">${esc(shown)}</span>
      ${pc.acct_nick?`<span class="shrink-0 text-gray-600">${esc(pc.acct_nick)}</span>`:''}
      ${srv?`<span class="ml-auto shrink-0 text-cyan-400/80">${esc(srv)}</span>`:''}
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
    card.setAttribute('draggable','false');
    // 핸들에서만 드래그 시작
    handle.addEventListener('mousedown', e => {
      e.stopPropagation();
      card.setAttribute('draggable','true');
      dragSrcId = gridKeyOf(card);
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
function stackShow(base, pcid){ stackFront[base] = pcid; renderCards(); }
function buildStack(s){
  if (s.n <= 1) return buildCard(s.top);
  const LH = 18;                       // 뒤 카드가 엿보이는 층 높이(px)
  const ghosts = s.list.filter(p => p !== s.top);
  let layers = '';
  for (let k = s.n - 1; k >= 1; k--) { // k = 깊이(클수록 더 뒤)
    const g = ghosts[k-1];             // 실카드 없으면(자격증명만 선언) 빈 층
    const num = g ? acctNumOf(g.pc_id) : '';
    const on = g ? ((STATUS_CFG[g.status||'offline']||STATUS_CFG.offline).online) : false;
    // 접미사 pc_id 대신 아이디(전 계정 지도)로 — "20b" 노출 금지(v1.1.424 사용자)
    const gid = g ? (g.acct_id || groupAcctMaps(s.base).ids[num] || '') : '';
    const lab = g ? `계정 ${num}${gid?` · ${esc(gid)}`:''}${on?'':' · offline'}` : '계정 (미접속)';
    layers += `<div class="stack-layer" style="top:${LH*(s.n-1-k)}px;left:${5*k}px;right:${5*k}px;bottom:8px;z-index:${5-k}"
      ${g?`onclick="stackShow('${s.base}','${g.pc_id}')" title="클릭하면 이 계정 카드를 앞으로"`:''}>
      <div class="px-3 flex items-center" style="height:${LH-2}px;font-size:10px">${lab}</div></div>`;
  }
  // ★wrapper 에 id 를 준다 (2026-08-18)★ — 없으면 드래그가 wrapper 가 아니라
  //   ★안쪽 앞 카드★ 를 집어 옮긴다(앞장만 움직이는 것처럼 보이고, 저장 때
  //   grid.children 의 id 가 빈 값이라 아무것도 안 남아 원래대로 돌아온다).
  //   사용자 실측: "겹친거 통째로넘어가는것처럼안보이고 제일앞에거만 넘아가는것처럼
  //   보이면서 다시 원래대로 돌아옴"
  return `<div id="stack-${s.base}" class="relative" style="padding-top:${LH*(s.n-1)}px">${layers}
    <div class="relative" style="z-index:6">${buildCard(s.top)}</div></div>`;
}

function renderCards() {
  migrateOrder();          // 옛 순서 목록 1회 이관(baseId 정의 뒤에 안전하게)
  // ★PC-TEST 는 화면에 안 띄운다 (2026-08-20 사용자: "거슬린다")★
  //   배포 검증이 pc_id=PC-TEST 로 /check 를 때리면서 카드가 생긴다. 지워도 다음
  //   검증 때 또 생기므로 ★렌더 단계에서 거른다★ (전광판 합계에서도 같이 빠진다).
  const pcs = Object.values(state)
    .filter(p => baseId(p.pc_id||'') !== 'PC-TEST')
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
    const n = Math.min(4, Math.max(list.length, ...list.map(p => p.acct_total || 1)));
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
  go.innerHTML  = all.length ? all.map(buildStack).join('') : '<div class="text-gray-700 text-sm col-span-full text-center py-10">매크로 연결 없음</div>';
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
    if(!isDungeonDone(p)) dungeonLeft.add(p.pc_id);
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
    if (r.awakening_ticket != null) { awakenSeen = true; totalAwaken += (parseInt(r.awakening_ticket) || 0); }
    if (r.trade_kina != null) { tradeSeen = true; totalTrade += (Number(r.trade_kina) || 0); }
  });
  const elOn = document.getElementById('cnt-online');
  elOn.textContent = c.onlineChars;
  // 숫자는 '전체 캐릭터', 대수 정보는 툴팁에 남긴다 (온라인/오프라인 구분은 여기서 확인)
  elOn.title = `전체 캐릭터 ${c.onlineChars}명 — 뒷카드(다른 계정)·오프라인 PC 포함`
             + ` / PC 온라인 ${c.online}대 · 오프라인 ${c.offline}대`;
  document.getElementById('cnt-odd-energy').textContent=totalOdd > 0 ? totalOdd.toLocaleString() : '–';
  document.getElementById('cnt-awakening').textContent=awakenSeen ? totalAwaken.toLocaleString() : '–';
  document.getElementById('cnt-trade-kina').textContent=tradeSeen ? fmtKinaKor(totalTrade) : '–';
  document.getElementById('cnt-dungeon-left').textContent=pcs.length ? String(dungeonLeft.size) : '–';
  const elDone = document.getElementById('cnt-completed');
  elDone.textContent = c.completedChars;
  elDone.title = `오늘 사냥을 끝낸 캐릭터 ${c.completedChars}명 · 전 캐릭 완료한 PC ${c.completedPcs}대 (새벽 5시 초기화)`;
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

// ★멀티계정(v1.1.412 리뷰 결함 4/11): 업데이터 명령은 base id로★ — 업데이터는 PC 단위라
//   base id(PC-03)로만 폴링한다. 부계정 카드(PC-03b)로 보내면 아무도 안 가져가는 고아 명령이
//   된다. 접미사(b/c/d)를 벗겨 base로 보낸다. (매크로 명령 sendCmd는 그대로 계정별로 간다)
function baseId(id){ return (id && 'bcd'.includes(id.slice(-1))) ? id.slice(0,-1) : id; }
async function selUpdaterCmd(command, args={}) {
  if(selectedPcs.size===0){alert('PC를 선택하세요');return;}
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
  // ★고아 명령 차단(2026-08-15)★ — '다른 계정 접속중' 카드의 pc_id로 매크로 명령을 보내면
  //   아무도 안 가져간다(매크로는 현재 정체성 pc_id로만 수신, 15분 뒤 만료). 조용히 증발하는
  //   대신 안내하고 막는다. 그 계정에 실행하려면 먼저 계정 전환(오른클릭 계정 N).
  if (((state[pc_id]||{}).status) === 'other_account') {
    showToast(`⚠️ ${pc_id}는 지금 다른 계정 접속 중 — 계정 전환 후 실행하세요`);
    return false;
  }
  // ★★계정 자동순환 무장 신호 (2026-08-20)★★
  //   "시작을 눌러줫을때만 그작업을 하면되고" — 방아쇠는 ★사람이 누른 이 버튼★ 이다.
  //   ★왜 서버가 command=='start' 만 보면 안 되나★ /command 로 start 를 쏘는 건 이
  //   버튼만이 아니다 — 운영 스크립트(deploy_to.py·start_idle.py·up_and_start.py …)가
  //   전부 쓴다. 그러면 알람 조치로 한 대를 재개시키는 것만으로 순환까지 켜지고,
  //   up_and_start.py 한 번이면 함대 전체가 무장된다(CLAUDE.md A7 우회).
  //   → ★대시보드에서 나가는 start 에만★ 이 표시를 싣는다. 여기가 단일 초크포인트다.
  if (command === 'start') args = Object.assign({rotate: true}, args);
  const res=await fetch(`/command/${pc_id}`,{
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({command,args})});
  // ★무장이 거부되면 반드시 말한다★ — start 자체는 200 이라 화면엔 아무 표시가 없었다.
  //   순환이 안 켜진 걸 사람이 알 방법이 카드의 🔁 배지 '없음' 뿐이면 아무도 못 알아챈다.
  if (command === 'start') {
    try {
      const j = await res.clone().json();
      if (j && j.armed === false) showToast(`⚠️ ${pc_id} 사냥은 시작 — 계정 순환은 무장 안 됨 (${j.why||''})`);
    } catch(e) {}
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
  const ids=Object.keys(state);
  if(!ids.length){showToast('연결된 PC 없음');return;}
  await Promise.all(ids.map(id=>sendCmd(id,command,args)));
  showToast(`✓ ${command} → 전체 ${ids.length}대`);
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
  const n=selectedPcs.size;
  await Promise.all([...selectedPcs].map(id=>sendCmd(id,command,args)));
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
  await Promise.all(targets.map(id=>sendCmd(id, command, {rotate:true})));
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
    `선택 PC들을 전환할 계정 번호 (1~4)\n1 = 본계정 / 2,3,4 = 부계정\n` +
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
    if ((((t.match(/([bcd])$/)||[])[1]) || 'a') === v) { already.push(baseId(t)); continue; }
    targets.push(t);
  }
  if(!targets.length){ showToast(`선택한 PC는 이미 전부 계정 ${n} 입니다`); clearSelection(); return; }
  if(!confirm(`${targets.length}대 → 계정 ${n} 통짜 전환` +
              (already.length ? `\n(이미 계정 ${n} 인 ${already.length}대는 제외)` : '') +
              `\n\n① 본컴 런처 계정 교체 + 게임 실행 (파섹 경유)\n② 원격컴 크롬 로그인 교체\n③ 매크로 재시작\n\n` +
              `★대당 1~2분, 게임 세션 끊김★. 진행할까요?`))return;
  await Promise.all(targets.map(id=>sendCmd(id,'switch_launcher',
        {acct_no:n, acct_index:1, acct_label:`계정${n}`, chrome_label:v})));
  showToast(`🔁 ${targets.length}대 계정 ${n} 통짜 전환 시작 (결과는 텔레그램)`);
  loadCmdHistory();
  clearSelection();
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
  const _mAcct = isMultiAcct(pc_id) ? ` <span class="text-purple-300" style="font-size:11px">계정 ${acctNumOf(pc_id)}</span>` : '';
  document.getElementById('menu-pc-label').innerHTML=
    `<span class="font-bold text-gray-100">${baseId(pc_id)}${_mAcct}</span>`+
    `<span class="inline-flex items-center gap-1 ${cfg.text}" style="font-size:11px"><span class="w-2 h-2 rounded-full ${cfg.badge}"></span>${cfg.label}</span>`+
    (ver?`<span class="text-gray-500 ml-auto" style="font-size:10px">${ver}</span>`:'');
  refreshAcctButtons(pc_id);   // 계정 1~4 버튼 활성/비활성 (있는 계정만, 현재 계정 ✓)
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
  const id=menuPcId, u=((state[id]||{}).lan_url)||'';
  closeCardMenu();
  // ★내부망 서버가 안 열린 PC★ — 아직 구버전이거나 info.txt에 lan_prefix가 없거나
  //   내부망 랜선이 안 꽂힌 경우다. 조용히 아무 일도 안 하면 원인을 알 수 없으니 알려준다.
  if(!/^http:\/\/[\d.]+:\d+\/\?k=/.test(u)){
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
    for(let n=1; n<=4; n++){
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
  if(!confirm(`선택 ${selectedPcs.size}대 준비 실행\n(전 캐릭: 정산(계정1회)→추출→창고보관→정렬→귀환주문서)`))return;
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

let _ws=null, _wsLastMsg=0;
function connectWS() {
  const proto=location.protocol==='https:'?'wss':'ws';
  const ws=new WebSocket(`${proto}://${location.host}/ws`);
  _ws=ws; _wsLastMsg=Date.now();
  ws.onopen=()=>{document.getElementById('ws-dot').className='w-2.5 h-2.5 rounded-full bg-green-500 transition-colors';};
  ws.onmessage=(e)=>{
    _wsLastMsg=Date.now();
    const msg=JSON.parse(e.data);
    if(msg.type==='state'){state={};(msg.pcs||[]).forEach(p=>{state[p.pc_id]=p;});if(msg.latest)latestVersions=msg.latest;scheduleRender();}
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
let corridorRemaining={};   // {pc_id: {remaining, total}}
function updateCorridorTile(){
  let rem=0,has=false;
  Object.values(corridorRemaining).forEach(v=>{
    if(v&&typeof v.remaining==='number'){has=true;rem+=v.remaining;}});
  const el=document.getElementById('cnt-corridor');
  if(el)el.textContent=has?String(rem):'–';
}
async function loadCorridorSummary(){
  try{
    const r=await fetch('/corridor/progress');if(!r.ok)return;
    const d=await r.json();corridorRemaining={};
    Object.entries(d.pcs||{}).forEach(([pc,v])=>{corridorRemaining[pc]={remaining:v.remaining,total:v.total};});
    updateCorridorTile();
    scheduleRender();   // 🌀 뱃지도 갱신 (만료로 사라진 PC 반영)
  }catch(e){}
}
// ★5분 주기 재조회★ — 수·토 22시 리셋 경계가 지나면 서버가 옛 스냅샷을 만료 처리하는데,
// 페이지를 안 새로고침해도 뱃지·타일이 5분 안에 따라오게 한다 (2026-08-05 "뱃지 안 사라짐" 사고)
setInterval(loadCorridorSummary,300000);
function handleCorridorMsg(msg){
  corridorRemaining[msg.pc_id]={remaining:(msg.data||{}).remaining,total:(msg.data||{}).total};
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
// ★★2026-08-20: 업데이터 로그를 같이 본다★★
//   매크로가 죽으면 그 PC 는 완전 실명이 된다(PC-23 사고). 그때 유일하게 살아 있는 눈이
//   업데이터인데, 그 로그는 C:\auto\updater.log 에만 있어서 대시보드로는 볼 수 없었다.
//   이제 /updater/logs/{basePc} 를 같이 읽어 ★시간순으로 섞어★ 보여준다.
//   줄 앞의 M(매크로)/U(업데이터) 뱃지로 출처를 구분한다.
function _basePc(id){ return /\d[bcd]$/.test(id||'') ? id.slice(0,-1) : (id||''); }

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
  rows.forEach(l=>appendLogLine(l.level,`${String(l.created_at||'').slice(11,19)} ${l.message}`,l.src));
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
// ★★상위/하위 던전 (2026-08-23 주인님 지시)★★
//   원문: "오늘의던전 옆에 상위던전 이라고 버튼 만들어서 그걸 눌렀을때 280k 이상 되는
//          출력하면되고 그옆에 하위던전 버튼하나만들어서 그걸 눌렀을때는 280k 미만인
//          애들 출력하게끔하면돼. 구독이나 오드에너지 말해준건 똑같이하고"
//   → 파워 기준선 하나(280,000)로 목록을 둘로 가른다. 정렬·구독배지·에너지 표시·
//     완료 체크는 ★손대지 않는다★ (완료 키가 'pc:slot' 이라 필터를 바꿔도 유지된다).
//   기본은 '상위' — 기존 화면(30만 이상)과 가장 가깝다.
const AI_PW_CUT = 280000;
let aiFilter = localStorage.getItem('aiFilter') || 'hi';   // 'hi' = 280k 이상 / 'lo' = 미만
let aiDone = { day: '', keys: [] };

const AI_T = {
  vi: { title:'🤖 Hầm ngục hôm nay', power:'Lực', energy:'Năng lượng', bonus:'thêm',
        slot:'Ô', chars:'nhân vật', sub:'Có đăng ký', nosub:'KHÔNG đăng ký',
        warn:'⚠ Không đăng ký — 1 lượt chỉ 40 NL, không bán được ở chợ, không dùng được kho từ xa',
        empty:'Chưa có dữ liệu. Hãy chạy thu thập thông tin trước.',
        fHi:'Cấp cao ≥280k', fLo:'Cấp thấp <280k',
        foot:'Lực ≥ 280,000 · tài khoản CÓ đăng ký lên trước · ưu tiên nhân vật còn nhiều năng lượng hằng ngày. Đánh dấu xong sẽ được lưu (vẫn ở nguyên chỗ), tự reset lúc 5 giờ sáng.',
        footLo:'Lực < 280,000 (hầm ngục cấp thấp) · tài khoản CÓ đăng ký lên trước · ưu tiên nhân vật còn nhiều năng lượng hằng ngày. Đánh dấu xong sẽ được lưu, tự reset lúc 5 giờ sáng.',
        summary:(a,c,d)=>`${a} tài khoản · ${c} nhân vật · đã xong ${d}` },
  ko: { title:'🤖 오늘의 던전', power:'파워', energy:'에너지', bonus:'보너스',
        slot:'슬롯', chars:'캐릭', sub:'구독 O', nosub:'구독 X',
        warn:'⚠ 구독 해제 — 한 판 40에너지, 거래소 판매 불가, 원격창고 불가',
        empty:'데이터가 없습니다. 먼저 정보수집을 돌려주세요.',
        fHi:'상위 던전 28만↑', fLo:'하위 던전 28만↓',
        foot:'파워 28만 이상 · 구독 계정이 위 · 매일 차는 에너지 많은 순. 완료 체크는 저장되며(자리는 안 움직임) 새벽 5시에 리셋됩니다.',
        footLo:'파워 28만 미만(하위 던전) · 구독 계정이 위 · 매일 차는 에너지 많은 순. 완료 체크는 저장되며 새벽 5시에 리셋됩니다.',
        summary:(a,c,d)=>`계정 ${a}개 · 캐릭 ${c}명 · 완료 ${d}` },
};

// ★게임일 — 새벽 5시 경계 (주인님 지시)★ 5시 전이면 전날로 친다.
function aiGameDay(){
  const d = new Date();
  if (d.getHours() < 5) d.setDate(d.getDate() - 1);
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
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
    const sub = a.max >= 840;
    // ★파워 기준선 하나로 상/하위를 가른다 (2026-08-23)★ 나머지 규칙은 그대로.
    const elig = a.chars
      .filter(c => aiFilter === 'lo' ? c.pw < AI_PW_CUT : c.pw >= AI_PW_CUT)
      .sort((x,y) => y.daily - x.daily);
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

// ★상/하위 전환 — 완료 체크는 건드리지 않는다(키가 pc:slot 이라 그대로 살아 있다)★
function setAiFilter(f){
  aiFilter = (f === 'lo') ? 'lo' : 'hi';
  try{ localStorage.setItem('aiFilter', aiFilter); }catch(e){}
  renderAiPlan();
}

function renderAiPlan(){
  const T = AI_T[aiLang] || AI_T.vi;
  document.getElementById('ai-title').textContent = T.title;
  document.getElementById('ai-foot').textContent =
    (aiFilter === 'lo' && T.footLo) ? T.footLo : T.foot;
  // 필터 버튼 라벨 + 활성 표시
  const bHi = document.getElementById('ai-f-hi'), bLo = document.getElementById('ai-f-lo');
  if (bHi && bLo) {
    bHi.textContent = T.fHi; bLo.textContent = T.fLo;
    const on = 'text-xs px-2.5 py-1 rounded font-bold bg-fuchsia-700 text-white';
    const off = 'text-xs px-2.5 py-1 rounded font-bold bg-gray-700 text-gray-300 hover:bg-gray-600';
    bHi.className = (aiFilter === 'hi') ? on : off;
    bLo.className = (aiFilter === 'lo') ? on : off;
  }
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
      h += `<div class="flex items-center gap-2 px-3 py-1.5" style="${done?'opacity:.45':''}">
        <input type="checkbox" ${done?'checked':''} onchange="aiToggleDone('${c.key}', this)"
               style="width:18px;height:18px;accent-color:#22c55e;cursor:pointer;flex:none">
        <span class="text-xs text-gray-500 w-10">${T.slot}${c.slot}</span>
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
const ttsCfg = Object.assign(
  { engine: 'server', name: '', ratePct: 25, pitchHz: 18 },
  JSON.parse(localStorage.getItem('ttsCfg') || '{}')
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

// ★1순위는 서버 신경망 음성(사람 목소리). 서버가 못 만들면 브라우저 내장 음성으로
//   자동 폴백한다 — 목소리는 아쉬워도 알림 자체가 끊기면 안 되기 때문.★
function speak(text, force){
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
  try {
    const res = await fetch(`/updater/command/${baseId(pc_id)}`, {   // 업데이터=base id (멀티계정)
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command, args})
    });
    return res.ok;
  } catch (e) {          // ★네트워크 예외도 실패다 (2026-08-22)★ 안 잡으면 호출부가 통째로 죽는다
    console.error('sendUpdaterCmd 실패', pc_id, command, e);
    return false;
  }
}

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
  const _base = (typeof baseId === 'function') ? baseId(pc_id) : String(pc_id).replace(/[bcd]$/, '');
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
    // 물약 열 제거(2026-07-30 사용자 지시) — v1.1.343부터 매크로가 판독하지 않는다(항상 0)
    const scroll = r.return_scroll_count != null ? r.return_scroll_count : '–';
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
      <td class="px-3 py-1.5 text-center">${scrollLow?rc(scroll):scroll}</td>
      <td class="px-3 py-1.5">${extFull?rc(ext):ext}</td>
      <td class="px-3 py-1.5 text-center">${arcanaLink}</td>
      <td class="px-3 py-1.5 text-center">${equipLink}</td>
      <td class="px-3 py-1.5 text-right text-emerald-400">${gakin}</td>
      <td class="px-3 py-1.5 text-right text-orange-400">${trade}</td>
      <td class="px-3 py-1.5 text-right text-yellow-300 font-medium">${kina}</td>
      <td class="px-3 py-1.5 text-center text-fuchsia-300">${r.abyss_time || '–'}</td>
      <td class="px-3 py-1.5 text-right text-fuchsia-200">${r.abyss_point ? Number(r.abyss_point).toLocaleString() : '–'}</td>
      <td class="px-3 py-1.5 text-center ${r.corridor_full ? 'text-green-400 font-medium' : 'text-sky-300'}">${r.corridor_progress || '–'}</td>
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
            <button onclick="selectAllSlots('${pc}', ${JSON.stringify(pcRows.map(r=>r.slot))}, true)" class="px-1.5 py-0.5 text-xs rounded bg-gray-600/60 hover:bg-gray-500 text-gray-200 whitespace-nowrap">전체선택</button>
            <button onclick="selectAllSlots('${pc}', ${JSON.stringify(pcRows.map(r=>r.slot))}, false)" class="px-1.5 py-0.5 text-xs rounded bg-gray-600/60 hover:bg-gray-500 text-gray-400 whitespace-nowrap">전체해제</button>
            <span class="text-gray-600">|</span>
            <button onclick="sendCmd('${pc}','start')" class="px-1.5 py-0.5 text-xs rounded bg-green-900/60 hover:bg-green-700 text-green-300 whitespace-nowrap">▶ 시작</button>
            <button onclick="sendCmd('${pc}','exit')" class="px-1.5 py-0.5 text-xs rounded bg-red-900/60 hover:bg-red-700 text-red-300 whitespace-nowrap">✕ 종료</button>
            <button onclick="sendUpdaterCmd('${pc}','update')" class="px-1.5 py-0.5 text-xs rounded bg-yellow-900/60 hover:bg-yellow-700 text-yellow-300 whitespace-nowrap">↺ 재시작</button>
            <button onclick="sendCmd('${pc}','daily_dungeon')" class="px-1.5 py-0.5 text-xs rounded bg-purple-900/60 hover:bg-purple-700 text-purple-300 whitespace-nowrap">일일던전</button>
            <button onclick="sendCmd('${pc}','nightmare')" class="px-1.5 py-0.5 text-xs rounded bg-pink-900/60 hover:bg-pink-700 text-pink-300 whitespace-nowrap">악몽</button>
            <button onclick="sendCmd('${pc}','abyss')" class="px-1.5 py-0.5 text-xs rounded bg-blue-900/60 hover:bg-blue-700 text-blue-300 whitespace-nowrap">어비스</button>
            <button onclick="sendCmd('${pc}','corridor')" class="px-1.5 py-0.5 text-xs rounded bg-indigo-900/60 hover:bg-indigo-700 text-indigo-300 whitespace-nowrap">회랑</button>
            <button onclick="sendCmd('${pc}','awakening')" class="px-1.5 py-0.5 text-xs rounded bg-violet-900/60 hover:bg-violet-700 text-violet-300 whitespace-nowrap">각성전</button>
            <button onclick="sendCmd('${pc}','prepare')" class="px-1.5 py-0.5 text-xs rounded bg-amber-900/60 hover:bg-amber-700 text-amber-300 whitespace-nowrap" title="정산→추출→창고→정렬→귀환주문서">준비</button>
            <button onclick="sendCmd('${pc}','collect_info')" class="px-1.5 py-0.5 text-xs rounded bg-sky-900/60 hover:bg-sky-700 text-sky-300 whitespace-nowrap">정보수집</button>
            <button onclick="sellAllCard('${pc}')" class="px-1.5 py-0.5 text-xs rounded bg-yellow-900/60 hover:bg-yellow-700 text-yellow-300 whitespace-nowrap">판매</button>
            <button onclick="openLive('${pc}')" class="px-1.5 py-0.5 text-xs rounded bg-emerald-900/60 hover:bg-emerald-700 text-emerald-300 whitespace-nowrap" title="이 PC의 게임 화면을 실시간으로 봅니다 (열려 있는 동안만 전송)">🖵 화면</button>
            ${(/^http:\/\/[\d.]+:\d+\/$/.test(((state[pc]||{}).lan_url)||'')) ? `<button onclick="window.open('${(state[pc]||{}).lan_url}','_blank')" class="px-1.5 py-0.5 text-xs rounded bg-teal-900/60 hover:bg-teal-700 text-teal-300 whitespace-nowrap" title="내부망 직결 — 원본 해상도·고프레임, 새 탭으로 열립니다 (같은 내부망에 있어야 열림)">⚡ 내부망</button>` : ''}
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
    avail.add('bcd'.includes(c) ? ({b:2,c:3,d:4}[c]) : 1);
  });
  for (let n=1; n<=Math.min(4,total); n++) avail.add(n);
  return avail;
}
function liveCardOf(base){
  const isOn = p => (STATUS_CFG[p.status||'offline']||STATUS_CFG.offline).online;
  return Object.values(state).find(p => baseId(p.pc_id||'')===base && isOn(p)) || null;
}
function currentAcctNum(base){
  const live = liveCardOf(base);
  if (!live) return 0;
  const c = (live.pc_id||'').slice(-1);
  return live.acct_num || ({b:2,c:3,d:4}[c] || 1);
}
function refreshAcctButtons(pc_id){
  const base = baseId(pc_id);
  const avail = acctAvail(base);
  const cur = currentAcctNum(base);
  for (let n=1; n<=4; n++){
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
  const lab = {1:'a',2:'b',3:'c',4:'d'}[n];
  if (!lab) return false;
  // 명령은 '지금 온라인인 카드'로 — 매크로는 현재 정체성의 pc_id로만 수신한다
  const live = liveCardOf(base);
  const target = live ? live.pc_id : id;
  const curAcct = ((target.match(/([bcd])$/)||[])[1]) || 'a';
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
  if(ok) toast(adopt ? '본컴 계정 찾기 → 전환까지 진행합니다'
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
  const res=await fetch('/status');
  if(res.ok)(await res.json()).pcs?.forEach(p=>{state[p.pc_id]=p;});
  renderCards(); loadCmdHistory(); loadCharTable(); connectWS(); loadSalePrice(); loadAwakenPreset();
  setInterval(renderCards,60000);
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
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    pcs = await _build_full_state(tenant)
    return JSONResponse({"pcs": pcs})


def _strip_cmds(cmds: list, tenant: str) -> list:
    """명령 내역을 테넌트 것만 남기고 pc_id 접두사 제거."""
    out = []
    for c in cmds:
        t, raw = split_ns(c.get("pc_id") or "")
        if t == tenant:
            c = dict(c)
            c["pc_id"] = raw
            out.append(c)
    return out


async def _push_cmd_history(tenant: str):
    cmds = _strip_cmds(await get_recent_commands(20, ns_prefix=("" if tenant == "main" else tenant)), tenant)
    await manager.broadcast({"type": "cmd_history", "commands": cmds}, tenant)


@app.get("/logs/{pc_id}")
async def pc_logs(pc_id: str, request: Request):
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    logs = await get_logs(ns(tenant, pc_id), limit=2000)
    return JSONResponse({"logs": logs})


@app.post("/log/{pc_id}")
async def receive_logs(pc_id: str, request: Request):
    """매크로가 보내는 로그 배치 수신"""
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    mark_seen(ns(tenant, pc_id))   # ★어떤 요청이든 = 그 PC 프로세스가 살아있다는 증거★
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400)
    logs = data.get("logs", [])
    for entry in logs[:50]:   # 배치당 최대 50개
        level   = str(entry.get("level", "info"))[:10]
        message = str(entry.get("message", ""))[:500]
        if message:
            await insert_log(ns(tenant, pc_id), level, message)
            # ★★로그 경로는 둘이다 — 여기도 부팅을 봐야 한다 [S2 치명]★★
            #   매크로는 WS 가 끊겨 있으면 이 HTTP 로 로그를 보낸다(report_module _flush_logs).
            #   초판은 WS 쪽에만 감지를 달아서, ★부팅 직후 WS 가 아직 안 붙은 경우★ 의
            #   [BOOT] 를 통째로 놓쳤다. 실측: 부팅 83회 중 4회가 서버에 [BOOT] 무기록.
            #   그러면 expect_restart 가 True 로 남아 ★다음번 사람의 재시작을 삼킨다★ =
            #   "껐다 켜면 순환 해제" 보장이 조용히 깨진다.
            if "[BOOT" in message:
                try:
                    _rot_note_boot(ns(tenant, pc_id), message)
                except Exception as _be:
                    print(f"[순환] 부팅 감지 실패(무시): {_be}")
    return JSONResponse({"ok": True, "count": len(logs)})


@app.get("/commands/recent")
async def recent_commands(request: Request):
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    cmds = _strip_cmds(await get_recent_commands(20, ns_prefix=("" if tenant == "main" else tenant)), tenant)
    return JSONResponse({"commands": cmds})


# ══════════════════════════════════════════════════════════════════════════════
# 실시간 화면 보기 (2026-07-31 신설)
# ══════════════════════════════════════════════════════════════════════════════
# ★디스크·DB를 쓰지 않는다★ — PC별 ★최신 1프레임★만 메모리에 둔다. 프레임은 1.5초마다
# 들어오는 휘발성 데이터라 볼륨에 쌓으면 순식간에 고갈되고(버그스샷 prune을 만든 이유),
# Railway 재배포마다 어차피 사라진다. 지금 보고 있는 것만 보여주면 되는 기능이다.
#
# ★"보는 사람 없으면 매크로가 스스로 끈다"★ — 업로드 응답이 204면 매크로가 전송을 멈춘다.
# 대시보드가 프레임을 마지막으로 가져간 지 LIVE_TTL초가 지나면 아무도 안 본다고 판단한다.
# 탭을 그냥 닫아도(live_off를 못 보내도) 자동으로 멈추는 안전장치 — 함대 20대가 공인IP
# 하나를 공유하므로 '켠 줄도 모르고 계속 흐르는' 상태를 절대 만들면 안 된다.
LIVE_FRAMES: dict = {}          # {"tenant::PC-01": {"jpg": bytes, "meta": dict, "ts": float}}
# ★'보는 중' 표시는 프레임과 분리해서 둔다★ — 프레임 안에 seen을 두면 첫 프레임이 도착할 때
# seen이 아직 0이라 서버가 곧바로 204를 돌려주고 매크로가 꺼진다(= 화면이 영영 안 뜬다).
# 대시보드는 프레임이 없어도 /meta를 1초마다 두드리므로, 여기서 관심을 먼저 등록해 둔다.
LIVE_WATCH: dict = {}           # {"tenant::PC-01": 마지막 조회 epoch}
LIVE_TTL = 15.0                 # 이 시간 동안 조회가 없으면 '보는 사람 없음'
LIVE_MAX_BYTES = 512 * 1024     # 프레임 상한 — 640x360 q40이면 40KB대라 넉넉하다


def _live_touch(key: str):
    """대시보드가 이 PC를 보고 있다고 표시. 오래된 항목은 함께 청소."""
    now = time.time()
    LIVE_WATCH[key] = now
    # 메모리 누수 방지 — 아무도 안 보는 지 오래된 프레임/관심은 버린다(프로세스 메모리 dict).
    for k in [k for k, v in LIVE_WATCH.items() if now - v > 600]:
        LIVE_WATCH.pop(k, None)
        LIVE_FRAMES.pop(k, None)


@app.post("/live/{pc_id}")
async def upload_live_frame(pc_id: str, request: Request):
    """매크로 → 서버. 본문은 JPEG 원본, 메타는 X-Live-Meta 헤더(JSON).
    반환 204 = 보는 사람 없으니 그만 보내라."""
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    pc_id = clean_pc_id(pc_id)
    body = await request.body()
    if len(body) > LIVE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="프레임이 너무 큽니다")
    try:
        meta = json.loads(request.headers.get("X-Live-Meta") or "{}")
    except Exception:
        meta = {}
    key = ns(tenant, pc_id)
    now = time.time()
    # ★아무도 안 보는 프레임은 애초에 담지 않는다(2026-08-06 감사 major)★ — 예전엔 무조건 저장한 뒤
    #   204만 돌려줘서, 대시보드가 한 번도 조회하지 않은 pc_id는 LIVE_WATCH에 없어 청소 대상에도
    #   못 들어갔다(=영구 잔류). 매번 다른 이름으로 512KB씩 올리면 서버가 OOM으로 죽는다.
    if now - LIVE_WATCH.get(key, 0.0) > LIVE_TTL:
        LIVE_FRAMES.pop(key, None)
        return Response(status_code=204)      # 아무도 안 봄 → 매크로가 스스로 끈다
    LIVE_FRAMES[key] = {"jpg": body, "meta": meta, "ts": now}
    # 2차 안전망: 시청 중이라도 총량 상한(동시 시청은 한두 대지 수십 대가 아니다)
    if len(LIVE_FRAMES) > 40:
        for k in sorted(LIVE_FRAMES, key=lambda k: LIVE_FRAMES[k]["ts"])[:len(LIVE_FRAMES) - 40]:
            LIVE_FRAMES.pop(k, None)
    return JSONResponse({"ok": True})


@app.get("/live/{pc_id}.jpg")
async def get_live_frame(pc_id: str, request: Request):
    """대시보드 → 서버. 최신 프레임 1장. 가져갈 때마다 '보는 중' 시각을 갱신한다."""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    key = ns(tenant, clean_pc_id(pc_id))
    _live_touch(key)
    f = LIVE_FRAMES.get(key)
    if not f:
        raise HTTPException(status_code=404, detail="아직 프레임이 없습니다")
    return Response(content=f["jpg"], media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/live/{pc_id}/meta")
async def get_live_meta(pc_id: str, request: Request):
    """클릭 좌표 + 단계 자막 + 프레임 나이(초)."""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    key = ns(tenant, clean_pc_id(pc_id))
    # ★프레임이 없어도 관심은 등록한다★ — 대시보드를 연 직후엔 아직 프레임이 없는데,
    #   여기서 등록해 두지 않으면 매크로의 첫 프레임이 곧바로 204를 맞고 꺼진다.
    _live_touch(key)
    f = LIVE_FRAMES.get(key)
    if not f:
        return JSONResponse({"alive": False})
    return JSONResponse({"alive": True, "age": round(time.time() - f["ts"], 1),
                         **(f.get("meta") or {})})


def _base_pc(pc_id: str) -> str:
    """멀티계정 가상 id → 물리 PC id. 'PC-20b' → 'PC-20' (접미사 b/c/d)."""
    s = pc_id.strip()
    return s[:-1] if len(s) > 1 and s[-1] in "bcd" and s[-2].isdigit() else s


async def enrich_cmd_args(tenant: str, pc_id: str, command: str, args: dict) -> dict:
    """★배달 직전에★ 비밀·주소록을 args 에 채운다. DB 에는 저장하지 않는다.

    ★왜 '배달 직전'인가 (2026-08-16 자기 리뷰에서 잡음)★
    처음엔 insert 할 때 한 번만 채우게 짰는데, 매크로는 WS 가 끊기면 ★HTTP 폴링★으로
    같은 명령을 가져간다. 그 경로는 DB 행을 그대로 읽으므로 마스킹된 '***' 를 비번으로
    받아 로그인에 실패한다. → WS·폴링 ★두 경로 모두★ 여기를 거치게 한다.

    - parsec_id/parsec_pw : 서버 설정(대시보드 세션으로만 수정 가능)
    - peer_id             : 파섹 주소록. ★매크로는 주소록을 조회하지 않는다★
                            (매크로↔파섹 분리 — 매크로가 죽어도 주소록은 서버에 남는다)

    ★acct_tour 도 같은 대접(2026-08-17)★
    계정 순회는 내부에서 switch_launcher 를 계정 수만큼 돌린다. peer_id 를 안 채우면
    acct_tour.start() 가 "peer_id 가 없다"로 ★시작조차 안 한다★. 여기 한 줄이 린치핀.

    ★switch_account 도 같은 대접 (2026-08-18 사용자 지시)★
    "이제 계정전환하면 본컴도 바뀌게" — switch_account 는 오랫동안 ★원격컴 크롬만★
    갈아끼웠다. 이제 매크로가 본컴(파섹→런처)부터 바꾸는 통짜 경로로 승격됐는데,
    peer_id 가 없으면 본컴에 갈 주소가 없어 예전처럼 원격컴만 하고 만다.
    → 여기 목록에 넣는 것이 그 승격의 린치핀이다(switch_launcher 와 같은 이유).

    ★find_host 도 같은 대접 (2026-08-18)★
    본컴이 어느 계정으로 켜졌는지 찾는 명령. 기본은 원격컴 크롬만 훑으므로 peer_id 가
    필요 없지만, adopt=true 로 보내면 찾은 계정으로 ★정식 전환★(switch_account)까지
    이어진다. 그때 peer_id 가 없으면 본컴이 안 바뀌어 반쪽이 된다.
    """
    if command == "set_info":
        # ★★마스킹본이 info.txt 의 진짜 비번을 '***' 로 덮는다 (2026-08-20 감사)★★
        #   DB 에는 비번을 '***' 로 마스킹해 저장한다(이력 평문 방지). 그런데 WS 재접속·
        #   폴링으로 ★다시 배달★ 될 때는 그 DB 행이 그대로 나간다. 매크로의 info.txt
        #   화이트리스트는 `계정N_비번` 을 정상 키로 받으므로 ★'***' 를 진짜 비번으로 쓴다.★
        #   → 그 PC 의 비번이 파괴되고, 다음 계정전환부터 로그인이 전부 실패한다.
        #   C6 의 peer_id 3Hx42I… 사고와 ★완전히 같은 기계★ 다.
        #   되살릴 원본이 서버에 없으므로(마스킹이 목적) ★그 칸을 빼는 것★ 이 정답이다.
        #   칸이 빠지면 매크로는 그 칸만 안 바꾼다 — 파괴보다 훨씬 낫다.
        _kv = dict((args or {}).get("kv") or {})
        _drop = [k for k, v in _kv.items() if str(v) == "***"]
        for k in _drop:
            _kv.pop(k, None)
        if _drop:
            print(f"[명령] set_info 마스킹본 {len(_drop)}칸 제외(재배달) — {_drop}")
        return {**dict(args), "kv": _kv}
    # ★kill_game 추가 (2026-08-22)★ — 파섹으로 본컴 작업관리자를 열어 게임을 죽인다.
    #   peer_id 가 없으면 붙을 본컴이 없어 시작조차 못 한다(switch_launcher 와 같은 린치핀).
    if command not in ("switch_launcher", "acct_tour", "switch_account", "find_host",
                       # ★plrow_shot 추가 (2026-08-22)★ — 계정줄 템플릿 촬영도
                       #   connect_parsec 를 타므로 peer_id + 파섹 자격증명이 필요하다.
                       #   ★chrome_view 로는 못 한다★ — 파섹 로그인 화면에서 멈춘다(실측).
                       "kill_game", "plrow_shot"):
        return dict(args)
    out = dict(args)
    # ★★peer_id 는 ★무조건★ 주소록으로 덮어쓴다 (2026-08-19 실사고, PC-21)★★
    #   예전 조건은 `if not out.get("peer_id")` 였다. 그런데 DB 에 저장되는 args 는
    #   ★마스킹본("3Hx42I…")★ 이고 그건 truthy 라 ★덮어쓰지 않고 그대로 배달됐다.★
    #   그래서 두 번째 배달부터 매크로가 ★7자짜리 peer_id★ 로 파섹 [Join] 을 눌렀고,
    #   아무 데도 안 붙어 "런처가 안 보인다"로만 보였다(원인이 두 단계 뒤에 나타남).
    #   PC-21 계정3 전환이 이걸로 실패했다 — 첫 시도 27자 OK, 재시도 7자 FAIL.
    #   parsec_pw 는 원래도 무조건 덮어쓰고 있었다. peer_id 만 조건부였던 게 구멍.
    #   → 주소록에 값이 있으면 항상 그걸 쓴다. 없을 때만 들어온 값을 남긴다.
    pmap = await _get_parsec_map(tenant)
    base = _base_pc(pc_id)
    num = "".join(ch for ch in base if ch.isdigit()).lstrip("0") or base
    _pid = pmap.get(num) or pmap.get(base) or ""
    if _pid:
        out["peer_id"] = _pid
    elif "…" in str(out.get("peer_id") or ""):
        out["peer_id"] = ""          # 주소록에 없는데 마스킹본뿐이면 빈 값이 낫다
                                     # (매크로가 "peer_id 없음"으로 멈춘다 — 오접속보다 안전)
    for key in ("parsec_id", "parsec_pw"):
        v = (await get_setting(ns(tenant, key))) or ""
        if v:
            out[key] = v
    return out


# ★★브로드캐스트 'all' 은 A7 위반 규모다 (2026-08-20 감사)★★
#   get_pending_command(..., all_key=ns(tenant,"all")) 이 설계상 브로드캐스트를 지원해서,
#   POST /command/all 한 번이면 ★폴링 창에 걸린 PC 전부★ 가 그 명령을 실행한다.
#   몇 대가 실행할지가 ack 레이스로 결정된다 = 되돌릴 수도 셀 수도 없다.
#   대시보드 JS 에는 이걸 부르는 코드가 없다(전수 확인) — 즉 잃을 기능이 0 이다.
#   막을 수단이 있는데 안 막으면 '말로 남긴 규칙' 이 되고, 그건 전부 다시 깨졌다(§A6).
_BROADCAST_IDS = {"all", "ALL", "All", "*"}


@app.post("/command/{pc_id}")
async def send_command(pc_id: str, request: Request):
    # ★명령 '주입'은 대시보드 세션 전용(2026-07-27 보안감사 critical).
    #   기존엔 API 키로도 주입이 가능했는데, API 키는 배포되는 exe·공개 소스에 각인될 수밖에
    #   없어(구조적) 유출을 전제해야 한다. 실제로 공개 저장소에 평문 노출돼 있었고,
    #   그 키 하나로 로그인 없이 함대 전체에 시작/정지/판매 명령을 넣을 수 있었다.
    #   매크로·업데이터는 명령을 '폴링(GET)'하고 'ack'만 하므로 이 변경에 기능 손실이 없다.★
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    # ★브로드캐스트 차단 [A7]★ — 위 _BROADCAST_IDS 주석 참조.
    #   막다가 잃는 기능이 0 이라(대시보드에 호출부 없음) 그냥 거부한다.
    if str(pc_id).strip() in _BROADCAST_IDS:
        raise HTTPException(
            status_code=400,
            detail="브로드캐스트 명령은 막혀 있습니다(A7) — PC 를 하나씩 지정하십시오")
    body = await request.json()
    command = body.get("command")
    if not command:
        raise HTTPException(status_code=400, detail="command 필드 필요")
    args = body.get("args", {})
    nspc = ns(tenant, pc_id)
    _rot_result: dict = {}          # ▶시작이면 무장 결과를 응답에 실어 화면에 알린다

    # ★비밀은 서버가 끼워넣는다 (2026-08-16, switch_launcher)★
    #   대시보드는 비번을 ★모른다★ — 브라우저에도, 명령 DB 행에도, 명령 이력 화면에도
    #   안 남는다. 매크로로 나가는 그 순간에만 args 에 실린다.
    #   ★매크로 API 키 경유로 만들면 안 되는 이유★: 그 키는 공개 exe·공개 저장소에 각인돼
    #   있어(구조적) 누구나 꺼낼 수 있다. 여기는 대시보드 세션 전용 경로라 그게 막힌다.
    #   peer_id 도 같은 원리로 서버가 채운다 — 매크로는 파섹 주소록을 조회하지 않는다
    #   (매크로↔파섹 분리 원칙: 매크로가 죽어도 주소록은 서버에 남는다).
    send_args = await enrich_cmd_args(tenant, pc_id, command, args)
    if command == "set_info":
        # ★계정 비번을 이력에 남기지 않는다 (2026-08-17)★
        #   set_info 는 info.txt 의 계정 칸(아이디·비번·PIN…)을 채우는 명령이라
        #   args 를 그대로 저장하면 ★21대치 비밀번호가 명령 이력에 평문으로 쌓인다.★
        #   매크로로 나가는 send_args 는 원본 그대로, DB·화면에는 마스킹본만.
        _kv = (args.get("kv") or {})
        args = {"kv": {k: ("***" if ("비번" in k or "PIN" in k) else v)
                       for k, v in _kv.items()},
                "_note": f"{len(_kv)}칸"}
    elif command in ("switch_launcher", "acct_tour", "switch_account", "find_host"):
        # DB·이력에는 ★마스킹된 것만★ 남긴다 (아래 enrich 가 배달 때마다 다시 채운다)
        args = {**args, "peer_id": (send_args.get("peer_id") or "")[:6] + "…",
                "parsec_pw": "***" if send_args.get("parsec_pw") else ""}

    # ★★계정 자동순환 무장/해제 (2026-08-20 사용자 지시)★★
    #   "이게 플켯다고 하는게 아니라 ★시작을 눌러줫을때만★ 그작업을 하면되고"
    #   → 방아쇠는 ★부팅이 아니라 이 ▶시작 명령★ 이다. 정지/종료는 사람의 뜻이므로 해제.
    #   순환 엔진이 스스로 보내는 start 는 _rot_send 를 쓰므로 여기를 안 거친다
    #   (그쪽은 이미 무장 상태를 들고 있다 — 이중 무장/덮어쓰기 없음).
    try:
        if command == "start" and bool((args or {}).get("rotate")):
            _ok, _why = await _rot_arm(tenant, pc_id)
            await _rot_save(force=True)
            _rot_result = {"armed": _ok, "why": _why}
            if not _ok:
                print(f"[순환] {pc_id} 무장 거부: {_why}")
        elif command in ROT_TASKS and bool((args or {}).get("rotate")):
            # ★전 계정 순환 작업 (2026-08-23)★ 상단 [🔁 전 계정 순환] 버튼만 rotate 를 싣는다.
            #   카드 우클릭·[🎯 선택 카드만] 은 rotate 가 없으므로 예전처럼 한 번만 하고 끝난다.
            _ok, _why = await _rot_arm(tenant, pc_id, task=command)
            await _rot_save(force=True)
            _rot_result = {"armed": _ok, "why": _why}
            if not _ok:
                print(f"[순환] {pc_id} 작업순환({command}) 무장 거부: {_why}")
        elif command in ("update", "update_only", "restart"):
            # ══════════════════════════════════════════════════════
            # ★★업데이트/재시작은 '사람이 껐다 켠 것' 이 아니다 (2026-08-23)★★
            #
            # ★실사고★ 주인님이 v1.1.644 전 함대 업데이트를 누르자 순환이 통째로 풀렸다.
            #   디스크에는 14대가 무장돼 있는데 서버 메모리 _ROT 에는 1대만 남았다.
            #   주인님: "위쪽 메뉴로 시작 눌럿는데 순환이 안떠있는데 괜찮은거지?"
            #
            # ★왜 풀렸나★ update 는 updater 에서 stop_macro() → start_macro() 다.
            #   즉 ★새 부팅★ 이고 매크로가 새 부팅 지문 [BOOT#uuid] 을 보낸다.
            #   _rot_note_boot 은 그걸 "사람이 껐다 켬" 으로 읽고 순환을 해제한다.
            #   그 판정 자체는 옳다 — 사람이 끈 건 존중해야 한다. 다만
            #   ★업데이트는 사람이 '끈' 게 아니라 '올린' 것★ 이고, 순환은 유지돼야 한다.
            #
            # ★고치는 법은 이미 있었다★ — 순환 엔진이 자기 재시작을 예고할 때 쓰는
            #   expect_restart 를 여기서도 세워둔다. 그러면 뒤이어 오는 부팅 지문을
            #   "예상된 재시작" 으로 소비하고 순환을 유지한다.
            #   ★기한을 둔다★ — 기한 없이 세우면 며칠 뒤 사람이 껐다 켠 것까지 삼킨다
            #   (그게 바로 8375 줄 주석이 경고하는 그 함정이다). 업데이트는 다운로드+
            #   재기동까지 넉넉잡아 5분이면 끝난다.
            # ══════════════════════════════════════════════════════
            try:
                _rk = ns(tenant, _base_pc(pc_id))
                _rs = _ROT.get(_rk)
                if _rs:
                    _rs["expect_restart"] = True
                    _rs["expect_until"] = _rot_now() + 300.0    # 5분
                    await _rot_save(force=True)
                    print(f"[순환] {_rk} {command} — 재시작 예고(5분) → 순환 유지")
            except Exception as _ue:
                print(f"[순환] {command} 재시작 예고 실패(무시): {_ue}")
        elif command in ("stop", "exit"):
            if _rot_disarm(tenant, pc_id, f"사람이 {command}"):
                await _rot_save(force=True)
                await _rot_say(tenant, pc_id, f"{command} 명령으로 순환을 해제했습니다")
    except Exception as _re:
        print(f"[순환] 무장/해제 실패(무시): {_re}")

    cmd_id = await insert_command(nspc, command, args)
    # 매크로 WS 연결되어 있으면 즉시 전달
    ws_sent = await send_command_to_macro(nspc, command, send_args, cmd_id)
    # 브로드캐스트 (명령 내역 갱신용)
    await _push_cmd_history(tenant)
    return JSONResponse({"ok": True, "id": cmd_id, "ws": ws_sent, **_rot_result})


@app.delete("/status/{pc_id}")
async def remove_pc(pc_id: str, request: Request):
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    await delete_pc_all_data(ns(tenant, pc_id))
    await push_state(tenant)
    return JSONResponse({"ok": True})


@app.websocket("/ws/macro/{pc_id}")
async def macro_websocket(websocket: WebSocket, pc_id: str):
    """매크로 클라이언트 WebSocket — 상태 수신 + 명령 송신"""
    # API 키 인증 (쿼리 파라미터) → 테넌트 결정 (만료 테넌트 차단)
    # ★HTTP와 동일한 상수시간 비교 + IP 실패 카운터 공유(2026-07-27): 여기만 딕셔너리 조회라
    #   무제한 키 추측 채널로 쓸 수 있었다.★
    api_key = websocket.query_params.get("key", "")
    _wip = _ip_from_xff(websocket.headers.get("x-forwarded-for", ""),
                        websocket.client.host if websocket.client else "")
    if _key_probe_blocked(_wip):
        await websocket.close(code=1008)
        return
    tenant = None
    for _k, _tn in KEY_TO_TENANT.items():
        if hmac.compare_digest(api_key, _k):
            tenant = _tn
    if not tenant or tenant_blocked(tenant):
        # 미등록 키만 추측 카운터에 계상 — 킬/만료 테넌트의 재접속 폭주가 자기 IP를 잠가
        # /license 확인(429→network 오분류)까지 막는 걸 방지 (check_api_key와 동일 원칙)
        if not tenant:
            _key_probe_failed(_wip)
        await websocket.close(code=1008)
        return
    pc_id = clean_pc_id(pc_id)   # 에코 일관성(저장 키 소독과 동일)
    nspc = ns(tenant, pc_id)
    await websocket.accept()
    macro_ws_connections[nspc] = websocket
    try:
        # 대기 중인 명령 즉시 전달 (브로드캐스트 'all'도 테넌트 스코프)
        pending = await get_pending_command(nspc, all_key=ns(tenant, "all"))
        if pending:
            # ★★WS 재접속 경로도 언마스킹을 거쳐야 한다 (2026-08-20 감사)★★
            #   여기만 DB 행을 ★그대로★ 보내고 있었다. 폴링 경로(GET /command/{pc})는
            #   enrich_cmd_args 를 거치는데 이 경로는 안 거쳤다 = PC-21 peer_id 사고가
            #   ★절반만★ 막혀 있었다. WS 가 끊겼다 붙는 건 흔한 일이라 실전 경로다.
            _pargs = await enrich_cmd_args(tenant, pc_id,
                                           pending["command"], pending.get("args") or {})
            await websocket.send_text(json.dumps({
                "type": "command", "id": pending["id"],
                "command": pending["command"], "args": _pargs
            }))
        while True:
            raw = await websocket.receive_text()
            # ★차단 재검사(2026-08-06 감사 major)★ — 핸드셰이크 때 한 번만 보면, 이미 붙어 있던
            #   렌탈 매크로가 킬 이후에도 상태·로그를 계속 기록하고 명령까지 받아간다(반쪽 차단).
            if tenant_blocked(tenant):
                await websocket.close(code=1008)
                return
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            msg_type = msg.get("type", "")
            if msg_type == "status":
                payload = msg.get("payload", {})
                payload["pc_id"] = nspc   # 저장 키와 일치(테넌트 필터 기준) — 출력 시 벗김
                await upsert_status(nspc, payload)
                errors = payload.get("errors") or []
                for e in errors[:3]:
                    await insert_log(nspc, "warn", str(e))
                await push_state(tenant)
            elif msg_type == "log":
                logs = msg.get("logs", [])
                for entry in logs:
                    _m = entry.get("message", "")
                    await insert_log(nspc, entry.get("level", "info"), _m)
                    # ★매크로 프로세스가 새로 떴는가★ — 순환 해제 판정 근거.
                    #   WS 재연결이 아니라 ★부팅★ 일 때만 나오는 줄을 본다
                    #   (PC-21b 처럼 60초마다 WS 가 끊겼다 붙는 PC 가 있다).
                    if "[BOOT" in str(_m):
                        try:
                            _rot_note_boot(nspc, _m)
                        except Exception as _be:
                            print(f"[순환] 부팅 감지 실패(무시): {_be}")
            elif msg_type == "ack":
                cmd_id = msg.get("command_id")
                if cmd_id and await _cmd_belongs_to(cmd_id, tenant):
                    await ack_command(cmd_id)
            elif msg_type == "pong":
                pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        macro_ws_connections.pop(nspc, None)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # session 쿠키로 인증 → 테넌트별 연결
    session_token = websocket.cookies.get("session")
    tenant = valid_session(session_token)
    # ★만료 테넌트 차단(2026-07-27 보안감사): HTTP는 check_session이 만료를 거르는데
    #   여기만 valid_session(서명·유효기간만 확인)이라, 이용 기간이 끝난 지인이
    #   WS로는 실시간 데이터를 계속 받아볼 수 있었다.★
    if not tenant or tenant_blocked(tenant):
        await websocket.close(code=1008)
        return
    await manager.connect(websocket, tenant)
    # 초기 상태 전송 (updater 정보 포함)
    pcs = await _build_full_state(tenant)
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
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    mark_seen(ns(tenant, pc_id))   # ★어떤 요청이든 = 그 PC 프로세스가 살아있다는 증거★
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    nspc = ns(tenant, pc_id)
    data["pc_id"] = nspc
    await upsert_status(nspc, data)
    # 중요 이벤트는 로그 테이블에 저장
    errors = data.get("errors") or []
    if errors:
        for e in errors[:3]:
            await insert_log(nspc, "warn", str(e))
    # WS 브로드캐스트 (updater 정보 포함)
    await push_state(tenant)
    return JSONResponse({"ok": True})


@app.post("/alert/{pc_id}")
async def receive_alert(pc_id: str, request: Request):
    """매크로 → 대시보드 실시간 알림. 캡차 3회 실패처럼 '사람이 지금 봐야 하는' 이벤트용.
    본문이 브라우저 DOM과 TTS로 그대로 흘러가므로 길이·문자 제한을 서버에서 건다."""
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    kind = re.sub(r"[^a-z0-9_]", "", str(data.get("kind") or "info").lower())[:32] or "info"
    message = str(data.get("message") or "").strip()[:200]
    if not message:
        raise HTTPException(status_code=400, detail="message 없음")
    # say = 소리내어 읽을 짧은 문구. 없으면 message를 읽는다.
    say = str(data.get("say") or "").strip()[:60]
    speak = bool(data.get("speak", True))
    nspc = ns(tenant, pc_id)
    await insert_log(nspc, "warn", f"[알림] {message}")
    await push_alert(tenant, clean_pc_id(pc_id), kind, message, speak, say)
    return JSONResponse({"ok": True})


# ─── 텔레그램 중계 API (매크로가 호출) ──────────────────────────────────────

@app.get("/telegram/status")
async def telegram_status(request: Request):
    """매크로가 부팅/주기적으로 물어본다. enabled면 매크로는 자기 봇 폴링을 끄고
    코드 수신을 서버 명령 큐에 맡긴다(봇 1개로 통합되는 지점)."""
    tenant = check_api_key(request)
    if not tenant:
        # ★차단 테넌트도 '중계 가능'만은 알려준다(2026-08-06 리뷰 major)★ — 여기서 403을 주면
        #   클라가 중계를 5분간 꺼버려(report_module.telegram_relay_enabled) 정작 '이용 중지'
        #   안내가 영영 못 나간다. 읽기 전용이라 정보 노출도 없다(자기 테넌트의 on/off뿐).
        supplied = request.headers.get("X-Api-Key", "")
        for _k, _tn in KEY_TO_TENANT.items():
            if supplied and hmac.compare_digest(supplied, _k) and tenant_blocked(_tn):
                tenant = _tn
        if not tenant:
            raise HTTPException(status_code=403)
    return JSONResponse({"enabled": bool(tg_enabled() and tenant_chat_id(tenant))})


# ══════════════════════════════════════════════════════════════════════════
# PC별 텔레그램 음소거 (2026-08-20 사용자 요청)
#
#   사용자: "pc 22, 23 이랑 저거 텔레그램 메세지 안오게 5시간만 안오게해"
#
# ★왜 서버에 두나★ 텔레그램은 두 곳에서 나간다 — ①매크로 자신(핀/사망/전환실패…)
#   ②스카우터(운영 스크립트). manned.txt 는 ②만 막는다. ★여기가 둘의 공통 출구★ 라
#   한 자리에서 막힌다. 그리고 매크로 재배포 없이 즉시 먹는다.
# ★반드시 만료가 있다★ — 무기한 음소거는 잊혀지고, 잊혀진 음소거는 사고를 가린다.
#   (manned.txt 가 08-18 줄을 이틀간 달고 있어 멀쩡한 6대가 감시에서 빠져 있었다)
_TG_MUTE: dict[str, float] = {}      # pc_id(접미사 제거) → 만료 epoch


def _tg_muted(pc_id: str) -> float:
    """음소거 중이면 남은 초, 아니면 0. 만료된 항목은 즉시 청소한다."""
    base = _base_pc(clean_pc_id(pc_id) or "")
    now = time.time()
    for k in [k for k, v in _TG_MUTE.items() if v <= now]:
        _TG_MUTE.pop(k, None)
    return max(0.0, _TG_MUTE.get(base, 0.0) - now)


@app.post("/telegram/mute/{pc_id}")
async def telegram_mute(pc_id: str, request: Request):
    """PC 텔레그램 음소거. body: {"hours": 5}  / hours<=0 이면 해제.

    대시보드 세션 전용 — 알림을 끄는 일이라 사람이 눌러야 한다.
    """
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    hours = float(body.get("hours") or 0)
    base = _base_pc(clean_pc_id(pc_id) or "")
    if not base:
        raise HTTPException(status_code=400, detail="pc_id 이상")
    if hours <= 0:
        _TG_MUTE.pop(base, None)
        return JSONResponse({"ok": True, "pc": base, "muted": False})
    hours = min(hours, 24.0)                  # 하루 넘는 음소거는 만들지 않는다
    _TG_MUTE[base] = time.time() + hours * 3600.0
    return JSONResponse({"ok": True, "pc": base, "muted": True,
                         "hours": hours,
                         "until": time.strftime("%H:%M", time.localtime(_TG_MUTE[base]))})


@app.get("/telegram/mute")
async def telegram_mute_list(request: Request):
    if not check_session(request):
        raise HTTPException(status_code=401)
    now = time.time()
    return JSONResponse({"muted": {k: round((v - now) / 60.0, 1)
                                   for k, v in _TG_MUTE.items() if v > now}})


@app.post("/telegram/send/{pc_id}")
async def telegram_send(pc_id: str, request: Request):
    """매크로 → 텔레그램 텍스트 중계. 매크로에 봇 토큰이 없어도 알림이 간다."""
    tenant = check_api_key(request)
    if not tenant:
        # ★차단 테넌트의 '정지 안내' 예외(2026-08-06 리뷰): 킬/만료된 테넌트가 check_api_key에서
        #   막히면 "이용이 중지되었습니다" 텔레그램이 영영 못 나간다. ★단, 이 폴백이 레이트리밋을
        #   우회하면 2026-07-27에 막은 키 추측 오라클이 재개방된다(2라운드 critical)★ —
        #   probe 차단 IP는 여기서도 거부하고, '등록된 키 + 차단된 테넌트'만 통과시킨다.
        #   미등록 키는 위 check_api_key가 이미 실패로 계상했으므로 그냥 403.
        #   전송처는 어차피 아래에서 그 테넌트 '자신의' chat_id로만 조회되므로 남용 여지 없음.★
        ip = _client_ip(request)
        if _key_probe_blocked(ip):
            raise HTTPException(status_code=403)
        supplied = request.headers.get("X-Api-Key", "")
        cand = None
        for _k, _tn in KEY_TO_TENANT.items():
            if hmac.compare_digest(supplied, _k):
                cand = _tn
        if not (cand and tenant_blocked(cand)):
            raise HTTPException(status_code=403)
        # ★정지 안내만 통과시킨다(2026-08-07 리뷰)★ — 예산을 다른 알림(회랑 진행·복구 경고 등)이
        #   먼저 써버리면 정작 '이용이 중지되었습니다'가 429로 잘려 이용자는 이유도 모른 채 멈춘다.
        #   _block()의 안내는 "⛔"로 시작한다.
        try:
            _peek = await request.json()
        except Exception:
            _peek = {}
        if not isinstance(_peek, dict):      # 본문이 [] / "x" / 123이면 .get에서 500 (리뷰 minor)
            _peek = {}
        if not str(_peek.get("text") or "").lstrip().startswith("⛔"):
            raise HTTPException(status_code=403, detail="차단 상태에서는 정지 안내만 전송됩니다")
        # ★남용 상한(2026-08-06 감사): 이 예외는 '정지 안내 몇 줄'을 위한 것이다.
        #   상한이 없으면 킬된 지인이 소유자 봇을 무제한 중계기로 계속 쓴다.★
        _rec = _KILL_TG.get(cand)
        _nw = time.time()
        if not _rec or _nw - _rec["since"] > 3600:
            _rec = {"n": 0, "since": _nw}
            _KILL_TG[cand] = _rec
        if _rec["n"] >= 3:
            raise HTTPException(status_code=429, detail="정지 안내 전송 상한")
        _rec["n"] += 1
        tenant = cand
    chat = tenant_chat_id(tenant)
    if not (tg_enabled() and chat):
        return JSONResponse({"ok": False, "reason": "disabled"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    text = str(data.get("text") or "").strip()[:1000]
    if not text:
        raise HTTPException(status_code=400, detail="text 없음")
    name = clean_pc_id(pc_id)
    # ★음소거 확인 (2026-08-20)★ — 조용히 삼키지 않고 ok:true + muted 로 알려준다.
    #   매크로는 실패로 재시도하지 않고, 나중에 로그를 보면 왜 안 왔는지 알 수 있다.
    _left = _tg_muted(pc_id)
    if _left > 0:
        print(f"[tg-mute] {name} 음소거 중({_left/60:.0f}분 남음) — 전송 생략: {text[:60]}",
              flush=True)
        return JSONResponse({"ok": True, "muted": True,
                             "minutes_left": round(_left / 60.0, 1)})
    mid = await tg_send_text(chat, f"{name} | {text}" if name else text)
    if mid is None:
        return JSONResponse({"ok": False, "reason": "send_failed"}, status_code=502)
    if bool(data.get("expect_reply")):
        await tg_map_put(mid, ns(tenant, pc_id), chat)
    return JSONResponse({"ok": True, "message_id": mid})


@app.post("/telegram/photo/{pc_id}")
async def telegram_photo(pc_id: str, request: Request, file: UploadFile = File(...)):
    """매크로 → 텔레그램 사진 중계(캡차 스샷). expect_reply면 답장 라우팅 대상으로 등록."""
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    chat = tenant_chat_id(tenant)
    if not (tg_enabled() and chat):
        return JSONResponse({"ok": False, "reason": "disabled"}, status_code=503)
    form = await request.form()
    caption = str(form.get("caption") or "").strip()[:800]
    expect_reply = str(form.get("expect_reply") or "").lower() in ("1", "true", "yes")
    raw = await file.read()
    if not raw or len(raw) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="이미지 크기 오류")
    name = clean_pc_id(pc_id)
    mid = await tg_send_photo(chat, f"{name} | {caption}" if name else caption, raw)
    if mid is None:
        return JSONResponse({"ok": False, "reason": "send_failed"}, status_code=502)
    if expect_reply:
        await tg_map_put(mid, ns(tenant, pc_id), chat)
    return JSONResponse({"ok": True, "message_id": mid})


@app.get("/command/{pc_id}")
async def poll_command(pc_id: str, request: Request):
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    mark_seen(ns(tenant, pc_id))   # ★어떤 요청이든 = 그 PC 프로세스가 살아있다는 증거★
    cmd = await get_pending_command(ns(tenant, pc_id), all_key=ns(tenant, "all"))
    if cmd:
        # ★WS 가 끊겨 폴링으로 받아가는 경로★ — DB 는 마스킹돼 있으므로 여기서 다시 채운다
        # (안 채우면 매크로가 '***' 를 파섹 비번으로 입력해 로그인 실패)
        cargs = await enrich_cmd_args(tenant, pc_id, cmd["command"], cmd["args"] or {})
        return JSONResponse({"command": cmd["command"], "args": cargs, "id": cmd["id"]})
    return JSONResponse({"command": None})


async def _cmd_belongs_to(cmd_id: int, tenant: str) -> bool:
    """명령 id의 소유 테넌트 확인(단건 DB 조회) — 타 테넌트 명령 ack/취소 차단.
    ※최근 N건 창 스캔 방식은 창 밖의 오래된 pending 명령 ack가 404 → 무한 재실행이 되므로 금지."""
    pc = await get_command_pc(cmd_id)
    return pc is not None and ns_of(pc) == tenant


async def _updater_cmd_belongs_to(cmd_id: int, tenant: str) -> bool:
    pc = await get_updater_command_pc(cmd_id)
    return pc is not None and ns_of(pc) == tenant


@app.post("/command/{pc_id}/ack/{cmd_id}")
async def ack_cmd(pc_id: str, cmd_id: int, request: Request):
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    if not await _cmd_belongs_to(cmd_id, tenant):
        raise HTTPException(status_code=404)
    ok = await ack_command(cmd_id)
    # 내역 브로드캐스트
    await _push_cmd_history(tenant)
    return JSONResponse({"ok": ok})


@app.delete("/commands/{cmd_id}")
async def cancel_cmd(cmd_id: int, request: Request):
    """pending 명령 취소 (dashboard용)"""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    if not await _cmd_belongs_to(cmd_id, tenant):
        raise HTTPException(status_code=404)
    ok = await cancel_command(cmd_id)
    await _push_cmd_history(tenant)
    return JSONResponse({"ok": ok})


# ─────────────────────────────────────────────────────────────────────────────
# Updater API (API key auth) — 업데이터 데몬이 호출
# ─────────────────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# 업데이터 원격 로그 (2026-08-20 신설)
# ══════════════════════════════════════════════════════════════════════════════
# ★왜 (PC-23 실사고)★
#   매크로가 죽으면 그 PC 는 완전 실명이 된다. 업데이터는 macro_state 한 칸만 보내고
#   자기 로그는 C:\auto\updater.log 에만 남기니, "되살리려 했나 / 왜 실패했나" 를 볼
#   방법이 없었다. 업데이터도 로그를 올리게 하고, 대시보드에서 매크로 로그와 같이 본다.
#
# ★저장은 기존 logs 표를 그대로 쓴다 — 키만 다르게★
#   "PC-01" → "PC-01.upd" 로 접미사를 붙여 같은 표에 넣는다. 새 표를 만들지 않으므로
#   스키마 변경도, insert_log 의 3000줄 자동 정리도 그대로 따라온다(PC당 3000 + 업데이터
#   3000 으로 자연히 분리된다 — 한쪽 폭주가 다른 쪽을 밀어내지 않는다).
#
# ★멀티계정 접미사는 벗긴다★ — 매크로 pc_id 는 PC-20b/c/d 로 갈라지지만 업데이터는
#   물리 PC 당 하나뿐이다. _base_pc 로 접어야 b/c/d 계정 어느 카드에서 열어도 같은
#   업데이터 로그가 보인다.
UPD_LOG_SUFFIX    = ".upd"
UPD_LOG_BATCH_MAX = 50        # 배치당 최대 줄 수(클라는 25씩 보낸다 — 여유분)
UPD_LOG_TS_SKEW   = 86400.0   # 클라 시각을 믿는 범위(초). 벗어나면 수신 시각을 쓴다


def _upd_log_key(tenant: str, pc_id: str) -> str:
    """업데이터 로그 저장 키. 'PC-20b' → 'PC-20.upd' (테넌트 네임스페이스 포함)."""
    return ns(tenant, _base_pc(clean_pc_id(pc_id)) + UPD_LOG_SUFFIX)


@app.post("/updater/log/{pc_id}")
async def receive_updater_logs(pc_id: str, request: Request):
    """업데이터가 보내는 로그 배치 수신 (API키 인증).

    ★매크로용 /log/ 와 일부러 분리했다★ — 그쪽 수신부는 메시지에 "[BOOT" 가 있으면
      계정 자동순환 상태기계(_rot_note_boot)를 건드린다. 업데이터 부팅은 매크로 부팅이
      아니므로 그 경로를 절대 타면 안 된다. 여기는 순수 저장만 한다.
    """
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    logs = data.get("logs")
    if not isinstance(logs, list):
        raise HTTPException(status_code=400, detail="logs 는 리스트여야 함")
    key = _upd_log_key(tenant, pc_id)
    now = time.time()
    n = 0
    for entry in logs[:UPD_LOG_BATCH_MAX]:
        if not isinstance(entry, dict):
            continue
        level   = str(entry.get("level", "info"))[:10]
        message = str(entry.get("message", ""))[:500]
        if not message:
            continue
        # ★클라가 찍은 시각을 그대로 쓴다★ — 배치는 20초~5분씩 묶여서 온다. 수신 시각으로
        #   적으면 한 배치가 통째로 같은 시각이 되어 사고 순서를 못 읽는다.
        #   단 시계가 어긋난 PC(부팅 직후 NTP 전 등)의 값을 그대로 믿으면 로그가 1970년이나
        #   2030년으로 튀어 목록에서 사라진다 → 하루 이상 어긋나면 수신 시각으로 대체.
        created = None
        try:
            t = float(entry.get("ts") or 0)
            if t > 0 and abs(now - t) <= UPD_LOG_TS_SKEW:
                created = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            created = None
        await insert_log(key, level, message, created_at=created)
        n += 1
    return JSONResponse({"ok": True, "count": n})


@app.get("/updater/logs/{pc_id}")
async def updater_logs(pc_id: str, request: Request):
    """대시보드가 읽는다 (세션 인증)."""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    return JSONResponse({"logs": await get_logs(_upd_log_key(tenant, pc_id), limit=1000)})


@app.post("/updater/status/{pc_id}")
async def updater_report_status(pc_id: str, request: Request):
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    nspc = ns(tenant, pc_id)
    data["pc_id"] = nspc
    await upsert_updater_status(nspc, data)
    await push_state(tenant)
    return JSONResponse({"ok": True})


@app.get("/updater/command/{pc_id}")
async def updater_poll_command(pc_id: str, request: Request):
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    cmd = await get_pending_updater_command(ns(tenant, pc_id), all_key=ns(tenant, "all"))
    if cmd:
        return JSONResponse({"command": cmd["command"], "args": cmd.get("args", {}), "id": cmd["id"]})
    return JSONResponse({"command": None})


@app.post("/updater/command/{pc_id}")
async def dashboard_send_updater_command(pc_id: str, request: Request):
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    body = await request.json()
    command = body.get("command")
    if not command:
        raise HTTPException(status_code=400, detail="command 필드 필요")
    cmd_id = await insert_updater_command(ns(tenant, pc_id), command, body.get("args", {}))
    return JSONResponse({"ok": True, "id": cmd_id})


@app.get("/updater/commands/recent")
async def dashboard_recent_updater_commands(request: Request, limit: int = 60):
    """★업데이터 명령 큐 조회 (2026-08-22 사고 146)★
    /commands/recent 는 ★매크로 큐★ 만 본다. 업데이터 큐는 여태 밖에서 볼 수가 없어서
    "업데이트를 눌러도 안 한다" 를 확증도 반증도 못 했다. 이게 그 눈이다."""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    limit = max(1, min(int(limit or 60), 300))
    # ★테넌트 격리★ — main 은 접두어가 없으므로 LIKE 로는 못 좁힌다(남의 테넌트가 샌다).
    #   넉넉히 받아서 ns_of() 로 거른 뒤 limit 만큼만 돌려준다.
    raw = await recent_updater_commands(min(limit * 6, 1200))
    out = []
    for r in raw:
        key = str(r.get("pc_id") or "")
        t, pid = split_ns(key)
        if t != tenant:
            continue
        r["pc_id"] = pid
        out.append(r)
        if len(out) >= limit:
            break
    return JSONResponse({"commands": out})


@app.post("/updater/command/{pc_id}/ack/{cmd_id}")
async def updater_ack_command(pc_id: str, cmd_id: int, request: Request):
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    if not await _updater_cmd_belongs_to(cmd_id, tenant):
        raise HTTPException(status_code=404)
    ok = await ack_updater_command(cmd_id)
    return JSONResponse({"ok": ok})


# ─────────────────────────────────────────────────────────────────────────────
# Bug API — 스크린샷 업로드/조회/삭제
# ─────────────────────────────────────────────────────────────────────────────

def _list_bug_files(tenant: str, pc_id: Optional[str] = None) -> list[dict]:
    result = []
    bdir = tenant_bugs_dir(tenant)
    if not os.path.isdir(bdir):
        return result
    for fname in sorted(os.listdir(bdir), reverse=True):
        if not fname.endswith('.png'):
            continue
        if pc_id:
            m = re.match(r'^(.+?)_\d{8}_\d{6}_', fname)
            # ★★베이스끼리 비교한다 (2026-08-23)★★ — 예전엔 정확일치라
            #   `PC-24b` 로 물어보면 `PC-24_...` 파일이 하나도 안 걸렸다.
            #   묻는 쪽이 접미사를 달고 오든 아니든, 스샷은 그 PC 의 것이다.
            if not m or _base_pc(m.group(1)) != _base_pc(pc_id):
                continue
        path = os.path.join(bdir, fname)
        try:
            size = os.path.getsize(path)
        except Exception:
            size = 0
        result.append({"filename": fname, "size": size})
    return result


# ── 버그스샷 자동 정리 ──────────────────────────────────────────────────────
# 정리 장치가 없어서 무한정 쌓였다(2026-07-30 실측 1032장 → 하루 만에 다시 835장).
# 디스크만 먹는 게 아니라 목록 API·대시보드 뱃지가 전부 느려진다.
# ★학습 크롭(ocrlearn_*)은 따로 더 넉넉히 잡는다★ — 그건 '모아야 뱅크가 채워지는'
#   자산이라 사고 증거와 같은 잣대로 지우면 안 된다.
BUG_KEEP_PER_PC = 40        # PC당 일반 스샷(사고 증거) 보관 수
BUG_KEEP_LEARN  = 120       # PC당 학습 크롭(ocrlearn_/ocrdiff_) 보관 수


def _prune_bugs(bdir: str, pc_id: str):
    """그 PC 것만 오래된 순으로 지운다. 업로드 때마다 도니까 그 PC 폴더만 훑는다.

    ★파일명이 {pc_id}_{YYYYMMDD}_{HHMMSS}_... 라 이름 정렬 = 시간 정렬이다★
      (mtime을 쓰면 파일 복사·볼륨 이전 때 전부 같은 시각이 되어 순서가 무너진다.)
    실패해도 업로드는 성공시킨다 — 정리하다 증거를 못 받는 게 더 나쁘다.
    """
    try:
        # ★단순 startswith 금지(2026-07-30 리뷰)★ — clean_pc_id가 언더스코어를 허용하므로
        #   'PC-01'의 접두사 매칭이 'PC-01_sub'의 파일까지 자기 그룹으로 집계한다.
        #   두 그룹이 섞여 정렬되면 숫자가 문자보다 앞이라 PC-01의 ★최신★ 파일부터
        #   지워진다(시간 역순 삭제). 목록 API와 같은 기준: pc_id 뒤에 곧장 타임스탬프.
        pat = re.compile(r"^" + re.escape(pc_id) + r"_\d{8}_\d{6}_")
        learn, other = [], []
        for f in os.listdir(bdir):
            if not pat.match(f) or not f.endswith(".png"):
                continue
            (learn if ("ocrlearn_" in f or "ocrdiff_" in f) else other).append(f)
        for group, keep in ((learn, BUG_KEEP_LEARN), (other, BUG_KEEP_PER_PC)):
            if len(group) <= keep:
                continue
            group.sort()                       # 이름순 = 오래된 것부터
            for f in group[:len(group) - keep]:
                try:
                    os.remove(os.path.join(bdir, f))
                except Exception:
                    pass
    except Exception as e:
        print(f"[bugs] prune 실패(무시): {e.__class__.__name__}: {e}")


@app.post("/bugs/{pc_id}")
async def upload_bug(pc_id: str, request: Request, file: UploadFile = File(...)):
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    pc_id = clean_pc_id(pc_id)   # 파일명 접두사도 저장 키 소독과 일관
    bdir = tenant_bugs_dir(tenant)
    os.makedirs(bdir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    orig = os.path.basename(file.filename or "bug.png")
    # ★.png 강제(2026-07-30 리뷰)★ — 목록·뱃지·prune이 전부 .png만 취급하므로,
    #   다른 확장자는 '보이지도 지워지지도 않는' 무한 축적 경로가 된다(볼륨 고갈).
    #   클라이언트는 항상 png를 보내므로 정상 경로엔 영향 없음. 대문자 .PNG는 소문자화.
    if orig.lower().endswith(".png"):
        orig = orig[:-4] + ".png"
    else:
        raise HTTPException(status_code=400, detail="png만 받습니다")
    # 반드시 {pc_id}_{YYYYMMDD}_{HHMMSS}_{orig} 형태로 저장해야 배지/목록이 작동함
    filename = f"{pc_id}_{ts}_{orig}"
    dest = os.path.join(bdir, filename)
    # ★크기 상한(2026-07-27 보안감사): 무제한이면 통짜로 메모리에 올려 OOM,
    #   반복 업로드로 /data를 채워 DB까지 마비시킬 수 있다. 스샷은 1280x720 PNG라 8MB면 충분.★
    content = await file.read()
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="파일이 너무 큽니다(최대 8MB)")
    with open(dest, 'wb') as f:
        f.write(content)
    _prune_bugs(bdir, pc_id)
    await push_state(tenant)
    return JSONResponse({"ok": True, "filename": filename})


@app.get("/bugs/download")
async def download_bugs_zip(request: Request, pc_id: Optional[str] = None):
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    # ★CSRF 방어(2026-07-27 보안감사): 이 GET은 '다운로드 후 서버 파일 삭제'라는 파괴적
    #   부수효과가 있는데, 쿠키가 SameSite=Lax라 다른 사이트의 링크·이미지 태그로도 실행된다
    #   (= 링크 한 번에 증거 스샷 전량 소실). 대시보드에서 온 요청만 허용한다.★
    _ref = request.headers.get("referer", "") or request.headers.get("origin", "")
    _host = request.headers.get("host", "")
    if _host and _ref and _host not in _ref:
        raise HTTPException(status_code=403, detail="cross-site request rejected")
    if not _ref:      # 링크·이미지 태그 직접 호출(Referer 없음)도 거부
        raise HTTPException(status_code=403, detail="direct request rejected")
    bugs = _list_bug_files(tenant, pc_id)
    if not bugs:
        raise HTTPException(status_code=404, detail="다운로드할 버그 이미지 없음")
    bdir = tenant_bugs_dir(tenant)
    buf = io.BytesIO()
    downloaded_paths = []
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for bug in bugs:
            path = os.path.join(bdir, bug["filename"])
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
    await push_state(tenant)
    zip_name = f"bugs_{pc_id or 'all'}_{int(time.time())}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={zip_name}"},
    )


@app.get("/bugs")
async def list_all_bugs(request: Request):
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    return JSONResponse({"bugs": _list_bug_files(tenant)})


@app.get("/bugs/{pc_id}")
async def list_pc_bugs(pc_id: str, request: Request):
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    return JSONResponse({"bugs": _list_bug_files(tenant, pc_id)})


@app.get("/bugs/image/{filename:path}")
async def serve_bug_image(filename: str, request: Request):
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    filename = os.path.basename(filename)
    path = os.path.join(tenant_bugs_dir(tenant), filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png")


@app.get("/tenants")
async def list_tenants(request: Request):
    """대시보드 '렌탈 관리' 패널용 (2026-08-06). ★main 세션에만 목록을 준다★ —
    렌탈 세션엔 is_main=false + 빈 목록(다른 지인이 있는지조차 노출 금지).
    비밀번호·api_key는 절대 싣지 않는다(화면에 필요 없음)."""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    if tenant != "main":
        return JSONResponse({"is_main": False, "tenants": []})
    rows = []
    for name in sorted(t for t in TENANTS if t != "main"):
        info = TENANTS.get(name) or {}
        rows.append({"name": name, "killed": name in KILLED_TENANTS,
                     "expires": info.get("expires") or "", "has_chat": bool(info.get("chat_id"))})
    return JSONResponse({"is_main": True, "tenants": rows})


def _rental_info_txt(tenant: str) -> str:
    """렌탈 설치용 info.txt — ★키·에디션은 채워서, 본인이 넣을 것만 비워서★ 준다.
    (주석은 '=' 없이 쓴다 — 업데이터/매크로 파서가 '='가 있는 줄을 전부 항목으로 읽는다.)"""
    key = (TENANTS.get(tenant) or {}).get("api_key") or ""
    return (
        "pc_id=PC-01\r\n"
        "edition=rental\r\n"
        f"control_api_key={key}\r\n"
        "server=\r\n"
        "password_digits=\r\n"
        "token=\r\n"
        "telegram_chat_id=\r\n"
        "anthropic_api_key=\r\n"
        "gemini_api_key=\r\n"
        "twocaptcha_api_key=\r\n"
        "total_slots=6\r\n"
        "screenshot_key=ctrl+q\r\n"
        "char1=\r\nchar2=\r\nchar3=\r\nchar4=\r\nchar5=\r\nchar6=\r\n"
        "\r\n"
        "[ 채워야 하는 것 ]\r\n"
        "pc_id       컴퓨터마다 다르게 (PC-01, PC-02 ...)\r\n"
        "server      접속하는 게임 서버 이름\r\n"
        "password_digits  퍼플 웹플레이 재접속 PIN\r\n"
        "telegram_chat_id  userinfobot이 알려주는 숫자 (판매자에게도 알려주세요)\r\n"
        "anthropic / gemini / twocaptcha  본인이 발급한 키\r\n"
        "total_slots 돌릴 캐릭터 수 (매뉴얼 2장 참고)\r\n"
        "token 은 비워두세요 (알림은 판매자 봇이 대신 보냅니다)\r\n"
    )


@app.get("/updater.exe")
async def download_updater(request: Request):
    """대시보드 업데이터 내려받기 (2026-08-06 사용자 요청: 본판 비번 로그인 → updater.exe,
    렌탈 비번 로그인 → rental_updater.exe).
    ★단일 바이너리★ — 내용은 동일하고 에디션은 지인 info.txt의 edition=rental이 결정한다.
    파일명만 테넌트별로 다르게 준다(매뉴얼이 rental_updater.exe로 안내하므로).
    Railway 이미지엔 server/만 있어 exe/updater.exe가 없다 → GitHub raw를 프록시 스트리밍."""
    tenant = check_session(request)
    if not tenant:
        return RedirectResponse(url="/login")
    ver = _load_version_json()
    url = (ver.get("updater") or {}).get("download_url") or \
        "https://raw.githubusercontent.com/kevincom-honjong/aion2-macro-releases/main/exe/updater.exe"
    fname = "updater.exe" if tenant == "main" else "rental_updater.exe"

    # ★렌탈은 exe + 채워진 info.txt를 ZIP으로 준다(2026-08-07 사용자 요청)★
    #   "인포 만들 때 알아서 키를 넣어놔라" — 그런데 업데이터는 main·rental 공용 단일 바이너리라
    #   자기 안에 지인 키를 가질 수 없다(지인마다 별도 빌드를 하지 않는 한). 반면 이 다운로드는
    #   ★이미 그 지인 비번으로 로그인한 상태★라 서버는 키를 안다 → 여기서 info.txt를 만들어 함께 준다.
    #   압축을 C:\auto에 풀면 키·에디션이 이미 들어가 있어 첫 실행부터 렌탈 채널로 붙는다.
    if tenant != "main":
        info = _rental_info_txt(tenant)
        try:
            import httpx as _hx2, zipfile as _zf, tempfile as _tf
            with _hx2.Client(timeout=_hx2.Timeout(10.0, read=180.0), follow_redirects=True) as _c:
                _r = _c.get(url)
                if _r.status_code != 200:
                    raise HTTPException(status_code=502, detail="업데이터 원본 조회 실패")
                exe_bytes = _r.content
            tmp = _tf.NamedTemporaryFile(delete=False, suffix=".zip")
            with _zf.ZipFile(tmp, "w", _zf.ZIP_DEFLATED) as z:
                z.writestr("rental_updater.exe", exe_bytes)
                z.writestr("info.txt", info)
            tmp.close()
            from starlette.background import BackgroundTask as _BT

            def _rm(p=tmp.name):
                try:
                    os.remove(p)
                except Exception:
                    pass
            return FileResponse(tmp.name, media_type="application/zip",
                                filename="rental_setup.zip", background=_BT(_rm))
        except HTTPException:
            raise
        except Exception as e:
            print(f"[updater.zip] 생성 실패 → exe 단독 제공: {e}")
            # 실패해도 설치는 되게 — 아래 단독 exe 스트리밍으로 폴백
    import httpx as _hx
    client = _hx.AsyncClient(timeout=_hx.Timeout(10.0, read=180.0), follow_redirects=True)
    try:
        req = client.build_request("GET", url)
        resp = await client.send(req, stream=True)
        if resp.status_code != 200:
            await resp.aclose()
            await client.aclose()
            raise HTTPException(status_code=502, detail="업데이터 원본 조회 실패")
    except HTTPException:
        raise
    except Exception:
        await client.aclose()
        raise HTTPException(status_code=502, detail="업데이터 원본 조회 실패")

    async def _stream():
        try:
            async for chunk in resp.aiter_bytes(65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    headers = {"Content-Disposition": f'attachment; filename="{fname}"'}
    if resp.headers.get("content-length"):
        headers["Content-Length"] = resp.headers["content-length"]
    return StreamingResponse(_stream(), media_type="application/octet-stream", headers=headers)


@app.get("/manual")
async def serve_manual(request: Request):
    """이용 매뉴얼 PDF (2026-07-27 사용자 요청 — 대시보드에서 바로 열기/내려받기).
    로그인한 사용자만. 파일은 server/manual.pdf로 함께 배포된다."""
    if not check_session(request):
        return RedirectResponse("/login", status_code=302)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manual.pdf")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="매뉴얼 파일 없음")
    # ★no-store 필수: 매뉴얼을 갱신해도 브라우저가 이전 PDF를 계속 보여준다(실제 발생).
    #   파일명에도 갱신시각을 붙여 캐시 키가 달라지게 한다.★
    stamp = time.strftime("%Y%m%d", time.localtime(os.path.getmtime(path)))
    return FileResponse(path, media_type="application/pdf",
                        headers={
                            "Content-Disposition": f'inline; filename="AION2_manual_{stamp}.pdf"',
                            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                            "Pragma": "no-cache",
                            "Expires": "0",
                        })


# ★억양 패턴: 한 문장을 한 톤으로 통째로 읽으면 평문 낭독처럼 들린다(사용자 지적).
#   문구를 '|'로 끊어 보내면 조각마다 아래 만큼 속도·높이를 어긋나게 줘서 억양을 만든다.
#   값은 사용자가 슬라이더로 정한 기준값에 더해지는 '상대 편차'다.★
TTS_PATTERN = [(11, 15), (-19, 5), (6, 20)]   # (rate %p, pitch Hz) — 조각 순서대로 순환


async def _synth_segments(text: str, rate: str, pitch: str) -> bytes:
    """'|'로 끊긴 조각들을 각각 다른 톤으로 합성해 이어붙인다.
    MP3는 프레임 단위라 바이트 이어붙이기만으로 브라우저가 정상 재생한다."""
    import edge_tts
    base_r = int(rate.rstrip("%"))
    base_p = int(pitch[:-2])
    parts = [p.strip() for p in text.split("|") if p.strip()] or [text]
    out = b""
    for i, part in enumerate(parts):
        dr, dp = TTS_PATTERN[i % len(TTS_PATTERN)] if len(parts) > 1 else (0, 0)
        r = max(-50, min(50, base_r + dr))
        p = max(-50, min(50, base_p + dp))
        comm = edge_tts.Communicate(part, TTS_VOICE,
                                    rate=f"{r:+d}%", pitch=f"{p:+d}Hz")
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                out += chunk["data"]
    if not out:
        raise RuntimeError("빈 오디오")
    return out


TTS_MAX_FILES = 400            # 알림 문구는 반복돼서 캐시 적중률이 높다 — 400개면 충분


def _prune_tts(limit: int = TTS_MAX_FILES):
    """TTS 캐시 상한 유지 — 오래된 것부터 버린다(2026-08-06 감사: 정리 루틴이 아예 없어
    임의 문구를 반복 요청하면 /data 볼륨이 차고, 같은 볼륨의 SQLite까지 마비됐다)."""
    try:
        files = [(os.path.getmtime(os.path.join(TTS_DIR, f)), os.path.join(TTS_DIR, f))
                 for f in os.listdir(TTS_DIR) if f.endswith(".mp3")]
        if len(files) <= limit:
            return
        for _mt, fp in sorted(files)[:len(files) - limit]:
            try:
                os.remove(fp)
            except Exception:
                pass
    except Exception:
        pass


@app.get("/tts")
async def synth_tts(request: Request, text: str = "", rate: str = "", pitch: str = ""):
    """알림 문구를 마이크로소프트 신경망 음성(SunHi)으로 합성해 MP3로 돌려준다.

    ★왜 서버에서 만드나: 브라우저 내장 speechSynthesis가 쓰는 윈도우 기본 한국어
      음성(Heami)은 대놓고 기계음이다. 신경망 음성은 브라우저에 없어서 서버가
      만들어 내려줄 수밖에 없다(2026-07-27 사용자: "너무 기계같잖아").
      합성 실패 시 대시보드가 알아서 브라우저 음성으로 폴백하므로 알림은 안 끊긴다.
    같은 (문구·톤) 조합은 디스크에 캐시 → 두 번째부터 즉시 재생."""
    if not check_session(request):
        raise HTTPException(status_code=403)
    text = (text or "").strip()[:200]
    if not text:
        raise HTTPException(status_code=400, detail="text 없음")
    # ★edge-tts에 임의 문자열이 흘러가지 않게 형식을 강제한다(값 자체가 SSML로 들어감)★
    if not re.fullmatch(r"[+-]\d{1,2}%", rate or ""):
        rate = TTS_RATE
    if not re.fullmatch(r"[+-]\d{1,2}Hz", pitch or ""):
        pitch = TTS_PITCH
    key = hashlib.sha256(f"{TTS_VOICE}|{rate}|{pitch}|{text}".encode("utf-8")).hexdigest()[:32]
    path = os.path.join(TTS_DIR, key + ".mp3")
    if not os.path.exists(path):
        try:
            audio = await _synth_segments(text, rate, pitch)
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(audio)
            os.replace(tmp, path)          # 부분 파일이 캐시로 굳는 것 방지
            _prune_tts()                   # ★무한 적재 차단(2026-08-06 감사 major)★
        except Exception as e:
            print(f"[TTS] 합성 실패: {e}")
            raise HTTPException(status_code=503, detail="tts 합성 실패")
    return FileResponse(path, media_type="audio/mpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.delete("/bugs")
async def delete_bugs_bulk(request: Request, pc_id: Optional[str] = None):
    """버그스샷 일괄 삭제. pc_id 주면 그 PC만, 없으면 테넌트 전체(2026-07-27 사용자 요청)."""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    bugs = _list_bug_files(tenant, pc_id)
    bdir = tenant_bugs_dir(tenant)
    removed = 0
    for b in bugs:
        try:
            os.remove(os.path.join(bdir, os.path.basename(b["filename"])))
            removed += 1
        except Exception:
            pass
    await push_state(tenant)
    return JSONResponse({"ok": True, "removed": removed})


@app.delete("/bugs/image/{filename:path}")
async def delete_bug_image(filename: str, request: Request):
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    filename = os.path.basename(filename)
    path = os.path.join(tenant_bugs_dir(tenant), filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    os.remove(path)
    await push_state(tenant)
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# Char Info API (macro → server, server → dashboard)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/char_info/{pc_id}")
async def receive_char_info(pc_id: str, request: Request):
    """매크로가 수집한 캐릭터 세부정보 저장"""
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    pc_id = clean_pc_id(pc_id)   # 브로드캐스트 에코도 저장 키 소독과 일관
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    nspc = ns(tenant, pc_id)
    total_kina = data.get("total_kina", 0)
    chars = data.get("characters", [])
    merge = bool(data.get("merge", False))   # 단일 캐릭 수집: slot 기준 병합(나머지 보존)
    # ★수집 시각 = '전체수집 기준'(2026-07-25, 사용자 정의): 단일수집(merge)은 구버전 매크로가
    #   새 시각을 보내와도 무시하고 기존(전체수집) 시각 유지★
    ca = None if merge else (data.get("collected_at") or None)
    merged = await upsert_char_info(nspc, total_kina, chars, merge=merge, collected_at=ca)
    # 병합 시 최종 total_kina/collected_at을 다시 읽어 브로드캐스트(기존값 유지분 반영)
    final_kina = total_kina
    final_ca = data.get("collected_at", "")
    if merge:
        info = await get_char_info(nspc)
        if info:
            final_kina = info.get("total_kina", total_kina)
            final_ca = info.get("collected_at", final_ca)
    await manager.broadcast({"type": "char_info", "pc_id": pc_id,
                              "total_kina": final_kina, "chars": merged,
                              "collected_at": final_ca}, tenant)
    return JSONResponse({"ok": True})


@app.post("/slot_filter/{pc_id}")
async def set_slot_filter(pc_id: str, request: Request):
    """대시보드 → 슬롯 활성화/비활성화 저장 + 매크로에 명령 전달"""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    body = await request.json()
    filters = body.get("filters", {})
    # int 키로 정규화
    filters = {int(k): bool(v) for k, v in filters.items()}
    nspc = ns(tenant, pc_id)
    await upsert_slot_filters(nspc, filters)
    # 매크로에 set_slot_filter 명령 전달 (※인자 순서 버그 수정: command, args, cmd_id)
    cmd_id = await insert_command(nspc, "set_slot_filter", {"filters": filters})
    await send_command_to_macro(nspc, "set_slot_filter", {"filters": filters}, cmd_id)
    await push_state(tenant)
    return {"ok": True}


@app.get("/char_info/{pc_id}")
async def query_char_info(pc_id: str, request: Request):
    """캐릭터 세부정보 조회 — 대시보드(세션) + ★매크로(API키)★.
    ★2026-07-29: 매크로의 '저레벨 캐릭 스킵'이 이 GET을 쓰는데 세션 전용이라 401 →
      항상 로컬 char_info.json 폴백으로 낡은/오염 값을 읽었다(PC-06/07/09 실사고:
      오염값 10000/10000을 저레벨로 오판해 사냥 없이 완료 도장). 읽기는 자기 테넌트
      데이터뿐이라 API키 허용이 안전하다 — 명령 주입(POST /command)과는 다르다.★"""
    tenant = check_session(request) or check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=401)
    info = await get_char_info(ns(tenant, pc_id))
    if not info:
        return JSONResponse({"pc_id": pc_id, "total_kina": 0, "chars": [], "collected_at": None})
    info = dict(info)
    info["pc_id"] = pc_id
    return JSONResponse(info)


# ─────────────────────────────────────────────────────────────────────────────
# 악몽 진행 상태
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/nightmare/progress/{pc_id}")
async def save_nightmare_progress(pc_id: str, request: Request):
    """매크로가 악몽 진행 상태 전송"""
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    pc_id = clean_pc_id(pc_id)   # 브로드캐스트 에코도 저장 키 소독과 일관
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    slot = data.get("slot", 1)
    tab = data.get("tab", "몽충I")
    bosses = data.get("bosses", {})
    await upsert_nightmare_progress(ns(tenant, pc_id), slot, tab, bosses)
    await manager.broadcast({"type": "nightmare_progress", "pc_id": pc_id,
                              "slot": slot, "tab": tab, "bosses": bosses}, tenant)
    return JSONResponse({"ok": True})


@app.get("/nightmare/progress/{pc_id}")
async def query_nightmare_progress(pc_id: str, request: Request):
    """대시보드가 악몽 진행 상태 조회"""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    progress = await get_nightmare_progress(ns(tenant, pc_id))
    return JSONResponse({"pc_id": pc_id, "slots": progress})


# ─────────────────────────────────────────────────────────────────────────────
# 스크린샷 업로드/조회 (아르카나, 장비 등)
# ─────────────────────────────────────────────────────────────────────────────
import base64 as _b64

_SS_SAFE = re.compile(r"[^A-Za-z0-9가-힣_-]")


def _screenshot_path(tenant: str, category: str, pc_id: str, slot: int) -> str:
    """테넌트별 스크린샷 경로. main = 기존 경로(호환), 그 외 = 테넌트 하위 폴더.
    ★basename은 봉쇄가 아니다(2026-08-06 감사)★ — basename("..") == ".."이라 상위 폴더로
    새어 나갔다. 화이트리스트로 걸러 경로 구분자·점 자체를 없앤다."""
    category = _SS_SAFE.sub("", category)[:32] or "misc"
    pc_id = _SS_SAFE.sub("", pc_id)[:40] or "unknown"
    slot = max(0, min(int(slot), 99))
    base = SCREENSHOTS_DIR if tenant == "main" else os.path.join(SCREENSHOTS_DIR, _SS_SAFE.sub("", tenant)[:40] or "t")
    return os.path.join(base, category, f"{pc_id}_s{slot}.png")


@app.post("/screenshot/{category}/{pc_id}/{slot}")
async def upload_screenshot(category: str, pc_id: str, slot: int, request: Request):
    """매크로가 스크린샷 업로드 (arcana, equip 등)"""
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400)
    img_b64 = data.get("image", "")
    if not img_b64:
        raise HTTPException(status_code=400, detail="No image")
    # ★크기 상한(2026-08-06 감사 major)★ — 여기만 상한이 없어 공용 볼륨(/data)을 채워
    #   같은 볼륨의 SQLite까지 마비시킬 수 있었다(버그스샷 8MB·라이브 512KB와 눈높이 맞춤).
    if len(img_b64) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="이미지가 너무 큽니다")
    try:
        img_bytes = _b64.b64decode(img_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="이미지 디코드 실패")
    if len(img_bytes) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="이미지가 너무 큽니다")
    fpath = _screenshot_path(tenant, category, pc_id, slot)
    os.makedirs(os.path.dirname(fpath), exist_ok=True)
    with open(fpath, "wb") as f:
        f.write(img_bytes)
    return JSONResponse({"ok": True, "path": f"/screenshot/{category}/{pc_id}/{slot}"})


@app.get("/screenshot/{category}/{pc_id}/{slot}")
async def get_screenshot(category: str, pc_id: str, slot: int, request: Request):
    """대시보드가 스크린샷 조회"""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    fpath = _screenshot_path(tenant, category, pc_id, slot)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(fpath, media_type="image/png")


# ─────────────────────────────────────────────────────────────────────────────
# 회랑 진행 상태 (2026-08-01) — 스프레드 '회랑' 열 + 전광판 '회랑 남음' 타일
# ─────────────────────────────────────────────────────────────────────────────
# ★메모리 + 볼륨 DB 영속(2026-08-07)★ — 원래 메모리 전용이었는데, "다음 회랑 전송 때 복원된다"는
#   전제가 실사용과 안 맞았다: 회랑은 수·토 리셋 사이에 한 번만 돌기 때문에, 그 사이 서버를
#   재배포하면 다음 리셋까지 진행표가 빈칸으로 남는다(사용자: "정보수집했는데 회랑정보가 다 날아갔네?"
#   — 실제 원인은 정보수집이 아니라 그날의 서버 재배포 5회였다).
#   → 갱신 때마다 설정 KV(볼륨 DB)에 통째로 저장하고 부팅 때 되읽는다. 스키마 변경 없음.
CORRIDOR_PROG: dict = {}    # {"tenant::PC-01": {our, slots, remaining, total, ts}}
CORRIDOR_KEY = "corridor_prog_all"


async def _corridor_persist():
    """CORRIDOR_PROG 전체를 KV에 저장(마지막 쓰기가 곧 전체 상태 — 부분 갱신 경합 없음)."""
    try:
        await set_setting(CORRIDOR_KEY, json.dumps(CORRIDOR_PROG, ensure_ascii=False)[:200000])
    except Exception as e:
        print(f"[corridor] 영속 저장 실패(무시): {e}")


async def _corridor_restore():
    try:
        raw = await get_setting(CORRIDOR_KEY)
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                CORRIDOR_PROG.update(data)
                print(f"[corridor] 진행 스냅샷 복원: {len(CORRIDOR_PROG)}대")
    except Exception as e:
        print(f"[corridor] 복원 실패(무시): {e}")


def _corridor_cutoff() -> float:
    """가장 최근 수/토 22:00 KST의 epoch. ★이 경계보다 오래된 스냅샷 = 리셋 전 옛 판★ —
    수요일 22시가 지나도 🌀 뱃지·'회랑 남음'이 옛 값(완료)으로 남아 있던 사고(2026-08-05).
    회랑 장부(corridor.py _reset_key)와 같은 경계 계산."""
    import datetime as _dt
    kst = _dt.timezone(_dt.timedelta(hours=9))
    now = _dt.datetime.now(kst)
    d = now
    for _ in range(8):
        if d.weekday() in (2, 5) and (d.date() < now.date() or now.hour >= 22):
            return d.replace(hour=22, minute=0, second=0, microsecond=0).timestamp()
        d -= _dt.timedelta(days=1)
    return 0.0


@app.post("/corridor/progress/{pc_id}")
async def save_corridor_progress(pc_id: str, request: Request):
    """매크로 → 서버. 회랑 진행 전체 스냅샷."""
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
    pc_id = clean_pc_id(pc_id)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 파싱 실패")
    data["ts"] = time.time()
    CORRIDOR_PROG[ns(tenant, pc_id)] = data
    await _corridor_persist()          # 재배포에도 살아남게(2026-08-07)
    await manager.broadcast({"type": "corridor_progress", "pc_id": pc_id,
                             "data": {"remaining": data.get("remaining"),
                                      "total": data.get("total")}}, tenant)
    return JSONResponse({"ok": True})


@app.get("/corridor/progress")
async def all_corridor_progress(request: Request):
    """대시보드 → 서버. 전광판 '회랑 남음' 집계용."""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    out = {}
    cutoff = _corridor_cutoff()
    for k, v in CORRIDOR_PROG.items():
        t, raw = split_ns(k)
        if t == tenant and (v.get("ts") or 0) >= cutoff:   # 리셋 경계 지난 스냅샷 제외
            out[raw] = {"remaining": v.get("remaining"), "total": v.get("total"),
                        "ts": v.get("ts")}
    return JSONResponse({"pcs": out})


# ─────────────────────────────────────────────────────────────────────────────
# 전체 캐릭터 테이블 API
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/characters")
async def get_all_characters(request: Request):
    """해당 테넌트 PC들의 모든 캐릭터 정보를 플랫 테이블로 반환"""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    all_info = [i for i in await get_all_char_info() if ns_of(i.get("pc_id") or "") == tenant]
    # 악몽 진행 상태도 같이 조회
    all_nm = [n for n in await get_all_nightmare_progress() if ns_of(n.get("pc_id") or "") == tenant]
    nm_map = {}  # (pc_id, slot) → nightmare summary
    for nm in all_nm:
        bosses = nm.get("bosses", {})
        cleared = sum(1 for b in bosses.values() if b.get("cleared"))
        total = len(bosses) if bosses else 7
        best_stage = max((b.get("stage", 0) for b in bosses.values()), default=0) if bosses else 0
        nm_map[(split_ns(nm["pc_id"])[1], nm["slot"])] = f"{nm.get('tab','몽충I')} {cleared}/{total}" if bosses else ""
    # 회랑 진행(2026-08-01) — 메모리 스냅샷을 (pc,slot) 문자열로 병합 (사용자: "하2/2·중1/1" 형식)
    cor_map, cor_full = {}, {}
    _cor_cut = _corridor_cutoff()
    for k, v in CORRIDOR_PROG.items():
        t, raw = split_ns(k)
        if t != tenant or (v.get("ts") or 0) < _cor_cut:   # 리셋 경계 지난 스냅샷 제외
            continue
        our = v.get("our") or {}
        ol, om = our.get("lower"), our.get("middle")
        for s_str, e in (v.get("slots") or {}).items():
            try:
                s = int(s_str)
            except Exception:
                continue
            dl, dm = e.get("lower", 0), e.get("middle", 0)
            cor_map[(raw, s)] = (f"하{dl}/{ol if ol is not None else '?'}"
                                 f"·중{dm}/{om if om is not None else '?'}")
            cor_full[(raw, s)] = (ol is not None and dl >= ol
                                  and om is not None and dm >= om)
    rows = []
    for info in all_info:
        pc_id = split_ns(info["pc_id"])[1]
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
                "corridor_progress": cor_map.get((pc_id, slot), ""),
                "corridor_full": cor_full.get((pc_id, slot), False),
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
_GH_CDN_JSDELIVR = "https://cdn.jsdelivr.net/gh/kevincom-honjong/aion2-macro-releases@main"
# ★2026-08-18: 이미지 배포를 raw 로 되돌렸다★
#   jsDelivr 가 이 레포를 통째로 못 가져온다 — 본문이 그대로
#   "Failed to fetch kevincom-honjong/aion2-macro-releases@main from GitHub." 였고
#   ★기존 파일(ps_login_title.png)까지 503/404★ 였다. 새 파일만의 캐시 문제가 아니다.
#   (레포에 75MB exe 가 여러 개라 jsDelivr 패키지 한도에 걸린 것으로 보인다.)
#   실제 피해: 새 템플릿 ps_login_logo.png 가 함대에 안 내려가 매크로가
#   '로드 스킵(파일 없음) 216/217' 로 떴고, 파섹 로그인 화면 감지가 통째로 죽었다.
#   ★raw 429 걱정★ 은 '13PC × 이미지 150개 첫 동기화' 시나리오였다. 지금은 서버가
#   ★해시가 다른 것만★ 목록에 넣으므로 평상시 0~수 개다. 대량 재동기화가 필요하면
#   그때 _GH_CDN_JSDELIVR 로 되돌리거나 내부망 시드를 쓴다.
_GH_CDN = _GH_RAW

_version_cache = {"data": {}, "ts": 0}

def _load_version_json() -> dict:
    """version.json 로드 (5분 캐시).

    ★순서를 뒤집었다: GitHub raw 우선, 로컬 파일은 폴백(2026-07-28).★
    로컬 파일은 **이미지에 구워진 사본**이라 재배포해야만 갱신된다. 그래서 예전엔
    릴리스마다 server/version.json 커밋으로 서버를 재배포시켜 최신을 반영했는데,
    그 재배포가 /data(DB·버그스샷·OCR 학습크롭)를 통째로 날리고 있었다.
    → 감시 패턴에서 version.json을 빼고(railway.toml), 최신값은 raw에서 읽는다.
      raw가 죽어도 구워진 사본으로 서빙은 계속된다(조금 낡을 뿐 함대는 안 멈춤).
    """
    import time as _time
    now = _time.time()
    if _version_cache["data"] and now - _version_cache["ts"] < 300:
        return _version_cache["data"]
    # 1차: GitHub raw (항상 최신 — 재배포 없이 반영되는 유일한 경로)
    # ★httpx를 쓴다: requests는 requirements.txt에 없다. 예전엔 로컬 파일이 1순위라
    #   이 경로가 한 번도 안 돌아서 아무도 몰랐는데, 순서를 뒤집는 순간 ImportError로
    #   영영 낡은 버전을 서빙할 뻔했다(2026-07-28).★
    try:
        import httpx as _hx
        r = _hx.get("https://raw.githubusercontent.com/kevincom-honjong/aion2-macro-releases/main/server/version.json",
                    timeout=6.0, follow_redirects=True)
        if r.status_code == 200:
            data = r.json()
            if data.get("exe", {}).get("version"):
                _version_cache["data"] = data
                _version_cache["ts"] = now
                return data
    except Exception as e:
        print(f"[version] raw 조회 실패: {e.__class__.__name__}: {e}")
    # ★1.5차: GitHub API (api.github.com) — raw 가 죽어도 여긴 산다 (2026-08-18)★
    #   raw 는 간헐적으로 통째로 안 된다(503/타임아웃). 그때 바로 '구워진 사본'으로
    #   떨어지는데 그건 ★마지막 서버 재배포 시점★ 이라 몇 버전씩 낡았다.
    #   실제 피해: 1.1.499 를 릴리스했는데 /check 가 1.1.495 를 계속 내보냈고,
    #   그대로 업데이트를 쏘면 함대가 ★다운그레이드★ 될 뻔했다. 6분을 헛기다렸다.
    #   호스트가 다르면 같이 죽을 확률이 확 떨어진다. (미인증 60회/시, 5분 캐시라 여유)
    try:
        import base64 as _b64
        import httpx as _hx2
        r = _hx2.get("https://api.github.com/repos/kevincom-honjong/"
                     "aion2-macro-releases/contents/server/version.json?ref=main",
                     headers={"Accept": "application/vnd.github+json"},
                     timeout=8.0, follow_redirects=True)
        if r.status_code == 200:
            data = json.loads(_b64.b64decode(r.json().get("content", "")).decode("utf-8"))
            if data.get("exe", {}).get("version"):
                _version_cache["data"] = data
                _version_cache["ts"] = now
                print(f"[version] GitHub API 로 조회 성공 (raw 대체) "
                      f"— exe {data['exe']['version']}")
                return data
    except Exception as e:
        print(f"[version] API 조회도 실패 → 로컬 사본: {e.__class__.__name__}: {e}")
    # 2차: 이미지에 구워진 로컬 사본 (raw 장애 시 함대가 멈추지 않게)
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
    return _version_cache.get("data", {})

_IMG_PROXY_CACHE: dict = {}          # fname -> bytes (프로세스 메모리, 최대 80장)


@app.get("/img/{fname}")
async def serve_image(fname: str):
    """이미지 중계 — ★서버가 GitHub 에서 받아 함대에 넘겨준다★ (2026-08-18 신설)

    ★왜 필요했나★ 함대가 이미지를 받을 통로가 둘 다 죽었다:
      · jsDelivr — 이 레포를 통째로 못 가져온다("Failed to fetch ... from GitHub").
        새 파일뿐 아니라 ★기존 파일까지★ 503/404 였다.
      · raw.githubusercontent.com — 서버(Railway)에서는 되는데 ★함대에서는 안 된다★.
        exe(GitHub Releases)는 멀쩡히 받으므로 인터넷 자체 문제가 아니다.
        현지 ISP 가 raw 를 막는 것으로 보인다(개발 PC 에서도 503/타임아웃).
    증상은 조용했다: 새 템플릿 ps_login_logo.png 하나가 안 내려가
    '이미지 로드 완료 (216/217)' 로 뜨고, 그걸 쓰는 파섹 로그인 감지가 통째로 죽었다.
    ★함대가 확실히 닿는 유일한 곳은 이 서버다.★ 그래서 여기서 중계한다.

    무결성은 그대로 — 업데이터가 version.json 의 sha256 을 받아 검증한다.
    """
    import re as _re2
    if not _re2.fullmatch(r"(?i)[^/\\\x00]{1,120}\.png", fname):
        raise HTTPException(status_code=404)
    data = _IMG_PROXY_CACHE.get(fname)
    if data is None:
        # ★★상류를 하나만 쓰지 않는다 (2026-08-19 사용자 지적)★★
        #   사용자: "내부망이 실패하면 외부망으로 해야지 왜 그건 안 하냐"
        #   맞는 지적이다. 여기는 상류가 raw.githubusercontent 하나뿐이라,
        #   그게 잠깐만 흔들려도 함대는 ★그 템플릿을 영영 못 받는다★
        #   (업데이터 쪽 URL 도 하나뿐이라 4회 재시도 후 포기한다).
        #   → raw → jsDelivr → GitHub API(base64) 순으로 내려간다.
        #     하나라도 200 이면 성공이고, 무결성은 업데이터가 sha256 으로 본다.
        _q = _urlparse.quote(fname)
        _repo = "kevincom-honjong/aion2-macro-releases"
        _sources = [
            ("raw", f"{_GH_RAW}/images2/{_q}"),
            ("jsdelivr", f"https://cdn.jsdelivr.net/gh/{_repo}@main/images2/{_q}"),
            ("ghapi", f"https://api.github.com/repos/{_repo}/contents/images2/{_q}?ref=main"),
        ]
        data = None
        _errs = []
        import httpx as _hx
        for _name, _url in _sources:
            try:
                r = _hx.get(_url, timeout=15.0, follow_redirects=True,
                            headers={"Accept": "application/vnd.github.raw"}
                            if _name == "ghapi" else None)
            except Exception as _e:
                _errs.append(f"{_name}:{_e.__class__.__name__}")
                continue
            if r.status_code == 200 and r.content:
                data = r.content
                if _name != "raw":
                    print(f"[img] {fname} — raw 실패 → {_name} 로 받음", flush=True)
                break
            _errs.append(f"{_name}:{r.status_code}")
        if data is None:
            print(f"[img] {fname} 전 상류 실패: {_errs}", flush=True)
            raise HTTPException(status_code=404)
        if len(_IMG_PROXY_CACHE) > 80:
            _IMG_PROXY_CACHE.clear()
        _IMG_PROXY_CACHE[fname] = data
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.post("/check")
async def updater_check(request: Request):
    """updater.exe가 호출 — exe/이미지/updater 업데이트 필요 여부 응답"""
    body = await request.json()
    client_exe_ver     = body.get("exe_version", "0.0.0")
    client_img_hashes  = body.get("image_hashes", {})
    client_updater_ver = body.get("updater_version", "0.0.0")
    client_edition     = body.get("edition", "main")   # 렌탈 채널(2026-07-26): rental이면 rental exe 배포

    # ★에디션 자기신고 봉쇄(2026-08-06 감사 critical)★ — info.txt에서 edition=rental 한 줄만 지우면
    #   킬스위치가 없는 본판 exe를 받아 영구히 통제 밖으로 나갈 수 있었다. 키를 보내오면
    #   ★키가 곧 채널★이다(렌탈 테넌트 = 무조건 렌탈 채널). 키가 없으면 구버전 업데이터이므로
    #   기존 동작 유지(하위호환) — 대신 그런 요청엔 내부망 시드를 주지 않는다.
    #   ※차단(killed/만료) 테넌트도 채널 판정에는 그대로 쓴다: 업데이트를 막는 게 목적이 아니라
    #     '렌탈 exe를 유지시켜 라이선스 검사를 계속 받게' 하는 게 목적이다.
    _supplied_key = request.headers.get("X-Api-Key", "")
    _ip = _client_ip(request)
    key_tenant = None
    if _supplied_key and not _key_probe_blocked(_ip):
        for _k, _tn in KEY_TO_TENANT.items():
            if hmac.compare_digest(_supplied_key, _k):
                key_tenant = _tn
        if not key_tenant:
            _key_probe_failed(_ip)      # 키 오라클 방지(2026-07-27 조치를 여기에도 적용)
    if key_tenant and key_tenant != "main":
        client_edition = "rental"
    elif key_tenant == "main":
        client_edition = "main"

    ver = _load_version_json()
    result: dict = {}

    # exe 업데이트 체크 (에디션별 채널)
    if client_edition == "rental":
        exe_info = ver.get("rental", {})
        asset_prefix = "rental"
    else:
        exe_info = ver.get("exe", {})
        asset_prefix = "macro"
    server_exe_ver = exe_info.get("version", "0.0.0")
    # ★키 없는 요청엔 '본판'을 주지 않는다(2026-08-06 리뷰 critical)★ — info.txt에서 edition과
    #   control_api_key 두 줄만 지우면 '자칭 main'으로 킬스위치 없는 본판을 받아갈 수 있었다.
    # ★단 자칭 rental은 준다(2026-08-07 수정)★ — 렌탈 exe는 그 자체가 라이선스 검사를 하므로
    #   키 없이 받아가도 실행이 안 된다(license_guard.boot_check가 키 없으면 시작 차단).
    #   반대로 안 주면 ★첫 설치가 조용히 실패한다★ — 사용자 실사고: "rental_updater 실행하면
    #   업데이트는 하는데(이미지 250장) 프로그램을 안 받는다". 보안은 그대로, 설치는 살린다.
    if not key_tenant and client_edition != "rental":
        exe_info = {}
        result["notice"] = "no_key_no_exe"      # 진단용(구버전 업데이터는 무시)
    if exe_info and server_exe_ver != client_exe_ver:
        result["exe_update"] = {
            "version":      server_exe_ver,
            "sha256":       exe_info.get("sha256"),
            # exe(71MB)는 GitHub Releases(CDN)에서 배포 — raw 429 우회. jsDelivr는 용량초과라 불가.
            # 규칙: 릴리스 태그 v<버전>, 에셋 이름 macro-<버전>.exe / rental-<버전>.exe
            "download_url": f"https://github.com/kevincom-honjong/aion2-macro-releases/releases/download/v{server_exe_ver}/{asset_prefix}-{server_exe_ver}.exe",
        }

    # 이미지 업데이트 체크
    # ★배포처는 이 서버 자신 (/img 중계) — jsDelivr 사망 + 함대에서 raw 차단★
    _img_base = str(request.base_url).rstrip('/')
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
                "download_url": f"{_img_base}/img/{_urlparse.quote(fname)}",
            })
    if images_to_update:
        result["images_update"] = images_to_update

    # updater 자가 업데이트 체크
    updater_info = ver.get("updater", {})
    server_updater_ver = updater_info.get("version", "0.0.0")
    # ★★업데이터는 ★올라갈 때만★ 준다 — 다운그레이드 금지 (2026-08-22 사고 146-b)★★
    #   옛 조건은 문자열 `!=` 였다. 그런데 업데이터 클라는 자가업데이트를 ★최우선★ 으로
    #   돌리고 성공하면 sys.exit() 한다(client/updater.py:858-861) — 즉 자가업데이트가
    #   걸리면 그 회차 ★매크로 exe 업데이트에 영영 도달하지 못한다.★
    #   실측 지뢰: 소스는 UPDATER_VERSION="3.1.6"(client/updater.py:34) 인데
    #   version.json 의 updater 는 3.1.5 다. 3.1.6 을 빌드해 함대에 깔면
    #     함대 3.1.6 → 서버가 "3.1.5 로 바꿔라" → 자가업데이트 → 재시작 → 다시 3.1.6…
    #   ★전 함대가 매크로 업데이트를 영원히 못 받는 무한 루프★ 가 된다.
    #   되돌리기가 정말 필요하면 version.json 의 updater 버전을 ★더 큰 수★ 로 올린다.
    def _vtup(s):
        out = []
        for p in str(s or "0").split("."):
            try:
                out.append(int(p))
            except Exception:
                out.append(-1)      # 숫자가 아니면 가장 낮게 — 미지 버전으로 되돌리지 않는다
        return tuple(out)
    if server_updater_ver != client_updater_ver and _vtup(server_updater_ver) > _vtup(client_updater_ver):
        result["updater_update"] = {
            "version":      server_updater_ver,
            "sha256":       updater_info.get("sha256"),
            "download_url": updater_info.get("download_url",
                f"{_GH_RAW}/exe/updater.exe"),
        }

    # ★내부망 시드(2026-08-06, updater 3.0.8+)★ — 설정 lan_seed(예: http://172.30.1.70:8766)가
    #   있으면 응답에 실어, 업데이터가 exe를 내부망에서 먼저 받게 한다(같은 SHA256 검증,
    #   실패 시 GitHub 폴백). ★렌탈(edition=rental)은 다른 내부망이므로 제외★.
    #   끄는 법 = 설정 값 비우기 → 다음 /check부터 전 함대 GitHub 복귀. 구버전 업데이터(≤3.0.7)는
    #   이 필드를 몰라서 무시한다(하위호환).
    #   ★키로 main임이 확인된 요청에만 준다(2026-08-06 감사)★ — 예전엔 무인증 요청이 edition=main만
    #   자칭해도 사설 IP가 나갔다. 구버전 업데이터(키 미동봉)는 시드를 못 받고 GitHub로 받는다(무해).
    if key_tenant == "main":
        try:
            _seed = await get_setting(ns("main", "lan_seed"))
            if _seed:
                result["lan_seed"] = _seed
        except Exception:
            pass

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


# ═════════════════════════════════════════════════════════════════════════════
# 계정 자동순환 (2026-08-20 사용자 지시)
# ═════════════════════════════════════════════════════════════════════════════
# 원문: "계정1이 끝났잖아. 이 끝났을때 정보수집한번때려주고 다음 계정으로 자연스럽게
#        넘어가게끔하고 … 이게 플켯다고 하는게 아니라 ★시작을 눌러줫을때만★ 그작업을
#        하면되고 만약에 ★캐릭터이름이 안적혀잇으면 들어가서 진행하지말고 멈추고
#        나한테 알람★ 주는형식으로해"
#        "계정1을 6개중 4개만 끝내고 계정2로 갔어 … 계정2 2캐릭을 다하고 정보수집을
#        다했어 그러면 끝이아니라 ★계정1로 전환해서 남은 2캐릭하고 정보수집★"
#
# ★왜 매크로가 아니라 서버가 주관하나★
#   본컴 런처 전환(switch_launcher)은 ★peer_id★ 가 있어야 하는데 매크로는 파섹 주소록을
#   조회하지 않는다(매크로↔파섹 분리 원칙). 매크로 혼자 바꾸면 원격컴 크롬만 바뀌고
#   본컴은 그대로 → 짝이 안 맞아 "퍼플온이 실행된 PC가 없습니다"(2026-08-20 PC-20·21 실측).
#
# ★★2026-08-20 적대 리뷰 3건에서 나온 결함 23개를 반영한 재작성판★★
#   초판은 요구사항을 ★정반대로 두 번★ 깼다:
#     · 어제 완주가 오늘 완주로 읽혀 ★둘째 날부터 한 번도 안 돌고★ "전 계정 완주"로 종료
#     · 부팅 감지가 새는 경로가 있어 ★사람이 껐다 켜도 계속 돌았다★
#   아래 주석의 [Sn]/[An]/[Cn] 은 그 리뷰 항목 번호다. 지우지 말 것 — 왜 이렇게
#   생겼는지가 전부 거기 있다.
ROT_KEY          = "acct_rotate"      # DB 설정 키(순환 상태 + 부팅 지문)
ROT_ALLOW_KEY    = "rot_allow"        # ★카나리아 게이트★ 허용 PC 목록(쉼표). 비면 아무도 무장 못 함
ROT_TICK         = 30.0               # 엔진 주기(초)
ROT_COLLECT_MAX  = 420.0              # 정보수집 대기 ★하한★ (실제 상한은 _rot_collect_max)
ROT_COLLECT_PER_CHAR = 150.0          # 캐릭 1명당 여유(초) — 실측 98초/캐릭 + 50% 여유
ROT_COLLECT_HARD_MAX = 1800.0         # 캐릭이 아무리 많아도 30분
ROT_SWITCH_MAX   = 1200.0             # 계정전환(본컴+원격컴+재시작) 대기 상한
ROT_START_MAX    = 420.0              # start 후 사냥 진입 대기 상한
# ★사냥 상한 < 무장 수명★ — 반대로 두면 TTL 이 먼저 걸려 사냥 상한 알람이 ★영원히 안 뜬다★
#   (재검증 지적: ROT_HUNT_MAX 20h > ROT_TTL 18h 라 도달 불가 코드였다).
ROT_HUNT_MAX     = 14 * 3600.0        # 사냥 단계 절대 상한 [S9] — 좀비 방지용
ROT_TTL          = 18 * 3600.0        # 무장 자체의 수명 [C6-②] — 넘으면 해제하고 알린다
ROT_MAX_HOPS     = 12                 # 한 번 무장에 허용하는 계정 전환 횟수(마지막 그물)
ROT_ARM_GRACE    = 90.0               # ★무장 직후 유예★ — 매크로가 그 start 를 소화할 시간
ROT_BOOT_DEBOUNCE = 60.0              # 옛 판(마커 없는) 매크로용 [BOOT] 디바운스 [S2]
# ══════════════════════════════════════════════════════════════════════════════
# ★★작업 순환 (2026-08-23 주인님 지시)★★
#   원문: "일일던전 악몽 각성 회랑 정보수집은 다 피씨의 전체계정순환으로 되야할거야"
#         "위쪽상단에 순환용이랑 선택카드만 하는거 두개로 나눠있는게 낫겟네"
#   → 상단 명령바가 두 벌이다. [🎯 선택 카드만] 은 예전 그대로 그 계정 한 번(selCmd),
#     [🔁 전 계정 순환] 은 그 PC 의 ★모든 계정을 돌며★ 같은 작업을 한다(rotCmd).
#
#   완주 순환(사냥)과 ★같은 상태기계를 쓰되 단계가 둘뿐★ 이다: tasking → switching → tasking
#     tasking   : 그 계정에서 작업이 끝나기를 기다린다
#     switching : 다음 계정으로 통짜 전환(본컴 런처 + 원격컴 크롬 + 매크로 재시작)
#
#   ★완료 판정은 '상태가 쉬는 자리로 돌아왔는가' 다★ — 네 모듈 모두 끝에서
#   report_status("idle") 을 부르고 config.running=False 로 사냥까지 내린다(실측):
#     dungeon.py:261/586 · corridor.py:1569/1623 · nightmare.py:423/1220 · awakening.py:139/492
#   그래서 idle 이 ★안정된 종착역★ 이다(작업 뒤에 사냥이 되살아나 idle 을 덮지 않는다).
#   awakening_wait / nightmare_wait 는 "오늘 더 못 한다"라서 같이 종착으로 친다.
ROT_TASKS = ("daily_dungeon", "nightmare", "awakening", "corridor", "collect_info")
ROT_TASK_LABEL = {"daily_dungeon": "일일던전", "nightmare": "악몽", "awakening": "각성",
                  "corridor": "회랑", "collect_info": "정보수집"}
# ★'쉬는 자리' 목록에 paused 를 넣지 않는다★ — 일시정지는 사람이 잠깐 세운 것이지
#   작업이 끝난 게 아니다. 넣으면 주인님이 화면 보려고 멈춘 순간 계정이 넘어간다.
ROT_IDLE_SET = ("idle", "awakening_wait", "nightmare_wait")
ROT_TASK_GRACE = 150.0                # 명령을 보내고 '바빠지기' 를 기다리는 시간
ROT_TASK_MAX   = 90 * 60.0            # 한 계정에서 한 작업의 절대 상한
_ROT: dict[str, dict] = {}            # "tenant::PC-20" → 순환 상태
_ROT_BOOT: dict[str, dict] = {}       # "tenant::PC-20" → {"id": 부팅지문, "at": epoch}
_ROT_SAVED = ""                       # 마지막으로 DB 에 쓴 직렬화본(변경 없으면 안 쓴다)
# ★★사유 없이 사라지는 무장을 잡는다 (2026-08-21 PC-17 미해결건)★★
#   실측: /command 응답은 armed=true 인데 30초 뒤 /status 의 _rot 가 None 이었다.
#   배제한 것 — 허용목록(*), 서버 재시작(uptime 유지), 워커 분리(8회 폴링 동일),
#   BOOT 늦게 도착(그 경로는 알람을 보내는데 알람 없음), stop/exit(이력 없음).
#   ★원인을 못 찾았다.★ 그래서 다음에 같은 일이 나면 ★그 순간을 잡도록★ 흔적을 남긴다.
#   정상 해제(_rot_disarm·_rot_stop·_rot_note_boot)는 사유를 남기므로 조용히 지나가고,
#   ★사유 없이 사라진 것만★ 텔레그램으로 튀어나온다.
_ROT_SEEN: set = set()                # 직전에 무장돼 있던 키들
_ROT_GONE_WHY: dict = {}              # key -> 정상 해제 사유 (한 번 쓰고 소비)


def _rot_now() -> float:
    return time.time()


# ── 게임일(하루) 판정 ────────────────────────────────────────────────────────
# ★왜 필요한가 [S1 — 치명]★
#   서버 DB 의 daily_progress 는 ★늙지 않는다.★ upsert_status 가 마지막 보고를 영구
#   보관하고 날짜로 리셋하는 코드가 없다(리셋은 매크로가 자기 로컬 파일에만 한다).
#   그래서 completed 플래그만 보면 ★어제 완주가 오늘도 완주★ 로 읽히고,
#   순환은 계정을 한 번도 안 갈고 "✅ 전 계정 오늘 완주"로 정상 종료해버린다.
#   → completed_time(로컬 KST 문자열, 실측 "2026-08-20 17:51:16")으로 오늘 것만 센다.
#   ★게임일 경계는 새벽 5시★ (slot_manager 의 새벽리셋과 같은 기준).
def _kst_today_key(ts: "datetime | None" = None) -> str:
    """게임일 키. 새벽 5시 이전은 '어제'로 친다."""
    now = ts or (datetime.now(timezone.utc) + timedelta(hours=9))
    if now.hour < 5:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def _rot_is_today(ts_str: str | None) -> bool:
    """completed_time("YYYY-MM-DD HH:MM:SS", KST)이 오늘 게임일인가. 모르면 False."""
    if not ts_str:
        return False
    try:
        dt = datetime.strptime(str(ts_str)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    return _kst_today_key(dt) == _kst_today_key()


def _rot_progress(card: dict) -> int:
    """그 계정이 ★오늘★ 끝낸 캐릭 수. 슬롯 정보가 아예 없으면 -1(모름)."""
    dp = card.get("daily_progress") or []
    if not dp:
        return -1
    return sum(1 for x in dp
               if x.get("completed") and _rot_is_today(x.get("completed_time")))


def _rot_done(card: dict) -> bool:
    """★오늘★ 이 계정 완주했나 — 슬롯이 있고 전부 완료이며 그 완료가 오늘일 때만 True.

    ★판정 불가는 '미완'으로 떨어뜨린다 [S1]★ — 모르면 가서 확인하는 쪽이 안전하다.
    (반대로 떨어뜨리면 순환이 통째로 안 돈다.)
    """
    dp = card.get("daily_progress") or []
    if not dp:
        return False
    return all(x.get("completed") and _rot_is_today(x.get("completed_time")) for x in dp)


# ── 저장/복원 ────────────────────────────────────────────────────────────────
def _rot_watch_vanish() -> None:
    """무장이 ★사유 없이★ 사라졌으면 알린다. (원인 미상 사고를 다음번에 현행범으로 잡기 위함)"""
    global _ROT_SEEN
    try:
        now_keys = set(_ROT)
        for k in (_ROT_SEEN - now_keys):
            why = _ROT_GONE_WHY.pop(k, "")
            if why:
                continue                          # 정상 해제 — 이미 알렸다
            print(f"[순환] ★★사유 없이 무장이 사라졌다★★ {k} — 원인 미상")
            try:
                _t, _b = split_ns(k)
                asyncio.create_task(_rot_say(
                    _t, _b, "⚠️ 순환 무장이 ★사유 없이★ 사라졌습니다(원인 미상). "
                            "▶시작을 다시 눌러주세요 — 이 메시지 자체가 버그 신고입니다"))
            except Exception:
                pass
        for k in (now_keys - _ROT_SEEN):
            _ROT_GONE_WHY.pop(k, None)            # 재무장 — 옛 사유는 버린다
        _ROT_SEEN = now_keys
    except Exception as e:
        print(f"[순환] 소실 감시 실패(무시): {e}")


async def _rot_save(force: bool = False) -> None:
    """★변경이 있을 때만 쓴다★ — 30초마다 볼륨 DB 를 두드리지 않는다(리뷰 잔소리)."""
    global _ROT_SAVED
    try:
        _rot_watch_vanish()
        blob = json.dumps({"rot": _ROT, "boot": _ROT_BOOT}, ensure_ascii=False, sort_keys=True)
        if not force and blob == _ROT_SAVED:
            return
        await set_setting(ROT_KEY, blob)
        _ROT_SAVED = blob
    except Exception as e:
        print(f"[순환] 저장 실패(무시): {e}")


async def _rot_load() -> None:
    """부팅 시 복원. ★복원 0건도 조용히 넘기지 않는다 [C6-①]★

    Railway 재배포/재시작은 /data 를 통째로 날릴 수 있다(C6). 그러면 무장해둔 PC 가
    ★아무 말 없이★ 순환을 잃고 완주 후 idle 로 영원히 앉아 있게 된다. 활성 카드는
    미완 슬롯이 없어 감시기에도 안 걸린다 — 완전 무음. 그래서 복원 결과를 반드시 남긴다.
    """
    global _ROT_SAVED
    try:
        raw = await get_setting(ROT_KEY)
        if not raw:
            print("[순환] 복원할 상태 없음 (첫 부팅이거나 /data 초기화 — 무장된 PC 는 "
                  "▶시작을 다시 눌러야 합니다)")
            return
        d = json.loads(raw)
        if isinstance(d, dict) and "rot" in d:
            _ROT.update(d.get("rot") or {})
            _ROT_BOOT.update(d.get("boot") or {})
        else:                                   # 옛 포맷(순환만) 호환
            _ROT.update(d or {})
        _ROT_SAVED = raw
        print(f"[순환] 복원 {len(_ROT)}대: {sorted(_ROT)} / 부팅지문 {len(_ROT_BOOT)}건")
    except Exception as e:
        print(f"[순환] 복원 실패(무시): {e}")


async def _rot_allow(tenant: str = "main") -> set:
    """★카나리아 게이트 [A7-②]★ — CLAUDE.md A7 "한 대에서 검증되기 전에 함대에 뿌리지 않는다".

    비어 있으면 ★아무도 무장하지 못한다.★ 실패해도 안전한 쪽(안 도는 쪽)으로 기운다.
    켜는 법:  POST /rotate/allow  {"pcs": "PC-10"}      ← 한 대로 시작
    넓히는 법: POST /rotate/allow {"pcs": "PC-10,PC-20"}
    전부 허용: POST /rotate/allow {"pcs": "*"}
    """
    try:
        raw = (await get_setting(ns(tenant, ROT_ALLOW_KEY))) or ""
    except Exception:
        raw = ""
    return {x.strip() for x in raw.split(",") if x.strip()}


# ── 알림 ─────────────────────────────────────────────────────────────────────
async def _rot_say(tenant: str, pc_id: str, text: str) -> None:
    """순환 알림 — 로그 + 텔레그램.

    ★⛔(정지)는 음소거를 무시한다 [A6-③]★ — 음소거는 '시끄러운 정상 알림'을 끄려는
    것이지 '기능이 죽었다'를 숨기려는 게 아니다. 음소거 중인 PC 의 순환 정지가 무음이면
    아침에 왜 안 돌았는지 아무도 모른다.
    ★로그에는 반드시 "[텔레그램] 중계 전송" 문구를 넣는다★ — ops/watch_all.py 가 그
    문자열로 알람을 줍기 때문이다(그게 없으면 감시기가 순환 사고를 영영 못 본다).
    """
    base = _base_pc(pc_id)
    hard = text.lstrip().startswith(("⛔", "🚨"))
    # ★N3: 매크로 로그 포맷을 그대로 흉내낸다★ — ops/tg_sweep.py 는 줄 맨 앞의
    #   [YYYY-MM-DD HH:MM:SS] 로 시각을 뽑고, watch_all.py 는 ★텍스트 전체★ 로 중복을
    #   거른다. 접두가 없으면 ①시각이 None 이라 '최근 N분' 에서 영구 제외되고
    #   ②같은 문구가 두 번째부터 영영 안 뜬다.
    _ts = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    # ★★로그 문구는 '실제로 보냈는가' 를 말해야 한다 (2026-08-20 최종검증 🟠5)★★
    #   초판은 음소거 확인 ★전에★ "중계 전송" 을 찍어서 ①안 보냈는데 보냈다고 적고
    #   ②로그만 보는 감시기가 음소거를 우회해 그대로 울렸다. 로그가 증거인데 거짓이면
    #   A2("증거를 붙인다")가 통째로 무너진다.
    _chat = (TENANTS.get(tenant) or {}).get("chat_id") or ""
    _will_send = bool(_chat) and tg_enabled() and (hard or not _tg_muted(base))
    _line = (f"[{_ts}] [텔레그램] "
             + ("중계 전송" if _will_send else "중계 생략(음소거/미설정)")
             + f": {base} | [순환] {text}")
    # ★N4: base 카드와 ★현역 카드★ 양쪽에 남긴다★ — 순환이 계정 b/c/d 에 있는 동안
    #   base 카드는 other_account 로 강등되고, 감시기는 그 카드를 통째로 건너뛴다.
    #   그러면 "감시기가 줍게 하려고" 넣은 이 줄이 정확히 반대로 작동한다.
    _targets = {base}
    if str(pc_id) != base:
        _targets.add(str(pc_id))
    for _t in _targets:
        try:
            await insert_log(ns(tenant, _t), "warn" if hard else "info", _line)
        except Exception:
            pass
    if not _will_send:
        print(f"[순환] {base} 텔레그램 생략(음소거/미설정): {text}")
        return
    try:
        await tg_send_text(_chat, f"🔁 {base} · {text}")
    except Exception as e:
        print(f"[순환] 텔레그램 실패(무시): {e}")


async def _rot_send(tenant: str, pc_id: str, command: str, args: dict | None = None) -> bool:
    """순환이 쓰는 명령 송신 — /command 핸들러와 ★같은 경로★(enrich + 마스킹 저장).
    ★성공/실패를 반환한다 [S13]★ — 호출부는 성공했을 때만 단계를 넘긴다."""
    args = dict(args or {})
    nspc = ns(tenant, pc_id)
    try:
        send_args = await enrich_cmd_args(tenant, pc_id, command, args)
        db_args = args
        if command in ("switch_launcher", "acct_tour", "switch_account", "find_host"):
            db_args = {**args, "peer_id": (send_args.get("peer_id") or "")[:6] + "…",
                       "parsec_pw": "***" if send_args.get("parsec_pw") else ""}
        cmd_id = await insert_command(nspc, command, db_args)
        await send_command_to_macro(nspc, command, send_args, cmd_id)
        try:
            await _push_cmd_history(tenant)
        except Exception:
            pass
        print(f"[순환] {pc_id} ▶ {command} (#{cmd_id})")
        return True
    except Exception as e:
        print(f"[순환] {pc_id} ▶ {command} ★송신 실패★: {e}")
        return False


# ── 무장 / 해제 ──────────────────────────────────────────────────────────────
async def _rot_arm(tenant: str, pc_id: str, task: str = "") -> tuple[bool, str]:
    """▶시작 버튼을 누르면 무장. ★부팅이 아니라 사람의 '시작' 이 방아쇠★ (사용자 지시).

    ★task 를 주면 '작업 순환' 이다 (2026-08-23)★ — 사냥 완주가 아니라 그 작업
    (일일던전·악몽·각성·회랑·정보수집)을 PC 의 전 계정에 한 번씩 돌린다.

    ★A7-① 방어★ 초판은 `command == "start"` 면 발신자를 안 가리고 무장했다. 그런데
    /command 로 start 를 쏘는 것은 대시보드 버튼만이 아니다 — 운영 스크립트
    (deploy_to.py · start_idle.py · up_and_start.py · switch_acct.py …)가 전부 쓴다.
    그러면 내가 알람 조치로 한 대를 재개시키는 것만으로 그 PC 에 순환까지 켜지고,
    up_and_start.py 한 번이면 ★함대 전체가 무장★ 된다 = A7("2대 이상에 상태를 바꾸는
    명령을 내 판단으로 보내지 않는다") 우회.
    → 대시보드 ▶시작만 args {"rotate": true} 를 싣고, 그게 있을 때만 무장한다.
    """
    base = _base_pc(pc_id)
    # ★★가짜 PC 는 무장하지 않는다 (2026-08-23)★★
    #   PC-TEST 는 배포 검증 스크립트가 /check 를 두드릴 때 쓰는 이름이라 카드로 남는데,
    #   한 번 무장되면 ★영원히 사냥 중★ 이라 14시간 뒤 "사냥 단계가 14시간째입니다" 라는
    #   유령 알람이 튀어나온다(2026-08-23 실제로 울렸다). 실체가 없으니 조치도 불가능하다.
    if base.upper() in ("PC-TEST", "PC-DEMO"):
        return False, f"{base} 는 검증용 가짜 PC — 순환 대상이 아님"
    allow = await _rot_allow(tenant)
    if not allow:
        return False, "허용 목록이 비어 있음 (POST /rotate/allow 로 대상 PC 지정 필요)"
    if "*" not in allow and base not in allow:
        return False, f"{base} 는 허용 목록에 없음"
    key = ns(tenant, base)
    # ★N7: 재무장이 왕복 방지 가드를 리셋하지 않게 이어받는다★
    #   전환 도중 ▶시작을 한 번 더 누르면 hops/visits 가 0 으로 돌아가 계정1↔계정2
    #   무한 왕복 방지가 통째로 풀렸다.
    _old = _ROT.get(key) or {}
    # ★작업이 바뀌면 왕복 가드도 새로 센다★ — 일일던전 순환 뒤에 회랑 순환을 걸면
    #   그건 새 일이다. 같은 작업을 다시 누른 것만 hops 를 이어받아 폭주를 막는다.
    _same = str(_old.get("task") or "") == str(task or "")
    _ROT[key] = {"stage": ("tasking" if task else "hunting"), "since": _rot_now(),
                 "armed_at": _rot_now(), "expect_restart": False, "target": "",
                 "day": _kst_today_key(),   # ★게임일 [C3]★
                 "task": str(task or ""),
                 # 작업 순환 전용 — sent_at: 명령을 보낸 시각 / busy: 실제로 시작된 증거 /
                 #                  tvisit: 이번 무장에서 작업을 보낸 계정 번호들
                 "sent_at": _rot_now(), "busy": False,
                 "tvisit": ([str(_rot_acct_no(pc_id))] if task else []),
                 "hops": int(_old.get("hops") or 0) if _same else 0,
                 "visits": dict(_old.get("visits") or {}) if _same else {}}
    # ★★②-a: 부팅지문 자리를 미리 깔아둔다★★
    #   _rot_note_boot 은 그 PC 지문을 ★처음 볼 때★ 판정을 보류한다(엉뚱한 해제 방지).
    #   그런데 무장 시점에 자리가 비어 있으면, 그 뒤 ★사람이 껐다 켠 첫 부팅★ 이
    #   통째로 '최초 등록'으로 흡수돼 순환이 살아남는다 = 이 기능이 막으려던 사고 그 자체.
    #   무장된 PC 는 '보류'를 쓸 이유가 없으므로 빈 지문을 심어 prev 가 절대 비지 않게 한다.
    _ROT_BOOT.setdefault(key, {"id": "", "at": _rot_now()})
    return True, ""


def _rot_disarm(tenant: str, pc_id: str, why: str) -> bool:
    key = ns(tenant, _base_pc(pc_id))
    if key in _ROT:
        _ROT_GONE_WHY[key] = why or "disarm"      # ★사유를 남긴다★ (소실 감시가 조용히 넘기게)
        _ROT.pop(key, None)
        print(f"[순환] {key} 해제 ({why})")
        return True
    return False


_BOOT_RE = re.compile(r"\[BOOT#([0-9a-fA-F]{6,})\]")


def _rot_note_boot(nspc: str, message: str) -> None:
    """매크로 부팅 감지 — 순환이 일으킨 재시작이 아니면 ★끈다.★

    ★왜 이렇게까지 하나 [S2 — 치명]★
      초판은 로그에 "[BOOT]" 가 있으면 부팅으로 봤다. 독립 검증에서 실측으로 깨졌다:
        · 서버에 실제로 닿는 [BOOT] 줄은 ★"[BOOT] ✅ Gemini 키 정상" 하나뿐★ 이었다
          (부팅 초반 줄들은 report_module.start() 전이라 send_log 가 버린다).
          즉 순환 해제 전체가 ★OCR 키 검증 로그 한 줄★ 에 얹혀 있었다.
        · 로그 경로가 둘인데(WS / HTTP 폴백) HTTP 쪽엔 이 검사가 없었다.
          실측 83회 부팅 중 ★4회는 서버에 [BOOT] 가 아예 안 남았다.★ 그러면
          expect_restart 가 True 로 남아 ★다음번 사람의 재시작을 삼킨다.★
        · 한 배치에 [BOOT] 가 2줄 오면 첫 줄이 expect_restart 를 먹고 둘째 줄이
          순환을 죽인다(이중발화).
      → ①매크로가 부팅마다 ★고유 지문★ `[BOOT#<uuid>]` 을 한 줄 남기고
        ②서버는 ★지문이 바뀌었을 때만★ 부팅으로 판정한다(재전송·이중발화 면역)
        ③WS·HTTP ★두 경로 모두★ 여기를 거친다
        ④지문 없는 옛 판 매크로는 [BOOT] + 60초 디바운스로 폴백한다
    """
    tenant, raw = split_ns(nspc)
    base = _base_pc(raw)
    key = ns(tenant, base)
    m = _BOOT_RE.search(str(message or ""))
    now = _rot_now()
    prev = _ROT_BOOT.get(key) or {}
    if m:
        fp = m.group(1)
        if prev.get("id") == fp:
            return                                   # 같은 부팅 — 재전송/중복
        _ROT_BOOT[key] = {"id": fp, "at": now}
        if not prev:
            # 이 PC 의 부팅 지문을 처음 본다 = 새 부팅인지 서버가 처음 보는 건지 모른다.
            # ★모를 때는 순환을 건드리지 않는다★ (엉뚱한 해제가 더 나쁘다).
            print(f"[순환] {key} 부팅지문 최초 등록({fp[:8]}) — 판정 보류")
            return
    else:
        if "[BOOT]" not in str(message or ""):
            return
        # ★★②-b: 지문을 한 번이라도 본 PC 는 옛 마커를 아예 무시한다★★
        #   새 매크로는 두 줄을 다 낸다: [BOOT#uuid] 와 "[BOOT] ✅ Gemini 키 정상".
        #   앞줄이 expect_restart 를 먹고 뒷줄이 디바운스를 못 넘기면 ★순환이 자기
        #   전환 직후 스스로 죽는다.★ 그리고 60초 디바운스는 부족하다 — 로그는 30초
        #   배치로 올라오고 전송 지연 실측이 중앙값 55초·최대 278초다(81건 중 37건이 60초 이상).
        if prev.get("id"):
            return
        if now - float(prev.get("at") or 0) < ROT_BOOT_DEBOUNCE:
            return                                   # 같은 부팅의 다른 줄
        _ROT_BOOT[key] = {"id": "", "at": now}

    st = _ROT.get(key)
    if not st:
        return
    if st.get("expect_restart") and _rot_now() <= float(st.get("expect_until") or 0):
        # ★만료 검사 [C2]★ — 기한이 지난 expect_restart 는 소비하지 않는다.
        #   기한 없이 삼키면 며칠 뒤 주인님이 껐다 켠 것까지 "전환 재시작" 으로 읽는다.
        st["expect_restart"] = False
        st["since"] = now
        print(f"[순환] {key} 재시작 확인 — 순환 유지")
        return
    _ROT_GONE_WHY[key] = "사람이 껐다 켬(BOOT)"
    _ROT.pop(key, None)
    print(f"[순환] {key} ★사람이 껐다 켬★ → 순환 해제")
    # ★★N2: 여기서 저장하지 않으면 해제가 DB 에 안 남는다★★
    #   _rot_save 는 엔진 루프의 `if _ROT:` 안에서만 돈다. 마지막 한 대가 해제되면
    #   _ROT 가 비어 그 블록을 영영 안 타고, Railway 재배포 뒤 _rot_load 가
    #   ★주인님이 손으로 껐던 무장을 부활★ 시킨다. 부팅지문도 같은 이유로 안 남는다.
    try:
        asyncio.create_task(_rot_save(force=True))
    except Exception:
        pass
    # ★해제도 반드시 알린다 [S4]★ — 조용히 죽으면 아침에 왜 안 돌았는지 알 수 없다.
    try:
        asyncio.create_task(_rot_say(
            tenant, base, "매크로가 재시작돼 순환을 해제했습니다 — 이어가려면 ▶시작을 눌러주세요"))
    except Exception:
        pass


# ── 카드 선택 ────────────────────────────────────────────────────────────────
def _rot_cards(pcs: list, base: str) -> list:
    return [p for p in pcs if _base_pc(str(p.get("pc_id") or "")) == base]


def _rot_acct_no(pc_id) -> int:
    """카드 id → 계정 번호. PC-20=1, PC-20b=2, PC-20c=3, PC-20d=4."""
    s = str(pc_id or "").strip()
    if len(s) > 1 and s[-1] in "bcd" and s[-2].isdigit():
        return "abcd".index(s[-1]) + 1
    return 1


def _rot_active(cards: list) -> dict | None:
    """지금 살아있는 계정 카드. ★없으면 None★ — cs[0] 폴백을 쓰지 않는다.

    ★두 가지를 같이 본다 [S3 / 리뷰 ②]★
      ① status 가 other_account/offline 이 아닐 것
      ② ★보고가 신선할 것★ — base 카드(PC-20)의 offline 판정은 매크로가 아니라
         ★업데이터 신선도★ 로 내려간다. 그래서 매크로가 마지막 offline 보고를 못 하고
         죽으면(taskkill·크래시) 카드가 'hunting' 또는 'idle+완주' 로 ★얼어붙는다.★
         그 박제를 현역으로 읽으면 순환이 죽은 매크로에 명령을 쏘고 20분을 버린다.
      동률이면 last_active 가 가장 최신인 카드를 고른다(대시보드 JS 와 같은 규칙).
    """
    live = []
    for c in cards:
        st = str(c.get("status"))
        if st in ("other_account", "offline"):
            continue
        # ★★신선도는 '보고해야 마땅한 상태'에만 요구한다 (2026-08-20 실측으로 정정)★★
        #   처음엔 전부 _fresh(300) 을 걸었는데, 그게 ★기능을 통째로 막았다.★
        #   매크로는 idle 이 되면 변경-해시 게이트 때문에 ★수천 초 무보고★ 다
        #   (실측 PC-10 8014초, PC-14 5488초, PC-08 3926초 — 전부 idle).
        #   그런데 순환이 완주를 감지해야 하는 카드가 바로 그 idle 카드다.
        #   → 사냥/이동/수집/전환처럼 ★계속 움직이는 상태★ 는 무보고면 박제로 보고 버리고,
        #     idle 같은 ★가만히 있는 게 정상인 상태★ 는 무보고를 이유로 버리지 않는다.
        #   (박제된 idle 카드에 명령이 나가는 경우는 switching 20분 상한이 ⛔ 로 잡는다 —
        #    조용히 죽는 게 아니라 시끄럽게 실패한다.)
        if st in ("hunting", "moving", "collecting", "switching",
                  "selling", "settling") and not _fresh(c.get("last_active"), 300):
            continue
        live.append(c)
    if not live:
        return None
    return max(live, key=lambda c: str(c.get("last_active") or ""))


def _rot_collect_max(cards) -> float:
    """★정보수집 상한은 ★캐릭 수★ 에 따라 다르다 (2026-08-20 PC-09 실측)★

    ★무엇이 문제였나★ 고정 7분이었다. 그런데 실측:
      PC-09(6캐릭) 22:44:35 → 22:54:26 = ★9분 51초★  (98초/캐릭)
      PC-10(2캐릭) 23:01:12 → 23:05:06 = 3분 54초
    6캐릭 계정은 ★애초에 7분 안에 끝날 수가 없다.★ 그래서 PC-09 는 수집을
    정상적으로 마쳤는데도 순환이 두 번 다 죽었다(22:55 / 23:11).
    PC-10 이 통과한 건 캐릭이 2명이라서였지 코드가 옳아서가 아니다.
    ★한 대에서 됐다고 다 되는 게 아니다 — 규모가 다른 케이스로 재봐야 한다.★

    캐릭 수는 카드의 daily_progress 길이로 센다(그 계정이 오늘 돌 슬롯 수 = 수집 대상).
    """
    try:
        n = max((len(c.get("daily_progress") or []) for c in (cards or [])), default=0)
    except Exception:
        n = 0
    return min(ROT_COLLECT_HARD_MAX,
               ROT_COLLECT_MAX + ROT_COLLECT_PER_CHAR * max(0, n))


def _rot_next_acct(cards: list, active: dict) -> tuple[int, str]:
    """다음으로 갈 계정 번호. 반환 (번호, 사유).
       번호 > 0 : 그 계정으로 간다
       번호 < 0 : |번호| 계정의 캐릭 이름이 없다 → ★정지 + 알람★
       번호 == 0: 갈 곳 없음(전 계정 오늘 완주)

    ★한 바퀴가 아니라 '미완이 없어질 때까지' 다 (2026-08-20 사용자 지시)★
      원문: "계정1을 6개중 4개만 끝내고 계정2로 갔어 … 계정2 2캐릭을 다하고 정보수집을
             다했어 그러면 끝이아니라 ★계정1로 전환해서 남은 2캐릭하고 정보수집★"
      → 지나간 계정이라도 미완이면 다시 간다. 번호가 낮은 미완 계정부터 고른다.
      ★무한 왕복은 호출부가 막는다★ — 갔다 왔는데 진행이 안 늘었으면 세운다.

    ★판정 근거는 매크로가 보고한 acct_ids(자격증명)와 acct_names(캐릭 이름)★
      info.txt 는 그 PC 만 안다(C7). 서버는 파일을 못 읽으므로 보고값을 쓴다.
      ★보고가 아예 없으면(옛 버전 매크로) 판정하지 않고 0 을 돌려 조용히 끝낸다★ —
      "이름이 없다"고 단정해 헛알람을 내는 것보다 낫다.
    """
    ids, names = {}, {}
    saw_names = False
    # ★오래된 것부터 병합 = 최신이 이긴다 [M5]★ — 예전엔 카드 순서에 그냥 의존했다.
    #   매크로는 빈 계정의 키를 아예 안 넣으므로 '빈 값이 최신을 덮는' 방향은 막혀 있지만,
    #   ★며칠 전 카드의 살아있는 이름이 최신의 '삭제됨' 을 덮는★ 방향은 열려 있었다.
    #   주인님이 info.txt 에서 캐릭 이름을 지운 계정으로 순환이 들어간다 = 요구 4 구멍.
    for c in sorted(cards, key=lambda c: str(c.get("last_active") or "")):
        ids.update(c.get("acct_ids") or {})
        if c.get("acct_names") is not None:
            saw_names = True
            names.update(c.get("acct_names") or {})
    cur = _rot_acct_no(active.get("pc_id"))
    done_no = {_rot_acct_no(c.get("pc_id")) for c in cards if _rot_done(c)}
    done_no.add(cur)                    # 방금 끝낸 계정은 다시 고르지 않는다
    for n in range(1, 5):
        if n in done_no:
            continue
        if not str(ids.get(str(n)) or "").strip():
            continue                    # 자격증명 없는 계정 = 없는 계정
        if not saw_names:
            return 0, "매크로가 계정별 캐릭 이름을 아직 보고하지 않음(구버전) — 순환 보류"
        if not (names.get(str(n)) or []):
            return -n, f"계정{n} 캐릭 이름이 info.txt 에 없다"
        return n, ""
    return 0, "남은 계정 없음"


def _rot_next_acct_task(cards: list, active: dict, st: dict) -> tuple[int, str]:
    """작업 순환에서 다음 계정. 반환 (번호, 사유). 0 = 갈 곳 없음(끝).

    ★완주 순환과 규칙이 다르다 — 여기는 '한 계정 한 번' 이다★
      완주 순환은 daily_progress(슬롯별 완료 + 완료시각)라는 ★서버가 볼 수 있는 근거★ 가
      있어서 "미완이면 다시 간다" 가 가능했다. 그런데 일일던전·악몽·각성·회랑은 서버에
      그런 계정별 완료 기록이 ★없다★ (일일던전만 dungeon_done_at 이 있고 나머지 셋은
      아무것도 없다). 근거 없이 '미완' 을 추측하면 같은 계정을 밤새 왕복한다.
      → 이 모드는 ★이번 무장에서 이미 작업을 보낸 계정(tvisit)을 다시 고르지 않는다.★

    ★캐릭 이름(acct_names)은 요구하지 않는다★ — 완주 순환의 start 는 이름 목록이 있어야
      슬롯을 돌지만, 이 작업들은 그렇지 않다. 특히 collect_info 는 ★그 이름을 만들러
      가는★ 명령이라 이름을 요구하면 이름 없는 계정에 영영 못 간다.
    """
    ids = {}
    for c in sorted(cards, key=lambda c: str(c.get("last_active") or "")):
        ids.update(c.get("acct_ids") or {})
    seen = {str(x) for x in (st.get("tvisit") or [])}
    seen.add(str(_rot_acct_no(active.get("pc_id"))))
    for n in range(1, 5):
        if str(n) in seen:
            continue
        if not str(ids.get(str(n)) or "").strip():
            continue                    # 자격증명 없는 계정 = 없는 계정
        return n, ""
    return 0, "남은 계정 없음"


# ── 상태 기계 ────────────────────────────────────────────────────────────────
async def _rot_stop(tenant: str, base: str, msg: str, st: dict | None = None) -> None:
    """순환 정지 + 알림. ★st 를 주면 '내가 아직 그 무장인가' 를 확인하고 지운다★
    (지금은 호출 직전에 await 가 없어 사고가 안 나지만, 하나만 생겨도 주인님이 방금
     다시 누른 무장을 지우게 된다)."""
    key = ns(tenant, base)
    if st is not None and _ROT.get(key) is not st:
        return
    _ROT_GONE_WHY[key] = "rot_stop: " + str(msg or "")[:40]   # ★소실 감시가 오탐하지 않게★
    _ROT.pop(key, None)
    try:
        await _rot_save(force=True)
    except Exception:
        pass
    await _rot_say(tenant, base, msg)


async def _rot_step_pc(tenant: str, base: str, st: dict, pcs: list) -> None:
    """한 물리 PC 의 순환 한 걸음.

    ★st 는 await 마다 바뀔 수 있다 [S6]★ — 사람이 도중에 정지/시작을 누르면
    _ROT[key] 가 사라지거나 ★새 dict 로 교체★ 된다. 그런데 이 함수는 처음 받은 dict
    ★참조★ 를 계속 들고 있으므로, 확인 없이 명령을 쏘면
      · 정지를 눌렀는데 본컴+원격컴 전환이 나가거나
      · 새로 무장했는데 옛 dict 에 stage 를 써서 추적이 끊긴다.
    → 명령을 쏘기 직전마다 _alive() 로 '내가 아직 그 상태 객체인가' 를 확인한다.
    """
    key = ns(tenant, base)

    def _alive() -> bool:
        return _ROT.get(key) is st

    # ★TTL 을 카드 유무보다 먼저 본다 [M1]★ — 예전엔 카드가 사라지면 여기서 바로
    #   return 이라 ROT_TTL 검사에 영영 도달하지 못했다. 대시보드에서 PC 를 지우면
    #   그 엔트리가 DB 에 영구 저장돼 재부팅마다 되살아나고, 엔진이 30초마다
    #   그 PC 를 위해 전체 상태를 조립했다 = 불사신 좀비.
    if _rot_now() - float(st.get("armed_at") or 0) > ROT_TTL:
        await _rot_stop(tenant, base,
                        f"⛔ 순환 무장이 {int(ROT_TTL/3600)}시간을 넘겨 자동 해제했습니다 — "
                        f"계속하려면 ▶시작을 다시 눌러주세요")
        return
    cards = _rot_cards(pcs, base)
    if not cards:
        return                                       # 카드가 사라진 PC — 아무것도 안 한다
    active = _rot_active(cards)
    stage = str(st.get("stage") or "")
    _task = str(st.get("task") or "")                 # "" 면 완주(사냥) 순환
    _tlabel = ROT_TASK_LABEL.get(_task, _task or "작업")
    # ★작업 순환은 완주 순환의 단계를 쓰지 않는다★ — 옛 판이 디스크에 남긴 상태나
    #   손상값이 섞여 들어와도 작업 모드의 단계는 tasking / switching 둘뿐이다.
    if _task and stage in ("hunting", "collecting", "starting"):
        print(f"[순환] {key} 작업모드({_task})인데 stage='{stage}' → tasking 으로 복구")
        st.update({"stage": "tasking", "since": _rot_now()})
        stage = "tasking"
    if stage not in ("hunting", "collecting", "switching", "starting", "tasking"):
        # ★미지 stage 는 조용히 굳지 않는다 [S9]★ — 옛 포맷/손상값이 들어오면 영구 정지였다.
        print(f"[순환] {key} 알 수 없는 stage='{stage}' → 처음 단계로 복구")
        st.update({"stage": ("tasking" if _task else "hunting"), "since": _rot_now()})
        stage = "tasking" if _task else "hunting"
    age = _rot_now() - float(st.get("since") or 0)

    # ── ★게임일이 바뀌면 왕복 가드를 새로 센다 [C3]★ ───────────────────────
    #   visits 는 '그 계정에 갔을 때 오늘 몇 칸 끝나 있었나' 를 적어두고, 돌아왔을 때
    #   그대로면 "진행 없음" 으로 순환을 세운다. 그런데 ★새벽 5시 게임일 경계★ 를 넘기면
    #   오늘 완료수가 전부 0 으로 떨어지는데 visits 에는 경계 이전의 양수가 남아 있다.
    #   → 5칸이 통째로 남은 계정을 "3개 그대로입니다" 라는 ★사실과 정반대 문구★ 로 죽였다.
    #   hops(12회 상한)도 같은 이유로 하루를 넘기면 부당하게 소모된다.
    _day = _kst_today_key()
    if str(st.get("day") or "") != _day:
        if st.get("day"):
            print(f"[순환] {key} 게임일 바뀜 {st.get('day')} -> {_day} — 왕복 가드 리셋")
        st.update({"day": _day, "visits": {}, "hops": 0})

    # ── 무장 수명 [C6-②] ──────────────────────────────────────────────────
    #   DB 가 살아남으면 _ROT 는 만료 없이 복원된다. 그 사이 주인님이 ★매크로 핫키★로
    #   멈춰두고 작업 중일 수 있는데(그 경로는 /command 를 안 거쳐 해제가 안 된다),
    #   그때 서버가 계정을 갈아끼우면 사고다.
    if _rot_now() - float(st.get("armed_at") or 0) > ROT_TTL:
        await _rot_stop(tenant, base,
                        f"⛔ 순환 무장이 {int(ROT_TTL/3600)}시간을 넘겨 자동 해제했습니다 — "
                        f"계속하려면 ▶시작을 다시 눌러주세요")
        return

    # ── ⑤ 작업 순환: 이 계정에서 작업이 끝나기를 기다린다 (2026-08-23) ──────
    #   ★'보냈다' 와 '했다' 는 다르다 (§A2)★ — 명령을 큐에 넣은 것만으로 넘어가면
    #   매크로가 그 명령을 거부했을 때(다른 세션 진행 중 등) 아무 일도 안 하고
    #   순환만 다음 계정으로 굴러간다. 그래서 ★한 번 바빠진 것을 본 뒤에★ 쉬는 자리로
    #   돌아온 것만 완료로 친다(busy 플래그). 끝내 안 바빠지면 유예 뒤 사유를 남기고 넘어간다.
    if stage == "tasking":
        if age > ROT_TASK_MAX:
            await _rot_stop(tenant, base,
                            f"⛔ 순환 정지 — {_tlabel} 이 {int(age/60)}분째 안 끝났습니다. "
                            f"화면 확인 필요")
            return
        if not active:
            return                                   # 매크로가 죽음 = 순환이 다룰 일이 아니다
        _s = str(active.get("status") or "")
        if _s not in ROT_IDLE_SET:
            if not st.get("busy"):
                st["busy"] = True                    # ★작업이 실제로 시작된 증거★
                print(f"[순환] {key} {_tlabel} 진행 확인(status={_s})")
            return
        # ── 여기부터 '쉬는 자리' — 끝났거나, 아직 시작을 안 했거나 둘 중 하나다
        if not st.get("busy"):
            if _rot_now() - float(st.get("sent_at") or st.get("since") or 0) < ROT_TASK_GRACE:
                return                               # 아직 시작 전일 수 있다 — 기다린다
            await _rot_say(tenant, base,
                           f"계정{_rot_acct_no(active.get('pc_id'))} 는 {_tlabel} 할 게 "
                           f"없었습니다(status={_s}) — 다음 계정으로")
        nxt, why = _rot_next_acct_task(cards, active, st)
        if nxt == 0:
            await _rot_stop(tenant, base,
                            f"✅ 순환 종료 — {_tlabel} 전 계정 완료 "
                            f"({len(st.get('tvisit') or [])}개 계정)")
            return
        if int(st.get("hops") or 0) + 1 > ROT_MAX_HOPS:
            await _rot_stop(tenant, base,
                            f"⛔ 순환 정지 — 계정 전환을 {ROT_MAX_HOPS}회 했습니다(상한). 확인 필요")
            return
        if not _alive():
            return
        # ★peer_id 없으면 반쪽 전환이 된다 — 완주 순환과 같은 방어 (2026-08-20 PC-22 실사고)★
        try:
            _pm = await _get_parsec_map(tenant)
            _num = "".join(ch for ch in base if ch.isdigit()).lstrip("0") or base
            if not (_pm.get(_num) or _pm.get(base)):
                await _rot_stop(tenant, base,
                                f"⛔ 순환 정지 — 파섹 주소록에 {base} 의 peer_id 가 없습니다. "
                                f"본컴 런처를 못 바꿔 반쪽 전환이 됩니다. "
                                f"관제컴에서 parsec_multi.py push 후 다시 눌러주세요")
                return
        except Exception as _pe:
            print(f"[순환] {base} 주소록 확인 실패(계속 진행): {_pe}")
        # acct_index 는 1 고정 [S11] — 완주 순환과 같은 이유(런처 드롭다운의 '다른 계정' 줄 번호)
        ok = await _rot_send(tenant, str(active.get("pc_id")), "switch_launcher", {
            "acct_index": 1, "acct_no": nxt,
            "acct_label": f"계정{nxt}", "chrome_label": "abcd"[nxt - 1], "launch": True,
        })
        if not ok or not _alive():
            return
        st.setdefault("tvisit", []).append(str(nxt))
        st.update({"stage": "switching", "since": _rot_now(),
                   "expect_restart": True,
                   "expect_until": _rot_now() + ROT_SWITCH_MAX,
                   "target": "abcd"[nxt - 1],
                   "hops": int(st.get("hops") or 0) + 1})
        await _rot_say(tenant, base, f"{_tlabel} 끝 → 계정{nxt} 로 전환")
        return

    # ── ① 사냥 중 — 완주를 기다린다 ────────────────────────────────────────
    if stage == "hunting":
        if age > ROT_HUNT_MAX:
            await _rot_stop(tenant, base,
                            f"⛔ 순환 정지 — 사냥 단계가 {int(age/3600)}시간째입니다. 화면 확인 필요")
            return
        if not active:
            return                                   # 매크로가 죽음 = 순환이 다룰 일이 아니다
        if str(active.get("status")) != "idle" or not _rot_done(active):
            return
        # ★★무장 직후 유예 [2026-08-21 PC-09 실사고]★★
        #   ★무엇이 있었나★ 주인님이 ▶시작을 누르면 매크로는 그 start 를 소화하느라
        #   30초 넘게 바쁘다(캐릭선택창 이동 → 슬롯 진입 → 완주 판정 = 실측 33초).
        #   그런데 서버는 무장 ★19초 뒤★ 다음 tick 에서 'idle + 완주' 를 보고 곧바로
        #   collect_info 를 쐈고, 매크로는 "이전 명령 처리 중 → 거부" 하며 ★조용히 버렸다.★
        #   loot.py 의 그 경로는 ack 도 재큐도 없다. 그런데 _rot_send 는 '큐에 넣기' 에
        #   성공했으므로 True 를 돌려주고, 순환은 collecting 으로 넘어가
        #   ★아무도 수집을 안 하는 채로★ 상한(최대 30분)을 태웠다.
        #   = A2 그 자리(배달됨 ≠ 실행됨). 실측 PC-09 16:40:50 start / 16:41:09 거부.
        #   → 무장 직후에는 그 start 가 끝날 시간을 준다. 이미 사냥 중이던 PC 는
        #     유예가 그냥 흘러가므로 무해하다.
        if _rot_now() - float(st.get("armed_at") or 0) < ROT_ARM_GRACE:
            return
        acct = _rot_acct_no(active.get("pc_id"))
        # ★수집 전후를 비교할 기준점 [S10 / A2-②]★ — 초판은 존재하지도 않는 필드명
        #   (char_updated_at)을 저장하고 읽지도 않았다. 실제 필드는 _char_collected_at.
        st["char_before"] = str(active.get("_char_collected_at") or "")
        if not _alive():
            return
        if not await _rot_send(tenant, str(active.get("pc_id")), "collect_info", {}):
            return                                   # ★송신 실패면 단계를 넘기지 않는다 [S13]★
        if not _alive():
            return
        st.update({"stage": "collecting", "since": _rot_now(), "recollect": False})
        await _rot_say(tenant, base, f"계정{acct} 완주 → 정보수집 시작")
        return

    # ── ② 정보수집 대기 — ★끝난 증거를 보고 넘어간다★ ─────────────────────
    if stage == "collecting":
        # ★타임아웃을 'active 없음' 보다 ★위에★ 둔다 (재검증 지적)★
        #   초판은 `if not active: return` 이 먼저라, 수집 중 매크로가 죽으면 7분 알람이
        #   아니라 ★18시간 TTL 까지 무음★ 이었다. 다른 단계는 타임아웃이 위에 있었다.
        # ★★수집 증거를 ★신선도와 무관하게★ 먼저 본다 (2026-08-20 PC-09 실측)★★
        #   ★무엇이 있었나★ PC-09 는 22:54:26 에 "정보수집 완료 (6/6캐릭)" 를 찍었는데
        #   순환은 22:55:42 에 "매크로가 7분째 응답이 없습니다" 로 죽었다. ★76초 차이.★
        #   원인: _rot_active() 가 collecting 카드에 _fresh(last_active,300) 을 요구하는데,
        #   ★정보수집 중에는 status 가 'collecting' 으로 고정이라 _hash_status() 가 안 바뀐다★
        #   → push 가 안 나가고 → last_active 가 5분 넘게 낡는다 → active=None.
        #   사고 99(cdp 를 해시에 안 넣어 idle PC 가 영구 동결)와 ★완전히 같은 기계★ 다.
        #   → 수집 증거(_char_collected_at)는 ★어느 카드에든★ 남는다. 신선도와 상관없이
        #     그것부터 본다. '살아있냐' 보다 '끝냈냐' 가 먼저다.
        _ev = ""
        for _c in cards:
            _g = str(_c.get("_char_collected_at") or "")
            if _g and _g != str(st.get("char_before") or ""):
                _ev = _g
                break
        _cmax = _rot_collect_max(cards)
        if age > _cmax and not active and not _ev:
            await _rot_stop(tenant, base,
                            f"⛔ 순환 정지 — 정보수집 중 매크로가 {int(age/60)}분째 "
                            f"응답이 없습니다. 화면 확인 필요")
            return
        # ★A2-① 방어★ 초판은 status 만 보고 60초 뒤 넘어갔다. 그런데 정보수집은
        #   report_status("collecting") ★전에★ 반환하는 중단 경로가 있다
        #   (계정 혼입 의심 → 수집 중단). 하필 가장 갈아서는 안 되는 상황이다.
        #   → 서버가 실제로 char_info 를 ★새로 받았는지★ 로 판정한다.
        #     그 값은 전송에 성공한 것만 갱신되므로 '전달됨 ≠ 적용됨' 함정을 넘는다.
        if not active and not _ev:
            return
        got = str(active.get("_char_collected_at") or "") if active else _ev
        if st.pop("skip_collect", False):
            pass                                     # 수집을 보낸 적이 없다(위 S-F 경로)
        elif (got and got != str(st.get("char_before") or "")
              and (not active or str(active.get("status")) == "idle")):
            # ★★status=="idle" 을 같이 요구한다 (2026-08-20 최종검증 🔴2)★★
            #   매크로는 char_info 를 ★보낸 뒤에도★ 뱅크 자가점검 +
            #   _ensure_char_select_screen(재연결 사다리·비번·출석부 내장) 을 더 돌고,
            #   그 finally 에서야 release_pause("collecting") 과 report_status("idle") 을
            #   ★함께★ 놓는다(info_collector.py 2300-2301). 그 창은 분 단위일 수 있다.
            #   그 사이에 전환을 쏘면 loot.py 의 '수집 중 거부' 가드가 ★조용히 버린다★
            #   (ack 없음·재큐 없음). 서버는 배달 성공만 보고 넘어가 20분 뒤
            #   "전환이 20분째 안 끝났다" 라는 ★진짜 원인과 무관한 사유★ 로 죽었다.
            #   → 잠금이 실제로 풀린 신호(idle)를 같이 본다. 두 값이 같은 finally 라 정확히 일치.
            pass                                     # 수집 확인 — 아래로 진행
        elif age <= _cmax:
            # ★★본컴이 스트리밍 대기가 아니면 7분을 버리지 않는다 (2026-08-20 PC-12 실측)★★
            #   ★무엇이 문제였나★ 정보수집은 ★게임 화면★ 을 요구한다. 그런데 본컴이
            #   퍼플 런처에서 '재시작'(스트리밍 대기)을 안 눌렀으면 웹플레이는
            #   "퍼플온이 실행된 PC가 없습니다" 이고, 매크로는 20초마다 새로고침만 한다.
            #   ★그 상태에서는 수집이 절대 성공할 수 없다.★ 그런데 순환은 그걸 모르고
            #   ROT_COLLECT_MAX(7분)를 다 태운 뒤 "정보수집이 확인되지 않습니다" 라고
            #   ★원인과 다른 문구★ 로 죽는다 — 주인님이 엉뚱한 데를 찾아가게 된다.
            #   주인님: "12번 화면봐로 엉망진창이다"
            #   → 매크로가 nohost 를 찍고 있으면 ★즉시★ 진짜 사유로 세운다.
            #     (호스트는 원격컴이 못 만든다 — 본컴 런처를 눌러야 하므로 사람/파섹 일이다)
            try:
                _lg = await get_logs(ns(tenant, str(active.get("pc_id") or base)), limit=25)
                _nh = sum(1 for _l in (_lg or [])
                          if "아직 호스트 없음" in str(_l.get("message") or ""))
            except Exception:
                _lg, _nh = [], 0
            if _nh >= 3:                             # 새로고침 3회 = 최소 60초째 무호스트
                await _rot_stop(
                    tenant, base,
                    "⛔ 순환 정지 — ★본컴이 스트리밍 대기가 아닙니다★ "
                    "(웹플레이: '퍼플온이 실행된 PC가 없습니다'). "
                    "정보수집은 게임 화면이 있어야 되므로 여기서는 절대 성공하지 않습니다. "
                    "본컴 퍼플 런처에서 '재시작' 을 누르거나 계정전환으로 호스트를 잡아주세요",
                    st)
                return
            # ★★매크로가 수집을 '거부' 했으면 한 번 다시 보낸다 (2026-08-21 PC-09)★★
            #   위 유예(ROT_ARM_GRACE)가 못 막는 경합이 아직 남는다 — 주인님이나 내가
            #   다른 긴 명령(switch·screenshot…)을 쏜 직후에도 같은 거부가 난다.
            #   그때 로그의 "'collect_info'를 거부" 는 ★버려졌다는 확정 증거★ 다.
            #   (추측이 아니라 그 PC 가 직접 찍은 줄이다 — A2 를 만족한다)
            #   → 딱 한 번만 재전송한다. 무한 재시도로 바꾸지 않는다.
            if not st.get("recollect") and any(
                    "'collect_info'를 거부" in str(_l.get("message") or "")
                    for _l in (_lg or [])):
                st["recollect"] = True
                print(f"[순환] {base} 수집 거부 감지 → collect_info 재전송")
                if await _rot_send(tenant, str(active.get("pc_id")), "collect_info", {}):
                    st["since"] = _rot_now()         # 상한도 그 시점부터 다시 센다
                    await _rot_say(tenant, base,
                                   "정보수집이 거부돼 있었습니다 → 다시 보냈습니다")
                return
            return                                   # 아직 기다린다
        else:
            await _rot_stop(tenant, base,
                            f"⛔ 순환 정지 — 정보수집이 {int(age/60)}분째 "
                            f"확인되지 않습니다(상한 {int(_cmax/60)}분)"
                            f"(char_info 갱신 없음). 화면 확인 필요")
            return

        nxt, why = _rot_next_acct(cards, active)
        if nxt < 0:
            await _rot_stop(tenant, base,
                            f"⛔ 순환 정지 — {why}\n"
                            f"info.txt 에 캐릭 이름을 적고 ▶시작을 다시 눌러주세요")
            return
        if nxt == 0:
            # ★N8★ '남은 계정 없음' 은 성공이지만 '구버전이라 보류' 는 실패다.
            #   둘 다 0 이라 초판은 배포 안 된 PC 에서 ★초록 ✅ 로 조용히 끝났다.★
            _ok_end = "남은 계정" in why
            await _rot_stop(tenant, base,
                            (f"✅ 순환 종료 — {why}" if _ok_end else f"⛔ 순환 정지 — {why}"))
            return

        # ★무한 왕복 방지★ 미완 계정을 계속 도는 설계라, 어떤 계정이 영영 진행이 안 되면
        #   계정1↔계정2 를 밤새 왕복하며 전환만 반복한다(1회 = 본컴+원격컴+재시작).
        _tgt = next((c for c in cards if _rot_acct_no(c.get("pc_id")) == nxt), None)
        _prog = _rot_progress(_tgt) if _tgt else -1
        _vis = st.setdefault("visits", {})
        _prev = _vis.get(str(nxt))
        if _prev is not None and _prog >= 0 and _prog <= _prev:
            await _rot_stop(tenant, base,
                            f"⛔ 순환 정지 — 계정{nxt} 로 갔다 왔는데 오늘 끝낸 캐릭이 "
                            f"{_prev}개 그대로입니다(진행 없음). 화면 확인 필요")
            return
        if int(st.get("hops") or 0) + 1 > ROT_MAX_HOPS:
            await _rot_stop(tenant, base,
                            f"⛔ 순환 정지 — 계정 전환을 {ROT_MAX_HOPS}회 했습니다(상한). 확인 필요")
            return

        if not _alive():
            return
        # ★★peer_id 가 없으면 아예 시작하지 않는다 (2026-08-20 PC-22 실사고)★★
        #   주소록에 없는 PC 로 switch_launcher 를 쏘면 매크로가 ★반쪽만★ 한다:
        #     "[원격명령] 계정전환 'b': peer_id 가 없어 ★원격컴 크롬만★ 바꾼다"
        #   그러면 원격컴은 계정2, 본컴은 계정1 — 짝이 안 맞아 스트림이 영영 안 뜬다.
        #   그런데 명령 응답은 200 이라 겉으로는 성공으로 보인다(A2 그 자리).
        #   → 순환은 반쪽 전환을 만들지 않는다. 세우고 알린다.
        try:
            _pm = await _get_parsec_map(tenant)
            _num = "".join(ch for ch in base if ch.isdigit()).lstrip("0") or base
            if not (_pm.get(_num) or _pm.get(base)):
                await _rot_stop(tenant, base,
                                f"⛔ 순환 정지 — 파섹 주소록에 {base} 의 peer_id 가 없습니다. "
                                f"본컴 런처를 못 바꿔 반쪽 전환이 됩니다. "
                                f"관제컴에서 parsec_multi.py push 후 ▶시작을 다시 눌러주세요")
                return
        except Exception as _pe:
            print(f"[순환] {base} 주소록 확인 실패(계속 진행): {_pe}")
        # ★acct_index 는 1 고정 [S11]★ — 이 값은 계정 번호가 아니라 런처 드롭다운의
        #   '다른 계정' 몇 번째 줄인가다. 목록은 ★현재 계정을 뺀 것★ 이라 nxt 로
        #   유추하면 틀린다(계정3→계정2 역주행에서 계정1 을 가리킨다). 대시보드의 다른
        #   두 호출부도 전부 1 고정이고, 정확한 판정은 acct_no(이메일 줄 템플릿)가 한다.
        ok = await _rot_send(tenant, str(active.get("pc_id")), "switch_launcher", {
            "acct_index": 1, "acct_no": nxt,
            "acct_label": f"계정{nxt}", "chrome_label": "abcd"[nxt - 1], "launch": True,
        })
        if not ok or not _alive():
            return
        _vis[str(nxt)] = _prog
        st.update({"stage": "switching", "since": _rot_now(),
                   # ★만료를 같이 심는다 [C2]★ — 부팅 지문이 유실되면(실측 83회 중 4회)
                   #   expect_restart 가 True 로 남아 ★다음번 주인님의 재시작을 삼킨다.★
                   #   그러면 "껐다 켜면 순환 해제"(요구 1) 가 조용히 깨진다.
                   "expect_restart": True,
                   "expect_until": _rot_now() + ROT_SWITCH_MAX,
                   "target": "abcd"[nxt - 1],
                   "hops": int(st.get("hops") or 0) + 1})
        await _rot_say(tenant, base, f"계정{nxt} 로 전환 시작 (본컴 런처 + 원격컴 크롬)")
        return

    # ── ③ 전환 대기 — 목표 계정 카드가 살아나면 ▶시작 ──────────────────────
    if stage == "switching":
        want = str(st.get("target") or "")
        want_no = ("abcd".index(want) + 1) if want in "abcd" else 0
        # ★작업 순환은 전환이 끝나면 start 가 아니라 ★그 작업★ 을 보낸다 (2026-08-23)★
        #   여기서 완주 순환 코드로 흘려보내면 사냥만 시작하고 작업은 영영 안 한다
        #   = "버튼은 있는데 안 도는" 반쪽 실행(§A4). 그래서 이 분기는 자기 상한까지
        #   전부 들고 ★반드시 return 한다.★
        if _task:
            _s = str((active or {}).get("status") or "")
            if active and _rot_acct_no(active.get("pc_id")) == want_no                     and _s in ("idle", "hunting", "moving"):
                if not _alive():
                    return
                if not await _rot_send(tenant, str(active.get("pc_id")), _task, {}):
                    return                           # ★송신 실패면 단계를 넘기지 않는다 [S13]★
                if not _alive():
                    return
                st.update({"stage": "tasking", "since": _rot_now(), "sent_at": _rot_now(),
                           "busy": False, "expect_restart": False})   # ★전환 확인 → 기대 소비 [C2]★
                await _rot_say(tenant, base, f"계정{want_no} 전환 완료 → {_tlabel} 시작")
                return
            if age > ROT_SWITCH_MAX:
                await _rot_stop(tenant, base,
                                f"⛔ 순환 정지 — 계정{want_no} 전환이 {int(age/60)}분째 "
                                f"안 끝났습니다(status={_s or '카드 없음'}). 화면 확인 필요")
            return
        if active and _rot_acct_no(active.get("pc_id")) == want_no:
            s = str(active.get("status"))
            if s in ("hunting", "moving"):
                st.update({"stage": "hunting", "since": _rot_now(),
                           "expect_restart": False})   # ★전환 확인 → 기대 소비 [C2]★
                await _rot_say(tenant, base, f"계정{want_no} 사냥 시작 확인")
                return
            if s == "idle":
                # ★이미 오늘 할 게 없는 계정이면 start 를 쏘지 않는다 [S12]★
                #   loot.py 의 start 는 "오늘 모든 슬롯 완료 → 시작 불가"로 조용히 반환한다.
                #   그러면 7분 뒤 오경보로 순환이 죽는다. 여기서 미리 걸러 다음 계정으로.
                if _rot_done(active):
                    # ★수집을 안 했으니 수집 증거를 요구하면 안 된다 [재검증 S-F]★
                    #   초판은 char_before 에 ★현재값★ 을 넣고 collecting 으로 넘겼다.
                    #   수집을 보낸 적이 없으니 그 값은 영원히 안 바뀌고, 7분 뒤
                    #   "⛔ 정보수집이 확인되지 않습니다" 라는 ★엉뚱한 사유로 순환이 죽었다.★
                    #   (텔레그램은 "다음 계정을 찾습니다" 라고 말해놓고 못 찾는다 =
                    #    미완 계정이 남아 있어도 거기서 끝 → 사용자 요구 4가 깨진다.)
                    st.update({"stage": "collecting", "since": _rot_now(),
                               "char_before": "", "skip_collect": True,
                               "expect_restart": False})   # ★전환 확인 → 기대 소비 [C2]★
                    await _rot_say(tenant, base,
                                   f"계정{want_no} 는 오늘 이미 완주 — 다음 계정을 찾습니다")
                    return
                if not _alive():
                    return
                if not await _rot_send(tenant, str(active.get("pc_id")), "start", {}):
                    return
                if not _alive():
                    return
                st.update({"stage": "starting", "since": _rot_now(),
                           "expect_restart": False})   # ★전환 확인 → 기대 소비 [C2]★
                await _rot_say(tenant, base, f"계정{want_no} 로 전환 완료 → ▶시작")
                return
            # ★★여기서 return 하면 아래 20분 상한이 ★영원히 평가되지 않는다★ [C1]★★
            #   2026-08-20 적대검증에서 잡혔다. 목표 계정 카드가 살아는 있는데 상태가
            #   captcha·reconnecting·error 면 매 틱 이 줄로 빠져나가 ROT_SWITCH_MAX 를
            #   건너뛰었다. 캡차는 ★계정 전환 직후 가장 흔한 상태★ 라 실전에서 바로 걸린다.
            #   증상: 20분 뒤 떠야 할 "⛔ 계정N 전환이 20분째" 알람이 안 뜨고,
            #        18시간 뒤 ROT_TTL 이 "무장이 18시간을 넘겨 자동 해제" 라는
            #        ★원인과 무관한 문구★ 로 끝난다 = 주인님이 엉뚱한 데를 찾아간다.
            #   → return 이 아니라 pass. 더 기다리되 ★상한은 적용한다.★
            pass
        if age > ROT_SWITCH_MAX:
            await _rot_stop(tenant, base,
                            f"⛔ 순환 정지 — 계정{want_no} 전환이 {int(age/60)}분째 "
                            f"안 끝났습니다. 화면 확인 필요")
        return

    # ── ④ 시작 대기 ────────────────────────────────────────────────────────
    if stage == "starting":
        if active and str(active.get("status")) in ("hunting", "moving"):
            st.update({"stage": "hunting", "since": _rot_now()})
            await _rot_say(tenant, base, f"계정{_rot_acct_no(active.get('pc_id'))} 사냥 시작 확인")
            return
        if age > ROT_START_MAX:
            await _rot_stop(tenant, base,
                            f"⛔ 순환 정지 — ▶시작 뒤 {int(age/60)}분째 사냥이 안 잡힙니다. "
                            f"화면 확인 필요")
        return


async def _rot_engine() -> None:
    """계정 자동순환 엔진 — 무장된 PC 만 본다. 무장이 없으면 아무 일도 안 한다."""
    await _rot_load()
    await asyncio.sleep(25)              # 부팅 직후 상태가 채워질 여유
    print("[순환] 엔진 시작")
    while True:
        try:
            # ★★소실 감시는 `if _ROT:` ★밖★ 에서 돈다 (2026-08-21 적대검증이 잡음)★★
            #   초판은 _rot_save() 안에서만 불렀는데, _rot_save 는 아래 `if _ROT:` 블록
            #   안에서만 호출된다. 그래서 ★마지막 한 대가 사유 없이 사라지면★ 다음 틱에
            #   _ROT 가 비어 블록을 통째로 건너뛰고 → 감시기가 영영 안 돌아 ★침묵★ 한다.
            #   하필 그게 이 장치를 만든 이유(PC-17, 무장 1대 상태에서 사라짐)와 같은 상황이다.
            #   같은 파일 N2 주석이 이미 경고한 함정인데(_rot_note_boot 만 force 우회를 넣었다)
            #   감시기에는 그 우회를 안 넣었다.
            _rot_watch_vanish()
            if _ROT:
                by_tenant: dict[str, list[str]] = {}
                for k in list(_ROT):
                    t, raw = split_ns(k)
                    by_tenant.setdefault(t, []).append(raw)
                for tenant, bases in by_tenant.items():
                    # ★테넌트 하나가 터져도 나머지는 돈다 [S13]★
                    try:
                        pcs = await _build_full_state(tenant)
                    except Exception as e:
                        print(f"[순환] {tenant} 상태 조회 실패(이번 틱 건너뜀): {e}")
                        continue
                    for base in bases:
                        st = _ROT.get(ns(tenant, base))
                        if not st:
                            continue
                        try:
                            await _rot_step_pc(tenant, base, st, pcs)
                        except Exception as e:
                            print(f"[순환] {base} 단계 예외(무시): {e}")
                await _rot_save()
        except Exception as e:
            print(f"[순환] 엔진 예외(무시): {e}")
        await asyncio.sleep(ROT_TICK)


# ── 조회 / 수동 조작 ─────────────────────────────────────────────────────────
@app.get("/rotate")
async def rotate_list(request: Request):
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    out = {}
    for k, st in list(_ROT.items()):
        t, raw = split_ns(k)
        if t == tenant:
            out[raw] = {"stage": st.get("stage"), "target": st.get("target"),
                        "hops": st.get("hops"), "visits": st.get("visits"),
                        "age_s": int(_rot_now() - float(st.get("since") or 0))}
    return JSONResponse({"rotating": out, "allow": sorted(await _rot_allow(tenant))})


@app.post("/rotate/allow")
async def rotate_allow(request: Request):
    """카나리아 게이트 설정 — body {"pcs": "PC-10,PC-20"} 또는 {"pcs": "*"}."""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="객체가 필요합니다")
    pcs = str(body.get("pcs") or "").strip()
    await set_setting(ns(tenant, ROT_ALLOW_KEY), pcs)
    return JSONResponse({"ok": True, "allow": pcs})


@app.post("/rotate/{pc_id}")
async def rotate_set(pc_id: str, request: Request):
    """순환 수동 on/off — body {"on": true|false}."""
    tenant = check_session(request)
    if not tenant:
        raise HTTPException(status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="객체가 필요합니다")
    pc_id = clean_pc_id(pc_id)
    if body.get("on"):
        ok, why = await _rot_arm(tenant, pc_id)
        await _rot_save(force=True)
        return JSONResponse({"ok": ok, "on": ok, "pc": _base_pc(pc_id), "why": why})
    _rot_disarm(tenant, pc_id, "수동 해제")
    await _rot_save(force=True)
    return JSONResponse({"ok": True, "on": False, "pc": _base_pc(pc_id)})

# redeploy trigger 2026-08-21 00:55 (Railway 502 — 새 배포 시도)
