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


def _lan_ip() -> str:
    """함대 내부망 대역(172.30.x) IP를 고른다. 없으면 기본 라우트 소스 IP."""
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
        if ip.startswith("172.30."):     # 함대 내부망 대역 (실측 확정)
            return ip
    return cands[0] if cands else ""


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
    while True:
        try:
            ip = _lan_ip()
            want = f"http://{ip}:{PORT}" if ip else ""
            if want and want != last_pushed:
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
                    print(f"[시드] 내부망 IP 등록 갱신: {cur or '(없음)'} -> {want}", flush=True)
                last_pushed = want
        except Exception as e:
            print(f"[시드] 자동 IP 등록 실패(재시도 예정): {e}", flush=True)
        time.sleep(CHECK_EVERY)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        m = re.match(r"^/(macro|rental)-([\d.]+)\.exe$", self.path)
        if not m:
            self.send_error(404)
            return
        kind, req_ver = m.group(1), m.group(2)
        try:
            ver = json.load(open(VERSION_JSON, encoding="utf-8"))
        except Exception:
            self.send_error(503)
            return
        section, fname = MAP[kind]
        cur_ver = (ver.get(section) or {}).get("version")
        if req_ver != cur_ver:
            # 요청 버전 ≠ 로컬 최신 = 이 시드가 낡았거나(배포 직후 pull 전) 요청이 낡음 → GitHub로
            self.send_error(404, "version mismatch")
            return
        path = os.path.join(EXE_DIR, fname)
        if not os.path.exists(path):
            self.send_error(404)
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
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
