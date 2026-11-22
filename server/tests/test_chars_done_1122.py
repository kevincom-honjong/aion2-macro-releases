# -*- coding: utf-8 -*-
"""[대시보드] 2026-11-22 주인님 «완료 69 인데 세어보니 13 남았다» — 완료(분자)가 분모(캐릭 수)와 같은 모집단만 센다.
실측 원인: 이름 지도(acct_names)에 없는 daily_progress 슬롯·정지(OUT) 계정 슬롯이 분자에만 들어가 분모를 넘었다.

    cd updater/server && python -X utf8 tests/test_chars_done_1122.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from _harness import main, ok, run_all, finish   # noqa: E402

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


def _dp(*done_flags):
    return [{"slot": i + 1, "completed": f, "today": True} for i, f in enumerate(done_flags)]


def _card(pc, names, dp, banned=None):
    return {"pc_id": pc, "acct_names": names, "banned_slots": banned or [], "daily_progress": dp}


FIX = {
    # PC-23 형: 지도엔 2명인데 daily_progress 5개 전부 완료 → 2
    "extra_slots": [_card("PC-23", {"1": {"1": "a", "2": "b"}}, _dp(True, True, True, True, True))],
    # 정지 계정 카드의 완료는 뺀다 + 활성 계정은 센다
    "banned": [_card("PC-20", {"1": {"1": "a", "2": "b"}, "2": {"1": "c"}}, _dp(True, True), banned=[2]),
               _card("PC-20b", {"1": {"1": "a", "2": "b"}, "2": {"1": "c"}}, _dp(True), banned=[2])],
    # 정상: 지도 6, 완료 3 → 3 (슬롯이 이름 있는 칸이면 그대로)
    "normal": [_card("PC-01", {"1": {"1": "a", "2": "b", "3": "c", "4": "d", "5": "e", "6": "f"}}, _dp(True, True, True))],
    # 지도 없음(폴백): 전부 센다
    "fallback": [_card("PC-09", None, _dp(True, False, True))],
    # 지도 있고 슬롯이 이름 칸 중 일부 빈칸(이름 없는 3번)
    # 실측 PC-04: 활성 카드(새 명단 5명) + 멈춘 뒷카드(옛 명단 4명, 더 이른 시각) — 뒷카드가 카드 순서상 뒤여도 새 명단이 이긴다
    "stale_back": [dict(_card("PC-04", {"1": {"1": "a", "2": "b", "3": "c", "4": "d", "5": "e"}}, _dp(True, True, True, True, True)), _updated_at="2026-11-22T11:16:01"),
                   dict(_card("PC-04b", {"1": {"1": "a", "2": "c", "3": "d", "4": "b"}}, []), _updated_at="2026-09-30T14:51:15")],
    "blank_name": [_card("PC-04", {"1": {"1": "a", "2": "b", "3": "", "4": "d"}}, _dp(True, True, True, True, True))],
}
WANT = {"extra_slots": 2, "banned": 2, "normal": 3, "fallback": 2, "blank_name": 3, "stale_back": 5}


def _node(js):
    d = tempfile.mkdtemp(prefix="cd_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([shutil.which("node"), p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr[-400:]
    except Exception:
        return None, (r.stderr or r.stdout)[-400:]


async def t_done():
    src = main.HTML_DASHBOARD
    py = {k: main._fv_group_done(v) for k, v in FIX.items()}
    ok("CD-1 서버: 지도에 없는 완료 슬롯은 안 센다(2/5 → 2)", py["extra_slots"] == WANT["extra_slots"], str(py))
    ok("CD-2 서버: 정지(OUT) 계정 완료는 뺀다", py["banned"] == WANT["banned"], str(py))
    ok("CD-3 서버: 정상·폴백·빈 이름칸", py["normal"] == 3 and py["fallback"] == 2 and py["blank_name"] == 3, str(py))
    ok("CD-4 서버: 완료 ≤ 캐릭 수(분모) — 어떤 입력도 분자가 분모를 못 넘는다",
       all(main._fv_group_done(v) <= max(main._fv_group_chars(v)["n"], 0) or not main._fv_group_chars(v)["from_map"] for v in FIX.values()), "")
    js = "const dpDone = c => !!(c && c.completed) && c.today !== false;\n" + _fn(src, "groupAmap") + "\n" + _fn(src, "groupChars") + "\n" + _fn(src, "groupDone") + "\n" \
        "const acctNumOf = id => ({b:2, c:3, d:4})[(id||'').slice(-1)] || 1;\n" \
        "const FIX=%s;console.log(JSON.stringify(Object.fromEntries(Object.entries(FIX).map(([k,v])=>[k,groupDone(v)]))));\n" % json.dumps(FIX)
    g, err = _node(js)
    ok("CD-8 ★멈춘 뒷카드의 낡은 명단이 활성 카드 명단을 덮지 않는다★ (분모 5·완료 5, 옛 순서면 4·4)",
       main._fv_group_chars(FIX["stale_back"])["n"] == 5 and py["stale_back"] == 5, str(main._fv_group_chars(FIX["stale_back"])))
    ok("CD-5 node 실행", g is not None, err)
    ok("CD-6 ★파이썬 _fv_group_done == JS groupDone★", g == py, str(g) + " vs " + str(py))
    ok("CD-7 화면 폴백이 묶음 루프에서 groupDone 으로 센다(카드별 dp.filter(dpDone) 합산 아님)",
       "doneN += groupDone(list)" in src and "c.completedChars += dp.filter(dpDone).length" not in src, "")


def test_all():
    run_all([t_done])
    finish("test_chars_done_1122", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
