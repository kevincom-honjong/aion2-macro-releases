#!/usr/bin/env python3
# update_version.py - GitHub Actions에서 실행
# exe와 images2 파일들의 SHA256을 계산해 version.json 자동 업데이트
#
# 실행: python version_scripts/update_version.py
# (레포 루트에서 실행되어야 함)

import json
import hashlib
import os
from datetime import datetime, timezone

REPO_ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(REPO_ROOT, "version.json")
EXE_DIR      = os.path.join(REPO_ROOT, "exe")
IMAGES_DIR   = os.path.join(REPO_ROOT, "images2")
EXE_FILENAME = "혼종_통합_자동.exe"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def bump_patch(version: str) -> str:
    """1.0.5 → 1.0.6"""
    parts = version.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def main():
    # version.json 로드
    with open(VERSION_FILE, "r", encoding="utf-8") as f:
        ver = json.load(f)

    changed = False

    # ── exe 처리 ──────────────────────────────────
    exe_path = os.path.join(EXE_DIR, EXE_FILENAME)
    if os.path.exists(exe_path):
        new_hash = sha256_file(exe_path)
        old_hash = ver["exe"].get("sha256", "")

        if new_hash != old_hash:
            old_version = ver["exe"]["version"]
            new_version = bump_patch(old_version)
            ver["exe"]["version"]  = new_version
            ver["exe"]["sha256"]   = new_hash
            ver["exe"]["filename"] = EXE_FILENAME
            # download_url는 유지 (이미 올바른 GitHub raw URL이 있어야 함)
            print(f"[exe] 버전 업데이트: {old_version} → {new_version} (hash 변경)")
            changed = True
        else:
            print(f"[exe] 변경 없음 (v{ver['exe']['version']})")
    else:
        print(f"[exe] 파일 없음: {exe_path}")

    # ── images2 처리 ──────────────────────────────
    if os.path.exists(IMAGES_DIR):
        new_images: dict = {}
        for fname in sorted(os.listdir(IMAGES_DIR)):
            fpath = os.path.join(IMAGES_DIR, fname)
            if os.path.isfile(fpath):
                new_images[fname] = sha256_file(fpath)

        old_images = ver.get("images", {})
        added   = set(new_images) - set(old_images)
        removed = set(old_images) - set(new_images)
        modified = {
            k for k in new_images
            if k in old_images and new_images[k] != old_images[k]
        }

        if added or removed or modified:
            ver["images"] = new_images
            ver["images_version"] = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            print(f"[images] 변경 감지: +{len(added)} -{len(removed)} ~{len(modified)}")
            if added:    print(f"  추가: {', '.join(sorted(added))}")
            if removed:  print(f"  삭제: {', '.join(sorted(removed))}")
            if modified: print(f"  수정: {', '.join(sorted(modified))}")
            changed = True
        else:
            print(f"[images] 변경 없음 ({len(new_images)}개)")
    else:
        print(f"[images] 디렉토리 없음: {IMAGES_DIR}")

    # ── version.json 저장 ─────────────────────────
    if changed:
        ver["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            json.dump(ver, f, indent=2, ensure_ascii=False)
        print(f"\n✓ version.json 업데이트 완료")
        print(f"  exe: v{ver['exe']['version']} | images: {ver.get('images_version', '')}")
    else:
        print("\n변경 없음 — version.json 유지")


if __name__ == "__main__":
    main()
