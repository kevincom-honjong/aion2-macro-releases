# -*- coding: utf-8 -*-
"""[대시보드] 2026-11-19 «AI» 오늘의 던전 — 에너지 꽉참(840/840) 진한 파랑 + 성역(루드라) 티켓 칸(모르면 «?»).
패널 HTML 을 실제 renderAiPlan 으로 그려 DOM 문자열을 본다.

    cd updater/server && python -X utf8 tests/test_ai_energy_1119.py
"""
import json
import re

from _harness import main, ok, run_all, finish   # noqa: E402
from test_js_b import _grab, _run_node, _DOM, _need_node   # noqa: E402

MIN_CHECKS = 9
NEW = "2099-01-01T00:00:00"


async def t_energy():
    if not _need_node("AE"):
        return
    src = main.HTML_DASHBOARD
    code = _grab(src, ("esc", "aiParseOdd", "collectedAtDate", "kstGameDayNum", "lastWeeklyReset", "isBeforeReset",
                       "resetAwarePlays", "aiPowerK", "aiEnergyFull", "aiSancTxt", "aiGakinTxt", "aiClsHtml", "aiCell", "aiPcOrder", "aiCountFmt", "aiBuildPlan", "renderAiPlan"))
    i = src.index("const AI_T = ")
    ai_t = src[i:src.index("};\n", i) + 2]
    rows = [
        {"pc_id": "PC-01", "slot": 1, "name": "full", "power_power": 1, "odd_energy": "840/840", "sanctuary": "2/3", "collected_at": NEW},
        {"pc_id": "PC-01", "slot": 2, "name": "part", "power_power": 1, "odd_energy": "100/840", "sanctuary": "", "collected_at": NEW},
        {"pc_id": "PC-01", "slot": 3, "name": "reset", "power_power": 1, "odd_energy": "839/840", "sanctuary": "초기화됨", "collected_at": NEW},
        {"pc_id": "PC-02", "slot": 1, "name": "sub800", "power_power": 1, "odd_energy": "800/800", "collected_at": NEW},
    ]
    js = _DOM + ai_t + "\nconst SUB_DEN_MIN = 700;\nconst KST_OFF_MS = 9 * 3600000, GAME_RESET_MS = 5 * 3600000;\n" + code + r"""
let aiLang = 'ko', aiFilter = 'dg', aiDone = {keys: []}; const AI_CLS_COLOR = {}; let aiPwFilter = ''; const AI_PW_BANDS = []; function aiPwMatch(){ return true; }
function baseId(x){ return x; } function acctTagSpread(){ return ''; } function aiAcctInfo(){ return ''; } function isExcludedPc(){ return false; }
let charTableData = %s;
renderAiPlan();
console.log(JSON.stringify({h: els['ai-body'].innerHTML}));
""" % json.dumps(rows)
    try:
        o = _run_node(js)
    except Exception as e:
        ok("AE-0 node 실행", False, str(e)[:400])
        return
    h = re.sub(r"\s+", " ", o["h"])
    parts = h.split('<div class="flex items-center gap-2 px-3 py-1.5"')[1:]
    rowsh = {n: p for n in ("full", "part", "reset", "sub800") for p in parts if ">" + n + "<" in p}
    ok("AE-0 네 줄 다 그려졌다", len(rowsh) == 4, str(list(rowsh)))
    f = rowsh.get("full", "")
    ok("AE-1 840/840 → 진한 파랑(#2563eb) 굵게 «840/840»", 'color:#2563eb' in f and '>840/840<' in f, f[-260:])
    ok("AE-2 부분(100/840)은 파랑 아님·옛 청록 유지", '#2563eb' not in rowsh.get("part", "") and 'text-cyan-300">100<' in rowsh.get("part", ""), rowsh.get("part", "")[-260:])
    ok("AE-3 839/840 은 꽉참 아님", '#2563eb' not in rowsh.get("reset", ""), "")
    ok("AE-4 800/800(분모 800 구독)도 꽉참 = 파랑", 'color:#2563eb' in rowsh.get("sub800", ""), rowsh.get("sub800", "")[-200:])
    ok("AE-5 성역 칸: 값 «2/3» 그대로(성역 열과 같은 값)", '성역 <b class="text-amber-200">2&#47;3</b>' in f, f[-200:])
    ok("AE-6 성역 못 읽음(빈 값·필드 없음) = «?» (0 아님)",
       '성역 <b class="text-amber-200">?</b>' in rowsh.get("part", "") and '성역 <b class="text-amber-200">?</b>' in rowsh.get("sub800", ""), "")
    ok("AE-7 리셋된 성역은 «초기화됨» 그대로(표 열과 동일 규칙)", '>초기화됨<' in rowsh.get("reset", ""), "")
    ok("AE-8 AI_T 두 언어에 성역 라벨", "sanc:'성역'" in src and "sanc:'Thánh địa'" in src, "")


def test_all():
    run_all([t_energy])
    finish("test_ai_energy_1119", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
