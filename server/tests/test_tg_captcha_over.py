# -*- coding: utf-8 -*-
"""[대시보드] 주인님 #351 (2026-09-30 22:51) — 끝난 캡차의 대기가 다음 PC 의 코드를 막았다.

  PC-02b 캡차는 22:26 에 끝났는데 tg_map 대기가 30분(TG_REPLY_WINDOW 1800) 남아, 22:51 에 온 PC-03b 코드(답장 없이)가
  «어느 PC인지 알 수 없습니다 … 대기 중: PC-02b, PC-03b» 로 거절됐다.
  ① 기본 창 300초  ② POST /telegram/captcha_over/{pc} 가 그 PC 대기를 바로 걷는다(멱등·매크로 API 키)  ③ 낡은 대기는 창 밖.

    cd updater/server && python -X utf8 tests/test_tg_captcha_over.py
"""
import json
from datetime import datetime, timezone, timedelta

from _harness import main, db, ok, Req, run_all, finish, HTTPException, TG_SENT   # noqa: E402

MIN_CHECKS = 11
CHAT = "8851"


def _setup():
    main.TENANTS.setdefault("main", {})["chat_id"] = CHAT
    main.CHAT_TO_TENANT[CHAT] = "main"


async def _caps():
    rows = await db.get_recent_commands(200)
    return [(r["pc_id"], str(r.get("args"))) for r in rows if r["command"] == "captcha_code"]


async def _say(text, reply=None):
    m = {"chat": {"id": CHAT}, "text": text}
    if reply is not None:
        m["reply_to_message"] = {"message_id": reply}
    await main._tg_handle_update({"message": m})


async def _age(mid, seconds):
    """대기 행의 시각을 과거로 민다(가상 시계 대신 행 시각)."""
    import aiosqlite
    t = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE telegram_map SET created_at=? WHERE message_id=?", (t, mid))
        await c.commit()


async def t_window_default():
    ok("T351-1 ★기본 답장 추론 창이 60초(#352: 캡차는 1분이면 끝난다)★", main.TG_REPLY_WINDOW == 60, str(main.TG_REPLY_WINDOW))


async def t_incident_replay():
    """22:51 그대로 — 끝난 PC-02b 대기 + 새 PC-03b 캡차, 답장 없는 코드."""
    _setup()
    TG_SENT.clear()
    await db.tg_map_put(9001, "PC-02b", CHAT)
    await _age(9001, 25 * 60)                       # 22:26 에 끝난 캡차 — 25분 전
    await db.tg_map_put(9002, "PC-03b", CHAT)       # 22:51 새 캡차
    await _say("7ZTIJ")
    caps = await _caps()
    ok("T351-2 ★사고 재현: 25분 전 끝난 PC-02b 대기가 창 밖이라 코드는 유일한 대기 PC-03b 로 간다★",
       any(p == "PC-03b" and "7ZTIJ" in a for p, a in caps)
       and not any("어느 PC인지 알 수 없습니다" in t for _c, t in TG_SENT), "caps=%s sent=%s" % (caps[-2:], TG_SENT[-2:]))
    await db.tg_map_delete_pc("PC-02b")


async def t_reset_photo_recount():
    """#352 — 60초는 ★가장 최근 사진★ 부터. 리셋마다 사진이 새로 나가 새 행(새 시각)이 쌓인다."""
    _setup()
    TG_SENT.clear()
    await db.tg_map_put(9301, "PC-07b", CHAT)
    await _age(9301, 90)                            # 첫 사진 90초 전 — 창 밖
    await _say("old001")
    caps = await _caps()
    ok("T352-1 ★사진이 90초 전 것뿐이면 코드는 추론 대상이 아니다(대기 없음 → 조용히 무시)★",
       not any("old001" in a for _p, a in caps) and not any("어느 PC인지" in t for _c, t in TG_SENT), str(caps[-2:]))
    await db.tg_map_put(9302, "PC-07b", CHAT)       # 오답 → 리셋 → 새 사진(새 행·새 시각)
    await _age(9302, 20)                            # 20초 전 — 창 안
    await _say("new002")
    caps = await _caps()
    ok("T352-2 ★리셋으로 새 사진이 나가면 그 시각부터 다시 센다(첫 사진은 90초 전이어도 코드가 PC-07b 로 간다)★",
       any(p == "PC-07b" and "new002" in a for p, a in caps), str(caps[-2:]))
    await db.tg_map_delete_pc("PC-07b")


async def t_captcha_over():
    _setup()
    TG_SENT.clear()
    # 창 안(방금 끝남)이라도 매크로가 「끝났다」고 하면 바로 걷는다
    await db.tg_map_put(9101, "PC-04b", CHAT)
    await db.tg_map_put(9102, "PC-05b", CHAT)
    await _say("zz9999")                            # 대기 둘 · 답장 없음 → 거절(이 시험이 헛돌지 않는 대조)
    refused = any("어느 PC인지 알 수 없습니다" in t and "PC-04b" in t for _c, t in TG_SENT)
    r = await main.telegram_captcha_over("PC-04b", Req())
    body = json.loads(bytes(r.body))
    TG_SENT.clear()
    await _say("yy8888")
    caps = await _caps()
    ok("T351-3 ★대조: 대기 둘이면 거절 → captcha_over(PC-04b) 뒤 코드는 남은 PC-05b 로 간다★",
       refused and body.get("ok") is True and any(p == "PC-05b" and "yy8888" in a for p, a in caps)
       and not any("어느 PC인지 알 수 없습니다" in t for _c, t in TG_SENT), "refused=%s caps=%s" % (refused, caps[-2:]))
    row = await db.tg_map_get(9101)
    ok("T351-4 걷은 행은 kind=done 으로 남는다(늦은 답장은 «이미 처리됐거나 만료»)", row is not None and row.get("kind") == "done", str(row))
    TG_SENT.clear()
    await _say("late001", reply=9101)
    ok("T351-5 걷힌 사진에 늦게 단 답장은 거절되고 코드가 안 간다",
       any("이미 처리됐거나 만료" in t for _c, t in TG_SENT)
       and not any("late001" in a for _p, a in await _caps()))
    r1 = await main.telegram_captcha_over("PC-04b", Req())
    r2 = await main.telegram_captcha_over("PC-99z", Req())
    ok("T351-6 ★멱등: 두 번째·대기 없는 PC 도 200(ok:true)★",
       json.loads(bytes(r1.body)).get("ok") is True and json.loads(bytes(r2.body)).get("ok") is True)
    await db.tg_map_delete_pc("PC-05b")


async def t_auth_and_scope():
    _setup()
    await db.tg_map_put(9201, "PC-06b", CHAT)
    try:
        await main.telegram_captcha_over("PC-06b", Req(api_key=None))
        denied = False
    except HTTPException as e:
        denied = e.status_code == 403
    try:
        await main.telegram_captcha_over("PC-06b", Req(api_key="wrong"))
        denied2 = False
    except HTTPException as e:
        denied2 = e.status_code == 403
    row = await db.tg_map_get(9201)
    ok("T351-7 ★매크로 API 키가 없거나 틀리면 403 · 대기는 그대로★", denied and denied2 and row.get("kind") == "captcha", str(row))
    await main.telegram_captcha_over("PC-06", Req())          # 형제 카드(PC-06)는 PC-06b 를 안 지운다
    row = await db.tg_map_get(9201)
    ok("T351-8 그 카드 것만 걷는다(PC-06 로 불러도 PC-06b 대기는 그대로)", row.get("kind") == "captcha", str(row))
    await main.telegram_captcha_over("PC-06b", Req())
    row = await db.tg_map_get(9201)
    ok("T351-9 PC-06b 로 부르면 걷힌다", row.get("kind") == "done", str(row))


def test_all():
    run_all([t_window_default, t_incident_replay, t_reset_photo_recount, t_captcha_over, t_auth_and_scope])
    finish("test_tg_captcha_over", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
