# -*- coding: utf-8 -*-
"""[대시보드] B 묶음(2026-09-23) — FarmView API(/api/fv/*) · DB 층 확정 버그 시험.

반증 에이전트 probe(b/fvdb/tests/probe_fvdb.py)가 잡은 것을 ★실제 호출★ 로 고정한다.
각 검사는 고치기 전 서버(base)에서는 FAIL, 고친 서버에서는 PASS 여야 한다.
  B-FV1  스냅샷 캐시 미스 갈래도 ?raw=1 없이는 raw 를 안 준다
  B-FV2  아직 안 닫힌 초의 행을 내주지 않아 같은 초 늦은 행이 유실되지 않는다
  B-FV3  20초 늦게 온 업데이터 배치 줄이 커서 뒤에 떨어지지 않는다
  B-DB2  시계가 빠른 업데이터 줄이 전역 커서를 미래로 밀지 않는다
  B-FV4  업데이트 큐 id 가 재배포(0 초기화) 뒤에도 팜뷰 커서보다 크다
  B-FV5  한 출처가 limit 을 채우면 truncated · 남의 테넌트 행이 커서를 멈추지 않는다
  B-FV6  set_info args 의 이메일·휴대폰·_by·_note 가 FV 로 안 나간다
  B-FV9  숫자 파라미터 오류가 가드보다 먼저 422 로 새지 않는다
  B-FV10 id 없는/문자열 id ack · B-NEW2 배열 본문 ack
  B-DB3  업데이터 큐 만료·덮어쓰기 · B-DB5 은퇴 카드 부활 · B-DB9 삭제 잔재 · B-NEW1 ai_kina_sold 상한
  B-DB7  끝난 명령 30일 정리 · 명령 이벤트 쿼리 인덱스
"""
import json
import time
from datetime import datetime, timezone, timedelta

import aiosqlite
from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 37
TOK = "fvsecret-b"
FMT = "%Y-%m-%dT%H:%M:%S"
H = {"X-FV-Token": TOK}


def _utc(delta_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).strftime(FMT)


def _body(r):
    return json.loads(bytes(r.body))


async def _ev(since, limit=500):
    return _body(await main.fv_events(Req(api_key=None, headers=H), since=since, limit=limit))


async def _drain(since, limit=500, pages=10):
    """팜뷰 pull_events 처럼 truncated 면 바로 다음 장. (메시지 목록, 마지막 next_since)"""
    got = []
    for _ in range(pages):
        d = await _ev(since, limit)
        got += [e.get("message") for e in d["events"]]
        since = d["next_since"]
        if not d["truncated"]:
            break
    return got, since


async def t_fv1_raw_on_miss():
    main.FV_TOKEN = TOK
    await db.upsert_status("PC-B1", {"pc_id": "PC-B1", "status": "idle"})
    main._fv_snap["body"] = None
    b = _body(await main.fv_snapshot(Req(api_key=None, headers=H)))
    pcs = b.get("pcs") or {}
    ok("B-FV1 캐시 미스 응답에도 raw 가 없다", b.get("cached") is False and pcs
       and all("raw" not in v for v in pcs.values()), str(b.get("cached")))
    main._fv_snap["body"] = None
    r = Req(api_key=None, headers=H)
    r.query_params = {"raw": "1"}
    b2 = _body(await main.fv_snapshot(r))
    ok("B-FV1 ?raw=1 이면 캐시 미스에도 raw 가 있다", any("raw" in v for v in (b2.get("pcs") or {}).values()))


async def t_fv2_same_second():
    main.FV_TOKEN = TOK
    T = _utc()
    await db.insert_log("PC-B2", "info", "B2-first", created_at=T)
    d1 = await _ev(_utc(-60))
    first = [e.get("message") for e in d1["events"]]
    ok("B-FV2 방금(안 닫힌) 초의 행은 아직 안 준다", "B2-first" not in first, str(first[-3:]))
    await db.insert_log("PC-B2", "info", "B2-same-second-late", created_at=T)   # 같은 초, 폴링 뒤 커밋
    time.sleep(getattr(main, "FV_EVENT_SETTLE_S", 2) + 1.2)
    got, _ = await _drain(d1["next_since"])
    ok("B-FV2 폴링 뒤 같은 초로 찍힌 행도 결국 온다", "B2-same-second-late" in got, str(got[-4:]))
    ok("B-FV2 첫 행도 빠짐없이 온다(한 번)", (first + got).count("B2-first") == 1, str(first[-2:] + got[-4:]))


async def t_fv3_db2_updater_ts():
    main.FV_TOKEN = TOK
    await db.insert_log("PC-B3L", "info", "B3-anchor", created_at=_utc(-5))
    d = await _ev(_utc(-60))
    cur = d["next_since"]
    ok("B-FV3 준비: 앵커 행으로 커서가 지금-5초 근처까지 왔다", cur >= _utc(-10), cur)
    now = time.time()
    await main.receive_updater_logs("PC-B3", Req({"logs": [
        {"level": "info", "message": "UPD-late-20s", "ts": now - 20},
        {"level": "info", "message": "UPD-future-1h", "ts": now + 3600}]}))
    rows = await db.get_logs("PC-B3.upd", limit=10)
    fut = next((r for r in rows if "UPD-future-1h" in r["message"]), None)
    ok("B-DB2 미래 시각 줄은 서버 지금으로 눌려 저장된다", fut is not None and fut["created_at"] <= _utc(1),
       str(fut))
    late = next((r for r in rows if "UPD-late-20s" in r["message"]), None)
    ok("B-FV3 커서가 지나간 옛 시각 줄은 커서 뒤로 올려 적힌다", late is not None and late["created_at"] > cur,
       "%s vs cur %s" % (late and late["created_at"], cur))
    time.sleep(getattr(main, "FV_EVENT_SETTLE_S", 2) + 1.2)
    got, nxt = await _drain(cur)
    ok("B-FV3 늦게 온 업데이터 배치 줄이 팜뷰 폴링에 나온다", any("UPD-late-20s" in (m or "") for m in got), str(got[-4:]))
    ok("B-DB2 커서(next_since)가 미래로 튀지 않는다", nxt <= _utc(1), nxt)
    await db.insert_log("PC-B3L", "info", "B3-after", created_at=None)
    time.sleep(getattr(main, "FV_EVENT_SETTLE_S", 2) + 1.2)
    got2, _ = await _drain(nxt)
    ok("B-DB2 그 뒤 정상 행이 계속 보인다(함대 정전 없음)", "B3-after" in got2, str(got2[-3:]))
    ok("B-FV3 원래 찍힌 시각은 줄 끝에 남는다", late is not None and "찍힘" in late["message"], str(late))


async def t_fv4_seq():
    main.FV_TOKEN = TOK
    main.FV_UPDCMD_SEQ[0] = 0              # 재배포 흉내
    main.FV_UPDCMD_QUEUE.clear()
    main._fv_updcmd_push("main", "PC-B4", "updater")
    cid = main.FV_UPDCMD_QUEUE[main.ns("main", "PC-B4")]["id"]
    b = _body(await main.fv_updcmd_list(Req(api_key=None, headers=H), since=7))
    ok("B-FV4 재배포 뒤 id 가 팜뷰 옛 커서(7)보다 크다", cid > 7, str(cid))
    ok("B-FV4 since=7 인 팜뷰가 새 명령을 받는다", [c["pc"] for c in b["cmds"]] == ["PC-B4"], str(b))
    main._fv_updcmd_push("main", "PC-B4x", "restart")
    ok("B-FV4 다음 id 는 더 크다(단조 증가)", main.FV_UPDCMD_QUEUE[main.ns("main", "PC-B4x")]["id"] > cid)
    main.FV_UPDCMD_QUEUE.clear()


async def t_fv5_trunc():
    main.FV_TOKEN = TOK
    await db.insert_log("PC-B5L", "info", "X", created_at="2026-09-13T01:00:01")
    for i in range(3):
        await db.insert_log("PC-B5L", "info", "Y%d" % i, created_at="2026-09-13T01:00:02")
    d = await _ev("2026-09-13T01:00:00", 3)
    ok("B-FV5 로그만 딱 limit 줄이어도 넘치면 truncated", d["truncated"] is True, str({k: d[k] for k in ("count", "truncated", "next_since")}))
    got, _ = await _drain("2026-09-13T01:00:00", 3)
    mine = sorted(m for m in got if m in ("X", "Y0", "Y1", "Y2"))
    ok("B-FV5 잘린 초의 나머지(Y2)까지 전부 온다(각 한 번)", mine == ["X", "Y0", "Y1", "Y2"], str(mine))
    # 남의 테넌트 행이 LIMIT 을 먹어 커서가 멈추던 것
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.executemany("INSERT INTO logs(pc_id,level,message,created_at) VALUES(?,?,?,?)",
                            [("t9::PC-X", "info", "other%d" % i, "2026-09-13T03:00:01") for i in range(5)])
        await c.commit()
    await db.insert_log("PC-B5M", "info", "mine-after-other-tenant", created_at="2026-09-13T03:00:02")
    got2, _ = await _drain("2026-09-13T03:00:00", 3, pages=5)
    ok("B-FV5 남의 테넌트 행이 limit 을 채워도 내 행이 나온다", "mine-after-other-tenant" in got2, str(got2))
    # 명령 출처: 이벤트 시각순이라야 경계가 맞다 — 옛 id 가 나중에 ack 되면 시각은 뒤다
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("INSERT INTO commands(pc_id,command,args,status,created_at,updated_at) "
                        "VALUES('PC-B5C','stop','{}','acked','2026-09-13T05:00:01','2026-09-13T05:00:09')")
        for i in range(3):
            await c.execute("INSERT INTO commands(pc_id,command,args,status,created_at) "
                            "VALUES('PC-B5C','start','{}','pending','2026-09-13T05:00:0%d')" % (2 + i))
        await c.commit()
    seen = []
    since = "2026-09-13T05:00:00"
    for _ in range(6):
        dd = await _ev(since, 2)
        seen += [(e["command"], e["status"]) for e in dd["events"]
                 if e["type"] == "command" and e.get("pc") == "PC-B5C"]
        since = dd["next_since"]
        if not dd["truncated"]:
            break
    ok("B-FV5 명령이 limit 에 걸려도 4건 전부 · 시각순", seen == [("start", "pending")] * 3 + [("stop", "acked")], str(seen))


async def t_fv6_mask():
    main.FV_TOKEN = TOK
    args = {"kv": {"계정1_아이디": "idb6", "계정1_이메일": "leak@example.com", "계정1_휴대폰": "010-1234",
                   "계정1_비번": "***", "계정1_PIN": "***"}, "_note": "5칸", "_by": "human"}
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("INSERT INTO commands(pc_id,command,args,status,created_at) VALUES(?,?,?,?,?)",
                        ("PC-B6", "set_info", json.dumps(args, ensure_ascii=False), "acked", "2026-09-13T07:00:01"))
        await c.commit()
    d = await _ev("2026-09-13T07:00:00")
    ev = next((e for e in d["events"] if e.get("pc") == "PC-B6"), None)
    a = str(ev and ev.get("args"))
    ok("B-FV6 events: 이메일·휴대폰 평문이 안 나간다", ev is not None and "leak@example.com" not in a and "010-1234" not in a, a)
    ok("B-FV6 events: _by·_note 가 안 나간다", ev is not None and "_by" not in a and "_note" not in a, a)
    ok("B-FV6 events: 아이디는 그대로 · 모양(문자열 JSON) 그대로", ev is not None and isinstance(ev.get("args"), str)
       and json.loads(ev["args"])["kv"]["계정1_아이디"] == "idb6", a)
    await db.upsert_status("PC-B6", {"pc_id": "PC-B6", "status": "idle"})
    p = _body(await main.fv_pc_detail("PC-B6", Req(api_key=None, headers=H), logs=10))
    pa = json.dumps(p.get("commands"), ensure_ascii=False)
    ok("B-FV6 pc 상세 commands 도 가린다", "leak@example.com" not in pa and "_by" not in pa and "idb6" in pa, pa[:120])
    row = next((r for r in await db.get_recent_commands(50) if r.get("pc_id") == "PC-B6"), None)
    ok("B-FV6 DB 원문은 그대로(재배달용)", row is not None and "leak@example.com" in row["args"])


def t_fv9_422():
    from fastapi.testclient import TestClient
    main.FV_TOKEN = TOK
    C = TestClient(main.app, raise_server_exceptions=False)
    r = C.get("/api/fv/events", params={"limit": "abc"})
    ok("B-FV9 토큰 없는 limit=abc 는 401(422 아님)", r.status_code == 401, "%s %s" % (r.status_code, r.text[:60]))
    r = C.get("/api/fv/events", params={"limit": "abc"}, headers=H)
    ok("B-FV9 토큰 있는 limit=abc 는 계약 400 {error,code}", r.status_code == 400 and r.json().get("code") == 400, r.text[:80])
    r = C.get("/api/fv/updcmd", params={"since": "x"}, headers=H)
    ok("B-FV9 updcmd since=x 는 계약 400", r.status_code == 400 and "error" in r.json(), r.text[:80])
    r = C.get("/api/fv/pc/PC-B1", params={"logs": "zz"})
    ok("B-FV9 pc 상세 logs=zz 토큰 없으면 401", r.status_code == 401, "%s" % r.status_code)
    main.FV_TOKEN = ""
    r = C.get("/api/fv/events", params={"limit": "abc"})
    ok("B-FV9 FV_TOKEN 미설정이면 limit=abc 도 404(숨김)", r.status_code == 404, "%s" % r.status_code)
    main.FV_TOKEN = TOK


async def t_fv10_ack():
    main.FV_TOKEN = TOK
    main.FV_UPDCMD_QUEUE.clear()
    main._fv_updcmd_push("main", "PC-B10", "restart")
    k = main.ns("main", "PC-B10")
    cid = main.FV_UPDCMD_QUEUE[k]["id"]
    r = await main.fv_updcmd_ack(Req({"pc": "PC-B10"}, api_key=None, headers=H))
    ok("B-FV10 id 없는 ack 는 400 이고 큐를 안 지운다", r.status_code == 400 and k in main.FV_UPDCMD_QUEUE, str(r.status_code))
    b = _body(await main.fv_updcmd_ack(Req({"pc": "PC-B10", "id": str(cid)}, api_key=None, headers=H)))
    ok("B-FV10 문자열 id 도 정수로 비교해 지운다", b.get("removed") is True and k not in main.FV_UPDCMD_QUEUE, str(b))
    try:
        r2 = await main.fv_updcmd_ack(Req([1], api_key=None, headers=H))
        code = r2.status_code
    except Exception as e:
        code = "exc:%s" % type(e).__name__
    ok("B-NEW2 배열 본문 ack 는 500 이 아니라 400", code == 400, str(code))


async def t_db3_updq():
    for c in ("restart", "update", "restart"):
        await db.insert_updater_command("PC-B3Q", c, {})
    got = []
    for _ in range(4):
        x = await db.get_pending_updater_command("PC-B3Q", "all")
        if not x:
            break
        got.append(x["command"])
        await db.ack_updater_command(x["id"])
    ok("B-DB3 밀린 상태변경 명령은 ★종류마다★ 최신 1건만 배달(배포 반증 #2 — update 가 restart 에 안 덮인다)",
       got == ["update", "restart"], str(got))
    st = [r["status"] for r in await db.recent_updater_commands(50) if r["pc_id"] == "PC-B3Q"]
    ok("B-DB3 옛 것은 superseded 로 보인다(조용히 안 지움)", sorted(st) == ["acked", "acked", "superseded"], str(st))
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime(FMT)
    async with aiosqlite.connect(db.DB_PATH) as c:
        cur = await c.execute("INSERT INTO updater_commands(pc_id,command,args,status,created_at) "
                              "VALUES('PC-B3E','update','{}','pending',?)", (old,))
        oid = cur.lastrowid
        await c.commit()
    x = await db.get_pending_updater_command("PC-B3E", "all")
    ok("B-DB3 30시간 된 대기 명령은 배달 안 한다", x is None, str(x))
    await db.ack_updater_command(oid)
    st2 = [r["status"] for r in await db.recent_updater_commands(50) if r["id"] == oid]
    ok("B-DB3 만료 표시가 남고 늦은 ack 로 안 뒤집힌다", st2 == ["expired"], str(st2))
    await db.insert_updater_command("PC-B3S", "screenshot", {})
    await db.insert_updater_command("PC-B3S", "update", {})
    x1 = await db.get_pending_updater_command("PC-B3S", "all")
    ok("B-DB3 screenshot 은 덮어쓰기 대상이 아니다(순서대로 먼저)", x1 and x1["command"] == "screenshot", str(x1))


async def t_db5_retired():
    main.RETIRED_PCS.add(main.ns("main", "PC-B7"))
    await db.delete_pc_all_data(main.ns("main", "PC-B7"))
    await main.updater_report_status("PC-B7", Req({"macro_state": "running"}))
    rows = await main._build_full_state("main")
    ok("B-DB5 은퇴 id 의 업데이터 보고로 카드가 부활하지 않는다", not any(r.get("pc_id") == "PC-B7" for r in rows))
    await db.upsert_status(main.ns("main", "PC-B7b"), {"pc_id": "PC-B7b", "status": "idle"})
    rows = await main._build_full_state("main")
    sib = next((r for r in rows if r.get("pc_id") == "PC-B7b"), None)
    ok("B-DB5 형제 카드는 업데이터 상태를 계속 조인한다", sib is not None and sib.get("_updater_state") == "running", str(sib and sib.get("_updater_state")))
    main.RETIRED_PCS.discard(main.ns("main", "PC-B7"))
    await db.delete_pc_all_data(main.ns("main", "PC-B7b"))


async def t_db9_delete():
    k = main.ns("main", "PC-B9")
    await db.tg_map_put(9_000_001, k, "chat-b9")
    await db.upsert_slot_filters(k, {1: False})
    await db.upsert_nightmare_progress(k, 1, "몽충I", {})
    await db.delete_pc_all_data(k)
    # ★2026-09-23 병합 반증으로 뒤집었다★ — 카드 삭제(청소)는 같은 id 로 되살아나므로 캡차 답장 경로를 남긴다
    #   (지우면 그 사진에 단 답장이 버려졌다). 은퇴(purge_all)만 처리됨(kind='done')으로 묻는다 — test_merge_b2 M4.
    _r = await db.tg_map_get(9_000_001)
    ok("B-DB9 카드 삭제(청소)는 텔레그램 답장 경로를 남긴다(같은 id 로 부활)", _r is not None and _r.get("kind") == "captcha", str(_r))
    ok("B-DB9 카드 삭제(청소)는 슬롯 필터를 남긴다(재보고 대비)", (await db.get_slot_filters(k)) != {})
    try:
        await db.delete_pc_all_data(k, purge_all=True)
        purged = (await db.get_slot_filters(k)) == {} and (await db.get_nightmare_progress(k)) == []
    except TypeError as e:
        purged = False
    ok("B-DB9 은퇴(purge_all)는 슬롯 필터·악몽 진행까지 지운다", purged)
    _r = await db.tg_map_get(9_000_001)
    ok("B-DB9 은퇴는 답장 경로를 대기 후보에서 뺀다(처리됨으로 묻음)",
       _r is not None and _r.get("kind") == "done" and not await db.tg_map_recent("chat-b9", 3600), str(_r))


async def t_new1_cap():
    v = json.dumps({"day": "2026-09-23", "keys": ["PC-%02d" % i for i in range(1, 25)]}, separators=(",", ":"))
    orig = main._require_session
    main._require_session = lambda request: "main"
    try:
        await main.set_setting_ep("ai_kina_sold", Req({"value": v}))
    finally:
        main._require_session = orig
    back = await db.get_setting("ai_kina_sold") or ""
    try:
        okp = json.loads(back) == json.loads(v)
    except Exception:
        okp = False
    ok("B-NEW1 ai_kina_sold 24대치 JSON 이 안 잘린다", okp, "%d→%d" % (len(v), len(back)))


async def t_db7_prune():
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(FMT)
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("INSERT INTO commands(pc_id,command,args,status,created_at,updated_at) VALUES('PC-B8','stop','{}','acked',?,?)", (old, old))
        await c.execute("INSERT INTO commands(pc_id,command,args,status,created_at) VALUES('PC-B8','set_slot_filter','{}','pending',?)", (old,))
        await c.execute("INSERT INTO updater_commands(pc_id,command,args,status,created_at) VALUES('PC-B8','update','{}','acked',?)", (old,))
        await c.commit()
    await db.init_db()
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT COUNT(*) FROM commands WHERE pc_id='PC-B8'") as cur:
            n_boot = (await cur.fetchone())[0]
    ok("B-DB7-a ★부팅(init_db)은 큰 DELETE 를 안 한다 — healthcheck 10초 안(배포 반증 #6)★", n_boot == 2, str(n_boot))
    r = await db.maintain_command_tables(batch=1, pause=0)     # 한 행씩 나눠 지우는 길을 태운다
    ok("B-DB7-b 부팅 뒤 백그라운드 정리가 나눠서 지운다", r["commands"] == 1 and r["updater_commands"] >= 1 and r["index"], str(r))
    import inspect
    ok("B-DB7-c lifespan 이 정리를 백그라운드 작업으로 띄운다", "_cmd_table_maint()" in inspect.getsource(main.lifespan))
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT status FROM commands WHERE pc_id='PC-B8'") as cur:
            st = sorted(r[0] for r in await cur.fetchall())
        async with c.execute("SELECT COUNT(*) FROM updater_commands WHERE pc_id='PC-B8'") as cur:
            un = (await cur.fetchone())[0]
        async with c.execute("EXPLAIN QUERY PLAN SELECT id FROM commands WHERE COALESCE(updated_at, created_at) > ? "
                             "ORDER BY COALESCE(updated_at, created_at) ASC, id ASC LIMIT 5", ("2026-01-01",)) as cur:
            plan = " ".join(str(x) for r in await cur.fetchall() for x in r)
    ok("B-DB7 40일 된 끝난 명령은 정리가 지우고 pending 은 남긴다", st == ["pending"] and un == 0, "%s upd=%d" % (st, un))
    ok("B-DB7 명령 이벤트 쿼리가 식 인덱스를 탄다", "idx_cmd_at" in plan, plan[:100])


def test_all():
    run_all([t_fv1_raw_on_miss, t_fv2_same_second, t_fv3_db2_updater_ts, t_fv4_seq, t_fv5_trunc,
             t_fv6_mask, t_fv9_422, t_fv10_ack, t_db3_updq, t_db5_retired, t_db9_delete,
             t_new1_cap, t_db7_prune])
    finish("test_fvdb_b", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
