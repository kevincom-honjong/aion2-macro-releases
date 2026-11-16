# -*- coding: utf-8 -*-
"""[대시보드] 2026-11-17 «AI» 오늘의 던전 패널 — 파워 K 표기·색, 슬롯 오름차순, 정복 N/62·초월 N/14.

    cd updater/server && python -X utf8 tests/test_ai_dungeon_1117.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from _harness import main as M, ok, run_all, finish   # noqa: E402

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


def _node(js):
    d = tempfile.mkdtemp(prefix="aid_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([shutil.which("node"), p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr[-400:]
    except Exception:
        return None, (r.stderr or r.stdout)[-400:]


async def t_ai():
    src = M.HTML_DASHBOARD
    ok("AID-1 서버 /characters 행이 conquest_plays·transcend_plays 를 내보낸다(없으면 null)",
       '"conquest_plays": ch.get("conquest_plays")' in __import__("inspect").getsource(M)
       and '"transcend_plays": ch.get("transcend_plays")' in __import__("inspect").getsource(M), "")
    ok("AID-2 화면 라벨 ko·vi 둘 다 정복·초월", "conquest:'정복', transcend:'초월'" in src and "conquest:'Chinh phục'" in src, "")
    fns = "\n".join(_fn(src, n) for n in ("aiPowerK", "aiCountFmt", "aiParseOdd", "collectedAtDate", "aiBuildPlan"))
    i = src.index("const SUB_DEN_MIN")
    pre = src[i:src.index("\n", i)] + "\n"
    rows = [
        {"pc_id": "PC-A", "slot": 3, "name": "c", "power_power": 400912, "odd_energy": "300/840", "collected_at": "2099-01-01T00:00:00", "conquest_plays": 10, "transcend_plays": 3},
        {"pc_id": "PC-A", "slot": 1, "name": "a", "power_power": 399999, "odd_energy": "100/840", "collected_at": "2099-01-01T00:00:00", "conquest_plays": 10, "transcend_plays": 3},
        {"pc_id": "PC-A", "slot": 2, "name": "b", "power_power": 0, "odd_energy": "200/840", "collected_at": "2099-01-01T00:00:00", "conquest_plays": 10, "transcend_plays": 3},
        {"pc_id": "PC-B", "slot": 1, "name": "x", "power_power": 1, "odd_energy": "1/840", "collected_at": "2099-01-01T00:00:00"},
        {"pc_id": "PC-C", "slot": 1, "name": "y", "power_power": 1, "odd_energy": "1/840", "collected_at": "2099-01-01T00:00:00", "conquest_plays": 63, "transcend_plays": 14},
        {"pc_id": "PC-C", "slot": 2, "name": "z", "power_power": 1, "odd_energy": "1/840", "collected_at": "2098-01-01T00:00:00", "conquest_plays": 5, "transcend_plays": 1},
        {"pc_id": "PC-D", "slot": 1, "name": "w", "power_power": 1, "odd_energy": "1/840", "collected_at": "2099-01-01T00:00:00", "conquest_plays": "", "transcend_plays": None},
    ]
    js = (pre + fns + "\nlet charTableData=%s;\nconst plan=aiBuildPlan();\nconst by={};plan.forEach(a=>by[a.pc]=a);\n"
          "console.log(JSON.stringify({slots:by['PC-A'].chars.map(c=>c.slot),cq:Object.fromEntries(Object.entries(by).map(([k,a])=>[k,[a.cq,a.tr]])),"
          "k:[400912,400000,399999,0,'',1500].map(aiPowerK),n:[[0,62],[62,62],[63,62],[14,14],[15,14],['',62],[null,14],[undefined,14],['x',14]].map(c=>aiCountFmt(c[0],c[1]))}));" % json.dumps(rows))
    g, err = _node(js)
    ok("AID-0 node 실행", g is not None, err)
    g = g or {"slots": [], "cq": {}, "k": [{}] * 6, "n": [{}] * 9}
    ok("AID-3 슬롯 오름차순(1,2,3) — 에너지 순이 아님", g["slots"] == [1, 2, 3], str(g["slots"]) + err)
    k = g["k"]
    ok("AID-4 400,912 → «400K» 노랑", k[0] == {"txt": "400K", "color": "#facc15"}, str(k[0]))
    ok("AID-5 400,000 경계는 노랑(≥400K), 399,999 → «399K» 빨강(내림)", k[1]["color"] == "#facc15" and k[2] == {"txt": "399K", "color": "#f87171"}, str(k[1:3]))
    ok("AID-6 파워 0·빈 값은 «–»(빨강 아님)", k[3]["txt"] == "–" and k[4]["txt"] == "–" and k[3]["color"] != "#f87171", str(k[3:5]))
    ok("AID-7 1,500 → «1K» 빨강", k[5] == {"txt": "1K", "color": "#f87171"}, str(k[5]))
    n = g["n"]
    ok("AID-8 N/62·N/14 서식, 한도 이하(=한도 포함)는 빨강 아님", n[0]["txt"] == "0/62" and n[1]["txt"] == "62/62" and n[1]["color"] != "#f87171" and n[3]["color"] != "#f87171", str(n[:4]))
    ok("AID-9 한도 초과 63/62·15/14 빨강", n[2] == {"txt": "63/62", "color": "#f87171"} and n[4]["color"] == "#f87171", str(n[2:5]))
    ok("AID-10 모름('' null undefined 숫자아님) = «—»", all(x["txt"] == "—" for x in n[5:9]), str(n[5:9]))
    cq = g["cq"]
    ok("AID-11 계정 값 한 번(슬롯마다 더하지 않음): PC-A 정복 10·초월 3", cq.get("PC-A") == [10, 3], str(cq))
    ok("AID-12 필드 없는 PC(옛 데이터)는 null → «—» 로 그려진다", cq.get("PC-B") == [None, None] and cq.get("PC-D") == [None, None], str(cq))
    ok("AID-13 가장 늦게 읽은 캐릭의 값(PC-C 슬롯1 63, 낡은 슬롯2 5 아님)", cq.get("PC-C") == [63, 14], str(cq))


def test_all():
    run_all([t_ai])
    finish("test_ai_dungeon_1117", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
