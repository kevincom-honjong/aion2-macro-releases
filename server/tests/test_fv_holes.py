# -*- coding: utf-8 -*-
"""배포 반증 3차가 찾은 팜뷰 명령 경로 구멍 H1~H8 + 살아남은 뮤턴트 M9 (2026-09-23 밤).

  각 시험은 «안전한 동작»을 단정한다 — 옛 코드(구멍 있던 판)에서는 [FAIL] 이다(반증 원본: scratchpad refute_r3).
  H1 업데이터 바쁨(90초)으로 팜뷰가 거절한 명령을 ★버리지 않고 미룬다★ — 90초 뒤 업데이터가 죽어 있으면 팜뷰가 받는다
  H2 같은 PC 에 다른 종류 명령이 새로 와도 ★팜뷰가 이미 집은★ 명령이 목록에서 안 사라진다(곁칸 nspc#id)
  H3 재배포 뒤 재건이 한 번 실패해도 다음 목록에서 다시 한다
  H4 재건 도중 ack 된 명령은 다시 안 나온다
  H5 업데이터 바쁨 규칙이 재배포 뒤에도 산다(DB: 같은 종류 acked 90초 안)
  H6 팜뷰 커서(since=ack 한 가장 큰 id) 아래로 숨은 ★ack 전★ 명령도 나온다
  H7 업데이터가 죽어도 오래된 팜뷰 집음이 30초 청소로 fv_unknown 이 된다
  H8 팜뷰가 같은 종류를 실행 중(집음, ack 전)이면 업데이터가 새 명령을 안 가져간다
  M9 fv_unknown 은 재배포 뒤 다시 안 세운다(반증 v2 #2 — «다시 보내지 않았습니다») · 늦은 ack 는 id 로 fv_done

    cd updater/server && python -X utf8 tests/test_fv_holes.py
"""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import aiosqlite
from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 13
TOK = "fvsecret-holes"
H = {"X-FV-Token": TOK}


def _body(r):
    return json.loads(bytes(r.body))


def _reset():
    main.FV_TOKEN = TOK
    main.FV_TENANT = "main"
    main.FV_UPDCMD_QUEUE.clear(); main._UPDCMD_OWNER.clear(); main._UPDCMD_FV_PC.clear()
    main._UPDCMD_UP_PC.clear(); main._FV_ACKED_RECENT.clear(); main._FV_REBUILT[0] = True
    main._FV_LAST_SEEN[0] = time.monotonic()


def _redeploy():
    """재배포 = 메모리만 비고 DB 는 남는다."""
    main.FV_UPDCMD_QUEUE.clear(); main._UPDCMD_OWNER.clear(); main._UPDCMD_FV_PC.clear()
    main._UPDCMD_UP_PC.clear(); main._FV_ACKED_RECENT.clear(); main._FV_REBUILT[0] = False


async def _send(pc, cmd="update"):
    r = await main.dashboard_send_updater_command(pc, Req({"command": cmd}, api_key=None, session=main.new_session("main")))
    return _body(r)["id"]


async def _upoll(pc):
    return _body(await main.updater_poll_command(pc, Req()))


async def _fvl(since=0):
    return _body(await main.fv_updcmd_list(Req(api_key=None, headers=H), since=str(since)))["cmds"]


async def _ack(pc, fid, ok_, **kw):
    return _body(await main.fv_updcmd_ack(Req({"pc": pc, "id": fid, "ok": ok_, **kw}, api_key=None, headers=H)))


async def _st(cid):
    async with aiosqlite.connect(db.DB_PATH) as c:
        async with c.execute("SELECT status FROM updater_commands WHERE id=?", (cid,)) as cur:
            r = await cur.fetchone()
    return r[0] if r else None


async def _age_row(cid, sec, col="updated_at"):
    t = (datetime.now(timezone.utc) - timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute(f"UPDATE updater_commands SET {col}=? WHERE id=?", (t, cid))
        await c.commit()


def _age_up(pc, cmd, sec):
    k = (pc, cmd)
    main._UPDCMD_UP_PC[k] = (main._UPDCMD_UP_PC[k][0], time.monotonic() - sec)


async def h1_busy_refusal_defers():
    _reset(); pc = "PC-H1"
    a = await _send(pc, "restart")
    main.FV_UPDCMD_QUEUE.clear()
    pa = await _upoll(pc)                                  # 업데이터가 a 를 집고 ack 한 뒤 멈춤
    await main.updater_ack_command(pc, pa["id"], Req())
    b = await _send(pc, "restart")                         # 사람이 다시 누름
    first = [e for e in await _fvl() if e["pc"] == pc]     # 90초 안 — 거절(설계)
    _age_up(pc, "restart", main.UPDCMD_UP_BUSY_SEC + 5)
    await _age_row(a, db.UPDATER_BUSY_SEC + 5)
    later = [e for e in await _fvl() if e["pc"] == pc]
    ok("H1 바쁨 거절은 버리지 않고 미룬다 — 90초 뒤(업데이터 죽음) 팜뷰가 b 를 받는다",
       first == [] and [x["act"] for x in later] == ["restart"] and await _st(b) == "fv_claimed",
       "first=%s later=%s b=%s a=%s" % (first, later, await _st(b), await _st(a)))


async def h2_push_keeps_inflight_claim():
    _reset(); pc = "PC-H2"
    x = await _send(pc, "update")
    g = [e for e in await _fvl() if e["pc"] == pc]         # 서버는 X 를 집었는데 응답이 전선에서 사라짐
    y = await _send(pc, "restart")                         # 다른 종류(둘 다 돌아야 한다)
    g2 = [e for e in await _fvl(0) if e["pc"] == pc]
    ids = sorted(e["id"] for e in g2)
    ok("H2 같은 PC 에 다른 종류가 와도 집힌 X 는 목록에 남는다(둘 다 나온다)",
       len(g) == 1 and g[0]["id"] in ids and len(ids) == 2,
       "X=%s(%s) listed=%s Y=%s" % (x, await _st(x), g2, await _st(y)))
    r = await _ack(pc, g[0]["id"], True)
    ok("H2b 곁칸의 X 도 ack 로 닫힌다(fv_done) — 아무도 실행 안 한 채 fv_unknown 으로 가지 않는다",
       await _st(x) == "fv_done", "ack=%s X=%s" % (r, await _st(x)))


async def h3_rebuild_retries():
    _reset(); pc = "PC-H3"
    c = await _send(pc, "restart")
    g = [e for e in await _fvl() if e["pc"] == pc]
    _redeploy()
    orig = main.open_fv_claims
    n = [0]

    async def flaky():
        n[0] += 1
        if n[0] == 1:
            raise RuntimeError("database is locked")
        return await orig()
    main.open_fv_claims = flaky
    try:
        l1 = await _fvl(0)
        l2 = await _fvl(0)
    finally:
        main.open_fv_claims = orig
    ok("H3 재건이 한 번 실패해도 다음 목록에서 다시 한다",
       [e["id"] for e in l2 if e["pc"] == pc] == [g[0]["id"]] and n[0] == 2,
       "l1=%s l2=%s calls=%d st=%s" % (l1, l2, n[0], await _st(c)))


async def h4_rebuild_skips_acked():
    _reset(); pc = "PC-H4"
    c = await _send(pc, "restart")
    g = [e for e in await _fvl() if e["pc"] == pc]
    _redeploy()
    orig = main.open_fv_claims
    gate = asyncio.Event()

    async def slow():
        r = await orig()
        await gate.wait()
        return r
    main.open_fv_claims = slow
    try:
        async def acker():
            await asyncio.sleep(0.05)
            await _ack(pc, g[0]["id"], True)
            gate.set()
        l1, _ = await asyncio.gather(_fvl(0), acker())
    finally:
        main.open_fv_claims = orig
    l2 = await _fvl(0)
    ok("H4 재건 도중 ack 된 명령(fv_done)은 다시 안 나온다",
       await _st(c) == "fv_done" and not [e for e in l1 + l2 if e["pc"] == pc],
       "st=%s l1=%s l2=%s" % (await _st(c), l1, l2))


async def h5_busy_survives_redeploy():
    _reset(); pc = "PC-H5"
    a = await _send(pc, "restart")
    main.FV_UPDCMD_QUEUE.clear()
    pa = await _upoll(pc)
    await main.updater_ack_command(pc, pa["id"], Req())
    _redeploy()
    b = await _send(pc, "restart")
    fv = [e for e in await _fvl() if e["pc"] == pc]
    ok("H5 재배포 뒤에도 같은 종류 acked 90초 안이면 팜뷰가 안 받는다(b 는 pending)",
       fv == [] and await _st(b) == "pending", "fv=%s a=%s b=%s" % (fv, await _st(a), await _st(b)))


async def h6_cursor_below_unacked():
    _reset(); pa_, pb_ = "PC-HA6", "PC-HB6"
    await _send(pa_, "restart")
    await _send(pb_, "restart")
    g = await _fvl()
    ida = [e["id"] for e in g if e["pc"] == pa_][0]
    idb = [e["id"] for e in g if e["pc"] == pb_][0]
    lo, hi = (ida, idb) if ida < idb else (idb, ida)
    lo_pc, hi_pc = (pa_, pb_) if ida < idb else (pb_, pa_)
    _redeploy()                                            # 낮은 id 의 ack 는 옛 인스턴스와 함께 사라짐
    await _fvl(0)                                          # 새 인스턴스 첫 목록 = 재건
    await _ack(hi_pc, hi, True)                            # 높은 id 는 ack 성공 → 팜뷰 커서 = hi
    l = await _fvl(hi)
    ok("H6 커서(ack 한 가장 큰 id) 아래의 ack 전 명령도 나온다",
       lo in [e["id"] for e in l if e["pc"] == lo_pc], "since=%d list=%s" % (hi, l))
    await _ack(lo_pc, lo, True)
    l2 = await _fvl(hi)
    ok("H6b ack 뒤에는 커서 아래 명령이 다시 안 나온다",
       not [e for e in l2 if e["pc"] in (pa_, pb_)], str(l2))


async def h7_sweep_without_updater():
    _reset(); pc = "PC-H7"
    c = await _send(pc, "update")
    await _fvl()
    async with aiosqlite.connect(db.DB_PATH) as cx:
        t = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        await cx.execute("UPDATE updater_commands SET created_at=?, updated_at=? WHERE id=?", (t, t, c))
        await cx.commit()
    main._FV_LAST_SEEN[0] = time.monotonic() - main.FV_QUIET_SEC - 10
    await main._fv_claim_sweep_once()                      # 업데이터 폴링 없이 30초 청소만
    ok("H7 업데이터가 안 와도 2시간 된 팜뷰 집음이 fv_claimed 로 남지 않는다(fv_unknown)",
       await _st(c) == "fv_unknown", str(await _st(c)))
    main._FV_LAST_SEEN[0] = time.monotonic()


async def h8_updater_waits_for_fv():
    _reset(); pc = "PC-H8"
    x = await _send(pc, "restart")
    await _fvl()                                           # 팜뷰가 X 를 실행 중
    y = await _send(pc, "restart")
    up = await _upoll(pc)
    ok("H8 팜뷰가 같은 종류를 실행 중이면 업데이터가 새 명령을 안 가져간다",
       up.get("command") is None and await _st(y) in ("pending", "fv_claimed"),
       "X=%s up=%s Y=%s" % (await _st(x), up, await _st(y)))
    up2 = await _upoll("PC-H8x")                           # 다른 PC 는 영향 없음(빈 큐)
    ok("H8b 다른 PC 의 폴링은 막히지 않는다", up2.get("command") is None, str(up2))


async def m9_unknown_rebuilt_then_late_ack():
    _reset(); pc = "PC-M9"
    c = await _send(pc, "update")
    g = [e for e in await _fvl() if e["pc"] == pc]
    main._FV_LAST_SEEN[0] = time.monotonic() - main.FV_QUIET_SEC - 10
    await _age_row(c, db.FV_CLAIM_ACK_SEC + 5)
    await main._fv_claim_sweep_once()
    s1 = await _st(c)
    main._FV_LAST_SEEN[0] = time.monotonic()
    _redeploy()
    l = [e for e in await _fvl(0) if e["pc"] == pc]
    ok("M9 fv_unknown 은 재배포 뒤 목록에 다시 안 선다(재시작한 팜뷰가 다시 실행하지 않게, 반증 v2 #2)",
       s1 == "fv_unknown" and l == [], "s1=%s list=%s" % (s1, l))
    await _ack(pc, g[0]["id"], True)
    ok("M9b 목록에 없어도 늦은 ack 로 fv_unknown → fv_done", await _st(c) == "fv_done", str(await _st(c)))


def test_all():
    run_all([h1_busy_refusal_defers, h2_push_keeps_inflight_claim, h3_rebuild_retries, h4_rebuild_skips_acked,
             h5_busy_survives_redeploy, h6_cursor_below_unacked, h7_sweep_without_updater, h8_updater_waits_for_fv,
             m9_unknown_rebuilt_then_late_ack])
    finish("test_fv_holes", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
