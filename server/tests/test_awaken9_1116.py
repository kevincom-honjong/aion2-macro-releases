# -*- coding: utf-8 -*-
"""[대시보드] 2026-11-16 게임 업데이트(주인님) — 각성전 티켓이 계정 공용 9/9 (예전 캐릭당 3).
매크로는 계정 값을 daily_ticket 처럼 모든 슬롯에 같은 값으로 보낸다 → 합산하지 않고 카드(PC)당 한 번(가장 늦게 읽은 값).
옛 행(0~3, 캐릭별)도 에러 없이 읽힌다.

    cd updater/server && python -X utf8 tests/test_awaken9_1116.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from _harness import main as M, db, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 10
OLD = "2026-09-01T00:00:00"      # 확실히 지난 수요일 05시 이전
NEW = "2099-01-01T00:00:00"


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
    d = tempfile.mkdtemp(prefix="aw9_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([shutil.which("node"), p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), r.stderr[-300:]
    except Exception:
        return None, (r.stderr or r.stdout)[-300:]


async def t_js():
    src = M.HTML_DASHBOARD
    ok("AW9-1 JS 상수 AWAKEN_MAX=9 · 서버 _FV_AWAKEN_MAX=9 (같은 값)", "const AWAKEN_MAX = 9;" in src and M._FV_AWAKEN_MAX == 9, "")
    ok("AW9-2 화면에 옛 «/3» 각성 표기·>=3 판정이 남지 않았다",
       "awakening_ticket+'/3'" not in src and "awakening_ticket >= 3" not in src and "awakening_ticket}/3" not in src, "")
    rows = [
        {"pc_id": "PC-A", "slot": 1, "awakening_ticket": 4, "collected_at": NEW},
        {"pc_id": "PC-A", "slot": 2, "awakening_ticket": 4, "collected_at": NEW},
        {"pc_id": "PC-A", "slot": 3, "awakening_ticket": 4, "collected_at": NEW},
        {"pc_id": "PC-B", "slot": 1, "awakening_ticket": 0, "collected_at": NEW},
        {"pc_id": "PC-B", "slot": 2, "awakening_ticket": 9, "collected_at": OLD},     # 낡은 슬롯 — 가장 늦게 읽은 0 이 계정 값
        {"pc_id": "PC-C", "slot": 1, "awakening_ticket": "", "collected_at": NEW},     # 못 읽음
        {"pc_id": "PC-D", "slot": 1, "awakening_ticket": 2, "collected_at": ""},       # 옛 행(시각 없음) 에러 없음
        {"pc_id": "PC-D", "slot": 2, "awakening_ticket": 3, "collected_at": ""},
    ]
    js = "\n".join([_fn(src, "collectedAtDate"), _fn(src, "awakenByPc")]) + "\nconst rows=%s;\nconst m=awakenByPc(rows);\nconsole.log(JSON.stringify([...m.entries()]));" % json.dumps(rows)
    got, err = _node(js)
    got = dict(got or [])
    ok("AW9-3 같은 값 3슬롯(4,4,4)은 합 12 가 아니라 계정 4", got.get("PC-A") == 4, str(got) + err)
    ok("AW9-4 낡은 슬롯 9 보다 가장 늦게 읽은 0 이 계정 값", got.get("PC-B") == 0, str(got))
    ok("AW9-5 못 읽은 카드(빈 문자열)는 키가 없다(0 과 모름을 가름)", "PC-C" not in got, str(got))
    ok("AW9-6 옛 행(시각 없음·값 2,3)도 에러 없이 읽는다", got.get("PC-D") == 3, str(got))
    t = _fn(src, "isAwakenDone")
    ok("AW9-7 완료 스탬프 문구가 계정 0/9", "계정 각성 티켓 0/9" in src and t.count("parseInt(r.awakening_ticket)===0") == 1, "")


async def t_server():
    def info(pid, chars, ca=NEW):
        return {"pc_id": pid, "collected_at": ca, "chars": chars}
    sess = M.new_session("main")                      # noqa: F841
    ok("AW9-8 서버 _fv_ticket_reset_aware(리셋 전 낮은 값)는 full=9 로 올린다 · 이미 높으면 그대로",
       M._fv_ticket_reset_aware(OLD, 1, 9) == 9 and M._fv_ticket_reset_aware(NEW, 1, 9) == 1 and M._fv_ticket_reset_aware(OLD, 9, 9) == 9, "")
    ok("AW9-9 /summary 합계에 슬롯 합이 없다 — 코드가 최대 한 번(_aw_best)으로 센다",
       "_aw_best" in open(M.__file__, encoding="utf-8").read() and 'agg["awakening_ticket"] += ' not in open(M.__file__, encoding="utf-8").read(), "")


async def t_agg():
    """실제 _fv_char_agg 에 3슬롯 같은 값을 넣어 합산 안 하는지(db 조회를 가짜로 바꿔 끼운다)."""
    import types
    infos = [{"pc_id": "PC-T1", "collected_at": NEW, "chars": [
        {"slot": 1, "awakening_ticket": 7}, {"slot": 2, "awakening_ticket": 7}, {"slot": 3, "awakening_ticket": 7}]},
        {"pc_id": "PC-T2", "collected_at": NEW, "chars": [{"slot": 1, "awakening_ticket": 1}, {"slot": 2, "awakening_ticket": 1}]},
        {"pc_id": "PC-T3", "collected_at": OLD, "chars": [{"slot": 1, "awakening_ticket": 1}]}]   # 리셋 전 옛 값 → 9
    keep = M.get_all_char_info

    async def fake(*a, **k):
        return infos
    M.get_all_char_info = fake
    try:
        agg = await M._fv_char_agg("main")
    finally:
        M.get_all_char_info = keep
    ok("AW9-10 _fv_char_agg: 같은 값 3슬롯 7,7,7 → 7(합 21 아님) · 1,1 → 1 · 리셋 전 옛 1 → 9",
       (agg.get("PC-T1") or {}).get("awakening_ticket") == 7 and (agg.get("PC-T2") or {}).get("awakening_ticket") == 1
       and (agg.get("PC-T3") or {}).get("awakening_ticket") == 9, str({k: v.get("awakening_ticket") for k, v in agg.items()}))


def test_all():
    run_all([t_js, t_server, t_agg])
    finish("test_awaken9_1116", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
