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
UPDATER_VERSION  = "2.8.0"

UPDATE_SERVER    = "https://web-production-8d4c.up.railway.app"
CONTROL_SERVER   = "https://web-production-8d4c.up.railway.app"
CONTROL_API_KEY  = "aion2_secret_2026"

TIMEOUT_CONNECT  = 15
TIMEOUT_DOWNLOAD = 120

MACRO_EXE        = r"C:\auto\혼종_통합_자동.exe"
MACRO_EXE_BACKUP = r"C:\auto\혼종_통합_자동.exe.bak"
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
    global pc_id
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


def download_file(url: str, dest_path: str, expected_sha256: str = None) -> bool:
    tmp_path = dest_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        r = requests.get(url, stream=True,
                         timeout=(TIMEOUT_CONNECT, TIMEOUT_DOWNLOAD))
        r.raise_for_status()
        with open(tmp_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        if expected_sha256:
            actual = sha256_file(tmp_path)
            if actual != expected_sha256:
                err(f"[다운로드] 해시 불일치: {os.path.basename(dest_path)}")
                os.remove(tmp_path)
                return False
        shutil.move(tmp_path, dest_path)
        os.utime(dest_path, None)
        return True
    except requests.exceptions.ConnectionError:
        err(f"[다운로드] 연결 실패: {url}")
    except requests.exceptions.Timeout:
        err(f"[다운로드] 타임아웃: {url}")
    except Exception as e:
        err(f"[다운로드] 실패 {url}: {e}")
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except Exception:
            pass
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

        found = [False]
        def callback(hwnd, lParam):
            title = ctypes.create_unicode_buffer(256)
            GetWindowTextW(hwnd, title, 256)
            t = title.value.lower()
            # PURPLE On-NCSOFT - Chrome 또는 purpleon 등
            if ('purple' in t or 'aion' in t or 'ncsoft' in t) and 'chrome' in t:
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
        # 매크로 콘솔 + updater 콘솔 최소화 → 게임 창이 자동으로 앞으로
        time.sleep(2.0)
        _minimize_consoles()
        _focus_game_window()
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
        log(f"[자가업데이트] 새 버전 실행 완료 → 자신 종료")
        time.sleep(1)
        os._exit(0)
    except Exception as e:
        err(f"[자가업데이트] 실패: {e}")
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
    while True:
        try:
            r = requests.get(
                f"{CONTROL_SERVER}/updater/command/{pc_id}",
                headers=_headers(),
                timeout=(TIMEOUT_CONNECT, 10),
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("command"):
                    cmd_id = data.get("id")
                    # ACK 먼저
                    try:
                        requests.post(
                            f"{CONTROL_SERVER}/updater/command/{pc_id}/ack/{cmd_id}",
                            headers=_headers(),
                            timeout=(TIMEOUT_CONNECT, 5),
                        )
                    except Exception:
                        pass
                    threading.Thread(target=handle_command, args=(data,), daemon=True).start()
        except Exception as e:
            err(f"[폴링] 에러: {e}")
        time.sleep(POLL_INTERVAL)


# ==================================================
# 스레드: 상태 보고 (30s)
# ==================================================
def _status_thread():
    log("[상태보고] 시작")
    while True:
        try:
            with _state_lock:
                state = macro_state
                pid = macro_proc.pid if macro_proc and macro_proc.poll() is None else None
            # info.txt에서 token 읽기 (설정 완료 여부 확인)
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
            requests.post(
                f"{CONTROL_SERVER}/updater/status/{pc_id}",
                json={
                    "pc_id": pc_id,
                    "macro_state": state,
                    "macro_pid": pid,
                    "updater_version": UPDATER_VERSION,
                    "setup_complete": _setup_ok,
                },
                headers=_headers(),
                timeout=(TIMEOUT_CONNECT, 5),
            )
        except Exception as e:
            err(f"[상태보고] 에러: {e}")
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
