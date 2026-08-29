# updater.py - 매크로 상주형 업데이터 데몬 v2.0
# PyInstaller로 updater.exe 빌드 후 각 PC에 배포
# 동작: 항상 상주, 대시보드 명령 수신 → 매크로 시작/정지/재시작/업데이트
#
# 빌드 명령:
#   pyinstaller --onefile --noconsole updater.py

import os
import sys
import json
import hashlib

# PyInstaller exe에서 certifi TLS 인증서 경로 문제 해결
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
import shutil
import subprocess
import logging
import time
import threading
from pathlib import Path
from datetime import datetime

import ctypes
from ctypes import wintypes

import requests   # pip install requests
from PIL import ImageGrab  # pip install pillow

# ==================================================
# 설정
# ==================================================
UPDATER_VERSION  = "3.1.10"

UPDATE_SERVER    = "https://web-production-8d4c.up.railway.app"
CONTROL_SERVER   = "https://web-production-8d4c.up.railway.app"
# ★키를 소스에 박지 않는다(2026-07-27 보안감사 critical): 이 저장소는 공개라
#   평문 상수가 그대로 노출됐고, 그 키 하나로 함대 전체에 원격명령 주입이 가능했다.
#   → 환경변수 또는 C:\auto\info.txt 의 control_api_key 로만 받는다(load_pc_id에서 주입).★
#   ※주의: 이 상수를 비운 채 updater.exe를 새로 빌드해 배포하려면, 먼저 각 PC의
#     info.txt에 control_api_key= 를 넣어야 한다(안 넣으면 그 PC는 서버와 통신 불가).
#     이미 돌고 있는 기존 exe는 영향 없음(빌드 시점에 값이 박혀 있음).
CONTROL_API_KEY  = os.getenv("AION2_CONTROL_KEY", "")

TIMEOUT_CONNECT  = 15
TIMEOUT_DOWNLOAD = 180   # 청크 간 최대 대기(초). 75MB 느린망 대비 120→180

# 다운로드 재시도: 대시보드 '재시작' 업데이트가 75MB 받다 한 번 삐끗(순단/타임아웃)하면
# 재시도 없이 실패 → 옛 버전으로 재시작하던 문제 해결. 재시도마다 백오프 후 처음부터 다시 받음.
DOWNLOAD_MAX_RETRIES  = 4
DOWNLOAD_RETRY_BACKOFF = (3, 8, 15)   # 시도 2·3·4 전 대기(초)

MACRO_EXE        = r"C:\auto\혼종_통합_자동.exe"
MACRO_EXE_BACKUP = r"C:\auto\혼종_통합_자동.exe.bak"
EDITION          = "main"   # info.txt edition=rental 이면 load_pc_id에서 rental로 전환(v3.0.7)
IMAGES_DIR       = r"C:\auto\images2"
LOCAL_VERSION    = r"C:\auto\version.json"
LOG_FILE         = r"C:\auto\updater.log"
INFO_TXT         = r"C:\auto\info.txt"
BUGS_DIR         = r"C:\auto\bugs"

POLL_INTERVAL    = 10   # 명령 폴링 간격 (초)
STATUS_INTERVAL  = 30   # 상태 보고 간격 (초)
BUG_INTERVAL     = 60   # 버그 업로드 간격 (초)
CRASH_CHECK_INT  = 5    # 크래시 체크 간격 (초)

# ==================================================
# 로깅
# ==================================================
os.makedirs(r"C:\auto", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(),
    ]
)
log = logging.info
err = logging.error

# ==================================================
# 전역 상태
# ==================================================
pc_id: str = "PC-?"
macro_proc: subprocess.Popen | None = None
macro_state: str = "stopped"   # stopped / running / updating / crashed
_state_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# 원격 로그 전송 (3.1.6, 2026-08-20 신설)
# ══════════════════════════════════════════════════════════════════════════════
# ★왜 만들었나 (2026-08-20 PC-23 실사고)★
#   매크로가 죽으면 그 PC 는 ★완전 실명★이 된다. 업데이터는 서버에 macro_state 한 칸
#   (stopped/running/updating/crashed)만 보내고, 자기가 무슨 시도를 했는지는 전부
#   C:\auto\updater.log 에만 남긴다. 그래서 PC-23 이 죽었을 때
#     "되살리려 시도는 했나 / 몇 번 했나 / 왜 실패했나 / no_restart 때문이었나"
#   를 확인할 방법이 하나도 없었다. 그 PC 에 직접 붙기 전까지는 추측뿐이었다.
#   → 업데이터 로그도 매크로처럼 서버로 올린다. 매크로가 죽어도 눈은 남는다.
#
# ★설계 원칙 3가지 (전부 이 프로젝트에서 데인 것들)★
#   ① 로그가 본체를 느리게 하거나 죽이면 안 된다
#      → 찍는 쪽은 링버퍼에 넣기만 하고(_log_push), 네트워크는 전용 스레드가 한다.
#        _log_push 는 어떤 예외도 밖으로 내지 않는다.
#   ② 되먹임 고리를 만들면 안 된다
#      → 전송하며 찍은 로그가 다시 전송 대상이 되면 영원히 안 끝난다. 2겹으로 막는다
#        (_RemoteLogHandler 주석 참조).
#   ③ 종료 직전 로그가 제일 중요한데, 그걸 버리면 안 된다
#      → 자가업데이트/exit/치명적오류 직전에 _log_shutdown 으로 밀어내고,
#        그래도 남으면 디스크 스풀에 적어 ★다음 프로세스가 이어서★ 보낸다.
LOG_FLUSH_INTERVAL = 20        # 평상시 전송 주기(초)
LOG_BATCH_MAX      = 25        # 한 번에 보내는 줄 수 (서버 /updater/log 는 50까지 받는다)
LOG_RING_MAX       = 300       # 메모리에 들고 있는 최대 줄 수
LOG_RATE_MAX       = 60        # 분당 큐 적재 상한 — 폭주 로그가 서버 DB·대역폭을 먹는 것 방지
LOG_MSG_MAX        = 500       # 한 줄 길이 상한 (서버도 500 에서 자른다)
LOG_BACKOFF_MAX    = 300.0     # 서버가 죽어 있을 때 재시도 간격 상한(초)
LOG_SPOOL_PATH     = r"C:\auto\updater_log_spool.jsonl"
LOG_SPOOL_MAX      = 500       # 스풀 파일에 남기는 최대 줄 수

# ★deque(maxlen=…) 을 쓰지 않는다★ — 전송 실패로 배치를 ★앞으로 되돌릴 때★
#   maxlen 이 걸린 deque 는 앞에 넣는 순간 ★뒤(=최신)를 버린다★. 즉 방금 찍힌
#   "왜 실패했는지" 가 사라지고 옛날 줄만 남는다. 평범한 list + 명시적 트림으로 간다.
_log_ring: list = []
_log_lock = threading.RLock()  # _log_push 가 락 안에서 다시 호출될 여지를 열어두기 위해 RLock
_log_dropped = 0               # 링버퍼가 넘쳐서 버린 줄 수 (다음 배치에 한 줄로 보고)
_log_rate_since = 0.0          # 분당 상한 창의 시작 시각
_log_rate_n = 0                # 이번 창에서 적재한 줄 수
_log_suppressed = 0            # 분당 상한에 걸려 버린 줄 수
_log_tls = threading.local()   # .sending = True 인 동안은 로그를 큐에 넣지 않는다(되먹임 차단)


def _log_push(level: str, message: str):
    """링버퍼에 한 줄 넣는다. ★절대 예외를 밖으로 내지 않는다★ (로깅이 본체를 죽이면 안 된다)."""
    global _log_dropped, _log_rate_since, _log_rate_n, _log_suppressed
    try:
        now = time.time()
        with _log_lock:
            # ── 분당 상한 ────────────────────────────────────────────────────
            #   같은 에러가 초당 수십 줄 찍히는 상황(폴링 실패 루프 등)이 실제로 있다.
            #   버리되 ★몇 줄 버렸는지는 반드시 남긴다★ — 조용한 유실은 이 프로젝트에서
            #   제일 자주 사람을 속인 실패 방식이다.
            if now - _log_rate_since >= 60.0:
                if _log_suppressed:
                    _log_ring.append({"ts": now, "level": "warn",
                                      "message": f"[로그] 분당 상한 초과로 {_log_suppressed}줄 생략"})
                    _log_suppressed = 0
                _log_rate_since = now
                _log_rate_n = 0
            if _log_rate_n >= LOG_RATE_MAX:
                _log_suppressed += 1
                return
            _log_rate_n += 1
            _log_ring.append({"ts": now, "level": level,
                              "message": str(message)[:LOG_MSG_MAX]})
            # ── 링버퍼 상한 — ★오래된 것부터 버린다★ ──────────────────────────
            #   최근 줄이 사고 원인에 가깝다. 넘치면 앞(옛날)을 자른다.
            over = len(_log_ring) - LOG_RING_MAX
            if over > 0:
                del _log_ring[:over]
                _log_dropped += over
    except Exception:
        pass


class _RemoteLogHandler(logging.Handler):
    """log()/err() 한 줄을 그대로 원격 큐에 넣는 핸들러.

    ★되먹임 고리 차단 2겹★
      ① record.name != "root" 이면 무시
         — requests/urllib3 는 자기들끼리 DEBUG 로그를 찍는다. 그걸 큐에 넣으면
           '보내려고 찍은 로그' 가 다시 보낼 거리가 되어 큐가 영원히 안 비워진다.
           이 파일이 쓰는 logging.info/error 는 전부 root 로거다.
      ② _log_tls.sending 이면 무시
         — 전송 스레드가 전송 중에 찍는 로그(예외 메시지 등)를 큐에 넣지 않는다.
           ①만으로는 _log_flush_once 안에서 err() 를 부르는 순간 뚫린다.

    INFO 미만(DEBUG)은 서버로 올리지 않는다 — 파일 로그에는 그대로 남는다.
    """

    def emit(self, record):
        try:
            if record.name != "root" or record.levelno < logging.INFO:
                return
            if getattr(_log_tls, "sending", False):
                return
            lv = ("error" if record.levelno >= logging.ERROR
                  else "warn" if record.levelno >= logging.WARNING
                  else "info")
            _log_push(lv, record.getMessage())
        except Exception:
            pass


def _log_flush_once(timeout=(TIMEOUT_CONNECT, 10)):
    """한 배치 전송. True=보냄 / False=실패(되돌림 완료) / None=지금은 보낼 수 없음.

    ★None 과 False 를 구분하는 이유★
      키·pc_id 가 아직 없는 상태는 '실패' 가 아니라 '아직 아님' 이다. 여기에 백오프를
      태우면 세팅이 끝난 뒤에도 5분씩 조용해진다. 쌓아만 두고 다음 주기에 다시 본다.
      (신규 PC 는 info.txt 를 사람이 채우기 전까지 pc_id 가 PC-?? 다)
    """
    global _log_dropped
    if not CONTROL_API_KEY or pc_id in ("PC-?", "PC-??", ""):
        return None
    with _log_lock:
        if not _log_ring:
            return None
        batch = _log_ring[:LOG_BATCH_MAX]
        del _log_ring[:len(batch)]
        if _log_dropped:
            # 유실은 ★배치 맨 앞★에 붙인다 — 시간순으로 보면 버려진 구간이 이 앞이다.
            batch.insert(0, {"ts": time.time(), "level": "warn",
                             "message": f"[로그] 버퍼 넘침 — {_log_dropped}줄 유실"})
            _log_dropped = 0
    _log_tls.sending = True
    try:
        r = requests.post(
            f"{CONTROL_SERVER}/updater/log/{pc_id}",
            json={"logs": batch, "updater_version": UPDATER_VERSION},
            headers=_headers(),
            timeout=timeout,
        )
        if r.status_code == 200:
            return True
        raise RuntimeError(f"HTTP {r.status_code}")
    except Exception:
        # ★순서를 보존하며 앞으로 되돌린다★
        #   append 로 되돌리면 옛날 줄이 최신 줄 ★뒤★로 가서 대시보드 시간순이 뒤집힌다.
        #   슬라이스 대입은 통째 삽입이라 중간에 다른 스레드가 끼어들 틈이 없다.
        with _log_lock:
            _log_ring[:0] = batch
            over = len(_log_ring) - LOG_RING_MAX
            if over > 0:
                del _log_ring[:over]      # 되돌릴 때도 오래된 것부터 버린다
                _log_dropped += over
        return False
    finally:
        _log_tls.sending = False


def _spool_dump_and_clear():
    """링버퍼를 디스크에 적어두고 비운다 — ★이 프로세스가 곧 사라질 때★ 쓴다.

    ★왜 필요한가★
      자가업데이트는 새 인스턴스가 _kill_stale_updater_processes() 로 나를 taskkill 한다.
      그 순간 메모리에 있던 '무엇을 하다 죽었는지' 가 통째로 사라진다. 파일에 남겨두면
      다음 프로세스가 _spool_load() 로 집어 이어서 보낸다 = 로그가 안 끊긴다.

    쓰기는 임시파일 + os.replace 원자 교체 — 쓰다가 죽어도 반쪽 파일이 남지 않는다.
    """
    try:
        with _log_lock:
            batch = list(_log_ring)
            _log_ring.clear()
        if not batch:
            return
        old = []
        try:
            if os.path.exists(LOG_SPOOL_PATH):
                with open(LOG_SPOOL_PATH, "r", encoding="utf-8") as f:
                    old = [ln for ln in f.read().splitlines() if ln.strip()]
        except Exception:
            old = []
        lines = old + [json.dumps(e, ensure_ascii=False) for e in batch]
        lines = lines[-LOG_SPOOL_MAX:]      # 파일이 무한히 자라지 않게
        tmp = LOG_SPOOL_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, LOG_SPOOL_PATH)
    except Exception:
        pass


def _spool_load():
    """스풀 파일을 읽어 링 ★앞★에 넣는다(이전 프로세스 줄이 더 오래됐으니까).

    ★읽자마자 먼저 지운다★
      파싱이 깨진 파일이 남아 있으면 부팅할 때마다 같은 걸 다시 읽어 영원히 실패한다.
      한 번 읽으면 파일은 없앤다 — 최악의 경우 로그 몇 줄을 잃지만, 업데이터가 매 부팅
      같은 파일에 걸려 넘어지는 것보다 낫다.
    """
    global _log_dropped
    try:
        if not os.path.exists(LOG_SPOOL_PATH):
            return
        raw = ""
        try:
            with open(LOG_SPOOL_PATH, "r", encoding="utf-8") as f:
                raw = f.read()
        finally:
            try:
                os.remove(LOG_SPOOL_PATH)
            except Exception:
                pass
        items = []
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
                if isinstance(e, dict) and e.get("message"):
                    items.append(e)
            except Exception:
                pass
        if not items:
            return
        with _log_lock:
            _log_ring[:0] = items
            over = len(_log_ring) - LOG_RING_MAX
            if over > 0:
                del _log_ring[:over]
                _log_dropped += over      # 스풀(500) > 링(300) 이라 실제로 넘칠 수 있다
        log(f"[로그] 이전 프로세스가 남긴 {len(items)}줄 이어서 전송")
    except Exception:
        pass


def _log_shutdown(reason: str):
    """프로세스가 곧 사라진다 — 남은 로그를 최대한 밀어넣고, 못 보낸 건 디스크에 남긴다.

    ★반드시 '죽는 동작' 보다 ★앞★에서 불러야 한다★
      자가업데이트는 새 인스턴스가 나를 죽인다. Popen 뒤에 부르면 전송 도중에 잘린다.
      그래서 호출 지점이 Popen ★직전★이다(self_update 참조).
    타임아웃을 (5,5) 로 줄인 것도 같은 이유 — 종료 경로에서 15초씩 붙들려 있으면 안 된다.
    """
    try:
        _log_push("warn", f"[로그] 프로세스 종료: {reason}")
        for _ in range(2):
            if _log_flush_once(timeout=(5, 5)) is not True:
                break      # 실패(False)든 보낼 게 없든(None) 더 붙들고 있지 않는다
        _spool_dump_and_clear()
    except Exception:
        pass


def _log_thread():
    """전송 전용 스레드. 로그를 찍는 쪽은 여기서 무슨 일이 나든 영향받지 않는다."""
    _spool_load()
    wait = 1.0     # 부팅 직후 한 번은 빨리 — '언제 켜졌는지' 가 대시보드에 바로 보이게
    while True:
        try:
            r = _log_flush_once()
            if r is False:
                # 서버가 죽었거나 망이 끊겼다 → 점점 뜸하게(최대 5분). 그동안 줄은 링에 쌓인다.
                wait = min(max(wait * 2, LOG_FLUSH_INTERVAL), LOG_BACKOFF_MAX)
            elif r is True:
                # 아직 밀린 게 많으면 곧바로 다음 배치 — 큰 덩어리가 20초씩 끌리지 않게.
                with _log_lock:
                    more = len(_log_ring) >= LOG_BATCH_MAX
                wait = 1.0 if more else LOG_FLUSH_INTERVAL
            else:
                wait = LOG_FLUSH_INTERVAL     # 보낼 게 없거나 아직 키/pc_id 가 없음
        except Exception:
            wait = LOG_FLUSH_INTERVAL
        time.sleep(wait)


# ★핸들러 등록은 이 블록 맨 끝에서★ — 위 함수들이 전부 정의된 뒤여야 emit 이 안전하다.
logging.getLogger().addHandler(_RemoteLogHandler(level=logging.INFO))


# ==================================================
# PC ID 로드
# ==================================================
def load_pc_id() -> str:
    global pc_id, CONTROL_API_KEY, EDITION, MACRO_EXE, MACRO_EXE_BACKUP
    try:
        if os.path.exists(INFO_TXT):
            with open(INFO_TXT, 'r', encoding='utf-8') as f:
                lines = f.read().strip().splitlines()
            kv = {}
            for ln in lines:
                if '=' in ln:
                    k, v = ln.split('=', 1)
                    kv[k.strip()] = v.strip()
            pc_id = (kv.get('pc_id') or kv.get('pc_name')
                     or (lines[0].strip() if lines else 'PC-?'))
            log(f"[업데이터] PC ID: {pc_id}")
            # ★v3.0.7: 테넌트/렌탈 지원 — info.txt로 API키·에디션 오버라이드.
            #   control_api_key: 지인 테넌트 키(하드코딩 기본키는 main 테넌트 전용)
            #   edition=rental : 렌탈 채널(혼종_렌탈.exe)로 업데이트·실행★
            if kv.get('control_api_key'):
                CONTROL_API_KEY = kv['control_api_key']
                log("[업데이터] control_api_key 오버라이드 (info.txt)")
            if (kv.get('edition') or '').lower() == 'rental':
                EDITION = 'rental'
                MACRO_EXE = r"C:\auto\혼종_렌탈.exe"
                MACRO_EXE_BACKUP = r"C:\auto\혼종_렌탈.exe.bak"
                log("[업데이터] 에디션: rental (혼종_렌탈.exe 채널)")
    except Exception as e:
        err(f"[업데이터] info.txt 읽기 실패: {e}")
    return pc_id


# ==================================================
# 유틸
# ==================================================
def _headers() -> dict:
    return {"X-Api-Key": CONTROL_API_KEY}


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def load_local_version() -> dict:
    try:
        if os.path.exists(LOCAL_VERSION):
            with open(LOCAL_VERSION, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        err(f"[버전] 로드 실패: {e}")
    return {"exe_version": "0.0.0", "image_hashes": {}}


def save_local_version(data: dict):
    try:
        with open(LOCAL_VERSION, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        err(f"[버전] 저장 실패: {e}")


def get_local_image_hashes() -> dict:
    hashes = {}
    if not os.path.exists(IMAGES_DIR):
        return hashes
    for fname in os.listdir(IMAGES_DIR):
        fpath = os.path.join(IMAGES_DIR, fname)
        if os.path.isfile(fpath):
            try:
                hashes[fname] = sha256_file(fpath)
            except Exception as e:
                err(f"[해시] {fname} 실패: {e}")
    return hashes


def _safe_remove(path: str):
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def _download_once(url: str, tmp_path: str, dest_path: str, expected_sha256):
    """1회 시도. 성공 시 (True, bytes), 실패 시 (False, 사유문자열)."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    r = requests.get(url, stream=True, timeout=(TIMEOUT_CONNECT, TIMEOUT_DOWNLOAD))
    r.raise_for_status()
    written = 0
    with open(tmp_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                written += len(chunk)
    if expected_sha256 and sha256_file(tmp_path) != expected_sha256:
        return False, "해시 불일치"
    shutil.move(tmp_path, dest_path)
    os.utime(dest_path, None)
    return True, written


def _download_from_seed(url: str, dest_path: str, expected_sha256) -> bool:
    """★내부망 시드 1회 시도(v3.0.8)★ — 실패하면 빨리 GitHub로 폴백하는 게 목적이라 재시도 없음.
    SHA256은 서버 /check가 준 값으로 검증하므로 시드가 엉뚱한/변조된 파일을 줘도 여기서 기각된다."""
    tmp_path = dest_path + ".seed.tmp"
    try:
        r = requests.get(url, stream=True, timeout=(3, 180))
        r.raise_for_status()
        _d = os.path.dirname(dest_path)
        if _d:                       # 상대 경로면 dirname='' — makedirs('')는 WinError 3
            os.makedirs(_d, exist_ok=True)
        written = 0
        with open(tmp_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
        if expected_sha256 and sha256_file(tmp_path) != expected_sha256:
            err("[다운로드] 시드 해시 불일치 → 폐기, GitHub 폴백")
            _safe_remove(tmp_path)
            return False
        shutil.move(tmp_path, dest_path)
        os.utime(dest_path, None)
        log(f"[다운로드] ✓ 내부망 시드 수신: {os.path.basename(dest_path)} ({written // 1024}KB)")
        return True
    except Exception as e:
        log(f"[다운로드] 시드 실패({e}) → GitHub 폴백")
        _safe_remove(tmp_path)
        return False


def download_file(url: str, dest_path: str, expected_sha256: str = None) -> bool:
    """네트워크가 흔들려도 끝까지 받도록 재시도. 실패(순단/타임아웃/스트림끊김/해시불일치)마다
    tmp 정리 후 백오프 대기하고 처음부터 다시 받음. 대시보드 '재시작' 업데이트 안정화 핵심."""
    tmp_path = dest_path + ".tmp"
    last_reason = ""
    name = os.path.basename(dest_path)
    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        try:
            ok, info = _download_once(url, tmp_path, dest_path, expected_sha256)
            if ok:
                if attempt > 1:
                    log(f"[다운로드] {name} 재시도 성공 (시도 {attempt}/{DOWNLOAD_MAX_RETRIES}, {info//1024}KB)")
                return True
            last_reason = info   # 해시 불일치
        except requests.exceptions.ConnectionError:
            last_reason = "연결 실패"
        except requests.exceptions.Timeout:
            last_reason = "타임아웃"
        except requests.exceptions.ChunkedEncodingError:
            last_reason = "스트림 끊김"
        except Exception as e:
            last_reason = str(e)
        _safe_remove(tmp_path)
        err(f"[다운로드] {name} 실패 (시도 {attempt}/{DOWNLOAD_MAX_RETRIES}, {last_reason})")
        if attempt < DOWNLOAD_MAX_RETRIES:
            backoff = DOWNLOAD_RETRY_BACKOFF[min(attempt - 1, len(DOWNLOAD_RETRY_BACKOFF) - 1)]
            log(f"[다운로드] {backoff}초 후 재시도...")
            time.sleep(backoff)
    err(f"[다운로드] {name} 최종 실패 ({DOWNLOAD_MAX_RETRIES}회 시도, 마지막: {last_reason}): {url}")
    return False


# ==================================================
# 매크로 프로세스 관리
# ==================================================
def _set_state(state: str, expect: str = None) -> bool:
    """상태를 바꾼다. expect 를 주면 ★현재 값이 expect 일 때만★ 바꾼다(compare-and-set).

    ★왜 expect 가 필요한가 (2026-08-20)★
      크래시감지 스레드가 '되살아났네' 하고 running 을 쓰는 사이, 명령 스레드가
      stop/update 로 stopped·updating 을 쓸 수 있다(handle_command 는 별도 데몬 스레드).
      확인과 기록이 갈라져 있으면 늦게 쓴 쪽이 이겨서 ★죽은 매크로가 running 으로★
      남는다. 락 안에서 확인+기록을 붙여 그 창을 없앤다.
    """
    global macro_state
    with _state_lock:
        if expect is not None and macro_state != expect:
            return False
        macro_state = state
    log(f"[상태] macro_state → {state}")
    return True


def _minimize_consoles():
    """매크로 콘솔 + updater 콘솔 최소화"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        EnumWindows = user32.EnumWindows
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
        GetWindowTextW = user32.GetWindowTextW
        ShowWindow = user32.ShowWindow

        def callback(hwnd, lParam):
            title = ctypes.create_unicode_buffer(256)
            GetWindowTextW(hwnd, title, 256)
            t = title.value.lower()
            # 매크로 exe 콘솔 또는 updater 콘솔
            if '혼종' in title.value or 'updater' in t or '자동' in title.value:
                ShowWindow(hwnd, 6)  # SW_MINIMIZE
            return True

        EnumWindows(WNDENUMPROC(callback), 0)
        log("[포커스] 콘솔 창 최소화 완료")
    except Exception as e:
        log(f"[포커스] 콘솔 최소화 실패: {e}")


def _focus_game_window():
    """크롬 게임 창을 최상위로 올리기"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        EnumWindows = user32.EnumWindows
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
        GetWindowTextW = user32.GetWindowTextW
        SetForegroundWindow = user32.SetForegroundWindow
        ShowWindow = user32.ShowWindow
        BringWindowToTop = user32.BringWindowToTop

        IsIconic = user32.IsIconic

        found = [False]
        def callback(hwnd, lParam):
            title = ctypes.create_unicode_buffer(256)
            GetWindowTextW(hwnd, title, 256)
            t = title.value.lower()
            # PURPLE On-NCSOFT - Chrome 또는 purpleon 등
            if ('purple' in t or 'aion' in t or 'ncsoft' in t) and 'chrome' in t:
                # ★SW_RESTORE는 '최소화일 때만'(v3.0.6) — 전체화면 창에 쏘면 창모드로
                #   풀려버림(PC-18 실사고: 게임 전체화면 해제→창모드+스트리밍 끊김)★
                if IsIconic(hwnd):
                    ShowWindow(hwnd, 9)  # SW_RESTORE
                BringWindowToTop(hwnd)
                SetForegroundWindow(hwnd)
                log(f"[포커스] 게임 창 활성화: {title.value}")
                found[0] = True
                return False
            return True

        EnumWindows(WNDENUMPROC(callback), 0)

        # 못 찾으면 크롬 아무 창이라도
        if not found[0]:
            def callback2(hwnd, lParam):
                title = ctypes.create_unicode_buffer(256)
                GetWindowTextW(hwnd, title, 256)
                t = title.value.lower()
                if 'chrome' in t and user32.IsWindowVisible(hwnd):
                    if IsIconic(hwnd):
                        ShowWindow(hwnd, 9)
                    BringWindowToTop(hwnd)
                    SetForegroundWindow(hwnd)
                    log(f"[포커스] 크롬 창 활성화: {title.value}")
                    return False
                return True
            EnumWindows(WNDENUMPROC(callback2), 0)
    except Exception as e:
        log(f"[포커스] 게임 창 활성화 실패 (무시): {e}")


def _macro_running_anywhere() -> bool:
    """★시스템 전체에서 매크로 프로세스 존재 검사(v3.0.9, 사고 38-b)★ — macro_proc 핸들은
    '내가 띄운 자식'만 기억한다. 매크로의 자가치유가 업데이터를 재기동하면 새 업데이터는
    핸들이 비어 있어 이미 떠 있는 매크로를 몰라보고 하나 더 띄웠다(이중 실행 실사고)."""
    base = os.path.basename(MACRO_EXE).lower()
    try:
        import psutil
        for p in psutil.process_iter(['name']):
            try:
                if (p.info['name'] or "").lower() == base:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def start_macro() -> bool:
    """★★매크로를 켠다 — 켜는 순간 「되살리지 마라」 표시는 끝난 이야기다 (2026-08-29)★★

    ★실사고 PC-19★ 업데이트가 `stop_macro` 로 표시를 남겼는데 그 표시가 안 지워져서,
    나중에 매크로가 죽었을 때 크래시 감시가
      `[되살림] no_restart 표시 있음 — 사용자가 끈 것이므로 두고 본다`
    로 손을 놓았다. ★그 PC 는 그대로 멈춰 있었다.★
    표시는 매크로가 부팅 때 지우기로 돼 있는데, ★매크로가 안 뜨면 영영 남는다★ —
    지우는 책임을 「뜨는 쪽」에 뒀는데 정작 안 뜨는 게 문제였다.
    → ★일부러 켜는 여기서 지운다.★ 여기까지 왔다는 건 '사용자가 끈 상태' 가 아니다.
    """
    global macro_proc
    with _state_lock:
        if macro_proc is not None and macro_proc.poll() is None:
            log("[매크로] 이미 실행 중")
            return True
    if _macro_running_anywhere():
        log("[매크로] 이미 실행 중 (외부 기동 감지) → 중복 기동 생략")
        _set_state("running")     # 크래시감지는 macro_proc이 None이라 개입 안 함 — 표시만 유지
        return True
    if not os.path.exists(MACRO_EXE):
        err(f"[매크로] EXE 없음: {MACRO_EXE}")
        return False
    try:
        proc = subprocess.Popen(
            [MACRO_EXE],
            cwd=os.path.dirname(MACRO_EXE),
            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == 'win32' else 0
        )
        with _state_lock:
            macro_proc = proc
        # ★★표식은 ★띄운 뒤★ 지운다 (적대검토 2차)★★
        #   초입에서 지우면 ①아무것도 안 띄우고 나가는 경로 넷(이미 실행 중 · 외부 기동 감지 ·
        #   EXE 없음 · Popen 실패)에서도 지워져 「표식이 없다 = 지금 켜졌다」가 거짓이 되고,
        #   ②restart(stop→2초→start)가 매크로 부활 예약(powershell 이 죽음+3초에 검사)과
        #   ★경합★ 해 매크로가 2개 뜰 수 있다.
        #   여기까지 왔다는 건 ★실제로 새 프로세스를 띄웠다★ 는 뜻이다.
        #   ★기동에 실패하면 표식이 남는다★ — PC-19 가 겨냥한 그 안전판은 그대로다.
        try:
            if os.path.exists(NO_RESTART_PATH):
                os.remove(NO_RESTART_PATH)
                log("[매크로] 되살림 금지 표시 제거 (방금 실제로 띄웠다)")
        except Exception as e:
            err(f"[매크로] 되살림 금지 표시 제거 실패(무시): {e}")
        _set_state("running")
        log(f"[매크로] 시작 완료 PID={proc.pid}")
        # 콘솔 최소화 → 게임 화면 클릭
        # ★v3.0.4: 기존 '포그라운드 창 2연속 최소화'(1=매크로콘솔, 2=updater콘솔 가정)는
        #   매크로 콘솔을 내린 순간 포그라운드가 된 게임 창을 두 번째로 내려버리는 오발이
        #   있었음(07-24 05시 실사고: 다수 PC 게임창 최소화 → 매크로 클릭 전부 바탕화면행).
        #   제목 기반 정밀 최소화(_minimize_consoles) + 게임 창 복원(_focus_game_window)로 교체.★
        time.sleep(2.0)
        try:
            _minimize_consoles()
            time.sleep(0.5)
            _focus_game_window()
            time.sleep(0.5)
            import ctypes
            user32 = ctypes.windll.user32
            # 게임 화면 클릭 (포커스 확실화)
            user32.SetCursorPos(640, 360)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.05)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            log("[매크로] 콘솔 정밀 최소화 → 게임 창 복원 → 클릭 (640,360)")
        except Exception:
            pass
        return True
    except Exception as e:
        err(f"[매크로] 시작 실패: {e}")
        _set_state("crashed")
        return False


def stop_macro():
    """매크로를 죽인다. ★죽이기 전에 '되살리지 마라' 표시를 남긴다 (3.1.5)★

    ★왜 (2026-08-17 리뷰 M3)★
      매크로가 계정 순회 중이면 powershell 부활 예약(Wait-Process→Start-Process)이
      걸려 있다. 그 상태에서 [업데이트]를 누르면 stop_macro 가 taskkill 한 뒤
      ★3초 뒤 구버전이 되살아나 exe 파일을 잠근다★ → 새 exe 로 덮어쓰기 실패 →
      이어지는 start_macro 는 '이미 실행 중' 으로 건너뛴다 = 업데이트가 조용히 실패.
      표시를 먼저 남기면 powershell 쪽이 되살리기를 스스로 포기한다.
      (표시는 매크로가 다음 부팅 때 지운다 — 일회용)
    """
    global macro_proc
    try:
        with open(NO_RESTART_PATH, "w", encoding="utf-8") as f:
            f.write("updater stop_macro")
        log("[매크로] 되살림 금지 표시 남김 (업데이터가 의도적으로 끄는 중)")
    except Exception as e:
        err(f"[매크로] 되살림 금지 표시 실패(무시): {e}")
    with _state_lock:
        proc = macro_proc
        macro_proc = None
    if proc is not None and proc.poll() is None:
        pid = proc.pid
        log(f"[매크로] PID {pid} 종료 시도")
        try:
            subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                           capture_output=True, timeout=10)
            proc.wait(timeout=5)
            log(f"[매크로] PID {pid} 종료 완료")
        except Exception as e:
            err(f"[매크로] PID {pid} 종료 실패: {e}")
    # 혹시 남아있는 프로세스도 PID로 찾아서 kill
    try:
        import psutil
        for p in psutil.process_iter(['pid', 'exe']):
            try:
                if p.info['exe'] and os.path.basename(p.info['exe']) == os.path.basename(MACRO_EXE):
                    log(f"[매크로] 잔여 프로세스 PID {p.pid} 강제 종료")
                    p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except ImportError:
        # psutil 없으면 taskkill /F /IM 시도 (한글 인코딩 문제 가능)
        try:
            subprocess.run(['taskkill', '/F', '/IM', os.path.basename(MACRO_EXE)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    _set_state("stopped")


# ==================================================
# 업데이트 로직
# ==================================================
def self_update(updater_info: dict):
    """자가업데이트: 다운로드 → 자신을 rename → 새 파일을 updater.exe로 → 실행"""
    new_ver = updater_info["version"]
    url     = updater_info["download_url"]
    sha256  = updater_info.get("sha256")
    log(f"[자가업데이트] updater {UPDATER_VERSION} → {new_ver} 다운로드 중...")

    current_exe = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
    exe_dir = os.path.dirname(current_exe)
    new_tmp = os.path.join(exe_dir, "updater_new.exe")
    old_bak = os.path.join(exe_dir, "updater_old.exe")
    target  = os.path.join(exe_dir, "updater.exe")

    ok = download_file(url, new_tmp, sha256)
    if not ok:
        err("[자가업데이트] 다운로드 실패 — 기존 버전 유지")
        return

    launched = False
    try:
        # 이전 백업 삭제
        if os.path.exists(old_bak):
            try: os.remove(old_bak)
            except: pass

        # 현재 실행 중인 exe → old로 rename (Windows에서 실행 중 rename 가능)
        if os.path.exists(current_exe) and os.path.abspath(current_exe) == os.path.abspath(target):
            os.rename(current_exe, old_bak)
            log("[자가업데이트] 현재 exe → updater_old.exe")

        # 새 파일 → updater.exe
        os.rename(new_tmp, target)
        log("[자가업데이트] updater_new.exe → updater.exe")

        # ★로그를 먼저 비운다 — 순서가 핵심 (3.1.6)★
        #   새 인스턴스는 부팅하며 _kill_stale_updater_processes() 로 ★나를 taskkill★ 한다.
        #   그러니 네트워크 전송은 반드시 Popen ★앞★에서 끝나야 한다. 뒤에 두면
        #   "자가업데이트 하다 죽었다" 는 제일 중요한 로그가 전송 도중에 잘린다.
        _log_shutdown(f"자가업데이트 {UPDATER_VERSION} → {new_ver}")

        # 새 updater.exe 실행
        subprocess.Popen(
            [target],
            cwd=exe_dir,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        launched = True
        log(f"[자가업데이트] 새 버전 실행 완료 → 자신 종료")
        # Popen 뒤에 찍힌 줄(위 한 줄 + 그 사이 다른 스레드가 찍은 것)은 전송할 시간이
        # 없다 — 디스크에만 남겨 새 인스턴스가 이어서 보내게 한다.
        _spool_dump_and_clear()
        time.sleep(1)
        os._exit(0)
    except Exception as e:
        err(f"[자가업데이트] 실패: {e}")
        # ★새 인스턴스가 이미 떠 있으면 내가 남는 순간 이중 실행(명령 나눠먹기/파일 잠금
        #   꼬임) — 무조건 종료(v3.0.5). (새 쪽 부팅 시 잔여 프로세스 강제 정리도 있음)★
        if launched:
            # 여기 온 예외 메시지가 곧 '왜 자가업데이트가 삐끗했는지' 다 — 디스크에 남겨
            # 새 인스턴스가 이어서 올리게 한다(전송할 시간은 없다).
            _spool_dump_and_clear()
            os._exit(0)
        # 복구 시도
        try:
            if not os.path.exists(target) and os.path.exists(old_bak):
                os.rename(old_bak, target)
                log("[자가업데이트] 복구 완료")
        except: pass
        try: os.remove(new_tmp)
        except: pass


# ★★마지막 업데이트가 ★진짜로★ 됐는지 (2026-08-29)★★
#   예전엔 exe 다운로드가 실패해도 이미지 한 장만 성공하면 「완료!」 를 찍고
#   옛 버전으로 매크로를 켰다. 호출부는 그걸 성공으로 읽었다(사고 220 부류).
#   이제 여기에 사실을 남기고, update 명령이 그걸 보고 다시 받는다.
#   ★exe_ok 의 기본은 ★None(모름)★ 이다 — True(성공) 가 아니다.★
#   적대검토(2026-08-29)가 잡았다: 서버 연결 실패는 조기 return 이라 여기를 안 건드리는데,
#   기본이 True 면 「연결도 못 했다」가 ★성공★ 으로 읽혀 재시도도 재시작도 안 했다.
#   ★모르는 것을 성공으로 세지 않는다★ — 본 것만 채운다.
_LAST_UPD = {"exe_target": None, "exe_ok": None, "img_fail": 0}


def check_and_update() -> bool:
    """서버 버전 체크 후 필요시 업데이트. True = 업데이트 있었음.

    ★exe 가 실제로 갱신됐는지는 반환값이 아니라 `_LAST_UPD` 를 봐야 한다★ —
    반환값은 「뭐라도 바뀌었나」라서 이미지만 받아도 True 다.
    """
    _LAST_UPD.update({"exe_target": None, "exe_ok": None, "img_fail": 0})
    log("[업데이트] 체크 시작")
    _set_state("updating")
    local = load_local_version()
    local_image_hashes = get_local_image_hashes()

    try:
        resp = requests.post(
            f"{UPDATE_SERVER}/check",
            json={
                "exe_version":     local.get("exe_version", "0.0.0"),
                "image_hashes":    local_image_hashes,
                "updater_version": UPDATER_VERSION,
                "edition":         EDITION,   # v3.0.7: rental이면 서버가 렌탈 채널 exe 응답
            },
            # ★v3.1.0: 키를 동봉한다★ — 서버가 '자칭 edition'이 아니라 ★키★로 채널을 정한다.
            #   (렌탈 이용자가 info.txt에서 edition 줄만 지워 킬스위치 없는 본판 exe를 받아가던
            #    구멍 차단, 2026-08-06 감사 critical). 내부망 시드도 키로 main이 확인될 때만 온다.
            #   키가 없으면 빈 헤더 → 서버는 구버전과 동일하게 처리(하위호환).
            headers=_headers(),
            timeout=(TIMEOUT_CONNECT, 15),
        )
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        # ★★적대검토 치명 ①★★ 예전엔 여기서 _LAST_UPD 를 안 건드리고 나갔다.
        #   기본이 True 였으므로 「서버에 연결도 못 했다」가 ★성공★ 으로 읽혔다.
        _LAST_UPD["exe_ok"] = False
        err(f"[업데이트] 서버 연결 실패: {e} — ★확인 못 했다(성공 아님)★")
        _set_state("stopped")
        return False

    # ── updater 자가 업데이트 (최우선) ─────────────────────────────────────
    updater_info = result.get("updater_update")
    if updater_info:
        self_update(updater_info)
        # 성공 시 sys.exit() / 실패 시 계속 진행

    any_update = False

    # ── exe 업데이트 ────────────────────────────────────────────────────────
    exe_info = result.get("exe_update")
    if exe_info:
        new_ver   = exe_info["version"]
        local_ver = local.get("exe_version", "없음")
        log(f"[업데이트] 매크로 exe: {local_ver} → {new_ver}")
        if os.path.exists(MACRO_EXE):
            try:
                shutil.copy2(MACRO_EXE, MACRO_EXE_BACKUP)
            except Exception as e:
                err(f"[업데이트] 백업 실패: {e}")
        # ★내부망 시드 우선(v3.0.8)★ — 서버가 lan_seed를 주면(렌탈 제외) 그 주소에서 먼저 받는다.
        #   해시 검증 동일 + 어떤 실패든 GitHub 폴백이라 최악의 경우 = 기존과 동일.
        ok = False
        seed = str(result.get("lan_seed") or "").rstrip("/")   # 타입 방어(리뷰): 비문자열이 와도 안전
        # ★sha256이 없으면 시드 경로를 아예 안 탄다(리뷰 보강)★ — 시드는 평문 HTTP+LAN이라
        #   해시 검증이 유일한 무결성 보장. 검증 불가면 HTTPS(GitHub)로만 받는다.
        if seed and exe_info.get("sha256"):
            asset = exe_info["download_url"].rsplit("/", 1)[-1]
            log(f"[업데이트] 내부망 시드 시도: {seed}/{asset}")
            ok = _download_from_seed(f"{seed}/{asset}", MACRO_EXE, exe_info.get("sha256"))
        if not ok:
            ok = download_file(exe_info["download_url"], MACRO_EXE, exe_info.get("sha256"))
        _LAST_UPD["exe_target"] = new_ver
        if ok:
            local["exe_version"] = new_ver
            any_update = True
            _LAST_UPD["exe_ok"] = True
            log(f"[업데이트] ✓ 매크로 exe v{new_ver} 완료")
        else:
            _LAST_UPD["exe_ok"] = False
            err(f"[업데이트] ✗ 매크로 exe 다운로드 실패")
            if os.path.exists(MACRO_EXE_BACKUP):
                try:
                    shutil.copy2(MACRO_EXE_BACKUP, MACRO_EXE)
                    log("[업데이트] 백업으로 복구 완료")
                except Exception as e:
                    err(f"[업데이트] 복구 실패: {e}")
    else:
        _LAST_UPD["exe_ok"] = True          # 받을 게 없다 = 최신이다(확인됨)
        log(f"[업데이트] ✓ 매크로 exe 최신 (v{local.get('exe_version', '?')})")

    # ── 이미지 업데이트 ─────────────────────────────────────────────────────
    images_to_update = result.get("images_update", [])
    if images_to_update:
        os.makedirs(IMAGES_DIR, exist_ok=True)
        ok_cnt = fail_cnt = 0
        for img in images_to_update:
            fname = img["filename"]
            dest  = os.path.join(IMAGES_DIR, fname)
            ok = download_file(img["download_url"], dest, img.get("sha256"))
            if ok:
                local_image_hashes[fname] = img["sha256"]
                any_update = True
                ok_cnt += 1
                log(f"[업데이트] ✓ 이미지: {fname}")
            else:
                fail_cnt += 1
                err(f"[업데이트] ✗ 이미지 실패: {fname}")
        _LAST_UPD["img_fail"] = fail_cnt
        log(f"[업데이트] 이미지 완료 — 성공 {ok_cnt} / 실패 {fail_cnt}")
    else:
        log(f"[업데이트] ✓ 이미지 최신 ({len(local_image_hashes)}개)")

    local["image_hashes"] = local_image_hashes
    local["last_check"]   = time.strftime('%Y-%m-%d %H:%M:%S')
    save_local_version(local)
    # ★★거짓 「완료!」 금지 (2026-08-29)★★ 예전엔 exe 가 실패해도 이미지 한 장만
    #   받으면 「완료!」 였다. 화면에는 「눌렀는데 그대로」로만 보여서 주인님이 직접
    #   가보셔야 알았다. ★안 된 건 안 됐다고 찍는다.★
    if _LAST_UPD["exe_ok"] is not True:
        err(f"[업데이트] ★실패★ — 매크로 exe v{_LAST_UPD['exe_target']} 를 못 받았다 "
            f"(옛 버전으로 계속한다). 이미지 실패 {_LAST_UPD['img_fail']}장")
    elif _LAST_UPD["img_fail"]:
        err(f"[업데이트] 일부 실패 — 이미지 {_LAST_UPD['img_fail']}장 못 받았다")
    else:
        log("[업데이트] 완료!" if any_update else "[업데이트] 모든 항목 최신")
    _set_state("stopped")
    return any_update


# ==================================================
# 스크린샷 핫키
# info.txt 에 screenshot_key=f12 이렇게 설정 가능
# 기본값: ctrl+q
# 지원 형식: ctrl+q / ctrl+f12 / f9 / f10 / f11 / f12 / pause 등
# ==================================================

# VK 코드 테이블
_VK_MAP = {
    'f1':0x70,'f2':0x71,'f3':0x72,'f4':0x73,'f5':0x74,'f6':0x75,
    'f7':0x76,'f8':0x77,'f9':0x78,'f10':0x79,'f11':0x7A,'f12':0x7B,
    'pause':0x13,'scroll':0x91,'insert':0x2D,'home':0x24,
    'a':0x41,'b':0x42,'c':0x43,'d':0x44,'e':0x45,'f':0x46,'g':0x47,
    'h':0x48,'i':0x49,'j':0x4A,'k':0x4B,'l':0x4C,'m':0x4D,'n':0x4E,
    'o':0x4F,'p':0x50,'q':0x51,'r':0x52,'s':0x53,'t':0x54,'u':0x55,
    'v':0x56,'w':0x57,'x':0x58,'y':0x59,'z':0x5A,
}

def _parse_hotkey(key_str: str):
    """'ctrl+f12' → (MOD, VK) / 'f12' → (0, VK)"""
    MOD_ALT     = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT   = 0x0004
    parts = [p.strip().lower() for p in key_str.split('+')]
    mod = 0
    vk  = 0
    for p in parts:
        if p == 'ctrl':  mod |= MOD_CONTROL
        elif p == 'alt': mod |= MOD_ALT
        elif p == 'shift': mod |= MOD_SHIFT
        else:
            vk = _VK_MAP.get(p, 0)
    return mod, vk


def _read_screenshot_key() -> str:
    """info.txt 의 screenshot_key= 값 읽기. 없으면 ctrl+q"""
    try:
        if os.path.exists(INFO_TXT):
            with open(INFO_TXT, 'r', encoding='utf-8') as f:
                for ln in f:
                    if ln.strip().startswith('screenshot_key='):
                        return ln.split('=', 1)[1].strip()
    except Exception:
        pass
    return 'ctrl+q'


def take_bug_screenshot(immediate_upload=False):
    """전체화면 캡처 후 bugs 폴더에 저장. immediate_upload=True면 즉시 서버 업로드."""
    try:
        os.makedirs(BUGS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{pc_id}_{ts}_bug_{ts}.png"
        dest = os.path.join(BUGS_DIR, filename)
        img = ImageGrab.grab()
        img.save(dest)
        log(f"[스크린샷] ✓ 저장: {dest}")

        if immediate_upload:
            try:
                with open(dest, 'rb') as fp:
                    r = requests.post(
                        f"{CONTROL_SERVER}/bugs/{pc_id}",
                        files={"file": (filename, fp, "image/png")},
                        headers=_headers(),
                        timeout=(TIMEOUT_CONNECT, 30),
                    )
                if r.ok:
                    os.remove(dest)
                    log(f"[스크린샷] ✓ 즉시 업로드 완료")
                else:
                    log(f"[스크린샷] 즉시 업로드 실패: {r.status_code} (다음 주기에 재시도)")
            except Exception as e:
                err(f"[스크린샷] 즉시 업로드 실패: {e} (다음 주기에 재시도)")
    except Exception as e:
        err(f"[스크린샷] 실패: {e}")


def _hotkey_thread():
    """RegisterHotKey — RDP 풀스크린 / DirectX 풀스크린에서도 동작"""
    user32   = ctypes.windll.user32
    HOTKEY_ID = 9001
    WM_HOTKEY = 0x0312

    key_str = _read_screenshot_key()
    mod, vk = _parse_hotkey(key_str)
    if not vk:
        err(f"[단축키] 알 수 없는 키: {key_str} → 스크린샷 단축키 비활성화")
        return

    if not user32.RegisterHotKey(None, HOTKEY_ID, mod, vk):
        err(f"[단축키] RegisterHotKey 실패 ({key_str}) — 다른 프로그램이 점유 중일 수 있음")
        return
    log(f"[단축키] {key_str.upper()} → 버그 스크린샷 등록")

    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                threading.Thread(target=take_bug_screenshot, daemon=True).start()
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)


# ==================================================
# 명령 처리
# ==================================================
def _pc_slot(mod: int) -> int:
    """pc_id 의 숫자로 0..mod-1 자리를 준다 — ★24대가 동시에 안 받게 흩뜨리는 데 쓴다★."""
    try:
        import re as _re
        m = _re.search(r"(\d+)", str(pc_id or ""))
        return (int(m.group(1)) % mod) if m else 0
    except Exception:
        return 0


def _restart_self(why: str) -> bool:
    """★업데이터 자신을 다시 띄운다 (주인님 제안, 2026-08-29)★

    부팅 경로는 `check_and_update()` 를 무조건 한 번 돌기 때문에, 다운로드가
    계속 실패할 때 ★가장 확실한 재시도★ 다(주인님이 손으로 하시던 그 동작).

    ★★새 프로세스가 살아 있는 걸 보고 나서만 나간다★★ — 안 그러면 그 PC 는
    업데이터가 통째로 사라져서 ★사람이 직접 가야만★ 살아난다. 그건 지금 증상보다
    훨씬 나쁘다. 못 띄우면 그냥 계속 산다(다음 명령을 기다린다).
    """
    exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    if not os.path.exists(exe):
        err(f"[업데이터] 자기 재시작 불가 — 실행 파일이 없다: {exe}")
        return False
    try:
        _log_shutdown(f"자기 재시작: {why}")   # ★Popen 앞에서 남긴다★ — 뒤에 두면
    except Exception:                          #   자식이 부모를 죽일 때 사유가 통째로 사라진다
        pass
    try:
        args = [exe] if getattr(sys, "frozen", False) else [sys.executable, exe]
        # ★★자식이 ★부모를 죽이지 않게★ 한다 (적대검토 치명 ②)★★
        #   자식 main() 첫 줄의 `_kill_stale_updater_processes()` 는 다른 updater.exe 를
        #   전부 죽인다 — ★5초 자고 있는 이 부모가 그 대상★ 이다. 그러면 아래
        #   「살아 있는지 확인」이 아예 실행되지 않고, 자식이 곧 죽으면 그 PC 엔
        #   업데이터가 ★하나도 없다.★ 부모는 어차피 스스로 나가므로 정리는 불필요하다.
        _env = dict(os.environ)
        _env["AION2_UPD_RESTART"] = "1"
        proc = subprocess.Popen(
            args, cwd=os.path.dirname(exe), env=_env,
            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0)
    except Exception as e:
        err(f"[업데이터] 자기 재시작 실패 — 계속 산다: {e}")
        return False
    time.sleep(5.0)
    if proc.poll() is not None:
        err(f"[업데이터] 새 업데이터가 5초 만에 죽었다(rc={proc.poll()}) — "
            f"★나가지 않는다★ (나가면 이 PC 에 업데이터가 없어진다)")
        return False
    log(f"[업데이터] ★자기 재시작★ — {why} (새 PID={proc.pid}) → 이 프로세스는 나간다")
    time.sleep(1.5)
    os._exit(0)


# ★★업데이트는 한 번에 하나만 (적대검토 2차, 배포차단)★★
#   명령은 수신할 때마다 ★새 데몬 스레드★ 다(_poll_thread). 그런데 _LAST_UPD 는 락 없는
#   전역이고, _update_with_retry 는 이제 수 분짜리이며 그 안에 os._exit 가 있다.
#   겹치는 경로가 둘 다 코드로 있다 — ①대시보드를 두 번 누르면 큐에 두 행
#   ②ack POST 가 실패하면(except: pass) 같은 명령이 10초마다 재배달된다.
#   겹치면 서로의 판정을 덮고, 한쪽이 74MB 를 쓰는 도중 다른 쪽이 프로세스를 죽인다.
_upd_busy = threading.Lock()


def _update_with_retry(allow_restart: bool = True) -> bool:
    """★[업데이트] 명령 전용 — 안 되면 벌려서 다시, 그래도 안 되면 재시작★

    ★왜 (2026-08-29 20:07 함대 실측)★ 24대가 ★동시에 74MB★ 를 받는다.
    내부망 시드 서버 한 대가 23대를 먹이다 전송이 끊기고(IncompleteRead 32건),
    GitHub 폴백도 같은 순간에 몰려 실패한다. 그런데 예전 코드는 4번 연속 실패하면
    ★그대로 포기하고 「완료!」 를 찍은 뒤 옛 버전으로 매크로를 켰다.★

    그래서 ①번호로 흩뜨리고 ②실패하면 한참 벌려서 다시 받고 ③그래도 안 되면
    ★부팅 경로(자기 재시작)★ 로 넘긴다. 성공하면 즉시 빠져나온다.
    """
    if not _upd_busy.acquire(blocking=False):
        log("[업데이트] ★이미 업데이트가 도는 중이다 — 이번 명령은 그냥 돌려보낸다★ "
            "(겹치면 서로의 판정을 덮고 다운로드 중에 프로세스가 죽는다)")
        return False
    try:
        return _update_once(allow_restart)
    finally:
        _upd_busy.release()


def _update_once(allow_restart: bool = True) -> bool:
    delay = _pc_slot(6) * 7          # 0~35초 — 동시 출발을 깬다
    if delay:
        log(f"[업데이트] 동시 다운로드를 피해 {delay}초 기다렸다 받는다 "
            f"(24대가 한꺼번에 받으면 시드가 끊긴다)")
        time.sleep(delay)
    for _try in (1, 2):
        try:
            check_and_update()
        except Exception as e:
            _LAST_UPD["exe_ok"] = False       # 예외 = 확인 못 했다(성공 아님)
            err(f"[업데이트] 체크 예외({_try}차): {e}")
        if _LAST_UPD["exe_ok"] is True and not _LAST_UPD["img_fail"]:
            return True
        what = ("매크로 exe v%s" % _LAST_UPD["exe_target"]) \
            if _LAST_UPD["exe_ok"] is not True else ("이미지 %d장" % _LAST_UPD["img_fail"])
        if _try == 1:
            wait = 25 + _pc_slot(9) * 5      # 25~65초 — 몰린 것이 빠지길 기다린다
            log(f"[업데이트] ★{what} 를 못 받았다 — {wait}초 뒤 다시 받아 본다★ "
                f"(포기하지 않는다)")
            time.sleep(wait)
    if _LAST_UPD["exe_ok"] is not True and allow_restart:
        # ★여기까지 왔으면 두 번 다 실패했다 — 주인님이 손으로 하시던 그 방법을 쓴다★
        _restart_self(f"매크로 exe v{_LAST_UPD['exe_target']} 를 두 번 다 못 받았다")
        # _restart_self 가 나가지 않았다면(새 프로세스가 못 떴다) 여기로 온다
        err("[업데이트] ★재시작도 못 했다 — 옛 버전 그대로 둔다★ (사람 확인 필요)")
    return False


def handle_command(cmd: dict):
    command = cmd.get("command", "")
    log(f"[명령] 수신: {command}")

    if command == "start":
        start_macro()

    elif command == "stop":
        stop_macro()

    elif command == "restart":
        stop_macro()
        time.sleep(2.0)
        start_macro()

    elif command == "update":
        stop_macro()
        time.sleep(1.0)
        _update_with_retry()          # ★실패하면 벌려서 다시 → 그래도 안 되면 자기 재시작★
        time.sleep(1.0)
        start_macro()

    elif command == "update_only":
        stop_macro()
        time.sleep(1.0)
        # ★재시작하지 않는다 (적대검토 ③)★ — 재시작하면 자식이 부팅 끝에 start_macro 를
        #   불러서 ★「업데이트만」 인데 매크로가 켜진다.★ 이름과 정반대가 된다.
        _update_with_retry(allow_restart=False)

    elif command == "screenshot":
        threading.Thread(target=take_bug_screenshot, args=(True,), daemon=True).start()

    elif command == "exit":
        log("[명령] 업데이터 종료")
        stop_macro()
        # ★누가 껐는지 남긴다★ — exit 로 사라진 업데이터는 상태 보고도 끊기므로,
        #   서버에는 "그냥 응답 없음" 으로만 보인다. 종료 사유를 미리 밀어넣어야
        #   나중에 "죽은 건가 끈 건가" 를 구분할 수 있다.
        _log_shutdown("exit 명령 수신")
        time.sleep(1.0)
        os._exit(0)

    else:
        log(f"[명령] 알 수 없는 명령: {command}")


# ==================================================
# 스레드: 명령 폴링 (10s)
# ==================================================
def _poll_thread():
    log("[폴링] 시작")
    _consecutive_errors = 0
    _session = requests.Session()
    _session.headers.update(_headers())
    while True:
        try:
            r = _session.get(
                f"{CONTROL_SERVER}/updater/command/{pc_id}",
                timeout=(TIMEOUT_CONNECT, 10),
            )
            if r.status_code == 200:
                _consecutive_errors = 0
                data = r.json()
                if data.get("command"):
                    cmd_id = data.get("id")
                    try:
                        _session.post(
                            f"{CONTROL_SERVER}/updater/command/{pc_id}/ack/{cmd_id}",
                            timeout=(TIMEOUT_CONNECT, 5),
                        )
                    except Exception:
                        pass
                    threading.Thread(target=handle_command, args=(data,), daemon=True).start()
            else:
                _consecutive_errors += 1
        except Exception as e:
            _consecutive_errors += 1
            if _consecutive_errors <= 3 or _consecutive_errors % 10 == 0:
                err(f"[폴링] 에러 ({_consecutive_errors}회): {e}")
            # 연속 에러 5회 → 세션 재생성
            if _consecutive_errors >= 5:
                log(f"[폴링] 연속 에러 {_consecutive_errors}회 → 세션 재생성")
                try:
                    _session.close()
                except Exception:
                    pass
                _session = requests.Session()
                _session.headers.update(_headers())
                _consecutive_errors = 0
                time.sleep(5)
                continue
        time.sleep(POLL_INTERVAL)


# ==================================================
# 스레드: 상태 보고 (30s)
# ==================================================
def _status_thread():
    log("[상태보고] 시작")
    _sess = requests.Session()
    _sess.headers.update(_headers())
    _errs = 0
    while True:
        try:
            with _state_lock:
                state = macro_state
                pid = macro_proc.pid if macro_proc and macro_proc.poll() is None else None
            _token = ""
            try:
                if os.path.exists(INFO_TXT):
                    with open(INFO_TXT, 'r', encoding='utf-8') as _f:
                        for _ln in _f:
                            if _ln.strip().startswith("token="):
                                _token = _ln.split("=", 1)[1].strip()
            except Exception:
                pass
            _setup_ok = (pc_id not in ("PC-??", "PC-?", "") and _token != "")
            r = _sess.post(
                f"{CONTROL_SERVER}/updater/status/{pc_id}",
                json={
                    "pc_id": pc_id,
                    "macro_state": state,
                    "macro_pid": pid,
                    "updater_version": UPDATER_VERSION,
                    "setup_complete": _setup_ok,
                },
                timeout=(TIMEOUT_CONNECT, 5),
            )
            if r.status_code == 200:
                _errs = 0
            else:
                _errs += 1
        except Exception as e:
            _errs += 1
            if _errs <= 3 or _errs % 10 == 0:
                err(f"[상태보고] 에러 ({_errs}회): {e}")
            if _errs >= 5:
                log("[상태보고] 세션 재생성")
                try: _sess.close()
                except: pass
                _sess = requests.Session()
                _sess.headers.update(_headers())
                _errs = 0
        time.sleep(STATUS_INTERVAL)


# ==================================================
# 스레드: 크래시 감지 (5s)
# ==================================================
# ★★죽은 매크로를 되살린다 (3.1.4, 2026-08-17)★★
#
# 사용자: "아니 왜 매크로가 크래쉬되는거야" / "그냥 프로그램이 전체 4계정을 관리한다는
#          느낌으로 가야지"
#
# ★그동안 왜 안 돌아왔나★
#   여기(크래시감지)는 죽은 걸 ★알아채고 _set_state("crashed") 로 표시만★ 했다.
#   되살리는 코드가 아예 없었다. 매크로 쪽 재기동 예약(powershell Wait-Process)에만
#   의존했는데, 그건 ★계정 전환이 끝까지 성공한 경우에만★ 걸린다.
#   그래서 계정 순회 도중 매크로가 종료되면 함대에서 그 PC 가 통째로 빠졌다
#   (실측 2026-08-17: 17:32:31 '프로그램 종료' 이후 12분+ 무응답).
#
#   ★계정 전환은 '종료'가 정상 경로다★ — 계정을 바꾸면 pc_id 가 바뀌어 프로세스를
#   갈아끼우는 설계다(정체성 불변 원칙). 즉 이 종료는 사고가 아니라 일상이다.
#   그렇다면 되살리는 주체가 반드시 있어야 하고, 그건 ★항상 살아 있는 업데이터★다.
#
# ★사용자가 일부러 끈 것은 되살리지 않는다★
#   매크로가 종료 명령(exit)/PageDown 으로 끝날 때 C:\auto\no_restart 를 남긴다.
#   그게 있으면 여기서 손대지 않는다. 없으면 되살린다.
#   (매크로는 부팅 때 이 파일을 지운다 — 한 번 쓰고 마는 일회용 표시)
#
# ★폭주 방지★ 부팅 직후 바로 죽는 매크로를 무한히 띄우면 PC 가 마비된다.
#   1시간에 REVIVE_MAX 회까지만.
NO_RESTART_PATH = r"C:\auto\no_restart"
REVIVE_MAX      = 8       # 1시간당 최대 되살림 횟수
REVIVE_WINDOW   = 3600.0
REVIVE_DELAY    = 4.0     # 싱글턴 뮤텍스 해제 여유 (매크로 쪽 예약과 같은 취지)
_revive_times: list = []


def _auto_revive(ret):
    """매크로가 사라졌다 → 되살린다. (사용자가 끈 것/폭주는 제외)"""
    try:
        if os.path.exists(NO_RESTART_PATH):
            log("[되살림] no_restart 표시 있음 — 사용자가 끈 것이므로 두고 본다")
            # ★일부러 끈 것은 '크래시'가 아니다 (2026-08-20)★
            #   여기서 crashed 를 그대로 두면 대시보드가 영영 빨간 crashed 를 띄운다.
            #   의도적 종료의 정확한 이름은 stopped 다.
            _set_state("stopped", expect="crashed")
            return
    except Exception:
        pass
    now = time.time()
    _revive_times[:] = [t for t in _revive_times if now - t < REVIVE_WINDOW]
    if len(_revive_times) >= REVIVE_MAX:
        log(f"[되살림] 1시간에 {REVIVE_MAX}회를 넘었다 — 폭주로 보고 멈춘다 "
            f"(매크로가 부팅 직후 죽는 중일 수 있음)")
        return
    log(f"[되살림] {REVIVE_DELAY:.0f}초 뒤 매크로를 다시 띄운다 (returncode={ret})")
    time.sleep(REVIVE_DELAY)
    if _macro_running_anywhere():
        log("[되살림] 이미 떠 있다(매크로 자체 예약이 먼저 살렸음) — 생략")
        _set_state("running")
        return
    # ★크레딧은 '실제로 띄울 때만' 센다 (리뷰 M2 부수)★
    #   전에는 이 함수 초입에서 세어, 정상 계정 전환 4회만으로 시간당 8회 상한의
    #   절반을 태웠다. 정작 폭주(부팅 직후 죽음)를 막아야 할 때 크레딧이 없었다.
    _revive_times.append(now)
    log(f"[되살림] {len(_revive_times)}/{REVIVE_MAX}회째")
    if start_macro():
        log("[되살림] 매크로 재기동 완료")
    else:
        err("[되살림] 매크로 재기동 실패")


def _crash_check_thread():
    global macro_proc
    log("[크래시감지] 시작")
    while True:
        try:
            with _state_lock:
                proc = macro_proc
                state = macro_state
            if proc is not None and state == "running":
                ret = proc.poll()
                if ret is not None:
                    log(f"[크래시감지] 매크로 종료됨 (returncode={ret})")
                    with _state_lock:
                        macro_proc = None
                    _set_state("crashed")
                    _auto_revive(ret)
            elif proc is None and state == "running":
                # ★★핸들이 없어도 감시한다 (3.1.5, 2026-08-17 리뷰 M2)★★
                #   계정 전환으로 매크로가 ★스스로★ 되살아나면(powershell 재기동)
                #   macro_proc 은 None 인데 start_macro 의 '외부 기동 감지' 가
                #   state 를 running 으로 세운다. 그러면 위 분기가 영영 안 돌아
                #   ★그 뒤로는 진짜 크래시가 나도 되살리지 않는다★ — 3.1.4 를 만든
                #   바로 그 시나리오(4계정 순회)에서 기능이 죽어 있었다.
                #   → 핸들 대신 ★프로세스 존재★ 로 판정한다.
                if not _macro_running_anywhere():
                    log("[크래시감지] 매크로 프로세스가 사라짐 (핸들 없음 경로)")
                    _set_state("crashed")
                    _auto_revive("no-handle")
            elif state == "crashed":
                # ★★crashed 는 '사건'이지 '지속 상태'가 아니다 (3.1.6, 2026-08-20)★★
                #
                # ★증상★ 매크로가 죽어 crashed 가 되면, 매크로를 다시 살려도
                #   대시보드가 영영 crashed 로 남았다(실측: PC-02 139분 / PC-09 63분 /
                #   PC-17 324분). ★restart 명령을 사람이 쏴야만★ 빠져나왔다.
                #
                # ★왜★ 위 두 분기는 둘 다 `state == "running"` 을 전제로 한다.
                #   그래서 state 가 crashed 가 되는 순간 이 스레드는 아무 일도 안 한다.
                #   crashed 를 벗어나는 유일한 경로는 _auto_revive 안의 4초짜리 창
                #   (프로세스 존재 확인) 하나뿐인데, 그게 다음 세 경우엔 그냥 return 한다:
                #     ① no_restart 표시 있음  ② 시간당 되살림 상한 초과
                #     ③ start_macro 실패
                #   그 뒤 매크로가 ★스스로★ 살아나도(계정 순회의 powershell 부활 예약,
                #   사람이 직접 실행) 알아채는 코드가 없다 = ★편도 트랩★.
                #
                # ★고치는 방식★ 여기서는 ★띄우지 않는다. 이름만 바로잡는다.★
                #   되살리기는 위 분기와 _auto_revive 가 이미 한다. 여기까지 왔다는 건
                #   되살림이 끝났거나 포기했다는 뜻이므로, 남은 일은 '지금 실제로
                #   떠 있는가' 를 보고 표시를 사실과 맞추는 것뿐이다.
                #   (프로세스를 새로 띄우지 않으므로 이중 실행 위험이 원천적으로 없다)
                if _macro_running_anywhere():
                    if _set_state("running", expect="crashed"):
                        log("[크래시감지] 매크로가 다시 떠 있다 — crashed 해제 (재기동 안 함)")
        except Exception as e:
            err(f"[크래시감지] 에러: {e}")
        time.sleep(CRASH_CHECK_INT)


# ==================================================
# 스레드: 버그 업로드 (60s)
# ==================================================
def _bug_upload_thread():
    log("[버그업로드] 시작")
    while True:
        try:
            _upload_bugs()
        except Exception as e:
            err(f"[버그업로드] 에러: {e}")
        time.sleep(BUG_INTERVAL)


def _upload_bugs():
    if not os.path.isdir(BUGS_DIR):
        return
    files = sorted([f for f in os.listdir(BUGS_DIR) if f.endswith('.png')])[:5]
    if not files:
        return
    log(f"[버그업로드] {len(files)}개 파일 업로드 시작")
    for fname in files:
        fpath = os.path.join(BUGS_DIR, fname)
        try:
            with open(fpath, 'rb') as fp:
                r = requests.post(
                    f"{CONTROL_SERVER}/bugs/{pc_id}",
                    files={"file": (fname, fp, "image/png")},
                    headers=_headers(),
                    timeout=(TIMEOUT_CONNECT, 30),
                )
            if r.ok:
                os.remove(fpath)
                log(f"[버그업로드] ✓ {fname}")
            else:
                err(f"[버그업로드] ✗ {fname}: {r.status_code}")
        except Exception as e:
            err(f"[버그업로드] 실패 {fname}: {e}")


# ==================================================
# 진입점
# ==================================================
def _kill_stale_updater_processes():
    # ★★재시작으로 태어났으면 정리하지 않는다 (적대검토 치명 ②)★★
    #   부모가 「새 놈이 5초 살아 있나」를 보고 나서 나가기로 돼 있는데, 여기서
    #   그 부모를 죽여버리면 그 확인이 통째로 무의미해진다. 부모는 스스로 나간다.
    # ★★★pop 이다 — get 이 아니다 (적대검토 2차, 배포차단)★★★
    #   get 으로 두면 표식이 `os.environ` 에 ★영원히★ 남아 ★모든 자손★ 이 물려받는다:
    #     재시작 자식 → start_macro 가 띄운 매크로 → 매크로 자가치유가 다시 띄운 updater.exe
    #   그러면 v3.0.5 의 이중 실행 방어가 ★그 PC 에서 영영 꺼진다★ —
    #   증상이 「명령이 반만 먹는다」로만 보여서 진단이 제일 어려운 부류다.
    if os.environ.pop("AION2_UPD_RESTART", None) == "1":
        log("[정리] 자기 재시작으로 태어났다 — 이번 한 번만 잔여 정리를 건너뛴다"
            "(부모가 스스로 나간다). ★표식은 여기서 지웠다★")
        return
    """다른 updater 프로세스 강제 종료 (v3.0.5) — 자가업데이트 후 구버전이 안 죽고 남으면
    이중 실행(명령 나눠먹기, updater_old.exe 잠금으로 파일 정리 실패, 재자가업데이트 루프)이
    되던 것 차단. 새 인스턴스가 부팅하며 잔여를 정리하므로 어떤 경로로 꼬여도 1개로 수렴."""
    me = os.getpid()
    try:
        import psutil
        for p in psutil.process_iter(['pid', 'exe', 'name']):
            try:
                if p.pid == me:
                    continue
                base = (os.path.basename(p.info.get('exe') or p.info.get('name') or "")).lower()
                if base.startswith("updater") and base.endswith(".exe"):
                    log(f"[정리] 잔여 updater 프로세스 강제 종료: PID {p.pid} ({base})")
                    p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(0.5)   # 종료 후 파일 잠금 해제 대기 (이어지는 파일 정리 성공률)
    except Exception as e:
        err(f"[정리] 잔여 updater 정리 실패 (무시): {e}")


def _cleanup_old_updaters():
    """이전 버전 파일 삭제 (updater_old.exe, updater_v*.exe, updater_new.exe)"""
    current = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
    exe_dir = os.path.dirname(current)
    current_name = os.path.basename(current).lower()
    for fname in os.listdir(exe_dir):
        fl = fname.lower()
        if fl == current_name:
            continue
        if fl in ("updater_old.exe", "updater_new.exe") or (fl.startswith("updater_v") and fl.endswith(".exe")):
            fpath = os.path.join(exe_dir, fname)
            try:
                os.remove(fpath)
                log(f"[정리] 이전 파일 삭제: {fname}")
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# info.txt — 계정별로 묶은 새 폼 (2026-08-16 사용자 지시)
# ══════════════════════════════════════════════════════════════════════════════
# ★왜 다시 쓰나★ 예전엔 칸이 필요할 때마다 파일 ★뒤에 갖다 붙였다★. 그래서 아이디는 위,
#   캐릭은 중간, 서버는 맨 아래로 흩어졌다(사용자: "계정1 관련된건 모아놓게 보이게해
#   너무나눠져잇으면 헷갈려"). 이제 계정 하나의 칸은 한 덩어리로 모은다.
#
# ★안전장치★ info.txt 에는 pc_id 가 들어 있고, 이게 날아가면 그 PC가 PC-01 로 잡혀
#   대시보드 카드가 충돌하는 사고가 이미 있었다. 그래서 다시 쓰기는 이렇게만 한다:
#     ① info.txt.bak 으로 원본 백업  ② 임시파일에 쓰고 os.replace 로 원자 교체
#     ③ ★옛 파일의 값이 전부 새 파일에 살아있는지 검증★ — 하나라도 없으면 중단하고 원본 유지
#     ④ 한 번만 (표식 `계정1_이메일` 이 보이면 재실행 안 함)
#     ⑤ 내가 모르는 칸은 [ 기타 ] 로 그대로 보존
OWNER_CONTROL_KEY = "aion2_secret_2026"   # ★사용자 지시로 내장(2026-08-16)★ 공개 노출 인지·수용됨

INFO_FORM = "4"          # 파일 양식 번호 — 1=옛 폼(칸이 흩어짐), 2=계정별 묶음 + 캐릭수 9칸 환산됨,
#                          3=계정N_플랫폼 칸 추가 (퍼플/스토브 등 — 아이디와 별개, 2026-08-16)
#                          ★4=계정5 칸 추가 (2026-08-24 사고 196)★ — set_info 로 넣은
#                            계정5_* 가 파일 ★끝에 낱줄★ 로만 붙어서 사람이 열면 칸이 안 보였다.
#                            주인님: "info에 계정 5 칸이 없다". 폼을 올려 한 번 재구성한다.
#                            ★재환산 위험 없음★ — _src_form(3) >= 2 라 _conv_slots 를 안 탄다.
#                            (하네스: info_form4_test.py 로 실증)
# ★폼을 올릴 때마다 _build_new_info 의 캐릭수 처리를 반드시 확인할 것★ — 환산(+10)은
#   ★1→2 이관에서 단 한 번만★ 일어나야 한다. 폼 2 파일을 다시 환산하면 16이 26이 되어
#   20대가 한 칸씩 밀린 캐릭으로 들어간다(_build_new_info 의 _src_form 가드 참조).

_PC_FIELDS = [
    ("info_form",          "건드리지 마세요 (파일 양식 번호)"),
    ("pc_id",              "컴퓨터마다 다르게 (PC-01, PC-02 ...)"),
    ("control_api_key",    "관제 서버 열쇠 - 이미 채워져 있습니다"),
    ("edition",            "비워두세요"),
    ("token",              "비워두세요 (알림은 서버 봇이 대신 보냅니다)"),
    ("telegram_chat_id",   "비우면 내장 기본값을 씁니다"),
    ("screenshot_key",     "스샷 단축키"),
    ("lan_prefix",         "내부망 대역 (예: 172.30.1.)"),
]
_PC_RARE = ["lan_ip", "lan_allow", "live_fps", "live_q",
            "gemini_api_key", "anthropic_api_key", "twocaptcha_api_key"]


# ★★계정 개수 — lc/config.py 의 MAX_ACCT 와 같은 값이어야 한다 (2026-08-24 사고 193)★★
#   이 값이 작으면 계정5 칸이 정규화에서 빠져 "기타(예전 칸)" 구역으로 밀린다.
#   (데이터가 날아가지는 않는다 — leftover 로 보존된다)
MAX_ACCT    = 5
ACCT_LABELS = "abcdefghi"[:MAX_ACCT]


def _acct_fields(n):
    """계정 n 의 칸 목록 — 이 순서대로 한 덩어리로 쓴다."""
    return ([f"계정{n}_플랫폼", f"계정{n}_아이디", f"계정{n}_비번", f"계정{n}_이메일",
             f"계정{n}_휴대폰", f"계정{n}_서버", f"계정{n}_PIN", f"계정{n}_캐릭수"]
            + [f"계정{n}_캐릭{i}" for i in range(1, 10)])


def _read_kv(text):
    kv = {}
    for ln in text.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv


def _conv_slots(v):
    """캐릭수 값 변환 — 옛 규칙 → 새 규칙(십의자리=비울 칸수, 일의자리=캐릭수).

    ★옛 표(CHAR_SLOT_POSITIONS)는 9칸 그리드의 2~7칸이었다★ — 즉 예전에 숫자만 적은 것은
    실제로는 '한 칸 비우고 시작'이었다. 그래서 전부 +10 하면 지금 동작이 그대로 유지된다.
    'all' 은 예전에도 9칸 그리드 1번째부터였으므로 그냥 9.
    """
    v = (v or "").strip()
    if not v:
        return ""
    if v.lower() == "all":
        return "9"
    try:
        return str(int(v) + 10)
    except ValueError:
        return v


def _build_new_info(kv):
    """옛 kv → (새 본문, 변환된키맵, 남은키). 값은 하나도 버리지 않는다."""
    used, out, conv, compat = set(), {}, {}, {}
    # ★원본 파일의 양식 번호★ — 캐릭수 환산(+10)을 걸지 말지 정하는 유일한 근거.
    #   폼 1 → 2 에서 딱 한 번만 환산한다. 폼 2 이상인 파일을 다시 환산하면 16이 26이 되어
    #   전 함대가 한 칸 밀린 캐릭으로 들어간다. 폼 번호를 또 올릴 때도 이 가드가 지켜준다.
    #   ★못 읽으면 1 로 떨어뜨리지 않는다 (2026-08-24 사고 196 적대리뷰)★
    #     `info_form=` / `info_form=abc` / 줄 자체가 없음 → 예전엔 전부 _src_form=1 이 되어
    #     ★이미 환산된 파일을 또 환산★ 했다(22→32, 16→26 = 전 계정이 한 칸씩 밀림).
    #     그리고 missing 검사는 환산값을 conv_old 로 화이트리스트하므로 ★원리상 못 잡는다.★
    #     실제 도달 경로: 렌탈 setup ZIP 의 info.txt 에 info_form 줄이 없고,
    #     매뉴얼 4종이 "메모장으로 이렇게 적으세요" 양식에 info_form 을 안 넣는다.
    #     → 폼을 확신할 수 없으면 ★환산을 아예 걸지 않는다★(None). 안 하는 쪽이 안전하다.
    _raw_form = (kv.get("info_form") or "").strip()
    try:
        _src_form = int(_raw_form) if _raw_form else None
    except ValueError:
        _src_form = None

    def take(*names):
        """후보 중 ★처음 나오는 값★을 쓰되, 후보 전부를 '읽은 칸'으로 표시한다.

        ★전부 표시해야 한다★ — 값을 준 칸만 표시하면, 새 이름(계정1_캐릭1)에 값이 있을 때
        옛 이름(char1)이 '안 읽은 칸'으로 남아 [ 기타 ]에 그대로 다시 찍힌다. 같은 값이 파일에
        두 번 보이는 셈이라, 계정별로 묶어놓은 의미가 없어진다.
        """
        val = ""
        for nm in names:
            if nm in kv:
                used.add(nm)
                if kv[nm] and not val:
                    val = kv[nm]
        return val

    for k, _ in _PC_FIELDS:
        out[k] = take(k)
    for k in _PC_RARE:
        out[k] = take(k)
    # ★양식 번호는 이 함수가 정한다★ — 옛 파일 값을 그대로 물려받으면 안 된다.
    #   이 값이 곧 "캐릭수가 9칸 기준으로 환산됐다"는 보증이고, 매크로가 이걸 보고
    #   환산 보정을 걸지 말지 정한다.
    out["info_form"] = INFO_FORM
    if not out.get("control_api_key"):
        out["control_api_key"] = OWNER_CONTROL_KEY
    if not out.get("pc_id"):
        out["pc_id"] = take("pc_name") or "PC-??"
    if not out.get("screenshot_key"):
        out["screenshot_key"] = "ctrl+q"

    for n in range(1, MAX_ACCT + 1):
        lab = ACCT_LABELS[n - 1]
        base = (n == 1)
        # 플랫폼(퍼플/스토브 등) — 아이디와 별개 칸(2026-08-16 사용자 지시). 옛 키는 없다.
        out[f"계정{n}_플랫폼"] = take(f"계정{n}_플랫폼", f"{lab}_platform")
        out[f"계정{n}_아이디"] = take(f"계정{n}_아이디", f"{lab}_web_id")
        out[f"계정{n}_비번"]   = take(f"계정{n}_비번",   f"{lab}_web_pw")
        out[f"계정{n}_이메일"] = take(f"계정{n}_이메일", f"{lab}_email")
        out[f"계정{n}_휴대폰"] = take(f"계정{n}_휴대폰", f"{lab}_phone")
        out[f"계정{n}_서버"]   = take(f"계정{n}_서버", f"{lab}_server", *(["server"] if base else []))
        out[f"계정{n}_PIN"]    = take(f"계정{n}_PIN", f"{lab}_password_digits",
                                     *(["password_digits"] if base else []))
        # ★캐릭수만은 옛 키를 '소비하지 않고' 원래 값 그대로 남긴다(2026-08-16)★
        #   업데이트는 ①업데이터 자가갱신 → ②info.txt 이관 → ③매크로 exe 다운로드(75MB, 수 분)
        #   순서라, ②와 ③ 사이에는 ★옛 매크로가 새 파일을 읽는 구간★이 있다. 이때 옛 매크로가
        #   환산된 값(16)을 옛 규칙으로 해석하면 한 칸 어긋난 칸을 누른다(20대 동시).
        #   옛 키(total_slots / b_total_slots …)를 원래 값으로 남겨두면 옛 매크로는 그걸 읽어
        #   지금까지와 똑같이 동작하고, 새 매크로는 계정N_캐릭수를 우선해서 새 규칙으로 읽는다.
        _legacy_key = f"{lab}_total_slots" if not base else "total_slots"
        _slots_raw = (kv.get(f"계정{n}_캐릭수") or kv.get(_legacy_key) or "").strip()
        used.add(f"계정{n}_캐릭수")
        # ★환산은 옛 폼(1)에서 올라올 때만★ — 폼 2 이상은 이미 9칸 기준이라 그대로 둔다.
        # _src_form 이 None(=폼 불명) 이면 환산하지 않는다 — 위 주석 참조
        _new = _conv_slots(_slots_raw) if (_src_form is not None and _src_form < 2) else _slots_raw
        out[f"계정{n}_캐릭수"] = _new
        if _slots_raw and _new != _slots_raw:
            conv[f"계정{n}_캐릭수"] = (_slots_raw, _new)
        if kv.get(_legacy_key):
            compat[_legacy_key] = kv[_legacy_key]      # 원래 값 그대로 보존
            used.add(_legacy_key)
        for i in range(1, 10):
            out[f"계정{n}_캐릭{i}"] = take(f"계정{n}_캐릭{i}", *([f"char{i}"] if base else []))

    # ★신규 PC 기본 캐릭수(2026-08-16 리뷰) — 반드시 계정 루프 '뒤'★
    #   옛 양식은 `total_slots=5` 를 미리 박아뒀다. 새 폼에서 이 칸이 비면 매크로가 모듈
    #   기본값으로 굴러가고, 사람이 채우기 전까지 '몇 칸부터 몇 개'가 불분명해진다.
    #   옛 total_slots=5 는 9칸 그리드의 2~6칸이었으므로 같은 뜻인 15 를 넣어둔다.
    #   (루프 앞에 두면 루프가 빈 값으로 덮어쓴다 — 실제로 한 번 그렇게 짰다가 잡았다)
    if not out.get("계정1_캐릭수"):
        out["계정1_캐릭수"] = "15"

    leftover = {k: v for k, v in kv.items() if k not in used and v}

    L = []
    L.append("==================  이 PC 설정  ==================")
    for k, _ in _PC_FIELDS:
        L.append(f"{k}={out.get(k, '')}")
    L.append("")
    L.append("------  거의 안 씀 (비워두세요)  ------")
    for k in _PC_RARE:
        L.append(f"{k}={out.get(k, '')}")
    for n in range(1, MAX_ACCT + 1):
        L.append("")
        L.append(f"=================={'  계정 %d  (본계정)  ' % n if n == 1 else '  계정 %d  ' % n}==================")
        for k in _acct_fields(n):
            L.append(f"{k}={out.get(k, '')}")
    if compat:
        L.append("")
        L.append("------  예전 프로그램 호환용 (건드리지 마세요)  ------")
        for k in sorted(compat):
            L.append(f"{k}={compat[k]}")
    if leftover:
        L.append("")
        L.append("------  기타 (예전 칸 - 지우지 마세요)  ------")
        for k in sorted(leftover):
            L.append(f"{k}={leftover[k]}")
    L.append("")
    L.append("[ 채우는 법 ]")
    L.append("info_form        건드리지 마세요 (파일 양식 번호 - 프로그램이 씁니다)")
    L.append("pc_id            컴퓨터마다 다르게 (PC-01, PC-02 ...)")
    L.append("계정N_플랫폼     퍼플 / 스토브 등 - 그 계정이 어느 플랫폼 것인지")
    L.append("계정N_아이디     퍼플 로그인 아이디 - 비우면 그 계정은 없는 것으로 봅니다")
    L.append("계정N_캐릭수     십의자리는 앞에서 비울 칸수, 일의자리는 캐릭 수")
    L.append("                 6 은 9칸 중 1번째부터 6개, 16 은 2번째부터 6개, 26 은 3번째부터 6개")
    L.append("계정N_PIN        그 계정 퍼플 재접속 PIN")
    L.append("계정N_캐릭1~9    캐릭 이름 - 적으면 이름 읽기를 건너뜁니다. 없으면 비워두세요")
    return "\n".join(L) + "\n", conv, leftover


def ensure_info_txt():
    """info.txt 를 새 폼으로 만들거나 옮긴다. 실패하면 원본을 절대 건드리지 않는다."""
    try:
        if not os.path.exists(INFO_TXT):
            body, _, _ = _build_new_info({})
            with open(INFO_TXT, "w", encoding="utf-8") as f:
                f.write(body)
            log(f"[업데이터] info.txt 새 폼 생성됨(키 내장) → {INFO_TXT}")
            log("[업데이터] ※ pc_id 와 계정1_아이디/비번을 채우고 updater를 재시작하세요")
            return

        # ══════════════════════════════════════════════════════════
        # ★★인코딩 3단 방어 — 없으면 파일을 파괴한다 (2026-08-24 사고 196 적대리뷰)★★
        #   예전엔 utf-8-sig + errors="replace" 하나뿐이었다. 메모장 "ANSI 저장"(CP949)
        #   파일을 그렇게 읽으면 ★한글 키가 전부 U+FFFD 로 깨져★ take() 가 못 알아보고,
        #   계정1_아이디/비번/PIN/캐릭 이 통째로 빈 채 재작성된다.
        #   더 나쁜 건 ★검사를 통과한다★ 는 것 — 깨진 값이 [기타] 에 남아 있어
        #   `v in newvals` 가 참이 되기 때문이다. 로그는 "정리했습니다" 만 찍는다.
        #   pc_id·API키는 ASCII 라 살아남으므로 ★PC 는 멀쩡히 붙어 있는데 로그인만 안 된다.★
        #   그리고 계정1_캐릭수가 기본값 15 로 떨어져 ★사고 139(빈 [캐릭터 생성] 칸 클릭)★
        #   조건이 그대로 만들어진다.
        #   매크로(lc/config.py:load_info_txt)에는 이 폴백이 ★원래 있었다★ —
        #   방어가 ★읽는 쪽에만 있고 재작성하는 쪽에는 없었다.★
        # ══════════════════════════════════════════════════════════
        old = None
        for _enc in ("utf-8-sig", "cp949"):
            try:
                with open(INFO_TXT, encoding=_enc) as f:
                    old = f.read()
                break
            except UnicodeDecodeError:
                continue
        if old is None:
            log("[업데이터] ★info.txt 이관 중단★ — utf-8/cp949 둘 다로 못 읽습니다(원본 유지)")
            return
        if "\ufffd" in old:
            # 어떤 인코딩으로도 온전히 못 읽었다 = 손대면 안 된다
            log("[업데이터] ★info.txt 이관 중단★ — 깨진 문자가 있습니다(인코딩 불명, 원본 유지)")
            return
        # ★판정은 양식 번호로만★ — 예전엔 '계정1_이메일 이 있으면 새 폼'으로 봤는데, 사용자가
        #   옛 파일에 그 줄만 손으로 추가하면 ★캐릭수는 환산 안 된 채 새 폼 취급★이 돼
        #   매크로가 한 칸 어긋난 칸을 누른다. 양식 번호는 이관이 실제로 끝나야만 찍힌다.
        try:
            _form = int((_read_kv(old).get("info_form") or "1").strip() or "1")
        except ValueError:
            _form = 1
        if _form >= int(INFO_FORM):
            return                                   # 이미 새 폼 — 다시 건드리지 않는다

        kv = _read_kv(old)
        body, conv, leftover = _build_new_info(kv)

        # ★검증★ 옛 파일의 값이 전부 살아있나 (변환된 캐릭수는 새 값으로 확인)
        newkv = _read_kv(body)
        newvals = set(newkv.values())
        conv_old = {o for o, _ in conv.values()}
        # ★info_form 은 값 비교에서 빼되, ★대신 새 값이 제대로 찍혔는지 단언한다★
        #   (2026-08-24 사고 196 + 적대리뷰)
        #   · 빼야 하는 이유: 이 값은 ★이관 표식 자체★ 라 3→4 로 바뀌는 게 정상인데,
        #     '옛 값이 새 파일에 있나' 로만 보면 매번 '소실' 로 판정해 이관이 중단된다.
        #   · 그냥 빼기만 하면 ★새 파일에 info_form 이 안 써져도 아무도 못 잡는다.★
        #     그 경우 _form=1 이 되어 ★매 부팅마다 재이관★ 되고, 폼 불명이라
        #     캐릭수가 부팅마다 +10 복리로 밀린다(실측: 22→32→42→52…).
        #   → 제외 + 단언 두 개를 같이 둔다.
        if newkv.get("info_form") != INFO_FORM:
            log(f"[업데이터] ★info.txt 이관 중단★ — 새 본문에 info_form={INFO_FORM} 이 "
                f"안 찍혔습니다(실제 {newkv.get('info_form')!r}). 원본을 그대로 둡니다")
            return
        missing = [k for k, v in kv.items()
                   if k != "info_form" and v and v not in newvals and v not in conv_old]
        if missing:
            log(f"[업데이터] ★info.txt 이관 중단★ — 옮기지 못한 칸 {missing[:6]} "
                f"→ 원본을 그대로 둡니다(동작에 지장 없음)")
            return

        # ══════════════════════════════════════════════════════════
        # ★★값 검사만으로는 못 잡는 사고가 있다 — 그래서 한 겹 더 (사고 196 적대리뷰)★★
        #   CP949 파일이 깨져 들어오면 `계정N_아이디` 같은 ★아는 칸이 통째로 비는데★
        #   깨진 값은 [기타] 에 남아 있어 위 missing 검사를 그대로 통과한다.
        #   → "옛 파일엔 값이 있었는데 새 파일에선 빈 칸" 이 하나라도 있으면 손대지 않는다.
        #   ★이건 '값이 어디 있나' 가 아니라 '제자리에 있나' 를 본다.★
        _lost = [k for k in kv
                 if k.startswith("계정") and (kv.get(k) or "").strip()
                 and not (newkv.get(k) or "").strip()]
        if _lost:
            log(f"[업데이터] ★info.txt 이관 중단★ — 계정 칸 {len(_lost)}개가 빈 칸이 됩니다 "
                f"{_lost[:6]} (인코딩 문제 의심) → 원본을 그대로 둡니다")
            return

        # ★백업 이름에 ★옛 폼 번호★ 를 붙인다 (사고 196 적대리뷰)★ — 매크로 set_info 도
        #   같은 이름의 .bak 을 쓴다. 이관 직후 set_info 가 한 번 돌면 ★구폼 원본이 사라진다.★
        shutil.copy2(INFO_TXT, INFO_TXT + ".bak")
        shutil.copy2(INFO_TXT, f"{INFO_TXT}.form{_form}.bak")
        tmp = INFO_TXT + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())      # ★정전 내구성★ — rename 만 먼저 내려가면 0바이트가 남는다
        os.replace(tmp, INFO_TXT)
        log(f"[업데이터] info.txt 를 계정별 묶음 폼으로 정리했습니다 (원본: info.txt.bak)")
        for k, (o, n) in conv.items():
            log(f"[업데이터]   {k}: {o} → {n} (9칸 기준으로 환산 — 동작은 그대로)")
        if leftover:
            log(f"[업데이터]   모르는 칸 {len(leftover)}개는 [ 기타 ] 로 보존했습니다")
    except Exception as e:
        err(f"[업데이터] info.txt 처리 실패(원본 유지): {e}")


def main():
    log("=" * 60)
    log(f"[업데이터] 상주형 데몬 시작 v{UPDATER_VERSION}")
    _kill_stale_updater_processes()
    _cleanup_old_updaters()
    load_pc_id()
    log(f"[업데이터] PC: {pc_id}")

    # ── 원격 로그 전송 시작 (3.1.6) ─────────────────────────────────────────
    #   ★load_pc_id() 뒤여야 한다★ — 그 전에는 pc_id 도 CONTROL_API_KEY 도 없어서
    #   _log_flush_once 가 None(=쌓아만 둠)만 돌려준다. 여기서 띄우면 부팅 로그부터
    #   전부 실린다(이 위 줄들은 이미 링버퍼에 들어가 있다).
    threading.Thread(target=_log_thread, daemon=True, name="logsend").start()
    # ★문구에 `[BOOT` 를 넣지 말 것★ — 서버 /log/ 수신부가 그 문자열을 보면
    #   계정 자동순환 상태기계(_rot_note_boot)를 건드린다. 업데이터 부팅은 매크로
    #   부팅이 아니므로 순환 상태를 만지면 안 된다.
    log(f"[UPDLOG] updater v{UPDATER_VERSION} pc={pc_id} 원격 로그전송 ON")

    # ── 필수 디렉토리 보장 ───────────────────────────────────────────────────
    for d in [IMAGES_DIR, BUGS_DIR]:
        os.makedirs(d, exist_ok=True)
        log(f"[업데이터] 폴더 확인: {d}")

    # ── info.txt 새 폼(계정별 묶음) 생성 / 이관 ─────────────────────────────
    ensure_info_txt()

    # ── 시작 시 자동 업데이트 (exe + 이미지) ────────────────────────────────
    log("[업데이터] 시작 업데이트 체크 중...")
    try:
        check_and_update()
    except Exception as e:
        err(f"[업데이터] 시작 업데이트 실패 (무시하고 계속): {e}")

    # ── 업데이트 완료 후 매크로 자동 실행 ───────────────────────────────────
    _setup_ok_for_start = pc_id not in ("PC-??", "PC-?", "")

    if not _setup_ok_for_start:
        log("[업데이터] ※ info.txt 에 pc_id 미설정 (PC-?? 기본값) → 매크로 자동 실행 생략")
        log("[업데이터] info.txt 에서 pc_id=PC-01 처럼 설정 후 updater를 재시작하세요")
    elif not os.path.exists(MACRO_EXE):
        log(f"[업데이터] 매크로 EXE 없음 ({MACRO_EXE}) → 대시보드에서 수동 시작하세요")
    else:
        log("[업데이터] 업데이트 완료 → 매크로 자동 실행")
        start_macro()

    threads = [
        threading.Thread(target=_poll_thread,        daemon=True, name="poll"),
        threading.Thread(target=_status_thread,      daemon=True, name="status"),
        threading.Thread(target=_crash_check_thread, daemon=True, name="crash"),
        threading.Thread(target=_bug_upload_thread,  daemon=True, name="bugs"),
        threading.Thread(target=_hotkey_thread,      daemon=True, name="hotkey"),
    ]
    for t in threads:
        t.start()
    log(f"[업데이터] {len(threads)}개 스레드 시작 완료")

    while True:
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[업데이터] 인터럽트 → 종료")
    except Exception as e:
        err(f"[업데이터] 치명적 오류: {e}")
        import traceback
        err(traceback.format_exc())
        # ★제일 중요한 로그가 여기다★ — 업데이터가 통째로 죽는 경로. 여기서 안 보내면
        #   서버에는 아무 흔적도 안 남고 그 PC 는 조용히 함대에서 빠진다(PC-23 사고).
        _log_shutdown(f"치명적 오류: {e}")
