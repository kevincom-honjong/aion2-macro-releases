# -*- coding: utf-8 -*-
"""v2 반증 1부 (2026-09-24, 아이온2 반증자) — 확인 4건 + 그럴듯 2건 + 살아남은 뮤턴트 M3.

  A  창고키나 «보낸 시각» — v2 는 카드별 마지막 어긋남 하나를 배워 merge·재전송에 썼다.
     (2)  NTP 로 고친 빠른 PC 의 merge 판독이 두 번 빠졌다(400, 정답 700)
     (2b) NTP 로 고친 느린 PC 의 merge 판독이 버려져 창고키나가 얼었다
     (1)  엉터리 보고 하나(+1일 / −1일, 창고키나 0)가 배운 값을 뒤집었다 → 두 번 빼거나 얼었다
     (5c) 재배포로 배운 값이 사라져 판매 전 판독의 재전송이 차감을 되살렸다(1000, 정답 700)
     → 요청마다 그 요청 안의 직접 증거(sent_at · collected_at · 새 판독 merge 의 kina_read_at) · 배운 값은 재전송에만,
       최근 3개가 서로 맞을 때만 · 표(pc_clock)에 남김 · 하루 넘는 어긋남은 증거가 아님(M3) · 창고키나 0 에선 안 배움.
     엄격한 total_kina(1.9·−0.5·전각·«1_000» 은 400) · 기억에서 밀려난 옛 판독(순번·시각 둘 다 옛것)은 옛것.
  B  업데이터에 내준 행(ack 전)을 다시 누른 새 행이 superseded 로 찍었다(돈 재시작이 «안 돎») → handed_at 은 안 덮는다.
  W  함대 가드를 요청마다만 셌다(7대+7대 통과) → 15분 굴러가는 창.
  (팜뷰 토큰 칸 = IP+토큰 은 test_kina_guard K-12e~g)

★«v2:» 를 적은 줄은 v2 판에서 실패한다★ — 겉 API 만 써서 v2 복사본에 그대로 돌려 확인했다(HANDOFF). 나머지 줄은
뮤턴트 막이(예: A-5d = 모든 표본의 가운데값을 쓰면 실패).

    cd updater/server && python -X utf8 tests/test_refute_v2p1.py
"""
import json
import time
from datetime import datetime, timedelta, timezone

import aiosqlite
from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 37
TOK = "fvsecret-rf1"
H = {"X-FV-Token": TOK}
C1 = [{"slot": 1, "name": "러닝"}]


def U(ds):
    return (datetime.now(timezone.utc) + timedelta(seconds=ds)).strftime("%Y-%m-%dT%H:%M:%S")


def _body(r):
    return json.loads(bytes(r.body))


async def _post(pc, body):
    """→ (상태코드, 저장된 total_kina)."""
    try:
        r = await main.receive_char_info(pc, Req(dict({"characters": C1}, **body)))
        code = r.status_code
    except main.HTTPException as e:
        code = e.status_code
    info = await db.get_char_info(main.ns("main", pc)) or {}
    return code, info.get("total_kina")


async def _sell(pc, delta, tid, ago_s):
    main.FV_TOKEN = TOK
    r = await main.fv_kina_adjust(Req({"pc_id": pc, "delta_kina": delta, "why": {"tid": tid}}, api_key=None, headers=H))
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE kina_adjust SET at=? WHERE tid=?", (U(-ago_s), tid))
        await c.commit()
    return r.status_code


async def _age_clock(k, sec):
    """표(pc_clock)의 표본을 sec 초 옛것으로 + 캐시 비움(v2 판엔 표가 없다 — 그땐 아무것도 안 한다)."""
    try:
        async with aiosqlite.connect(db.DB_PATH) as c:
            async with c.execute("SELECT samples FROM pc_clock WHERE pc_id=?", (k,)) as cur:
                r = await cur.fetchone()
            if r:
                await c.execute("UPDATE pc_clock SET samples=? WHERE pc_id=?",
                                (json.dumps([[t - sec, o] for t, o in json.loads(r[0])]), k))
                await c.commit()
    except aiosqlite.OperationalError:
        return
    _redeploy()


def _redeploy():
    """재배포 — 메모리는 다 비고 DB 만 남는다."""
    for name in ("_PC_CLOCK", "_PC_CLOCK_OFS"):
        if hasattr(main, name):
            getattr(main, name).clear()


# ── A ─────────────────────────────────────────────────────────────────────────
async def t_ntp_corrected():
    # (2) 10분 빠른 PC 가 전체보고로 −600 을 배움 → NTP 로 고쳐짐 → 판매(3분 전) 뒤 새 판독 700 을 merge 로
    pc = "PC-RN1"
    await _post(pc, {"total_kina": 1000, "kina_read_at": U(-3600 + 600), "kina_seq": 1, "collected_at": U(600)})
    await _sell(pc, -300, "rf-n1", 180)
    _, v = await _post(pc, {"total_kina": 700, "merge": True, "kina_read_at": U(-1), "kina_seq": 2,
                            "collected_at": U(-3600 + 600)})
    ok("A-2 ★NTP 로 고친 빠른 PC — 판매 뒤 merge 판독 700 을 두 번 안 뺀다(v2: 400)★", v == 700, str(v))
    # (2b) 10분 느린 PC 가 +600 을 배움 → 고쳐짐 → merge 새 판독 셋이 다 들어간다
    pc = "PC-RN2"
    await _post(pc, {"total_kina": 1000, "kina_read_at": U(-3600 - 600), "kina_seq": 1, "collected_at": U(-600)})
    outs = [(await _post(pc, {"total_kina": 900 - i, "merge": True, "kina_read_at": U(-1), "kina_seq": 2 + i}))[1]
            for i in range(3)]
    ok("A-2b ★NTP 로 고친 느린 PC — merge 새 판독이 얼지 않는다(900·899·898)★", outs == [900, 899, 898], str(outs))


async def t_one_bad_report():
    # (1) +1일 엉터리 보고 하나(창고키나 못 읽음 0) 뒤, 판매(1시간 전) 뒤 진짜 새 판독
    pc = "PC-RP1"
    await _post(pc, {"total_kina": 1000, "kina_read_at": U(-7200), "kina_seq": 1, "collected_at": U(-7200 + 5)})
    await _post(pc, {"total_kina": 0, "collected_at": U(86400)})
    await _sell(pc, -300, "rf-p1", 3600)
    _, v = await _post(pc, {"total_kina": 700, "merge": True, "kina_read_at": U(-1), "kina_seq": 5})
    ok("A-1 ★+1일 엉터리 보고 하나가 판매 뒤 새 판독을 두 번 빼게 하지 않는다(700)★", v == 700, str(v))
    # (1b) −1일 엉터리 보고 하나 뒤 새 merge 판독 · (1c) 그 판독의 재전송
    pc = "PC-RP2"
    await _post(pc, {"total_kina": 1000, "kina_read_at": U(-7200), "kina_seq": 1, "collected_at": U(-7200 + 5)})
    await _post(pc, {"total_kina": 0, "collected_at": U(-86400)})
    _, v = await _post(pc, {"total_kina": 1234, "merge": True, "kina_read_at": U(-1), "kina_seq": 5})
    ok("A-1b ★−1일 엉터리 보고 하나가 새 판독을 버리게 하지 않는다(1234)★", v == 1234, str(v))
    pc = "PC-RP2c"
    await _post(pc, {"total_kina": 1000, "kina_read_at": U(-7200), "kina_seq": 1, "collected_at": U(-7200 + 5)})
    await _post(pc, {"total_kina": 0, "collected_at": U(-86400)})
    _, v = await _post(pc, {"total_kina": 1234, "resend": True, "kina_read_at": U(-5), "kina_seq": 5,
                            "collected_at": U(-4000)})
    ok("A-1c ★…그 뒤 재전송된 새 판독도 버리지 않는다(1234)★", v == 1234, str(v))
    # (1d) 마지막 판독 2일 전 + +1일 엉터리 보고 → 판매 뒤 새 판독
    pc = "PC-RP3"
    await _post(pc, {"total_kina": 1000, "kina_read_at": U(-2 * 86400), "kina_seq": 1, "collected_at": U(0)})
    await _post(pc, {"total_kina": 0, "collected_at": U(86400)})
    await _sell(pc, -300, "rf-p3", 3600)
    _, v = await _post(pc, {"total_kina": 700, "merge": True, "kina_read_at": U(-5), "kina_seq": 5})
    ok("A-1d ★옛 판독 + 엉터리 보고 뒤에도 판매 뒤 새 판독은 700★", v == 700, str(v))


async def t_after_redeploy():
    # (5c) 10분 빠른 PC — 새 전송 셋으로 −600 을 배움 → 판매(2분 전) → 재배포 → 판매 전(5분 전) 판독이 재전송으로
    pc = "PC-RR1"
    for _ in range(3):
        await _post(pc, {"total_kina": 1000, "kina_read_at": U(-3600 + 600), "kina_seq": 1, "collected_at": U(600)})
    await _sell(pc, -300, "rf-r1", 120)
    _redeploy()
    _, v = await _post(pc, {"total_kina": 1000, "resend": True, "kina_read_at": U(-300 + 600), "kina_seq": 2,
                            "collected_at": U(-3600 + 600)})
    ok("A-5c ★재배포 뒤에도 배운 어긋남이 남아 판매 전 판독의 재전송이 차감을 안 되살린다(700, v2: 1000)★", v == 700, str(v))
    # 시계가 고쳐진 직후(마지막 3개가 서로 안 맞음) — 배운 값을 안 쓰고 PC 시계로: 판매 뒤 진짜 판독의 재전송이 들어간다
    pc = "PC-RR2"
    for _ in range(3):
        await _post(pc, {"total_kina": 1000, "kina_read_at": U(-7200 + 600), "kina_seq": 1, "collected_at": U(600)})
    await _post(pc, {"total_kina": 1000, "kina_read_at": U(-7200 + 600), "kina_seq": 1, "collected_at": U(0)})
    await _sell(pc, -300, "rf-r2", 300)
    _, v = await _post(pc, {"total_kina": 700, "resend": True, "kina_read_at": U(-60), "kina_seq": 2,
                            "collected_at": U(-7200)})
    ok("A-5d ★시계가 막 고쳐지면(마지막 3개 불일치) 옛 어긋남을 안 쓴다 — 판매 뒤 판독 재전송 700(두 번 안 뺀다)★",
       v == 700, str(v))
    # 배운 값이 2시간 넘게 낡았으면 안 쓴다 — 그 사이 NTP 로 고쳐졌을 수 있다(판매 뒤 판독 재전송을 두 번 빼지 않게)
    pc = "PC-RR4"
    for _ in range(3):
        await _post(pc, {"total_kina": 1000, "kina_read_at": U(-7200 + 600), "kina_seq": 1, "collected_at": U(600)})
    await _age_clock(main.ns("main", pc), 3 * 3600)
    await _sell(pc, -300, "rf-r4", 300)
    _, v = await _post(pc, {"total_kina": 700, "resend": True, "kina_read_at": U(-60), "kina_seq": 2,
                            "collected_at": U(-7200)})
    ok("A-5f ★3시간 낡은 어긋남은 안 쓴다 — 판매 뒤 판독 재전송 700(두 번 안 뺀다)★", v == 700, str(v))
    # 시계가 한 번 더 뛰어(+600 → +1200) 새 표본 셋이 맞으면 옛 표본과 안 맞아도 새 값을 쓴다(마지막 3개만 본다)
    pc = "PC-RR5"
    for sk in (600, 600, 600, 1200, 1200, 1200):
        await _post(pc, {"total_kina": 1000, "kina_read_at": U(-7200 + sk), "kina_seq": 1, "collected_at": U(sk)})
    await _sell(pc, -300, "rf-r5", 120)
    _, v = await _post(pc, {"total_kina": 800, "resend": True, "kina_read_at": U(-60 + 1200), "kina_seq": 2,
                            "collected_at": U(-7200 + 1200)})
    ok("A-5g ★마지막 3개가 맞으면 그 값 — 20분 빠른 PC 의 판매 뒤 판독 재전송 800 이 «미래» 로 안 버려진다★", v == 800, str(v))
    # 배운 어긋남 때문에 «보낸 시각보다 뒤» 로 버려질 판정이면 PC 시계로 다시 본다(배운 값은 버리는 근거가 못 된다)
    pc = "PC-RR3"
    for _ in range(3):
        await _post(pc, {"total_kina": 1000, "kina_read_at": U(-660), "kina_seq": 1, "collected_at": U(-600)})
    _, v = await _post(pc, {"total_kina": 1100, "resend": True, "kina_read_at": U(-5), "kina_seq": 2,
                            "collected_at": U(-600)})
    ok("A-5e ★10분 느리다 배운 PC 가 막 고쳐진 뒤 첫 재전송 — 배운 값으로 버리지 않고 PC 시계로 받는다(1100)★",
       v == 1100, str(v))


async def t_bound_and_zero():
    # M3 — 하루 넘게 어긋난 sent_at 은 증거가 아니다: 판정에 쓰면 새 판독이 «보낸 시각보다 한참 뒤» 로 버려진다
    pc = "PC-RB1"
    await _post(pc, {"total_kina": 1000, "kina_read_at": U(-600), "kina_seq": 1, "collected_at": U(-590)})
    _, v = await _post(pc, {"total_kina": 1500, "kina_read_at": U(-1), "kina_seq": 2, "sent_at": U(-2 * 86400)})
    ok("A-M3 ★2일 어긋난 sent_at 은 버리고 PC 시계로 — 새 판독이 들어간다(1500)★", v == 1500, str(v))
    _, v = await _post(pc, {"total_kina": 1600, "kina_read_at": U(-1), "kina_seq": 3, "collected_at": U(2 * 86400)})
    ok("A-M3b 2일 앞선 collected_at 도 증거가 아니다 — 새 판독이 판매 전으로 밀리지 않는다(1600)", v == 1600, str(v))
    # 창고키나 0(못 읽음) 보고로는 배우지 않는다 — 셋이 +1시간이어도 재전송 판정에 안 쓴다
    pc = "PC-RB2"
    await _post(pc, {"total_kina": 1000, "kina_read_at": U(-900), "kina_seq": 1, "collected_at": U(0)})
    for _ in range(3):
        await _post(pc, {"total_kina": 0, "collected_at": U(3600)})      # 1시간 앞선 시각 셋(창고키나 못 읽음)
    await _sell(pc, -300, "rf-b2", 300)
    _, v = await _post(pc, {"total_kina": 750, "resend": True, "kina_read_at": U(-60), "kina_seq": 2,
                            "collected_at": U(-890)})
    ok("A-Z ★창고키나 0 보고 셋으로 어긋남을 안 배운다 — 판매 뒤 새 판독 재전송 750 이 들어간다(v2: 700 — 판매 전으로 밀려 버려짐)★",
       v == 750, str(v))


async def t_evicted_old_read():
    # (3c) 새 판독 60개로 기억(seen 50)에서 밀려난 판매 전 판독의 재전송 · (3d) 옛 전체보고가 그대로 다시 옴
    pc = "PC-RE1"
    await _post(pc, {"total_kina": 1000, "kina_read_at": U(-900), "kina_seq": 1, "collected_at": U(-900)})
    await _sell(pc, -100, "rf-e1", 600)
    for i in range(60):
        await _post(pc, {"total_kina": 900, "merge": True, "kina_read_at": U(-590 + i * 4), "kina_seq": 2 + i})
    _, v = await _post(pc, {"total_kina": 1000, "resend": True, "kina_read_at": U(-900), "kina_seq": 1,
                            "collected_at": U(-900)})
    ok("A-3c ★기억에서 밀려난 판매 전 판독의 재전송 → 900(순번·시각 둘 다 옛것)★", v == 900, str(v))
    _, v = await _post(pc, {"total_kina": 1000, "kina_read_at": U(-900), "kina_seq": 1, "collected_at": U(-900)})
    ok("A-3d ★옛 전체보고가 그대로 다시 와도 900★", v == 900, str(v))
    # 마지막 판독을 받은 지 30분 — 기억에서 밀려난 옛 전체보고가 그대로(collected_at 도 옛것) 다시 오면 «방금 보냄» 으로
    #   보여 서버 시각이 마지막 판독보다 새것이 된다 → 순번·판독 시각 둘 다 옛것이면 옛것
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE kina_read SET read_srv=? WHERE pc_id=?", (U(-1800), main.ns("main", pc)))
        await c.commit()
    _, v = await _post(pc, {"total_kina": 1000, "kina_read_at": U(-900), "kina_seq": 1, "collected_at": U(-900)})
    ok("A-3e ★30분 뒤 그대로 다시 온 밀려난 옛 전체보고 → 900(v2: 1000)★", v == 900, str(v))
    # (반증 에이전트 #6) 30분 빠른 PC(순번 500) → 매크로 폴더를 다시 깔아 순번 1 + NTP 로 시계가 뒤로 고쳐짐 →
    #   새 판독이 «순번·판독 시각 둘 다 옛것» 으로 전부 버려졌다. 시계 표본 셋이 새 시계로 맞으면 들어간다.
    pc = "PC-RE2"
    for i in range(3):
        await _post(pc, {"total_kina": 1000, "kina_read_at": U(1800 - 600), "kina_seq": 500 + i, "collected_at": U(1800)})
    outs = [(await _post(pc, {"total_kina": 1500 + i * 100, "kina_read_at": U(-5), "kina_seq": 1 + i,
                              "collected_at": U(0)}))[1] for i in range(3)]
    ok("A-3f ★순번이 1 로 돌아가고 시계가 뒤로 고쳐져도 새 전송 셋째부터는 새 판독이 들어간다(…→1700)★",
       outs[-1] == 1700, str(outs))


async def t_strict_parser():
    pc = "PC-RK1"
    await _post(pc, {"total_kina": 1000})
    bad = []
    for val in (1.9, -0.5, "１２３", "1_000", "1,23", "12,3456", "0x10", " +5", "1e3", float("nan"), float("inf"), [1], {"a": 1}):
        c, _ = await _post(pc, {"total_kina": val})
        if c != 400:
            bad.append((val, c))
    ok("A-6 ★1.9·−0.5·전각·«1_000»·어긋난 쉼표·16진·부호·지수·NaN·목록 → 400★", bad == [], str(bad))
    good = []
    for val, want in ((5.0, 5), (" 1,234 ", 1234), ("1234567", 1234567), (1_000_000, 1_000_000)):
        c, v = await _post(pc, {"total_kina": val})
        if (c, v) != (200, want):
            good.append((val, c, v))
    ok("A-6b 정수·소수부 0 실수·세 자리 쉼표 숫자는 받는다", good == [], str(good))
    c, _ = await _post(pc, {"total_kina": -1})
    ok("A-6c 음수 정수는 여전히 400(범위)", c == 400, str(c))


# ── B ─────────────────────────────────────────────────────────────────────────
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


async def t_inflight():
    # 업데이터가 #1 을 받아 도는 중(ack 가 사라짐) → 같은 종류 #2 를 누름 → 다음 폴링
    _reset_q()
    pc = "PC-RIF"
    a = await db.insert_updater_command(main.ns("main", pc), "restart", {})
    r1 = _body(await main.updater_poll_command(pc, Req()))
    b = await db.insert_updater_command(main.ns("main", pc), "restart", {})
    await main.updater_poll_command(pc, Req())
    ok("B-1 ★내준 #1 은 다시 누른 #2 때문에 superseded 로 안 찍힌다(v2: superseded — 돈 명령이 «안 돎»)★",
       r1.get("id") == a and await _st(a) == "pending", "%s %s" % (r1, await _st(a)))
    await main.updater_ack_command(pc, a, Req())
    ok("B-1b 늦은 ack 가 #1 을 acked 로 적는다", await _st(a) == "acked", str(await _st(a)))
    r3 = _body(await main.updater_poll_command(pc, Req()))
    ok("B-1c #2 는 사라지지 않고 다음 폴링에 차례로", r3.get("id") == b, str(r3))
    # 재배포로 메모리 소유가 비어도 — 팜뷰가 #2 를 집으며 내준 #1 을 superseded 로 안 찍는다, #2 는 90초 안이라 팜뷰에 안 준다
    _reset_q()
    pc = "PC-RIF2"
    a = _body(await main.dashboard_send_updater_command(pc, Req({"command": "restart"}, api_key=None,
                                                                 session=main.new_session("main"))))["id"]
    main.FV_UPDCMD_QUEUE.clear()
    await main.updater_poll_command(pc, Req())
    main._UPDCMD_UP_PC.clear(); main._UPDCMD_OWNER.clear()          # 재배포
    b = _body(await main.dashboard_send_updater_command(pc, Req({"command": "restart"}, api_key=None,
                                                                 session=main.new_session("main"))))["id"]
    fv = _body(await main.fv_updcmd_list(Req(api_key=None, headers=H), since="0"))["cmds"]
    ok("B-2 ★재배포 뒤에도 업데이터가 도는 #1 을 팜뷰 집기가 superseded 로 안 찍는다(DB handed_at)★",
       await _st(a) == "pending", str(await _st(a)))
    ok("B-2b 90초 안에 다시 누른 #2 는 팜뷰에 안 준다(두 길이 겹쳐 안 돈다) — 대기로 남는다",
       fv == [] and await _st(b) == "pending", "%s %s" % (fv, await _st(b)))
    # 90초가 지나도(업데이터가 멈췄을 수 있어 팜뷰가 #2 를 받는 길) 내준 #1 은 superseded 로 안 찍힌다
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE updater_commands SET handed_at=? WHERE id=?", (U(-(db.UPDATER_BUSY_SEC + 10)), a))
        await c.commit()
    fv = _body(await main.fv_updcmd_list(Req(api_key=None, headers=H), since="0"))["cmds"]
    ok("B-4 ★90초 뒤 팜뷰가 #2 를 집어도 업데이터에 내준 #1 은 superseded 가 아니다(늦은 ack 가 적을 자리)★",
       [x["act"] for x in fv] == ["restart"] and await _st(b) == "fv_claimed" and await _st(a) == "pending",
       "%s a=%s b=%s" % (fv, await _st(a), await _st(b)))
    await main.updater_poll_command(pc, Req())      # 업데이터 다음 폴링 — 팜뷰가 더 새 것을 집었으니 #1 은 다시 안 준다
    await main.updater_ack_command(pc, a, Req())    # 그리고 사라졌던 #1 의 ack 가 늦게 도착
    ok("B-4b ★늦은 ack 는 (그 사이 치워진) 내준 #1 을 acked 로 적는다 — 돈 명령이 «안 돎» 으로 안 남는다★",
       await _st(a) == "acked", str(await _st(a)))
    # 겹친 두 인스턴스(재배포) — 한쪽이 업데이터에 준 #1 이 다른 쪽 팜뷰 큐에 아직 있다 → 팜뷰가 집으면 두 번 돈다
    _reset_q()
    pc = "PC-RIF3"
    a = _body(await main.dashboard_send_updater_command(pc, Req({"command": "update"}, api_key=None,
                                                                 session=main.new_session("main"))))["id"]
    snap = {k: dict(v) for k, v in main.FV_UPDCMD_QUEUE.items()}
    r1 = _body(await main.updater_poll_command(pc, Req()))
    main.FV_UPDCMD_QUEUE.clear(); main.FV_UPDCMD_QUEUE.update(snap)       # 다른 인스턴스의 메모리
    main._UPDCMD_OWNER.clear(); main._UPDCMD_UP_PC.clear()
    fv = _body(await main.fv_updcmd_list(Req(api_key=None, headers=H), since="0"))["cmds"]
    ok("B-3 ★업데이터에 내준 행은 (소유 표가 안 보이는 인스턴스의) 팜뷰가 집지 않는다 — 두 번 안 돈다★",
       r1.get("id") == a and fv == [] and await _st(a) == "pending", "%s %s %s" % (r1, fv, await _st(a)))


# ── W ─────────────────────────────────────────────────────────────────────────
async def t_fleet_window():
    main.FV_TOKEN = TOK
    main.FV_TENANT = "main"
    for _n in ("_FV_FLEET_SEEN", "_FV_FLEET_OK"):
        getattr(main, _n).clear() if hasattr(main, _n) else None
    pcs = ["PC-RW%02d" % i for i in range(14)]
    for p in pcs:
        await db.upsert_status(p, {"pc_id": p, "status": "idle"})
    sent = []
    od = main._dispatch_macro_command

    async def disp(t, p, c, a):
        sent.append(p)
        return {"ok": True}
    main._dispatch_macro_command = disp
    try:
        r1 = await main.fv_command_send(Req({"cmd": "start", "pc": pcs[:7]}, api_key=None, headers=H))
        r2 = await main.fv_command_send(Req({"cmd": "start", "pc": pcs[7:]}, api_key=None, headers=H))
        n_after_2 = len(sent)
        r3 = await main.fv_command_send(Req({"cmd": "start", "pc": pcs[:7]}, api_key=None, headers=H))
        r4 = await main.fv_command_send(Req({"cmd": "start", "pc": pcs[7:], "confirm_fleet": True}, api_key=None, headers=H))
        # (반증 에이전트 #3) 함대로 확인해 보낸 PC 에 뒤이어 한 대씩 보내는 것은 막지 않는다
        r6 = await main.fv_command_send(Req({"cmd": "start", "pc": pcs[10]}, api_key=None, headers=H))
        # 정지 쪽은 세지도 막지도 않는다
        await db.upsert_status("PC-RW99", {"pc_id": "PC-RW99", "status": "idle"})
        r7 = await main.fv_command_send(Req({"cmd": "stop", "pc": "PC-RW99"}, api_key=None, headers=H))
        # 한 대씩 새 PC 를 눌러 8대째가 되면 거부(창의 목적 그대로 — 팜뷰는 400 이면 확인을 받아 confirm_fleet 로 다시)
        await db.upsert_status("PC-RW98", {"pc_id": "PC-RW98", "status": "idle"})
        r8 = await main.fv_command_send(Req({"cmd": "start", "pc": "PC-RW98"}, api_key=None, headers=H))
    finally:
        main._dispatch_macro_command = od
    ok("W-1 7대는 통과", r1.status_code == 200 and len(sent) >= 7, str(r1.status_code))
    ok("W-2 ★15분 안에 다른 7대 — 합 14대는 함대 규모, confirm_fleet 없이 400(v2: 통과)★",
       r2.status_code == 400 and n_after_2 == 7, "%s %s" % (r2.status_code, n_after_2))
    ok("W-3 같은 7대를 다시 — 합이 7대라 통과(거부된 요청은 창에 안 센다)", r3.status_code == 200, str(r3.status_code))
    ok("W-4 confirm_fleet:true 면 보낸다", r4.status_code == 200 and len(sent) >= 21, "%s %s" % (r4.status_code, len(sent)))
    ok("W-6 ★함대로 확인해 보낸 PC 에 뒤이은 한 대 명령은 통과(확인한 PC 는 창에 안 센다)★", r6.status_code == 200,
       str(r6.status_code))
    ok("W-7 ★정지는 창과 무관하게 통과★", r7.status_code == 200, str(r7.status_code))
    ok("W-8 창에 7대(확인 없이) + 확인 안 된 새 PC 한 대 = 8대 → 400", r8.status_code == 400, str(r8.status_code))
    if hasattr(main, "_FV_FLEET_SEEN"):
        for p in pcs:
            main._FV_FLEET_SEEN[p] = time.monotonic() - main.FV_FLEET_WINDOW_S - 1
        main._FV_FLEET_OK.clear()
        main._dispatch_macro_command = disp
        try:
            r5 = await main.fv_command_send(Req({"cmd": "start", "pc": pcs[7:]}, api_key=None, headers=H))
        finally:
            main._dispatch_macro_command = od
        ok("W-5 창(15분)이 지나면 다시 7대는 통과", r5.status_code == 200, str(r5.status_code))
    else:
        ok("W-5 창(15분)이 지나면 다시 7대는 통과", False, "창 없음")


def test_all():
    run_all([t_ntp_corrected, t_one_bad_report, t_after_redeploy, t_bound_and_zero, t_evicted_old_read,
             t_strict_parser, t_inflight, t_fleet_window])
    finish("test_refute_v2p1", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
