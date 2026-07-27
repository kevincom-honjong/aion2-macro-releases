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
UPDATER_VERSION  = "3.0.7"

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


def start_macro() -> bool:
    global macro_proc
    with _state_lock:
        if macro_proc is not None and macro_proc.poll() is None:
            log("[매크로] 이미 실행 중")
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

    # ── info.txt 없으면 기본 양식 생성 ──────────────────────────────────────
    if not os.path.exists(INFO_TXT):
        try:
            with open(INFO_TXT, 'w', encoding='utf-8') as f:
                f.write("pc_id=PC-??\n")
                f.write("token=\n")
                f.write("server=\n")
                f.write("total_slots=5\n")
                f.write("screenshot_key=ctrl+q\n")
            log(f"[업데이터] info.txt 기본 양식 생성됨 → {INFO_TXT}")
            log("[업데이터] ※ info.txt 에서 pc_id / char1~3 을 수정하고 updater를 재시작하세요")
        except Exception as e:
            err(f"[업데이터] info.txt 생성 실패: {e}")

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
