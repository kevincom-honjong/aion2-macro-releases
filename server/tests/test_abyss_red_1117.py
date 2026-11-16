# -*- coding: utf-8 -*-
"""[대시보드] 2026-11-17 주인님 「00:00:00 이면 빨간색 글자로」 — 캐릭 표 어비스 칸.
0 이고 리셋 이전 값이 아닐 때만 빨강(#f87171 굵게). 리셋 이전 0 은 14:00:00 으로 표시되므로 빨강 아님.

    cd updater/server && python -X utf8 tests/test_abyss_red_1117.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from _harness import main as M, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 8
OLD = "2026-09-01T00:00:00"
NEW = "2099-01-01T00:00:00"


def _fn(src, name):
    i = src.index("function " + name + "(")
    d, k = 0, src.index("{", i)
    while True:
        c = src[k]
        d += (c == "{") - (c == "}")
        k += 1
        if d == 0:
            return src[i:k]


def _node(js):
    d = tempfile.mkdtemp(prefix="abr_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([shutil.which("node"), p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr[-900:]
    except Exception:
        return None, (r.stderr or r.stdout)[-900:]


async def t_red():
    src = M.HTML_DASHBOARD
    ok("ABR-1 어비스 칸이 isAbyssZeroRed 로 빨강(#f87171·font-bold)을 정한다",
       "isAbyssZeroRed(r)" in src and "color:#f87171" in src and "'font-bold'" in src, "")
    ok("ABR-2 빨강이 아닐 때 옛 text-fuchsia-300 유지", "'text-fuchsia-300'" in src, "")
    # wk 리셋 경계는 시계에 의존 — lastWeeklyReset 은 실제 코드 그대로 쓴다(OLD=확실히 이전, NEW=확실히 이후)
    fns = "\n".join(_fn(src, n) for n in ("collectedAtDate", "lastWeeklyReset", "isBeforeReset", "isSurfaceZero",
                                          "displayAbyssTime", "isAbyssZeroRed"))
    i = src.index("const KST_OFF_MS")
    pre = src[i:src.index("\n", i)] + "\n" + _fn(src, "kstGameDayNum") + "\n"
    rows = [
        {"abyss_time": "00:00:00", "collected_at": NEW},      # 리셋 뒤 0 → 빨강
        {"abyss_time": "00:00:00", "collected_at": OLD},      # 리셋 앞 0 → 14:00:00, 빨강 아님
        {"abyss_time": "00:00:00", "collected_at": ""},       # 시각 모름 → 원본 0, 빨강
        {"abyss_time": "03:12:00", "collected_at": NEW},      # 0 아님
        {"abyss_time": "", "collected_at": NEW},              # 못 읽음 → 빨강 아님
        {"abyss_time": "–", "collected_at": NEW},
        {"abyss_time": "0:00:00", "collected_at": NEW},       # 자릿수 다른 0
    ]
    js = pre + fns + "\nconst rows=%s;\nconsole.log(JSON.stringify(rows.map(r=>[isAbyssZeroRed(r),displayAbyssTime(r)])));" % json.dumps(rows)
    got, err = _node(js)
    ok("ABR-0 node 실행", got is not None, err)
    got = got or [[None, None]] * 7
    ok("ABR-3 리셋 뒤 00:00:00 → 빨강 + 표시 00:00:00", got[0] == [True, "00:00:00"], str(got[0]) + err)
    ok("ABR-4 리셋 앞 00:00:00 → 14:00:00 표시, 빨강 아님", got[1] == [False, "14:00:00"], str(got[1]))
    ok("ABR-5 수집 시각 모름 00:00:00 → 원본 그대로 빨강", got[2] == [True, "00:00:00"], str(got[2]))
    ok("ABR-6 0 아닌 값·빈 값·'–' 는 빨강 아님", not got[3][0] and not got[4][0] and not got[5][0], str(got[3:6]))
    ok("ABR-7 0:00:00 도 빨강", got[6][0] is True, str(got[6]))


def test_all():
    run_all([t_red])
    finish("test_abyss_red_1117", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
