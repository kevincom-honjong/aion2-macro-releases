# -*- coding: utf-8 -*-
"""[대시보드] #271 — server/bug_kinds.py 는 lc/bug_kinds.py(매크로 정본)의 거울이다. 코드가 한 글자라도 다르면 빨강.

  서버는 Railway 에 server/ 만 올라가서 lc/ 를 import 할 수 없다 → 복사 + 이 시험.
  정본을 고쳤으면: cp ../../lc/bug_kinds.py bug_kinds.py (주석·독스트링 차이는 봐준다 — 코드만 대조)

    cd updater/server && python -X utf8 tests/test_bug_kinds_mirror.py
"""
import ast
import os

from _harness import main, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 4
SRV = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANON = os.path.normpath(os.environ.get("BUGK_CANON") or os.path.join(SRV, "..", "..", "lc", "bug_kinds.py"))
MIRROR = os.path.join(SRV, "bug_kinds.py")


def _code(path):
    t = ast.parse(open(path, encoding="utf-8").read())
    if t.body and isinstance(t.body[0], ast.Expr) and isinstance(getattr(t.body[0], "value", None), ast.Constant):
        t.body = t.body[1:]                              # 모듈 독스트링은 빼고
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body and isinstance(n.body[0], ast.Expr) \
                and isinstance(getattr(n.body[0], "value", None), ast.Constant) and isinstance(n.body[0].value.value, str):
            n.body = n.body[1:] or [ast.Pass()]          # 함수 독스트링도
    return ast.dump(t)


def _lit(path, name):
    for n in ast.parse(open(path, encoding="utf-8").read()).body:
        if isinstance(n, ast.Assign) and any(getattr(x, "id", "") == name for x in n.targets):
            return ast.literal_eval(n.value)


async def t_mirror():
    ok("K-1 정본 lc/bug_kinds.py 가 있다(없으면 대조를 못 한다 — 초록으로 넘기지 않는다)", os.path.isfile(CANON), CANON)
    if not os.path.isfile(CANON):
        return
    same = all(_lit(CANON, n) == _lit(MIRROR, n) for n in ("CLASSES", "TABLE", "NODEDUP"))
    ok("K-2 표(CLASSES·TABLE·NODEDUP)가 글자까지 같다", same, "cp ../../lc/bug_kinds.py bug_kinds.py")
    ok("K-3 판정 코드(classify·split_name…)도 같다(주석·독스트링만 봐준다)", _code(CANON) == _code(MIRROR), "")
    ok("K-4 서버 main 이 그 거울을 쓴다", main.BUGK.TABLE == _lit(MIRROR, "TABLE")
       and main.BUGK.__file__ and os.path.samefile(main.BUGK.__file__, MIRROR), str(getattr(main.BUGK, "__file__", "")))


def test_all():
    run_all([t_mirror])
    finish("test_bug_kinds_mirror", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
