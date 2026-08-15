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
UPDATER_VERSION  = "3.1.2"

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
def _set_state(state: str):
    global macro_state
    with _state_lock:
        macro_state = state
    log(f"[상태] macro_state → {state}")


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
    global macro_proc
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

        # 새 updater.exe 실행
        subprocess.Popen(
            [target],
            cwd=exe_dir,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        launched = True
        log(f"[자가업데이트] 새 버전 실행 완료 → 자신 종료")
        time.sleep(1)
        os._exit(0)
    except Exception as e:
        err(f"[자가업데이트] 실패: {e}")
        # ★새 인스턴스가 이미 떠 있으면 내가 남는 순간 이중 실행(명령 나눠먹기/파일 잠금
        #   꼬임) — 무조건 종료(v3.0.5). (새 쪽 부팅 시 잔여 프로세스 강제 정리도 있음)★
        if launched:
            os._exit(0)
        # 복구 시도
        try:
            if not os.path.exists(target) and os.path.exists(old_bak):
                os.rename(old_bak, target)
                log("[자가업데이트] 복구 완료")
        except: pass
        try: os.remove(new_tmp)
        except: pass


def check_and_update() -> bool:
    """서버 버전 체크 후 필요시 업데이트. True = 업데이트 있었음."""
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
        err(f"[업데이트] 서버 연결 실패: {e}")
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
        if ok:
            local["exe_version"] = new_ver
            any_update = True
            log(f"[업데이트] ✓ 매크로 exe v{new_ver} 완료")
        else:
            err(f"[업데이트] ✗ 매크로 exe 다운로드 실패")
            if os.path.exists(MACRO_EXE_BACKUP):
                try:
                    shutil.copy2(MACRO_EXE_BACKUP, MACRO_EXE)
                    log("[업데이트] 백업으로 복구 완료")
                except Exception as e:
                    err(f"[업데이트] 복구 실패: {e}")
    else:
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
        log(f"[업데이트] 이미지 완료 — 성공 {ok_cnt} / 실패 {fail_cnt}")
    else:
        log(f"[업데이트] ✓ 이미지 최신 ({len(local_image_hashes)}개)")

    local["image_hashes"] = local_image_hashes
    local["last_check"]   = time.strftime('%Y-%m-%d %H:%M:%S')
    save_local_version(local)
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
        check_and_update()
        time.sleep(1.0)
        start_macro()

    elif command == "update_only":
        stop_macro()
        time.sleep(1.0)
        check_and_update()

    elif command == "screenshot":
        threading.Thread(target=take_bug_screenshot, args=(True,), daemon=True).start()

    elif command == "exit":
        log("[명령] 업데이터 종료")
        stop_macro()
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
                    log(f"[크래시감지] 매크로 예기치 않게 종료됨 (returncode={ret})")
                    with _state_lock:
                        macro_proc = None
                    _set_state("crashed")
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

INFO_FORM = "2"          # 파일 양식 번호 — 1=옛 폼(칸이 흩어짐), 2=계정별 묶음 + 캐릭수 9칸 환산됨

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


def _acct_fields(n):
    """계정 n 의 칸 목록 — 이 순서대로 한 덩어리로 쓴다."""
    return ([f"계정{n}_아이디", f"계정{n}_비번", f"계정{n}_이메일", f"계정{n}_휴대폰",
             f"계정{n}_서버", f"계정{n}_PIN", f"계정{n}_캐릭수"]
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

    for n in range(1, 5):
        lab = "abcd"[n - 1]
        base = (n == 1)
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
        _new = _conv_slots(_slots_raw)
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
    for n in range(1, 5):
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

        with open(INFO_TXT, encoding="utf-8-sig", errors="replace") as f:
            old = f.read()
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
        missing = [k for k, v in kv.items()
                   if v and v not in newvals and v not in conv_old]
        if missing:
            log(f"[업데이터] ★info.txt 이관 중단★ — 옮기지 못한 칸 {missing[:6]} "
                f"→ 원본을 그대로 둡니다(동작에 지장 없음)")
            return

        shutil.copy2(INFO_TXT, INFO_TXT + ".bak")
        tmp = INFO_TXT + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
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
