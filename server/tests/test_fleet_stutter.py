# -*- coding: utf-8 -*-
"""[대시보드] #453 (사고 678) — GET /fleet/stutter?pc=PC-xx : 함대 렉 집계(메모리만).

  창 180초(신선/낡음) · 물리 PC(baseId) 중복 제거 · 나 제외 · 모르는 pc/자료 없음 → 0/0 · 가짜 PC 제외 · 테넌트 분리 · API 키 인증

    cd updater/server && python -X utf8 tests/test_fleet_stutter.py
"""
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from _harness import main, db, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 12
M = main
C = TestClient(main.app, raise_server_exceptions=False)
ZERO = {"bad_others": 0, "reporting": 0}


def _put(key, data, age_s):
    at = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S")
    db._ST_MEM[key] = {"data": json.dumps(data), "at": at, "status": data.get("status"), "sig": "x", "saved": 0.0}


class _Clean:
    def __enter__(self):
        self.o = dict(db._ST_MEM)
        db._ST_MEM.clear()
        return self

    def __exit__(self, *a):
        db._ST_MEM.clear()
        db._ST_MEM.update(self.o)


async def t_core():
    with _Clean():
        n = M.ns
        _put(n("main", "PC-01"), {"status": "hunting", "stutter": {"bad": True}}, 10)       # 나쁨(다른 PC)
        _put(n("main", "PC-02"), {"status": "hunting", "stutter": {"bad": False}}, 20)
        _put(n("main", "PC-03"), {"status": "hunting"}, 30)                                  # 필드 없음(옛 매크로) → 나쁘지 않음, 그래도 보고 중
        _put(n("main", "PC-04"), {"status": "hunting", "stutter": {"bad": True}}, 400)       # 낡음 → 안 센다
        _put(n("main", "PC-05"), {"status": "hunting", "stutter": {"bad": True}}, 170)       # 창 안쪽 경계 → 센다
        _put(n("main", "PC-09"), {"status": "hunting", "stutter": {"bad": True}}, 5)         # 호출자(나)
        r = M._fleet_stutter_core("main", "PC-09")
        ok("F-1 ★보고 중 = 최근 180초 안 PC(01·02·03·05·09 = 5), 나쁨(나 제외) = 01·05 = 2★",
           r == {"bad_others": 2, "reporting": 5}, str(r))
        r2 = M._fleet_stutter_core("main", "PC-02")
        ok("F-2 ★나는 bad_others 에서 빠진다(PC-02 로 물으면 나쁨 01·05·09 = 3)★ · reporting 은 나 포함",
           r2 == {"bad_others": 3, "reporting": 5}, str(r2))
        ok("F-3 낡은 보고(PC-04, 400초)는 보고 중에도 나쁨에도 안 센다", r["reporting"] == 5 and r["bad_others"] == 2)
        # 같은 물리 PC 의 카드 여럿 — 최신 하나만, 한 대로 센다
        _put(n("main", "PC-06"), {"status": "other_account", "stutter": {"bad": True}}, 100)    # 옛 카드(나쁨)
        _put(n("main", "PC-06b"), {"status": "hunting", "stutter": {"bad": False}}, 15)         # 최신 카드(괜찮음) → PC-06 은 괜찮음
        _put(n("main", "PC-07b"), {"status": "hunting", "stutter": {"bad": True}}, 20)
        _put(n("main", "PC-07c"), {"status": "hunting", "stutter": {"bad": True}}, 25)          # 같은 PC 두 카드 모두 나쁨 → 한 대
        r3 = M._fleet_stutter_core("main", "PC-09")
        ok("F-4 ★baseId 중복 제거: PC-06(+b) 한 대(최신 카드가 정함 → 괜찮음) · PC-07b/c 한 대(나쁨)★",
           r3 == {"bad_others": 3, "reporting": 7}, str(r3))
        ok("F-5 호출자가 b/c 카드 id 로 물어도 baseId 로 제외(PC-07c 로 물으면 PC-07 제외)",
           M._fleet_stutter_core("main", "PC-07c") == {"bad_others": 3, "reporting": 7}, str(M._fleet_stutter_core("main", "PC-07c")))
        ok("F-6 ★모르는 pc → 0/0 (건강한 척 안 채운다)★ · pc 비어 있음 → 0/0",
           M._fleet_stutter_core("main", "PC-99") == ZERO and M._fleet_stutter_core("main", "") == ZERO)
        _put(n("main", "PC-TEST"), {"status": "hunting", "stutter": {"bad": True}}, 5)
        ok("F-7 가짜 PC(PC-TEST·PC-DEMO)는 집계에서 뺀다", M._fleet_stutter_core("main", "PC-09") == r3)
        _put(n("other", "PC-10"), {"status": "hunting", "stutter": {"bad": True}}, 5)
        ok("F-8 테넌트가 다르면 안 섞인다", M._fleet_stutter_core("main", "PC-09") == r3
           and M._fleet_stutter_core("other", "PC-10") == {"bad_others": 0, "reporting": 1})
        db._ST_MEM.clear()
        _put(n("main", "PC-01"), {"status": "hunting", "stutter": {"bad": True}}, 900)
        _put(n("main", "PC-09"), {"status": "hunting"}, 900)
        ok("F-9 전부 낡았으면 0/0 (모름)", M._fleet_stutter_core("main", "PC-09") == ZERO)
        db._ST_MEM.clear()
        ok("F-10 메모리가 비었으면(부팅 직후) 0/0", M._fleet_stutter_core("main", "PC-09") == ZERO)


async def t_http():
    with _Clean():
        _put(M.ns("main", "PC-01"), {"status": "hunting", "stutter": {"bad": True}}, 10)
        _put(M.ns("main", "PC-09"), {"status": "hunting"}, 10)
        r = C.get("/fleet/stutter?pc=PC-09")
        ok("F-11 ★API 키 없으면 403★", r.status_code == 403, str(r.status_code))
        r = C.get("/fleet/stutter?pc=PC-09", headers={"X-Api-Key": main.API_KEY})
        ok("F-12 키가 있으면 200 · 본문 {bad_others, reporting} · 캐시 금지",
           r.status_code == 200 and r.json() == {"bad_others": 1, "reporting": 2} and "no-store" in r.headers.get("cache-control", ""),
           "%s %s" % (r.status_code, r.text[:100]))
        r = C.get("/fleet/stutter", headers={"X-Api-Key": main.API_KEY})
        ok("F-13 pc 가 없으면 0/0", r.status_code == 200 and r.json() == ZERO)


def test_all():
    run_all([t_core, t_http])
    finish("test_fleet_stutter", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
