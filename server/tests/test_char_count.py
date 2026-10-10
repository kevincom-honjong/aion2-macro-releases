# -*- coding: utf-8 -*-
"""[대시보드] #451 (2026-10-05 주인님 «전광판 캐릭터수 맨날 바뀐다») — 캐릭 수는 묶음(물리 PC)별 «전 계정 지도»(acct_names)로.

  예전: 전광판 = 카드별 daily_progress 길이 합 → no_account/비워진 카드가 빠져 100 (지도 합은 149) · 카드가 비워지거나 순환으로 계정이 바뀌면 숫자가 뛴다.
  지금: 카드 배지와 전광판이 같은 함수 groupChars(list) — 지도 있으면 지도의 이름 있는 칸 수, 없는 묶음만 카드 합산 폴백.

    cd updater/server && python -X utf8 tests/test_char_count.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from _harness import main, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 12


def _fn(src, name):
    i = src.index("function " + name + "(")
    d, k = 0, src.index("{", i)
    while True:
        c = src[k]
        d += (c == "{") - (c == "}")
        k += 1
        if d == 0:
            return src[i:k]


async def t_helper():
    src = main.HTML_DASHBOARD
    node = shutil.which("node")
    ok("CC-0 node 가 있어야 한다", bool(node))
    if not node:
        return
    names = lambda *per: {str(i + 1): {str(j + 1): "c%d_%d" % (i, j) for j in range(n)} for i, n in enumerate(per)}   # noqa: E731
    js = _fn(src, "groupChars") + r"""
const acctNumOf = id => ({b:2, c:3, d:4})[(id||'').slice(-1)] || 1;
const M = %s;                       // 6 + 2 + 2 = 10 캐릭 지도(PC-03 실측 모양)
const o = {};
// 지도가 있는 묶음: 카드 일부가 비워져도(no_account · daily_progress 없음) 같은 10
o.full = groupChars([{pc_id:'PC-03', acct_names:M, daily_progress:[1,1,1,1,1,1]}, {pc_id:'PC-03b', acct_names:M, daily_progress:[1,1]}, {pc_id:'PC-03c', acct_names:M, daily_progress:[1,1]}]);
o.blankedCards = groupChars([{pc_id:'PC-03', acct_names:M, status:'no_account', daily_progress:[], chars:[]}, {pc_id:'PC-03b', acct_names:M, status:'no_account', daily_progress:[]}]);
o.oneCard = groupChars([{pc_id:'PC-03b', acct_names:M, daily_progress:[1,1]}]);
// 카드마다 지도의 일부만 들고 있어도(계정 키 합침) 합친다
o.merged = groupChars([{pc_id:'PC-05', acct_names:{'1':{'1':'a','2':'b'}}}, {pc_id:'PC-05b', acct_names:{'2':{'1':'c'}}}]);
// 빈 이름은 세지 않는다
o.blankNames = groupChars([{pc_id:'PC-06', acct_names:{'1':{'1':'a','2':'  ','3':''}}}]);
// 지도가 없는 묶음 → 카드 합산(계정번호별 최대) 폴백, fromMap=false
o.fallback = groupChars([{pc_id:'PC-07', daily_progress:[1,1,1]}, {pc_id:'PC-07b', daily_progress:[1,1]}, {pc_id:'PC-07b', daily_progress:[1]}]);
o.fallbackChars = groupChars([{pc_id:'PC-08', chars:['x','y']}]);
// 지도도 카드 정보도 없다 → 0
o.none = groupChars([{pc_id:'PC-09', status:'no_account'}]);
o.empty = groupChars([]);
// ★정지(OUT) 슬롯 제외(2026-10-05 «지금 아웃시킨거 제외하고»)★
o.outOne = groupChars([{pc_id:'PC-03', acct_names:M, banned_slots:[2]}, {pc_id:'PC-03b', acct_names:M, banned_slots:[2]}]);   // 계정2(2명) OUT → 8
o.outUnion = groupChars([{pc_id:'PC-03', acct_names:M, banned_slots:[2]}, {pc_id:'PC-03c', acct_names:M, banned_slots:['3']}]);   // 카드마다 일부만 들고 있어도 합집합, 문자열·정수 섞임 → 6
o.outAll = groupChars([{pc_id:'PC-15', acct_names:M, banned_slots:[1,2,3]}, {pc_id:'PC-15b', acct_names:M, banned_slots:[1,2,3], status:'no_account'}]);   // PC 통째로 OUT → 0, 폴백으로 새지 않는다
o.outAcct1 = groupChars([{pc_id:'PC-16', acct_names:M, banned_slots:[1]}]);                                                       // 계정1(접미사 없음)=키 "1"
o.outFallback = groupChars([{pc_id:'PC-07', daily_progress:[1,1,1], banned_slots:[2]}, {pc_id:'PC-07b', daily_progress:[1,1], banned_slots:[2]}]);  // 지도 없음 + 계정2 OUT → 3, out 2
o.outFallbackAll = groupChars([{pc_id:'PC-17b', daily_progress:[1,1], banned_slots:[2]}]);
// 전광판: 묶음별 합 (실측 모양 — 비워진 묶음이 있어도 지도 합)
const groups = {A:[{pc_id:'PC-01', acct_names:M, status:'no_account'}], B:[{pc_id:'PC-02', acct_names:M, daily_progress:[1,1,1,1,1,1]}], C:[{pc_id:'PC-04', daily_progress:[1,1]}]};
let n = 0, fb = 0, mp = 0; Object.values(groups).forEach(l => { const g = groupChars(l); n += g.n; if (g.fromMap) mp++; else if (g.n > 0) fb++; });
o.board = {n, mp, fb};
// 전광판(OUT 반영): 계정2 OUT 묶음(8) + 통째 OUT 묶음(0) + 정상 묶음(10) = 18, 뺀 수 2+10
let n2 = 0, o2 = 0; [[{pc_id:'PC-03', acct_names:M, banned_slots:[2]}], [{pc_id:'PC-15', acct_names:M, banned_slots:[1,2,3]}], [{pc_id:'PC-05', acct_names:M}]].forEach(l => { const g = groupChars(l); n2 += g.n; o2 += g.out; });
o.boardOut = {n: n2, out: o2};
console.log(JSON.stringify(o));
""" % json.dumps(names(6, 2, 2))
    d = tempfile.mkdtemp(prefix="cc451_")
    pth = os.path.join(d, "t.js")
    open(pth, "w", encoding="utf-8").write(js)
    rr = subprocess.run([node, pth], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        o = json.loads(rr.stdout.strip().splitlines()[-1])
    except Exception:
        o = None
    ok("CC-1 node 실행", o is not None, rr.stderr[-400:])
    if o is None:
        return
    ok("CC-2 ★지도가 있으면 지도의 이름 있는 칸 수(10)·계정 3개 — 카드 일부/전부가 비워져도 같다★",
       (o["full"]["n"], o["full"]["accts"], o["full"]["fromMap"], o["full"]["out"]) == (10, 3, True, 0) and (o["blankedCards"]["n"], o["blankedCards"]["accts"]) == (10, 3)
       and o["oneCard"]["n"] == 10, str((o["full"], o["blankedCards"], o["oneCard"])))
    ok("CC-3 카드마다 지도 일부만 들고 있어도 합쳐 센다 · 빈 이름은 안 센다",
       (o["merged"]["n"], o["merged"]["accts"], o["merged"]["fromMap"]) == (3, 2, True) and o["blankNames"]["n"] == 1, str((o["merged"], o["blankNames"])))
    ok("CC-4 ★지도 없는 묶음만 카드 합산 폴백(계정번호별 최대: 3+2=5), fromMap=false★",
       (o["fallback"]["n"], o["fallback"]["accts"], o["fallback"]["fromMap"]) == (5, 2, False) and o["fallbackChars"]["n"] == 2, str((o["fallback"], o["fallbackChars"])))
    ok("CC-5 지도도 카드 정보도 없으면 0", o["none"]["n"] == 0 and o["none"]["fromMap"] is False and o["empty"]["n"] == 0)
    ok("CC-6 전광판 합: 지도 묶음 둘(10+10) + 폴백 묶음(2) = 22 · 출처 집계 맵 2대/폴백 1대", o["board"] == {"n": 22, "mp": 2, "fb": 1}, str(o["board"]))
    ok("CC-9 ★정지(OUT) 계정은 세지 않는다: 계정2(2명) OUT → 8 · 뺀 수 2 · 카드마다 일부만 들고 있어도 합집합(문자열·정수 섞임) → 6 · 계정1 OUT → 4★",
       (o["outOne"]["n"], o["outOne"]["out"], o["outOne"]["accts"]) == (8, 2, 2) and (o["outUnion"]["n"], o["outUnion"]["out"]) == (6, 4)
       and (o["outAcct1"]["n"], o["outAcct1"]["out"]) == (4, 6), str((o["outOne"], o["outUnion"], o["outAcct1"])))
    ok("CC-10 ★PC 통째로 OUT 이면 0(폴백으로 새지 않는다 · 지도 있음 표시 유지)★",
       (o["outAll"]["n"], o["outAll"]["fromMap"], o["outAll"]["out"]) == (0, True, 10), str(o["outAll"]))
    ok("CC-11 폴백 경로에도 같은 규칙(지도 없음 + 계정2 OUT → 3, 뺀 2 / 유일한 카드가 OUT → 0)",
       (o["outFallback"]["n"], o["outFallback"]["out"], o["outFallback"]["fromMap"]) == (3, 2, False)
       and (o["outFallbackAll"]["n"], o["outFallbackAll"]["out"]) == (0, 2), str((o["outFallback"], o["outFallbackAll"])))
    ok("CC-12 전광판 합계에 OUT 반영: 8 + 0 + 10 = 18, 뺀 수 12", o["boardOut"] == {"n": 18, "out": 12}, str(o["boardOut"]))
    # 배선
    i = src.index("function refreshSummary(")
    body = src[i:i + 9000]
    ok("CC-7 ★전광판(refreshSummary)과 카드 배지가 같은 groupChars 를 쓴다(카드 합산 직접 계산 제거)★",
       "groupChars(list)" in body and "n += dp.length ||" not in body and "const _cg = groupChars(s.list);" in src, "")
    ok("CC-8 툴팁에 출처와 폴백 PC 수 · completedChars 계산은 그대로",
       "출처: 계정별 캐릭 명단(info.txt)" in body and "카드 합산으로 센 PC" in body and "정지(OUT) 계정 캐릭 ${c._charSrc.outN}명 제외" in body
       and "c.completedChars += dp.filter(dpDone).length;" in body)


def test_all():
    run_all([t_helper])
    finish("test_char_count", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
