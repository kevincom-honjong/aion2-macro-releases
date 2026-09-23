# -*- coding: utf-8 -*-
"""#128 팜뷰 알람 목소리 — /telegram/send·/telegram/photo 가 ★실제로 내보내는★ 알람을 그 PC 로그에 «[알람] …» 한 줄로
즉시 남기고, 팜뷰 /api/fv/events 가 그 줄을 준다(계약: FV_API «[알람] 이벤트»). 예전엔 매크로 info 줄이 하트비트(30초)에
실려 ~35초 늦었다.

    cd updater/server && python -X utf8 tests/test_alarm_event.py
"""
import json
import time

from _harness import main, db, ok, Req, run_all, finish, TG_SENT   # noqa: E402

MIN_CHECKS = 11
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


async def _alarms(pc):
    return [x["message"] for x in await db.get_logs(pc, limit=50) if (x.get("message") or "").startswith("[알람]")]


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
       len(ev) == 1 and ev[0]["message"] == "[알람] PC-A1 | 🚨 캡차 — 답해 주세요" and ev[0]["level"] == "info", str(ev))
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


def test_all():
    run_all([t_text_alarm, t_photo_alarm])
    finish("test_alarm_event", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
