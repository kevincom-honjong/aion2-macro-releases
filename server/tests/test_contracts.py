# -*- coding: utf-8 -*-
"""[대시보드] 인터페이스 계약 시험 — 다른 영역과 주고받는 ★모양★ 이 바뀌면 여기가 빨간불.

계약 상대
  · 아이온2 매크로(lc/report_module.py·loot.py)  → 서버 : /report · /log · /command 폴링 · ack · WS 프레임
  · 서버 → 매크로                                : 명령 봉투 {type:"command", id, command, args(+_by)}
  · 서버 → 브라우저 대시보드(main.py 인라인 JS)   : cmd_history 항목 · state 카드 필드
  · 서버 → 팜뷰(farmview/fvdash.py)              : /api/fv/snapshot 의 pcs[].progress/today · global.totals

★상대가 형식을 바꾸면 내 쪽 시험이 먼저 죽어야 한다★ — 그래서 여기서는 「있어야 하는 키」를
값이 아니라 ★이름과 타입★ 으로 못 박는다. 키를 빼거나 타입을 바꾸는 변경은 CONTRACTS_대시보드.md 로.
"""
import json

from _harness import main, db, ok, FakeWS, Req, run_all, finish, TG_SENT   # noqa: E402

MIN_CHECKS = 26


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


def test_all():
    run_all([t_macro_to_server, t_server_to_dashboard, t_server_to_farmview, t_ws_frames])
    finish("test_contracts", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
