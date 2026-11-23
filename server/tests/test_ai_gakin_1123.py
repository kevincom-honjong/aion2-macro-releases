# -*- coding: utf-8 -*-
"""[대시보드] 2026-11-23 «AI» 오늘의 던전 — PC 번호순 정렬 + 캐릭터별 각인키나(없으면 «모름»).

    cd updater/server && python -X utf8 tests/test_ai_gakin_1123.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from _harness import main as M, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 8


def _fn(src, name):
    i = src.index("function " + name + "(")
    d, k = 0, src.index("{", i)
    while True:
        c = src[k]
        d += (c == "{") - (c == "}")
        k += 1
        if d == 0:
            return src[i:k]


def _node(js):
    d = tempfile.mkdtemp(prefix="aig_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([shutil.which("node"), p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr[-400:]
    except Exception:
        return None, (r.stderr or r.stdout)[-400:]


def _row(pc, slot, gk, daily="100/840"):
    return {"pc_id": pc, "slot": slot, "name": pc + "-" + str(slot), "power_power": 1, "odd_energy": daily,
            "collected_at": "2099-01-01T00:00:00", "gakin_kina": gk}


async def t_gk():
    src = M.HTML_DASHBOARD
    ok("AIG-1 ko·vi 라벨에 각인키나", "gakin:'각인키나'" in src and "gakin:'Kina khắc'" in src, "")
    ok("AIG-2 던전 줄에 각인키나 칸(T.gakin · aiGakinTxt(c.gk))", "${T.gakin}" in src and "aiGakinTxt(c.gk)" in src, "")
    fns = "\n".join(_fn(src, n) for n in ("aiGakinTxt", "aiPcOrder", "aiParseOdd", "collectedAtDate", "kstGameDayNum",
                                          "lastWeeklyReset", "isBeforeReset", "resetAwarePlays", "aiBuildPlan"))
    i = src.index("const SUB_DEN_MIN")
    j = src.index("const KST_OFF_MS")
    pre = src[i:src.index("\n", i)] + "\n" + src[j:src.index("\n", j)] + "\nconst isExcludedPc = () => false;\n"
    rows = [_row("PC-10", 1, 5), _row("PC-02b", 1, 1), _row("PC-02", 2, 300000), _row("PC-02", 1, 0),
            _row("PC-01", 1, None), _row("PC-02c", 1, 2), _row("PC-9", 1, ""), _row("PC-02", 3, 1234567)]
    js = (pre + fns + "\nlet charTableData=%s;\nconst plan=aiBuildPlan();\n"
          "console.log(JSON.stringify({order:plan.map(a=>a.pc),"
          "g:plan.map(a=>a.chars.map(c=>aiGakinTxt(c.gk))),"
          "t:[0,'',null,undefined,'x',5,'1234567',-3].map(aiGakinTxt)}));" % json.dumps(rows))
    g, err = _node(js)
    ok("AIG-0 node 실행", g is not None, err)
    g = g or {"order": [], "g": [], "t": []}
    ok("AIG-3 PC 숫자순 → 같은 PC 는 본카드·b·c: 01, 02, 02b, 02c, 9, 10",
       g["order"] == ["PC-01", "PC-02", "PC-02b", "PC-02c", "PC-9", "PC-10"], str(g["order"]) + err)
    ok("AIG-4 PC-02 안은 슬롯 오름차순 · 값: 0→모름 · 300,000 · 1,234,567", g["g"][1] == ["모름", "300,000", "1,234,567"], str(g["g"]))
    ok("AIG-5 값 없음(null·빈 문자열)은 «모름» — 0 으로 보이지 않는다", g["g"][0] == ["모름"] and g["g"][4] == ["모름"], str(g["g"]))
    ok("AIG-6 aiGakinTxt: 0·''·null·undefined·숫자아님·음수 = 모름, 5·'1234567' = 숫자",
       g["t"] == ["모름", "모름", "모름", "모름", "모름", "5", "1,234,567", "모름"], str(g["t"]))
    ok("AIG-7 서버 /characters 가 gakin_kina 를 내보낸다", '"gakin_kina": ch.get("gakin_kina", 0)' in __import__("inspect").getsource(M), "")


def test_all():
    run_all([t_gk])
    finish("test_ai_gakin_1123", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
