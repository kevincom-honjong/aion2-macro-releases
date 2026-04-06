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

import requests  # pip install requests

# ==================================================
# 설정
# ==================================================
UPDATER_VERSION  = "2.0.0"

UPDATE_SERVER    = "https://aion2-macro-releases-production.up.railway.app"
CONTROL_SERVER   = "https://web-production-8d4c.up.railway.app"
CONTROL_API_KEY  = "aion2_secret_2026"

TIMEOUT_CONNECT  = 5
TIMEOUT_DOWNLOAD = 60

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
    if proc is None:
        return
    if proc.poll() is not None:
        _set_state("stopped")
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log("[매크로] 정지 완료")
    except Exception as e:
        err(f"[매크로] 정지 실패: {e}")
    _set_state("stopped")


# ==================================================
# 업데이트 로직
# ==================================================
def check_and_update() -> bool:
    """서버 버전 체크 후 필요시 업데이트. True = 업데이트 있었음."""
    log("[업데이트] 체크 시작")
    _set_state("updating")
    local = load_local_version()
    local_image_hashes = get_local_image_hashes()

    try:
        resp = requests.post(
            f"{UPDATE_SERVER}/check",
            json={"exe_version": local.get("exe_version", "0.0.0"),
                  "image_hashes": local_image_hashes},
            timeout=(TIMEOUT_CONNECT, 15),
        )
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        err(f"[업데이트] 서버 연결 실패: {e}")
        _set_state("stopped")
        return False

    any_update = False

    # exe 업데이트
    exe_info = result.get("exe_update")
    if exe_info:
        new_ver = exe_info["version"]
        local_ver = local.get("exe_version", "없음")
        log(f"[업데이트] exe {local_ver} → {new_ver}")
        if os.path.exists(MACRO_EXE):
            try:
                shutil.copy2(MACRO_EXE, MACRO_EXE_BACKUP)
            except Exception as e:
                err(f"[업데이트] 백업 실패: {e}")
        ok = download_file(exe_info["download_url"], MACRO_EXE, exe_info.get("sha256"))
        if ok:
            local["exe_version"] = new_ver
            any_update = True
            log(f"[업데이트] exe 완료: v{new_ver}")
        else:
            err("[업데이트] exe 다운로드 실패")
            if os.path.exists(MACRO_EXE_BACKUP):
                try:
                    shutil.copy2(MACRO_EXE_BACKUP, MACRO_EXE)
                    log("[업데이트] 백업 복구 완료")
                except Exception as e:
                    err(f"[업데이트] 복구 실패: {e}")
    else:
        log(f"[업데이트] exe 최신 ({local.get('exe_version', '?')})")

    # 이미지 업데이트
    images_to_update = result.get("images_update", [])
    if images_to_update:
        os.makedirs(IMAGES_DIR, exist_ok=True)
        for img in images_to_update:
            dest = os.path.join(IMAGES_DIR, img["filename"])
            ok = download_file(img["download_url"], dest, img.get("sha256"))
            if ok:
                local_image_hashes[img["filename"]] = img["sha256"]
                any_update = True
                log(f"[업데이트] ✓ 이미지: {img['filename']}")
            else:
                err(f"[업데이트] ✗ 이미지: {img['filename']}")
    else:
        log("[업데이트] 이미지 최신 상태")

    local["image_hashes"] = local_image_hashes
    local["last_check"] = time.strftime('%Y-%m-%d %H:%M:%S')
    save_local_version(local)
    log("[업데이트] 완료!" if any_update else "[업데이트] 최신 버전")
    _set_state("stopped")
    return any_update


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
            requests.post(
                f"{CONTROL_SERVER}/updater/status/{pc_id}",
                json={
                    "pc_id": pc_id,
                    "macro_state": state,
                    "macro_pid": pid,
                    "updater_version": UPDATER_VERSION,
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
def main():
    log("=" * 60)
    log(f"[업데이터] 상주형 데몬 시작 v{UPDATER_VERSION}")
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
            log(f"[업데이터] info.txt 기본 양식 생성됨 → {INFO_TXT}")
            log("[업데이터] ※ info.txt 에서 pc_id 를 수정하고 updater를 재시작하세요")
        except Exception as e:
            err(f"[업데이터] info.txt 생성 실패: {e}")

    # ── 시작 시 자동 업데이트 (exe + 이미지) ────────────────────────────────
    log("[업데이터] 시작 업데이트 체크 중...")
    try:
        check_and_update()
    except Exception as e:
        err(f"[업데이터] 시작 업데이트 실패 (무시하고 계속): {e}")

    log("[업데이터] 대시보드에서 ▶ 시작 명령을 보내면 매크로 실행됩니다.")

    threads = [
        threading.Thread(target=_poll_thread,        daemon=True, name="poll"),
        threading.Thread(target=_status_thread,      daemon=True, name="status"),
        threading.Thread(target=_crash_check_thread, daemon=True, name="crash"),
        threading.Thread(target=_bug_upload_thread,  daemon=True, name="bugs"),
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
