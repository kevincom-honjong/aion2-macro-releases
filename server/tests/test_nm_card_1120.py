# -*- coding: utf-8 -*-
"""[대시보드] 2026-11-20 카드 캐릭 이름 밑 «악몽 N/14» — 표 「악몽」 열과 같은 헬퍼·색, 값 없으면 줄 없음.

    cd updater/server && python -X utf8 tests/test_nm_card_1120.py
"""
import json
import re

from _harness import main, ok, run_all, finish   # noqa: E402
from test_js_b import _grab, _run_node, _DOM, _need_node   # noqa: E402

MIN_CHECKS = 9


async def t_nm():
    if not _need_node("NM"):
        return
    src = main.HTML_DASHBOARD
    code = _grab(src, ("esc", "escAttr", "nmTicketText", "nmTicketFull", "nmTicketWarn", "nightmareBySlot", "nightmareCardLine",
                       "buildDailyProgress"))
    i = src.index("const NIGHTMARE_TICKET_MAX")
    consts = src[i:src.index("\n", i)] + "\n"
    rows = [
        {"pc_id": "PC-01", "slot": 1, "nightmare_ticket": 5},
        {"pc_id": "PC-01", "slot": 2, "nightmare_ticket": 12},
        {"pc_id": "PC-01", "slot": 3, "nightmare_ticket": 14},
        {"pc_id": "PC-01", "slot": 4, "nightmare_ticket": None},
        {"pc_id": "PC-01", "slot": 5, "nightmare_ticket": 0},
        {"pc_id": "PC-02", "slot": 1, "nightmare_ticket": 9},
    ]
    js = _DOM + consts + code + r"""
let charTableData = %s;
function surfaceZeroSlots(){ return new Set(); }
function dpDone(c){ return !!c.completed; }
function dpMarks(){ return ''; }
function fmtKinaShort(n){ return String(n); }
const CLASS_LABEL = {};
const dp = [1,2,3,4,5,6].map(n => ({slot:n, completed:false, name:'c'+n}));
const h = buildDailyProgress(dp, null, [], {pc_id:'PC-01'});
console.log(JSON.stringify({h}));
""" % json.dumps(rows)
    try:
        o = _run_node(js)
    except Exception as e:
        ok("NM-0 node 실행", False, str(e)[:500])
        return
    h = re.sub(r"\s+", " ", o["h"])
    cells = h.split('<div class="flex flex-col items-center')[1:]
    def cell(n):
        return next((c for c in cells if ">c%d<" % n in c), "")
    ok("NM-0 여섯 칸이 그려졌다", all(cell(n) for n in range(1, 7)), str(len(cells)))
    ok("NM-1 이름 밑에 «악몽 5/14» (표와 같은 서식 nmTicketText), 이름 뒤에 온다",
       "악몽 5&#47;14" in cell(1) and cell(1).index(">c1<") < cell(1).index("악몽"), cell(1)[-200:])
    ok("NM-2 12 = 경고 호박색", "#fbbf24" in cell(2) and "악몽 12&#47;14" in cell(2), cell(2)[-160:])
    ok("NM-3 14 = 꽉참 빨강 굵게", "#f87171" in cell(3) and "악몽 14&#47;14" in cell(3), cell(3)[-160:])
    ok("NM-4 5 = 보통 회색(경고색 아님)", "#9ca3af" in cell(1) and "#fbbf24" not in cell(1), "")
    ok("NM-5 못 읽음(null)은 줄을 안 그린다", "악몽" not in cell(4), cell(4)[-120:])
    ok("NM-6 0 은 «악몽 0/14» 로 그린다(모름과 구분)", "악몽 0&#47;14" in cell(5), cell(5)[-120:])
    ok("NM-7 테이블에 없는 슬롯(6)은 줄 없음 · 다른 PC 값(PC-02 슬롯1=9)은 안 섞인다", "악몽" not in cell(6) and "9&#47;14" not in h, "")
    ok("NM-8 카드 이름 줄은 그대로(이름 칸 유지)", all(">c%d<" % n in h for n in range(1, 7)), "")


def test_all():
    run_all([t_nm])
    finish("test_nm_card_1120", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
