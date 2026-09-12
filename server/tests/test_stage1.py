# -*- coding: utf-8 -*-
"""[대시보드] 1단계 수정분 — ★전부 실제 함수를 불러★ 확인한다 (2026-09-12).

★소스를 grep 하는 것은 「있다」지 「먹힌다」가 아니다★ — 여기서는 실제로 부르고,
루프가 막히는지는 ★동시에 도는 코루틴이 늦잠을 잤는지★ 로 잰다.

  python -X utf8 tests/test_stage1.py      (server/ 에서)
  python -m pytest tests/ -q               (pytest 도 된다 — 아래 test_* 함수)
"""
import asyncio
import json
import os
import sys
import time

from _harness import (main, db, aiosqlite, ok, FakeWS, Req, run_all, finish,   # noqa: E402
                      HTTPException)

MIN_CHECKS = 40


# ─────────────────────────────────────────────────────────────── 도우미
async def _loop_max_stall(coro, period=0.02):
    """coro 를 돌리는 동안 이벤트 루프가 얼마나 늦잠을 잤는지(최대 초)."""
    worst = [0.0]
    stop = [False]

    async def _probe():
        while not stop[0]:
            t0 = time.monotonic()
            await asyncio.sleep(period)
            worst[0] = max(worst[0], time.monotonic() - t0 - period)
    p = asyncio.create_task(_probe())
    try:
        await coro
    finally:
        stop[0] = True
        await p
    return worst[0]


async def t_version_async():
    """T2 — version.json 갱신이 루프를 안 막는다."""
    main._version_cache["data"] = {}
    main._version_cache["ts"] = 0
    _real = main._load_version_json

    def _slow():
        time.sleep(0.6)                       # 동기 httpx 가 느린 판을 흉내
        return {"exe": {"version": "9.9.9"}, "images": {"a.png": "h1"}}
    main._load_version_json = _slow
    try:
        stall = await _loop_max_stall(main._load_version_json_async())
        ok("T2 느린 version.json 갱신이 루프를 안 막는다(스레드)", stall < 0.3, "최대 늦잠 %.2f초" % stall)
        ok("T2-b 갱신 결과가 캐시에 남는다", main._version_cache["data"].get("exe", {}).get("version") == "9.9.9")
        # 동시 요청 24개 → 실제 갱신은 ★한 번★
        calls = [0]

        def _count():
            calls[0] += 1
            time.sleep(0.1)
            return {"exe": {"version": "1"}}
        main._version_cache["data"] = {}
        main._version_cache["ts"] = 0
        main._load_version_json = _count
        await asyncio.gather(*[main._load_version_json_async() for _ in range(24)])
        ok("T2-c 캐시가 비었을 때 24대가 몰려도 갱신은 한 번", calls[0] == 1, "실제 %d번" % calls[0])
    finally:
        main._load_version_json = _real
        main._version_cache["data"] = {"exe": {"version": "0"}, "images": {"known.png": "abc"}}
        main._version_cache["ts"] = time.time()


async def t_img():
    """T1 — /img 매니페스트 밖 404 · 스레드 · LRU."""
    main._IMG_PROXY_CACHE.clear()
    import hashlib
    # ★매니페스트 해시는 진짜 값으로★ — 다르면 서버가 「캐시가 옛것」이라며 다시 받는다(사고 413 가드).
    #   처음에 가짜 "abc" 를 넣어 캐시 시험이 헛실패했다. 그 가드가 사는 것도 아래 T1-e 로 본다.
    _h = hashlib.sha256(b"PNGDATA").hexdigest()
    main._version_cache["data"] = {"exe": {"version": "0"}, "images": {"known.png": _h}}
    main._version_cache["ts"] = time.time()
    hit = [0]
    _real = main._fetch_image_upstream

    def _fake(fname, sources):
        hit[0] += 1
        time.sleep(0.5)                       # 상류가 느린 판
        return b"PNGDATA", "raw", []
    main._fetch_image_upstream = _fake
    try:
        threw = None
        try:
            await main.serve_image("nope.png")
        except HTTPException as e:
            threw = e.status_code
        ok("T1 매니페스트에 없는 이름은 상류를 안 찌르고 404", threw == 404 and hit[0] == 0,
           "status=%s 상류호출=%d" % (threw, hit[0]))
        stall = await _loop_max_stall(main.serve_image("known.png"))
        ok("T1-b 느린 상류를 기다리는 동안 루프가 안 막힌다", stall < 0.3 and hit[0] == 1,
           "최대 늦잠 %.2f초" % stall)
        r = await main.serve_image("known.png")
        ok("T1-c 두 번째는 캐시(상류 호출 안 늘음)", hit[0] == 1 and r.body == b"PNGDATA")
        # 매니페스트 해시가 바뀌면(새 릴리스) 캐시를 믿지 않고 다시 받는다 — 사고 413 가드 회귀 확인
        main._version_cache["data"]["images"]["known.png"] = "changed"
        await main.serve_image("known.png")
        ok("T1-e 매니페스트 해시가 바뀌면 캐시를 버리고 다시 받는다", hit[0] == 2, "상류호출=%d" % hit[0])
        main._version_cache["data"]["images"]["known.png"] = _h
        # LRU — 상한을 넘기면 ★가장 오래된 것부터★ 빠진다, 통째로 안 비운다
        main._IMG_PROXY_CACHE.clear()
        for i in range(main._IMG_CACHE_MAX + 5):
            main._IMG_PROXY_CACHE["f%d.png" % i] = (b"x", "h", 1000 + i)
        main._version_cache["data"]["images"]["last.png"] = "z"
        await main.serve_image("last.png")
        n = len(main._IMG_PROXY_CACHE)
        ok("T1-d 캐시가 상한을 넘으면 오래된 것만 빠진다(통째 clear 아님)",
           n == main._IMG_CACHE_MAX and "f0.png" not in main._IMG_PROXY_CACHE
           and "last.png" in main._IMG_PROXY_CACHE, "남은 %d장" % n)
    finally:
        main._fetch_image_upstream = _real
        main._IMG_PROXY_CACHE.clear()


async def t_by_marker():
    """S1 — 사람 표식이 DB 에도 남아 재배달에 실린다 · 화면엔 안 보인다."""
    main.macro_ws_connections.clear()
    tok = main.new_session("main")
    r = await main._dispatch_macro_command("main", "PC-B1", "start", {})
    row = (await db.get_pending_commands("PC-B1", "all", 8))[0]
    ok("S1 재배달용 DB 행에 _by=human 이 있다", row["args"].get("_by") == "human", str(row["args"]))
    hist = main._strip_cmds(await db.get_recent_commands(5, ns_prefix=""), "main")
    shown = [c for c in hist if c["pc_id"] == "PC-B1"][0]
    ok("S1-b 화면용 이력에서는 _by 가 빠진다", "_by" not in json.loads(shown["args"]), str(shown["args"]))
    ok("S1-c 문자열이던 args 는 문자열 그대로", isinstance(shown["args"], str))
    ok("S1-d dict 도 같은 규칙", main._public_args({"a": 1, "_by": "human"}) == {"a": 1})
    ok("S1-e 깨진 JSON 은 손대지 않는다", main._public_args("{not json") == "{not json")
    # set_info 마스킹본에도 표식이 남는다
    await main._dispatch_macro_command("main", "PC-B2", "set_info", {"kv": {"계정1_비번": "pw", "계정1_아이디": "id"}})
    row2 = (await db.get_pending_commands("PC-B2", "all", 8))[0]
    ok("S1-f set_info 마스킹본에도 _by 가 있고 비번은 가려진다",
       row2["args"].get("_by") == "human" and row2["args"]["kv"]["계정1_비번"] == "***", str(row2["args"]))


def t_bug_list():
    """S2 — 목록 훑기 캐시 · 버스트 · PC 단위 필터."""
    bdir = main.tenant_bugs_dir("main")
    os.makedirs(bdir, exist_ok=True)
    for f in os.listdir(bdir):
        os.remove(os.path.join(bdir, f))
    for nm in ("PC-01_20260912_010101_a.png", "PC-01b_20260912_010102_b.png", "PC-02_20260912_010103_c.png"):
        with open(os.path.join(bdir, nm), "wb") as fh:
            fh.write(b"x" * 10)
    main._bug_cache_bust("main")
    scans = [0]
    _real = os.scandir

    def _counting(p):
        scans[0] += 1
        return _real(p)
    os.scandir = _counting
    try:
        a = main._list_bug_files("main")
        b = main._list_bug_files("main", "PC-01b")
        ok("S2 목록이 최신순·크기 포함으로 온다", len(a) == 3 and a[0]["filename"].startswith("PC-02") and a[0]["size"] == 10, str(a))
        ok("S2-b PC 필터는 베이스로 (PC-01b → PC-01 것까지)", sorted(x["filename"][:6] for x in b) == ["PC-01_", "PC-01b"], str(b))
        ok("S2-c 두 번 불러도 폴더는 한 번만 훑는다", scans[0] == 1, "훑기 %d회" % scans[0])
        with open(os.path.join(bdir, "PC-03_20260912_010104_d.png"), "wb") as fh:
            fh.write(b"y")
        ok("S2-d 버스트 전엔 캐시(새 파일 안 보임)", len(main._list_bug_files("main")) == 3)
        main._bug_cache_bust("main")
        ok("S2-e 버스트하면 다시 훑어 새 파일이 보인다", len(main._list_bug_files("main")) == 4 and scans[0] == 2)
    finally:
        os.scandir = _real


async def t_setting_allowlist():
    """S4 — API 키 조회는 허용 목록만."""
    await db.set_setting("sale_price", "159999")
    await db.set_setting("corridor_prog_all", "{\"x::PC-01\": 1}")
    r = await main.get_setting_ep("sale_price", Req(api_key="testkey"))
    ok("S4 매크로가 읽는 키는 API 키로 된다", json.loads(bytes(r.body))["value"] == "159999")
    threw = None
    try:
        await main.get_setting_ep("corridor_prog_all", Req(api_key="testkey"))
    except HTTPException as e:
        threw = e.status_code
    ok("S4-b 그 밖의 키는 API 키로 401", threw == 401, "status=%s" % threw)
    r2 = await main.get_setting_ep("corridor_prog_all", Req(api_key=None, session=main.new_session("main")))
    ok("S4-c 세션으로는 된다", r2.status_code == 200)


async def t_purge_and_fv_guard():
    """S21 /check/purge main 전용 · S7 FV 토큰 추측 카운터."""
    r = await main.check_purge(Req(api_key="testkey"))
    ok("S21 main 키로 purge 된다", r.status_code == 200 if hasattr(r, "status_code") else isinstance(r, dict))
    main.TENANTS["zz"] = {"password": "p", "api_key": "zzkey", "expires": "", "chat_id": ""}
    main.KEY_TO_TENANT["zzkey"] = "zz"
    r2 = await main.check_purge(Req(api_key="zzkey"))
    ok("S21-b 지인 키로는 401", getattr(r2, "status_code", 200) == 401)
    main._KEY_FAILS.clear()
    main.FV_TOKEN = "fvsecret"

    class _R:
        headers = {"X-FV-Token": "wrong"}
        client = type("C", (), {"host": "10.9.9.9"})()
    for _ in range(main.KEY_MAX_FAILS):
        main._fv_guard(_R())
    blocked = main._fv_guard(_R())
    ok("S7 FV 토큰 추측이 누적되면 429", getattr(blocked, "status_code", None) == 429,
       "status=%s" % getattr(blocked, "status_code", None))

    class _G(_R):
        headers = {"X-FV-Token": "fvsecret"}
        client = type("C", (), {"host": "10.9.9.10"})()
    ok("S7-b 맞는 토큰(다른 IP)은 통과", main._fv_guard(_G()) is None)
    main._KEY_FAILS.clear()


async def t_mute_tenant():
    """S8 — 음소거가 테넌트별이다."""
    main._TG_MUTE.clear()
    main.TENANTS["zz"] = {"password": "p", "api_key": "zzkey", "expires": "", "chat_id": "1"}
    await main.telegram_mute("PC-16", Req({"hours": 1}, api_key=None, session=main.new_session("zz")))
    ok("S8 지인이 PC-16 을 음소거해도 본판 PC-16 은 안 꺼진다",
       main._tg_muted("main", "PC-16") == 0 and main._tg_muted("zz", "PC-16") > 0)
    lst = json.loads(bytes((await main.telegram_mute_list(Req(api_key=None, session=main.new_session("main")))).body))
    ok("S8-b 본판 목록에 지인 것이 안 보인다", lst["muted"] == {}, str(lst))
    lst2 = json.loads(bytes((await main.telegram_mute_list(Req(api_key=None, session=main.new_session("zz")))).body))
    ok("S8-c 지인 목록엔 접두사 없이 보인다", "PC-16" in lst2["muted"], str(lst2))
    main._TG_MUTE.clear()


async def t_rot_say():
    """S13 — 로그가 실제 전송 결과를 말한다 · ⛔ 는 화면 배너도."""
    main.TELEGRAM_BOT_TOKEN = "T"
    main.TENANTS["main"]["chat_id"] = "42"
    banners = []

    async def _fake_alert(tenant, pc, kind, message, speak=True, say=""):
        banners.append((pc, kind, message))
    _ra, _rt = main.push_alert, main.tg_send_text
    main.push_alert = _fake_alert

    async def _fail(chat, text, **kw):
        raise RuntimeError("텔레그램 죽음")
    main.tg_send_text = _fail
    try:
        await main._rot_say("main", "PC-R1", "⛔ 정지 시험")
        logs = await db.get_logs("PC-R1", limit=5)
        msg = (logs[0].get("message") if logs else "") or ""
        ok("S13 텔레그램이 죽으면 로그가 「중계 실패」라고 적는다", "중계 실패" in msg, msg[:80])
        ok("S13-b ⛔ 는 대시보드 배너로도 나간다", banners and banners[0][0] == "PC-R1", str(banners))

        async def _okay(chat, text, **kw):
            return 7
        main.tg_send_text = _okay
        await main._rot_say("main", "PC-R2", "⛔ 정지 시험2")
        msg2 = (await db.get_logs("PC-R2", limit=5))[0]["message"]
        ok("S13-c 실제로 보내지면 「중계 전송」", "중계 전송" in msg2, msg2[:80])
    finally:
        main.push_alert, main.tg_send_text = _ra, _rt


async def t_rot_engine_guards():
    """S5 옛 무장 보호 · S14 재무장 사유 정리 · S10 예외 카운터 · S11 깨진 저장본."""
    key = main.ns("main", "PC-E1")
    # S5 — 단계 함수가 옛 무장을 보던 사이 재무장되면 정지가 새 무장을 못 지운다
    old = {"stage": "hunting", "since": 0}
    new = {"stage": "hunting", "since": 1}
    main._ROT[key] = new
    main._ROT_STEP_CUR.update(key=key, st=old)          # 단계 함수는 옛 것을 들고 있었다
    await main._rot_stop("main", "PC-E1", "옛 판단으로 정지")
    ok("S5 옛 무장을 가리킨 정지가 새 무장을 안 지운다", main._ROT.get(key) is new)
    main._ROT_STEP_CUR.update(key=key, st=new)
    await main._rot_stop("main", "PC-E1", "진짜 정지")
    ok("S5-b 현재 무장을 가리키면 정지된다", key not in main._ROT)
    main._ROT_STEP_CUR.update(key=None, st=None)
    # S14 — 재무장이 옛 해제 사유를 지운다
    main._ROT_GONE_WHY[key] = "rot_stop: 옛것"
    await db.set_setting(main.ns("main", main.ROT_ALLOW_KEY), "*")
    okk, why = await main._rot_arm("main", "PC-E1")
    ok("S14 재무장하면 옛 해제 사유가 사라진다", okk and key not in main._ROT_GONE_WHY, "%s %s" % (okk, why))
    main._ROT.pop(key, None)
    # S10 — 단계가 계속 터지면 N틱째 알린다 (엔진을 진짜로 몇 틱 돌린다)
    said = []

    async def _say(tenant, pc, text, *a, **k):
        said.append(text)
    _rs, _rst, _tick, _grace = main._rot_say, main._rot_step_pc, main.ROT_TICK, main.ROT_BOOT_GRACE_S

    async def _boom(*a, **k):
        raise RuntimeError("단계 폭발")
    # 엔진은 부팅 직후 ROT_BOOT_GRACE_S(운영 25초)를 잔다 — 시험은 0 으로 놓는다
    main._rot_say, main._rot_step_pc, main.ROT_TICK, main.ROT_BOOT_GRACE_S = _say, _boom, 0.02, 0
    main._ROT[key] = {"stage": "hunting", "since": main._rot_now()}
    eng = asyncio.create_task(main._rot_engine())
    for _ in range(80):
        await asyncio.sleep(0.02)
        if said:
            break
    eng.cancel()
    try:
        await eng
    except (asyncio.CancelledError, Exception):
        pass
    st = main._ROT.get(key) or {}
    main._rot_say, main._rot_step_pc, main.ROT_TICK, main.ROT_BOOT_GRACE_S = _rs, _rst, _tick, _grace
    ok("S10 단계 예외를 세고 %d틱 연속이면 알린다" % main.ROT_ERR_ALARM_N,
       bool(said) and int(st.get("err_n") or 0) >= main.ROT_ERR_ALARM_N and "단계 폭발" in st.get("err_last", ""),
       "err_n=%s 알림=%d" % (st.get("err_n"), len(said)))
    ok("S10-b 알림은 한 번만(N 틱째에만)", len(said) == 1, "%d번" % len(said))
    main._ROT.pop(key, None)
    # S11 — 깨진 저장본은 보관되고 덮이지 않는다
    await db.set_setting(main.ROT_KEY, "{깨진 json")
    main.TELEGRAM_BOT_TOKEN = "T"
    main.TENANTS["main"]["chat_id"] = "42"
    sent = []

    async def _tg(chat, text, **kw):
        sent.append(text)
        return 1
    _rt = main.tg_send_text
    main.tg_send_text = _tg
    await main._rot_load()
    main.tg_send_text = _rt
    ok("S11 깨진 저장본이 .broken 으로 보관된다", (await db.get_setting(main.ROT_KEY + ".broken")) == "{깨진 json")
    ok("S11-b 복원 실패를 텔레그램으로 알린다", sent and "복원에 실패" in sent[0], str(sent)[:80])
    await db.set_setting(main.ROT_KEY, "")


async def t_fv():
    """S17 완료 규칙 · S18 버그 합산 · S19 같은 초 유실 · S8-FV 일괄 ok."""
    v = main._fv_pc_view({"pc_id": "PC-F1", "status": "idle",
                          "daily_progress": [{"slot": 1, "completed": True, "today": False},
                                             {"slot": 2, "completed": True},
                                             {"slot": 3, "completed": False}]}, None)
    ok("S17 어제 완주(today=false)는 오늘 완료로 안 센다", v["today"]["slots_done"] == 1 and v["today"]["slots_left"] == 2,
       str(v["today"]))
    # S19 — 같은 초에 걸린 이벤트는 통째로 다음 장으로
    for i in range(6):
        await db.insert_log("PC-F2", "info", "e%d" % i, created_at="2026-09-12T00:00:0%d" % (1 if i < 4 else 2))
    main.FV_TOKEN = "fvsecret"
    r = await main.fv_events(Req(api_key=None, headers={"X-FV-Token": "fvsecret"}), since="2026-09-11T00:00:00", limit=3)
    body = json.loads(bytes(r.body)) if hasattr(r, "body") else r
    ok("S19 limit 3 인데 경계 초에 4개가 걸리면 그 초를 통째로 다음 장으로(0건이 아니라 자른다)",
       body.get("truncated") is True and body.get("count") in (3, 4) and "next_since" in body, str({k: body.get(k) for k in ("count", "truncated", "next_since")}))
    # 명확한 경우: 1초에 2개, 2초에 2개, limit 3 → 2개만 주고 next_since=1초
    await db.init_db()
    for i in range(4):
        await db.insert_log("PC-F3", "info", "g%d" % i, created_at="2026-09-12T00:10:0%d" % (1 if i < 2 else 2))
    r2 = await main.fv_events(Req(api_key=None, headers={"X-FV-Token": "fvsecret"}), since="2026-09-12T00:09:00", limit=3)
    b2 = json.loads(bytes(r2.body))
    ok("S19-b 경계 초를 반으로 안 자른다(2건·next_since=01초)",
       b2["count"] == 2 and b2["next_since"].endswith("00:10:01") and b2["truncated"] is True, str({k: b2.get(k) for k in ("count", "truncated", "next_since")}))


async def t_lan_cache():
    """S25 — 내부망 주소 캐시가 재배포를 넘긴다."""
    main._lan_cache_last.clear()
    main._lan_cache_last["PC-L1"] = "http://172.30.1.9:8765/?k=abc"
    main._lan_cache_dirty[0] = True
    await db.set_setting(main.LAN_CACHE_KEY, json.dumps(main._lan_cache_last))
    main._lan_cache_last.clear()
    await main._lan_cache_restore()
    ok("S25 저장한 내부망 주소가 복원된다", main._lan_cache_last.get("PC-L1", "").endswith("k=abc"))
    main._lan_cache_dirty[0] = False
    await db.upsert_status("PC-L2", {"pc_id": "PC-L2", "status": "hunting", "lan_url": "http://x/1"})
    await main._build_full_state("main")
    ok("S25-b 새 주소가 오면 저장 표시가 켜진다", main._lan_cache_dirty[0] is True)


def t_js():
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
    js = max(re.findall(r"<script\b[^>]*>(.*?)</script>", src, re.S | re.I), key=lambda b: b.count("\n"))
    ok("S12 JS 가 rotate 명령의 armed 거부도 본다", "(args && args.rotate)" in js and "('armed' in j) && !j.armed" in js)


def test_all():
    run_all([t_version_async, t_img, t_by_marker, t_bug_list, t_setting_allowlist,
             t_purge_and_fv_guard, t_mute_tenant, t_rot_say, t_rot_engine_guards, t_fv, t_lan_cache])
    t_js()
    finish("test_stage1", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
