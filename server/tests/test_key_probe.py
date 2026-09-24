# -*- coding: utf-8 -*-
"""API 키 추측 오라클 (2026-09-24 아이온2 반증) — 키를 맞춰 보는 자리는 전부 IP 실패 카운터·probe 잠금을 거친다.

/telegram/status 는 차단 테넌트의 «중계 가능» 을 알려 주려고 키를 따로 훑었는데 probe 잠금을 안 봤다 — 잠긴 IP 가
200/403 으로 차단 테넌트 키를 끝없이 맞혀 볼 수 있었다(check_api_key 는 잠긴 IP 에 None 을 주고 세지 않는다).
  K-1  잠기지 않은 IP + 차단 테넌트 키 → 200 enabled (2026-08-06 major 그대로 — 정지 안내가 나가야 한다)
  K-2  틀린 키 30번 → 그 IP 가 잠긴다(check_api_key 가 센다)
  K-3  ★잠긴 IP + 차단 테넌트의 맞는 키 → 403★ (예전 200 = 오라클)
  K-4  ★같은 부류 전수 (AST)★ — 서버 .py 어디서든 KEY_TO_TENANT 를 ★읽는★ 함수(.items()·.get()·for·in·[]·main.KEY_TO_TENANT)는
       그 읽기 ★앞에서★ _key_probe_blocked(...) 를 ★실제로 부른다★(글자·주석 일치가 아니라 호출 노드). 예외는 _init_tenants(설정으로 표를
       만든다 — 요청 키를 안 본다) 하나. 모듈 맨바닥 읽기·다른 곳 재바인딩도 빨간불.
  K-5  잠금 판정과 창 초기화가 같은 순간(check_api_key 가 now 를 넘긴다)
  K-4s 스캐너 자가시험 — 아이온2 반증이 든 우회 다섯(.get·for·주석 KEY_MAX_FAILS·잠금을 뒤에서·main.KEY_TO_TENANT)을 잡고 정상형은 통과
    cd updater/server && python -X utf8 tests/test_key_probe.py
"""
import ast
import json
import os

from fastapi import HTTPException

from _harness import main, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 7
IP = "203.0.113.77"


async def _status(key):
    try:
        r = await main.telegram_status(Req({}, api_key=key, host=IP))
        return r.status_code, json.loads(bytes(r.body))
    except HTTPException as e:
        return e.status_code, e.detail


async def t_status_probe():
    saved = (main.tg_enabled, main.tenant_chat_id)
    main.tg_enabled, main.tenant_chat_id = (lambda: True), (lambda t: "1")
    main.KEY_TO_TENANT["kp-blk-key"] = "kpblk"
    main.KILLED_TENANTS.add("kpblk")
    main._KEY_FAILS.pop(IP, None)
    try:
        c, b = await _status("kp-blk-key")
        ok("K-1 잠기지 않은 IP + 차단 테넌트 키 → 200 enabled(정지 안내 길 그대로)", c == 200 and b == {"enabled": True}, f"{c} {b}")
        for i in range(main.KEY_MAX_FAILS):
            await _status("kp-wrong-%d" % i)
        ok("K-2 틀린 키 %d번 → 그 IP 잠김(check_api_key 가 센다)" % main.KEY_MAX_FAILS, main._key_probe_blocked(IP),
           str(main._KEY_FAILS.get(IP)))
        c, b = await _status("kp-blk-key")
        ok("K-3 ★잠긴 IP + 차단 테넌트의 맞는 키 → 403★(예전 200 = 키 추측 오라클)", c == 403, f"{c} {b}")
        c2, _ = await _status("kp-wrong-x")
        ok("K-3b 잠긴 IP + 틀린 키도 403(답이 같아야 가를 수 없다)", c2 == 403 and c == c2, f"{c2}")
    finally:
        main.tg_enabled, main.tenant_chat_id = saved
        main.KEY_TO_TENANT.pop("kp-blk-key", None)
        main.KILLED_TENANTS.discard("kpblk")
        main._KEY_FAILS.pop(IP, None)


_ALLOW = {"_init_tenants": "설정(TENANTS)으로 표를 만든다 — 요청이 준 키를 맞춰 보지 않는다"}


def _is_ref(n):
    return (isinstance(n, ast.Name) and n.id == "KEY_TO_TENANT") or (isinstance(n, ast.Attribute) and n.attr == "KEY_TO_TENANT")


def _is_lock_call(n):
    return isinstance(n, ast.Call) and ((isinstance(n.func, ast.Name) and n.func.id == "_key_probe_blocked")
                                        or (isinstance(n.func, ast.Attribute) and n.func.attr == "_key_probe_blocked"))


def _scan(src: str, fname: str = "x.py") -> list:
    """KEY_TO_TENANT 를 읽는데 그 앞에서 잠금 호출이 없는 자리 목록. 정의(모듈 맨바닥 대입)·_ALLOW 는 뺀다."""
    tree = ast.parse(src)
    parent = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parent[c] = n
    bad = []
    for n in ast.walk(tree):
        if not _is_ref(n):
            continue
        fn, p = None, n
        while p in parent:
            p = parent[p]
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = p
                break
        store = isinstance(getattr(n, "ctx", None), ast.Store)
        if fn is None:
            if not (store and isinstance(n, ast.Name)):
                bad.append("%s:%d 모듈 맨바닥에서 읽음" % (fname, n.lineno))
            continue
        if fn.name in _ALLOW:
            continue
        if store:
            bad.append("%s:%d %s 가 KEY_TO_TENANT 를 다시 묶음" % (fname, n.lineno, fn.name))
            continue
        locks = [c.lineno for c in ast.walk(fn) if _is_lock_call(c)]
        if not any(ln < n.lineno for ln in locks):
            bad.append("%s:%d %s — 읽기 앞에 _key_probe_blocked(...) 호출 없음" % (fname, n.lineno, fn.name))
    return bad


def t_all_key_scans_check_probe():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad, readers = [], set()
    for f in sorted(os.listdir(here)):
        if f.endswith(".py"):
            src = open(os.path.join(here, f), encoding="utf-8").read()
            if "KEY_TO_TENANT" not in src:
                continue
            bad += _scan(src, f)
            for n in ast.walk(ast.parse(src)):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name not in _ALLOW \
                        and any(_is_ref(c) for c in ast.walk(n)):
                    readers.add(n.name)
    ok("K-4 ★KEY_TO_TENANT 를 읽는 함수는 전부 읽기 앞에서 _key_probe_blocked() 를 부른다★(AST, %d곳: %s)"
       % (len(readers), ",".join(sorted(readers))), len(readers) >= 5 and not bad, "; ".join(bad[:6]))
    dodges = {
        "get": "def f(request):\n    return KEY_TO_TENANT.get(request.headers.get('k'))\n",
        "for": "def f(k):\n    for kk in KEY_TO_TENANT:\n        pass\n",
        "comment": "def f(k):\n    # KEY_MAX_FAILS _key_probe_blocked\n    for a, b in KEY_TO_TENANT.items():\n        pass\n",
        "lock_after": "def f(ip, k):\n    t = k in KEY_TO_TENANT\n    _key_probe_blocked(ip)\n    return t\n",
        "main_attr": "async def f(ip, k):\n    return main.KEY_TO_TENANT[k]\n",
        "module": "X = dict(KEY_TO_TENANT)\n",
        "lambda_word": "def f(ip, k):\n    _key_probe_blocked = 1\n    return KEY_TO_TENANT.get(k)\n",
    }
    caught = {k: bool(_scan(v)) for k, v in dodges.items()}
    good = "def f(ip, k):\n    if _key_probe_blocked(ip):\n        return None\n    return KEY_TO_TENANT.get(k)\n"
    ok("K-4s 스캐너 자가시험 — 우회 %d가지 전부 잡고(.get·for·주석·잠금 뒤·main.·모듈·이름만) 정상형은 통과" % len(dodges),
       all(caught.values()) and not _scan(good), "%s good=%s" % (caught, _scan(good)))


def t_same_instant():
    """K-5 ★잠금 판정과 창 초기화가 같은 순간 (2026-09-24 반증)★ — check_api_key 가 자기 now 를 _key_probe_blocked 에 넘긴다.
    예전엔 잠금 판정이 time.time() 을 다시 불러, 창 경계에서 «잠김»(판정)·«창 지남»(초기화)이 갈릴 수 있었다."""
    ip = "203.0.113.78"
    t0 = 1_000_000.0
    main._KEY_FAILS[ip] = {"n": main.KEY_MAX_FAILS, "since": t0}
    try:
        inside = main._key_probe_blocked(ip, t0 + main.KEY_WINDOW)
        after = main._key_probe_blocked(ip, t0 + main.KEY_WINDOW + 0.001)
    finally:
        main._KEY_FAILS.pop(ip, None)
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "check_api_key")
    calls = [c for c in ast.walk(fn) if _is_lock_call(c)]
    passes_now = bool(calls) and all(len(c.args) >= 2 and isinstance(c.args[1], ast.Name) and c.args[1].id == "now" for c in calls)
    ok("K-5 _key_probe_blocked(ip, now) 가 준 순간으로 판정(창 끝 포함·지나면 풀림) + check_api_key 가 자기 now 를 넘긴다",
       inside is True and after is False and passes_now, "inside=%s after=%s passes_now=%s" % (inside, after, passes_now))


def test_all():
    run_all([t_status_probe, t_all_key_scans_check_probe, t_same_instant])
    finish("test_key_probe", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
