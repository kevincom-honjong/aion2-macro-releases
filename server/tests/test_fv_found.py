# -*- coding: utf-8 -*-
"""팜뷰 5.55 전수·반증이 찾은 대시보드 쪽 셋 + 명령 시간 상한 (2026-09-23, SHARED_ISSUES_팜뷰.md).

  U  「업데이트」 한 번이 두 번 실행 — 업데이터 큐와 팜뷰 큐에 같이 들어가 양쪽이 다 실행할 수 있었다.
     → 먼저 집은 쪽만(_updcmd_take). 팜뷰가 집으면 업데이터 행 fv_claimed, 업데이터가 집으면 팜뷰 큐에서 뺀다.
  B  checkServerBoot 새로고침 연쇄 — 재배포 중 boot 가 A/B 로 흔들리면 새로고침이 이어졌다.
  Q  FV_UPDCMD_SEQ 재배포 0 초기화 — B-FV4(벽시계 ms) 로 이미 막혀 있다(여기서 다시 확인).
  T  /api/fv/command 시간 상한 — 팜뷰 12초 타임아웃 안에 results[]·partial·timeout 을 돌려준다.

    cd updater/server && python -X utf8 tests/test_fv_found.py
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import aiosqlite
from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_js_b import _run_node, _need_node   # noqa: E402

MIN_CHECKS = 84
TOK = "fvsecret-found"
H = {"X-FV-Token": TOK}


def _body(r):
    return json.loads(bytes(r.body))


class _FastState:
    """_build_full_state 를 미리 뽑은 값으로 바꾼다(시간 상한 시험 전용 — 부하 때 0.3초 상한을 카드 목록이 먹던 흔들림)."""
    async def __aenter__(self):
        self.orig = main._build_full_state
        rows = await self.orig("main")

        async def fast(t):
            return rows
        main._build_full_state = fast
        return self

    async def __aexit__(self, *a):
        main._build_full_state = self.orig


async def _send_upd(pc, cmd="update"):
    r = await main.dashboard_send_updater_command(pc, Req({"command": cmd}, api_key=None, session=main.new_session("main")))
    return _body(r)["id"]


async def _upd_poll(pc):
    return _body(await main.updater_poll_command(pc, Req()))


async def _fv_list(since=0):
    return _body(await main.fv_updcmd_list(Req(api_key=None, headers=H), since=str(since)))["cmds"]


async def _age_row(cid, sec):
    t = (datetime.now(timezone.utc) - timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(db.DB_PATH) as c:
        # 시간이 흐른 것 — 업데이터에 내준 시각(handed_at, v2 반증 1부 B)도 같이 옛것으로
        await c.execute("UPDATE updater_commands SET updated_at=?, "
                        "handed_at=CASE WHEN handed_at IS NULL THEN NULL ELSE ? END WHERE id=?", (t, t, cid))
        await c.commit()


async def _row_status(cid):
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT status FROM updater_commands WHERE id=?", (cid,)) as cur:
            r = await cur.fetchone()
    return r[0] if r else None


async def t_u_fv_first():
    main.FV_TOKEN = TOK
    main.FV_TENANT = "main"
    main.FV_UPDCMD_QUEUE.clear()
    pc = "PC-U01"
    cid = await _send_upd(pc)
    got = await _fv_list()
    ok("U-1 팜뷰가 목록으로 받는다", [c["pc"] for c in got] == [pc], str(got))
    ok("U-2 계약 모양 그대로 {id,pc,act,at} — 안쪽 칸(ucmd·claimed)은 안 나간다",
       got and set(got[0]) == {"id", "pc", "act", "at"}, str(got))
    ok("U-3 ★팜뷰가 집은 뒤 업데이터 폴링엔 안 나간다(두 번 실행 없음)★", (await _upd_poll(pc)).get("command") is None)
    ok("U-4 업데이터 행은 fv_claimed 로 남는다(재배포 뒤에도 업데이터에 안 나간다)", await _row_status(cid) == "fv_claimed",
       str(await _row_status(cid)))
    got2 = await _fv_list()
    ok("U-5 ack 전까지는 팜뷰 폴링에 계속 나온다(재전송 안전, 계약 §4-3)", [c["pc"] for c in got2] == [pc], str(got2))
    fid = got[0]["id"]
    b = _body(await main.fv_updcmd_ack(Req({"pc": pc, "id": fid, "ok": True}, api_key=None, headers=H)))
    ok("U-6 ack 가 팜뷰 큐를 지우고 업데이터 행에 fv_done 을 남긴다",
       b.get("removed") is True and await _row_status(cid) == "fv_done", "%s %s" % (b, await _row_status(cid)))
    ok("U-7 ack 뒤에도 업데이터 폴링엔 안 나간다", (await _upd_poll(pc)).get("command") is None)
    # ★배포 반증 #1★ 실패 ack → 업데이터 큐로 되돌린다(정확히 한 번). reached:true 로 명시한 실패만 fv_failed.
    cid2 = await _send_upd(pc, "restart")
    g = await _fv_list()
    await main.fv_updcmd_ack(Req({"pc": pc, "id": g[0]["id"], "ok": False, "why": "없는 PC"}, api_key=None, headers=H))
    up1 = await _upd_poll(pc)
    await main.updater_ack_command(pc, up1.get("id") or 0, Req())
    up2 = await _upd_poll(pc)
    ok("U-8 ★팜뷰 실패 ack(설정에 없는 PC·nobulk·에이전트 없음) → 업데이터가 정확히 한 번 받는다★",
       up1.get("id") == cid2 and up2.get("command") is None and await _row_status(cid2) == "acked"
       and await _fv_list() == [], "up1=%s up2=%s row=%s" % (up1, up2, await _row_status(cid2)))
    main._UPDCMD_UP_PC.clear()      # 업데이터가 cid2 를 받은 지 90초가 지났다(그 안의 같은 종류는 업데이터 몫 — R-3)
    await _age_row(cid2, db.UPDATER_BUSY_SEC + 5)       # DB 쪽 «방금 받음» 도(H5)
    cid3 = await _send_upd(pc, "restart")
    g = await _fv_list()
    await main.fv_updcmd_ack(Req({"pc": pc, "id": g[0]["id"], "ok": False, "reached": True, "why": "agent err"},
                                 api_key=None, headers=H))
    ok("U-8b 팜뷰가 reached:true(에이전트에 닿음) 로 명시한 실패만 fv_failed — 업데이터로 안 되살린다(두 번 실행 방지)",
       await _row_status(cid3) == "fv_failed" and (await _upd_poll(pc)).get("command") is None, str(await _row_status(cid3)))


async def t_u_updater_first():
    main.FV_UPDCMD_QUEUE.clear()
    pc = "PC-U02"
    cid = await _send_upd(pc)
    p = await _upd_poll(pc)
    ok("U-9 업데이터가 먼저 폴링하면 받는다", p.get("id") == cid and p.get("command") == "update", str(p))
    ok("U-10 ★그 순간 팜뷰 큐에서 빠진다 — 팜뷰 목록에 안 나온다★", await _fv_list() == [])
    ok("U-11 업데이터 재폴링(ack 전)은 같은 명령을 다시 준다(기존 재전송 규칙 그대로)",
       (await _upd_poll(pc)).get("id") == cid)
    await main.updater_ack_command(pc, cid, Req())
    ok("U-12 업데이터 ack → acked", await _row_status(cid) == "acked")


async def t_u_race():
    # 두 폴링이 같은 틈에 오는 판 — 한쪽만 이긴다
    main.FV_UPDCMD_QUEUE.clear()
    wins = []
    for i in range(8):
        pc = "PC-U1%d" % i
        cid = await _send_upd(pc)
        up, fv = await asyncio.gather(_upd_poll(pc), _fv_list())
        u = up.get("id") == cid
        f = any(c["pc"] == pc for c in fv)
        wins.append((u, f))
        if f:
            await main.fv_updcmd_ack(Req({"pc": pc, "id": [c for c in fv if c["pc"] == pc][0]["id"], "ok": True},
                                         api_key=None, headers=H))
        main.FV_UPDCMD_QUEUE.clear()
    ok("U-13 ★동시 폴링 8판 — 매 판 정확히 한쪽만 받는다★", all(u != f for u, f in wins), str(wins))


async def t_u_expired_and_legacy():
    main.FV_UPDCMD_QUEUE.clear()
    pc = "PC-U03"
    cid = await _send_upd(pc)
    async with aiosqlite.connect(db.DB_PATH) as c:     # 유효기간(10분) 지난 행
        await c.execute("UPDATE updater_commands SET created_at='2020-01-01T00:00:00' WHERE id=?", (cid,))
        await c.commit()
    ok("U-14 업데이터 큐에서 만료된 명령은 팜뷰도 실행하지 않는다(큐에서 뺀다)",
       await _fv_list() == [] and main.ns("main", pc) not in main.FV_UPDCMD_QUEUE)
    main._fv_updcmd_push("main", "PC-U04", "restart")          # 짝 없는 옛 모양(ucmd 없음)
    ok("U-15 짝 없는 옛 항목은 예전처럼 나간다", [c["pc"] for c in await _fv_list()] == ["PC-U04"])
    main.FV_UPDCMD_QUEUE.clear()


async def t_q_seq():
    main.FV_UPDCMD_SEQ[0] = 0                  # 재배포 흉내
    main.FV_UPDCMD_QUEUE.clear()
    await _send_upd("PC-U05")
    got = await _fv_list(since=1_700_000_000_000)          # 팜뷰의 낡은 큰 커서(2023년 ms)
    ok("Q-1 재배포 뒤 새 명령 id 가 팜뷰 옛 커서보다 크다(B-FV4 벽시계 ms)", [c["pc"] for c in got] == ["PC-U05"], str(got))
    main.FV_UPDCMD_QUEUE.clear()


async def t_timeout():
    main.FV_TOKEN = TOK
    pcs = ["PC-T01", "PC-T02", "PC-T03"]
    for p in pcs:
        await db.upsert_status(p, {"pc_id": p, "status": "idle"})
    orig, orig_dl = main._dispatch_macro_command, main.FV_CMD_DEADLINE_S
    done = []

    async def slow(tenant, pc_id, command, args):
        await asyncio.sleep(0.6 if pc_id == "PC-T02" else 0.05)
        done.append(pc_id)
        return {"ok": True, "id": 1}
    main._dispatch_macro_command = slow
    main.FV_CMD_DEADLINE_S = 0.3
    try:
        async with _FastState():
            b = _body(await main.fv_command_send(Req({"cmd": "start", "pc": pcs}, api_key=None, headers=H)))
    finally:
        main.FV_CMD_DEADLINE_S = orig_dl
    r = {x["pc"]: x for x in b.get("results", [])}
    ok("T-1 시간초과면 ok:false · partial:true · timeout:true", b.get("ok") is False and b.get("partial") is True
       and b.get("timeout") is True, str({k: b.get(k) for k in ("ok", "partial", "timeout", "sent")}))
    ok("T-2 끝난 PC 는 results 에 ok:true", r.get("PC-T01", {}).get("ok") is True, str(r.get("PC-T01")))
    ok("T-3 시작했는데 안 끝난 PC = ok:null·unknown:true(닿았는지 모름)",
       r.get("PC-T02", {}).get("ok") is None and r.get("PC-T02", {}).get("unknown") is True, str(r.get("PC-T02")))
    ok("T-4 시작 못 한 PC = sent:false(다시 보내도 됨)", r.get("PC-T03", {}).get("sent") is False, str(r.get("PC-T03")))
    await asyncio.sleep(0.8)
    ok("T-5 안 끝난 PC 도 뒤에서 끝까지 돈다(중간에 끊지 않는다 — 반쪽 명령 없음)", "PC-T02" in done, str(done))
    try:
        b2 = _body(await main.fv_command_send(Req({"cmd": "start", "pc": ["PC-T01"]}, api_key=None, headers=H)))
    finally:
        main._dispatch_macro_command = orig
    ok("T-6 제시간이면 예전 모양(ok:true · partial:false · timeout:false)",
       b2.get("ok") is True and b2.get("partial") is False and b2.get("timeout") is False, str(b2))


# ── 반증 2차(2026-09-23 break_fv) — 두 번 누름·겹친 목록·claim 예외·시간 상한·PC 별 순서·못 닿음 되돌림 ──
def _reset():
    main.FV_TOKEN = TOK; main.FV_TENANT = "main"
    main.FV_UPDCMD_QUEUE.clear(); main._UPDCMD_OWNER.clear(); main._UPDCMD_FV_PC.clear()
    main._UPDCMD_UP_PC.clear(); main._FV_REBUILT[0] = True     # 복구는 그 시험(R-2)에서만 일부러 켠다


async def t_break2_updcmd():
    _reset(); pc = "PC-X01"
    c1 = await _send_upd(pc, "update"); c2 = await _send_upd(pc, "update")     # 다시 누름(팜뷰 항목은 c2)
    fv = await _fv_list(); up = await _upd_poll(pc)
    ok("U-16 ★두 번 누름: 팜뷰와 업데이터가 둘 다 실행하지 않는다(반증 B1)★", not (fv and up.get("command")),
       "fv=%d up=%s rows=%s,%s" % (len(fv), up.get("id"), await _row_status(c1), await _row_status(c2)))
    ok("U-17 옛 대기 명령은 superseded 로 남는다", await _row_status(c1) == "superseded", str(await _row_status(c1)))
    _reset(); pc = "PC-X02"
    await _send_upd(pc, "update"); c2 = await _send_upd(pc, "update_only")
    fv = await _fv_list(); up = await _upd_poll(pc)
    ok("U-18 update 뒤 update_only: 다른 종류라 둘 다 나간다(팜뷰=update, 업데이터=update_only — 배포 반증 #2)",
       [x["act"] for x in fv] == ["updater"] and up.get("id") == c2, "fv=%s up=%s" % (fv, up))
    # 업데이터 폴링의 get_pending 이 옛 행을 들고 있는 사이 팜뷰가 새 행을 집는 틈
    _reset(); pc = "PC-X08"
    c1 = await _send_upd(pc, "update")
    orig = main.get_pending_updater_command

    async def slow_pending(*a, **k):
        r = await orig(*a, **k)
        await asyncio.sleep(0.2)
        return r
    main.get_pending_updater_command = slow_pending
    try:
        async def press_and_list():
            await asyncio.sleep(0.05)
            box.append(await _send_upd(pc, "update"))
            return await _fv_list()
        box = []
        up, fv = await asyncio.gather(_upd_poll(pc), press_and_list())
    finally:
        main.get_pending_updater_command = orig
    ok("U-19 ★업데이터가 옛 행을 들고 있는 사이 팜뷰가 새 행을 집어도 한쪽만★", not (fv and up.get("command")),
       "fv=%d up=%s c1=%s:%s c2=%s:%s" % (len(fv), up, c1, await _row_status(c1), box, box and await _row_status(box[0])))
    _reset(); pc = "PC-X03"
    c = await _send_upd(pc)
    a, b = await asyncio.gather(_fv_list(), _fv_list())
    got = a or b
    ack = _body(await main.fv_updcmd_ack(Req({"pc": pc, "id": got[0]["id"], "ok": True}, api_key=None, headers=H)))
    ok("U-20 겹친 팜뷰 목록 요청도 ack 가 fv_done 을 남긴다(반증 B2)", await _row_status(c) == "fv_done",
       "lists=%d/%d ack=%s" % (len(a), len(b), ack))
    _reset(); pc = "PC-X05"
    c = await _send_upd(pc)
    origc = main.claim_updater_command_for_fv

    async def boom(u):
        raise RuntimeError("db locked")
    main.claim_updater_command_for_fv = boom
    try:
        g = await _fv_list()
    finally:
        main.claim_updater_command_for_fv = origc
    up = await _upd_poll(pc)
    ok("U-21 claim 예외면 팜뷰는 안 받고 업데이터가 그대로 받는다(큐가 안 막힌다, 반증 B4)",
       g == [] and up.get("id") == c, "fv=%s up=%s" % (g, up))
    _reset(); pc = "PC-X09"
    c = await _send_upd(pc)
    g = await _fv_list()
    await main.fv_updcmd_ack(Req({"pc": pc, "id": g[0]["id"], "ok": False, "reached": False, "why": "connect refused"},
                                 api_key=None, headers=H))
    up = await _upd_poll(pc)
    ok("U-22 팜뷰가 «PC 에 못 닿음(reached:false)» 이면 업데이터 큐로 되돌린다(반증 P1)",
       up.get("id") == c and await _row_status(c) == "pending", "up=%s row=%s" % (up, await _row_status(c)))


async def t_break2_timeout():
    _reset()
    await db.upsert_status("PC-X06", {"pc_id": "PC-X06", "status": "idle"})
    ob, od, odl = main._build_full_state, main._dispatch_macro_command, main.FV_CMD_DEADLINE_S

    async def slow_build(t):
        await asyncio.sleep(0.4); return await ob(t)

    async def slow_disp(t, p, c, a):
        await asyncio.sleep(5); return {"ok": True}
    main._build_full_state, main._dispatch_macro_command, main.FV_CMD_DEADLINE_S = slow_build, slow_disp, 0.3
    t0 = time.monotonic()
    try:
        b = _body(await main.fv_command_send(Req({"cmd": "start", "pc": ["PC-X06"]}, api_key=None, headers=H)))
    finally:
        main._build_full_state, main._dispatch_macro_command, main.FV_CMD_DEADLINE_S = ob, od, odl
    el = time.monotonic() - t0
    ok("T-7 카드 목록 만들기까지 시간 상한 안(반증 B5)", el <= 0.4 and b.get("timeout") is True and b.get("sent") == 0,
       "elapsed=%.2f %s" % (el, b))
    order = []
    await db.upsert_status("PC-X07", {"pc_id": "PC-X07", "status": "idle"})

    async def disp(t, p, c, a):
        await asyncio.sleep(0.5 if c == "stop" else 0.01); order.append(c); return {"ok": True}
    main._dispatch_macro_command, main.FV_CMD_DEADLINE_S = disp, 0.1
    try:
        async with _FastState():
            await main.fv_command_send(Req({"cmd": "stop", "pc": ["PC-X07"]}, api_key=None, headers=H))
            await main.fv_command_send(Req({"cmd": "start", "pc": ["PC-X07"]}, api_key=None, headers=H))
        await asyncio.sleep(0.7)
    finally:
        main._dispatch_macro_command, main.FV_CMD_DEADLINE_S = od, odl
    ok("T-8 시간초과로 뒤에서 도는 stop 을 뒤에 온 start 가 앞지르지 않는다(반증 B6)", order == ["stop", "start"], str(order))


async def t_blockers_bundle():
    """배포 반증(2026-09-23 밤) 두 막힘 — ① 팜뷰 실패·무응답이 업데이트를 삼킴 ② 다른 종류 명령을 덮어씀."""
    _reset(); pc = "PC-X11"
    c = await _send_upd(pc)
    g = await _fv_list()
    await main.fv_updcmd_ack(Req({"pc": pc, "id": g[0]["id"], "ok": True}, api_key=None, headers=H))
    ok("V-1 팜뷰 성공 → 업데이터는 절대 안 받는다", (await _upd_poll(pc)).get("command") is None
       and await _row_status(c) == "fv_done", str(await _row_status(c)))
    # 팜뷰가 집고 ack 를 안 준다 → ★업데이터에 주지 않는다★(팜뷰가 이미 실행했을 수 있다 — 배포 반증 3차 #1) →
    #   fv_unknown(«실행됐는지 모름», PC 로그 한 줄) · 팜뷰 목록엔 남아 돌아온 팜뷰가 ack 로 닫는다
    _reset(); pc = "PC-X12"
    c = await _send_upd(pc)
    g = await _fv_list()
    ok("V-2 집은 직후(90초 전)엔 업데이터에 안 나간다", (await _upd_poll(pc)).get("command") is None)
    old = (datetime.now(timezone.utc) - timedelta(seconds=db.FV_CLAIM_ACK_SEC + 5)).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(db.DB_PATH) as cx:
        await cx.execute("UPDATE updater_commands SET updated_at=? WHERE id=?", (old, c)); await cx.commit()
    main._FV_LAST_SEEN[0] = time.monotonic()           # 팜뷰가 방금 목록을 불렀다 = 살아서 차례로 돌리는 중
    ok("V-3a ★팜뷰가 살아 있으면(60초 안에 목록·ack) 90초 넘어도 회수하지 않는다(차례 처리 중 두 번 실행 방지, 2차 F4)★",
       (await _upd_poll(pc)).get("command") is None and await _row_status(c) == "fv_claimed", str(await _row_status(c)))
    main._FV_LAST_SEEN[0] = time.monotonic() - main.FV_QUIET_SEC - 5   # 팜뷰가 조용하다(죽음)
    up = await _upd_poll(pc)
    logs = [x["message"] for x in await db.get_logs(pc, 20)]
    ok("V-3 ★팜뷰가 조용하고 90초 ack 없음 → 업데이터에 안 준다, fv_unknown + PC 로그 «실행됐는지 모름»(3차 #1)★",
       up.get("command") is None and await _row_status(c) == "fv_unknown" and any("실행됐는지 모름" in m for m in logs),
       "%s %s %s" % (up, await _row_status(c), logs[-1:]))
    main._FV_LAST_SEEN[0] = time.monotonic()
    ok("V-4 ★«모름» 이 된 명령은 팜뷰 목록에서 빠진다 — 재시작한 팜뷰가 다시 실행하지 않게(반증 v2 #2)★",
       g[0]["id"] not in [x["id"] for x in await _fv_list()])
    b = _body(await main.fv_updcmd_ack(Req({"pc": pc, "id": g[0]["id"], "ok": True}, api_key=None, headers=H)))
    ok("V-5 늦은 ok ack 가 «모름» 을 fv_done 으로 닫는다 — 업데이터는 끝까지 안 받는다",
       await _row_status(c) == "fv_done" and (await _upd_poll(pc)).get("command") is None,
       "%s %s" % (b, await _row_status(c)))
    main._FV_LAST_SEEN[0] = time.monotonic()
    # 팜뷰가 집은 채 10분이 지나면 되살리지 않고 fv_unknown(내역에 «실행됐는지 모름» 으로 보인다)
    _reset(); pc = "PC-X13"
    c = await _send_upd(pc)
    await _fv_list()
    vold = (datetime.now(timezone.utc) - timedelta(seconds=db.UPDATER_COMMAND_MAX_AGE_SEC + 5)).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(db.DB_PATH) as cx:
        await cx.execute("UPDATE updater_commands SET created_at=?, updated_at=? WHERE id=?", (vold, vold, c)); await cx.commit()
    up = await _upd_poll(pc)
    ok("V-6 팜뷰가 집은 채 10분 지난 명령은 되살리지 않고 fv_unknown(팜뷰가 살아 있어도)",
       up.get("command") is None and await _row_status(c) == "fv_unknown",
       "%s %s" % (up, await _row_status(c)))
    # ② update 뒤 restart — 둘 다 나간다(업데이터 큐만)
    for c in ("update", "restart", "update"):
        pass
    await db.insert_updater_command("PC-X14", "update", {})
    await db.insert_updater_command("PC-X14", "restart", {})
    got = []
    for _ in range(4):
        x = await db.get_pending_updater_command("PC-X14", "all")
        if not x:
            break
        got.append(x["command"]); await db.ack_updater_command(x["id"])
    ok("V-7 ★update 뒤 restart → update 도 restart 도 순서대로 나간다(update 가 사라지지 않는다)★",
       got == ["update", "restart"], str(got))
    for c in ("restart", "restart", "update", "update"):
        await db.insert_updater_command("PC-X15", c, {})
    got = []
    for _ in range(5):
        x = await db.get_pending_updater_command("PC-X15", "all")
        if not x:
            break
        got.append(x["command"]); await db.ack_updater_command(x["id"])
    ok("V-8 같은 종류 중복만 덮는다(restart×2 → 1, update×2 → 1)", got == ["restart", "update"], str(got))
    # 대시보드 경로: update 누르고 restart — 팜뷰 큐엔 restart(최신 1건), update 는 업데이터가 받아야 한다
    _reset(); pc = "PC-X16"
    cu = await _send_upd(pc, "update"); cr = await _send_upd(pc, "restart")
    fv = await _fv_list(); up = await _upd_poll(pc)
    ok("V-9 ★대시보드 update→restart: 팜뷰=restart, 업데이터=update — 둘 다 실행★",
       [x["act"] for x in fv] == ["restart"] and up.get("id") == cu and await _row_status(cr) == "fv_claimed",
       "fv=%s up=%s" % (fv, up))
    # 팜뷰가 restart 를 집은 뒤에도 업데이터가 들고 있던 옛 restart 는 거절(같은 종류 표식 그대로)
    _reset(); pc = "PC-X17"
    r1 = await _send_upd(pc, "restart"); r2 = await _send_upd(pc, "restart")
    fv = await _fv_list(); up = await _upd_poll(pc)
    ok("V-10 같은 종류 두 번 누름은 여전히 한쪽만", len(fv) == 1 and up.get("command") is None
       and await _row_status(r1) == "superseded", "fv=%s up=%s" % (fv, up))


async def t_fv_command_err():
    _reset()
    await db.upsert_status("PC-X18", {"pc_id": "PC-X18", "status": "idle"})
    od, odl = main._dispatch_macro_command, main.FV_CMD_DEADLINE_S

    async def slow_disp(t, p, c, a):
        await asyncio.sleep(2); return {"ok": True}
    main._dispatch_macro_command, main.FV_CMD_DEADLINE_S = slow_disp, 0.2
    try:
        async with _FastState():
            b = _body(await main.fv_command_send(Req({"cmd": "start", "pc": ["PC-X18"]}, api_key=None, headers=H)))
    finally:
        main._dispatch_macro_command, main.FV_CMD_DEADLINE_S = od, odl
    ok("T-9 시간초과 응답에 err(옛 팜뷰 토스트가 j.err 만 쓴다) — «닿았는지 모름, 다시 누르지 마십시오»",
       b.get("ok") is False and "다시 누르지 마십시오" in str(b.get("err")), str(b))

    async def fast(t, p, c, a):
        return {"ok": True}
    main._dispatch_macro_command = fast
    try:
        b = _body(await main.fv_command_send(Req({"cmd": "start", "pc": ["PC-X18"]}, api_key=None, headers=H)))
    finally:
        main._dispatch_macro_command = od
    ok("T-10 성공 응답엔 err 가 없다(모양 그대로)", b.get("ok") is True and "err" not in b, str(b))


async def t_refute2():
    """배포 반증 2차(2026-09-23 밤) — ack 를 팜뷰 id 로 찾기 · 옛 것 안 되살림 · 큐 머리 막힘."""
    async def ack(pc, fid, ok_, **kw):
        return _body(await main.fv_updcmd_ack(Req({"pc": pc, "id": fid, "ok": ok_, **kw}, api_key=None, headers=H)))
    # F1: 팜뷰가 update 를 집은 뒤 restart 를 누름(칸 덮임) → update 의 ok:true ack 가 제 행에 닿는다
    _reset(); pc = "PC-Y01"
    cu = await _send_upd(pc, "update")
    g = await _fv_list()
    cr = await _send_upd(pc, "restart")
    await ack(pc, g[0]["id"], True)
    ok("W-1 ★칸이 새 누름에 덮여도 ack 가 제 행에 닿는다 → fv_done(업데이터가 다시 안 돌림)★",
       await _row_status(cu) == "fv_done", str(await _row_status(cu)))
    # F2: 집은 뒤 재배포(메모리 전부 비움) → ack 가 DB 표로 제 행을 찾는다
    _reset(); pc = "PC-Y02"
    c = await _send_upd(pc)
    g = await _fv_list()
    main.FV_UPDCMD_QUEUE.clear(); main._UPDCMD_OWNER.clear(); main._UPDCMD_FV_PC.clear()   # 재배포
    main._UPDCMD_UP_PC.clear()
    await ack(pc, g[0]["id"], True)
    ok("W-2 ★재배포 뒤 온 ack 도 제 행을 fv_done 으로 닫는다★", await _row_status(c) == "fv_done", str(await _row_status(c)))
    main._FV_LAST_SEEN[0] = time.monotonic() - main.FV_QUIET_SEC - 5
    ok("W-3 그 뒤 업데이터는 안 받는다", (await _upd_poll(pc)).get("command") is None)
    main._FV_LAST_SEEN[0] = time.monotonic()
    # F3: 팜뷰가 restart c1 을 집음(도는 중) → c2 누름 → ★업데이터는 c2 를 안 받는다(H8 — 두 길이 겹치지 않게)★ →
    #   팜뷰가 c1 뒤에 c2 를 집는다 → c1 실패 ack(안-감) → 더 새 c2 가 살아 있으니 c1 은 되살아나지 않는다
    _reset(); pc = "PC-Y03"
    c1 = await _send_upd(pc, "restart")
    g = await _fv_list()
    c2 = await _send_upd(pc, "restart")
    up = await _upd_poll(pc)
    g2 = await _fv_list()
    await ack(pc, g[0]["id"], False, why="에이전트 없음")
    up2 = await _upd_poll(pc)
    ok("W-4 ★팜뷰가 restart 를 도는 중에 다시 누른 restart 는 업데이터가 아니라 팜뷰가 차례로 — 옛 것의 실패 ack 는 "
       "되살리지 않는다(superseded)★",
       up.get("command") is None and sorted(x["id"] for x in g2) == sorted([g[0]["id"], g2[-1]["id"]]) and len(g2) == 2
       and await _row_status(c2) == "fv_claimed" and up2.get("command") is None and await _row_status(c1) == "superseded",
       "up=%s g2=%s up2=%s c1=%s c2=%s" % (up, g2, up2, await _row_status(c1), await _row_status(c2)))
    # F3 회수 갈래: 팜뷰가 c1 을 집고 죽음 → c2 누름(팜뷰 목록도 죽음) → c1 이 «도는 중» 인 동안 업데이터는 c2 를
    #   기다린다(H8) → 팜뷰 조용 90초 → c1 은 fv_unknown(업데이터에 안 감), 같은 폴링에서 업데이터가 c2 를 받는다
    _reset(); pc = "PC-Y04"
    c1 = await _send_upd(pc, "restart")
    await _fv_list()
    main.FV_UPDCMD_QUEUE.clear()
    c2 = await _send_upd(pc, "restart")
    main.FV_UPDCMD_QUEUE.clear()            # 팜뷰 죽음 — c2 는 업데이터로만
    up0 = await _upd_poll(pc)
    await _age_row(c1, db.FV_CLAIM_ACK_SEC + 5)
    main._FV_LAST_SEEN[0] = time.monotonic() - main.FV_QUIET_SEC - 5
    up = await _upd_poll(pc)
    if up.get("id"):
        await main.updater_ack_command(pc, up["id"], Req())
    up2 = await _upd_poll(pc)
    main._FV_LAST_SEEN[0] = time.monotonic()
    ok("W-5 ★팜뷰가 조용해도 옛 restart 는 업데이터에 안 간다 — fv_unknown · 새 것은 그 뒤 업데이터가 한 번★",
       up0.get("command") is None and up.get("id") == c2 and up2.get("command") is None
       and await _row_status(c1) == "fv_unknown",
       "up0=%s up=%s up2=%s c1=%s" % (up0, up, up2, await _row_status(c1)))
    # F1b: 팜뷰가 더 새 같은 종류를 집어 둔 채 옛 행이 대기로 남았다 → 업데이터는 그 옛 행을 치우고 다음 행을 받는다
    _reset(); pc = "PC-Y05"
    o = await _send_upd(pc, "update")
    main.FV_UPDCMD_QUEUE.clear()
    n = await _send_upd(pc, "update")
    await _fv_list()                                    # 팜뷰가 n 을 집음(o 는 claim 이 superseded 로 치움)
    async with aiosqlite.connect(db.DB_PATH) as cx:     # 틈을 일부러 만든다: o 가 다시 대기
        await cx.execute("UPDATE updater_commands SET status='pending' WHERE id=?", (o,)); await cx.commit()
    r = await _send_upd(pc, "screenshot")
    up = await _upd_poll(pc)
    ok("W-6 ★받을 수 없는 옛 대기 행이 큐 머리를 막지 않는다 — superseded 로 치우고 다음(screenshot)을 준다★",
       up.get("id") == r and await _row_status(o) == "superseded", "up=%s o=%s" % (up, await _row_status(o)))
    # F6: 이상한 본문은 500 이 아니라 400
    codes = []
    for body in ([1], {"command": ["x"]}, {"command": {"a": 1}}, {"command": "update", "args": [1]}):
        try:
            await main.dashboard_send_updater_command("PC-Y06", Req(body, api_key=None, session=main.new_session("main")))
            codes.append(200)
        except main.HTTPException as e:
            codes.append(e.status_code)
        except Exception as e:
            codes.append("exc:" + type(e).__name__)
    ok("W-7 업데이터 명령 넣기 — 배열 본문·목록/객체 command·목록 args 는 400", codes == [400, 400, 400, 400], str(codes))
    # F5: 팜뷰 실패 문구로 «안 갔다» 가 확실할 때만 되돌린다 — 읽기 시간초과는 에이전트가 이미 받았을 수 있다
    res = {}
    for i, why in enumerate(("Timeout on reading data from socket", "실패", "Cannot connect to host 10.0.0.5:8766",
                             "이 PC 는 매크로 대상이 아닙니다", "에이전트 없음 — 원격으로 켤 수 없다")):
        _reset(); pc = "PC-Y1%d" % i
        c = await _send_upd(pc, "restart")
        g = await _fv_list()
        await ack(pc, g[0]["id"], False, why=why)
        res[i] = ((await _upd_poll(pc)).get("id") == c, await _row_status(c))
    ok("W-9 ★읽기 시간초과·빈 실패 → fv_failed, 업데이터가 다시 안 돌린다(재시작 두 번 방지, 2차 F5)★",
       res[0] == (False, "fv_failed") and res[1] == (False, "fv_failed"), str(res))
    ok("W-10 연결 거부·nobulk·에이전트 없음 → 업데이터가 받는다(안 간 게 확실)",
       res[2][0] and res[3][0] and res[4][0], str(res))
    # 팜뷰 ack 는 다른 테넌트 행을 못 건드린다(팜뷰 id 는 FV_TENANT 행만)
    ok("W-8 팜뷰 id 표에 없는 id 의 ack 는 아무것도 안 바꾼다", (await ack("PC-Y07", 123456789, True)).get("removed") is False)


async def t_refute3():
    """배포 반증 3차(2026-09-23 밤, 아이온2 c_agent/probe1·mig·mig2) — 두 번 실행 · 재배포 뒤 잃음 · 실행 중 행 이름표 ·
    ack 끼어듦 · 부팅 정리."""
    async def ack(pc, fid, ok_, **kw):
        return _body(await main.fv_updcmd_ack(Req({"pc": pc, "id": fid, "ok": ok_, **kw}, api_key=None, headers=H)))

    async def age(cid, sec, created=False):
        t = (datetime.now(timezone.utc) - timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S")
        async with aiosqlite.connect(db.DB_PATH) as cx:
            await cx.execute("UPDATE updater_commands SET updated_at=?%s WHERE id=?" % (", created_at=?" if created else ""),
                             (t, t, cid) if created else (t, cid))
            await cx.commit()

    def redeploy():
        main.FV_UPDCMD_QUEUE.clear(); main._UPDCMD_OWNER.clear(); main._UPDCMD_FV_PC.clear()
        main._UPDCMD_UP_PC.clear(); main._FV_REBUILT[0] = False

    # #1 probe s1 — 팜뷰가 에이전트로 restart 를 돌린 뒤 인터넷이 끊김(ack·목록 없음 60초+) → 업데이터가 또 돌리면 두 번
    _reset(); pc = "PC-R01"
    c = await _send_upd(pc, "restart")
    g = await _fv_list()
    await age(c, db.FV_CLAIM_ACK_SEC + 10)
    main._FV_LAST_SEEN[0] = time.monotonic() - main.FV_QUIET_SEC - 10
    up = await _upd_poll(pc); up2 = await _upd_poll(pc)
    main._FV_LAST_SEEN[0] = time.monotonic()
    ok("R-1 ★팜뷰 침묵 회수가 업데이터에 넘기지 않는다(두 번 실행 없음) — fv_unknown★",
       up.get("command") is None and up2.get("command") is None and await _row_status(c) == "fv_unknown",
       "%s %s %s" % (up, up2, await _row_status(c)))
    await ack(pc, g[0]["id"], False, why="Cannot connect to host")      # 모름 뒤의 «안 갔다» 는 되돌린다
    up3 = await _upd_poll(pc)
    if up3.get("id"):          # 못 받았으면 아래 ok 가 실패로 적는다(여기서 404 로 시험 전체가 죽지 않게 — 반증 r3 M1·M7)
        await main.updater_ack_command(pc, up3["id"], Req())
    ok("R-1b «모름» 행에 «안 간 게 확실» 한 실패 ack → 그때만 업데이터가 한 번 받는다",
       up3.get("id") == c and (await _upd_poll(pc)).get("command") is None and await _row_status(c) == "acked",
       "%s %s" % (up3, await _row_status(c)))
    _reset(); pc = "PC-R02"
    c = await _send_upd(pc, "restart")
    g = await _fv_list()
    await age(c, db.FV_CLAIM_ACK_SEC + 10)
    main._FV_LAST_SEEN[0] = time.monotonic() - main.FV_QUIET_SEC - 10
    await _upd_poll(pc)
    main._FV_LAST_SEEN[0] = time.monotonic()
    await ack(pc, g[0]["id"], False, why="Timeout on reading data from socket")
    ok("R-1c «모름» 행에 닿았을 수 있는 실패 ack → fv_failed, 업데이터 안 받음",
       await _row_status(c) == "fv_failed" and (await _upd_poll(pc)).get("command") is None, str(await _row_status(c)))

    # #2 probe s3 — 팜뷰가 집은 직후(응답이 선에서 사라짐) 재배포 → 팜뷰 목록에 다시 나와야 ack 로 닫힌다
    _reset(); pc = "PC-R03"
    c = await _send_upd(pc, "restart")
    g = await _fv_list()
    redeploy()
    g2 = [x for x in await _fv_list(0) if x["pc"] == pc]
    ok("R-2 ★재배포 뒤 팜뷰 목록이 DB 에서 다시 선다 — 같은 팜뷰 id(팜뷰는 UPD_DONE 으로 재실행 안 하고 ack 만)★",
       [x["id"] for x in g2] == [g[0]["id"]] and g2[0]["act"] == "restart", "%s vs %s" % (g2, g))
    ok("R-2b 재배포 뒤에도 업데이터는 그 명령을 안 받는다", (await _upd_poll(pc)).get("command") is None)
    await ack(pc, g[0]["id"], True)
    ok("R-2c 다시 선 항목의 ack → fv_done, 목록에서 빠진다",
       await _row_status(c) == "fv_done" and not [x for x in await _fv_list(0) if x["pc"] == pc], str(await _row_status(c)))
    # 재배포 뒤 목록을 부르기 전에 같은 PC 에 새 명령(다른 종류)이 들어와 칸이 차 있으면 옆 칸에 세운다
    _reset(); pc = "PC-R04"
    cu = await _send_upd(pc, "update")
    g = await _fv_list()
    redeploy()
    cr = await _send_upd(pc, "restart")
    g2 = sorted((x["act"], x["id"]) for x in await _fv_list(0) if x["pc"] == pc)
    ok("R-2d 칸이 새 명령으로 차 있어도 복구 항목은 옆 칸으로 — 둘 다 목록에",
       [a for a, _ in g2] == ["restart", "updater"] and dict(g2)["updater"] == g[0]["id"], str(g2))
    await ack(pc, g[0]["id"], True)
    ok("R-2e 옆 칸 항목의 ack 도 제 행을 닫고 새 명령은 그대로", await _row_status(cu) == "fv_done"
       and [x["act"] for x in await _fv_list(0) if x["pc"] == pc] == ["restart"] and await _row_status(cr) == "fv_claimed",
       "%s %s" % (await _row_status(cu), await _row_status(cr)))
    # 복구는 10분 지난 «모름» 은 안 세운다(끝난 것·오래된 것은 목록을 어지럽히지 않는다)
    _reset(); pc = "PC-R05"
    c = await _send_upd(pc, "restart")
    await _fv_list()
    await age(c, db.UPDATER_COMMAND_MAX_AGE_SEC + 30, created=True)
    redeploy()
    ok("R-2f 10분 지난 집은 명령은 재배포 복구에 안 나온다", not [x for x in await _fv_list(0) if x["pc"] == pc])

    # #3 probe s5 — 업데이터가 restart a 를 받아 도는 중 사람이 다시 누른 b: 팜뷰가 겹쳐 돌리지 않고, a 이름표는 그대로
    _reset(); pc = "PC-R06"
    a = await _send_upd(pc, "restart")
    main.FV_UPDCMD_QUEUE.clear()                        # a 는 업데이터 길로
    pa = await _upd_poll(pc)
    b = await _send_upd(pc, "restart")
    fv = await _fv_list()
    ok("R-3 ★업데이터가 같은 종류를 도는 중(90초 안) 새로 누른 것은 팜뷰에 안 준다(두 길이 겹쳐 안 돈다)★",
       pa.get("id") == a and fv == [] and await _row_status(b) == "pending", "pa=%s fv=%s b=%s" % (pa, fv, await _row_status(b)))
    ok("R-3b ★실행 중인 a 는 superseded 로 안 찍힌다(3차 #3 — 돈 명령이 «안 돈» 것으로 보이던 것)★",
       await _row_status(a) == "pending", str(await _row_status(a)))
    ok("R-3c 업데이터 ack 가 a 를 acked 로", await db.ack_updater_command(a) and await _row_status(a) == "acked")
    pb = await _upd_poll(pc)
    ok("R-3d 새로 누른 b 는 업데이터가 다음 폴링에 차례로 받는다(사라지지 않는다)", pb.get("id") == b, str(pb))
    # 90초가 지나 다시 누른 것은 «업데이터가 멈춤» 일 수 있어 팜뷰가 받는다
    _reset(); pc = "PC-R07"
    a = await _send_upd(pc, "restart")
    main.FV_UPDCMD_QUEUE.clear()
    await _upd_poll(pc); await db.ack_updater_command(a)
    k = (pc, "restart")
    main._UPDCMD_UP_PC[k] = (main._UPDCMD_UP_PC[k][0], time.monotonic() - main.UPDCMD_UP_BUSY_SEC - 1)
    await _age_row(a, db.UPDATER_BUSY_SEC + 5)           # DB 쪽 «방금 받음» 도 지났다(H5)
    b = await _send_upd(pc, "restart")
    fv = await _fv_list()
    ok("R-3e 90초 지나 다시 누르면 팜뷰가 받는다(멈춘 업데이터를 에이전트로 살리는 길은 그대로)",
       [x["act"] for x in fv] == ["restart"] and await _row_status(b) == "fv_claimed" and await _row_status(a) == "acked",
       "%s %s" % (fv, await _row_status(b)))
    # 다른 종류는 막지 않는다(업데이터가 update 중이어도 팜뷰 restart 는 간다 — V-9 와 같은 규칙)
    _reset(); pc = "PC-R08"
    u = await _send_upd(pc, "update")
    main.FV_UPDCMD_QUEUE.clear()
    await _upd_poll(pc)
    r = await _send_upd(pc, "restart")
    ok("R-3f 업데이터 update 중 팜뷰 restart 는 막지 않는다(같은 종류만)",
       [x["act"] for x in await _fv_list()] == ["restart"] and await _row_status(r) == "fv_claimed")
    # claim 의 busy_ids — 업데이터가 집었는데 아직 ack 전(pending)인 옛 행을 팜뷰 claim 이 superseded 로 안 찍는다
    _reset(); pc = "PC-R09"
    a = await _send_upd(pc, "update")
    main.FV_UPDCMD_QUEUE.clear()
    await _upd_poll(pc)
    k = (pc, "update")
    main._UPDCMD_UP_PC[k] = (main._UPDCMD_UP_PC[k][0], time.monotonic() - main.UPDCMD_UP_BUSY_SEC - 1)
    await _age_row(a, db.UPDATER_BUSY_SEC + 5)           # DB 쪽 «방금 내줌»(handed_at) 도 지났다
    b = await _send_upd(pc, "update")
    await _fv_list()
    ok("R-3g 업데이터가 들고 있는(ack 전) 옛 행은 팜뷰 claim 이 superseded 로 안 찍는다(busy_ids)",
       await _row_status(a) == "pending" and await _row_status(b) == "fv_claimed", "%s %s" % (await _row_status(a), await _row_status(b)))

    # #5 — 업데이터 폴링의 get_pending 을 기다리는 사이 팜뷰가 집고 ok ack(fv_done) → 업데이터는 그 행을 안 준다
    _reset(); pc = "PC-R10"
    c = await _send_upd(pc, "restart")
    orig = main.get_pending_updater_command
    gate = asyncio.Event()

    async def held(*a, **k):
        r = await orig(*a, **k)
        await gate.wait()
        return r
    main.get_pending_updater_command = held
    try:
        async def fv_side():
            await asyncio.sleep(0.05)
            g = await _fv_list()
            main._UPDCMD_OWNER.clear(); main._UPDCMD_FV_PC.clear()   # 겹친 두 인스턴스(재배포) — 소유 표가 안 보인다
            await ack(pc, g[0]["id"], True)
            gate.set()
        up, _ = await asyncio.gather(_upd_poll(pc), fv_side())
    finally:
        main.get_pending_updater_command = orig
    ok("R-5 ★get_pending 사이에 fv_done 이 된 행은 업데이터에 안 준다(소유 표가 안 보여도 DB 로 한 번 더)★",
       up.get("command") is None and await _row_status(c) == "fv_done", "%s %s" % (up, await _row_status(c)))
    ok("R-5b 대기 행은 fv_done 이 될 수 없다(finish 는 fv_claimed·fv_unknown 에서만)",
       not await db.finish_updater_command_fv(await _send_upd("PC-R11", "update"), True)
       and await _row_status(await db.insert_updater_command("PC-R11b", "update", {})) == "pending")
    # 집은 뒤 DB 가 대기가 아니면(소유를 이번에 새로 잡았을 때만) 소유를 풀어 팜뷰 길을 막지 않는다
    ok("R-5c 버린 행의 소유 표식은 남지 않는다", main._UPDCMD_OWNER.get(c) != "updater", str(main._UPDCMD_OWNER.get(c)))

    # #6 mig — 부팅은 큰 DELETE·인덱스를 안 한다(healthcheck 10초) — B-DB7-a 와 짝: lifespan 이 백그라운드로 띄운다
    import inspect
    src = inspect.getsource(db.init_db)
    ok("R-6 init_db 에 명령 표 DELETE·idx_cmd_at 생성이 없다(백그라운드 maintain_command_tables 로)",
       "DELETE FROM commands" not in src and "CREATE INDEX IF NOT EXISTS idx_cmd_at" not in src
       and "CREATE INDEX IF NOT EXISTS idx_cmd_at" in inspect.getsource(db.maintain_command_tables), "")


_BOOT_JS = r"""
const ss = {}; global.sessionStorage = {getItem:k => (k in ss ? ss[k] : null), setItem:(k,v) => { ss[k] = String(v); }};
let reloads = 0; global.location = {reload(){ reloads++; }};
let answers = []; let slow = false;
global.fetch = async () => { const b = answers.shift(); if (slow) await new Promise(r => setTimeout(r, 30));
  return {ok:true, json: async () => ({boot:b})}; };
let t = 0; Date.now = () => t;
""" + "%s" + r"""
(async () => {
  const o = {};
  const run = async (seq) => { for (const b of seq) { answers.push(b); await checkServerBoot(); answers.length = 0; } };   // 안 쓴 답은 버린다(조기 반환)
  await run(['A','A']);                     o.stable = reloads;
  await run(['B','A','B','A']);             o.flap = reloads;          // 재배포 중 두 인스턴스가 번갈아
  await run(['B','B']);                     o.real = reloads;          // 같은 새 boot 두 번 = 한 번 새로고침
  await run(['B','B','B']);                 o.afterReload = reloads;   // 새로고침 중 재호출 무시
  // 새 화면: 옛 인스턴스(A)가 먼저 답해도 기준값은 B → 이어서 B 가 와도 다시 안 새로고침
  serverBoot = null; _bootCand = null; _bootReloading = false;
  t = 5000; await run(['A','A','B','B','A','B','B']); o.newPage = reloads;
  // 1분 안 진짜 새 재배포 C — 60초 뒤에야 한 번
  await run(['C','C']); o.cWithin = reloads; t = 70000; await run(['C']); o.cAfter = reloads;
  // 겹친 폴링(느린 fetch) 두 번이 동시에 와도 한 번 본 것으로
  serverBoot = 'X'; _bootCand = null; _bootReloading = false; slow = true; t = 500000;
  answers.push('Y','Y'); await Promise.all([checkServerBoot(), checkServerBoot()]); o.overlap = reloads;
  console.log(JSON.stringify(o));
})();
"""


_BOOT_JS2 = r"""
global.window = {name: ''};
global.sessionStorage = {getItem(){ throw new Error('denied'); }, setItem(){ throw new Error('denied'); }};
let reloads = 0; global.location = {reload(){ reloads++; }};
let hang = false, answers = [];
global.fetch = async () => hang ? new Promise(() => {}) : ({ok:true, json: async () => ({boot: answers.shift()})});
let t = 1000; Date.now = () => t;
""" + "%s" + r"""
(async () => {
  const o = {};
  BOOT_PING_TIMEOUT_MS = 50;
  const one = async (b) => { answers = [b]; await checkServerBoot(); };
  await one('A');
  hang = true; await checkServerBoot(); hang = false;                  // 답 없는 ping — 50ms 뒤 풀려야 한다
  await one('B'); await one('B'); o.afterHang = reloads;
  // sessionStorage 가 막힌 창: 새로고침 뒤(같은 탭, window.name 유지) 옛 인스턴스 A 가 답해도 다시 안 새로고침
  serverBoot = null; _bootCand = null; _bootReloading = false; t = 5000;
  for (const b of ['A','A','B','A','B','B','A','A']) await one(b);
  o.ssBlocked = reloads; o.wname = String(window.name).slice(0, 14);
  console.log(JSON.stringify(o));
})();
"""


def t_boot_js():
    if not _need_node("B"):
        return
    src = main.HTML_DASHBOARD
    i = src.index("let serverBoot=null")
    j = src.index("// ─── 명령 내역", i)
    o = _run_node(_BOOT_JS % src[i:j])
    ok("B-1 boot 가 그대로면 새로고침 0", o["stable"] == 0, str(o))
    ok("B-2 ★재배포 중 A/B 번갈아 답해도 새로고침 0(연쇄 없음)★", o["flap"] == 0, str(o))
    ok("B-3 같은 새 boot 를 연속 두 번 보면 한 번 새로고침", o["real"] == 1, str(o))
    ok("B-4 새로고침 진행 중엔 더 안 부른다", o["afterReload"] == 1, str(o))
    ok("B-5 새 화면이 옛 인스턴스 답(A)을 기준값·변화로 안 읽는다", o["newPage"] == 1, str(o))
    ok("B-6 1분 안 재새로고침 금지, 지나면 한 번", o["cWithin"] == 1 and o["cAfter"] == 2, str(o))
    ok("B-7 겹친 폴링 두 번은 한 번 본 것으로(새로고침 0)", o["overlap"] == 2, str(o))
    o2 = _run_node(_BOOT_JS2 % src[i:j])
    ok("B-8 답 없는 ping 이 가드를 영영 쥐지 않는다 — 풀린 뒤 새 boot 두 번이면 새로고침(반증 J1)", o2["afterHang"] == 1, str(o2))
    ok("B-9 sessionStorage 가 막혀도 window.name 으로 기억 — 새 화면이 옛 인스턴스에 다시 새로고침 안 함(반증 J2)",
       o2["ssBlocked"] == 1 and o2["wname"].startswith("dashBootReload"), str(o2))


def test_all():
    run_all([t_u_fv_first, t_u_updater_first, t_u_race, t_u_expired_and_legacy, t_q_seq, t_timeout, t_boot_js,
             t_break2_updcmd, t_break2_timeout, t_blockers_bundle, t_fv_command_err, t_refute2, t_refute3])
    finish("test_fv_found", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
