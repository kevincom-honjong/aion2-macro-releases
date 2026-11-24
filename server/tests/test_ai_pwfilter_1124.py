# -*- coding: utf-8 -*-
"""[대시보드] 2026-11-24 «AI» 오늘의 던전 — 파워 구간 필터(800K↑ / 400~800 / 280~400 / 200~280 / 전체).

    cd updater/server && python -X utf8 tests/test_ai_pwfilter_1124.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from _harness import main as M, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 8


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
    d = tempfile.mkdtemp(prefix="aipw_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([shutil.which("node"), p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr[-400:]
    except Exception:
        return None, (r.stderr or r.stdout)[-400:]


async def t_pw():
    src = M.HTML_DASHBOARD
    i = src.index("const AI_PW_BANDS")
    j = src.index("];", i) + 2
    js = src[i:j] + "\n" + _fn(src, "aiPwMatch") + "\n"
    vals = [800000, 799999, 400000, 399999, 280000, 279999, 200000, 199999, 0, "", None, "x", 1500000]
    js += ("const V=%s;const out={};['', '800','400','280','200','zz'].forEach(k=>out[k]=V.map(v=>aiPwMatch(v,k)));"
           "console.log(JSON.stringify(out));" % json.dumps(vals))
    g, err = _node(js)
    ok("PWF-0 node 실행", g is not None, err)
    g = g or {}
    # 순서: 800000, 799999, 400000, 399999, 280000, 279999, 200000, 199999, 0, "", None, "x", 1500000
    ok("PWF-1 전체: 모름(0·빈값·null·숫자아님) 포함 전부 통과", g.get("") == [True] * 13, str(g.get("")))
    ok("PWF-2 800K 이상: 800000·1500000 만", g.get("800") == [True, False] + [False] * 10 + [True], str(g.get("800")))
    ok("PWF-3 400K~800K 미만: 경계 400000 포함·800000 제외", g.get("400") == [False, True, True] + [False] * 10, str(g.get("400")))
    ok("PWF-4 280K~400K 미만 / 200K~280K 미만(경계 포함·미만 제외)",
       g.get("280") == [False, False, False, True, True] + [False] * 8 and g.get("200") == [False] * 5 + [True, True] + [False] * 6,
       str(g.get("280")) + str(g.get("200")))
    ok("PWF-5 200K 미만·파워 모름은 구간 버튼에서 안 보인다(전체에서만)",
       all(not g[k][i] for k in ("800", "400", "280", "200") for i in (7, 8, 9, 10, 11)), "")
    ok("PWF-6 알 수 없는 키는 아무도 통과 못 한다(오타로 전부 보이는 사고 방지)", g.get("zz") == [False] * 13, str(g.get("zz")))
    ok("PWF-7 필터 바가 제목 줄 아래·요약 위에 있고 던전 탭에서만 보인다",
       src.index('id="ai-pw-bar"') > src.index('id="ai-title"') and src.index('id="ai-pw-bar"') < src.index('id="ai-summary"')
       and "aiFilter !== 'dg'" in src and "setAiPw('')" in src and "aiPwMatch(c.pw, aiPwFilter)" in src, "")


def test_all():
    run_all([t_pw])
    finish("test_ai_pwfilter_1124", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
