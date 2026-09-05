# -*- coding: utf-8 -*-
"""★사고 481·495 하네스★ — 내부망 시드를 「멈춤」으로만 버리고 「느림」으로는 안 버리는가.
★481(09-04)★ 느리되 안 끊기는 시드를 30분 붙들었다 → 상한이 필요하다.
★495(09-05)★ 3.1.11 이 속도 1MB/s 로 버리자 24대 동시 다운로드에서 PC-04 가 24MB 받고도 0.99MB/s 로 버렸다
  → 속도 판정 폐기. 버리는 조건은 ① SEED_STALL_S 동안 진행 0 ② SEED_MAX_S 초과 둘뿐.
돌리기: python -X utf8 seedspeed_test.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import updater as U
OK = FAIL = 0
MB = 1048576
def chk(name, cond, extra=""):
    global OK, FAIL
    print(("✔ " if cond else "✘ ") + name + (("  " + extra) if extra else ""))
    if cond: OK += 1
    else: FAIL += 1
def r(mb, sec, since=0.0): return U.seed_slow_reason(int(mb * MB), sec, since)
# 유예
chk("0초에는 안 버린다", r(0, 0) == "")
chk("유예 안이면 0바이트·멈춤이어도 안 버린다", r(0, U.SEED_GRACE_S - 0.1, U.SEED_GRACE_S - 0.1) == "")
# ★속도로는 안 버린다★ (495 실측 4건)
chk("★PC-04: 24MB/24초=0.99MB/s 는 안 버린다★", r(23.6, 24, 0.5) == "")
chk("★PC-14: 448KB/10초 라도 진행 중이면 안 버린다★", r(0.44, 10, 0.5) == "")
chk("★PC-16: 192KB/9초 라도 진행 중이면 안 버린다★", r(0.19, 9, 1.0) == "")
chk("0.05MB/s 로 100초 (진행 중) 안 버린다", r(5, 100, 2.0) == "")
# ★멈춤으로 버린다★
chk("유예 뒤 SEED_STALL_S 진행 없음 → 버린다", "멈춤" in r(3, 40, U.SEED_STALL_S))
chk("멈춤 직전(STALL-0.1)은 안 버린다", r(3, 40, U.SEED_STALL_S - 0.1) == "")
chk("멈춤 문장에 받은 KB 가 적힌다", "KB" in r(3, 40, U.SEED_STALL_S))
# ★상한★ (481)
chk("SEED_MAX_S 초과면 진행 중이어도 버린다", "넘겼다" in r(50, U.SEED_MAX_S + 1, 0.1))
chk("SEED_MAX_S 직전은 안 버린다", r(50, U.SEED_MAX_S - 1, 0.1) == "")
chk("상한 문장에 MB/s 가 적힌다", "MB/s" in r(50, U.SEED_MAX_S + 1, 0.1))
# 상수 관계
chk("STALL < MAX", U.SEED_STALL_S < U.SEED_MAX_S)
chk("GRACE < STALL", U.SEED_GRACE_S < U.SEED_STALL_S)
chk("읽기 타임아웃이 멈춤 판정과 같은 값이다(소스 확인)", f"timeout=(3, SEED_STALL_S)" in open(U.__file__, encoding="utf-8").read())
chk("버전 3.1.12", U.UPDATER_VERSION == "3.1.12")
print(f"\n{OK}/{OK + FAIL}")
sys.exit(1 if FAIL else 0)
