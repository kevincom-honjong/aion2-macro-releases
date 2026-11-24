# -*- coding: utf-8 -*-
"""[대시보드] 2026-11-25 «AI» 오늘의 던전 — 캐릭터 직업을 파워 앞에 표시(모르면 아무것도 안 그림).

    cd updater/server && python -X utf8 tests/test_ai_cls_1125.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from _harness import main as M, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 6


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
    d = tempfile.mkdtemp(prefix="aicls_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([shutil.which("node"), p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr[-400:]
    except Exception:
        return None, (r.stderr or r.stdout)[-400:]


async def t_cls():
    src = M.HTML_DASHBOARD
    i = src.index("const AI_CLS_COLOR")
    j = src.index("\n", i)
    esc = "function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }\n"
    js = esc + src[i:j] + "\n" + _fn(src, "aiClsHtml") + "\n" \
        "console.log(JSON.stringify(['궁성','치유성','',null,undefined,'-','  ','<b>x</b>','새직업'].map(aiClsHtml)));"
    g, err = _node(js)
    ok("CLS-0 node 실행", g is not None, err)
    g = g or [""] * 9
    ok("CLS-1 직업이 있으면 이름이 그대로 나온다(궁성·치유성) + 직업 색", "궁성" in g[0] and "text-green-400" in g[0] and "치유성" in g[1] and "text-pink-400" in g[1], str(g[:2]))
    ok("CLS-2 모름('' null undefined '-' 공백)은 빈 문자열 — 자리표시 없음", all(x == "" for x in g[2:7]), str(g[2:7]))
    ok("CLS-3 꺾쇠는 이스케이프(XSS 방지), 모르는 직업은 회색으로라도 표시", "<b>" not in g[7] and "&lt;b&gt;" in g[7] and "새직업" in g[8] and "text-gray-300" in g[8], str(g[7:]))
    ok("CLS-4 던전 줄에서 직업이 파워 앞(aiClsHtml(c.cls) → ${T.power}), 계획에 cls 가 실린다",
       src.index("${aiClsHtml(c.cls)}") < src.index("${T.power} <b") and "cls:r.char_class" in src, "")
    ok("CLS-5 파워 필터(aiPwMatch)는 그대로 — 필터 호출과 파워 구간 상수 유지", "aiPwMatch(c.pw, aiPwFilter)" in src and "const AI_PW_BANDS" in src, "")


def test_all():
    run_all([t_cls])
    finish("test_ai_cls_1125", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
