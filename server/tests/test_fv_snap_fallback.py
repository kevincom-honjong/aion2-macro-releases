# -*- coding: utf-8 -*-
"""FV 스냅샷 조립 실패 때 마지막 성공본 (2026-09-24 아이온2 승인, #201 부류 — 일시 실패를 화면 공백으로 만들지 않는다).

  · 조립이 실패해도 마지막 성공본이 FV_SNAP_STALE_MAX(120초) 안이면 200 + cached:true + 진짜 cache_age_s
  · 넘으면 예전대로 500 {ok:false, err}
  · 무효화된 본문(판매 차감 등, _fv_snap["body"]=None)은 절대 안 준다
  · 돌려주는 본문도 raw 를 거른다(B-FV1)
  · ★char_info·장부가 그 사이 쓰였으면(database.char_info_gen 세대가 다르면) 폴백도 3초 캐시도 안 준다★(아이온2 반증 #2)
  · database 의 char_info·kina_adjust 쓰기 함수는 전부 세대를 올린다(새 쓰기 자리가 빠지면 빨간불)
    cd updater/server && python -X utf8 tests/test_fv_snap_fallback.py
"""
import json

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 16
TOK = "fvsecret-sf"
H = {"X-FV-Token": TOK}


async def _get(raw=False):
    r = Req({}, api_key=None, headers=H)
    if raw:
        r.query_params = {"raw": "1"}
    resp = await main.fv_snapshot(r)
    return resp.status_code, json.loads(bytes(resp.body))


async def t_fallback():
    main.FV_TOKEN = TOK
    await db.upsert_status("PC-SF1", {"pc_id": "PC-SF1", "status": "idle"})
    real_build = main._fv_build_snapshot

    async def _boom(*a, **k):
        raise RuntimeError("database is locked")

    def _age(sec):                       # 마지막 성공본을 sec 초 전 것으로(시계 대신 캐시 시각을 민다)
        main._fv_snap["ts"] = main.time.time() - sec
    try:
        main._fv_snap.update(ts=0.0, body=None, gen=-1)
        code, good = await _get()
        ok("SF-0 대조 — 정상 조립은 200 cached:false", code == 200 and good.get("cached") is False and "PC-SF1" in (good.get("pcs") or {}),
           f"{code} {good.get('cached')}")
        main._fv_build_snapshot = _boom
        _age(30)
        code, b = await _get()
        ok("SF-1 ★조립 실패 + 30초 전 성공본 → 200 cached:true, cache_age_s≈30★",
           code == 200 and b.get("cached") is True and 29.5 <= float(b.get("cache_age_s") or 0) <= 31
           and "PC-SF1" in (b.get("pcs") or {}), f"{code} {b.get('cached')} {b.get('cache_age_s')}")
        ok("SF-2 돌려준 성공본도 raw 를 거른다(?raw=1 없으면 raw 칸 없음)",
           all("raw" not in (v or {}) for v in (b.get("pcs") or {}).values()), str(list((b.get("pcs") or {}).get("PC-SF1", {}).keys())[:8]))
        _age(121)
        code, b = await _get()
        ok("SF-3 ★120초가 넘으면 예전대로 500 {ok:false, err}★", code == 500 and b.get("ok") is False and "스냅샷 조립 실패" in str(b.get("err")),
           f"{code} {b}")
        main._fv_build_snapshot = real_build
        main._fv_snap.update(ts=0.0, body=None, gen=-1)
        code, good = await _get()
        main._fv_build_snapshot = _boom
        main._fv_snap["body"] = None     # = 판매 차감(kina_adjust)·장부 재적용이 캐시를 무효화
        _age(5)
        code, b = await _get()
        ok("SF-4 ★무효화된 본문은 안 준다(바뀐 걸 아는 옛 값) → 500★", code == 500 and b.get("ok") is False, f"{code} {b}")
        main._fv_build_snapshot = real_build
        code, b = await _get()
        ok("SF-5 조립이 살아나면 다시 200 cached:false", code == 200 and b.get("cached") is False, f"{code} {b.get('cached')}")
    finally:
        main._fv_build_snapshot = real_build
        main._fv_snap.update(ts=0.0, body=None, gen=-1)


async def _good_then_fail():
    """성공본 하나 만들고 조립을 망가뜨린다 — (real_build, boom) 돌려줌."""
    real_build = main._fv_build_snapshot
    main._fv_snap.update(ts=0.0, body=None, gen=-1)
    code, _ = await _get()
    assert code == 200

    async def _boom(*a, **k):
        raise RuntimeError("database is locked")
    main._fv_build_snapshot = _boom
    return real_build


async def t_generation():
    import aiosqlite
    main.FV_TOKEN = TOK
    await db.upsert_status("PC-SF2", {"pc_id": "PC-SF2", "status": "idle"})
    r = await main.receive_char_info("PC-SF2", Req({"total_kina": 500_000_000, "characters": [{"slot": 1, "name": "러닝"}]}))
    assert r.status_code == 200, r.body
    real_build = main._fv_build_snapshot
    try:
        real_build = await _good_then_fail()
        rs = await main.fv_kina_adjust(Req({"pc_id": "PC-SF2", "delta_kina": -100_000_000, "why": {"tid": "tid-SF1"}},
                                           api_key=None, headers=H))
        main._fv_snap["body"] = main._fv_snap["body"] or {"pcs": {}}   # 호출부가 캐시를 안 비웠다고 쳐도(세대가 막아야 한다)
        code, b = await _get()
        ok("SF-6 ★판매 차감(kina_adjust) 뒤엔 폴백을 안 준다 → 500★", rs.status_code == 200 and code == 500, f"{rs.status_code} {code}")
        main._fv_build_snapshot = real_build

        real_build = await _good_then_fail()
        r = await main.receive_char_info("PC-SF2", Req({"total_kina": 450_000_000, "characters": [{"slot": 1, "name": "러닝"}]}))
        code, b = await _get()
        ok("SF-7 ★새 판독(/char_info) 뒤엔 폴백을 안 준다 → 500★", r.status_code == 200 and code == 500, f"{r.status_code} {code}")
        main._fv_build_snapshot = real_build

        # 조립 ★중에★ 쓰기가 끼면 그 본문은 옛 세대로 적혀 3초 캐시로도 안 나간다
        main._fv_snap.update(ts=0.0, body=None, gen=-1)

        async def _build_with_write(*a, **k):
            out = await real_build(*a, **k)
            await db.upsert_char_info(main.ns("main", "PC-SF2"), 440_000_000, [{"slot": 1, "name": "러닝"}])
            return out
        main._fv_build_snapshot = _build_with_write
        code, b = await _get()
        main._fv_build_snapshot = real_build
        code2, b2 = await _get()
        ok("SF-8 조립 중 쓰기가 끼면 다음 요청은 캐시가 아니라 새로 만든다(cached:false, 새 값)",
           code == 200 and code2 == 200 and b2.get("cached") is False
           and ((b2.get("pcs") or {}).get("PC-SF2") or {}).get("progress", {}).get("total_kina") == 440_000_000,
           f"{b2.get('cached')} {((b2.get('pcs') or {}).get('PC-SF2') or {}).get('progress', {}).get('total_kina')}")
        code3, b3 = await _get()
        ok("SF-9 대조 — 쓰기가 없으면 3초 캐시는 그대로(cached:true)", code3 == 200 and b3.get("cached") is True, str(b3.get("cached")))
    finally:
        main._fv_build_snapshot = real_build
        main._fv_snap.update(ts=0.0, body=None, gen=-1)


def _writer_report(src):
    """database.py 소스 → (쓰기 함수 이름들, 어긴 곳 목록). 어김 = ★await …commit() 바로 다음 문장이 _char_info_bump() 호출이 아님★
    (주석만·커밋 앞으로 옮김·다른 문장 끼움 전부 잡는다 — 아이온2 변이 M10·M5), 또는 짝이 하나도 없음."""
    import ast
    import re as _re
    wr = _re.compile(r"(INSERT(\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+(char_info|kina_adjust)\b", _re.I)

    def _is_commit(st):
        return (isinstance(st, ast.Expr) and isinstance(st.value, ast.Await) and isinstance(st.value.value, ast.Call)
                and isinstance(st.value.value.func, ast.Attribute) and st.value.value.func.attr == "commit")

    def _is_bump(st):
        return (isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Name)
                and st.value.func.id == "_char_info_bump")

    writers, bad = [], []
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not wr.search(ast.get_source_segment(src, fn) or ""):
            continue
        writers.append(fn.name)
        pairs = 0
        for node in ast.walk(fn):
            for field in ("body", "orelse", "finalbody"):
                lst = getattr(node, field, None)
                if not isinstance(lst, list):
                    continue
                for k, st in enumerate(lst):
                    if _is_commit(st):
                        if k + 1 < len(lst) and _is_bump(lst[k + 1]):
                            pairs += 1
                        else:
                            bad.append("%s:%d commit 다음이 bump 호출이 아님" % (fn.name, st.lineno))
        if pairs == 0:
            bad.append("%s: commit→bump 짝 없음" % fn.name)
    return writers, bad


def t_writers_bump():
    """database.py 에서 char_info·kina_adjust 를 쓰는 함수는 ★커밋 바로 뒤에 진짜 _char_info_bump() 호출★(새 쓰기 자리 누락 방지)."""
    import os as _os
    src = open(_os.path.join(_os.path.dirname(_os.path.abspath(db.__file__)), "database.py"), encoding="utf-8").read()
    writers, bad = _writer_report(src)
    ok("SF-10 ★char_info·kina_adjust 쓰기 함수는 전부 커밋 바로 뒤에 세대를 올린다(AST 호출)★",
       not bad and len(writers) >= 4, f"쓰기 {writers} 어김 {bad}")
    probe = {
        "comment_only": "async def f(db):\n    await db.execute('UPDATE char_info SET x=1')\n    await db.commit()\n    # _char_info_bump()\n",
        "bump_before_commit": "async def f(db):\n    await db.execute('UPDATE char_info SET x=1')\n    _char_info_bump()\n    await db.commit()\n",
        "string_only": "async def f(db):\n    await db.execute('UPDATE char_info SET x=1')\n    await db.commit()\n    '_char_info_bump()'\n",
        "ok": "async def f(db):\n    await db.execute('UPDATE char_info SET x=1')\n    await db.commit()\n    _char_info_bump()\n",
    }
    got = {k: bool(_writer_report(v)[1]) for k, v in probe.items()}
    ok("SF-10s 검사기 자가시험 — 주석만·커밋 앞·글자만은 잡고 바른 판은 통과",
       got == {"comment_only": True, "bump_before_commit": True, "string_only": True, "ok": False}, str(got))


def t_worker_warning():
    w = main._worker_warning
    got = [bool(w({"WEB_CONCURRENCY": "2"}, [])), bool(w({"WEB_CONCURRENCY": "1"}, [])), bool(w({}, [])),
           bool(w({"WEB_CONCURRENCY": "x"}, [])), bool(w({}, ["uvicorn", "main:app", "--workers", "3"])),
           bool(w({}, ["uvicorn", "main:app", "--workers=2"])), bool(w({}, ["uvicorn", "main:app", "--workers", "1"])),
           bool(w({"UVICORN_WORKERS": "4"}, []))]
    ok("SF-11 ★워커 둘 이상(WEB_CONCURRENCY·--workers·UVICORN_WORKERS)이면 경고, 하나·없음·글자면 조용★",
       got == [True, False, False, False, True, True, False, True], str(got))
    import inspect
    ok("SF-11b lifespan 이 시작할 때 _worker_warning 을 부른다", "_worker_warning()" in inspect.getsource(main.lifespan), "")
    cm = main.lifespan(main.app)          # 만들기만 한다(실행 안 함)
    ok("SF-11c lifespan 은 여전히 @asynccontextmanager — 새 함수를 그 위에 끼워 넣다 장식자를 빼앗은 적 있다(2026-09-24)",
       type(cm).__name__ == "_AsyncGeneratorContextManager" and isinstance(main._worker_warning({}, []), str),
       "%s %s" % (type(cm).__name__, type(main._worker_warning({}, [])).__name__))


async def t_age_after_failure():
    import asyncio
    main.FV_TOKEN = TOK
    real_build = main._fv_build_snapshot
    try:
        main._fv_snap.update(ts=0.0, body=None, gen=-1)
        code, _ = await _get()

        async def _slow_boom(*a, **k):
            await asyncio.sleep(1.2)
            raise RuntimeError("slow then locked")
        main._fv_build_snapshot = _slow_boom
        main._fv_snap["ts"] = main.time.time() - 30
        code, b = await _get()
        ok("SF-12 폴백의 cache_age_s 는 ★실패한 뒤★ 잰 나이(조립에 1.2초 걸렸으면 31초대)",
           code == 200 and float(b.get("cache_age_s") or 0) >= 31.1, f"{code} {b.get('cache_age_s')}")
    finally:
        main._fv_build_snapshot = real_build
        main._fv_snap.update(ts=0.0, body=None, gen=-1)


def test_all():
    run_all([t_fallback, t_generation, t_writers_bump, t_worker_warning, t_age_after_failure])
    finish("test_fv_snap_fallback", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
