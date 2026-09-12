# -*- coding: utf-8 -*-
"""[통합 2026-09-12] 업데이터 클라이언트 — 같은 명령 id 를 두 번 실행하지 않는다 (SHARED_ISSUES_대시보드 #6).

실함수(_remember_cmd_id · _ack_updater_cmd)를 부르고, 폴링 분기는 소스로 「중복이면 handle_command 를 안 띄운다」를 본다.
  python -X utf8 cmd_dedup_test.py
"""
import io, os, re, sys, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("AION2_UPDATER_TEST", "1")

ran, bad = [], []


def ck(why, got, want=True):
    ran.append(why)
    if got != want:
        bad.append("%s got=%r want=%r" % (why, got, want))
    print(("  ✔ " if got == want else "  ✘ ") + why)


src = io.open(os.path.join(HERE, "updater.py"), encoding="utf-8", errors="replace").read()
src = src.replace("\r\n", "\n")     # updater.py 는 CRLF — 줄끝을 하나로 맞춰야 자르기·정규식이 맞는다

# 소스 — 폴링 분기 모양
ck("중복 분기가 있다", "cmd_id in _done_cmd_ids" in src)
m = re.search(r"if cmd_id is not None and cmd_id in _done_cmd_ids:(.*?)else:(.*?)threading\.Thread\(target=handle_command", src, re.S)
ck("중복 분기 안에서는 handle_command 를 띄우지 않는다", m is not None and "handle_command" not in m.group(1))
ck("중복 분기에 continue 가 없다(폴링 sleep 을 건너뛰어 폭주하지 않게)", m is not None and "continue" not in m.group(1))
ck("ack 실패가 로그에 남는다(except: pass 아님)", "ack 실패" in src and "def _ack_updater_cmd" in src)

# 실행 — 헬퍼를 진짜로 부른다 (모듈 전체 import 는 부팅 부작용이 있어 함수만 떼어 실행)
ns = {}
blk = src[src.find("_done_cmd_ids: list = []"):src.find("def _ack_updater_cmd")]
exec(blk, ns)
for i in range(60):
    ns["_remember_cmd_id"](i)
ck("최근 50개만 기억한다", len(ns["_done_cmd_ids"]) == ns["DONE_CMD_IDS_MAX"])
ck("오래된 id 는 잊는다(0 은 없다)", 0 not in ns["_done_cmd_ids"])
ck("최근 id 는 기억한다(59 있다)", 59 in ns["_done_cmd_ids"])
ns["_remember_cmd_id"](None)
ck("None 은 안 넣는다", None not in ns["_done_cmd_ids"])

# _ack_updater_cmd — 실패해도 예외를 안 내고 False
_a = src.find("def _ack_updater_cmd")
_lines = src[_a:].split("\n")
_end = len(_lines)
for _i, _l in enumerate(_lines[1:], 1):          # 함수 끝 = 다음에 오는 «들여쓰기 없는 비어 있지 않은 줄»
    if _l.strip() and not _l.startswith((" ", "\t")):
        _end = _i
        break
blk2 = "\n".join(_lines[:_end])
ns2 = {"CONTROL_SERVER": "http://127.0.0.1:1", "pc_id": "PC-T", "TIMEOUT_CONNECT": 0.2, "err": lambda m: ns2.setdefault("_e", []).append(m)}
exec(blk2, ns2)


class _Boom:
    def post(self, *a, **k):
        raise RuntimeError("죽음")


ck("ack 실패는 False 를 돌려주고 예외를 안 낸다", ns2["_ack_updater_cmd"](_Boom(), 7) is False)
ck("  그 실패가 err 로 남는다", any("ack 실패" in m for m in ns2.get("_e", [])))

print("\ncmd_dedup_test %d/%d %s" % (len(ran) - len(bad), len(ran), "✔" if not bad else "✘"))
for b in bad:
    print("   ✘ " + b)
sys.exit(1 if bad else 0)
