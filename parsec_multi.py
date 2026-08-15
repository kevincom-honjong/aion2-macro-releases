# -*- coding: utf-8 -*-
"""
parsec_multi.py — 파섹 다중접속기 (관제컴 전용, 2026-08-15)

★무엇★ 관제컴에서 함대 본컴 ★여러 대를 동시에★ 파섹으로 띄우고 화면에 타일로 깐다.

★왜 이런 방식인가 (실측으로 확정한 제약)★
  파섹 앱은 한 PC에 ★인스턴스 1개★만 뜬다. %APPDATA%\\Parsec\\lock_client 를 배타 잠금
  (CreateFile dwShareMode=0)으로 잡아서다. 두 번째로 실행하면 새 창이 생기는 게 아니라
  argv를 이미 떠 있는 창에 넘기고 자기는 종료코드 0으로 죽는다(2026-08-15 실측).
  → 그래서 "창을 여러 개"는 ★포터블 모드★로만 된다.

★포터블 모드★ = 아래 3개 파일을 한 폴더에 같이 두면, 파섹이 그 폴더 안에만 상태를 가둔다
  (설정·자격증명·lock·lock_client·로그 전부). 잠금이 폴더별로 갈리니 인스턴스가 분리된다.
      parsecd.exe                (C:\\Program Files\\Parsec\\ 에서 복사)
      appdata.json               (%APPDATA%\\Parsec\\ 에서 복사)
      parsecd-<빌드>.dll          (%APPDATA%\\Parsec\\ 에서 복사, appdata.json의 so_name)
  appdata.json 의 hash 는 그 dll 의 SHA256이고 로더가 검사한다 → 셋은 ★같은 빌드로 한 세트★
  여야 한다. 파섹이 업데이트되면 setup 을 다시 돌려야 한다(재setup이 정답, 부분 교체 금지).
  이 방식은 파섹 공식 문서가 안내하는 방법이다("Connecting to multiple computers at once" —
  "four quadrant grid of small windows with each connected to a different computer").

★슬롯마다 로그인 1회★ — 포터블 폴더는 자격증명도 따로 가지므로, 슬롯을 만든 뒤 각 창에서
  한 번씩 파섹 로그인을 해야 한다(같은 계정으로 해도 된다). login 명령이 전부 띄워준다.
  ★비밀번호 입력은 사람이 한다★ — 이 스크립트는 자격증명을 저장하지도 입력하지도 않는다.

사용법
  python parsec_multi.py list                     함대 파섹 호스트 목록 (이름 + peer_id)
  python parsec_multi.py push                     ★대시보드 [🎮 파섹] 버튼용 주소록 갱신★
  python parsec_multi.py setup [N]                포터블 슬롯 N개 생성 (기본 4)
  python parsec_multi.py login                    슬롯 전부 실행 (각각 로그인 1회용)
  python parsec_multi.py grid NAME1 NAME2 ...     슬롯별로 각 호스트 접속 + 화면 타일 배치
  python parsec_multi.py grid --all               온라인 호스트 전부 (슬롯 수만큼)

★push 를 언제 하나★ 파섹에 PC를 새로 깔았을 때 / 컴퓨터 이름을 바꿨을 때 / 파섹을 재설치했을 때.
평소엔 할 필요 없다(peer_id 는 잘 안 변한다).

★왜 매크로가 아니라 여기서 미나★ 매크로 보고에 얹으면 매크로가 죽는 순간 파섹 버튼도 사라진다 —
원격으로 들어가 봐야 하는 때가 정확히 그때다. 그래서 파섹 주소록은 매크로와 완전히 분리한다.

토큰은 %APPDATA%\\Parsec\\user.bin 을 DPAPI로 그때그때 풀어 쓰며 ★어디에도 저장하지 않는다★.
서버로 올라가는 건 peer_id 뿐이다(그것만으론 접속 안 된다 — 내 파섹 계정 로그인이 있어야 한다).
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = r"C:\auto\parsec_multi"
API = "https://kessel-api.parsec.app/v2/hosts?mode=desktop"
INSTALL = r"C:\Program Files\Parsec\parsecd.exe"
SERVER = os.environ.get("AION2_SERVER", "https://web-production-8d4c.up.railway.app")


def _num_of(name: str):
    """★이름 전체가 번호꼴일 때만★ 번호로 읽는다. "18"/"PC-18"/"pc 18" → 18.

    ★부분 숫자를 절대 뽑지 않는다★ — 뽑으면 "DESKTOP-NGAQG8K" 의 8 이 PC-08 로 둔갑해
    대시보드에서 8번을 눌렀는데 엉뚱한 PC가 열린다. 서버(_pc_num)와 같은 규칙이다.
    """
    m = re.match(r"^\s*(?:PC[-_ ]?)?(\d{1,3})\s*$", str(name or ""), re.IGNORECASE)
    return int(m.group(1)) if m else None


# ─── 파섹 세션 토큰 (DPAPI) ───────────────────────────────────────────────────
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _session_id() -> str:
    """%APPDATA%\\Parsec\\user.bin → session_id. ★출력·저장 금지★"""
    path = os.path.join(os.environ.get("APPDATA", ""), "Parsec", "user.bin")
    if not os.path.exists(path):
        sys.exit("파섹이 설치/로그인돼 있지 않습니다 (user.bin 없음)")
    blob = open(path, "rb").read()
    buf = ctypes.create_string_buffer(blob, len(blob))
    src = _BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        sys.exit("user.bin 복호화 실패 — 이 윈도우 계정에서 파섹에 로그인했는지 확인하세요")
    try:
        plain = ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)
    return json.loads(plain.decode("utf-8").rstrip("\x00"))["session_id"]


def hosts() -> list:
    req = urllib.request.Request(API, headers={
        "Authorization": f"Bearer {_session_id()}",
        "User-Agent": "parsec/150-104a windows",
        "X-Parsec-OS": "windows",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        doc = json.loads(r.read().decode("utf-8"))
    rows = doc.get("data") or []
    return [h for h in rows if h.get("peer_id")]


def resolve(name: str, rows: list) -> dict:
    """이름으로 호스트 찾기 — 정확일치 → 대소문자무시 → 부분일치. ★모호하면 찍지 않는다★"""
    for pred, why in ((lambda h: h["name"] == name, "정확일치"),
                      (lambda h: h["name"].lower() == name.lower(), "대소문자무시"),
                      (lambda h: name.lower() in h["name"].lower(), "부분일치")):
        hit = [h for h in rows if pred(h)]
        if len(hit) == 1:
            return hit[0]
        if len(hit) > 1:
            sys.exit(f"'{name}' 에 {len(hit)}개가 걸립니다({why}): "
                     f"{', '.join(h['name'] for h in hit)} — 이름을 더 정확히 주세요")
    sys.exit(f"'{name}' 인 파섹 호스트가 없습니다. list 로 이름을 확인하세요")


# ─── 포터블 슬롯 ──────────────────────────────────────────────────────────────
def slot_dir(n: int) -> str:
    return os.path.join(ROOT, f"slot{n}")


def existing_slots() -> list:
    if not os.path.isdir(ROOT):
        return []
    out = []
    for name in sorted(os.listdir(ROOT)):
        if name.startswith("slot") and os.path.exists(os.path.join(ROOT, name, "parsecd.exe")):
            out.append(int(name[4:]))
    return sorted(out)


def setup(n: int) -> None:
    roam = os.path.join(os.environ["APPDATA"], "Parsec")
    aj_path = os.path.join(roam, "appdata.json")
    if not os.path.exists(INSTALL):
        sys.exit(f"파섹이 설치돼 있지 않습니다: {INSTALL}")
    if not os.path.exists(aj_path):
        sys.exit("%APPDATA%\\Parsec\\appdata.json 이 없습니다 — 파섹을 한 번 실행해 주세요")

    aj = json.loads(open(aj_path, encoding="utf-8").read())
    dll = os.path.join(roam, aj["so_name"])
    if not os.path.exists(dll):
        sys.exit(f"런타임 DLL이 없습니다: {dll}")

    import hashlib
    got = hashlib.sha256(open(dll, "rb").read()).hexdigest()
    if got != aj.get("hash"):
        sys.exit("appdata.json 의 hash 와 DLL SHA256이 다릅니다 — 파섹을 실행해 갱신한 뒤 다시 시도하세요")

    print(f"빌드 {aj['so_name']} 로 슬롯 {n}개 생성")
    for i in range(1, n + 1):
        d = slot_dir(i)
        os.makedirs(d, exist_ok=True)
        for src in (INSTALL, aj_path, dll):
            dst = os.path.join(d, os.path.basename(src))
            with open(src, "rb") as f_in, open(dst, "wb") as f_out:
                f_out.write(f_in.read())
        print(f"  slot{i} → {d}")

    print("\n★다음 단계★ 슬롯마다 파섹 로그인이 1회씩 필요합니다(같은 계정 OK).")
    print("   python parsec_multi.py login      ← 슬롯을 전부 띄웁니다. 각 창에서 로그인하세요.")


def launch(slot: int, peer_id: str = "", windowed: bool = True):
    exe = os.path.join(slot_dir(slot), "parsecd.exe")
    if not os.path.exists(exe):
        sys.exit(f"slot{slot} 이 없습니다 — 먼저 setup 을 실행하세요")
    # ★명령줄 인자는 콜론 구분★(파섹 공식: parsecd peer_id=x:client_vsync=1).
    #   URI(parsec://)는 & 구분이라 형식이 다르니 섞지 말 것.
    args = [exe]
    if peer_id:
        opt = f"peer_id={peer_id}"
        if windowed:
            opt += ":client_windowed=1"
        args.append(opt)
    return subprocess.Popen(args, cwd=slot_dir(slot))


# ─── 창 타일 배치 ─────────────────────────────────────────────────────────────
def _windows_of(pids: set) -> dict:
    """pid → 최상위 보이는 창 핸들. 파섹은 창을 늦게 만드니 호출 측에서 재시도한다."""
    found = {}
    user32 = ctypes.windll.user32
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and pid.value not in found:
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                found[pid.value] = hwnd
        return True

    user32.EnumWindows(CB(cb), 0)
    return found


def tile(procs: list) -> None:
    """실행한 창들을 화면에 격자로 깐다. 창을 못 찾은 건 조용히 건너뛴다(사용자가 직접 옮기면 됨)."""
    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 좌표를 물리 픽셀로 (스케일 왜곡 방지)
    except Exception:
        pass
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)

    n = len(procs)
    cols = 1 if n <= 1 else (2 if n <= 4 else 3)
    rows = (n + cols - 1) // cols
    w, h = sw // cols, sh // rows

    pids = {p.pid for p in procs}
    # 파섹 창은 기동 후 몇 초 뒤에 뜬다 — 다 뜰 때까지 최대 40초 기다린다.
    wins = {}
    for _ in range(40):
        wins = _windows_of(pids)
        if len(wins) >= n:
            break
        time.sleep(1)

    for i, p in enumerate(procs):
        hwnd = wins.get(p.pid)
        if not hwnd:
            print(f"  · slot 창을 못 찾음(pid {p.pid}) — 수동으로 배치하세요")
            continue
        x, y = (i % cols) * w, (i // cols) * h
        user32.ShowWindow(hwnd, 9)                       # SW_RESTORE (최소화 상태 대비)
        user32.MoveWindow(hwnd, x, y, w, h, True)
    print(f"타일 배치: {cols}x{rows} ({w}x{h} 각)")


# ─── 명령 ─────────────────────────────────────────────────────────────────────
def cmd_list():
    rows = hosts()
    print(f"파섹 호스트 {len(rows)}대\n")
    print(f"  {'이름':<24} {'온라인':<7} peer_id")
    for h in sorted(rows, key=lambda r: r["name"]):
        print(f"  {h['name']:<24} {'O' if h.get('online') else 'X':<7} {h['peer_id']}")


def cmd_grid(names):
    slots = existing_slots()
    if not slots:
        sys.exit("포터블 슬롯이 없습니다 — 먼저 `python parsec_multi.py setup 4` 를 실행하세요")

    rows = hosts()
    if names == ["--all"]:
        targets = [h for h in sorted(rows, key=lambda r: r["name"]) if h.get("online")]
    else:
        targets = [resolve(nm, rows) for nm in names]

    if len(targets) > len(slots):
        print(f"※ 슬롯이 {len(slots)}개뿐이라 앞 {len(slots)}대만 엽니다 "
              f"(전체 {len(targets)}대 — 더 필요하면 setup {len(targets)})")
        targets = targets[:len(slots)]

    procs = []
    for slot, h in zip(slots, targets):
        print(f"  slot{slot} → {h['name']}")
        procs.append(launch(slot, h["peer_id"]))
        time.sleep(1.5)          # 동시 기동 시 서로 포트/시그널링이 엉키는 걸 피한다
    tile(procs)
    print("\n각 창이 로그인 화면이면 그 슬롯은 아직 로그인 전입니다(슬롯마다 1회 필요).")


def _api_key() -> str:
    """대시보드 서버 API 키. ★소스에 절대 박지 않는다★ — 이 파일은 공개 레포에 들어갈 수 있다.

    찾는 순서: --key 인자 → 환경변수 → 스크립트 옆 파일 → C:\\auto\\info.txt
    """
    for i, a in enumerate(sys.argv):
        if a == "--key" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    if os.environ.get("PARSEC_PUSH_KEY"):
        return os.environ["PARSEC_PUSH_KEY"]
    side = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parsec_push_key.txt")
    if os.path.exists(side):
        return open(side, encoding="utf-8").read().strip()
    info = r"C:\auto\info.txt"
    if os.path.exists(info):
        for ln in open(info, encoding="utf-8", errors="replace"):
            if ln.strip().startswith("control_api_key"):
                return ln.split("=", 1)[1].strip()
    sys.exit("API 키를 못 찾았습니다. 다음 중 하나로 주세요:\n"
             "  python parsec_multi.py push --key <키>\n"
             "  set PARSEC_PUSH_KEY=<키>  (또는 updater\\parsec_push_key.txt 에 한 줄)")


def cmd_push():
    """번호로 이름 붙은 파섹 호스트만 골라 {번호: peer_id} 를 서버에 밀어넣는다."""
    rows = hosts()
    m, skipped = {}, []
    for h in rows:
        n = _num_of(h["name"])
        if n is None:
            skipped.append(h["name"])
            continue
        key = str(n)
        if key in m:
            sys.exit(f"번호 {key} 가 중복입니다 — 파섹에서 이름을 서로 다르게 해주세요")
        m[key] = h["peer_id"]

    if not m:
        sys.exit("번호로 이름 붙은 파섹 컴퓨터가 하나도 없습니다.\n"
                 "  파섹에서 각 PC의 컴퓨터 이름을 PC 번호(예: 18)로 바꿔주세요.\n"
                 f"  현재 이름: {', '.join(h['name'] for h in rows)}")

    body = json.dumps({"map": m}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{SERVER}/parsec/map", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Api-Key": _api_key()})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"서버 거부 (HTTP {e.code}): {e.read().decode('utf-8', 'replace')[:200]}")

    print(f"주소록 전송 완료 — {res.get('count')}대")
    for k in sorted(m, key=int):
        print(f"  PC-{k}  ->  {m[k]}")
    if skipped:
        print(f"\n번호가 아니라 건너뛴 컴퓨터: {', '.join(skipped)}")
        print("  (이 PC들도 대시보드에서 쓰려면 파섹 컴퓨터 이름을 번호로 바꾸세요)")


def cmd_login():
    slots = existing_slots()
    if not slots:
        sys.exit("포터블 슬롯이 없습니다 — 먼저 setup 을 실행하세요")
    procs = []
    for s in slots:
        print(f"  slot{s} 실행")
        procs.append(launch(s))
        time.sleep(1.5)
    tile(procs)
    print("\n각 창에서 파섹 로그인을 1회씩 해주세요(같은 계정 OK). 이후에는 grid 로 바로 접속됩니다.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "list":
        cmd_list()
    elif cmd == "push":
        cmd_push()
    elif cmd == "setup":
        setup(int(rest[0]) if rest else 4)
    elif cmd == "login":
        cmd_login()
    elif cmd == "grid":
        if not rest:
            sys.exit("대상을 지정하세요: grid PC이름들...  또는  grid --all")
        cmd_grid(rest)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
