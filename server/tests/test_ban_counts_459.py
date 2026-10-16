# -*- coding: utf-8 -*-
"""[대시보드] #459 (2026-10-17 주인님 «여기에 분류중에 정지 X 정지 O 이분류 두개도 추가해줘») — 히어로 «구독 O/X» 아래 «정지 O/X».

  대시보드에서 O/X 로 나뉘는 «분류» 는 히어로 왼쪽의 구독 O : / 구독 X : 한 곳뿐이다 → 같은 모양으로 정지 O : / 정지 X : 두 줄을 더한다.
  칸(= 계정 카드) 단위: 정지 O = banned(정지(OUT))인 칸 + 카드가 아직 없는 정지 슬롯 · 정지 X = 그 밖의 계정 칸(계정 없음·은퇴·가짜 PC 제외).
  서버·매크로·FV 계약은 안 바꾼다 — 화면(state) 계산 한 곳(dkBanCount).

    cd updater/server && python -X utf8 tests/test_ban_counts_459.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from fastapi.testclient import TestClient

from _harness import main as M, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 12
C = TestClient(M.app, raise_server_exceptions=False)
H = {"X-Api-Key": "testkey"}


def _fn(src, name, kw="function "):
    i = src.index(kw + name + "(")
    d, k = 0, src.index("{", i)
    while True:
        c = src[k]
        d += (c == "{") - (c == "}")
        k += 1
        if d == 0:
            return src[i:k]


def _node(js):
    node = shutil.which("node")
    d = tempfile.mkdtemp(prefix="bc459_")
    pth = os.path.join(d, "t.js")
    open(pth, "w", encoding="utf-8").write(js)
    rr = subprocess.run([node, pth], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return json.loads(rr.stdout.strip().splitlines()[-1]), rr.stderr[-500:]
    except Exception:
        return None, (rr.stderr or rr.stdout)[-500:]


def _line(src, start):
    """«const X = ...;» 한 문장을 화면 소스에서 그대로 떼어 온다."""
    i = src.index(start)
    return src[i:src.index(";", i) + 1]


def _js_harness(src):
    """dkBanCount 와 그것이 부르는 한 곳짜리 도우미(baseId·acctNumOf·isFakePc·outSlotsOf …)를 화면 소스에서 그대로 떼어 온다."""
    parts = ["let state = {}; let RETIRED = new Set();"]
    for c in ("const MAX_ACCT", "const ACCT_LABELS", "const ACCT_SUFFIX", "const FAKE_PC_BASES"):
        parts.append(_line(src, c))
    for f in ("isAcctSuf", "acctNoOfSuf", "baseId", "acctNumOf", "isFakePc", "outSlotsOf", "dkBanCount"):
        parts.append(_fn(src, f))
    return "\n".join(parts) + "\n"


def _count(src, state, retired=()):
    js = _js_harness(src) + "state = %s; RETIRED = new Set(%s); console.log(JSON.stringify(dkBanCount()));" % (
        json.dumps(state, ensure_ascii=False), json.dumps(list(retired)))
    return _node(js)


def _card(pid, status="hunting", **kw):
    c = {"pc_id": pid, "status": status}
    c.update(kw)
    return c


async def t_count():
    src = M.HTML_DASHBOARD
    got, err = _count(src, {})
    ok("BC-1 카드가 없으면 0 / 0", got == {"on": 0, "off": 0}, "%s %s" % (got, err))
    got, _ = _count(src, {"PC-01": _card("PC-01")})
    ok("BC-2 정지 아닌 계정 칸 = 정지 X", got == {"on": 0, "off": 1}, str(got))
    got, _ = _count(src, {"PC-04": _card("PC-04", "no_account", banned=True), "PC-05": _card("PC-05", "hunting", banned=True, macro_live=True)})
    ok("BC-3 ★banned 칸은 정지 O — 매크로가 그 슬롯에서 살아 있어도(hunting) 정지 O★", got == {"on": 2, "off": 0}, str(got))
    got, _ = _count(src, {"PC-14": _card("PC-14", "no_account")})
    ok("BC-4 정지가 아닌 «계정 없음» 칸은 어느 쪽에도 안 센다", got == {"on": 0, "off": 0}, str(got))
    got, _ = _count(src, {"PC-12": _card("PC-12", banned_slots=[2, 3])})
    ok("BC-5 ★카드가 아직 없는 정지 슬롯(banned_slots)도 정지 O★ — 계정2·3 → O 2 · 카드(계정1)는 X 1", got == {"on": 2, "off": 1}, str(got))
    got, _ = _count(src, {"PC-12": _card("PC-12", banned_slots=[2]), "PC-12b": _card("PC-12b", "no_account", banned=True, banned_slots=[2])})
    ok("BC-6 같은 슬롯을 카드와 banned_slots 가 둘 다 말해도 한 번만(O 1 · X 1)", got == {"on": 1, "off": 1}, str(got))
    got, _ = _count(src, {"PC-TEST": _card("PC-TEST"), "PC-DEMO": _card("PC-DEMO", banned=True), "PC-21": _card("PC-21"), "PC-22": _card("PC-22", banned=True)},
                    retired=["PC-21"])
    ok("BC-7 가짜 PC(TEST·DEMO)와 은퇴 PC 는 둘 다 센 모집단에서 뺀다 → 남는 건 PC-22 정지 1", got == {"on": 1, "off": 0}, str(got))
    mixed = {"PC-01": _card("PC-01", banned_slots=[2]), "PC-01b": _card("PC-01b", "no_account", banned=True, banned_slots=[2]),
             "PC-02": _card("PC-02"), "PC-03": _card("PC-03", "offline"), "PC-04": _card("PC-04", "no_account"),
             "PC-05": _card("PC-05", "idle", banned=True), "PC-06": _card("PC-06", banned_slots=[3])}
    got, _ = _count(src, mixed)
    # 카드 7장: 정지 2(01b·05) · 계정없음 1(04) · 활성 4(01·02·03·06) + 카드 없는 정지 슬롯 1(06 의 3번)
    ok("BC-8 ★혼합: 정지 O = 3 · 정지 X = 4 (O+X = 계정 칸 전부, 계정 없음 칸 제외)★", got == {"on": 3, "off": 4}, str(got))


async def t_real_rows():
    """서버가 실제로 내는 /status 행(banned·banned_slots 모양)을 같은 화면 함수에 넣어, 파이썬 쪽 기대값과 맞춘다."""
    src = M.HTML_DASHBOARD
    keep = set(M.BANNED_ACCTS)
    try:
        M.BANNED_ACCTS.clear()
        for k in ("PC-T91b", "PC-T92", "PC-T93b"):                       # T93b 는 카드가 없는 정지 슬롯
            M.BANNED_ACCTS.add(M.ns("main", k))
        for pc, st in (("PC-T91", "hunting"), ("PC-T91b", "idle"), ("PC-T92", "no_account"), ("PC-T93", "hunting"), ("PC-T94", "no_account")):
            C.post("/updater/status/%s" % pc, headers=H, json={"macro_state": "running", "updater_version": "3.1.13", "updater_state": "running"})
            r = C.post("/report/%s" % pc, headers=H, json={"status": st, "slot": 1, "macro_version": "1.1.1081", "acct_total": 2})
            assert r.status_code == 200, (pc, r.status_code)
        rows = await M._build_full_state("main")
        mine = {r["pc_id"]: r for r in rows if str(r.get("pc_id", "")).startswith("PC-T9")}
        got, err = _count(src, mine)
        # 기대: 카드 5장 — 정지 칸 2(T91b·T92) · 계정없음 1(T94) · 활성 2(T91·T93) + 카드 없는 정지 슬롯 1(T93b)
        ok("BC-9 ★실제 /status 행으로: 정지 O = 3 · 정지 X = 2★ (T91b·T92·T93b / T91·T93 · 계정 없음 T94 제외)",
           got == {"on": 3, "off": 2}, "%s err=%s banned=%s" % (got, err, sorted(k for k, v in mine.items() if v.get("banned"))))
    finally:
        M.BANNED_ACCTS.clear()
        M.BANNED_ACCTS.update(keep)


async def t_ui():
    src = M.HTML_DASHBOARD
    i_sub = src.index('id="dk-h-sub-off"')
    i_bon = src.index('id="dk-h-ban-on"')
    i_bof = src.index('id="dk-h-ban-off"')
    ok("BC-10 히어로 같은 박스 안에 구독 O/X 다음 정지 O → 정지 X 순서(기존 O → X 와 같은 모양)",
       i_sub < i_bon < i_bof and src.index('id="dk-h-subbox"') < i_bon < src.index('class="dk-hero-main"'), "")
    hero = _fn(src, "dkHero")
    ok("BC-11 dkHero 가 «제외 PC 가 없으면 return» 보다 먼저 채운다(전부 정지여도 정지 O 는 떠야 한다) · 계산은 dkBanCount 한 곳",
       hero.index("dkBanCount()") < hero.index("if(!pcs.length) return;") and src.count("function dkBanCount(") == 1, "")
    stat = open(os.path.join(os.path.dirname(__file__), "..", "static", "dashboard.js"), encoding="utf-8").read()
    ok("BC-12 static 미러가 같다(--sync-static)", "function dkBanCount()" in stat and 'id="dk-h-ban-on"' not in stat and "dk-h-ban-on" in stat, "")
    ok("BC-13 서버 요약(/summary · FV 스냅샷) 계약은 안 건드렸다 — 새 키 없음", "ban_counts" not in open(M.__file__, encoding="utf-8").read(), "")


def test_all():
    run_all([t_count, t_real_rows, t_ui])
    finish("test_ban_counts_459", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
