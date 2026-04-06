# server/main.py - AION2 매크로 자동 업데이트 서버
# Railway에 배포, 클라이언트는 /check로 버전 확인 후 GitHub에서 직접 파일 다운로드
#
# 환경변수 필요:
#   GITHUB_OWNER   - GitHub 사용자명
#   GITHUB_REPO    - 레포 이름 (예: aion2-macro-releases)
#   GITHUB_BRANCH  - 브랜치 (기본: main)
#   GITHUB_TOKEN   - private 레포이면 필요 (public이면 없어도 됨)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import os
import json
import time
import logging
from datetime import datetime
from typing import Dict, Optional, List
from pydantic import BaseModel

# ==================================================
# 앱 설정
# ==================================================
app = FastAPI(
    title="AION2 Macro Updater",
    description="매크로 자동 업데이트 서버",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
log = logging.getLogger(__name__)

# ==================================================
# 설정 (환경변수)
# ==================================================
GITHUB_OWNER  = os.getenv("GITHUB_OWNER", "kevincom-honjong")
GITHUB_REPO   = os.getenv("GITHUB_REPO",  "aion2-macro-releases")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH","main")
GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN", "")

# GitHub raw 파일 베이스 URL
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}"
VERSION_JSON_URL = f"{RAW_BASE}/version.json"

# ==================================================
# version.json 캐시 (30초)
# ==================================================
_cache: Dict = {"data": None, "ts": 0.0}
CACHE_TTL = 30


async def fetch_version_json() -> dict:
    """GitHub에서 version.json 가져오기 (30초 캐시)"""
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    headers = {"Cache-Control": "no-cache"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(VERSION_JSON_URL, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            _cache["data"] = data
            _cache["ts"] = now
            log.info(f"version.json 갱신: exe={data['exe']['version']}, "
                     f"images={len(data.get('images', {}))}개")
            return data
    except Exception as e:
        log.error(f"version.json 가져오기 실패: {e}")
        if _cache["data"]:
            log.warning("캐시된 버전 사용")
            return _cache["data"]
        raise HTTPException(status_code=503, detail=f"version.json 가져오기 실패: {e}")


# ==================================================
# 요청/응답 모델
# ==================================================
class CheckRequest(BaseModel):
    exe_version: Optional[str] = None         # 클라이언트 현재 exe 버전
    image_hashes: Optional[Dict[str, str]] = None  # {파일명: sha256}


class ExeUpdateInfo(BaseModel):
    version: str
    filename: str
    sha256: str
    download_url: str


class ImageUpdateInfo(BaseModel):
    filename: str
    sha256: str
    download_url: str


class CheckResponse(BaseModel):
    exe_update: Optional[ExeUpdateInfo] = None    # None이면 최신 버전
    images_update: List[ImageUpdateInfo] = []     # 빈 리스트면 업데이트 없음
    latest_exe_version: str
    latest_images_version: str
    server_ts: str


# ==================================================
# 라우트
# ==================================================
@app.get("/")
async def root():
    return {
        "service": "AION2 Macro Updater Server",
        "status": "ok",
        "ts": datetime.utcnow().isoformat(),
        "github": f"{GITHUB_OWNER}/{GITHUB_REPO}",
    }


@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


@app.get("/version")
async def get_version():
    """현재 최신 version.json 전체 반환"""
    return await fetch_version_json()


@app.post("/check", response_model=CheckResponse)
async def check_updates(req: CheckRequest):
    """
    클라이언트 버전 체크.
    요청: 현재 exe 버전 + 이미지 해시 목록
    응답: 업데이트 필요한 항목과 다운로드 URL
    """
    ver = await fetch_version_json()
    server_exe = ver["exe"]
    server_images: Dict[str, str] = ver.get("images", {})

    # exe 버전 비교
    exe_update = None
    if req.exe_version != server_exe["version"]:
        log.info(f"exe 업데이트: {req.exe_version} → {server_exe['version']}")
        exe_update = ExeUpdateInfo(
            version=server_exe["version"],
            filename=server_exe["filename"],
            sha256=server_exe["sha256"],
            download_url=server_exe["download_url"],
        )

    # 이미지 diff 계산
    client_hashes: Dict[str, str] = req.image_hashes or {}
    images_update: List[ImageUpdateInfo] = []

    for filename, server_hash in server_images.items():
        client_hash = client_hashes.get(filename, "")
        if client_hash != server_hash:
            images_update.append(ImageUpdateInfo(
                filename=filename,
                sha256=server_hash,
                download_url=f"{RAW_BASE}/images2/{filename}",
            ))

    if images_update:
        log.info(f"이미지 업데이트 {len(images_update)}개")

    return CheckResponse(
        exe_update=exe_update,
        images_update=images_update,
        latest_exe_version=server_exe["version"],
        latest_images_version=ver.get("images_version", ""),
        server_ts=datetime.utcnow().isoformat(),
    )


# ==================================================
# 실행 (로컬 테스트용)
# ==================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
