# -*- coding: utf-8 -*-
"""[대시보드] T2 — 로그 커서 (2026-09-26 Railway 나가는 바이트: 감시기가 PC 마다 /logs/{pc} 2000줄을 1분 60번).

  GET /logs_since?since=&limit=&contains=&pc=   전 PC(또는 한 PC) since 뒤 로그 · 세션
  GET /logs/{pc}?since=                          같은 모양(한 PC) · since 없으면 예전 그대로

    cd updater/server && python -X utf8 tests/test_logs_since.py
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from _harness import main, db, aiosqlite, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 20
C = TestClient(main.app, raise_server_exceptions=False)
FMT = "%Y-%m-%dT%H:%M:%S"
NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def ts(sec_ago):
    return (NOW - timedelta(seconds=sec_ago)).strftime(FMT)


async def _put(rows):
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("DELETE FROM logs")
        for pc, msg, at in rows:
            await c.execute("INSERT INTO logs (pc_id, level, message, created_at) VALUES (?,?,?,?)", (pc, "info", msg, at))
        await c.commit()


def _get(path, **params):
    C.cookies.set("session", main.new_session("main"))
    try:
        return C.get(path, params=params)
    finally:
        C.cookies.clear()


async def t_basic():
    await _put([("PC-01", "사냥 시작", ts(100)), ("PC-02", "캡차 떴다", ts(90)), ("t2::PC-09", "남의 테넌트", ts(85)),
                ("PC-01", "사냥 끝", ts(80)), ("PC-03.upd", "업데이터 줄", ts(70)), ("PC-02", "방금", ts(0))])
    ok("L-1 세션 없으면 401(둘 다)", C.get("/logs_since").status_code == 401 and C.get("/logs/PC-01", params={"since": ts(200)}).status_code == 401, "")
    j = _get("/logs_since", since=ts(200)).json()
    pcs = [r["pc"] for r in j["logs"]]
    ok("L-2 전 PC 를 시각순으로 한 번에 · 남의 테넌트는 안 섞인다", pcs == ["PC-01", "PC-02", "PC-01", "PC-03.upd"], str(pcs))
    ok("L-3 ★아직 안 닫힌 초(지금)는 다음 판으로★", all(r["message"] != "방금" for r in j["logs"]), "")
    ok("L-4 next_since = 마지막으로 준 줄의 시각", j["next_since"] == ts(70) and j["truncated"] is False, str(j["next_since"]))
    j2 = _get("/logs_since", since=j["next_since"]).json()
    ok("L-5 이어 읽으면 겹치지 않는다(아직 닫히지 않은 초만 남음)", j2["logs"] == [], str(j2["logs"]))
    jc = _get("/logs_since", since=ts(200), contains="캡차").json()
    ok("L-6 contains= 로 거른다", [r["message"] for r in jc["logs"]] == ["캡차 떴다"], str(jc["logs"]))
    ok("L-7 ★거른 뒤에도 커서는 읽은 끝까지 간다★(안 맞는 줄이 많아도 안 멈춤)", jc["next_since"] == ts(70), jc["next_since"])
    jp = _get("/logs_since", since=ts(200), pc="PC-01").json()
    ok("L-8 pc= 로 한 대만", [r["message"] for r in jp["logs"]] == ["사냥 시작", "사냥 끝"], str(jp["logs"]))
    jl = _get("/logs/PC-01", since=ts(95)).json()
    ok("L-9 /logs/{pc}?since= — 그 PC 의 since 뒤만, 같은 모양", [r["message"] for r in jl["logs"]] == ["사냥 끝"]
       and "next_since" in jl, str(jl))
    old = _get("/logs/PC-01").json()
    ok("L-10 since 없으면 /logs/{pc} 는 예전 그대로(최근 줄, 커서 없음)", [r["message"] for r in old["logs"]] == ["사냥 시작", "사냥 끝"]
       and "next_since" not in old, str(old))


async def t_trunc():
    await _put([("PC-01", "a1", ts(50)), ("PC-02", "a2", ts(50)), ("PC-01", "b1", ts(40)), ("PC-02", "b2", ts(40)), ("PC-03", "b3", ts(40))])
    j = _get("/logs_since", since=ts(60), limit="3").json()
    ok("L-11 ★잘리면 경계 초를 통째로 다음 장으로★ — 앞 초 둘만", [r["message"] for r in j["logs"]] == ["a1", "a2"]
       and j["truncated"] is True and j["next_since"] == ts(50), str(j))
    j2 = _get("/logs_since", since=j["next_since"], limit="3").json()
    ok("L-12 다음 장에서 경계 초 셋이 빠짐없이", [r["message"] for r in j2["logs"]] == ["b1", "b2", "b3"] and j2["truncated"] is False, str(j2))
    await _put([("PC-0%d" % i, "s%d" % i, ts(30)) for i in range(1, 6)])
    j3 = _get("/logs_since", since=ts(60), limit="3").json()
    ok("L-13 한 초에 limit 넘게면 그 초만 limit 개(초 단위 커서의 한계 — FV 와 같다)", len(j3["logs"]) == 3 and j3["truncated"] is True, str(j3))


async def t_edges():
    await _put([("PC-01", "x", ts(20)), ("PC-01", "y", ts(10))])
    ok("L-14 since 가 날짜가 아니면 400", _get("/logs_since", since="어제").status_code == 400, "")
    ok("L-15 contains 가 너무 길면 400", _get("/logs_since", since=ts(60), contains="가" * 201).status_code == 400, "")
    ok("L-16 limit 이 숫자가 아니면 400", _get("/logs_since", since=ts(60), limit="many").status_code == 400, "")
    _keep = main.LOGS_SINCE_MAX
    main.LOGS_SINCE_MAX = 1
    try:
        jm = _get("/logs_since", since=ts(60), limit="99999").json()
    finally:
        main.LOGS_SINCE_MAX = _keep
    ok("L-17 limit 은 상한(LOGS_SINCE_MAX)에서 잘린다", jm["count"] == 1 and jm["truncated"] is True, str(jm["count"]))
    main._FV_INFLIGHT.append(ts(15))
    try:
        j = _get("/logs_since", since=ts(60)).json()
    finally:
        main._FV_INFLIGHT.remove(ts(15))
    ok("L-18 ★커밋 중인 업데이터 줄이 있으면 그 시각 앞까지만★(FV 와 같은 규칙)", [r["message"] for r in j["logs"]] == ["x"], str(j["logs"]))
    d = _get("/logs_since").json()
    ok("L-19 since 없으면 최근 10분", d["since"] <= ts(599) and d["since"] >= ts(601), d["since"])
    ok("L-20 줄마다 id·pc·level·message·created_at", set(j["logs"][0]) == {"id", "pc", "level", "message", "created_at"}, str(j["logs"][0]))


def test_all():
    run_all([t_basic, t_trunc, t_edges])
    finish("test_logs_since", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
