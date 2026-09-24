# -*- coding: utf-8 -*-
"""#114 B-DB10 (2026-09-24) — 서버가 관리하는 설정 키는 /setting/{key} POST 로 못 덮는다.

/setting 은 값을 100자에서 자른다. 세션으로 retired_pcs 를 한 번 POST 하면 메모리(RETIRED_PCS)는 그대로인데
저장본만 잘려 다음 재시작에 은퇴 PC 가 되살아났다. 순환 상태(acct_rotate)·텔레그램 오프셋도 같은 부류.
  S-1  서버 관리 키 POST → 400, 저장본 그대로
  S-2  대시보드·시드가 쓰는 키(sale_price·lan_seed·rental_kill …)는 그대로 200
  S-3  main.py 에서 set_setting 으로 직접 쓰는 키(상수 포함)가 전부 SERVER_MANAGED_SETTINGS 안 — 새 키가 새지 않게
  S-4  GET(세션)은 그대로 읽힌다
    cd updater/server && python -X utf8 tests/test_setting_reserved.py
"""
import os
import re

from _harness import main, db, ok, run_all, finish   # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

MIN_CHECKS = 9
C = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
SESS = main.new_session("main")
_EXPECT = {"retired_pcs", "no_account_pcs", "abyss_acc_all", "corridor_prog_all", "lan_cache_last", "acct_rotate",
           "acct_rotate.broken", "tg_poller_lease", "tg_offset", "rot_allow", "parsec_map"}


def S(method, url, **kw):
    C.cookies.clear()
    C.cookies.set("session", SESS)
    try:
        return getattr(C, method)(url, **kw)
    finally:
        C.cookies.clear()


async def t_reserved():
    long_list = ",".join("PC-%02d" % i for i in range(1, 40))          # 100자 넘는 콤마 목록
    await db.set_setting("retired_pcs", long_list)
    got = {}
    for k in sorted(_EXPECT | main.SERVER_MANAGED_SETTINGS):   # 고정 목록도 — 목록에서 빠진 키가 조용히 통과하지 않게
        got[k] = S("post", "/setting/" + k, json={"value": "x"}).status_code
    ok("S-1 서버 관리 키 POST 는 전부 400", set(got.values()) == {400}, str(got))
    ok("S-1b retired_pcs 저장본이 안 잘렸다", await db.get_setting("retired_pcs") == long_list)
    r = S("get", "/setting/retired_pcs")
    ok("S-4 GET(세션)은 그대로 읽힌다", r.status_code == 200 and r.json().get("value") == long_list, str(r.status_code))
    okk = {k: S("post", "/setting/" + k, json={"value": v}).status_code
           for k, v in (("sale_price", "1"), ("lan_seed", "http://172.30.1.70:8766"), ("awakening_preset", "{}"),
                        ("ai_kina_sold", "{}"), ("ai_dungeon_done", "{}"))}
    ok("S-2 대시보드·시드가 쓰는 키는 그대로 200", set(okk.values()) == {200}, str(okk))
    ok("S-2b lan_seed 는 저장된다(시드 재등록 길, C11)", await db.get_setting("lan_seed") == "http://172.30.1.70:8766")
    # ★S-5 소독 우회 (2026-09-24 아이온2 반증)★ — 값은 ns(tenant, key)=clean_pc_id 뒤 키에 저장된다. 날 키만 보던 판은
    #   retired%23pcs(→retired_pcs)·tg%40offset·rot%2Callow 가 200 으로 통과해 그 키를 덮었다.
    await db.set_setting("tg_offset", "777")
    await db.set_setting("rot_allow", "PC-10")
    enc = {k: S("post", "/setting/" + k, json={"value": "x"}).status_code
           for k in ("retired%23pcs", "tg%40offset", "rot%2Callow", "acct_rotate%2Ebroken", "parsec%3Amap", "no_account%3Cpcs")}
    ok("S-5 ★소독하면 서버 관리 키가 되는 이름도 400★(# @ , : <)", set(enc.values()) == {400}, str(enc))
    ok("S-5b 그 세 키 저장본이 그대로", (await db.get_setting("retired_pcs"), await db.get_setting("tg_offset"),
                                   await db.get_setting("rot_allow")) == (long_list, "777", "PC-10"))


def t_all_direct_writes_covered():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
    keys = set(re.findall(r'set_setting\("([^"]+)"', src))
    for const in re.findall(r"set_setting\((?:ns\(tenant, )?([A-Z_]+)\)?(?: \+ \"([^\"]*)\")?", src):
        m = re.search(r"^%s\s*=\s*\"([^\"]+)\"" % re.escape(const[0]), src, re.M)
        if m:
            keys.add(m.group(1) + const[1])
    ok("S-3 main.py 의 set_setting 직접 호출 키를 읽었다(0 이면 모양이 바뀐 것)", len(keys) >= 8, str(sorted(keys)))
    miss = sorted(keys - main.SERVER_MANAGED_SETTINGS)
    ok("S-3b ★그 키가 전부 SERVER_MANAGED_SETTINGS 안★(새 서버 관리 키가 /setting 으로 덮이지 않게)", not miss, "빠짐: %s" % miss)


def test_all():
    run_all([t_reserved, t_all_direct_writes_covered])
    finish("test_setting_reserved", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
