# -*- coding: utf-8 -*-
"""[대시보드] #208 (2026-09-25 주인님) — 표층 중인데 카드가 «대기». 매크로가 새 status `surface` 를 보낸다(회랑과 같은 방식).

  ① 카드: STATUS_CFG.surface = «표층 진입중», 모양(색·online)은 회랑과 같다. DK_BLEED 도 회랑과 같다.
  ② 순환: surface 는 ROT_SESSION(세션 중 — 사냥 아님, 끝나길 기다린다). 사냥 확인도, idle 도, «딴 일» 도 아니다.
     ▶시작 뒤 surface 카드를 보면 ⛔·start 없이 기다리고, 전환 뒤 목표 카드가 surface 여도 세션 대기.
  ③ 노는 PC 자동진행(AUTO_IDLE_BUSY)·명령 추적(CMD_TRACK start/surface exp → FV 명령표 expect_status)에 surface.
  ★모든 검사는 고치기 전 main.py 에서 실패한다★ (튜플을 도는 게 아니라 "surface" 를 글자로 박았다).

    cd updater/server && python -X utf8 tests/test_surface_208.py
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

from _harness import main, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 15
M = main
SENT, SAID = [], []


async def _f_send(t, pc, cmd, args=None):
    SENT.append((pc, cmd))
    return True


async def _f_say(t, pc, text, routine=False):
    SAID.append(text)


async def _f_save(force=False):
    pass


async def _f_logs(pc, limit=1000):
    return []


class _AnyPeer(dict):
    def get(self, k, d=None):
        return "peer"


async def _f_pm(t):
    return _AnyPeer()


async def _f_allow(t="main"):
    return {"*"}


async def _f_nm(t):
    return {}


_PATCH = {"_rot_send": _f_send, "_rot_say": _f_say, "_rot_save": _f_save, "get_logs": _f_logs,
          "_get_parsec_map": _f_pm, "_rot_allow": _f_allow, "_rot_nm_clears": _f_nm}
_ORIG = {}


def _iso(ago_s):
    return (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).strftime("%Y-%m-%dT%H:%M:%S")


def card(pid, status, ago):
    return {"pc_id": pid, "status": status, "last_active": _iso(ago), "_updated_at": _iso(ago),
            "acct_ids": {"1": "a@x", "2": "b@x"}, "acct_names": {"1": ["A"], "2": ["B"]}}


async def step(st, pcs, base):
    key = M.ns("main", base)
    M._ROT[key] = st
    M._ROT_STEP_CUR.update(key=key, st=st)
    try:
        await M._rot_step_pc("main", base, st, pcs)
    finally:
        M._ROT_STEP_CUR.update(key=None, st=None)
    return M._ROT.get(key) is st


def reset():
    SENT.clear(); SAID.clear(); M._ROT.clear()


async def t_rotation():
    ok("S-1 surface 는 세션 중(ROT_SESSION) — 사냥 확인(ROT_HUNT_OK)·쉬는 자리(ROT_IDLE_SET)가 아니다",
       "surface" in M.ROT_SESSION and "surface" in M.ROT_SESS_HOLD and "surface" in M.ROT_HUNT_LIKE
       and "surface" not in M.ROT_HUNT_OK and "surface" not in M.ROT_IDLE_SET,
       "%s %s" % (M.ROT_SESSION, M.ROT_IDLE_SET))
    ok("S-2 순환 말은 «표층 진입중»(텔레그램에 영어 status 가 안 샌다)", M.ROT_ST_KOR.get("surface") == "표층 진입중",
       repr(M.ROT_ST_KOR.get("surface")))
    # ▶시작 뒤 7분이 지났는데 카드가 표층 — 옛 코드: «사냥이 안 잡힙니다» ⛔
    reset()
    st = {"stage": "starting", "since": M._rot_now() - (M.ROT_START_MAX + 60), "armed_at": M._rot_now() - 9999,
          "day": M._kst_today_key(), "full": False, "hops": 0, "visits": {}, "target": "a"}
    alive = await step(st, [card("PC-70", "surface", 10)], "PC-70")
    ok("S-3 ▶시작 뒤 7분에 표층 카드 — ⛔·start 없이 세션 대기(말 한 번 «표층 진입중 중입니다»)",
       alive and st.get("stage") == "starting" and not SENT and any("표층 진입중 중입니다" in x for x in SAID),
       "%s %s %s" % (st.get("stage"), SENT, SAID))
    # 전환 단계 — 목표 계정 카드가 표층이면 사냥 확인도 idle 도 아니다
    reset()
    now = M._rot_now()
    st = {"stage": "switching", "since": now - 60, "armed_at": now - 9999, "day": M._kst_today_key(),
          "target": "b", "full": False, "hops": 1, "visits": {}, "expect_restart": False}
    alive = await step(st, [card("PC-71", "other_account", 3000), card("PC-71b", "surface", 10)], "PC-71")
    ok("S-4 전환 뒤 목표 카드가 surface — 세션 대기(starting·sess_since), start 안 보냄",
       alive and st.get("stage") == "starting" and st.get("sess_since") and not SENT,
       "%s %s %s" % (st.get("stage"), SENT, SAID))
    ok("S-5 신선한 surface 카드는 살아 있는 카드, 2000초 무보고면 박제(회랑과 같은 신선도)",
       bool(M._rot_active([card("PC-72", "surface", 10)])) and not M._rot_active([card("PC-72", "surface", 2000)]), "")


def _js_obj(name, kind="{"):
    """HTML_DASHBOARD 의 `const NAME = {…};` / `[…];` 블록을 그대로 잘라 온다."""
    close = "\n};" if kind == "{" else "];"
    m = re.search(r"const " + name + r" = " + re.escape(kind) + r"(.*?)" + re.escape(close), M.HTML_DASHBOARD, re.S)
    assert m, name
    return "const " + name + " = " + kind + m.group(1) + close


def _node(js):
    node = shutil.which("node")
    if not node:
        return None
    d = tempfile.mkdtemp(prefix="s208_")
    p = os.path.join(d, "t.js")
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


async def t_card_js():
    js = "\n".join([_js_obj("STATUS_CFG"), _js_obj("DK_BLEED"), _js_obj("AUTO_IDLE_BUSY", "["),
                    _js_obj("CMD_TRACK"),
                    "console.log(JSON.stringify({s: STATUS_CFG.surface || null, c: STATUS_CFG.corridor,"
                    " ks: DK_BLEED.surface || null, kc: DK_BLEED.corridor, busy: AUTO_IDLE_BUSY,"
                    " start: CMD_TRACK.start.exp, sf: CMD_TRACK.surface.exp}));"])
    o = _node(js)
    ok("S-6 node 로 화면 표를 실행했다(없으면 빨간불 — 문자열 grep 으로 때우지 않는다)", o is not None, "" if o is not None else "node 없음")
    if o is None:
        return
    s, c = o["s"] or {}, o["c"]
    ok("S-7 카드 라벨 surface = «표층 진입중» (옛: 없음 → 빨간 오프라인)", s.get("label") == "표층 진입중", str(s))
    ok("S-8 모양은 회랑과 같다(bg·border·badge·text·online)",
       all(s.get(k) == c.get(k) for k in ("bg", "border", "badge", "text", "online")), "%s vs %s" % (s, c))
    ok("S-9 다크 번짐색도 회랑과 같다", o["ks"] == o["kc"], "%s vs %s" % (o["ks"], o["kc"]))
    ok("S-10 노는 PC 자동진행은 표층 도는 PC 를 «하는 중» 으로 건너뛴다", "surface" in o["busy"], str(o["busy"]))
    ok("S-11 ▶시작 명령 추적 — 세션 remap 인 surface 도 «먹힘»(회랑과 같이)", "surface" in o["start"], str(o["start"]))
    ok("S-12 표층 명령 추적 — surface 가 먹힘 표시, 옛 판 어휘(abyss·idle…)도 그대로",
       o["sf"][:1] == ["surface"] and {"abyss", "idle", "hunting", "corridor", "paused"} <= set(o["sf"]), str(o["sf"]))


async def t_fv_catalog():
    cat = M._fv_parse_cmd_table()
    ok("S-13 FV 명령표(CMD_TRACK 을 읽는다) — surface.expect_status 에 surface",
       "surface" in (cat.get("surface") or {}).get("expect_status", []), str(cat.get("surface")))
    ok("S-14 FV 명령표 — start.expect_status 에 surface",
       "surface" in (cat.get("start") or {}).get("expect_status", []), str(cat.get("start")))
    blk = re.search(r"const CMD_TRACK = \{(.*?)\n\};", M.HTML_DASHBOARD, re.S).group(1)
    keys = set(re.findall(r"^\s*([a-z_][a-z0-9_]*)\s*:\s*\{", blk, re.M))
    ok("S-15 ★같은 부류★ CMD_TRACK 의 명령이 FV 명령표에서 하나도 안 빠진다(줄 끝 주석 달린 줄 포함 — 옛: surface 누락)",
       bool(keys) and keys <= set(cat),
       "빠짐=%s" % sorted(keys - set(cat)))


def test_all():
    for k, v in _PATCH.items():
        _ORIG[k] = getattr(M, k)
        setattr(M, k, v)
    try:
        run_all([t_rotation, t_card_js, t_fv_catalog])
    finally:
        for k, v in _ORIG.items():
            setattr(M, k, v)
    finish("test_surface_208", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
