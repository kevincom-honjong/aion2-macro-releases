# -*- coding: utf-8 -*-
"""[대시보드] #458 (2026-10-16 주인님 «11번 정지 out … 정지됐는데 왜 대시보드에 활성화되잇는거야») — 꺼진 PC 의 순환 «활성» 표시.

  실측(운영 /status 20:05): PC-11 status=offline · _macro_silent_s 1202 · ★_rot="hunting"(41048초)★. 사냥 단계의 `if not active: return`
  (사고 611: 시계로 안 푼다)이 «매크로가 죽음» 을 못 본 척하고, #276 정지 가드는 active 가 있어야 켜져서 정지(OUT) 등록 뒤에도 무장이 남았다.
  카드는 보라색 «🔁 순환» 을 그대로 그렸다.
  지금: ① 엔진 — 이 PC 의 모든 카드가 정지 슬롯이고 active 가 없으면 조용히 푼다(사실로 풀지 시계로 안 푼다)
        ② 카드 — offline·계정없음·다른계정 / 정지 슬롯에 매크로 없음 / 무보고 N분이면 회색 «보고 없음» · 깜빡임 끔.

    cd updater/server && python -X utf8 tests/test_rot_offline_458.py
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from _harness import main as M, db, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 12


def _node(js):
    node = shutil.which("node")
    d = tempfile.mkdtemp(prefix="ro458_")
    pth = os.path.join(d, "t.js")
    open(pth, "w", encoding="utf-8").write(js)
    rr = subprocess.run([node, pth], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(rr.stdout.strip().splitlines()[-1]), rr.stderr[-500:]
    except Exception:
        return None, (rr.stderr or rr.stdout)[-500:]


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _st(stage="hunting", task=""):
    st = {"stage": stage, "since": M._rot_now(), "armed_at": M._rot_now() - 9999, "day": M._kst_today_key(),
          "full": False, "hops": 0, "visits": {}, "sent_at": M._rot_now()}
    if task:
        st["task"] = task
    return st


def _card(pid, status="offline", **kw):
    c = {"pc_id": pid, "status": status, "last_active": _now_iso(), "daily_progress": [], "acct_ids": {"1": "a", "2": "b"}}
    c.update(kw)
    return c


async def _run_step(base, stage, pcs, task=""):
    """_rot_step_pc 를 한 번 돌리고 (무장이 남았나, _rot_say 로 나간 글, 큐에 쌓인 명령 수 변화) 를 돌려준다."""
    said = []
    o_say, o_save = M._rot_say, M._rot_save

    async def _say(t, pc, text, **kw):
        said.append((text, bool(kw.get("routine"))))

    async def _save(force=False):
        pass
    M._rot_say, M._rot_save = _say, _save
    key = M.ns("main", base)
    st = _st(stage, task)
    if stage == "switching":
        st.update(target="b", sent_at=M._rot_now())    # 정상 모양의 전환 중 무장(목표가 비면 «손상» 가드가 먼저 선다)
    M._ROT[key] = st
    M._ROT_STEP_CUR.update(key=key, st=st)
    before = len(await db.get_pending_commands(key) or [])
    exc = ""
    try:
        await M._rot_step_pc("main", base, st, pcs)
    except Exception as e:                           # noqa: BLE001
        exc = "EXC %s" % e
    finally:
        M._ROT_STEP_CUR.update(key=None, st=None)
        M._rot_say, M._rot_save = o_say, o_save
    after = len(await db.get_pending_commands(key) or [])
    armed = M._ROT.get(key) is st
    M._ROT.pop(key, None)
    return armed, said, after - before, exc


async def t_engine():
    keep = set(M.BANNED_ACCTS)
    try:
        M.BANNED_ACCTS.clear()
        M.BANNED_ACCTS.add(M.ns("main", "PC-T58"))                       # 계정 1 만 있는 PC — 정지
        pcs = [_card("PC-T58", "no_account", banned=True)]
        for stage, task in (("hunting", ""), ("tasking", "corridor")):
            armed, said, dq, exc = await _run_step("PC-T58", stage, pcs, task)
            ok("RO-%s %s 인데 모든 카드가 정지 슬롯 · 매크로 없음 → ★조용히 푼다★ (무장 제거 · 명령 0건 · routine 알림 한 줄)" %
               ("1" if stage == "hunting" else "2", stage),
               (not armed) and dq == 0 and not exc and len(said) == 1 and said[0][1] is True and "정지(OUT)" in said[0][0], str((armed, said, dq, exc)))
        armed, said, dq, exc = await _run_step("PC-T58", "switching", pcs)
        ok("RO-3 전환 중(switching)엔 #458 가드가 안 끼어든다(다른 계정으로 가는 중일 수 있다)",
           not any("#458" in t for t, _ in said) and not exc, str((armed, said, exc)))
        # 정지가 아닌 꺼진 PC 는 옛 그대로(사고 611 — 시계로 안 푼다)
        pcs2 = [_card("PC-T59", "offline")]
        armed, said, dq, exc = await _run_step("PC-T59", "hunting", pcs2)
        ok("RO-4 ★정지가 아닌 꺼진 PC 는 무장이 그대로 남는다★(사고 611 — 사실 없이 시계로 풀지 않는다)", armed and not said and dq == 0, str((armed, said, exc)))
        # 다계정: 1번 정지 · 2번은 정지 아님(꺼져 있음) → 돌아올 수 있으니 유지
        M.BANNED_ACCTS.add(M.ns("main", "PC-T60"))
        pcs3 = [_card("PC-T60", "no_account", banned=True), _card("PC-T60b", "offline")]
        armed, said, dq, exc = await _run_step("PC-T60", "hunting", pcs3)
        ok("RO-5 다계정 PC 는 정지 아닌 카드가 하나라도 있으면 유지", armed and not said, str((armed, said, exc)))
        # 매크로가 살아 정지 슬롯에 있으면 옛 가드(경보 메시지)가 그대로 켜진다
        M.BANNED_ACCTS.add(M.ns("main", "PC-T61"))
        pcs4 = [dict(_card("PC-T61", "idle"), banned=True, macro_live=True)]
        armed, said, dq, exc = await _run_step("PC-T61", "hunting", pcs4)
        ok("RO-6 매크로가 정지 슬롯에서 살아 있으면 옛 #276 가드(⛔ 알림)가 그대로", (not armed) and said and "순환 정지" in said[0][0] and said[0][1] is False, str((armed, said, exc)))
        # 정지 등록 직후 카드 빌드 통과 확인: _build_full_state 의 banned 표식이 오프라인 카드에도 붙는다
        ok("RO-7 정지 슬롯 카드는 offline 이어도 banned 표식이 붙는다(가드의 재료)",
           M._is_banned("main", "PC-T58", 1) is True and M._is_banned("main", "PC-T59", 1) is False, "")
    finally:
        M.BANNED_ACCTS.clear()
        M.BANNED_ACCTS.update(keep)


async def t_ui():
    src = M.HTML_DASHBOARD
    ok("RO-8 호출처가 한 곳이고 _silM 을 넘긴다", src.count("${rotChip(pc, _silM)}") == 1 and src.count("rotChip(pc") == 2, str(src.count("rotChip(pc")))
    node = shutil.which("node")
    ok("RO-0 node 가 있어야 한다", bool(node))
    if not node:
        return
    i = src.index("const ROT_CHIP = {")
    j = src.index("};", i) + 2
    k = src.index("function rotChip(")
    d, m = 0, src.index("{", k)
    e = m
    while True:
        c = src[e]
        d += (c == "{") - (c == "}")
        e += 1
        if d == 0:
            break
    js = ("const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;');\n" + src[i:j] + "\n" + src[k:e] + "\n" + r"""
const cases = [
  [{_rot:'hunting', status:'hunting'}, 0],
  [{_rot:'hunting', status:'offline'}, 0],
  [{_rot:'hunting', status:'no_account'}, 0],
  [{_rot:'hunting', status:'other_account'}, 0],
  [{_rot:'hunting', status:'hunting'}, 3],
  [{_rot:'switching', status:'offline', _rot_target:2}, 0],
  [{_rot:'switching', status:'idle', _rot_target:2}, 0],
  [{_rot:'hunting', status:'idle', banned:true, macro_live:false}, 0],
  [{_rot:'hunting', status:'idle', banned:true, macro_live:true}, 0],
  [{status:'offline'}, 0],
];
console.log(JSON.stringify(cases.map(([pc, s]) => { const h = rotChip(pc, s);
  return {empty: h === '', gray: h.includes('bg-gray-800'), purple: h.includes('purple'), pulse: h.includes(' pulse'),
          off: h.includes('보고 없음'), sil: h.includes('분째 무보고')}; })));
""")
    got, err = _node(js)
    want = [
        dict(empty=False, gray=False, purple=True, pulse=False, off=False, sil=False),   # 평상시 hunting
        dict(empty=False, gray=True, purple=False, pulse=False, off=True, sil=False),    # offline
        dict(empty=False, gray=True, purple=False, pulse=False, off=True, sil=False),    # no_account
        dict(empty=False, gray=True, purple=False, pulse=False, off=True, sil=False),    # other_account
        dict(empty=False, gray=True, purple=False, pulse=False, off=False, sil=True),    # 무보고 3분
        dict(empty=False, gray=True, purple=False, pulse=False, off=True, sil=False),    # offline switching — 깜빡임 끔
        dict(empty=False, gray=False, purple=False, pulse=True, off=False, sil=False),   # 살아 있는 switching — 옛 그대로 깜빡임
        dict(empty=False, gray=True, purple=False, pulse=False, off=True, sil=False),    # 정지 슬롯에 매크로 없음
        dict(empty=False, gray=False, purple=True, pulse=False, off=False, sil=False),   # 정지 슬롯에서 매크로 살아 있음 — 평상시
        dict(empty=True, gray=False, purple=False, pulse=False, off=False, sil=False),   # _rot 없음
    ]
    ok("RO-9 ★칩 10가지 입력 전부 기대대로(평상시 보라 · 꺼짐/무보고 회색+깜빡임 끔 · 살아 있는 전환은 옛 그대로)★", got == want, "got=%s err=%s" % (got, err))
    stat = open(os.path.join(os.path.dirname(__file__), "..", "static", "dashboard.js"), encoding="utf-8").read()
    ok("RO-10 static 미러가 같다(--sync-static)", "function rotChip(pc, silM)" in stat and "${rotChip(pc, _silM)}" in stat, "")
    ok("RO-11 엔진 가드가 사실(모든 카드 정지)로만 푼다 — 시계(age·ROT_*)를 읽지 않는다",
       "if stage in (\"hunting\", \"tasking\") and not active and cards and all(c.get(\"banned\") for c in cards):" in open(M.__file__, encoding="utf-8").read(), "")


def test_all():
    run_all([t_engine, t_ui])
    finish("test_rot_offline_458", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
