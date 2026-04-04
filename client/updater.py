# updater.py - AION2 매크로 자동 업데이터 클라이언트
# PyInstaller로 updater.exe 빌드 후 각 PC에 배포
# 동작: 버전 체크 → 업데이트 다운로드 → 혼종_통합_자동.exe 자동 실행
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
from pathlib import Path

import requests  # pip install requests

# ==================================================
# 설정 — 배포 전에 UPDATE_SERVER만 실제 URL로 교체
# ==================================================
# TODO: Railway 배포 후 아래 URL을 실제 주소로 교체
# Railway #1 (업데이터 서버) 배포 완료 후:
#   UPDATE_SERVER = "https://실제주소.railway.app"
UPDATE_SERVER    = "https://aion2-macro-releases-production.up.railway.app"
TIMEOUT_CONNECT  = 5     # 서버 연결 타임아웃 (초)
TIMEOUT_DOWNLOAD = 60    # 파일 다운로드 타임아웃 (초)

MACRO_EXE        = r"C:\auto\혼종_통합_자동.exe"
MACRO_EXE_BACKUP = r"C:\auto\혼종_통합_자동.exe.bak"
IMAGES_DIR       = r"C:\auto\images2"
LOCAL_VERSION    = r"C:\auto\version.json"
LOG_FILE         = r"C:\auto\updater.log"
INFO_TXT         = r"C:\auto\info.txt"   # 건드리지 않음

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
# 유틸 함수
# ==================================================
def sha256_file(path: str) -> str:
    """파일의 SHA256 해시 반환"""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def load_local_version() -> dict:
    """로컬 version.json 로드"""
    try:
        if os.path.exists(LOCAL_VERSION):
            with open(LOCAL_VERSION, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        err(f"[버전] 로드 실패: {e}")
    return {"exe_version": "0.0.0", "image_hashes": {}}


def save_local_version(data: dict):
    """로컬 version.json 저장"""
    try:
        with open(LOCAL_VERSION, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        err(f"[버전] 저장 실패: {e}")


def get_local_image_hashes() -> dict:
    """C:\\auto\\images2 내 모든 파일의 SHA256 해시 반환"""
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
    """
    URL에서 파일 다운로드.
    임시파일에 받은 후 해시 검증 → 교체.
    실패 시 False 반환.
    """
    tmp_path = dest_path + ".tmp"
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        r = requests.get(
            url, stream=True,
            timeout=(TIMEOUT_CONNECT, TIMEOUT_DOWNLOAD)
        )
        r.raise_for_status()

        with open(tmp_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)

        # SHA256 검증
        if expected_sha256:
            actual = sha256_file(tmp_path)
            if actual != expected_sha256:
                err(f"[다운로드] 해시 불일치 {os.path.basename(dest_path)}: "
                    f"expected={expected_sha256[:12]}... actual={actual[:12]}...")
                os.remove(tmp_path)
                return False

        shutil.move(tmp_path, dest_path)
        # 파일 수정 날짜를 현재 시간으로 강제 갱신 (shutil.move 후 타임스탬프 보장)
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
        except:
            pass
    return False


# ==================================================
# 업데이터 메인 로직
# ==================================================
def run_updater():
    log("=" * 50)
    log("[업데이터] 시작")

    local = load_local_version()
    local_image_hashes = get_local_image_hashes()
    log(f"[업데이터] 현재 exe 버전: {local.get('exe_version', '없음')}, "
        f"로컬 이미지: {len(local_image_hashes)}개")

    # ── 서버에 업데이트 체크 요청 ──────────────────
    try:
        log(f"[업데이터] 서버 체크: {UPDATE_SERVER}/check")
        resp = requests.post(
            f"{UPDATE_SERVER}/check",
            json={
                "exe_version": local.get("exe_version", "0.0.0"),
                "image_hashes": local_image_hashes,
            },
            timeout=(TIMEOUT_CONNECT, 15),
        )
        resp.raise_for_status()
        result = resp.json()
        log(f"[업데이터] 서버 응답 OK — "
            f"최신 exe={result.get('latest_exe_version')}, "
            f"이미지 업데이트={len(result.get('images_update', []))}개")
    except Exception as e:
        err(f"[업데이터] 서버 연결 실패: {e}")
        log("[업데이터] 기존 버전으로 매크로 실행")
        launch_macro()
        return

    any_update = False

    # ── exe 업데이트 ───────────────────────────────
    exe_info = result.get("exe_update")
    local_ver = local.get("exe_version", "없음")
    server_ver = result.get("latest_exe_version", "알 수 없음")

    if exe_info:
        new_ver = exe_info["version"]
        log(f"[버전] 로컬={local_ver}  서버={server_ver}  → 업데이트 필요")

        # 기존 exe 수정 날짜 기록
        old_mtime = None
        if os.path.exists(MACRO_EXE):
            old_mtime = os.path.getmtime(MACRO_EXE)
            try:
                shutil.copy2(MACRO_EXE, MACRO_EXE_BACKUP)
                log(f"[업데이터] 기존 exe 백업: {MACRO_EXE_BACKUP}")
            except Exception as e:
                err(f"[업데이터] 백업 실패: {e}")

        # 다운로드
        ok = download_file(
            exe_info["download_url"],
            MACRO_EXE,
            exe_info.get("sha256")
        )
        if ok:
            local["exe_version"] = new_ver
            any_update = True
            new_mtime = os.path.getmtime(MACRO_EXE)
            import datetime
            old_dt = datetime.datetime.fromtimestamp(old_mtime).strftime('%Y-%m-%d %H:%M:%S') if old_mtime else "없음"
            new_dt = datetime.datetime.fromtimestamp(new_mtime).strftime('%Y-%m-%d %H:%M:%S')
            log(f"[버전] exe 업데이트 완료: v{local_ver} → v{new_ver}")
            log(f"[버전] 파일 날짜: {old_dt} → {new_dt}")
        else:
            err("[업데이터] exe 업데이트 실패")
            if os.path.exists(MACRO_EXE_BACKUP):
                try:
                    shutil.copy2(MACRO_EXE_BACKUP, MACRO_EXE)
                    log("[업데이터] 백업에서 복구 완료")
                except Exception as e:
                    err(f"[업데이터] 복구 실패: {e}")
    else:
        log(f"[버전] 로컬={local_ver}  서버={server_ver}  → 최신 버전 (업데이트 없음)")

    # ── 이미지 업데이트 ────────────────────────────
    images_to_update = result.get("images_update", [])
    if images_to_update:
        log(f"[업데이터] 이미지 변경 {len(images_to_update)}개 다운로드 시작")
        os.makedirs(IMAGES_DIR, exist_ok=True)
        success = 0
        fail = 0

        for img in images_to_update:
            dest = os.path.join(IMAGES_DIR, img["filename"])
            ok = download_file(img["download_url"], dest, img.get("sha256"))
            if ok:
                local_image_hashes[img["filename"]] = img["sha256"]
                success += 1
                import datetime
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(dest)).strftime('%Y-%m-%d %H:%M:%S')
                log(f"[업데이터] ✓ 이미지: {img['filename']} (수정날짜: {mtime})")
            else:
                fail += 1
                err(f"[업데이터] ✗ 이미지: {img['filename']}")

        log(f"[업데이터] 이미지 완료: {success}개 성공, {fail}개 실패")
        if success > 0:
            any_update = True
    else:
        log("[업데이터] 이미지 최신 상태")

    # ── 로컬 버전 저장 ─────────────────────────────
    local["image_hashes"] = local_image_hashes
    local["last_check"] = time.strftime('%Y-%m-%d %H:%M:%S')
    save_local_version(local)

    if any_update:
        log("[업데이터] 업데이트 완료!")
    else:
        log("[업데이터] 최신 버전 사용 중")

    # ── 매크로 실행 ────────────────────────────────
    launch_macro()


def launch_macro():
    """혼종_통합_자동.exe 실행"""
    if not os.path.exists(MACRO_EXE):
        err(f"[업데이터] 매크로 없음: {MACRO_EXE}")
        return
    log(f"[업데이터] 매크로 실행: {MACRO_EXE}")
    try:
        subprocess.Popen(
            [MACRO_EXE],
            cwd=os.path.dirname(MACRO_EXE),
            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == 'win32' else 0
        )
        log("[업데이터] 매크로 실행 완료 — updater 종료")
    except Exception as e:
        err(f"[업데이터] 실행 실패: {e}")


# ==================================================
# 진입점
# ==================================================
if __name__ == "__main__":
    try:
        run_updater()
    except Exception as e:
        err(f"[업데이터] 치명적 오류: {e}")
        import traceback
        err(traceback.format_exc())
        # 실패해도 매크로는 실행 시도
        launch_macro()
