# -*- coding: utf-8 -*-
"""[대시보드] #448 (2026-10-05 주인님 «F5 안 눌러도 문제없게 · 서버 괴롭히지 말고») — 대시보드 WS 살아 있음 감시.

  S  서버: ping 이 상태 판 번호(ver)를 싣는다 · ping 주기 ≤ 20초(숨은 화면에도)
  C  화면(node 로 실제 JS 실행): 45초(숨으면 90초) 무수신이면 소켓을 버리고 재접속 예약 ·
     백오프 2→4→8…60초 + 지터 · 예약은 하나 · 연결 시도가 15초 넘게 걸려도 버린다 ·
     ping 의 판 번호가 계속 다르면 전량 요청 한 번 → 그래도 다르면 재접속 · 멀쩡하면 아무것도 안 한다
  U  표시: «갱신 N초 전 / 재연결 중» 칸 · 보이게 될 때 낡았을 때만 재접속 · 새로고침(reload)은 세션 만료 1008 에서만

    cd updater/server && python -X utf8 tests/test_ws_liveness.py
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

from _harness import main, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 24


def _fn(src, name):
    i = src.index("function " + name + "(")
    d, k = 0, src.index("{", i)
    while True:
        c = src[k]
        d += (c == "{") - (c == "}")
        k += 1
        if d == 0:
            return src[i:k]


def _line(src, prefix):
    for ln in src.splitlines():
        if ln.strip().startswith(prefix):
            return ln.strip()
    raise ValueError(prefix)


async def t_server():
    f = main._FEED
    keep = f.get("t448")
    try:
        f["t448"] = {"ver": 1234567, "keys": {}, "full": None, "diff": None, "retired": None, "latest": None, "at": 0.0}
        ok("S-1 ★ping 이 상태 판 번호를 싣는다★", main._ping_msg("t448") == {"type": "ping", "ver": 1234567}, str(main._ping_msg("t448")))
        ok("S-2 판이 아직 없으면 예전 모양 {type:ping} 그대로", main._ping_msg("nobody448") == {"type": "ping"})
    finally:
        if keep is None:
            f.pop("t448", None)
        else:
            f["t448"] = keep
    ok("S-3 ping 주기 ≤ 20초(상태 ping·숨은 화면 ping 둘 다) — 화면 45초 감시견이 두 번 연속 빠져야 오탐",
       main.DASH_KEEPALIVE_S <= 20 and main.STATE_PING_S <= 20, "%s %s" % (main.DASH_KEEPALIVE_S, main.STATE_PING_S))
    import inspect
    src = inspect.getsource(main)
    ok("S-4 ping 을 보내는 세 곳 중 판 번호를 싣는 두 곳(상태 ping·숨은 화면 ping)이 _ping_msg 를 쓴다",
       src.count("manager.broadcast(_ping_msg(") == 2, str(src.count("manager.broadcast(_ping_msg(")))


JS_HEAD = r"""
let now = 1000000, timers = [], connects = 0, sent = [], closed = 0, hidden = false, STATE_VER = 100, _resyncAsked = false;
Date.now = () => now;
function setTimeout(fn, ms){ const t = {fn, ms, on:true}; timers.push(t); return t; }
function clearTimeout(t){ if (t) t.on = false; }
function _wsHid(){ return hidden; }
function connectWS(){ connects++; _ws = mkSock(1); _wsLastMsg = now; _wsConnAt = now; }
function mkSock(rs){ return {readyState: rs, send(m){ sent.push(m); }, close(){ closed++; }, onmessage:()=>1, onclose:()=>1}; }
const els = {};
const document = {getElementById(id){ return els[id] || (els[id] = {id, className:'', textContent:''}); }};
const live = () => timers.filter(t => t.on);
"""


async def t_client():
    src = main.HTML_DASHBOARD
    node = shutil.which("node")
    ok("C-0 node 가 있어야 한다", bool(node))
    if not node:
        return
    try:
        body = "\n".join([_line(src, "let _ws=null"), _line(src, "const WS_DEAD_MS")] + [_fn(src, n) for n in
                         ("_wsDot", "_wsSchedule", "_wsAbandon", "_wsCheck", "_wsPing", "_wsAgeTick")])
    except Exception as e:
        ok("C-1 함수를 잘라낸다", False, str(e))
        return
    js = JS_HEAD + body.replace("setInterval", "void") + r"""
const o = {};
// 백오프 — 연속 실패
const d = [];
for (let i = 0; i < 8; i++) { _ws = null; _wsTimer = null; _wsSchedule(); d.push(live().pop().ms); }
o.delays = d;
// 예약은 하나
timers = []; _wsTimer = null; _wsFail = 0; _wsSchedule(); _wsSchedule(); o.oneTimer = live().length;
// 첫 메시지를 받으면 백오프 초기화(onmessage 가 _wsFail=0) — 여기선 값으로 흉내
// 살아 있는 소켓: 44초 무소식 = 그대로, 46초 = 버림
timers = []; _wsTimer = null; _wsFail = 0; connects = 0; closed = 0;
const s1 = mkSock(1); _ws = s1; _wsLastMsg = now;
now += 44000; _wsCheck(); o.at44 = {same: _ws === s1, closed, timers: live().length};
now += 2000; _wsCheck(); o.at46 = {gone: _ws === null, closed, timers: live().length, handlersCleared: s1.onmessage === null && s1.onclose === null};
// 숨은 탭: 60초 괜찮고 100초는 버림
timers = []; _wsTimer = null; _wsFail = 0; closed = 0; hidden = true;
const s2 = mkSock(1); _ws = s2; _wsLastMsg = now;
now += 60000; _wsCheck(); o.hid60 = _ws === s2;
now += 40000; _wsCheck(); o.hid100 = _ws === null;
hidden = false;
// 연결 시도가 15초 넘게 CONNECTING
timers = []; _wsTimer = null; closed = 0;
const s3 = mkSock(0); _ws = s3; _wsConnAt = now;
now += 10000; _wsCheck(); o.conn10 = _ws === s3;
now += 6000; _wsCheck(); o.conn16 = _ws === null;
// 소켓도 예약도 없으면 바로 접속
timers = []; _wsTimer = null; _ws = null; connects = 0; _wsCheck(); o.noSock = connects;
// 예약이 있으면 건드리지 않는다
timers = []; _wsTimer = null; _ws = null; connects = 0; _wsSchedule(); _wsCheck(); o.pending = connects;
// readyState 2·3(닫는 중·닫힘)이면 버림
timers = []; _wsTimer = null; const s4 = mkSock(3); _ws = s4; _wsCheck(); o.closedState = _ws === null;
// ping 판 번호 비교
function fresh(){ timers = []; _wsTimer = null; sent = []; closed = 0; _wsStaleSince = 0; _resyncAsked = false; const s = mkSock(1); _ws = s; _wsLastMsg = now; return s; }
let s = fresh(); STATE_VER = 100;
_wsPing({type:'ping', ver:100}, s); o.pingSame = {sent: sent.length, gone: _ws === null};
_wsPing({type:'ping'}, s); o.pingNoVer = {sent: sent.length, gone: _ws === null};
hidden = true; _wsPing({type:'ping', ver:999}, s); hidden = false; o.pingHidden = {sent: sent.length, gone: _ws === null, since: _wsStaleSince};
_wsPing({type:'ping', ver:999}, s); o.first = {sent: sent.length, gone: _ws === null, since: _wsStaleSince > 0};
now += 5000; _wsPing({type:'ping', ver:999}, s); o.early = {sent: sent.length, gone: _ws === null};
now += 20000; _wsPing({type:'ping', ver:999}, s); o.askResync = {sent: sent.slice(), asked: _resyncAsked, gone: _ws === null};
now += 21000; _wsPing({type:'ping', ver:999}, s); o.thenReconnect = {gone: _ws === null, timers: live().length};
// 어긋났다가 맞으면 타이머 초기화
s = fresh(); STATE_VER = 100; _wsPing({type:'ping', ver:555}, s); STATE_VER = 555; _wsPing({type:'ping', ver:555}, s); o.recovered = _wsStaleSince;
// 표시
_ws = mkSock(1); _wsLastMsg = now - 7000; _wsAgeTick(); o.ageLive = els['ws-age'].textContent;
_wsLastMsg = now - 40000; _wsAgeTick(); o.ageWarn = els['ws-age'].className;
_ws = null; _wsRetryAt = now + 4000; _wsAgeTick(); o.ageRe = els['ws-age'].textContent;
console.log(JSON.stringify(o));
"""
    d = tempfile.mkdtemp(prefix="wsliv_")
    pth = os.path.join(d, "t.js")
    open(pth, "w", encoding="utf-8").write(js)
    rr = subprocess.run([node, pth], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        o = json.loads(rr.stdout.strip().splitlines()[-1])
    except Exception:
        o = None
    err = rr.stderr[-500:]
    ok("C-1 node 실행", o is not None, err)
    if o is None:
        return
    dl = o["delays"]
    bases = [2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000]
    ok("C-2 ★백오프 2→4→8→16→32→60초 상한, 각각 +0~50% 지터★",
       all(b <= x <= b * 1.5 for b, x in zip(bases, dl)), str(dl))
    ok("C-3 예약 타이머는 하나(여러 번 불러도)", o["oneTimer"] == 1, str(o["oneTimer"]))
    ok("C-4 44초 무소식은 건드리지 않는다", o["at44"] == {"same": True, "closed": 0, "timers": 0}, str(o["at44"]))
    ok("C-5 ★46초 무소식이면 소켓을 버리고(close 호출·핸들러 제거 — onclose 를 기다리지 않는다) 재접속을 예약★",
       o["at46"] == {"gone": True, "closed": 1, "timers": 1, "handlersCleared": True}, str(o["at46"]))
    ok("C-6 숨은 탭은 90초 기준(60초 괜찮고 100초는 버림)", o["hid60"] is True and o["hid100"] is True, str((o["hid60"], o["hid100"])))
    ok("C-7 연결 시도가 15초를 넘기면 버린다(10초는 기다린다)", o["conn10"] is True and o["conn16"] is True, str((o["conn10"], o["conn16"])))
    ok("C-8 소켓도 예약도 없으면 바로 접속 · 예약이 있으면 건드리지 않는다", o["noSock"] == 1 and o["pending"] == 0, str((o["noSock"], o["pending"])))
    ok("C-9 닫는 중·닫힘 소켓은 버린다", o["closedState"] is True)
    ok("C-10 ★ping 판 번호가 같으면 아무것도 안 한다 · 번호 없는 예전 ping · 숨은 화면도 무시★",
       o["pingSame"] == {"sent": 0, "gone": False} and o["pingNoVer"] == {"sent": 0, "gone": False}
       and o["pingHidden"]["sent"] == 0 and o["pingHidden"]["gone"] is False and o["pingHidden"]["since"] == 0, str((o["pingSame"], o["pingNoVer"], o["pingHidden"])))
    ok("C-11 처음 본 어긋남은 기록만(전송 중일 수 있다) · 5초 뒤에도 기다린다",
       o["first"] == {"sent": 0, "gone": False, "since": True} and o["early"] == {"sent": 0, "gone": False}, str((o["first"], o["early"])))
    ok("C-12 ★20초 넘게 계속 다르면 전량 요청(resync) 한 번★",
       len(o["askResync"]["sent"]) == 1 and json.loads(o["askResync"]["sent"][0]) == {"type": "resync"} and o["askResync"]["asked"] is True
       and o["askResync"]["gone"] is False, str(o["askResync"]))
    ok("C-13 ★그래도 20초 더 다르면 재접속(소켓 버림 + 예약)★", o["thenReconnect"] == {"gone": True, "timers": 1}, str(o["thenReconnect"]))
    ok("C-14 맞춰지면 어긋남 기록을 지운다", o["recovered"] == 0)
    ok("C-15 ★표시: «갱신 N초 전» · 30초 넘으면 경고색 · 소켓 없으면 «재연결 중 N초»★",
       o["ageLive"] == "갱신 7초 전" and "amber" in o["ageWarn"] and o["ageRe"] == "재연결 중 4초", str((o["ageLive"], o["ageWarn"], o["ageRe"])))


async def t_wiring():
    src = main.HTML_DASHBOARD
    ok("U-1 ★재접속 3초 고정이 사라졌다(백오프 _wsSchedule 로 대체)★", "setTimeout(connectWS,3000)" not in src and "_wsSchedule();" in src)
    ok("U-2 감시 타이머가 _wsCheck(5초) · 보이게 될 때 _wsCheck · 온라인 복귀 처리",
       "setInterval(_wsCheck,5000);" in src and "      _wsCheck();" in src and "window.addEventListener('online'" in src)
    ok("U-3 새로고침은 세션 만료(1008) 한 곳뿐 — WS 감시 코드는 reload 를 안 한다",
       "if(e&&e.code===1008){location.reload();return;}" in src
       and "location.reload" not in "".join(_fn(src, n) for n in ("_wsAbandon", "_wsCheck", "_wsPing", "_wsSchedule")))
    ok("U-4 /status 반복 조회를 새로 만들지 않았다(WS 감시 함수들은 fetch 를 안 쓴다)",
       "fetch(" not in "".join(_fn(src, n) for n in ("_wsAbandon", "_wsCheck", "_wsPing", "_wsSchedule", "_wsAgeTick")))
    ok("U-5 표시 칸 #ws-age 가 머리줄에 있다", 'id="ws-age"' in src and src.index('id="ws-dot"') < src.index('id="ws-age"') < src.index('id="pc-count"'))
    ok("U-6 첫 메시지를 받으면 백오프 초기화 · 버린 소켓의 늦은 이벤트는 무시",
       "if(_ws!==ws) return;\n    _wsLastMsg=Date.now(); _wsFail=0;" in src and src.count("if(_ws!==ws) return;") >= 2)


def test_all():
    run_all([t_server, t_client, t_wiring])
    finish("test_ws_liveness", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
