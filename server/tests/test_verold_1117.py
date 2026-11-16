# -*- coding: utf-8 -*-
"""[대시보드] 2026-11-17 버전 뱃지 빨강은 «최신보다 옛것» 일 때만 (시험판 3.1.15 > 3.1.13 이 빨갰다).

    cd updater/server && python -X utf8 tests/test_verold_1117.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from _harness import main as M, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 9


def _fn(src, name):
    i = src.index("function " + name + "(")
    d, k = 0, src.index("{", i)
    while True:
        c = src[k]
        d += (c == "{") - (c == "}")
        k += 1
        if d == 0:
            return src[i:k]


async def t_ver():
    src = M.HTML_DASHBOARD
    ok("VO-1 뱃지 두 곳이 verOlder 를 쓴다(!== 판정 없음)",
       "verOlder(pc.macro_version, latestVersions.macro)" in src
       and "verOlder(pc._updater_version, latestVersions.updater)" in src
       and "_updater_version !== latestVersions.updater" not in src, "")
    cases = [("3.1.15", "3.1.13"), ("3.1.13", "3.1.15"), ("3.1.13", "3.1.13"), ("3.1.9", "3.1.10"),
             ("3.2", "3.1.13"), ("3.1", "3.1.0"), ("", "3.1.13"), (None, "3.1.13"), ("dev", "3.1.13"),
             ("3.1.13", ""), ("v3.1.12", "3.1.13"), ("3.1.13-beta", "3.1.13")]
    js = _fn(src, "verOlder") + "\nconsole.log(JSON.stringify(%s.map(c=>verOlder(c[0],c[1]))));" % json.dumps(cases)
    d = tempfile.mkdtemp(prefix="vo_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([shutil.which("node"), p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        g = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        g = [None] * len(cases)
    ok("VO-2 새것(3.1.15>3.1.13) 빨강 아님", g[0] is False, r.stderr[-300:])
    ok("VO-3 옛것(3.1.13<3.1.15) 빨강", g[1] is True, str(g))
    ok("VO-4 같은 버전 빨강 아님", g[2] is False, str(g))
    ok("VO-5 숫자 비교(3.1.9 < 3.1.10 이 옛것)", g[3] is True, str(g))
    ok("VO-6 자릿수 다른 3.2 > 3.1.13 · 3.1 == 3.1.0", g[4] is False and g[5] is False, str(g))
    ok("VO-7 빈 값·null·dev·최신 모름은 빨강 아님", g[6:10] == [False] * 4, str(g))
    ok("VO-8 v 접두사 옛것 빨강", g[10] is True, str(g))
    ok("VO-9 해석 못 하는 꼬리(-beta)는 빨강 아님", g[11] is False, str(g))


def test_all():
    run_all([t_ver])
    finish("test_verold_1117", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
