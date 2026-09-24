# -*- coding: utf-8 -*-
"""TG7 후속 (2026-09-24 주인님 결정) — 텔레그램 중계의 음소거 뚫기 규칙 `main._tg_hard` 한 곳.

  ① /telegram/send 본문 `hard:true` 는 음소거를 뚫는다(«음소거라도 올리기» — 매크로 1.1.1006 강제 알람). ⛔·🚨 접두사도 그대로.
  ② /telegram/photo expect_reply=1(캡차)은 ★늘★ 나간다(«항상 올리기») — 예전엔 사진 창구에 음소거 확인이 없어 우연히 나갔다.
     나머지 사진은 음소거면 텍스트와 같은 200 {ok:false, reason:"muted"}.
  ③ 테넌트 차단은 hard 보다 먼저다 — 차단이면 ⛔ 정지 안내만(403 = 처리됨, 클라이언트는 직접 보내지 않는다).
  계약: updater/CONTRACTS_대시보드.md §7 · updater/SHARED_ISSUES_대시보드.md (CONTRACTS_아이온2 반영 요청)
    cd updater/server && python -X utf8 tests/test_tg_hard.py
"""
import json
import time

from fastapi import HTTPException

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 20
SENT: list = []


class _UF:
    def __init__(self, data):
        self.filename, self._d = "c.png", data

    async def read(self, n=-1):
        return self._d if n < 0 else self._d[:n]


class _Form(Req):
    def __init__(self, form, api_key="testkey"):
        super().__init__({}, api_key=api_key)
        self._f = form

    async def form(self):
        return self._f


async def _photo(chat, caption, raw, **kw):
    SENT.append(("photo", caption))
    return 502


class _Env:
    def __enter__(self):
        self.saved = (main.tg_enabled, main.tenant_chat_id, main.tg_send_text, main.tg_send_photo)
        main.tg_enabled, main.tenant_chat_id = (lambda: True), (lambda t: "1")

        async def _t(chat, text, **kw):
            SENT.append(("text", text))
            return 501
        main.tg_send_text, main.tg_send_photo = _t, _photo
        main._TG_MUTE.clear()
        main._TG_MUTE[main.ns("main", "PC-31")] = time.time() + 3600      # PC-31 음소거 1시간
        SENT.clear()
        return self

    def __exit__(self, *e):
        main.tg_enabled, main.tenant_chat_id, main.tg_send_text, main.tg_send_photo = self.saved
        main._TG_MUTE.clear()


def _j(r):
    return json.loads(bytes(r.body))


async def _send(body, pc="PC-31"):
    n = len(SENT)
    b = _j(await main.telegram_send(pc, Req(body)))
    return b, len(SENT) - n


async def _pic(form, pc="PC-31", api_key="testkey"):
    n = len(SENT)
    b = _j(await main.telegram_photo(pc, _Form(form, api_key=api_key), _UF(b"\x89PNG" + b"\0" * 10)))
    return b, len(SENT) - n


async def t_text():
    with _Env():
        b, n = await _send({"text": "지역 차단 — 확인 필요", "hard": True})
        ok("H-1 ★음소거 + hard:true → 중계된다★", b.get("ok") is True and n == 1, str(b))
        b, n = await _send({"text": "보통 알림"})
        ok("H-2 음소거 + hard 없음 → 200 {ok:false, reason:muted}, 안 보냄",
           b.get("ok") is False and b.get("reason") == "muted" and n == 0, str(b))
        got = []
        for v in (1, "1", "true", "TRUE", "yes"):
            b, n = await _send({"text": "x", "hard": v})
            got.append(n)
        ok("H-1b hard 1·'1'·'true'·'TRUE'·'yes' 도 뚫는다", got == [1] * 5, str(got))
        got = []
        for v in (False, 0, "0", "false", "", None, [], {}):
            b, n = await _send({"text": "x", "hard": v})
            got.append((n, b.get("reason")))
        ok("H-2b hard false·0·'0'·'false'·''·None·[]·{} 는 못 뚫는다", got == [(0, "muted")] * 8, str(got))
        b, n = await _send({"text": "🚨 캡차"})
        ok("H-1c ⛔·🚨 접두사는 그대로 뚫는다(hard 없이)", b.get("ok") is True and n == 1, str(b))
        b, n = await _send({"text": "보통 알림", "hard": True}, pc="PC-32")
        ok("H-5 음소거 아닌 PC 는 그냥 나간다", b.get("ok") is True and n == 1, str(b))
        logs = [x.get("message") or "" for x in await db.get_logs("PC-31", limit=50)]
        ok("H-2c 생략은 그 PC 로그에 «중계 생략(음소거»", any("중계 생략(음소거" in m for m in logs), str(logs[:3]))


async def t_photo():
    with _Env():
        b, n = await _pic({"caption": "캡차", "expect_reply": "1"})
        ok("H-3 ★음소거 + 캡차 사진(expect_reply=1) → 늘 중계★", b.get("ok") is True and n == 1, str(b))
        b, n = await _pic({"caption": "그냥 사진", "expect_reply": "0"})
        ok("H-3b 음소거 + 보통 사진 → 200 muted, 안 보냄", b.get("reason") == "muted" and b.get("ok") is False and n == 0, str(b))
        b, n = await _pic({"caption": "그냥 사진", "expect_reply": "0", "hard": "1"})
        ok("H-3c 음소거 + 사진 hard=1 → 중계", b.get("ok") is True and n == 1, str(b))
        b, n = await _pic({"caption": "⛔ 정지 화면", "expect_reply": "0"})
        ok("H-3d 음소거 + ⛔ 캡션 사진 → 중계", b.get("ok") is True and n == 1, str(b))


async def _call(coro):
    """(상태코드, 본문) — JSONResponse 든 HTTPException 이든. 기본 HTTPException 의 detail 은 "Forbidden"."""
    try:
        r = await coro
        return r.status_code, _j(r)
    except HTTPException as e:
        return e.status_code, {"detail": e.detail}


def _rawpic(form, pc="PC-31", api_key="testkey"):
    return main.telegram_photo(pc, _Form(form, api_key=api_key), _UF(bytes([0x89]) + b"PNG" + bytes(10)))


_BLK = "차단 상태에서는 정지 안내만 전송됩니다"   # ★문구 그대로★ — 1.1.1006 매크로가 맞춰 본다(lc report_module._TG_BLOCKED_DETAIL)


async def t_blocked():
    """③ 차단 테넌트 — hard 로 못 뚫는다. ⛔ 정지 안내만(시간당 3). ★차단 응답엔 reason:"blocked"(2026-09-24 TG7 클라이언트 반)★ —
    예전 한국어 detail 은 그대로, 사진도 같은 문구로 키 오류(그냥 403 Forbidden)와 갈린다."""
    main.KEY_TO_TENANT["blk-key"] = "blk"
    main.KILLED_TENANTS.add("blk")
    main._KILL_TG.pop("blk", None)
    try:
        with _Env():
            n0 = len(SENT)
            code, b = await _call(main.telegram_send("PC-01", Req({"text": "지역 차단", "hard": True}, api_key="blk-key")))
            ok("H-4 ★차단 테넌트 + hard:true(⛔ 아님) → 403 {detail: 예전 문구, reason: blocked}, 안 보냄★",
               code == 403 and b == {"detail": _BLK, "reason": "blocked"} and len(SENT) == n0, f"{code} {b}")
            r = await main.telegram_send("PC-01", Req({"text": "⛔ 이용이 중지되었습니다", "hard": True}, api_key="blk-key"))
            ok("H-4b 차단 테넌트 + ⛔ 정지 안내 → 중계(예전 그대로)", _j(r).get("ok") is True and len(SENT) == n0 + 1, str(_j(r)))
            code, b = await _call(_rawpic({"caption": "캡차", "expect_reply": "1", "hard": "1"}, pc="PC-01", api_key="blk-key"))
            ok("H-4c ★차단 테넌트 캡차 사진 → 403 {detail: 텍스트와 같은 문구, reason: blocked}★(Forbidden 아님)",
               code == 403 and b == {"detail": _BLK, "reason": "blocked"} and len(SENT) == n0 + 1, f"{code} {b}")
            for _ in range(2):                              # 시간당 3 — 1개는 H-4b 가 썼다
                await main.telegram_send("PC-01", Req({"text": "⛔ 정지"}, api_key="blk-key"))
            code, b = await _call(main.telegram_send("PC-01", Req({"text": "⛔ 정지"}, api_key="blk-key")))
            ok("H-4d ★정지 안내 상한 429 도 reason: blocked★(detail 예전 그대로) — 폴백하면 차단이 뚫린다",
               code == 429 and b == {"detail": "정지 안내 전송 상한", "reason": "blocked"} and len(SENT) == n0 + 3, f"{code} {b}")
            n1 = len(SENT)
            ct, bt = await _call(main.telegram_send("PC-01", Req({"text": "⛔ x"}, api_key="nope-key")))
            cp, bp = await _call(_rawpic({"caption": "캡차", "expect_reply": "1"}, pc="PC-01", api_key="nope-key"))
            ok("H-4e 미등록 키 → 그냥 403 Forbidden(reason 없음 = 매크로 폴백) — 텍스트·사진 둘 다",
               (ct, bt, cp, bp) == (403, {"detail": "Forbidden"}, 403, {"detail": "Forbidden"}) and len(SENT) == n1,
               f"{ct} {bt} / {cp} {bp}")
            _pb = main._key_probe_blocked
            main._key_probe_blocked = lambda ip: True
            try:
                ct, bt = await _call(main.telegram_send("PC-01", Req({"text": "⛔ x"}, api_key="blk-key")))
                cp, bp = await _call(_rawpic({"caption": "캡차"}, pc="PC-01", api_key="blk-key"))
            finally:
                main._key_probe_blocked = _pb
            ok("H-4f ★probe 잠금 IP 는 차단 키라도 그냥 403★(reason 을 주면 키 추측 오라클 — 2026-08-06 critical)",
               (ct, bt, cp, bp) == (403, {"detail": "Forbidden"}, 403, {"detail": "Forbidden"}) and len(SENT) == n1,
               f"{ct} {bt} / {cp} {bp}")
            main.KILLED_TENANTS.discard("blk")
            code, b = await _call(_rawpic({"caption": "캡차", "expect_reply": "1"}, pc="PC-01", api_key="blk-key"))
            ok("H-4g 차단이 풀리면 같은 키 사진이 그대로 나간다(reason 갈래가 정상 키를 안 먹는다)",
               code == 200 and b.get("ok") is True and len(SENT) == n1 + 1, f"{code} {b}")
    finally:
        main.KEY_TO_TENANT.pop("blk-key", None)
        main.KILLED_TENANTS.discard("blk")
        main._KILL_TG.pop("blk", None)


def t_rule_one_place():
    import inspect
    srcs = {f.__name__: inspect.getsource(f) for f in (main.telegram_send, main.telegram_photo)}
    ok("H-6 규칙 함수 한 곳 — 두 창구가 _tg_hard 를 부르고 ⛔·🚨 를 따로 안 센다(§A12)",
       all("_tg_hard(" in v and 'startswith(("⛔", "🚨"))' not in v for v in srcs.values())
       and main._tg_hard("x", True) and main._tg_hard("x", None, True) and not main._tg_hard("x") and main._tg_hard(" ⛔x"),
       str({k: "_tg_hard(" in v for k, v in srcs.items()}))
    ok("H-6b 차단 판정·응답도 한 곳 — 두 창구가 _tg_blocked_tenant·_tg_blocked_resp 를 부르고 문구를 따로 안 쓴다",
       all("_tg_blocked_tenant(" in v and "_tg_blocked_resp(" in v and "정지 안내만 전송됩니다" not in v for v in srcs.values()),
       str({k: ("_tg_blocked_tenant(" in v, "_tg_blocked_resp(" in v) for k, v in srcs.items()}))


def test_all():
    run_all([t_text, t_photo, t_blocked, t_rule_one_place])
    finish("test_tg_hard", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
