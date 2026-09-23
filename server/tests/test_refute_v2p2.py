# -*- coding: utf-8 -*-
"""v2 반증 2부 (2026-09-24, 아이온2 반증자) — 살아남은 뮤턴트 M14·M16 을 죽이는 시험.
(M3 = 시계 어긋남 상한은 tests/test_refute_v2p1.py A-M3·A-M3b 가 죽인다.)

  M14 겹친 팜뷰 목록 요청의 «집는 중이면 건너뜀»(main.fv_updcmd_list `if v.get("claiming")`) — U-20 은 두 요청이 실제로
      겹치지 않았다. DB 집기를 늦춰 ★진짜로 겹친★ 두 /api/fv/updcmd 를 돌린다: 한쪽만 받고, DB 집기는 한 번, 그리고 ack 전
      다음 목록이 같은 명령을 redeliver 로 다시 준다(겹친 쪽이 큐에서 빼 버리면 팜뷰가 죽었을 때 영영 안 닫힌다).
  M16 /api/fv/command 시간 상한(main.FV_CMD_DEADLINE_S)은 팜뷰 타임아웃(farmview/fvdash.py C["timeout"])보다 3초 이상
      짧아야 한다 — 같거나 길면 팜뷰가 본문을 못 받고 «실패» 로 보고 사람이 다시 누른다(이미 나간 PC 에 두 번).

    cd updater/server && python -X utf8 tests/test_refute_v2p2.py
"""
import asyncio
import json
import os
import re
import time

from _harness import main, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 5
TOK = "fvsecret-rf2"
H = {"X-FV-Token": TOK}
_FVDASH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "farmview", "fvdash.py")


def _body(r):
    return json.loads(bytes(r.body))


def _reset():
    main.FV_TOKEN = TOK
    main.FV_TENANT = "main"
    main.FV_UPDCMD_QUEUE.clear(); main._UPDCMD_OWNER.clear(); main._UPDCMD_FV_PC.clear()
    main._UPDCMD_UP_PC.clear(); main._FV_ACKED_RECENT.clear(); main._FV_REBUILT[0] = True
    main._FV_LAST_SEEN[0] = time.monotonic()


async def _send_upd(pc, cmd="update"):
    r = await main.dashboard_send_updater_command(pc, Req({"command": cmd}, api_key=None, session=main.new_session("main")))
    return _body(r)["id"]


async def _fv_list():
    return _body(await main.fv_updcmd_list(Req(api_key=None, headers=H), since="0"))["cmds"]


async def t_overlapping_claim():
    _reset()
    pc = "PC-RC1"
    await _send_upd(pc, "restart")
    orig = main.claim_updater_command_for_fv
    entered = []

    async def slow(*a, **k):
        entered.append(1)
        await asyncio.sleep(0.3)            # 두 목록 요청이 ★확실히★ 겹치게 DB 집기를 늦춘다
        return await orig(*a, **k)
    main.claim_updater_command_for_fv = slow
    try:
        a, b = await asyncio.gather(_fv_list(), _fv_list())
    finally:
        main.claim_updater_command_for_fv = orig
    got = [x for x in a + b if x["pc"] == pc]
    ok("M14-a ★진짜로 겹친 목록 두 판 — 정확히 한쪽만 받는다★", len(got) == 1, "%s | %s" % (a, b))
    ok("M14-b ★DB 집기는 한 번만 — 겹친 쪽은 «집는 중» 이라 건너뛴다★", len(entered) == 1, str(len(entered)))
    third = [x for x in await _fv_list() if x["pc"] == pc]
    ok("M14-c ★ack 전 다음 목록이 같은 명령을 redeliver 로 다시 준다(겹친 쪽이 큐에서 안 뺐다)★",
       len(third) == 1 and third[0].get("redeliver") is True and got and third[0]["id"] == got[0]["id"], str(third))


async def t_deadline_under_farmview_timeout():
    fv_to, src = 12.0, "기본값(팜뷰 소스 없음)"
    try:
        m = re.search(r'"timeout"\s*:\s*([0-9.]+)', open(_FVDASH, encoding="utf-8").read())
        if m:
            fv_to, src = float(m.group(1)), "farmview/fvdash.py"
    except OSError:
        pass
    d = main.FV_CMD_DEADLINE_S
    ok("M16 ★FV_CMD_DEADLINE_S 는 팜뷰 타임아웃보다 3초 이상 짧다★", 0 < d <= fv_to - 3,
       "deadline=%s fv_timeout=%s (%s)" % (d, fv_to, src))
    ok("M16b 너무 짧지도 않다(한 대 명령이 보통 1초 안 — 4초 미만이면 멀쩡한 명령이 «닿았는지 모름» 이 된다)", d >= 4, str(d))


def test_all():
    run_all([t_overlapping_claim, t_deadline_under_farmview_timeout])
    finish("test_refute_v2p2", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
