# -*- coding: utf-8 -*-
"""[대시보드] #454 (2026-10-06 «전광판 숫자가 새로고침 안 하면 오르락내리락») — 한 칸 = 한 출처.

  원인: 서버값을 60초 묵으면 버리고 화면 계산으로 갈아탔다(두 계산이 달라 튐) · 캐릭터/완료는 화면 전용이라 카드 상태마다 움직였다.
  지금: 마지막 서버값 유지(첫 /summary 전에만 화면 계산) · char_total/chars_done 서버 계산 · 파이썬 _fv_group_chars == JS groupChars.

    cd updater/server && python -X utf8 tests/test_summary_stable.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from _harness import main, ok, run_all, finish   # noqa: E402
from test_summary import _Patch, _row, _info, SUB_CH   # noqa: E402

MIN_CHECKS = 14


def _fn(src, name):
    i = src.index("function " + name + "(")
    d, k = 0, src.index("{", i)
    while True:
        c = src[k]
        d += (c == "{") - (c == "}")
        k += 1
        if d == 0:
            return src[i:k]


M3 = {"1": {"1": "a", "2": "b", "3": "c"}, "2": {"1": "d", "2": "e"}, "3": {"1": "f"}}
FIX = [
    [{"pc_id": "PC-03", "acct_names": M3}, {"pc_id": "PC-03b", "acct_names": M3, "banned_slots": [2]}],
    [{"pc_id": "PC-15", "acct_names": M3, "banned_slots": [1, 2, 3]}],
    [{"pc_id": "PC-07", "daily_progress": [1, 1, 1]}, {"pc_id": "PC-07b", "daily_progress": [1, 1], "banned_slots": []}],
    [{"pc_id": "PC-08b", "daily_progress": [1, 1], "banned_slots": [2]}],
    [{"pc_id": "PC-09", "status": "no_account"}],
    [],
]


async def t_parity():
    src = main.HTML_DASHBOARD
    node = shutil.which("node")
    ok("ST-0 node 가 있어야 한다", bool(node))
    if not node:
        return
    js = _fn(src, "groupChars") + "\nconst acctNumOf = id => ({b:2, c:3, d:4})[(id||'').slice(-1)] || 1;\n" \
        "console.log(JSON.stringify(%s.map(l => groupChars(l))));\n" % json.dumps(FIX)
    d = tempfile.mkdtemp(prefix="st454_")
    pth = os.path.join(d, "t.js")
    open(pth, "w", encoding="utf-8").write(js)
    rr = subprocess.run([node, pth], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        o = json.loads(rr.stdout.strip().splitlines()[-1])
    except Exception:
        o = None
    ok("ST-1 node 실행", o is not None, rr.stderr[-300:])
    if o is None:
        return
    bad = []
    for i, (cards, js_g) in enumerate(zip(FIX, o)):
        if not cards:
            continue
        py = main._fv_group_chars(cards)
        if (py["n"], py["out"], py["from_map"]) != (js_g["n"], js_g["out"], js_g["fromMap"]):
            bad.append((i, py, js_g))
    ok("ST-2 ★파이썬 _fv_group_chars == JS groupChars (지도·OUT·통째OUT·폴백·빈 묶음)★", not bad, str(bad))
    ok("ST-3 값 확인: 지도 묶음 계정2 OUT → 4 · 통째 OUT 0 · 폴백 3+2 · 폴백 OUT 0",
       [o[0]["n"], o[1]["n"], o[2]["n"], o[3]["n"]] == [4, 0, 5, 0],
       str([x["n"] for x in o]))
    ok("ST-4 dpDone 규칙 같다(완료·today 가 false 면 아님)",
       main._dp_done({"completed": True}) and main._dp_done({"completed": True, "today": True})
       and not main._dp_done({"completed": True, "today": False}) and not main._dp_done({"completed": False}) and not main._dp_done(None))


async def t_totals():
    rows = [dict(_row("PC-01"), acct_names=M3, daily_progress=[{"completed": True}, {"completed": True, "today": False}]),
            dict(_row("PC-01b"), acct_names=M3, banned_slots=[2], daily_progress=[{"completed": True}]),
            dict(_row("PC-02"), daily_progress=[{"completed": True}, {"completed": True}]),
            dict(_row("PC-TEST"), acct_names=M3, daily_progress=[{"completed": True}])]
    with _Patch(rows, [_info("PC-01", [dict(SUB_CH)])]):
        t = (await main._fv_build_snapshot("main"))["global"]["totals"]
    ok("ST-5 ★/summary totals 에 새 키 6개(정수)★", all(isinstance(t.get(k), int) for k in
       ("char_total", "chars_out", "chars_map_pcs", "chars_fb_pcs", "chars_done", "pcs_done")), str(t))
    ok("ST-6 char_total = PC-01 묶음(OUT 계정2 제외 4) + PC-02 폴백 2 = 6 · OUT 2 · 가짜 PC 제외",
       (t["char_total"], t["chars_out"], t["chars_map_pcs"], t["chars_fb_pcs"]) == (6, 2, 1, 1), str(t))
    ok("ST-7 chars_done = 1(PC-01 today false 제외)+1(01b)+2(PC-02) = 4 · pcs_done = PC-01b·PC-02 = 2",
       (t["chars_done"], t["pcs_done"]) == (4, 2), str((t["chars_done"], t["pcs_done"])))


async def t_replay():
    src = main.HTML_DASHBOARD
    node = shutil.which("node")
    if not node:
        ok("ST-8 node 없음", False)
        return
    js = "let SERVER_SUMMARY=null, SERVER_SUMMARY_AT=0; let __t=0; const SERVER_SUMMARY_TTL_MS=60000;\nfunction sumClock(){return __t;}\n" \
        + _fn(src, "serverSum") + "\n" + _fn(src, "serverSumStale") + r"""
const tile = () => { const s = serverSum(); return s ? [s.char_total, s.chars_done, s.pcs_done, s.total_kina] : 'client-calc'; };
const log = [tile()];                                  // 첫 /summary 전 = 화면 계산(폴백)
SERVER_SUMMARY = {char_total:149, chars_done:30, pcs_done:5, total_kina:100}; SERVER_SUMMARY_AT = 0;
log.push(tile());
// 재생: 20초마다 /summary 실패(갱신 없음) + WS 변화가 상태를 흔드는 동안 10분
const seen = new Set();
for (__t = 1000; __t <= 600000; __t += 20000) { seen.add(JSON.stringify(tile())); }
console.log(JSON.stringify({first: log[0], second: log[1], distinct: [...seen].length, stale: serverSumStale(), last: tile()}));
"""
    d = tempfile.mkdtemp(prefix="st454r_")
    pth = os.path.join(d, "t.js")
    open(pth, "w", encoding="utf-8").write(js)
    rr = subprocess.run([node, pth], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        o = json.loads(rr.stdout.strip().splitlines()[-1])
    except Exception:
        o = None
    ok("ST-8 재생 실행", o is not None, rr.stderr[-300:])
    if o is None:
        return
    ok("ST-9 ★/summary 가 10분 실패해도 타일 값은 하나(마지막 서버값)이고 낡음만 표시★",
       o["distinct"] == 1 and o["stale"] is True and o["last"] == [149, 30, 5, 100], str(o))
    ok("ST-10 첫 /summary 전에만 화면 계산", o["first"] == "client-calc" and o["second"] == [149, 30, 5, 100], str(o))
    i = src.index("function refreshSummary(")
    body = src[i:i + 12000]
    ok("ST-11 refreshSummary 가 캐릭터·완료 타일을 서버값(char_total·chars_done)으로 채운다",
       "ssC.char_total" in body and "ssC.chars_done" in body and "ssC.pcs_done" in body)


async def t_dungeon():
    """#454-b 일일던전 남음 — 서버 dungeon_left == 화면 refreshSummary 의 dungeonLeft(같은 카드 입력)."""
    from datetime import datetime, timedelta
    src = main.HTML_DASHBOARD
    node = shutil.which("node")
    ok("ST-12 node 가 있어야 한다", bool(node))
    if not node:
        return
    cut = main._fv_last_weekly_reset_utc().astimezone(main._KST_TZ)
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")   # noqa: E731
    after, before = fmt(cut + timedelta(hours=3)), fmt(cut - timedelta(hours=3))
    cards = [{"pc_id": "PC-01", "status": "hunting", "dungeon_done_at": after},            # 끝남
             {"pc_id": "PC-02", "status": "hunting", "dungeon_done_at": before},           # 지난 주 기록 → 남음
             {"pc_id": "PC-03", "status": "offline"},                                      # 기록 없음 → 남음(오프라인도)
             {"pc_id": "PC-04", "status": "no_account"},                                   # 제외
             {"pc_id": "PC-05b", "status": "idle", "dungeon_done_at": after.replace(" ", "T")},   # T 구분자도 끝남
             {"pc_id": "PC-05c", "status": "idle"}]                                        # 같은 PC 다른 계정 카드 → 따로 센다
    py = sum(1 for c in cards if c["status"] != "no_account" and not main._dungeon_done(c))
    js = ("const KST_OFF_MS=9*3600000, GAME_RESET_MS=5*3600000; const kstGameDayNum = ms => Math.floor((ms+KST_OFF_MS-GAME_RESET_MS)/86400000);"
          + _fn(src, "fmtKstTs") + chr(10) + _fn(src, "lastWeeklyReset") + chr(10) + _fn(src, "isDungeonDone") + chr(10)
          + "const cards=%s; const left=new Set(); cards.forEach(p=>{ const s=p.status||'offline'; if(!isDungeonDone(p) && s!=='no_account') left.add(p.pc_id); });"
            "console.log(JSON.stringify({n:left.size}));" % json.dumps(cards))
    d = tempfile.mkdtemp(prefix="st454d_")
    pth = os.path.join(d, "t.js")
    open(pth, "w", encoding="utf-8").write(js)
    rr = subprocess.run([node, pth], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        o = json.loads(rr.stdout.strip().splitlines()[-1])
    except Exception:
        o = None
    ok("ST-13 JS 실행", o is not None, rr.stderr[-300:])
    if o is None:
        return
    ok("ST-14 ★Py _dungeon_done 집계 == JS isDungeonDone 집계 (남음 3: 지난주·기록없음·05c)★", py == o["n"] == 3, "py=%s js=%s" % (py, o["n"]))
    rows = [dict(_row(c["pc_id"], 0, c["status"]), dungeon_done_at=c.get("dungeon_done_at")) for c in cards] + [dict(_row("PC-TEST"), dungeon_done_at=None)]
    with _Patch(rows, []):
        t = (await main._fv_build_snapshot("main"))["global"]["totals"]
    ok("ST-15 /summary totals.dungeon_left = 3 (가짜 PC·no_account 제외, 카드 단위)", t.get("dungeon_left") == 3, str(t.get("dungeon_left")))
    i = src.index("function refreshSummary(")
    ok("ST-16 refreshSummary 가 서버 dungeon_left 를 쓰고 없을 때만 화면 계산", "ssD.dungeon_left" in src[i:i + 14000])


def test_all():
    run_all([t_parity, t_totals, t_replay, t_dungeon])
    finish("test_summary_stable", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
