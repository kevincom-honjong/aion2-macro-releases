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
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = os.path.dirname(os.path.abspath(__file__))
EXE_DIR = os.path.join(BASE, "exe")
VERSION_JSON = os.path.join(BASE, "version.json")
PORT = 8766
MAP = {"macro": ("exe", "혼종_통합_자동.exe"),
       "rental": ("rental", "혼종_렌탈.exe")}


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
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
