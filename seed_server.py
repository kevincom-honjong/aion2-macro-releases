# ─────────────────────────────────────────────────────────────────────────────
# 내부망 업데이트 시드 (2026-08-06) — updater/exe/ 를 GitHub 에셋 이름으로 서빙
#
# 왜: 함대 20대가 업데이트마다 각자 인터넷으로 73MB(합 1.5GB)를 받는다. 회선이 약한
#     PC는 다운로드가 실패하기도 한다. 이 시드가 있으면 내부망에서 받는다.
# 어떻게: updater 3.0.8이 서버 /check 응답의 lan_seed 주소로 GET /macro-<버전>.exe 를
#     먼저 시도한다. 여기서는 그 요청을 version.json의 현재 버전과 대조해 exe/혼종_*.exe로
#     매핑 서빙한다. ★버전 불일치는 404★ — 낡은/앞선 요청은 GitHub로 보낸다.
# 안전: 업데이터가 서버 /check의 SHA256으로 검증하므로 시드가 변조돼도 기각→GitHub 폴백.
# 실행: python seed_server.py   (개발 PC, 포트 8766. start_seed.bat 참조)
# ─────────────────────────────────────────────────────────────────────────────
import json
import os
import re
import shutil
import socket
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = os.path.dirname(os.path.abspath(__file__))
EXE_DIR = os.path.join(BASE, "exe")
VERSION_JSON = os.path.join(BASE, "version.json")
PORT = 8766
MAP = {"macro": ("exe", "혼종_통합_자동.exe"),
       "rental": ("rental", "혼종_렌탈.exe")}

# ─────────────────────────────────────────────────────────────────────────────
# ★내부망 IP 자동 등록(2026-08-14)★ — DHCP가 개발 PC 내부망 IP를 바꾸면 서버에 등록된
#   lan_seed(예 172.30.1.81)가 낡아 함대가 시드를 못 찾는다(실사고: .81→.42로 바뀜).
#   이 스레드가 자기 내부망 IP를 감지해, 서버 lan_seed와 다르면 자동으로 갱신한다.
#   → IP가 또 바뀌어도 최대 CHECK_EVERY 안에 스스로 복구된다.
#
#   ★비번은 이 소스에 절대 넣지 않는다★ — seed_server.py는 공개 릴리스 레포에 커밋된다.
#   대시보드 비번은 (1) 환경변수 AION2_PW  또는 (2) 개발 PC 로컬 파일 seed_secret.txt
#   (레포에서 .gitignore로 제외)에서만 읽는다. 둘 다 없으면 자동등록을 끄고 콘솔로 안내만
#   한다(서빙 자체는 정상 — 자동 IP 갱신만 비활성).
# ─────────────────────────────────────────────────────────────────────────────
CONTROL_URL = os.getenv("AION2_BASE") or "https://web-production-8d4c.up.railway.app"
CHECK_EVERY = 300     # 초 — 내부망 IP 변화 감시 주기

# ═════════════════════════════════════════════════════════════════════════════
# ★★사고 183 (2026-08-24) — 올바른 exe 를 손에 쥐고도 거절했다★★
#
# ★주인님 로그★
#   [업데이트] 매크로 exe: 1.1.656 → 1.1.657
#   [다운로드] 시드 실패(404 version mismatch ... macro-1.1.657.exe) → GitHub 폴백
#   주인님: "자꾸 내부망 실패뜨네"
#
# ★무엇이 틀렸나★ 이 서버는 ★로컬 version.json★ 을 읽어 요청 버전과 비교했다.
#   그런데 배포 흐름은 이렇다:
#     ① 내가 exe 를 푸시      → 디스크의 exe 는 ★이미 새 빌드★
#     ② CI 가 version.json 을 ★원격에★ 올린다 (1.1.657)
#     ③ 함대가 /check 로 1.1.657 을 알고 시드에 달라고 한다
#     ④ 그런데 이 PC 의 로컬 version.json 은 아직 1.1.656 (pull 안 했으니까)
#   → ★파일은 맞는데 표가 낡아서 404.★ 실측 2026-08-24 04:17 exe = 657 빌드,
#     로컬 version.json = 656, 원격 = 657.
#   ★배포할 때마다 난다★ — '푸시 뒤 git pull' 은 사람 기억에 의존하는 절차라
#   반드시 빠뜨린다(§A6). 그래서 코드로 막는다.
#
# ★고치는 법 — 버전 문자열이 아니라 ★실제 바이트★ 로 판정한다★
#   원격(origin/main)의 version.json 을 fetch 해서 읽고(작업 트리는 안 건드린다),
#   거기 적힌 sha256 과 ★디스크 exe 의 실제 sha256★ 이 같을 때만 준다.
#   이게 더 안전하다 — 버전 문자열이 맞아도 파일이 옛것이면 함대가 받아가서
#   업데이터의 해시 검증에서 튕긴다(사고 38 계열). 여기서 미리 막는다.
#
# ★git pull 을 안 쓰는 이유★ 이 PC 는 내가 계속 커밋·리베이스를 하는 개발 PC 다.
#   백그라운드에서 작업 트리를 바꾸면 내 작업과 충돌한다. fetch + show 는 읽기만 한다.
# ═════════════════════════════════════════════════════════════════════════════
REMOTE_REF = 'origin/main'
_remote_cache = {'at': 0.0, 'data': None}
_sha_cache = {}          # path → (mtime, size, sha256)


def _remote_version_json(max_age: float = 120.0):
    """원격 version.json — fetch 후 show 로 읽는다(작업 트리 무변경). 실패하면 None."""
    import subprocess
    now = time.time()
    if _remote_cache['data'] is not None and now - _remote_cache['at'] < max_age:
        return _remote_cache['data']
    try:
        subprocess.run(['git', '-C', BASE, 'fetch', '-q', 'origin', 'main'],
                       timeout=60, capture_output=True)
        out = subprocess.run(['git', '-C', BASE, 'show', REMOTE_REF + ':version.json'],
                             timeout=30, capture_output=True)
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout.decode('utf-8'))
    except Exception as e:
        print(f'[시드] 원격 version.json 읽기 실패(로컬로 폴백): {e}', flush=True)
        return None
    _remote_cache['at'] = now
    _remote_cache['data'] = data
    return data


def _file_sha256(path: str) -> str:
    """파일 sha256 — (mtime, size) 가 같으면 캐시. 76MB 를 매 요청 해싱하지 않는다."""
    import hashlib
    try:
        st = os.stat(path)
    except OSError:
        return ''
    key = (st.st_mtime_ns, st.st_size)
    hit = _sha_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for blk in iter(lambda: f.read(1 << 20), b''):
                h.update(blk)
    except OSError:
        return ''
    v = h.hexdigest()
    _sha_cache[path] = (key, v)
    return v

# ============================================================================
# ★★사고 248 (2026-08-27) — 디스크가 ★앞서면★ 시드가 통째로 죽는다★★
#
# ★주인님 로그★
#   [업데이트] 매크로 exe: 1.1.701 → 1.1.702
#   [다운로드] 시드 실패(404 exe hash mismatch) → GitHub 폴백
#   주인님: "몇번을 말해야 알아들을래?"
#
# ★무엇이 틀렸나★ 사고 183 은 '디스크가 ★뒤처진★ 경우'를 고쳐놓았다.
#   그런데 이번은 반대였다 — exe 를 빌드해 커밋만 하고 ★푸시를 안 했다★.
#     디스크 exe = 1.1.703 이 될 바이트   (서버는 모릅니다)
#     서버 version.json = 1.1.702
#     함대 24대가 macro-1.1.702.exe 를 달라고 한다 → 바이트가 없다 → 404
#   ★내가 빌드한 순간부터 푸시할 때까지, 전 함대의 내부망 업데이트가 죽는다.★
#   그 창이 문제가 아니라, 창 안에서 사람이 [업데이트] 를 누르면
#   20대가 각자 GitHub 로 73MB 씩 받는다(합 1.5GB). 회선 약한 PC 는 실패한다.
#
# ★고치는 법 — 버전별로 ★보관★한다★
#   디스크 exe 가 '서버가 아는 현재 버전' 일 때 그 바이트를 seed_cache/ 에
#   해시 이름으로 복사해 둔다. 다음 빌드가 덮어써도 지난 버전을 그대로 내보낸다.
#   지난 버전의 sha 는 version.json 에 안 남으므로, 보관할 때 우리가 색인에 적는다
#   (그 순간엔 원격 version.json 이 그 값을 보증했다).
#
# ★그리고 시끄럽게 말한다★ 디스크 exe 가 아무 버전과도 안 맞으면
#   = 미푸시 빌드가 앉아 있다는 뜻. 콘솔에 경고하고 /seedstat 으로 노출한다
#   (fleet.py 가 세션 시작 때 배너로 보여준다 — §A6: 기억이 아니라 코드로).
# ============================================================================
CACHE_DIR = os.path.join(BASE, 'seed_cache')
CACHE_INDEX = os.path.join(CACHE_DIR, 'index.json')
CACHE_KEEP = 4                       # 종류별 보관 개수 (76MB x 4)
SNAP_EVERY = 60.0                    # 초 — 디스크 exe 감시 주기
_seedstat = {'ok': None, 'why': '아직 확인 안 함', 'at': 0.0,
             'disk': {}, 'served': {}}


def _index_load() -> dict:
    try:
        with open(CACHE_INDEX, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _index_put(kind: str, ver: str, sha: str):
    d = _index_load()
    d.setdefault(kind, {})[ver] = sha
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_INDEX + '.part'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CACHE_INDEX)
    except Exception as e:
        print('[시드] 색인 기록 실패(무해): ' + str(e), flush=True)


def _index_get(kind: str, ver: str) -> str:
    return str((_index_load().get(kind) or {}).get(ver) or '').lower()


def _cache_path(kind: str, sha: str) -> str:
    return os.path.join(CACHE_DIR, kind + '-' + sha[:16] + '.exe')


def _cache_prune(kind: str):
    try:
        fs = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)
              if f.startswith(kind + '-') and f.endswith('.exe')]
    except OSError:
        return
    fs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for p in fs[CACHE_KEEP:]:
        try:
            os.remove(p)
            print('[시드] 보관함 정리: ' + os.path.basename(p), flush=True)
        except OSError:
            pass


def _cache_snapshot(kind: str, live_path: str, ver: str, sha: str) -> bool:
    """디스크 exe 가 서버가 아는 버전이면 그 바이트를 보관한다."""
    if not (ver and sha):
        return False
    dst = _cache_path(kind, sha)
    if os.path.exists(dst) and _file_sha256(dst) == sha:
        if _index_get(kind, ver) != sha:
            _index_put(kind, ver, sha)
        return True
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = dst + '.part'
        shutil.copyfile(live_path, tmp)
        os.replace(tmp, dst)
    except Exception as e:
        print('[시드] 보관 실패(무해, GitHub 폴백은 살아있음): ' + str(e), flush=True)
        return False
    _index_put(kind, ver, sha)
    print('[시드] 보관: ' + kind + '-' + ver + ' (' + sha[:12] + ') — '
          '다음 빌드 뒤에도 이 버전을 내보냅니다', flush=True)
    _cache_prune(kind)
    return True


def _find_bytes(kind: str, live_path: str, want_sha: str) -> str:
    """want_sha 와 바이트가 같은 파일 경로. 없으면 ''. 디스크 → 보관함 순."""
    if not want_sha:
        return ''
    if _file_sha256(live_path) == want_sha:
        return live_path
    p = _cache_path(kind, want_sha)
    if os.path.exists(p) and _file_sha256(p) == want_sha:
        return p
    return ''


def _snapshot_loop():
    """디스크 exe 를 감시해 ①현재 버전이면 보관 ②아무것도 아니면 경고."""
    warned_at = 0.0
    while True:
        try:
            ver = _remote_version_json(max_age=SNAP_EVERY)
            if ver is None:
                try:
                    ver = json.load(open(VERSION_JSON, encoding='utf-8'))
                except Exception:
                    ver = None
            if ver is not None:
                disk, served, bad = {}, {}, []
                for kind, (section, fname) in MAP.items():
                    live = os.path.join(EXE_DIR, fname)
                    if not os.path.exists(live):
                        continue
                    sec = ver.get(section) or {}
                    cur_ver = str(sec.get('version') or '')
                    cur_sha = str(sec.get('sha256') or '').lower()
                    live_sha = _file_sha256(live)
                    disk[kind] = live_sha[:12]
                    if cur_sha and live_sha == cur_sha:
                        _cache_snapshot(kind, live, cur_ver, cur_sha)
                    if cur_sha and _find_bytes(kind, live, cur_sha):
                        served[kind] = cur_ver
                    else:
                        bad.append(kind + ' ' + (cur_ver or '?'))
                _seedstat.update({'ok': not bad, 'at': time.time(),
                                  'disk': disk, 'served': served,
                                  'why': ('' if not bad else
                                          '배포본 바이트가 없음: ' + ', '.join(bad))})
                if bad and time.time() - warned_at > 600:
                    warned_at = time.time()
                    print('[시드] ⚠️ 함대가 받을 버전의 바이트가 여기 없습니다 — '
                          + ', '.join(bad) + '. 내부망 시드가 무력화되어 '
                          '전 함대가 GitHub 로 받습니다. '
                          '★빌드해 놓고 푸시를 안 했는지 확인하세요(git status / git push)★',
                          flush=True)
        except Exception as e:
            print('[시드] 보관 루프 예외(계속): ' + str(e), flush=True)
        time.sleep(SNAP_EVERY)

_SECRET_FILE = os.path.join(BASE, "seed_secret.txt")


def _dash_pw() -> str:
    """대시보드 비번 — env 우선, 없으면 로컬 gitignore 파일. 없으면 빈 문자열."""
    pw = os.getenv("AION2_PW") or ""
    if not pw:
        try:
            with open(_SECRET_FILE, encoding="utf-8") as f:
                pw = f.read().strip()
        except Exception:
            pw = ""
    return pw


LAN_PREFIX = "172.30."      # ★함대 내부망 대역 (실측 확정)★


def _lan_ip() -> str:
    """★함대 내부망 대역(172.30.x) IP만★ 고른다. 없으면 빈 문자열.

    ★★2026-08-16 실사고 — '아무 IP나 올리기'는 재앙이다★★
    사용자: "또 내부망시드를 192. 이지랄하고잇네"
    이 PC는 내부망 Wi-Fi(172.30.1.99)와 이더넷(192.168.1.12)을 둘 다 갖고 있다.
    Wi-Fi 가 끊기자 옛 코드가 `cands[0]`(=이더넷 192.168.1.12)을 골라 lan_seed 로
    올렸다. 함대는 172.30.1.x 라 그 주소에 영영 못 닿는다 →
    ★20대가 전부 3초씩 헛기다린 뒤 GitHub 폴백★ (치명적이진 않지만 업데이트가 기어간다).
    ★틀린 주소를 올리는 것은 아무것도 안 올리는 것보다 나쁘다★ — 그래서 추측하지 않는다.
    """
    cands = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                cands.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        cands.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    for ip in cands:
        if ip.startswith(LAN_PREFIX):
            return ip
    if cands:
        print(f"[시드] ⚠️ 내부망({LAN_PREFIX}x) IP 없음 — 가진 건 {sorted(set(cands))}. "
              "내부망 Wi-Fi 연결을 확인하세요. ★엉뚱한 주소를 올리지 않습니다★", flush=True)
    return ""


def _auto_register_loop():
    """자기 내부망 IP를 서버 lan_seed와 대조해 다르면 갱신 (비번 로그인 → 쿠키 → POST)."""
    pw = _dash_pw()
    if not pw:
        print("[시드] 자동 IP 등록 비활성 — 대시보드 비번 없음 "
              f"(env AION2_PW 또는 {os.path.basename(_SECRET_FILE)} 파일에 넣으면 켜짐). "
              "IP가 바뀌면 수동으로 lan_seed를 갱신하세요.", flush=True)
        return
    import http.cookiejar
    last_pushed = None
    miss = 0            # 내부망 IP 연속 미검출 횟수 (Wi-Fi 순간 끊김에 반응하지 않도록)
    MISS_LIMIT = 3      # 이만큼 연속으로 없으면 서버 lan_seed 를 비운다 (= 전 함대 GitHub 복귀)
    while True:
        try:
            ip = _lan_ip()
            want = f"http://{ip}:{PORT}" if ip else ""
            # ★내부망 IP 가 없으면 '비우기'가 정답★ — 죽은 주소를 남겨두면 함대 20대가
            #   매번 3초씩 헛기다린다. 비우면 곧장 GitHub 로 간다(2026-08-16 실사고).
            if not want:
                miss += 1
                if miss >= MISS_LIMIT and last_pushed != "":
                    want = ""          # 아래 공통 경로로 내려가 빈 값을 POST 한다
                else:
                    time.sleep(CHECK_EVERY)
                    continue
            else:
                miss = 0
            if want != last_pushed:
                cj = http.cookiejar.CookieJar()
                op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
                # 로그인 → 세션 쿠키
                op.open(urllib.request.Request(
                    CONTROL_URL + "/auth/login",
                    data=json.dumps({"password": pw}).encode(),
                    headers={"Content-Type": "application/json"}), timeout=10).read()
                # 현재 서버 값 확인 (같으면 POST 생략 — 서버 재배포 로그 노이즈 방지)
                cur = ""
                try:
                    r = op.open(CONTROL_URL + "/setting/lan_seed", timeout=10).read()
                    cur = (json.loads(r) or {}).get("value") or ""
                except Exception:
                    cur = ""
                if cur != want:
                    op.open(urllib.request.Request(
                        CONTROL_URL + "/setting/lan_seed",
                        data=json.dumps({"value": want}).encode(),
                        headers={"Content-Type": "application/json"}), timeout=10).read()
                    if want:
                        print(f"[시드] 내부망 IP 등록 갱신: {cur or '(없음)'} -> {want}", flush=True)
                    else:
                        print(f"[시드] ⚠️ lan_seed 비움 ({cur} 였음) — 내부망 IP를 "
                              f"{MISS_LIMIT}회 연속 못 찾았습니다. 함대는 GitHub 로 받습니다. "
                              "내부망 Wi-Fi 를 다시 연결하면 자동 복구됩니다.", flush=True)
                last_pushed = want
        except Exception as e:
            print(f"[시드] 자동 IP 등록 실패(재시도 예정): {e}", flush=True)
        time.sleep(CHECK_EVERY)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/seedstat':
            # ★내부망 시드가 진짜로 쓸모 있는지 — fleet.py 가 세션 시작 때 읽는다★
            body = json.dumps(_seedstat, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        m = re.match(r"^/(macro|rental)-([\d.]+)\.exe$", self.path)
        if not m:
            self.send_error(404)
            return
        kind, req_ver = m.group(1), m.group(2)
        section, fname = MAP[kind]
        path = os.path.join(EXE_DIR, fname)
        if not os.path.exists(path):
            self.send_error(404, "no local exe")
            return
        # ★★버전 문자열이 아니라 ★실제 바이트★ 로 판정한다 (사고 183)★★
        #   원격 version.json 이 우선. 못 읽으면 로컬로 폴백(옛 동작).
        ver = _remote_version_json()
        _src = 'origin'
        if ver is None:
            try:
                ver = json.load(open(VERSION_JSON, encoding='utf-8'))
                _src = 'local'
            except Exception:
                self.send_error(503)
                return
        sec = ver.get(section) or {}
        cur_ver = str(sec.get('version') or '')
        cur_sha = str(sec.get('sha256') or '').lower()
        # ★요청된 버전의 기대 해시를 정한다 (사고 248)★
        #   현재 버전 → 원격 version.json 이 정본.
        #   지난 버전 → 보관함 색인(그 버전이 현재였을 때 원격이 보증한 값).
        want_sha = cur_sha if (cur_ver and req_ver == cur_ver) else ''
        _how = 'current'
        if not want_sha:
            want_sha = _index_get(kind, req_ver)
            _how = 'cache-index'
        if not want_sha:
            if not cur_sha and req_ver == cur_ver:
                want_sha = ''           # 해시 없는 옛 포맷 → 버전 문자열로만
            else:
                print('[시드] ' + req_ver + ' 요청 — 모르는 버전(' + _src + ' = '
                      + (cur_ver or '?') + ') → GitHub 로', flush=True)
                self.send_error(404, 'unknown version')
                return
        if want_sha:
            found = _find_bytes(kind, path, want_sha)
            if not found:
                # ★그 버전의 바이트가 여기 없다★ — 빌드 중이거나, 내가 다음 빌드를
                #   덮어쓰고 푸시를 안 했거나(사고 248), 보관함이 정리된 것.
                #   반쪽 파일을 주느니 GitHub 로 보낸다.
                print('[시드] ' + req_ver + ' 요청 — 해당 바이트 없음('
                      + _how + ' ' + want_sha[:12] + ', 디스크 '
                      + (_file_sha256(path)[:12] or '?') + ') → GitHub 로', flush=True)
                self.send_error(404, 'exe hash mismatch')
                return
            path = found
        elif req_ver != cur_ver:
            self.send_error(404, 'version mismatch')
            return
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        sent = 0
        with open(path, "rb") as f:
            while True:
                b = f.read(65536)
                if not b:
                    break
                self.wfile.write(b)
                sent += len(b)
        # ★출력은 cp949 안전 문자만★ — em-dash/화살표 등 유니코드는 윈도 콘솔에서
        #   UnicodeEncodeError로 프로세스를 죽인다(첫 기동 실사고)
        print(f"[시드] {self.client_address[0]} <- {self.path} ({sent // (1024*1024)}MB) "
              f"{time.strftime('%H:%M:%S')}", flush=True)

    def log_message(self, fmt, *args):
        pass  # 성공 서빙은 위에서 직접 출력, 404류는 조용히


if __name__ == "__main__":
    print(f"[시드] 기동: 0.0.0.0:{PORT}, exe_dir={EXE_DIR}", flush=True)
    print(f"[시드] 내부망 IP 감지: {_lan_ip() or '(감지 실패)'}", flush=True)
    # 내부망 IP 자동 등록 스레드 (비번 있을 때만 — 위 배너 참조)
    threading.Thread(target=_auto_register_loop, daemon=True).start()
    # ★버전별 보관 + 미푸시 빌드 경고 (사고 248)★
    threading.Thread(target=_snapshot_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
