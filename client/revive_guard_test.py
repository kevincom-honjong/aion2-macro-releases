# -*- coding: utf-8 -*-
"""_auto_revive 가 「사람이 콘솔을 직접 닫은 것」을 크래시로 안 세는지 (사고 629, 2026-09-22)

★실사고★ 주인님: 「24번 내가 프로그램 꺼놨는데 업데이터가 다시 켜서 계정접속 시도한다」
PC-24.upd 09:41:00 [크래시감지] returncode=3221225786(=0xC000013A STATUS_CONTROL_C_EXIT,
콘솔 X 를 닫을 때 Windows 가 주는 코드) → _auto_revive 가 되살림 = 사람이 끈 걸 도로 켰다.

★실측(함대 전체 .upd 크래시로그, 2026-09-22)★ 이번 종류(0xC000013A) 4건 / 진짜 크래시
0xC000041D STATUS_FATAL_APP_EXIT(PC-19) 1건 — ★그 코드는 되살려야 맞다★. 그래서
0xC000013A 하나만 막는다(다른 코드는 실측 없이 안 넣는다).

가상 returncode 로 `_auto_revive` 실함수를 부른다(모듈 전체 import 는 부팅 부작용이 있어
그 블록만 떼어 exec — cmd_dedup_test.py 와 같은 방식).

    python -X utf8 revive_guard_test.py
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ran, bad = [], []


def ck(why, got, want=True):
    ran.append(why)
    if got != want:
        bad.append("%s got=%r want=%r" % (why, got, want))
    print(("  ✔ " if got == want else "  ✘ ") + why)


src = io.open(os.path.join(HERE, "updater.py"), encoding="utf-8", errors="replace").read()
src = src.replace("\r\n", "\n")

ck("코드셋에 0xC000013A 가 있다", "3221225786" in src and "_USER_CLOSED_CODES" in src)
ck("진짜 크래시 코드(0xC000041D)는 안 막는다", "3221226525" not in src)

blk = src[src.find("NO_RESTART_PATH = r"):src.find("def _crash_check_thread")]


def _make_ns(no_restart_exists=False):
    calls = {"state": [], "start_macro": 0, "slept": []}

    class _FakeTime:
        def sleep(self, s):
            calls["slept"].append(s)

        def time(self):
            return 1000000.0

    def _set_state(state, expect=None):
        calls["state"].append((state, expect))

    def _start_macro():
        calls["start_macro"] += 1
        return True

    def _macro_running_anywhere():
        return False

    ns = {
        "os": type("os_", (), {"path": type("p_", (), {
            "exists": staticmethod(lambda p: no_restart_exists)})()})(),
        "time": _FakeTime(),
        "log": lambda *a, **k: None,
        "err": lambda *a, **k: None,
        "_set_state": _set_state,
        "start_macro": _start_macro,
        "_macro_running_anywhere": _macro_running_anywhere,
    }
    exec(blk, ns)
    return ns, calls


# A. 콘솔 직접 닫음(0xC000013A) — 되살리지 않는다
ns, calls = _make_ns()
ns["_auto_revive"](3221225786)
ck("A: start_macro 안 부름", calls["start_macro"] == 0)
ck("A: state=stopped(expect=crashed) 기록", ("stopped", "crashed") in calls["state"])

# B. 진짜 크래시(0xC000041D, PC-19 실측) — 되살린다
ns, calls = _make_ns()
ns["_auto_revive"](3221226525)
ck("B: start_macro 부름(진짜 크래시는 되살려야 한다)", calls["start_macro"] == 1)

# C. 정상 종료(returncode=0, 가장 흔함·551건 실측) — no_restart 없으면 그래도 되살린다
#    (이 코드 자체는 §기존 동작 — 회귀만 확인, 사고629 로 안 바뀜)
ns, calls = _make_ns()
ns["_auto_revive"](0)
ck("C: returncode=0 은 여전히 되살린다(회귀 확인)", calls["start_macro"] == 1)

# D. 문자열 returncode("no-handle") — int() 변환 실패해도 안 죽고 정상 진행
ns, calls = _make_ns()
try:
    ns["_auto_revive"]("no-handle")
    d_ok = calls["start_macro"] == 1
except Exception as e:
    d_ok = False
    print("  ✘ D 예외:", e)
ck("D: 문자열 returncode 도 안 죽고 되살린다", d_ok)

# E. no_restart 표시 있으면 사고629 코드와 무관하게 그대로 둔다(기존 분기 우선순위 확인)
ns, calls = _make_ns(no_restart_exists=True)
ns["_auto_revive"](3221225786)
ck("E: no_restart 우선 — start_macro 안 부름", calls["start_macro"] == 0)
ck("E: no_restart 우선 — state=stopped 기록", ("stopped", "crashed") in calls["state"])

if bad:
    print("\n실패 %d/%d건" % (len(bad), len(ran)))
    for b in bad:
        print("  ✗", b)
    sys.exit(1)
print("\nrevive_guard_test %d/%d 통과" % (len(ran), len(ran)))
sys.exit(0)
