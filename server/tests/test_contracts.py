# -*- coding: utf-8 -*-
"""[대시보드] 인터페이스 계약 시험 — 다른 영역과 주고받는 ★모양★ 이 바뀌면 여기가 빨간불.

계약 상대
  · 아이온2 매크로(lc/report_module.py·loot.py)  → 서버 : /report · /log · /command 폴링 · ack · WS 프레임
  · 서버 → 매크로                                : 명령 봉투 {type:"command", id, command, args(+_by)}
  · 서버 → 브라우저 대시보드(main.py 인라인 JS)   : cmd_history 항목 · state 카드 필드
  · 서버 → 팜뷰(farmview/fvdash.py)              : /api/fv/snapshot 의 pcs[].progress/today · global.totals
  · 서버 → 팜뷰(farmview/ocr.py, 2026-09-24 #125) : /api/fv/ocr/queue·stats·seed_bugs 봉투 · 항목 키
  · 서버 → 매크로 텔레그램 중계(lc e32c01f TG7)   : reason 글자 muted·blocked·blocked_cap · 한국어 detail

★상대가 형식을 바꾸면 내 쪽 시험이 먼저 죽어야 한다★ — 그래서 여기서는 「있어야 하는 키」를
값이 아니라 ★이름과 타입★ 으로 못 박는다. 키를 빼거나 타입을 바꾸는 변경은 CONTRACTS_대시보드.md 로.
"""
import json

from _harness import main, db, ok, FakeWS, Req, run_all, finish, TG_SENT   # noqa: E402

MIN_CHECKS = 62


def _has(d, keys):
    return all(k in d for k in keys)


async def t_macro_to_server():
    # /report — 매크로가 30초마다 보내는 상태. 카드에 그대로 실려야 하는 키.
    payload = {"pc_id": "PC-C1", "status": "hunting", "macro_version": "1.1.950",
               "lan_url": "http://172.30.1.9:8765/?k=x", "daily_progress": [{"slot": 1, "completed": False}],
               "uptime_hours": 1.5, "efficiency": 80.0}
    r = await main.receive_report("PC-C1", Req(payload))
    ok("M→S /report 가 200 을 준다", getattr(r, "status_code", 200) == 200)
    # 업데이터 기록이 없는 base 카드는 규칙상 offline 으로 내려간다(main.py 「기존 규칙」) —
    # 계약 시험은 「매크로가 보낸 status 가 살아남는가」이므로 업데이터 기록을 같이 심는다.
    await db.upsert_updater_status("PC-C1", {"pc_id": "PC-C1", "macro_state": "running", "updater_version": "3.1.13"})
    rows = await main._build_full_state("main")
    card = next((c for c in rows if c["pc_id"] == "PC-C1"), None)
    ok("M→S 보고가 카드가 된다", card is not None)
    ok("S→대시보드 카드 필수 필드(status·pc_id·_ws_live·_macro_silent_s·daily_progress·_bug_count)",
       card is not None and _has(card, ("status", "pc_id", "_ws_live", "_macro_silent_s", "daily_progress", "_bug_count")),
       str(sorted(card)[:12]) if card else "")
    ok("S→대시보드 status 는 STATUS 어휘 안에 있다(매크로가 보낸 hunting 그대로)", card and card["status"] == "hunting")
    ok("S→대시보드 daily_progress 칸에 today 플래그가 붙는다(완료 규칙의 재료)",
       card and isinstance(card["daily_progress"], list) and all("today" in d for d in card["daily_progress"]),
       str(card and card["daily_progress"]))

    # /log — 배치 응답 계약 {ok, count(저장 수), received, dropped}
    r = await main.receive_logs("PC-C1", Req({"logs": [{"level": "info", "message": "a"}] * 3}))
    b = json.loads(bytes(r.body))
    ok("M→S /log 응답은 {ok,count,received,dropped}", _has(b, ("ok", "count", "received", "dropped")) and b["count"] == 3, str(b))

    # 명령 큐 — 넣기 → 폴링 → ack 의 모양
    cid = await main._dispatch_macro_command("main", "PC-C1", "start", {"x": 1})
    ok("S 명령 넣기 응답은 {ok,id,ws}", isinstance(cid, dict) and _has(cid, ("ok", "id", "ws")), str(cid))
    p = await main.poll_command("PC-C1", Req())
    pb = json.loads(bytes(p.body))
    ok("S→M 폴링 봉투는 {command,args,id}", _has(pb, ("command", "args", "id")) and pb["command"] == "start", str(pb))
    ok("S→M 사람이 넣은 명령엔 args._by='human' 이 실린다(재배달 포함)", pb["args"].get("_by") == "human" and pb["args"].get("x") == 1)
    a = await main.ack_cmd("PC-C1", pb["id"], Req())
    ok("M→S ack 응답은 {ok}", "ok" in json.loads(bytes(a.body)))
    p2 = json.loads(bytes((await main.poll_command("PC-C1", Req())).body))
    ok("S→M 큐가 비면 {command:null}", p2.get("command") is None, str(p2))

    # WS 명령 봉투 — 서버가 소켓으로 미는 모양
    ws = FakeWS()
    main.macro_ws_connections["PC-C2"] = ws
    await main.send_command_to_macro("PC-C2", "stop", {"_by": "human"}, 77)
    env = json.loads(ws.sent[0])
    ok("S→M WS 봉투는 {type:'command', id, command, args}",
       env.get("type") == "command" and env.get("id") == 77 and env.get("command") == "stop" and isinstance(env.get("args"), dict), str(env))
    main.macro_ws_connections.pop("PC-C2", None)


async def t_server_to_dashboard():
    hist = main._strip_cmds(await db.get_recent_commands(20, ns_prefix=""), "main")
    ok("S→대시보드 cmd_history 항목 키(id·pc_id·command·status·created_at·updated_at·args)",
       hist and _has(hist[0], ("id", "pc_id", "command", "status", "created_at", "updated_at", "args")), str(sorted(hist[0]) if hist else []))
    ok("S→대시보드 status 어휘 = pending|acked|expired|cancelled",
       all(c["status"] in ("pending", "acked", "expired", "cancelled") for c in hist))
    ok("S→대시보드 args 에 내부 표식(_by)이 안 보인다", all("_by" not in str(c["args"]) for c in hist))
    ok("S→대시보드 pc_id 는 테넌트 접두사 없이", all("::" not in c["pc_id"] for c in hist))


async def t_server_to_farmview():
    main.FV_TOKEN = "fvsecret"
    main._fv_snap["body"] = None
    r = await main.fv_snapshot(Req(api_key=None, headers={"X-FV-Token": "fvsecret"}))
    body = json.loads(bytes(r.body).decode("utf-8")) if r.headers.get("content-encoding") != "gzip" else json.loads(__import__("gzip").decompress(bytes(r.body)))
    ok("S→FV snapshot 최상위 {ts,pcs,global}", _has(body, ("ts", "pcs", "global")), str(sorted(body)))
    ok("S→FV pcs 는 ★pc_id 를 키로 한 dict★ (배열이 아니다 — 팜뷰 fvdash 가 그렇게 읽는다)",
       isinstance(body["pcs"], dict), type(body["pcs"]).__name__)
    pc = body["pcs"].get("PC-C1") if isinstance(body["pcs"], dict) else None
    ok("S→FV pcs[] 에 보고한 PC 가 있다", pc is not None)
    ok("S→FV pc 필수: pc_id·online·status·progress·today·raw",
       pc and _has(pc, ("pc_id", "online", "status", "progress", "today")), str(sorted(pc)) if pc else "")
    ok("S→FV progress 키(trade_kina·gakin_kina·odd_energy·awakening_ticket·subscribed·total_kina)",
       pc and _has(pc["progress"], ("trade_kina", "gakin_kina", "odd_energy", "awakening_ticket", "subscribed", "total_kina")),
       str(sorted(pc["progress"])) if pc else "")
    ok("S→FV progress 숫자 4종은 int", pc and all(isinstance(pc["progress"][k], int) for k in ("trade_kina", "gakin_kina", "odd_energy", "awakening_ticket")))
    ok("S→FV today 키(slots_done·slots_total·slots_left·daily_progress)",
       pc and _has(pc["today"], ("slots_done", "slots_total", "slots_left", "daily_progress")))
    ok("S→FV today.daily_progress[] 에 today 플래그", pc and all("today" in d for d in pc["today"]["daily_progress"]))
    g = body["global"]
    ok("S→FV global.totals 키(total_kina·bugs·trade_kina·gakin_kina·odd_energy·awakening_ticket)",
       "totals" in g and _has(g["totals"], ("total_kina", "bugs", "trade_kina", "gakin_kina", "odd_energy", "awakening_ticket")),
       str(sorted(g.get("totals", {}))))
    # 토큰 없으면 401 — 팜뷰가 토큰을 빼먹으면 조용히 빈 값이 아니라 거부
    r2 = await main.fv_snapshot(Req(api_key=None))
    ok("S→FV 토큰 없으면 401", r2.status_code == 401)
    # /api/fv/events 봉투
    r3 = await main.fv_events(Req(api_key=None, headers={"X-FV-Token": "fvsecret"}), since="2026-01-01T00:00:00Z", limit=10)
    e = json.loads(bytes(r3.body))
    ok("S→FV events 봉투 {since,next_since,count,truncated,events}", _has(e, ("since", "next_since", "count", "truncated", "events")))
    ok("S→FV events[] 항목에 at·type 이 있다", all(_has(x, ("at", "type")) for x in e["events"]))


async def t_fv_kina_adjust():
    """팜뷰 «팔린 만큼 줄인다»(CONTRACTS_팜뷰 2026-09-13): POST /api/fv/kina_adjust · progress.kina_age_s."""
    main.FV_TOKEN = "fvsecret"
    H = {"X-FV-Token": "fvsecret"}
    fresh = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    await db.upsert_char_info("PC-C1", 500_000_000, [{"slot": 1, "name": "러닝"}], collected_at=fresh)
    await db.upsert_updater_status("PC-C7", {"pc_id": "PC-C7", "macro_state": "running", "updater_version": "3.1.13"})
    await main.receive_report("PC-C7", Req({"pc_id": "PC-C7", "status": "idle"}))   # char_info 가 ★없는★ 카드
    # ① kina_age_s — 있으면 작은 정수, 없으면 10^9
    main._fv_snap["body"] = None
    r = await main.fv_snapshot(Req(api_key=None, headers=H))
    body = json.loads(bytes(r.body))
    age1 = body["pcs"]["PC-C1"]["progress"].get("kina_age_s")
    age7 = body["pcs"]["PC-C7"]["progress"].get("kina_age_s")
    ok("S→FV progress.kina_age_s 는 정수(수집 방금 → 60초 미만)", isinstance(age1, int) and not isinstance(age1, bool) and 0 <= age1 < 60, str(age1))
    ok("S→FV char_info 없는 카드는 kina_age_s = 10^9(모름)", age7 == 10 ** 9, str(age7))
    # ①-b kina_read_age_s (2026-09-24 팜뷰 #201 r3d) — 모든 카드에 정수, 판독 표식이 없으면 10^9
    ra = [body["pcs"][p]["progress"].get("kina_read_age_s") for p in ("PC-C1", "PC-C7")]
    ok("S→FV progress.kina_read_age_s 는 모든 카드에 정수(판독 표식 없음 → 10^9)",
       all(isinstance(x, int) and not isinstance(x, bool) for x in ra) and ra == [10 ** 9, 10 ** 9], str(ra))
    ok("S→FV _fv_age_int: 못 읽는 시각도 10^9", main._fv_age_int("garbage") == 10 ** 9 and main._fv_age_int(None) == 10 ** 9)
    # ② 차감 — 명세 원문 그대로
    why = {"tid": "2026090603221474", "server": "챈가룽", "man": 21000, "won": 84000, "src": "itemmania"}
    r = await main.fv_kina_adjust(Req({"pc_id": "PC-C1", "delta_kina": -210_000_000, "why": why}, api_key=None, headers=H))
    a = json.loads(bytes(r.body))
    ok("FV→S kina_adjust 응답 {ok,pc_id,before,after,dup}", r.status_code == 200 and _has(a, ("ok", "pc_id", "before", "after", "dup")), str(a))
    ok("FV→S after = before + delta (5억 - 2.1억 = 2.9억)", a.get("before") == 500_000_000 and a.get("after") == 290_000_000 and a.get("dup") is False, str(a))
    ci = await db.get_char_info("PC-C1")
    ok("FV→S char_info.total_kina 가 실제로 바뀌었다", ci and ci["total_kina"] == 290_000_000, str(ci and ci["total_kina"]))
    ok("FV→S collected_at 은 안 건드린다(수집 시각은 매크로 것)", ci and ci["collected_at"] == fresh, str(ci and ci["collected_at"]))
    main._fv_snap["ts"] = __import__("time").time()          # 캐시가 살아 있어도 …
    r = await main.fv_snapshot(Req(api_key=None, headers=H))
    ok("FV→S 차감 뒤 snapshot 캐시가 비워져 새 total_kina 가 바로 보인다",
       json.loads(bytes(r.body))["pcs"]["PC-C1"]["progress"]["total_kina"] == 290_000_000)
    logs = await db.get_logs("PC-C1", 5)
    ok("FV→S 그 PC 로그줄에 [팜뷰] 차감이 남는다(A2 증거)", any("[팜뷰]" in str(l.get("message")) and "2026090603221474" in str(l.get("message")) for l in logs), str([l.get("message") for l in logs][:2]))
    # ③ 같은 tid 두 번 → dup:true, 값 그대로
    r = await main.fv_kina_adjust(Req({"pc_id": "PC-C1", "delta_kina": -210_000_000, "why": why}, api_key=None, headers=H))
    d = json.loads(bytes(r.body))
    ci2 = await db.get_char_info("PC-C1")
    ok("FV→S 같은 why.tid 는 두 번 안 뺀다 → dup:true", r.status_code == 200 and d.get("dup") is True and d.get("ok") is True, str(d))
    ok("FV→S dup 일 때 total_kina 그대로(2.9억)", ci2["total_kina"] == 290_000_000 and d.get("after") == 290_000_000, str(ci2["total_kina"]))
    # ④ 바닥은 0
    r = await main.fv_kina_adjust(Req({"pc_id": "PC-C1", "delta_kina": -999_999_999, "why": {"tid": "t-floor"}}, api_key=None, headers=H))
    f = json.loads(bytes(r.body))
    ok("FV→S after = max(0, before+delta) — 음수로 안 내려간다", f.get("after") == 0 and f.get("before") == 290_000_000, str(f))
    # ⑤ 거부 모양 — 전부 {"error","code"}
    cases = [
        ("모르는 카드(char_info 없음) 404", {"pc_id": "PC-C7", "delta_kina": -1, "why": {"tid": "t1"}}, 404),
        ("없는 카드 404", {"pc_id": "PC-99", "delta_kina": -1, "why": {"tid": "t2"}}, 404),
        ("why.tid 없음 400", {"pc_id": "PC-C1", "delta_kina": -1, "why": {"server": "x"}}, 400),
        ("delta_kina 문자열 400", {"pc_id": "PC-C1", "delta_kina": "-1", "why": {"tid": "t3"}}, 400),
        ("delta_kina bool 400", {"pc_id": "PC-C1", "delta_kina": True, "why": {"tid": "t4"}}, 400),
        ("pc_id 'all' 400(A7)", {"pc_id": "all", "delta_kina": -1, "why": {"tid": "t5"}}, 400),
        ("상한 초과(±1조) 400", {"pc_id": "PC-C1", "delta_kina": -(10 ** 12 + 1), "why": {"tid": "t6"}}, 400),
    ]
    for label, b, code in cases:
        r = await main.fv_kina_adjust(Req(b, api_key=None, headers=H))
        e = json.loads(bytes(r.body))
        ok("FV→S kina_adjust " + label, r.status_code == code and _has(e, ("error", "code")) and e["code"] == code, "%s %s" % (r.status_code, e))
    r = await main.fv_kina_adjust(Req({"pc_id": "PC-C1", "delta_kina": -1, "why": {"tid": "t7"}}, api_key=None))
    ok("FV→S kina_adjust 토큰 없으면 401", r.status_code == 401)
    ok("FV→S 거부된 시도는 값을 안 바꾼다", (await db.get_char_info("PC-C1"))["total_kina"] == 0)


async def t_fv_ocr():
    """팜뷰 OCR 탭(farmview/ocr.py)이 읽는 모양 — FV_API «2026-09-23 (밤) 추가 › D» + 2026-09-24 seed_bugs."""
    import ocr_label as OL
    main.FV_TOKEN, main.FV_TENANT = "fvsecret", "main"
    H = {"X-FV-Token": "fvsecret"}

    def _j(r):
        return json.loads(bytes(r.body))
    r = await OL.fv_ocr_queue(Req(api_key=None, headers=H), limit="30")
    q = _j(r)
    ok("S→FV ocr/queue 봉투 {pending(int), items(list), suggest(dict), now}",
       _has(q, ("pending", "items", "suggest", "now")) and isinstance(q["pending"], int) and isinstance(q["items"], list)
       and isinstance(q["suggest"], dict), str(sorted(q)))
    await OL.submit_core("main", "PC-C9", "kina", b"\x89PNG\r\n\x1a\n" + b"c" * 40, "0f0f0f0f0f0f0f0f", None, "p", "12", "13")
    q = _j(await OL.fv_ocr_queue(Req(api_key=None, headers=H), limit="30"))
    it = next((x for x in q["items"] if x.get("pc") == "PC-C9"), None)
    ok("S→FV ocr/queue 항목 키(id·img·site·count·gemini·local·pc)",
       it is not None and _has(it, ("id", "img", "site", "count", "gemini", "local", "pc")), str(it))
    st = _j(await OL.fv_ocr_stats(Req(api_key=None, headers=H)))
    ok("S→FV ocr/stats 봉투 {sites, disk_bytes, disk_cap, disk_hard_cap}",
       _has(st, ("sites", "disk_bytes", "disk_cap", "disk_hard_cap")), str(sorted(st)))
    sb = _j(await OL.fv_ocr_seed_bugs(Req(api_key=None, headers=H)))
    ok("S→FV ocr/seed_bugs 봉투 {ok, scanned, added, exists, skipped(dict), full, more}",
       _has(sb, ("ok", "scanned", "added", "exists", "skipped", "full", "more")) and isinstance(sb["skipped"], dict), str(sb))
    r0 = await OL.fv_ocr_queue(Req(api_key=None), limit="30")
    ok("S→FV ocr 토큰 없으면 401", r0.status_code == 401, str(r0.status_code))


async def t_ws_frames():
    """매크로 → 서버 WS 프레임 어휘: status / log / ack / pong 을 서버가 받는다."""
    import asyncio
    ws = FakeWS()
    task = asyncio.create_task(main.macro_websocket(ws, "PC-C3"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if main.macro_ws_connections.get("PC-C3") is ws:
            break
    frames = [json.dumps({"type": "status", "payload": {"pc_id": "PC-C3", "status": "idle"}}),
              json.dumps({"type": "log", "logs": [{"level": "info", "message": "hello"}]}),
              json.dumps({"type": "pong"})]
    orig = ws.receive_text

    async def _feed():
        if frames:
            return frames.pop(0)
        ws.receive_text = orig
        return await orig()
    ws.receive_text = _feed
    for _ in range(3):
        ws._gate.set()
        await asyncio.sleep(0.03)
    for _ in range(30):
        await asyncio.sleep(0.02)
        if (await db.get_status("PC-C3")) and await db.get_logs("PC-C3", limit=1):
            break
    st = await db.get_status("PC-C3")
    lg = await db.get_logs("PC-C3", limit=5)
    ws.die()
    try:
        await asyncio.wait_for(task, timeout=5)
    except Exception:
        pass
    ok("M→S WS status 프레임이 카드가 된다", bool(st) and (st.get("status") == "idle" or (st.get("data") or {}).get("status") == "idle"), str(st)[:80])
    ok("M→S WS log 프레임이 로그가 된다", any("hello" in (x.get("message") or "") for x in lg))


async def t_tg_reasons():
    """매크로(lc/report_module._tg_status, e32c01f) ← 서버 /telegram/send·photo 의 reason 글자 셋 — ★값을 못 박는다★.
    매크로는 대소문자까지 그대로 맞춰 본다(TG_MUTED/TG_BLOCKED/TG_BLOCKED_CAP). 한국어 detail 도 옛 판이 맞춰 보므로 그대로."""
    import time
    from fastapi import HTTPException

    def _j(r):
        return json.loads(bytes(r.body))
    saved = (main.tg_enabled, main.tenant_chat_id)
    main.tg_enabled, main.tenant_chat_id = (lambda: True), (lambda t: "1")
    main.KEY_TO_TENANT["ctr-blk"] = "ctrblk"
    main.KILLED_TENANTS.add("ctrblk")
    main._KILL_TG.pop("ctrblk", None)
    try:
        main._TG_MUTE[main.ns("main", "PC-C7")] = time.time() + 600
        m = _j(await main.telegram_send("PC-C7", Req({"text": "보통 알림"})))
        ok("M←S 음소거 = 200 reason \"muted\"(글자 그대로)", m.get("reason") == "muted" and m.get("ok") is False, str(m))
        r = await main.telegram_send("PC-01", Req({"text": "보통 알림"}, api_key="ctr-blk"))
        ok("M←S 차단 텍스트 = 403 {detail:\"차단 상태에서는 정지 안내만 전송됩니다\", reason:\"blocked\"}",
           r.status_code == 403 and _j(r) == {"detail": "차단 상태에서는 정지 안내만 전송됩니다", "reason": "blocked"}, str(_j(r)))
        rp = await main.telegram_photo("PC-01", Req({}, api_key="ctr-blk"), None)
        ok("M←S 차단 사진 = 403 같은 문구·reason \"blocked\"(Forbidden 아님)",
           rp.status_code == 403 and _j(rp) == {"detail": "차단 상태에서는 정지 안내만 전송됩니다", "reason": "blocked"}, str(_j(rp)))
        main._KILL_TG["ctrblk"] = {"n": 3, "since": time.time()}
        rc = await main.telegram_send("PC-01", Req({"text": "⛔ 정지"}, api_key="ctr-blk"))
        ok("M←S 정지 안내 상한 = 429 {detail:\"정지 안내 전송 상한\", reason:\"blocked_cap\"}",
           rc.status_code == 429 and _j(rc) == {"detail": "정지 안내 전송 상한", "reason": "blocked_cap"}, str(_j(rc)))
        try:
            await main.telegram_send("PC-01", Req({"text": "x"}, api_key="ctr-none"))
            nr = "no-exception"
        except HTTPException as e:
            nr = (e.status_code, e.detail)
        ok("M←S 미등록 키 = reason 없는 403(키 문제 → 매크로 폴백)", nr == (403, "Forbidden"), str(nr))
    finally:
        main.tg_enabled, main.tenant_chat_id = saved
        main._TG_MUTE.pop(main.ns("main", "PC-C7"), None)
        main.KEY_TO_TENANT.pop("ctr-blk", None)
        main.KILLED_TENANTS.discard("ctrblk")
        main._KILL_TG.pop("ctrblk", None)


def test_all():
    run_all([t_macro_to_server, t_server_to_dashboard, t_server_to_farmview, t_fv_kina_adjust, t_ws_frames, t_fv_ocr, t_tg_reasons])
    finish("test_contracts", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
