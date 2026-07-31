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
    if tenant and tenant_expired(tenant):
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
    if not tenant or tenant_expired(tenant):
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
        """해당 테넌트의 대시보드에게만 전송 (테넌트 격리)."""
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
    if tenant_expired(tenant):
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
    tg_task = asyncio.create_task(_tg_poller()) if tg_enabled() else None
    try:
        yield
    finally:
        if tg_task:
            tg_task.cancel()
            try:
                await tg_task
            except (asyncio.CancelledError, Exception):
                pass


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
                "_bug_count":       bug_counts.get(pid, 0),
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
    return statuses


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
@app.get("/setting/{key}")
async def get_setting_ep(key: str, request: Request):
    """매크로(X-Api-Key)와 대시보드(세션) 양쪽 조회 허용."""
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
    await set_setting(ns(tenant, key), str(body.get("value", ""))[:100])
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
    elif tenant_expired(tenant):
        payload = {"valid": False, "reason": "expired", "expires": (TENANTS[tenant].get("expires") or ""), "now": now, "nonce": nonce}
    else:
        payload = {"valid": True, "reason": "", "expires": (TENANTS[tenant].get("expires") or ""), "now": now, "nonce": nonce}
    base = f"{payload['valid']}|{payload['reason']}|{payload['expires']}|{payload['now']}|{nonce}"
    payload["sig"] = hmac.new(LICENSE_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
    return JSONResponse(payload)


@app.get("/health")
async def health():
    """[진단] 업타임+메모리 — Railway 자발 재시작(배포 무관 boot 변경) 원인 추적용(2026-07-25).
    uptime이 짧으면 최근 크래시/재시작, rss가 계속 오르면 메모리 누수→OOM 의심. (민감정보 없음)"""
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
    return JSONResponse({
        "boot": SERVER_BOOT_ID[:8],
        "uptime_s": int(time.time() - SERVER_BOOT_TS),
        "rss_max_mb": rss_mb,
        "db_path": _dbp,
        "db_size_kb": (round(os.path.getsize(_dbp) / 1024, 1) if os.path.exists(_dbp) else 0),
        "bug_files": _bugs,
        # ★경로 추측이 아니라 실측: 지난 부팅의 마커가 살아남았는지로 판정(_probe_volume)★
        #   false면 재시작마다 DB·스샷이 전부 사라진다 → Railway 볼륨을 마운트해야 한다.
        "disk_persisted": VOLUME_PERSISTED,
        "prev_boot": (VOLUME_PREV.get("boot") or "")[:8] or None,
        "prev_boot_at": VOLUME_PREV.get("at"),
    })


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
  .chip-emerald{--c:52,211,153} .chip-teal{--c:45,212,191}
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
    <button id="tts-btn" onclick="toggleTts()" class="px-3 py-1 rounded-lg text-xs font-semibold bg-gray-700/70 hover:bg-gray-600 text-gray-300 transition-colors whitespace-nowrap">🔇 음성 꺼짐</button>
    <button onclick="toggleVoicePanel()" class="px-2 py-1 rounded-lg text-xs bg-gray-700/70 hover:bg-gray-600 text-gray-300 transition-colors" title="목소리 고르기">⚙</button>
    <a href="#" onclick="window.open('/manual?t='+Date.now(),'_blank');return false;" class="px-3 py-1 rounded-lg text-xs font-semibold bg-indigo-800/70 hover:bg-indigo-600 text-indigo-100 transition-colors whitespace-nowrap" title="이용 매뉴얼 PDF 열기 / 내려받기 (항상 최신본)">📘 매뉴얼</a>
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

  <!-- 전광판 (순서: 온라인 → 완료 → 오드에너지 → 각성전 → 일일던전 → 거래키나 → 창고키나)
       — 열 폭은 .stat-grid(숫자 긴 오드에너지/거래키나/창고키나=3fr, 나머지=2fr) -->
  <div class="stat-grid">
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
    <button class="cm-btn chip-amber"  onclick="settleFromMenu()" title="전 캐릭 준비 — 정산 → 추출 → 개인/서버창고 보관 → 인벤정렬 → 귀환주문서 보충">🧰 준비</button>
    <button class="cm-btn chip-cyan"   onclick="collectInfoFromMenu()">📡 정보수집</button>
    <button class="cm-btn chip-gray"   onclick="cardCmd('go_home')">⌂ 귀환</button>
  </div>
  <div class="cm-sec">VIEW</div>
  <div class="cm-grid3">
    <button class="cm-btn chip-indigo" onclick="openLogFromMenu()">📋 로그</button>
    <button class="cm-btn chip-sky"    onclick="openInfoFromMenu()">📊 정보</button>
    <button class="cm-btn chip-pink"   onclick="screenshotFromMenu()">📸 스샷</button>
  </div>
  <div class="cm-sec">화면 · 원격</div>
  <div class="cm-grid2">
    <button class="cm-btn chip-emerald" onclick="liveFromMenu()" title="실시간 화면 — 어디서나 됨 (Railway 경유, 960x540 · 3fps)">🖵 화면</button>
    <button class="cm-btn chip-teal" id="cm-lan" onclick="lanFromMenu()" title="내부망 직결 — 원본 해상도 + 원격 조작 (같은 내부망에서만)">⚡ 내부망 원격</button>
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
    <b class="text-sm text-gray-100">🎙 목소리 설정</b>
    <button onclick="toggleVoicePanel()" class="text-gray-500 hover:text-gray-200">✕</button>
  </div>
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
      style="min-width:0" title="${escAttr(name)}${done?' ✓ '+time:isActive?' 진행 중':''}">
      <span class="font-bold text-xs leading-none">${icon}</span>
      <span style="font-size:9px;line-height:1.2;max-width:100%;overflow:hidden;white-space:nowrap">${esc(short)}</span>
      ${classLabel?`<span style="font-size:8px;line-height:1;color:#9ca3af">${classLabel}</span>`:''}
    </div>`;
  }).join('');
  return `<div class="mt-2 pt-2 border-t border-gray-800/60">
    <div class="flex items-center justify-between mb-1">
      <span class="text-gray-400" style="font-size:10px">오늘 완료 <span class="${completed===total?'text-green-500':'text-gray-500'}">${completed}/${total}</span>${pc._char_collected_at?` · <span class="text-cyan-600">수집 ${relTime(pc._char_collected_at)}</span>`:''}</span>
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
    `<div class="text-xs text-red-400 bg-red-900/30 rounded px-2 py-0.5">⚠ ${esc(e)}</div>`).join('');
  const bugBadge = (pc._bug_count||0)>0
    ? `<span class="ml-1.5 px-1.5 py-0.5 bg-red-700/80 text-red-200 rounded text-xs font-bold leading-none cursor-pointer" onclick="event.stopPropagation();openBugsModal('${pc.pc_id}')">🐛 ${pc._bug_count}</span>`
    : '';
  const ucls = {'running':'text-green-400','stopped':'text-gray-500','updating':'text-cyan-400','crashed':'text-red-400'}[pc._updater_state]||'text-gray-600';
  const mvcls = (pc.macro_version && latestVersions.macro && pc.macro_version !== latestVersions.macro) ? 'text-red-400' : 'text-gray-700';
  const uvcls = (pc._updater_version && latestVersions.updater && pc._updater_version !== latestVersions.updater) ? 'text-red-400' : 'text-gray-700';
  const macroVer = pc.macro_version ? `<span class="${mvcls}">매크로 v${pc.macro_version}</span>` : '';
  const updaterRow = (pc._updater_state&&pc._updater_state!=='unknown')
    ? `<div class="mt-1 flex items-center gap-1 text-gray-600 whitespace-nowrap overflow-hidden" style="font-size:10px">${macroVer}${macroVer?'<span class="text-gray-800">|</span>':''}<span>업데이터</span><span class="${ucls}">${esc(pc._updater_state)}</span>${pc._updater_version?`<span class="${uvcls}">v${esc(pc._updater_version)}</span>`:''}</div>`
    : '';
  const activeSlot = pc.slot||0;
  const activeDp = (pc.daily_progress||[]).find(c=>c.slot===activeSlot&&!c.completed);
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
          <div class="font-bold text-base flex items-center gap-0 min-w-0">
            <span class="truncate">${esc(pc.pc_id||'?')}</span>
            <span class="shrink-0 flex items-center">${doneBadges}${bugBadge}</span>
          </div>
          ${activeTag?`<div class="mt-0.5 truncate">${activeTag}</div>`:''}
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
      <div class="col-span-2"><span class="text-gray-400">맵</span> <span class="text-white font-medium">${esc(pc.map_name||'–')}</span></div>
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
  const dungeonLeft = new Set();   // 일일던전(계정 티켓) 안 끝난 PC — 오프라인 포함(계정 기준)
  pcs.forEach(p=>{
    const s=p.status||'offline';
    const isOnline = (STATUS_CFG[s]||STATUS_CFG.offline).online;
    if(isOnline) c.online++; else c.offline++;
    if(!isDungeonDone(p)) dungeonLeft.add(p.pc_id);
    const dp = p.daily_progress||[];
    if(dp.length>0 && dp.every(d=>d.completed)) c.completed++;
    // 창고키나: PC별 1회만 합산 (창고 공유 → 중복 방지)
    if(p._total_kina && !seenPc.has(p.pc_id)) {
      seenPc.add(p.pc_id);
      c.totalKina += p._total_kina;
    }
  });
  // 오드에너지 + 각성전 티켓 + 거래키나 합산 (charTableData 기준, 거래키나는 캐릭터별 소지라 전 캐릭 합산)
  let totalOdd = 0, totalAwaken = 0, awakenSeen = false, totalTrade = 0, tradeSeen = false;
  charTableData.forEach(r => {
    totalOdd += parseOddEnergy(r.odd_energy);
    if (r.awakening_ticket != null) { awakenSeen = true; totalAwaken += (parseInt(r.awakening_ticket) || 0); }
    if (r.trade_kina != null) { tradeSeen = true; totalTrade += (Number(r.trade_kina) || 0); }
  });
  document.getElementById('cnt-online').textContent=c.online;
  document.getElementById('cnt-odd-energy').textContent=totalOdd > 0 ? totalOdd.toLocaleString() : '–';
  document.getElementById('cnt-awakening').textContent=awakenSeen ? totalAwaken.toLocaleString() : '–';
  document.getElementById('cnt-trade-kina').textContent=tradeSeen ? fmtKinaKor(totalTrade) : '–';
  document.getElementById('cnt-dungeon-left').textContent=pcs.length ? String(dungeonLeft.size) : '–';
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

function loadSalePrice() {
  const el=document.getElementById('sale-price'), btn=document.getElementById('sale-price-btn');
  if(!el||!btn) return;
  const v=localStorage.getItem('sale_price');
  if(v) el.value=v;                       // 프리셋에 없는 옛 저장값이면 select가 빈 값으로 남는다(재확정 유도)
  if(isSalePriceConfirmed()&&el.value){ el.disabled=true; el.classList.add('opacity-60'); btn.textContent='수정'; }
  else { el.disabled=false; el.classList.remove('opacity-60'); btn.textContent='확정'; }
}
function toggleSalePrice() {
  const el=document.getElementById('sale-price'), btn=document.getElementById('sale-price-btn');
  if(isSalePriceConfirmed()){
    localStorage.setItem('sale_price_confirmed','0');
    el.disabled=false; el.classList.remove('opacity-60'); btn.textContent='확정'; el.focus();
  } else {
    const p=parseInt(el.value||'0',10);
    if(!p||p<=0){alert('거래소 가격을 선택하세요');return;}
    localStorage.setItem('sale_price', String(p));
    localStorage.setItem('sale_price_confirmed','1');
    el.disabled=true; el.classList.add('opacity-60'); btn.textContent='수정';
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
  if(!confirm(`선택 ${selectedPcs.size}대 정산 실행\n(계정 단위 — 1캐릭만 접속해 판매대금 수령, ~2분)`))return;
  await selCmd('prepare');
}

async function settleFromMenu() {
  if(!menuPcId) return;
  const pc=menuPcId;
  if(!confirm(`${pc} 정산 실행\n(계정 단위 — 1캐릭만 접속해 판매대금 수령, ~2분)`))return;
  closeCardMenu();
  const ok=await sendCmd(pc,'prepare',{});
  showToast(ok?`✓ 준비 → ${pc}`:`✗ 준비 전송 실패`);
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
  }catch(e){}
}
function handleCorridorMsg(msg){
  corridorRemaining[msg.pc_id]={remaining:(msg.data||{}).remaining,total:(msg.data||{}).total};
  updateCorridorTile();
  scheduleRender();  // 카드 🌀 회랑 완료 뱃지 즉시 반영
  loadCharTable();   // 스프레드 '회랑' 열 갱신 (악몽 진행도와 같은 패턴)
}
loadCorridorSummary();

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
  document.getElementById('bug-clear-btn').onclick = () => clearBugsOf(pc_id);
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
    const pcServer = (state[pc] || {}).server || '';
    const serverTag = pcServer ? ` <span class="text-cyan-400 text-xs font-normal ml-1">[${pcServer}]</span>` : '';
    const pcKinaRaw = pcRows[0]?.total_kina;
    const kinaTag = pcKinaRaw ? ` <span class="text-yellow-300 text-xs font-normal ml-1">₭${Number(pcKinaRaw).toLocaleString()}</span>` : '';
    html += `<tr class="bg-gray-700/80 cursor-pointer" onclick="togglePcGroup('${pc}')">
      <td colspan="22" class="px-3 py-2 font-bold text-gray-100">
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
    LIVE_FRAMES[key] = {"jpg": body, "meta": meta, "ts": now}
    if now - LIVE_WATCH.get(key, 0.0) > LIVE_TTL:
        return Response(status_code=204)      # 아무도 안 봄 → 매크로가 스스로 끈다
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
    body = await request.json()
    command = body.get("command")
    if not command:
        raise HTTPException(status_code=400, detail="command 필드 필요")
    args = body.get("args", {})
    nspc = ns(tenant, pc_id)
    cmd_id = await insert_command(nspc, command, args)
    # 매크로 WS 연결되어 있으면 즉시 전달
    ws_sent = await send_command_to_macro(nspc, command, args, cmd_id)
    # 브로드캐스트 (명령 내역 갱신용)
    await _push_cmd_history(tenant)
    return JSONResponse({"ok": True, "id": cmd_id, "ws": ws_sent})


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
    if not tenant or tenant_expired(tenant):
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
                payload["pc_id"] = nspc   # 저장 키와 일치(테넌트 필터 기준) — 출력 시 벗김
                await upsert_status(nspc, payload)
                errors = payload.get("errors") or []
                for e in errors[:3]:
                    await insert_log(nspc, "warn", str(e))
                await push_state(tenant)
            elif msg_type == "log":
                logs = msg.get("logs", [])
                for entry in logs:
                    await insert_log(nspc, entry.get("level", "info"), entry.get("message", ""))
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
    if not tenant or tenant_expired(tenant):
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
        raise HTTPException(status_code=403)
    return JSONResponse({"enabled": bool(tg_enabled() and tenant_chat_id(tenant))})


@app.post("/telegram/send/{pc_id}")
async def telegram_send(pc_id: str, request: Request):
    """매크로 → 텔레그램 텍스트 중계. 매크로에 봇 토큰이 없어도 알림이 간다."""
    tenant = check_api_key(request)
    if not tenant:
        raise HTTPException(status_code=403)
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
    cmd = await get_pending_command(ns(tenant, pc_id), all_key=ns(tenant, "all"))
    if cmd:
        return JSONResponse({"command": cmd["command"], "args": cmd["args"], "id": cmd["id"]})
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
            if not m or m.group(1) != pc_id:
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

def _screenshot_path(tenant: str, category: str, pc_id: str, slot: int) -> str:
    """테넌트별 스크린샷 경로. main = 기존 경로(호환), 그 외 = 테넌트 하위 폴더."""
    category = os.path.basename(category)
    pc_id = os.path.basename(pc_id)
    base = SCREENSHOTS_DIR if tenant == "main" else os.path.join(SCREENSHOTS_DIR, tenant)
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
    img_bytes = _b64.b64decode(img_b64)
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
# ★메모리 저장★ — DB 스키마 무변경. 재배포로 날아가도 매크로가 슬롯 마감·런 종료마다
#   '전체 스냅샷'을 다시 보내므로 다음 전송 한 방에 복원된다(라이브 프레임과 같은 철학).
CORRIDOR_PROG: dict = {}    # {"tenant::PC-01": {our, slots, remaining, total, ts}}


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
    for k, v in CORRIDOR_PROG.items():
        t, raw = split_ns(k)
        if t == tenant:
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
    for k, v in CORRIDOR_PROG.items():
        t, raw = split_ns(k)
        if t != tenant:
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
_GH_CDN = "https://cdn.jsdelivr.net/gh/kevincom-honjong/aion2-macro-releases@main"

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
        print(f"[version] raw 조회 실패 → 로컬 사본 사용: {e.__class__.__name__}: {e}")
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

@app.post("/check")
async def updater_check(request: Request):
    """updater.exe가 호출 — exe/이미지/updater 업데이트 필요 여부 응답"""
    body = await request.json()
    client_exe_ver     = body.get("exe_version", "0.0.0")
    client_img_hashes  = body.get("image_hashes", {})
    client_updater_ver = body.get("updater_version", "0.0.0")
    client_edition     = body.get("edition", "main")   # 렌탈 채널(2026-07-26): rental이면 rental exe 배포

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
    if exe_info and server_exe_ver != client_exe_ver:
        result["exe_update"] = {
            "version":      server_exe_ver,
            "sha256":       exe_info.get("sha256"),
            # exe(71MB)는 GitHub Releases(CDN)에서 배포 — raw 429 우회. jsDelivr는 용량초과라 불가.
            # 규칙: 릴리스 태그 v<버전>, 에셋 이름 macro-<버전>.exe / rental-<버전>.exe
            "download_url": f"https://github.com/kevincom-honjong/aion2-macro-releases/releases/download/v{server_exe_ver}/{asset_prefix}-{server_exe_ver}.exe",
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
