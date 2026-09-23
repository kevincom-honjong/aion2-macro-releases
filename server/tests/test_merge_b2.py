# -*- coding: utf-8 -*-
"""[대시보드] 2026-09-23 네 패치 병합 반증(brk2_merge) 확정분 회귀 가드 — 실제 호출.

각 검사는 고치기 전 코드(r2base)에서 빨간불, 고친 코드에서 초록불이 되도록 짰다. 옛 가드(지켜야 할 쪽)는
빨간불이 나는 조건과 ★한 검사로 묶었다★ — 가드만 따로 두면 base 에서도 초록이라 증거가 못 된다.
  M1  B-JS1: merge 로 온 ★로컬 스냅샷 통째★(창고키나·각성 0·표층·재연결 재전송)가 모든 캐릭에 새 시각을
      찍어, 수요일 05시 리셋 전 값이 리셋 보정(/characters·_fv_char_agg)을 벗어났다
  M2  B-CQ12: 계정1 카드(base)만 은퇴한 PC 의 업데이터 명령을 410 으로 막았다(형제 카드 PC-XXb 는 살아 있음)
  M3  B-TG1: 매핑에 없는 메시지(봇 「다시 보내주세요」·expect_reply 없는 알림)에 단 답장까지 거절해,
      기다리는 PC 가 하나뿐이어도 코드가 버려졌다
  M4  B-DB9: 카드 삭제(청소 — 같은 id 로 부활)가 캡차 답장 경로를 지웠다
  M5  B-FV3: 업데이터 줄의 시각을 정하고 커밋하기 전 FV 폴링이 커서를 그 시각 너머로 내주면 영영 안 왔다
"""
import json
from datetime import datetime, timezone, timedelta

from _harness import (main, db, ok, Req, run_all, finish,   # noqa: E402
                      HTTPException, TG_SENT)

MIN_CHECKS = 17     # 2026-09-23 실측 17 — 검사를 더하면 같이 올린다

FMT = "%Y-%m-%dT%H:%M:%S"
CHAT = "8802"


def _body(r):
    return json.loads(bytes(r.body))


def _pre_reset():
    return (main._fv_last_weekly_reset_utc() - timedelta(hours=1)).strftime(FMT)


CHARS = [
    {"slot": 1, "name": "가", "awakening_ticket": 0, "daily_ticket": 2, "abyss_time": "00:00:00"},
    {"slot": 2, "name": "나", "awakening_ticket": 1, "daily_ticket": 5, "abyss_time": "03:00:00"},
]


async def _chars_rows(pid):
    sess = main.new_session("main")
    rows = _body(await main.get_all_characters(Req(api_key=None, session=sess)))["characters"]
    return {r["slot"]: r for r in rows if r["pc_id"] == pid}


def _aw(r):
    return main._fv_ticket_reset_aware(r["collected_at"], r["awakening_ticket"], 3)


# ───────────────────────── M1 ─────────────────────────
async def t_m1():
    pid = "PC-M1"
    pre = _pre_reset()
    await main.receive_char_info(pid, Req({"total_kina": 1000, "collected_at": pre,
                                           "characters": [dict(c) for c in CHARS]}))
    # 창고키나 갱신(info_collector 사고 569) — 로컬 스냅샷 통째 + merge
    await main.receive_char_info(pid, Req({"total_kina": 5000, "merge": True, "collected_at": pre,
                                           "characters": [dict(c) for c in CHARS]}))
    rb = await _chars_rows(pid)
    agg = (await main._fv_char_agg("main")).get(pid, {}).get("awakening_ticket")
    info = await db.get_char_info(pid)
    ok("M1-a 스냅샷 통째 merge 뒤에도 안 바뀐 캐릭은 리셋 보정(3,3 · 합 6) — 키나는 갱신",
       [_aw(rb[1]), _aw(rb[2])] == [3, 3] and agg == 6 and info["total_kina"] == 5000,
       "aw=%s agg=%s kina=%s ca=%s" % ([_aw(rb[1]), _aw(rb[2])], agg, info["total_kina"],
                                       [rb[1]["collected_at"], rb[2]["collected_at"]]))

    # 각성 0 기록(awakening._mark_slot_done) — 슬롯2 만 1→0, 나머지는 옛 값 그대로 통째로
    snap = [dict(c) for c in CHARS]
    snap[1]["awakening_ticket"] = 0
    await main.receive_char_info(pid, Req({"total_kina": 5000, "merge": True, "collected_at": pre,
                                           "characters": snap}))
    rb = await _chars_rows(pid)
    agg = (await main._fv_char_agg("main")).get(pid, {}).get("awakening_ticket")
    ok("M1-b 바뀐 슬롯2 만 새 시각(0 그대로), 안 바뀐 슬롯1 은 리셋 보정 3 — 합 3",
       _aw(rb[2]) == 0 and _aw(rb[1]) == 3 and agg == 3 and rb[1]["collected_at"] == pre,
       "aw=%s agg=%s ca=%s" % ([_aw(rb[1]), _aw(rb[2])], agg, [rb[1]["collected_at"], rb[2]["collected_at"]]))

    # 이미 자기 시각이 있는 캐릭(리셋 전) — 같은 값으로 다시 통째 merge 돼도 시각이 안 밀린다
    pid2 = "PC-M1c"
    old = [dict(c, collected_at=pre) for c in CHARS]
    await db.upsert_char_info(pid2, 10, old, merge=False, collected_at=pre)
    await db.upsert_char_info(pid2, 20, [dict(c) for c in CHARS], merge=True)
    by = {c["slot"]: c for c in (await db.get_char_info(pid2))["chars"]}
    ok("M1-c 안 바뀐 캐릭의 자기 시각(리셋 전)은 스냅샷 merge 로 안 밀린다",
       by[1].get("collected_at") == pre and by[2].get("collected_at") == pre, str(by))

    # 재연결 재전송(_push_local_char_info: 로컬 json 에 merge=True 가 남아 있다) 뒤에도 보정 유지
    await main.receive_char_info(pid, Req({"total_kina": 5000, "merge": True, "collected_at": pre,
                                           "characters": snap}))
    rb2 = await _chars_rows(pid)
    ok("M1-d 재전송(같은 스냅샷 반복)은 아무 캐릭 시각도 안 올린다(슬롯1 3·슬롯2 시각 그대로)",
       _aw(rb2[1]) == 3 and rb2[2]["collected_at"] == rb[2]["collected_at"] and rb2[1]["collected_at"] == pre,
       "%s / %s" % ([rb2[1]["collected_at"], rb2[2]["collected_at"]], [rb[1]["collected_at"], rb[2]["collected_at"]]))


# ───────────────────────── M2 ─────────────────────────
async def _send_upd(pc, cmd="update"):
    sess = main.new_session("main")
    try:
        r = await main.dashboard_send_updater_command(pc, Req({"command": cmd}, api_key=None, session=sess))
        return r.status_code
    except HTTPException as e:
        return e.status_code


async def t_m2():
    await db.upsert_status("PC-M2b", {"pc_id": "PC-M2b", "status": "hunting"})
    main.RETIRED_PCS.add("PC-M2")
    try:
        code_sib = await _send_upd("PC-M2b")
        got = _body(await main.updater_poll_command("PC-M2", Req())).get("command")
        ok("M2-a 계정1 카드만 은퇴한 PC(형제 PC-M2b 현역)의 업데이터 명령은 들어가 물리 PC 가 받는다",
           code_sib == 200 and got == "update", "code=%s got=%s" % (code_sib, got))
        code_base = await _send_upd("PC-M2", "restart")
        # 형제까지 은퇴하면(살아 있는 카드 0장) 여전히 410 — 옛 CQ12-b 와 같은 뜻
        main.RETIRED_PCS.add("PC-M2b")
        code_all = await _send_upd("PC-M2b")
        await db.upsert_status("PC-M3x", {"pc_id": "PC-M3x", "status": "idle"})   # 딴 PC 카드는 안 센다
        main.RETIRED_PCS.add("PC-M3")
        code_none = await _send_upd("PC-M3")
        ok("M2-b base 로 보내도 현역 형제가 있으면 200 · 전부 은퇴/카드 없음이면 410",
           (code_base, code_all, code_none) == (200, 410, 410), str((code_base, code_all, code_none)))
    finally:
        for k in ("PC-M2", "PC-M2b", "PC-M3"):
            main.RETIRED_PCS.discard(k)


# ───────────────────────── M3 ─────────────────────────
def _setup_chat():
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


async def t_m3():
    _setup_chat()
    await db.tg_map_put(8300, "PC-M3A", CHAT)
    await _say("a!", reply=8300)                  # 틀린 모양 → 봇이 「다시 보내주세요」(매핑 없는 봇 메시지)
    await _say("m3abc1", reply=8_999_001)          # 그 봇 메시지에 답장한 정정 코드
    caps = await _caps()
    ok("M3-a 봇 안내문(매핑 없음)에 단 답장은 대기 PC 하나(PC-M3A)로",
       ("PC-M3A", "{'code': 'm3abc1'}") in caps or any(p == "PC-M3A" and "m3abc1" in a for p, a in caps),
       "caps=%s last=%r" % (caps[-3:], TG_SENT[-1][1] if TG_SENT else None))

    # TG1 원래 사례: 처리된 A 사진에 다시 답장 → 기다리는 B 로 가면 안 된다. 모르는 메시지 답장은 B 로.
    await db.tg_map_put(8310, "PC-M3C", CHAT)
    await db.tg_map_put(8311, "PC-M3D", CHAT)
    await _say("m3c001", reply=8310)
    row = await db.tg_map_get(8310)
    await _say("m3c002", reply=8310)               # 처리된 사진 재답장
    refused = "이미 처리됐거나 만료" in (TG_SENT[-1][1] if TG_SENT else "")
    await _say("m3d001", reply=8_999_002)          # expect_reply 없던 알림에 답장
    caps = await _caps()
    ok("M3-b 처리된 사진 재답장은 거절(행은 kind=done 으로 남음) · 모르는 메시지 답장은 유일 대기 PC-M3D 로",
       refused and row is not None and row.get("kind") == "done"
       and not any("m3c002" in a for p, a in caps)
       and any(p == "PC-M3D" and "m3d001" in a for p, a in caps),
       "refused=%s row=%s caps=%s" % (refused, row, caps[-4:]))

    # 은퇴: 그 PC 사진은 처리됨으로 묻혀 다른 대기 PC 로 안 간다(행이 사라져 「모르는 메시지」가 되면 안 된다)
    await db.tg_map_put(8320, "PC-M3E", CHAT)
    await db.tg_map_put(8321, "PC-M3F", CHAT)
    await db.delete_pc_all_data("PC-M3E", purge_all=True)
    rowe = await db.tg_map_get(8320)
    await _say("m3e001", reply=8320)
    caps = await _caps()
    ok("M3-c 은퇴한 PC 사진의 행은 kind=done 으로 남고, 그 답장은 다른 대기 PC(PC-M3F)로 안 간다",
       rowe is not None and rowe.get("kind") == "done" and not any("m3e001" in a for p, a in caps),
       "row=%s caps=%s" % (rowe, caps[-3:]))
    await db.tg_map_delete_pc("PC-M3F")

    # 봇 자기 메시지 — 「전달했습니다」 에 단 정정은 그 PC 것(다른 대기 PC 로 추론 금지),
    # 「다시 보내주세요」 에 단 답장은 대기 PC 가 여럿이어도 그 PC 로. 메시지 id 를 하나씩 매긴다.
    real_send = main.tg_send_text
    seq = [8_500_000]

    async def _send(chat, text, **kw):
        seq[0] += 1
        TG_SENT.append((chat, text))
        return seq[0]
    main.tg_send_text = _send
    try:
        await db.tg_map_put(8330, "PC-M3G", CHAT)
        await db.tg_map_put(8331, "PC-M3H", CHAT)
        await _say("m3g001", reply=8330)
        confirm_mid = seq[0]
        await _say("m3g002", reply=confirm_mid)        # 오타 정정 — G 에 대한 것
        caps = await _caps()
        crow = await db.tg_map_get(confirm_mid)
        ok("M3-d 「전달했습니다」 에 단 정정 답장은 다른 대기 PC(PC-M3H)로 안 간다(처리됨으로 적힘)",
           crow is not None and crow.get("kind") == "done" and not any("m3g002" in a for p, a in caps),
           "row=%s caps=%s" % (crow, caps[-3:]))
        await db.tg_map_put(8332, "PC-M3I", CHAT)       # 대기: H, I 두 대
        await _say("!!", reply=8331)                   # H 사진에 틀린 모양 → 봇 「다시 보내주세요」
        prompt_mid = seq[0]
        await _say("m3h001", reply=prompt_mid)
        caps = await _caps()
        ok("M3-e 「다시 보내주세요」 에 단 답장은 대기 PC 가 둘이어도 그 PC(PC-M3H)로",
           any(p == "PC-M3H" and "m3h001" in a for p, a in caps), "caps=%s last=%r" % (caps[-3:], TG_SENT[-1:]))
    finally:
        main.tg_send_text = real_send
        await db.tg_map_delete_pc("PC-M3I")


# ───────────────────────── M4 ─────────────────────────
async def t_m4():
    _setup_chat()
    await db.tg_map_put(8400, "PC-M4", CHAT)
    await db.upsert_status("PC-M4", {"pc_id": "PC-M4", "status": "captcha"})
    await db.delete_pc_all_data("PC-M4")          # 「멈춘 카드 청소」 — 같은 id 로 되살아난다
    await db.upsert_status("PC-M4", {"pc_id": "PC-M4", "status": "captcha"})
    waiting = [r["pc_id"] for r in await db.tg_map_recent(CHAT, 3600)]
    ok("M4-a 카드 삭제(청소) 뒤에도 캡차 요청은 대기 후보로 남는다", "PC-M4" in waiting, str(waiting))
    await _say("m4xyz9", reply=8400)
    caps = await _caps()
    ok("M4-b 되살아난 PC 의 캡차 사진에 단 답장이 그 PC 로 간다",
       any(p == "PC-M4" and "m4xyz9" in a for p, a in caps), "caps=%s last=%r" % (caps[-3:], TG_SENT[-1:]))


# ───────────────────────── M5 ─────────────────────────
H = {"X-FV-Token": "tokM5"}


async def _poll(since):
    return _body(await main.fv_events(Req(api_key=None, headers=H), since=since))


async def _race(tag, horizon_ago, ts_ago, macro_ago):
    """업데이터 줄의 insert_log 가 커밋되기 전에 FV 폴링이 끼어든다. 반환 (끼어든 폴링, 그 뒤 폴링, 저장 시각)."""
    main.FV_TOKEN = "tokM5"
    now = datetime.now(timezone.utc)
    main._FV_HORIZON_HI[0] = (now - timedelta(seconds=horizon_ago)).strftime(FMT)
    await db.insert_log("PC-M5M", "info", f"{tag}-macro", created_at=(now - timedelta(seconds=macro_ago)).strftime(FMT))
    real = main.insert_log
    got = {}

    async def slow_insert(key, level, message, created_at=None):
        if tag in message and "d" not in got:     # 진짜 insert_log 는 aiosqlite 스레드로 루프를 양보한다
            got["d"] = await _poll((now - timedelta(seconds=90)).strftime(FMT))
        return await real(key, level, message, created_at=created_at)
    main.insert_log = slow_insert
    try:
        await main.receive_updater_logs("PC-M5", Req({"logs": [
            {"level": "info", "message": f"{tag}-upd", "ts": now.timestamp() - ts_ago}]}))
    finally:
        main.insert_log = real
    first = got["d"]
    second = await _poll(first["next_since"])
    rows = [r for r in await db.get_logs("PC-M5.upd", limit=20) if tag in str(r.get("message"))]
    return first, second, (rows[0]["created_at"] if rows else None)


def _has(d, tag):
    return any(f"{tag}-upd" in str(e.get("message")) for e in d["events"])


async def t_m5():
    # 옛 위 끝으로 올려 찍히는 줄(B-FV3 갈래)
    f, s, stamped = await _race("M5A", horizon_ago=30, ts_ago=40, macro_ago=10)
    ok("M5-a 올려 찍힌 업데이터 줄이 폴링과 겹쳐도 결국 배달된다", _has(f, "M5A") or _has(s, "M5A"),
       "stamped=%s cursor=%s" % (stamped, f["next_since"]))
    ok("M5-b 끼어든 폴링의 커서는 커밋 중인 줄 시각을 넘지 않는다", stamped and f["next_since"] < stamped,
       "stamped=%s cursor=%s" % (stamped, f["next_since"]))
    # 안 올려 찍히는 줄(클라 시각 ≥ 위 끝) — 같은 틈
    f, s, stamped = await _race("M5B", horizon_ago=60, ts_ago=30, macro_ago=10)
    ok("M5-c 클라 시각 그대로 찍히는 업데이터 줄도 폴링과 겹쳐 유실되지 않는다", _has(f, "M5B") or _has(s, "M5B"),
       "stamped=%s cursor=%s" % (stamped, f["next_since"]))

    # insert_log 가 죽어도 「진행 중」 표식이 남아 FV 가 영영 멈추지 않는다
    real = main.insert_log

    async def boom(*a, **k):
        raise RuntimeError("disk")
    main.insert_log = boom
    try:
        try:
            await main.receive_updater_logs("PC-M5", Req({"logs": [{"level": "info", "message": "M5D-upd"}]}))
        except RuntimeError:
            pass
    finally:
        main.insert_log = real
    fl = getattr(main, "_FV_INFLIGHT", None)
    ok("M5-d 커밋 중 표식은 실패해도 풀린다(폴링 위 끝이 영구히 눌리지 않게)", fl == [], str(fl))


run_all([t_m1, t_m2, t_m3, t_m4, t_m5])
finish("test_merge_b2", MIN_CHECKS)
