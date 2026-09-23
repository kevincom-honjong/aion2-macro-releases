# -*- coding: utf-8 -*-
"""주인님 «빠뜨린 것» 감사(2026-09-24, 아이온2 결정) — 대시보드 몫 두 가지.
  #114  첫 화면 GET / 를 gzip 으로(요청이 받을 때만) + `Server-Timing: app;dur=<ms>` (/ · /status · /summary)
        — 운영에서 로그인 세션으로 개발자도구 Network 에서 서버 시간과 네트워크를 갈라 잰다.
  #64   팜뷰 업데이트 큐 왕복 — T1(팜뷰가 가져감)·T2(팜뷰 ack) 를 그 PC 로그에 한 줄씩(예전엔 Railway 콘솔뿐, T1 은 덮여 사라짐).

    cd updater/server && python -X utf8 tests/test_audit_0924.py
"""
import gzip
import json
import re

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 16
TOK = "fvsecret-audit"
H = {"X-FV-Token": TOK}
_ST = re.compile(r"^app;dur=\d+\.\d$")


def _body(r):
    return json.loads(bytes(r.body))


async def t_speed_headers():
    sess = main.new_session("main")
    r = await main.dashboard(Req(api_key=None, session=sess, headers={"accept-encoding": "gzip, deflate, br"}))
    hd = {k.lower(): v for k, v in r.headers.items()}
    raw = bytes(r.body)
    try:
        html = gzip.decompress(raw).decode("utf-8")
    except Exception as e:
        html = "exc:%s" % type(e).__name__
    ok("S-1 ★gzip 을 받는 브라우저엔 첫 화면이 gzip — 풀면 HTML 그대로★",
       hd.get("content-encoding") == "gzip" and html == main.HTML_DASHBOARD, "%s %s" % (hd.get("content-encoding"), len(raw)))
    ok("S-1b 압축본은 원본의 절반 아래(544KB → 약 170KB)", len(raw) * 2 < len(main.HTML_DASHBOARD.encode("utf-8")),
       "%d / %d" % (len(raw), len(main.HTML_DASHBOARD.encode("utf-8"))))
    ok("S-2 Server-Timing «app;dur=N.N» · Vary: Accept-Encoding · no-store 유지",
       bool(_ST.match(hd.get("server-timing", ""))) and hd.get("vary") == "Accept-Encoding"
       and "no-store" in hd.get("cache-control", "") and hd.get("content-type", "").startswith("text/html"), str(hd))
    r2 = await main.dashboard(Req(api_key=None, session=sess, headers={"accept-encoding": "gzip"}))
    ok("S-3 두 번째부턴 압축을 다시 안 한다(같은 바이트 객체)", main._HTML_GZ["gz"] is not None
       and bytes(r2.body) == raw and main._HTML_GZ["src"] is main.HTML_DASHBOARD, "")
    r3 = await main.dashboard(Req(api_key=None, session=sess))
    hd3 = {k.lower(): v for k, v in r3.headers.items()}
    ok("S-4 gzip 을 말하지 않는 요청엔 예전 그대로 평문 HTML(Content-Encoding 없음) + Server-Timing",
       "content-encoding" not in hd3 and bytes(r3.body).decode("utf-8") == main.HTML_DASHBOARD
       and bool(_ST.match(hd3.get("server-timing", ""))), str(hd3))
    r4 = await main.dashboard(Req(api_key=None, headers={"accept-encoding": "gzip"}))
    ok("S-5 세션 없으면 예전처럼 /login 으로(압축본을 안 준다)", r4.status_code in (302, 307)
       and r4.headers.get("location") == "/login", str(r4.status_code))
    rs = await main.all_statuses(Req(api_key=None, session=sess))
    ok("S-6 /status 에 Server-Timing, 본문 모양 그대로(pcs·retired)",
       bool(_ST.match(rs.headers.get("server-timing", ""))) and set(_body(rs)) == {"pcs", "retired"}, str(dict(rs.headers)))
    rm = await main.dashboard_summary(Req(api_key=None, session=sess))
    ok("S-7 /summary 에 Server-Timing, 본문은 totals 그대로",
       bool(_ST.match(rm.headers.get("server-timing", ""))) and isinstance(_body(rm), dict), str(dict(rm.headers)))
    # 원본 HTML 이 바뀌면(시험·재부팅) 캐시도 새로 — 옛 압축본을 주지 않는다
    old = main.HTML_DASHBOARD
    try:
        main.HTML_DASHBOARD = old + "<!-- t -->"
        r5 = await main.dashboard(Req(api_key=None, session=sess, headers={"accept-encoding": "gzip"}))
        same = gzip.decompress(bytes(r5.body)).decode("utf-8") == main.HTML_DASHBOARD
    finally:
        main.HTML_DASHBOARD = old
    ok("S-8 HTML 이 바뀌면 새로 압축한다(옛 압축본을 안 준다)", same, "")


async def _send_upd(pc, cmd="update"):
    r = await main.dashboard_send_updater_command(pc, Req({"command": cmd}, api_key=None, session=main.new_session("main")))
    return _body(r)["id"]


async def _fv_list(since=0):
    return _body(await main.fv_updcmd_list(Req(api_key=None, headers=H), since=str(since)))["cmds"]


async def _qlog(pc):
    return [x["message"] for x in await db.get_logs(main.ns("main", pc), limit=50)
            if (x.get("message") or "").startswith("[업데이트큐]")]


async def t_updq_log():
    main.FV_TOKEN = TOK
    main.FV_TENANT = "main"
    main.FV_UPDCMD_QUEUE.clear()
    pc = "PC-Q01"
    await _send_upd(pc)
    got = await _fv_list()
    fid = got[0]["id"] if got else None
    lg = await _qlog(pc)
    ok("Q-1 ★팜뷰가 가져가면 그 PC 로그에 T1 한 줄 «팜뷰가 가져감 id=… act=updater (누른 뒤 N초)»★",
       len(lg) == 1 and re.fullmatch(r"\[업데이트큐\] 팜뷰가 가져감 id=%s act=updater \(누른 뒤 \d+초\)" % fid, lg[0]) is not None,
       str(lg))
    await _fv_list()
    await _fv_list()
    ok("Q-2 ack 전 재폴링(계약상 계속 나옴)마다 또 적지 않는다 — 한 번만", len(await _qlog(pc)) == 1, str(await _qlog(pc)))
    ok("Q-2b 팜뷰 응답 모양은 그대로 {id,pc,act,at}(로그 표식이 안 샌다)", got and set(got[0]) == {"id", "pc", "act", "at"},
       str(got))
    b = _body(await main.fv_updcmd_ack(Req({"pc": pc, "id": fid, "ok": True, "why": "done"}, api_key=None, headers=H)))
    lg = await _qlog(pc)
    ok("Q-3 ★ack 가 오면 T2 한 줄 «팜뷰 ack id=… ok=True why=done (누른 뒤 N초)»★",
       b.get("removed") is True and len(lg) == 2
       and re.fullmatch(r"\[업데이트큐\] 팜뷰 ack id=%s ok=True why=done \(누른 뒤 \d+초\)" % fid, lg[1]) is not None, str(lg))
    b2 = _body(await main.fv_updcmd_ack(Req({"pc": pc, "id": 999999, "ok": False, "why": "x"}, api_key=None, headers=H)))
    lg = await _qlog(pc)
    ok("Q-4 없는 id ack 도 남기되 «(이미 없음·id 불일치)» · 누른 시각 모름 «?»",
       b2.get("removed") is False and len(lg) == 3 and lg[2].endswith("(누른 뒤 ?) (이미 없음·id 불일치)"), str(lg))
    # 로그가 실패해도 큐는 그대로 돈다
    pc = "PC-Q02"
    await _send_upd(pc, "restart")
    real = main.insert_log

    async def _boom(*a, **k):
        raise RuntimeError("db down")
    main.insert_log = _boom
    try:
        g = await _fv_list()
        b3 = _body(await main.fv_updcmd_ack(Req({"pc": pc, "id": g[0]["id"], "ok": True}, api_key=None, headers=H)))
    finally:
        main.insert_log = real
    ok("Q-5 로그 쓰기가 실패해도 목록·ack 는 그대로(큐를 막지 않는다)", [c["pc"] for c in g] == [pc] and b3.get("removed") is True,
       "%s %s" % (g, b3))
    ok("Q-6 _updq_age: 형식 틀린 at → «?», 미래 시각 → «0초»",
       main._updq_age("nope") == "?" and main._updq_age(None) == "?" and main._updq_age("2999-01-01T00:00:00") == "0초", "")
    main.FV_UPDCMD_QUEUE.clear()


def test_all():
    run_all([t_speed_headers, t_updq_log])
    finish("test_audit_0924", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
