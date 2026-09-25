# -*- coding: utf-8 -*-
"""[대시보드] 검증 명령 — ★이 폴더(server/) 안만★ 본다. 코드를 고쳤으면 반드시 이걸 돌린다.

  cd server && python -X utf8 verify.py           # 전부 (종료코드 0 = 통과)
  cd server && python -X utf8 verify.py --quick   # 문법·정적만 (시험 제외)

무엇을 보나 (순서대로, 하나라도 실패하면 종료코드 1)
  1. py_compile          main.py · database.py 문법
  2. pyflakes            미정의 이름 / 선언 전 참조 (설치돼 있을 때만)
  3. 대시보드 JS         main.py 안 인라인 <script> 를 node --check + 최소 줄수 래칫
                         (2026-07-27 「JS 한 줄 깨져 대시보드 백지」 사고 · 2026-09-11 「정본 5,149줄을
                          3줄로 지워도 통과」 사고 — 문법 검사는 빈 파일도 통과시킨다, 밖에서 센다)
  4. 미러 동기            static/dashboard.js·css 가 정본(main.py 인라인)과 같은가
  5. 시험                tests/test_*.py 전부 (실제 함수 호출 · 래칫 있음)

★다른 영역(lc/·farmview/·imania2/·web/)은 여기서 안 돌린다.★
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
PY = sys.executable
MAIN = os.path.join(HERE, "main.py")
STATIC = os.path.join(HERE, "static")
MAIN_JS_MIN_LINES = 4500        # 2026-09-11 실측 5,159줄. 정본이 실제로 줄면 여기를 내린다.
MAIN_CSS_MIN_LINES = 600        # 실측 803줄

fails = []


def step(name, ok, detail=""):
    print(("✔ " if ok else "✘ ") + name + (("  — " + detail) if detail else ""))
    if not ok:
        fails.append(name)


def _run(cmd, timeout=900):
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "명령 없음: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "시간 초과"


def _blocks(text, tag):
    return re.findall(r"<%s\b[^>]*>(.*?)</%s>" % (tag, tag), text, re.S | re.I)


def _looks_right(tag, b):
    # ★크기만 보고 고르면 JS 주석 안의 <style 에서 시작한 4,898줄짜리 가짜를 집는다(2026-09-11 실제 사고)★
    if tag == "style":
        return ("function " not in b) and ("=>" not in b) and ("{" in b)
    return "function " in b


def _pick(text, tag, min_lines):
    best, n = None, -1
    for b in _blocks(text, tag):
        if _looks_right(tag, b) and b.count("\n") > n:
            best, n = b, b.count("\n")
    return best if n >= min_lines else None


_LOOKBEHIND = re.compile(r"\(\?<[=!]")
# ★브라우저가 JS 로 읽는 자리 셋 (2026-09-24 반증 2차)★ — ① <script …>…</script> (닫는 태그 대소문자·공백 «</SCRIPT >» 도)
#   ② on*= 이벤트 속성 값 ③ href/src="javascript:…". _blocks 는 모양 뽑기용이라 따로 둔다.
_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</\s*script\s*>", re.S | re.I)
_ONATTR_RE = re.compile(r"""\bon[a-z]+\s*=\s*("[^"]*"|'[^']*')""", re.I)
_JSURL_RE = re.compile(r"""\b(?:href|src|action)\s*=\s*("\s*javascript:[^"]*"|'\s*javascript:[^']*')""", re.I)


def _lookbehind_hits(texts=None) -> list:
    """브라우저로 나가는 JS 의 정규식 뒤보기 자리 목록(«파일:블록 n: 줄»). texts = {이름: 원문} 을 주면 그것만(시험용)."""
    if texts is None:
        texts = {}
        for f in ("main.py", "ocr_label.py"):
            p = os.path.join(HERE, f)
            if os.path.isfile(p):
                texts[f] = open(p, encoding="utf-8").read()
        if os.path.isdir(STATIC):
            for f in sorted(os.listdir(STATIC)):
                if f.endswith(".js"):
                    texts["static/" + f] = "<script>" + open(os.path.join(STATIC, f), encoding="utf-8").read() + "</script>"
                elif f.lower().endswith((".html", ".htm")):   # ★static HTML 의 인라인 <script> 도 (2026-09-24 아이온2 반증)★
                    texts["static/" + f] = open(os.path.join(STATIC, f), encoding="utf-8").read()
    out = []
    for name, text in texts.items():
        for i, b in enumerate(_SCRIPT_RE.findall(text)):
            for ln in b.splitlines():
                if _LOOKBEHIND.search(ln):
                    out.append("%s:<script>%d: %s" % (name, i, ln.strip()[:120]))
        for kind, rx in (("on*=", _ONATTR_RE), ("javascript:", _JSURL_RE)):
            for m in rx.finditer(text):
                if _LOOKBEHIND.search(m.group(1)):
                    out.append("%s:%s:%d: %s" % (name, kind, text.count("\n", 0, m.start()) + 1, m.group(0)[:120]))
    return out


def _norm(s):
    return s.replace("\r\n", "\n").strip("\n")


def main_(quick=False):
    # 1. 문법
    rc, out = _run([PY, "-m", "py_compile", "main.py", "database.py"])
    step("1 py_compile", rc == 0, out.strip()[-200:])

    # 2. pyflakes (있을 때만)
    rc, out = _run([PY, "-m", "pyflakes", "main.py", "database.py"])
    if rc == 127 or "No module named" in out:
        step("2 pyflakes (미설치 — 건너뜀)", True)
    else:
        bad = [ln for ln in out.splitlines()
               if "undefined name" in ln or "referenced before" in ln or "local variable" in ln]
        step("2 pyflakes 미정의 이름 0", not bad, "\n   ".join(bad[:8]))

    # 3. 인라인 JS 문법 + 래칫
    src = open(MAIN, encoding="utf-8").read()
    js = _pick(src, "script", 1)
    css = _pick(src, "style", 1)
    step("3-a 정본 JS 를 찾았다", js is not None)
    if js is not None:
        step("3-b 정본 JS 최소 줄수 래칫 (%d ≥ %d)" % (js.count("\n"), MAIN_JS_MIN_LINES),
             js.count("\n") >= MAIN_JS_MIN_LINES)
        tmp = os.path.join(tempfile.mkdtemp(prefix="dashjs_"), "inline.js")
        open(tmp, "w", encoding="utf-8").write(js)
        rc, out = _run(["node", "--check", tmp])
        if rc == 127:
            step("3-c node --check (node 없음 — 건너뜀)", True)
        else:
            step("3-c 정본 JS 문법 (node --check)", rc == 0, out.strip()[-300:])
    # 3-e ★정규식 뒤보기 금지 (2026-09-24 아이온2 반증)★ — Safari 16.4 미만은 뒤보기 한 줄 때문에 ★스크립트 전체★ 를 문법
    #   오류로 버린다(대시보드가 통째로 빈다). 크롬·WebView2 는 멀쩡해서 node --check 로는 못 잡는다. 브라우저로 나가는
    #   <script> 전부(main.py 대시보드·로그인 · ocr_label.py 판별 화면 · static/*.html, 닫는 태그 «</SCRIPT >» 도) + static/*.js +
    #   on*= 이벤트 속성 · javascript: 주소를 본다(2026-09-24 반증 2차).
    _probe = _lookbehind_hits({"t": "x = 1\n<script>var a=/(?<=[0-9])b/;var c=/(?<!x)y/;</script>\n(?<=z)\n"
                                     "<SCRIPT type=module>\nvar d=/(?<=q)r/;\n</SCRIPT >\n"
                                     "<b onclick=\"/(?<=a)b/.test(x)\" title='(?<=t)'>\n"
                                     "<a HREF='javascript:/(?<!a)b/.test(1)'>\n<i onmouseover=\"f()\" data-x=\"(?<=d)\">"})
    step("3-e0 뒤보기 탐지기 자가시험(script 2·</SCRIPT >·on*=·javascript: → 4건, 밖·다른 속성은 안 셈)", len(_probe) == 4,
         str(_probe))
    hits = _lookbehind_hits()
    step("3-e 브라우저 JS 에 정규식 뒤보기 없음 (Safari <16.4)", not hits, "\n   ".join(hits[:8]))
    step("3-d 정본 CSS 최소 줄수 (%s ≥ %d)" % (css.count("\n") if css else "-", MAIN_CSS_MIN_LINES),
         css is not None and css.count("\n") >= MAIN_CSS_MIN_LINES)

    # 4. 미러 동기 (static/ 이 있을 때만)
    if os.path.isdir(STATIC):
        for fname, blk in (("dashboard.js", js), ("dashboard.css", css)):
            p = os.path.join(STATIC, fname)
            if blk is None or not os.path.isfile(p):
                step("4 미러 %s" % fname, False, "정본 블록 또는 미러 파일 없음")
                continue
            cur = open(p, encoding="utf-8", newline="").read()
            same = _norm(cur) == _norm(blk)
            step("4 미러 %s = 정본" % fname, same,
                 "" if same else "다르다 — 정본(main.py 인라인)만 고치고 미러는 `python verify.py --sync-static` 로 다시 뽑는다")
    if quick:
        return

    # 5. 시험 — 파일마다 별 프로세스 (임시 DB 를 각자 쓴다)
    tests = sorted(f for f in os.listdir(os.path.join(HERE, "tests")) if f.startswith("test_") and f.endswith(".py"))
    for t in tests:
        rc, out = _run([PY, "-X", "utf8", os.path.join("tests", t)], timeout=1200)
        tail = [ln for ln in out.splitlines() if ln.strip()][-3:]
        step("5 %s" % t, rc == 0, " | ".join(x.strip() for x in tail))

    # 5-b. ★한 프로세스 격리★ (#228 — 파일마다 따로면 초록인데 pytest 한 번에 이어 돌리면 228 의 OCR 라벨·대기가 218 A-10
    #   stats 에 샜다, 아이온2 실측). _harness.run_all 이 파일마다 ocr_fresh() 로 OCR 상태를 비우는지 지킨다.
    chain = ["tests/test_ocr_trust_228.py", "tests/test_ocr_auto_218.py", "tests/test_ocr_label.py"]
    rc, out = _run([PY, "-X", "utf8", "-m", "pytest", "-q", "-p", "no:cacheprovider", *chain], timeout=1800)
    tail = [ln for ln in out.splitlines() if ln.strip()][-2:]
    step("5-b OCR 시험 한 프로세스 연쇄 228→218→ocr_label (파일 간 상태 누수 없음)", rc == 0, " | ".join(x.strip() for x in tail))


def sync_static():
    src = open(MAIN, encoding="utf-8").read()
    os.makedirs(STATIC, exist_ok=True)
    for fname, tag, mn in (("dashboard.js", "script", MAIN_JS_MIN_LINES), ("dashboard.css", "style", MAIN_CSS_MIN_LINES)):
        blk = _pick(src, tag, mn)
        if blk is None:
            print("✘ 정본에서 <%s> 를 못 찾았다" % tag)
            return 1
        open(os.path.join(STATIC, fname), "w", encoding="utf-8", newline="\n").write(_norm(blk) + "\n")
        print("↻ static/%s 를 정본에서 다시 뽑았다 (%d줄)" % (fname, blk.count("\n")))
    return 0


if __name__ == "__main__":
    if "--sync-static" in sys.argv:
        sys.exit(sync_static())
    main_(quick="--quick" in sys.argv)
    print("-" * 60)
    if fails:
        print("✘ 검증 실패 %d건: %s" % (len(fails), ", ".join(fails)))
        sys.exit(1)
    print("✔ 대시보드 검증 전부 통과")
