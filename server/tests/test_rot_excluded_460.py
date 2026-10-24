# -*- coding: utf-8 -*-
"""[대시보드] #460 순환 영구 제외(PC-05·PC-09) — 세 무장 길 거절 · 엔진 해제 · 설정 보존 · 해제 손잡이.
    cd updater/server && python -X utf8 tests/test_rot_excluded_460.py"""
from fastapi.testclient import TestClient

from _harness import main as M, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 10
C = TestClient(M.app, raise_server_exceptions=False)


def _sess():
    C.cookies.set("session", M.new_session("main"))


async def t_all():
    keep = set(M.ROT_EXCLUDED)
    try:
        M.ROT_EXCLUDED.clear()
        ok("RX-1 씨앗: 설정이 한 번도 안 쓰였으면 PC-05·PC-09 로 심는다", await M._rot_excl_restore() and
           M.ROT_EXCLUDED == {M.ns("main", "PC-05"), M.ns("main", "PC-09")}, str(M.ROT_EXCLUDED))
        M.ROT_EXCLUDED.clear()
        await M._rot_excl_restore()
        ok("RX-2 재배포(메모리 비움 → 복원)에도 남는다", M._rot_excluded("main", "PC-05b") and M._rot_excluded("main", "PC-09c"), "")
        ok("RX-3 다른 PC 는 안 건드린다", not M._rot_excluded("main", "PC-06") and not M._rot_excluded("main", "PC-10"), "")
        await M._rot_allow("main")
        await M.set_setting(M.ns("main", M.ROT_ALLOW_KEY), "*")
        for i, pc in enumerate(("PC-05", "PC-09b")):
            r, why = await M._rot_arm("main", pc, need_card=False)
            ok("RX-4.%d _rot_arm(▶시작·PageUp 공통 길)이 %s 를 거절하고 사유를 준다" % (i, pc), r is False and "순환 영구 제외" in why and "rot_excluded" in why, why)
        _sess()
        r = C.post("/rotate/PC-05", json={"on": True})
        ok("RX-5 POST /rotate 도 거절(응답에 사유)", r.status_code == 200 and r.json().get("ok") is False and "순환 영구 제외" in r.json().get("why", ""), r.text[:120])
        said = []
        o_say, o_save = M._rot_say, M._rot_save

        async def _say(t, pc, text, **kw):
            said.append(text)

        async def _save(force=False):
            pass
        M._rot_say, M._rot_save = _say, _save
        key = M.ns("main", "PC-05")
        st = {"stage": "hunting", "since": M._rot_now(), "armed_at": M._rot_now() - 9999, "day": M._kst_today_key(), "hops": 0, "visits": {}}
        M._ROT[key] = st
        M._ROT_STEP_CUR.update(key=key, st=st)
        try:
            await M._rot_step_pc("main", "PC-05", st, [{"pc_id": "PC-05", "status": "idle", "daily_progress": []}])
        finally:
            M._ROT_STEP_CUR.update(key=None, st=None)
            M._rot_say, M._rot_save = o_say, o_save
        ok("RX-6 이미 무장돼 있던 것도 엔진이 조용히 푼다", M._ROT.get(key) is not st and len(said) == 1 and "#460" in said[0], str(said))
        ok("RX-7 GET 목록", set(C.get("/admin/rot_excluded").json()["excluded"]) == {"PC-05", "PC-09"}, "")
        j = C.delete("/admin/rot_excluded/PC-05b").json()
        r2, why2 = await M._rot_arm("main", "PC-05", need_card=False)
        ok("RX-8 DELETE 로 풀면(접미사 무관) 거절이 사라진다 · 자동 무장은 안 됨", j["excluded"] == ["PC-09"] and "순환 영구 제외" not in why2, str((j, why2)))
        C.post("/admin/rot_excluded", json={"pcs": ["PC-05"]})
        ok("RX-9 POST 로 다시 더한다 · 잘못된 입력은 400", M._rot_excluded("main", "PC-05") and C.post("/admin/rot_excluded", json={"pcs": ["x"]}).status_code == 400, "")
        C.cookies.clear()
        ok("RX-10 세션 없으면 401", C.delete("/admin/rot_excluded/PC-09").status_code == 401 and C.get("/admin/rot_excluded").status_code == 401, "")
        ok("RX-11 서버 관리 설정 키(/setting 으로 못 덮는다)", "rot_excluded" in M.SERVER_MANAGED_SETTINGS, "")
        ok("RX-12 대시보드 토글이 있다", "rotExclToggle()" in M.HTML_DASHBOARD, "")
    finally:
        M.ROT_EXCLUDED.clear()
        M.ROT_EXCLUDED.update(keep)


def test_all():
    run_all([t_all])
    finish("test_rot_excluded_460", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
