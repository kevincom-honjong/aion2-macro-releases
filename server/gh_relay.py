# -*- coding: utf-8 -*-
"""GitHub 중계 (2026-09-26 주인님 #255 «서버 중계로 바꾼 뒤 비공개»).

릴리스 저장소 kevincom-honjong/aion2-macro-releases 를 ★비공개★ 로 돌리려면 함대가 GitHub 을 직접 읽으면 안 된다.
  · 서버만 GitHub 을 읽는다 — Railway env ★GH_READ_TOKEN★(읽기 전용 fine-grained, 그 저장소 하나 · Contents: Read).
    토큰은 로그·응답·예외 문구 어디에도 안 나간다(헤더에만 실린다). 없으면 예전처럼 무인증(공개일 동안은 그대로 된다).
  · 함대는 서버에서 받는다 — /check 가 ★서명된 기한부 주소★ /dl/<tok>/<asset> 를 준다.
    업데이터 download_file 은 헤더를 안 보낸다(키 없음) → 키를 요구할 수 없어서 /check(키 검사함)가 발급한 서명으로 대신한다.
    ★토큰을 경로에 넣는다★ — 업데이터는 내부망 시드 주소를 download_url 의 끝 조각으로 만든다(rsplit('/')) → 끝 조각은 그대로 macro-<ver>.exe.
  · 77MB × 24대가 GitHub 을 두드리지 않게 ★디스크 캐시★(볼륨) — version.json 의 sha256 과 맞는 파일만 둔다·내준다. 이름당 잠금 하나.
"""
import hashlib
import hmac
import base64
import os
import re
import threading
import time
import urllib.parse

import httpx

REPO = "kevincom-honjong/aion2-macro-releases"
GH_READ_TOKEN = os.getenv("GH_READ_TOKEN", "").strip()
_GH_HOSTS = ("api.github.com", "raw.githubusercontent.com", "github.com")

DL_TTL_S = 6 * 3600                      # 서명 주소 수명 — /check 는 5분마다, 받는 건 그 직후
DL_KEEP = 4                              # 캐시에 남길 파일 수(지금 판은 늘 남긴다)
DL_DEADLINE_S = 150                      # 한 번 채우기 전체 상한 — 업데이터는 첫 바이트를 180초 기다린다(반증 M2)
ASSET_RE = re.compile(r"^(?:(?:macro|rental)-\d+(?:\.\d+){1,3}\.exe|updater\.exe)$")


def gh_headers(url: str, extra: dict | None = None) -> dict:
    """GitHub 호스트에만 토큰을 싣는다 — jsDelivr·S3(서명 주소) 등 다른 곳에는 절대 안 보낸다."""
    h = dict(extra or {})
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if GH_READ_TOKEN and host in _GH_HOSTS:
        h["Authorization"] = "Bearer " + GH_READ_TOKEN
    return h


def token_state() -> str:
    """진단용 — 값은 절대 안 준다. 'set' / 'unset'."""
    return "set" if GH_READ_TOKEN else "unset"


# ── 서명 주소 ──────────────────────────────────────────────────────────
def _key(secret: bytes) -> bytes:
    return hmac.new(secret, b"aion2-dl-v1", hashlib.sha256).digest()


def sign(secret: bytes, asset: str, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + DL_TTL_S)
    mac = hmac.new(_key(secret), f"{asset}|{exp}".encode(), hashlib.sha256).digest()[:12]
    return f"{exp:x}.{base64.urlsafe_b64encode(mac).decode().rstrip('=')}"


def verify(secret: bytes, asset: str, tok: str, now: float | None = None) -> bool:
    try:
        exp_s, sig = str(tok).split(".", 1)
        exp = int(exp_s, 16)
    except (ValueError, TypeError):
        return False
    if exp < (now if now is not None else time.time()):
        return False
    mac = hmac.new(_key(secret), f"{asset}|{exp}".encode(), hashlib.sha256).digest()[:12]
    return hmac.compare_digest(base64.urlsafe_b64encode(mac).decode().rstrip("="), sig)


# ── 상류 주소 ──────────────────────────────────────────────────────────
def expected_sha(ver: dict, asset: str) -> str | None:
    """그 이름이 지금 version.json 의 판이면 sha256, 아니면 None(=안 내준다)."""
    ver = ver or {}
    if asset == "updater.exe":
        return (ver.get("updater") or {}).get("sha256") or None
    m = re.match(r"^(macro|rental)-(.+)\.exe$", asset)
    if not m:
        return None
    info = ver.get("exe" if m.group(1) == "macro" else "rental") or {}
    return info.get("sha256") if info.get("version") == m.group(2) else None


def _release_asset_url(client: httpx.Client, asset: str, timeout: float = 20.0) -> str | None:
    """비공개 저장소의 릴리스 에셋 — API 로 에셋 id 를 찾아 octet-stream 주소를 쓴다(토큰 필요)."""
    m = re.match(r"^(?:macro|rental)-(.+)\.exe$", asset)
    if not m:
        return None
    u = f"https://api.github.com/repos/{REPO}/releases/tags/v{m.group(1)}"
    r = client.get(u, headers=gh_headers(u, {"Accept": "application/vnd.github+json"}), timeout=timeout)
    if r.status_code != 200:
        return None
    for a in r.json().get("assets") or []:
        if a.get("name") == asset and a.get("url"):
            return a["url"]
    return None


def upstreams(client: httpx.Client, asset: str, api_timeout: float = 20.0) -> list:
    """(이름, 주소, 헤더) 순서대로. 토큰이 있으면 인증 경로가 먼저, 없으면 공개 경로(공개일 동안)."""
    out = []
    if asset == "updater.exe":
        raw = f"https://raw.githubusercontent.com/{REPO}/main/exe/updater.exe"
        api = f"https://api.github.com/repos/{REPO}/contents/exe/updater.exe?ref=main"
        out.append(("raw", raw, gh_headers(raw)))
        if GH_READ_TOKEN:
            out.append(("ghapi", api, gh_headers(api, {"Accept": "application/vnd.github.raw"})))
        return out
    m = re.match(r"^(?:macro|rental)-(.+)\.exe$", asset)
    if not m:
        return out
    if GH_READ_TOKEN:
        try:
            au = _release_asset_url(client, asset, timeout=api_timeout)
        except (httpx.HTTPError, ValueError):     # 깨진 JSON 도(반증 L3 — 500 이던 것)
            au = None
        if au:
            out.append(("release-api", au, gh_headers(au, {"Accept": "application/octet-stream"})))
    pub = f"https://github.com/{REPO}/releases/download/v{m.group(1)}/{asset}"
    out.append(("release-public", pub, gh_headers(pub)))
    return out


# ── 디스크 캐시 ────────────────────────────────────────────────────────
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _lock(asset: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(asset, threading.Lock())


def _sha_file(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def cached_path(cache_dir: str, asset: str, sha: str) -> str:
    return os.path.join(cache_dir, f"{sha[:16]}-{asset}")


def ensure(cache_dir: str, asset: str, sha: str, client: httpx.Client | None = None, log=print) -> tuple:
    """(경로 또는 None, 사유). 캐시에 sha 가 맞는 파일이 있으면 그것, 없으면 상류에서 받아 sha 검증 뒤 넣는다.
    같은 이름은 한 번에 하나만 받는다(24대가 동시에 와도 GitHub 은 한 번)."""
    if not ASSET_RE.match(asset or "") or not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        return None, "bad-asset"
    os.makedirs(cache_dir, exist_ok=True)
    dest = cached_path(cache_dir, asset, sha)
    if os.path.isfile(dest):
        try:
            os.utime(dest, None)                        # 쓰는 판은 정리(_prune)에서 가장 늦게 지운다
        except OSError:
            pass
        return dest, "hit"
    with _lock(asset):
        if os.path.isfile(dest):                        # 앞사람이 방금 채웠다
            return dest, "hit"
        own = client is None
        client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))
        errs = []
        t0 = time.monotonic()

        def _left() -> float:
            return DL_DEADLINE_S - (time.monotonic() - t0)
        try:
            # ★반증 2차(아이온2 2026-09-26)★ 상한을 조각 사이에서만 보면 멈춘 상류는 읽기 한 번에 120초를 더 먹었다
            #   (실측 5초 상한에 120.5초) → 모든 대기(API·연결·읽기)를 남은 시간으로 자른다.
            for name, url, hdr in upstreams(client, asset, api_timeout=max(0.5, min(20.0, _left()))):
                if _left() <= 0.5:
                    errs.append(f"{name}:deadline")
                    break
                tmp = dest + ".part"
                try:
                    h = hashlib.sha256()
                    _to = httpx.Timeout(max(0.5, min(120.0, _left())), connect=max(0.5, min(30.0, _left())))
                    with client.stream("GET", url, headers=hdr, timeout=_to) as r:
                        if r.status_code != 200:
                            errs.append(f"{name}:{r.status_code}")
                            continue
                        with open(tmp, "wb") as f:
                            for chunk in r.iter_bytes(1 << 16):
                                if _left() <= 0:
                                    raise TimeoutError("deadline")   # OSError 라 아래에서 이름만 적힌다
                                f.write(chunk)
                                h.update(chunk)
                    if h.hexdigest() != sha:
                        errs.append(f"{name}:sha")
                        os.remove(tmp)
                        continue
                    os.replace(tmp, dest)
                    _prune(cache_dir, keep=dest)
                    log(f"[dl] {asset} 캐시 채움 ← {name} ({os.path.getsize(dest)} B)")
                    return dest, name
                except (httpx.HTTPError, OSError) as e:
                    errs.append(f"{name}:{e.__class__.__name__}")   # 예외 문구는 안 싣는다(주소·헤더가 섞일 수 있다)
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        finally:
            if own:
                client.close()
        log(f"[dl] {asset} 상류 전부 실패: {errs}")
        return None, ",".join(errs) or "no-upstream"


def _prune(cache_dir: str, keep: str) -> None:
    try:
        files = [os.path.join(cache_dir, f) for f in os.listdir(cache_dir) if not f.endswith(".part")]
    except OSError:
        return
    def _mt(p):
        try:
            return os.path.getmtime(p)
        except OSError:                                 # 다른 정리가 방금 지웠다(반증 L3)
            return 0.0
    files.sort(key=_mt, reverse=True)
    for p in files[DL_KEEP:]:
        if p != keep:
            try:
                os.remove(p)
            except OSError:
                pass
