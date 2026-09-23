# -*- coding: utf-8 -*-
"""#128 팜뷰 알람 목소리 — /telegram/send·/telegram/photo 가 ★실제로 내보내는★ 알람을 그 PC 로그에 «[알람] …» 한 줄로
즉시 남기고, 팜뷰 /api/fv/events 가 그 줄을 준다(계약: FV_API «[알람] 이벤트»). 예전엔 매크로 info 줄이 하트비트(30초)에
실려 ~35초 늦었다.

    cd updater/server && python -X utf8 tests/test_alarm_event.py
"""
import json
import os
import re
import sys
import time
import types

from _harness import main, db, ok, Req, run_all, finish, TG_SENT   # noqa: E402

MIN_CHECKS = 22    # 2026-09-24 #124 알람 지연 실측 +11(A-7~A-9)
TOK = "fvsecret-alarm"
H = {"X-FV-Token": TOK}


class _UF:
    def __init__(self, data):
        self.filename, self._d = "c.png", data

    async def read(self, n=-1):
        return self._d if n < 0 else self._d[:n]


class _FormReq(Req):
    def __init__(self, caption):
        super().__init__({})
        self._cap = caption

    async def form(self):
        return {"caption": self._cap, "expect_reply": "0"}


_TIMING = re.compile(r" \(⏱ [^()]*\)$")


async def _alarms_raw(pc):
    return [x["message"] for x in await db.get_logs(pc, limit=50) if (x.get("message") or "").startswith("[알람]")]


async def _alarms(pc):
    """★#124★ 끝의 « (⏱ …)» 지연 꼬리를 뗀 본문 — 꼬리 자체는 A-7~A-9 가 따로 본다."""
    return [_TIMING.sub("", m) for m in await _alarms_raw(pc)]


async def _fv_alarm_events(pc):
    main.FV_TOKEN = TOK
    main.FV_TENANT = "main"
    old = main.FV_EVENT_SETTLE_S
    main.FV_EVENT_SETTLE_S = -2             # 시험에선 «정착 2초» 를 안 기다린다(같은 초 줄도 이번 판에)
    try:
        b = json.loads(bytes((await main.fv_events(Req(api_key=None, headers=H), since="2020-01-01T00:00:00Z",
                                                   limit="500")).body))
    finally:
        main.FV_EVENT_SETTLE_S = old
    return [e for e in b.get("events", []) if e.get("type") == "log" and e.get("pc") == pc
            and str(e.get("message") or "").startswith(main.ALARM_EVENT_PREFIX)]


async def t_text_alarm():
    main.TELEGRAM_BOT_TOKEN = "T"
    main.TENANTS.setdefault("main", {})["chat_id"] = "12345"
    main._TG_MUTE.clear()
    pc = "PC-A1"
    n0 = len(TG_SENT)
    r = await main.telegram_send(pc, Req({"text": "🚨 캡차 — 답해 주세요"}))
    b = json.loads(bytes(r.body))
    al = await _alarms(pc)
    ok("A-1 ★보낸 알람은 그 PC 로그에 «[알람] PC | 본문» 한 줄★", b.get("ok") is True and al == ["[알람] PC-A1 | 🚨 캡차 — 답해 주세요"],
       "%s %s" % (b, al))
    ok("A-1b 텔레그램으로도 그대로 나간다", len(TG_SENT) == n0 + 1 and TG_SENT[-1][1] == "PC-A1 | 🚨 캡차 — 답해 주세요",
       str(TG_SENT[-1:]))
    ev = await _fv_alarm_events(pc)
    ok("A-2 ★팜뷰 /api/fv/events 가 그 줄을 type:log 로 준다(계약)★",
       len(ev) == 1 and _TIMING.sub("", ev[0]["message"]) == "[알람] PC-A1 | 🚨 캡차 — 답해 주세요"
       and ev[0]["level"] == "info", str(ev))
    # 음소거 — 보통 알림은 안 나가고 [알람] 도 안 남는다 / ⛔·🚨 는 뚫고 나가며 [알람] 도 남는다
    pc = "PC-A2"
    main._TG_MUTE[main.ns("main", pc)] = time.time() + 3600
    await main.telegram_send(pc, Req({"text": "그냥 알림"}))
    ok("A-3 음소거로 생략한 알림은 [알람] 을 남기지 않는다(안 나간 알람을 말하지 않게)", await _alarms(pc) == [],
       str(await _alarms(pc)))
    await main.telegram_send(pc, Req({"text": "⛔ 정지했습니다"}))
    ok("A-3b 음소거를 뚫는 ⛔ 는 [알람] 을 남긴다", await _alarms(pc) == ["[알람] PC-A2 | ⛔ 정지했습니다"], str(await _alarms(pc)))
    main._TG_MUTE.clear()
    # 텔레그램 전송이 실패해도 목소리는 나간다(전송 전에 적는다)
    pc = "PC-A3"
    real = main.tg_send_text

    async def _fail(*a, **k):
        return None
    main.tg_send_text = _fail
    try:
        r = await main.telegram_send(pc, Req({"text": "🚨 사망"}))
    finally:
        main.tg_send_text = real
    ok("A-4 ★텔레그램이 실패(502)해도 [알람] 은 남되 «(텔레그램 실패)» 표시(v2 반증 2부)★",
       r.status_code == 502 and await _alarms(pc) == ["[알람] PC-A3 | 🚨 사망 (텔레그램 실패)"],
       "%s %s" % (r.status_code, await _alarms(pc)))
    # 줄바꿈은 빈칸으로 — 로그 한 줄·팜뷰 요약이 줄 단위
    pc = "PC-A3b"
    await main.telegram_send(pc, Req({"text": "🚨 캡차\r\n코드를\n답해 주세요\r"}))
    ok("A-4b ★본문의 \r\n 은 빈칸으로(한 줄)★", await _alarms(pc) == ["[알람] PC-A3b | 🚨 캡차 코드를 답해 주세요"],
       str(await _alarms(pc)))
    # 긴 본문은 300자로 자른다
    pc = "PC-A4"
    await main.telegram_send(pc, Req({"text": "가" * 900}))
    al = await _alarms(pc)
    ok("A-5 로그 본문은 300자까지", len(al) == 1 and al[0] == "[알람] PC-A4 | " + "가" * 300, str(len(al[0]) if al else 0))
    main.TELEGRAM_BOT_TOKEN = ""


async def t_photo_alarm():
    _en, _ch, _sp = main.tg_enabled, main.tenant_chat_id, main.tg_send_photo

    async def _send_photo(*a, **k):
        return 88
    main.tg_enabled, main.tenant_chat_id, main.tg_send_photo = (lambda: True), (lambda t: "1"), _send_photo
    try:
        r = await main.telegram_photo("PC-A5", _FormReq("캡차 화면"), _UF(b"\x89PNG" + b"\0" * 10))
    finally:
        main.tg_enabled, main.tenant_chat_id, main.tg_send_photo = _en, _ch, _sp
    ok("A-6 사진 알람도 «[알람] PC | 캡션 (사진)»", json.loads(bytes(r.body)).get("ok") is True
       and await _alarms("PC-A5") == ["[알람] PC-A5 | 캡차 화면 (사진)"], str(await _alarms("PC-A5")))
    ev = await _fv_alarm_events("PC-A5")
    ok("A-6b 사진 알람도 팜뷰 events 로 나온다", len(ev) == 1, str(ev))

    async def _fail_photo(*a, **k):
        return None
    main.tg_enabled, main.tenant_chat_id, main.tg_send_photo = (lambda: True), (lambda t: "1"), _fail_photo
    try:
        r = await main.telegram_photo("PC-A6", _FormReq("캡차"), _UF(b"\x89PNG" + b"\0" * 10))
    finally:
        main.tg_enabled, main.tenant_chat_id, main.tg_send_photo = _en, _ch, _sp
    ok("A-6c 사진 전송 실패도 «(텔레그램 실패)» 표시", r.status_code == 502
       and await _alarms("PC-A6") == ["[알람] PC-A6 | 캡차 (사진) (텔레그램 실패)"], str(await _alarms("PC-A6")))


class _FakeResp:
    status_code = 200
    text = ""

    def __init__(self, js):
        self._js = js

    def json(self):
        return self._js


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, files=None):
        return _FakeResp({"ok": True, "result": {"message_id": 7, "date": 1790200000}})


def _farmview_alarmvoice():
    here = os.path.dirname(os.path.abspath(__file__))
    fv = os.path.normpath(os.path.join(here, "..", "..", "..", "farmview"))
    if fv not in sys.path:
        sys.path.insert(0, fv)
    import alarmvoice                   # noqa: E402 — 팜뷰 실물(src/farmview)
    return alarmvoice


async def t_alarm_timing():
    """★#124 (2026-09-24 아이온2 결정)★ [알람] 줄 끝 « (⏱ 발생→수신 X초 · 수신→전송 Y초 · 텔레그램 HH:MM:SS)»."""
    main.TELEGRAM_BOT_TOKEN = "T"
    main.TENANTS.setdefault("main", {})["chat_id"] = "12345"
    main._TG_MUTE.clear()
    pc = "PC-A7"
    await main.telegram_send(pc, Req({"text": "🚨 캡차 — 답해 주세요", "t0": time.time() - 2.5}))
    raw = await _alarms_raw(pc)
    m = re.fullmatch(r"\[알람\] PC-A7 \| 🚨 캡차 — 답해 주세요 \(⏱ 발생→수신 (\d+\.\d\d)초 · 수신→전송 (\d+\.\d\d)초\)",
                     raw[0] if raw else "")
    ok("A-7 ★본문 t0 가 오면 «발생→수신 X초 · 수신→전송 Y초» 꼬리(텔레그램 date 없음 = 가짜 전송)★",
       len(raw) == 1 and m is not None and 2.4 <= float(m.group(1)) < 4.0 and float(m.group(2)) < 2.0, str(raw))
    pc = "PC-A7b"
    await main.telegram_send(pc, Req({"text": "🚨 사망"}))
    raw = await _alarms_raw(pc)
    ok("A-7b t0 가 없으면 «발생→수신» 은 빼고 «수신→전송» 만",
       len(raw) == 1 and re.fullmatch(r"\[알람\] PC-A7b \| 🚨 사망 \(⏱ 수신→전송 \d+\.\d\d초\)", raw[0]) is not None, str(raw))
    bad_ok = []
    for i, bad in enumerate(("abc", float("nan"), time.time() - 3 * 86400, True, [1], {"x": 1})):
        pc = "PC-A7c%d" % i
        try:
            await main.telegram_send(pc, Req({"text": "🚨 캡차", "t0": bad}))
            raw = await _alarms_raw(pc)
            good = len(raw) == 1 and "발생→수신" not in raw[0] and "수신→전송" in raw[0]
        except Exception as e:
            good, raw = False, "exc:%s" % type(e).__name__
        if i < 3:
            ok("A-7c t0 가 이상하면(%r) 빼고 알람은 그대로" % (bad,), good, str(raw))
        else:
            bad_ok.append((good, raw))
    ok("A-7d t0 가 bool·목록·객체여도 500 없이 뺀다", all(g for g, _ in bad_ok), str(bad_ok))
    rs = await main.telegram_send("PC-A7e", Req({"text": "🚨 캡차", "t0": "%.3f" % (time.time() - 1.0)}))
    raw = await _alarms_raw("PC-A7e")
    ok("A-7e t0 가 숫자 글자여도 읽는다", json.loads(bytes(rs.body)).get("ok") is True and raw and "발생→수신 1." in raw[0], str(raw))
    # ★실제 _tg_call 길★ — 가짜 httpx 로 텔레그램 응답 date 가 부른 과제에만 돌아오는지(하네스가 가짜로 바꾸지 않은
    #   tg_send_photo 는 진짜 → _tg_call 까지 그대로 탄다)
    real_httpx = sys.modules.get("httpx")
    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=_FakeClient)
    _en0, _ch0 = main.tg_enabled, main.tenant_chat_id

    class _FormT8(_FormReq):
        async def form(self):
            return {"caption": self._cap, "expect_reply": "0", "t0": str(time.time() - 1.0)}
    main.tenant_chat_id = lambda t: "1"
    try:
        r8 = await main.telegram_photo("PC-A8", _FormT8("캡차"), _UF(b"\x89PNG" + b"\0" * 10))
    finally:
        main.tenant_chat_id = _ch0
        if real_httpx is not None:
            sys.modules["httpx"] = real_httpx
        else:
            sys.modules.pop("httpx", None)
    raw = await _alarms_raw("PC-A8")
    from datetime import datetime as _dt
    hhmmss = _dt.fromtimestamp(1790200000, main._KST_TZ).strftime("%H:%M:%S")
    ok("A-8 ★진짜 _tg_call 길: 텔레그램 응답 date 가 «텔레그램 HH:MM:SS»(KST) 로 붙는다★",
       json.loads(bytes(r8.body)).get("message_id") == 7 and len(raw) == 1
       and raw[0].endswith("· 텔레그램 %s)" % hhmmss) and "발생→수신 1." in raw[0], str(raw))
    ok("A-8b date 는 부른 과제에만 — 과제 밖(다른 알람)엔 안 남는다", main._TG_RES.get() is None, str(main._TG_RES.get()))
    # 사진도 같은 꼬리
    _en, _ch, _sp = main.tg_enabled, main.tenant_chat_id, main.tg_send_photo

    async def _send_photo(*a, **k):
        return 88

    class _FormT(_FormReq):
        async def form(self):
            return {"caption": self._cap, "expect_reply": "0", "t0": str(time.time() - 3.0)}
    main.tg_enabled, main.tenant_chat_id, main.tg_send_photo = (lambda: True), (lambda t: "1"), _send_photo
    try:
        await main.telegram_photo("PC-A9", _FormT("캡차 화면"), _UF(b"\x89PNG" + b"\0" * 10))
    finally:
        main.tg_enabled, main.tenant_chat_id, main.tg_send_photo = _en, _ch, _sp
    raw = await _alarms_raw("PC-A9")
    ok("A-9 사진 알람도 t0 → «(사진) (⏱ 발생→수신 3.xx초 · …)»",
       len(raw) == 1 and raw[0].startswith("[알람] PC-A9 | 캡차 화면 (사진) (⏱ 발생→수신 3.") , str(raw))
    # ★팜뷰 목소리는 그대로★ — 실물 alarmvoice 로 꼬리 있는 줄 = 꼬리 없는 줄
    av = _farmview_alarmvoice()
    ev_t = {"type": "log", "pc": "PC-A7", "ts": "2026-09-24T00:00:00", "level": "info",
            "message": "[알람] PC-A7 | 🚨 캡차 아직 안 풀림(60초) — 코드를 답장해 주세요 (⏱ 발생→수신 1.20초 · 수신→전송 0.40초 · 텔레그램 05:41:02)"}
    ev_n = dict(ev_t, message="[알람] PC-A7 | 🚨 캡차 아직 안 풀림(60초) — 코드를 답장해 주세요")
    pt, pn = av.pick(ev_t), av.pick(ev_n)
    ok("A-9b ★팜뷰 alarmvoice 실물: 꼬리가 있어도 같은 말(summarize)·같은 무름 판정★",
       pt is not None and pn is not None and av.summarize(*pt) == av.summarize(*pn)
       and "⏱" not in av.summarize(*pt), "%s | %s" % (pt and av.summarize(*pt), pn and av.summarize(*pn)))
    main.TELEGRAM_BOT_TOKEN = ""


def test_all():
    run_all([t_text_alarm, t_photo_alarm, t_alarm_timing])
    finish("test_alarm_event", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
