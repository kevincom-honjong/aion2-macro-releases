# -*- coding: utf-8 -*-
"""API 키 추측 오라클 (2026-09-24 아이온2 반증) — 키를 맞춰 보는 자리는 전부 IP 실패 카운터·probe 잠금을 거친다.

/telegram/status 는 차단 테넌트의 «중계 가능» 을 알려 주려고 키를 따로 훑었는데 probe 잠금을 안 봤다 — 잠긴 IP 가
200/403 으로 차단 테넌트 키를 끝없이 맞혀 볼 수 있었다(check_api_key 는 잠긴 IP 에 None 을 주고 세지 않는다).
  K-1  잠기지 않은 IP + 차단 테넌트 키 → 200 enabled (2026-08-06 major 그대로 — 정지 안내가 나가야 한다)
  K-2  틀린 키 30번 → 그 IP 가 잠긴다(check_api_key 가 센다)
  K-3  ★잠긴 IP + 차단 테넌트의 맞는 키 → 403★ (예전 200 = 오라클)
  K-4  ★같은 부류 전수★ — main.py 에서 KEY_TO_TENANT 를 훑는 함수는 전부 probe 잠금(_key_probe_blocked·KEY_MAX_FAILS)을 본다
    cd updater/server && python -X utf8 tests/test_key_probe.py
"""
import ast
import json
import os

from fastapi import HTTPException

from _harness import main, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 5
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


def t_all_key_scans_check_probe():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    scanners, bad = [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = ast.get_source_segment(src, node) or ""
            if "KEY_TO_TENANT.items()" in body:
                scanners.append(node.name)
                if not ("_key_probe_blocked(" in body or "KEY_MAX_FAILS" in body):
                    bad.append(node.name)
    ok("K-4 ★KEY_TO_TENANT 를 훑는 함수는 전부 probe 잠금을 본다★(%d곳: %s)" % (len(scanners), ",".join(scanners)),
       len(scanners) >= 5 and not bad, "잠금 없음: %s" % bad)


def test_all():
    run_all([t_status_probe, t_all_key_scans_check_probe])
    finish("test_key_probe", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
