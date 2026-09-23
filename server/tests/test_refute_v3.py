# -*- coding: utf-8 -*-
"""v3 델타 반증 (2026-09-24, 아이온2 반증자) — 막는 것 1건(X1) + 낮음 3건(X2·X3·X4) + 계정 카드 셈(X7) + pc_clock 삭제.

  X1 ★함대 창 400 이 팜뷰에 «보냈습니다»★ — _fv_err 가 {error, code} 만 줬다. 배포된 팜뷰(5.54 ui/index.html dashCmd)는
     `j.ok===false` 일 때만 실패 토스트를 띄우고 문구는 `j.err` 에서 읽는다 → 모든 _fv_err 에 ok:false·err 를 더했다.
  X2 total_kina "9"*5000 → int() 4300자리 한도 ValueError → 500. 32자 넘는 문자열은 400.
  X3 폴링 길의 supersede_updater_command 가 handed_at 을 안 봤다 → 받아 간 행이 superseded(«안 돎»). → handed_noack.
     (만료 길도 같다 — 받아 간 행은 expired(«안 가져감») 가 아니라 handed_noack.) 늦은 ack 는 그대로 acked 로 적는다.
  X4 팜뷰가 받아 간 행을 거부하면 _UPDCMD_OWNER[id]="fv" 가 남아 업데이터 큐 머리를 최대 10분 막았다 → 소유를 푼다.
  X7 계정 카드(PC-20b·c)가 창에서 따로 셌다 → ★물리 PC 로 센다★(A7 의 «2대 이상» 은 기계 수 — 카드 셋 = PC 한 대).
     재전송이 창 시계를 새로 돌리던 것도 → 처음 보낸 시각(setdefault).
  pc_clock 은 카드 삭제(delete_pc_all_data)·remove_pc 에서 같이 지운다.

★«v3:» 를 적은 줄은 v3 판에서 실패한다★ (HANDOFF — v3 복사본에 돌려 확인).

    cd updater/server && python -X utf8 tests/test_refute_v3.py
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import aiosqlite
from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 34
TOK = "fvsecret-rf3"
H = {"X-FV-Token": TOK}
C1 = [{"slot": 1, "name": "러닝"}]
_FV_UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "farmview", "ui", "index.html")


def U(ds):
    return (datetime.now(timezone.utc) + timedelta(seconds=ds)).strftime("%Y-%m-%dT%H:%M:%S")


def _body(r):
    return json.loads(bytes(r.body))


def _ui_fails(j):
    """배포된 팜뷰 dashCmd 의 판정 그대로 — `if(j&&j.ok===false) 실패 — j.err`."""
    return bool(j) and j.get("ok") is False


async def _st(cid):
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT status FROM updater_commands WHERE id=?", (cid,)) as cur:
            r = await cur.fetchone()
    return r[0] if r else None


def _reset_q():
    main.FV_TOKEN = TOK
    main.FV_TENANT = "main"
    main.FV_UPDCMD_QUEUE.clear(); main._UPDCMD_OWNER.clear(); main._UPDCMD_FV_PC.clear()
    main._UPDCMD_UP_PC.clear(); main._FV_ACKED_RECENT.clear(); main._FV_REBUILT[0] = True
    main._FV_LAST_SEEN[0] = time.monotonic()


def _reset_fleet():
    main.FV_TOKEN = TOK
    main.FV_TENANT = "main"
    main._FV_FLEET_SEEN.clear(); main._FV_FLEET_OK.clear()


async def _press(pcs, cmd="start", **kw):
    """팜뷰가 한 장씩 누른다(1대 명령엔 fvdash 가 confirm_fleet 를 안 붙인다) → [상태코드], 마지막 본문."""
    od = main._dispatch_macro_command

    async def disp(t, p, c, a):
        return {"ok": True}
    main._dispatch_macro_command = disp
    codes, last = [], None
    try:
        for p in pcs:
            r = await main.fv_command_send(Req(dict({"cmd": cmd, "pc": p}, **kw), api_key=None, headers=H))
            codes.append(r.status_code)
            last = _body(r)
    finally:
        main._dispatch_macro_command = od
    return codes, last


# ── X1 ────────────────────────────────────────────────────────────────────────
async def t_err_shape():
    _reset_fleet()
    pcs = ["PC-V1%02d" % i for i in range(8)]
    for p in pcs:
        await db.upsert_status(p, {"pc_id": p, "status": "idle"})
    codes, last = await _press(pcs)
    ok("X1a 15분 안에 8번째 물리 PC 에 한 대씩 누른 start 는 400", codes == [200] * 7 + [400], str(codes))
    ok("X1b ★v3: 팜뷰 dashCmd 가 실패로 그린다(ok:false) — «보냈습니다» 가 아니다★", _ui_fails(last), str(last))
    ok("X1c ★v3: 실패 문구가 팜뷰 토스트까지(err 비지 않음, error 와 같은 글)★",
       bool(last.get("err")) and last.get("err") == last.get("error") and "confirm_fleet" in last["err"], str(last))
    ok("X1d 옛 칸(error·code)은 그대로 — 더하기만", last.get("code") == 400 and bool(last.get("error")), str(last))
    # 다른 _fv_err 길도 같은 모양 — 토큰 틀림(401)·카드 없음(404)
    main._FV_FAILS.clear() if hasattr(main, "_FV_FAILS") else None
    r401 = await main.fv_snapshot(Req(api_key=None, headers={"X-FV-Token": "wrong-rf3"}))
    j401 = _body(r401)
    ok("X1e ★v3: 토큰 틀림 401 도 ok:false·err★", r401.status_code == 401 and _ui_fails(j401) and j401.get("err"), str(j401))
    r404 = await main.fv_kina_adjust(Req({"pc_id": "PC-V1NONE", "delta_kina": -1, "why": {"tid": "v1e"}},
                                         api_key=None, headers=H))
    j404 = _body(r404)
    ok("X1f ★v3: 창고 키나 기록 없음 404 도 ok:false·err★", r404.status_code == 404 and _ui_fails(j404) and j404.get("err"),
       str(j404))
    # 계약의 닻 — 팜뷰 UI 가 정말 그 두 칸으로 판정하는지(팜뷰가 바뀌면 여기서 먼저 안다)
    try:
        src = open(_FV_UI, encoding="utf-8").read()
        anchored = "j.ok===false" in src and "j.err" in src
        why = "farmview/ui/index.html"
    except OSError:
        anchored, why = True, "팜뷰 소스 없음(건너뜀)"
    ok("X1g 팜뷰 UI 판정 = `j.ok===false` + `j.err` (이 시험이 흉내 내는 규칙이 실제 소스에 있다)", anchored, why)


# ── X2 ────────────────────────────────────────────────────────────────────────
async def _kina_code(pc, v):
    try:
        r = await main.receive_char_info(pc, Req({"characters": C1, "total_kina": v}))
        return r.status_code
    except main.HTTPException as e:
        return e.status_code
    except Exception as e:                   # 500 이 되는 길
        return "500:" + e.__class__.__name__


async def t_huge_kina():
    ok("X2a ★v3: total_kina «9»×5000 은 400(4300자리 int 한도 → 500 이 아니다)★",
       await _kina_code("PC-V2A", "9" * 5000) == 400, str(await _kina_code("PC-V2A", "9" * 5000)))
    ok("X2b 33자리 문자열도 400", await _kina_code("PC-V2B", "1" * 33) == 400, "")
    ok("X2c 쉼표 끼운 긴 문자열(40자)도 400", await _kina_code("PC-V2C", "1" + ",000" * 13) == 400, "")
    ok("X2d 보통 값(«1,234,567»)은 그대로 200", await _kina_code("PC-V2D", "1,234,567") == 200, "")
    info = await db.get_char_info(main.ns("main", "PC-V2D")) or {}
    ok("X2e 보통 값이 정수로 저장", info.get("total_kina") == 1234567, str(info.get("total_kina")))


# ── X3 ────────────────────────────────────────────────────────────────────────
async def t_handed_poll_supersede():
    _reset_q()
    pc = "PC-V3A"
    sess = main.new_session("main")
    a = _body(await main.dashboard_send_updater_command(pc, Req({"command": "restart"}, api_key=None, session=sess)))["id"]
    r1 = _body(await main.updater_poll_command(pc, Req()))           # 업데이터가 받아 감, ack 사라짐
    b = _body(await main.dashboard_send_updater_command(pc, Req({"command": "restart"}, api_key=None, session=sess)))["id"]
    async with aiosqlite.connect(db.DB_PATH) as c:                    # 90초 지남 — 팜뷰가 #b 를 받는 길
        await c.execute("UPDATE updater_commands SET handed_at=? WHERE id=?", (U(-(db.UPDATER_BUSY_SEC + 10)), a))
        await c.commit()
    main._UPDCMD_UP_PC.clear()
    await main.fv_updcmd_list(Req(api_key=None, headers=H), since="0")
    await main.updater_poll_command(pc, Req())                        # 폴링 길 supersede_updater_command
    st_a = await _st(a)
    ok("X3a ★v3: 받아 간 #a 는 폴링 길에서 superseded(«안 돎») 가 아니다 → handed_noack★",
       r1.get("id") == a and st_a == "handed_noack", "r1=%s a=%s b=%s" % (r1.get("id"), st_a, await _st(b)))
    await main.updater_ack_command(pc, a, Req())
    ok("X3b 늦은 ack 는 handed_noack 을 acked 로 적는다", await _st(a) == "acked", str(await _st(a)))
    # 받아 가지 않은 행은 여전히 superseded — 경계를 넘지 않았다
    _reset_q()
    pc = "PC-V3B"
    a = _body(await main.dashboard_send_updater_command(pc, Req({"command": "restart"}, api_key=None, session=sess)))["id"]
    b = _body(await main.dashboard_send_updater_command(pc, Req({"command": "restart"}, api_key=None, session=sess)))["id"]
    await main.updater_poll_command(pc, Req())
    ok("X3c 받아 가지 않은 옛 행은 그대로 superseded", await _st(a) == "superseded", str(await _st(a)))
    await main.updater_ack_command(pc, a, Req())
    ok("X3d 받아 가지 않은 superseded 행의 ack 는 안 먹는다(안 돈 명령을 «돎» 으로 안 적는다)",
       await _st(a) == "superseded", str(await _st(a)))
    # 만료 길 — 받아 간 뒤 ack 없이 10분 넘은 행은 expired(«안 가져감») 가 아니라 handed_noack
    _reset_q()
    pc = "PC-V3C"
    a = await db.insert_updater_command(main.ns("main", pc), "update", {})
    await main.updater_poll_command(pc, Req())
    c0 = await db.insert_updater_command(main.ns("main", pc), "restart", {})
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE updater_commands SET created_at=? WHERE id IN (?,?)", (U(-3600), a, c0))
        await c.commit()
    main._UPDCMD_UP_PC.clear()
    await main.updater_poll_command(pc, Req())
    ok("X3e ★v3: 받아 간 뒤 만료된 행은 handed_noack★", await _st(a) == "handed_noack", str(await _st(a)))
    ok("X3f 받아 가지 않고 만료된 행은 그대로 expired", await _st(c0) == "expired", str(await _st(c0)))
    # 팜뷰가 옛 #1 을 못 닿아 되돌리려는데, 더 새 같은 종류 #2 는 업데이터가 받아 갔다(handed_noack) → #1 은 안 되살린다
    pc = "PC-V3D"
    k = main.ns("main", pc)
    o1 = await db.insert_updater_command(k, "restart", {})
    o2 = await db.insert_updater_command(k, "restart", {})
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE updater_commands SET status='fv_unknown' WHERE id=?", (o1,))
        await c.execute("UPDATE updater_commands SET status='handed_noack', handed_at=? WHERE id=?", (U(-60), o2))
        await c.commit()
    back = await db.release_updater_command_from_fv(o1)
    ok("X3g 더 새 같은 종류가 handed_noack(돌았을 수 있음)이면 옛 #1 은 되살리지 않는다(옛 restart 가 새 것 뒤에 안 돈다)",
       back is False and await _st(o1) == "superseded", "%s %s" % (back, await _st(o1)))


# ── X4 ────────────────────────────────────────────────────────────────────────
async def t_owner_released():
    _reset_q()
    pc = "PC-V4A"
    sess = main.new_session("main")
    a = _body(await main.dashboard_send_updater_command(pc, Req({"command": "restart"}, api_key=None, session=sess)))["id"]
    snap = {k: dict(v) for k, v in main.FV_UPDCMD_QUEUE.items()}
    r1 = _body(await main.updater_poll_command(pc, Req()))
    main.FV_UPDCMD_QUEUE.clear(); main.FV_UPDCMD_QUEUE.update(snap)      # 겹친 인스턴스의 메모리
    main._UPDCMD_OWNER.clear(); main._UPDCMD_UP_PC.clear()
    fv = _body(await main.fv_updcmd_list(Req(api_key=None, headers=H), since="0"))["cmds"]
    ok("X4a 팜뷰는 받아 간 행을 안 받는다", r1.get("id") == a and fv == [], "%s %s" % (r1, fv))
    ok("X4b ★v3: 거부한 뒤 소유 표에 «fv» 가 남지 않는다★", main._UPDCMD_OWNER.get(a) != "fv",
       str(main._UPDCMD_OWNER.get(a)))
    main.FV_UPDCMD_QUEUE.clear()
    r2 = _body(await main.updater_poll_command(pc, Req()))
    ok("X4c ★v3: 업데이터는 곧바로 제 큐를 다시 받는다(10분 막힘 없음)★", r2.get("id") == a, str(r2))


# ── X7 · 창 시계 ───────────────────────────────────────────────────────────────
async def t_fleet_physical():
    _reset_fleet()
    cards = ["PC-V71", "PC-V71b", "PC-V71c", "PC-V72", "PC-V72b", "PC-V72c", "PC-V73", "PC-V73b"]
    for p in cards:
        await db.upsert_status(p, {"pc_id": p, "status": "idle"})
    codes, _ = await _press(cards)
    ok("X7a ★v3: 계정 카드 8장 = 물리 PC 3대 → 막지 않는다(물리 PC 로 센다)★", codes == [200] * 8, str(codes))
    ok("X7b 창에는 물리 PC 3대만", set(main._FV_FLEET_SEEN) >= {"PC-V71", "PC-V72", "PC-V73"}
       and not any(k.endswith(("b", "c")) for k in main._FV_FLEET_SEEN), str(sorted(main._FV_FLEET_SEEN)))
    # 물리 PC 가 8대가 되면 카드로 보내도 막는다
    more = ["PC-V74b", "PC-V75", "PC-V76c", "PC-V77", "PC-V78c"]
    for p in more:
        await db.upsert_status(p, {"pc_id": p, "status": "idle"})
    codes2, last = await _press(more)
    ok("X7c 물리 PC 7대까지 통과, 8번째 물리 PC(카드 PC-V78c)는 400", codes2 == [200, 200, 200, 200, 400], str(codes2))
    # 이미 창에 있는 물리 PC 의 다른 카드는 새 PC 가 아니다
    await db.upsert_status("PC-V71d", {"pc_id": "PC-V71d", "status": "idle"})
    codes3, _ = await _press(["PC-V71d"])
    ok("X7d 창에 있는 물리 PC 의 다른 카드(PC-V71d)는 통과", codes3 == [200], str(codes3))


async def t_window_first_send():
    _reset_fleet()
    await db.upsert_status("PC-V8A", {"pc_id": "PC-V8A", "status": "idle"})
    await _press(["PC-V8A"])
    old = time.monotonic() - main.FV_FLEET_WINDOW_S + 5
    main._FV_FLEET_SEEN["PC-V8A"] = old
    await _press(["PC-V8A"])
    ok("X8a ★v3: 같은 PC 재전송이 창 시계를 새로 돌리지 않는다(처음 보낸 시각)★",
       main._FV_FLEET_SEEN.get("PC-V8A") == old, "%s vs %s" % (main._FV_FLEET_SEEN.get("PC-V8A"), old))


# ── pc_clock 삭제 ──────────────────────────────────────────────────────────────
async def t_pc_clock_deleted():
    pc = "PC-V9A"
    k = main.ns("main", pc)
    await db.pc_clock_put(k, [[1.0, 2.0]])
    main._PC_CLOCK[k] = [[1.0, 2.0]]
    await main.remove_pc(pc, Req(api_key=None, session=main.new_session("main")))
    ok("X9a ★v3: 카드 삭제(remove_pc)가 pc_clock 표를 지운다★", await db.pc_clock_get(k) == [], str(await db.pc_clock_get(k)))
    ok("X9b 캐시(_PC_CLOCK)도 지운다", k not in main._PC_CLOCK, "")


# ── v4 반증 (독립 에이전트, 2026-09-24) ─────────────────────────────────────────────
async def t_v4_breaker():
    # B2 창 면제 명령(보기 전용·정지)에 붙은 confirm_fleet 는 «확인한 PC» 가 아니다
    _reset_fleet()
    pcs = ["PC-VB%02d" % i for i in range(8)]
    for p in pcs:
        await db.upsert_status(p, {"pc_id": p, "status": "idle"})
    od = main._dispatch_macro_command

    async def disp(t, p, c, a):
        return {"ok": True}
    main._dispatch_macro_command = disp
    try:
        rl = await main.fv_command_send(Req({"cmd": "get_logs", "pc": pcs, "confirm_fleet": True}, api_key=None, headers=H))
    finally:
        main._dispatch_macro_command = od
    codes, _ = await _press(pcs)
    ok("B2 ★get_logs 8대를 확인해 보낸 뒤에도 그 PC 들에 한 대씩 start 8번째는 400(면제 명령의 확인은 창을 안 연다)★",
       rl.status_code == 200 and codes == [200] * 7 + [400], "%s %s" % (rl.status_code, codes))
    # B3 요청 하나의 셈도 물리 PC — 물리 2대·카드 8장은 함대가 아니다
    _reset_fleet()
    cards = ["PC-VC1", "PC-VC1b", "PC-VC1c", "PC-VC1d", "PC-VC2", "PC-VC2b", "PC-VC2c", "PC-VC2d"]
    for p in cards:
        await db.upsert_status(p, {"pc_id": p, "status": "idle"})
    main._dispatch_macro_command = disp
    try:
        r8 = await main.fv_command_send(Req({"cmd": "start", "pc": cards}, api_key=None, headers=H))
        many = ["PC-VD%02d" % i for i in range(8)]
        r9 = await main.fv_command_send(Req({"cmd": "stop", "pc": many}, api_key=None, headers=H))
    finally:
        main._dispatch_macro_command = od
    ok("B3 요청 하나에 카드 8장·물리 2대는 확인 없이 통과(창과 같은 물리 PC 셈)", r8.status_code == 200, str(r8.status_code))
    ok("B3b 물리 8대 한 요청은 여전히 확인 없이 400(정지도)", r9.status_code == 400 and _ui_fails(_body(r9)), str(_body(r9)))
    # B4 DB 바쁨 판정이 handed_noack 도 본다 — 업데이터가 몇 초 전에 받아 간 행이 만료로 handed_noack 가 돼도 팜뷰는 안 집는다
    pc = "PC-VB4"
    a = await db.insert_updater_command(pc, "restart", {})
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE updater_commands SET created_at=? WHERE id=?", (U(-(db.UPDATER_COMMAND_MAX_AGE_SEC + 5)), a))
        await c.commit()
    await db.mark_updater_handed(a)
    await db.get_pending_updater_command(pc, all_key="all")
    st_a = await _st(a)
    b = await db.insert_updater_command(pc, "restart", {})
    busy = []
    claimed = await db.claim_updater_command_for_fv(b, fv_id=990041, busy_ids=[], busy_out=busy)
    ok("B4 ★몇 초 전에 받아 간 restart 가 handed_noack 이어도 팜뷰는 새 restart 를 집지 않는다(busy)★",
       st_a == "handed_noack" and not claimed and bool(busy), "a=%s claimed=%s busy=%s" % (st_a, claimed, busy))
    # B5 대리 문자 400 도 팜뷰 에러 모양
    r = await main._unicode_400(None, UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed"))
    j = _body(r)
    ok("B5 대리 문자 400 도 ok:false·err (+ 대시보드 detail 그대로)",
       r.status_code == 400 and _ui_fails(j) and j.get("err") and j.get("detail"), str(j))


def test_all():
    run_all([t_err_shape, t_huge_kina, t_handed_poll_supersede, t_owner_released, t_fleet_physical,
             t_window_first_send, t_pc_clock_deleted, t_v4_breaker])
    finish("test_refute_v3", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
