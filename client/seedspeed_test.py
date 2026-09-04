# -*- coding: utf-8 -*-
"""★사고 481 하네스★ — 느린 내부망 시드를 제때 버리는가.

★왜 있는가★ 2026-09-04 실측: 시드를 켜 둔 채 PC-12·15 가 ★30분 넘게★ 업데이트를 못 받았다.
  시드를 끄니 ★45초에 둘 다★ 받았다. 이 함수가 없던 때는 ★180초 읽기 타임아웃★ 만
  있었는데, 느리되 끊기지는 않으면 타임아웃이 매 청크마다 새로 시작되어
  ★사실상 무한히 버텼다.★ = 「시드가 살아 있으면 아무리 느려도 GitHub 를 안 본다」

★고른 임계의 근거(실측)★
  · 2.4GHz 무선 11Mbps = 1.4MB/s — 그때 관제컴이 여기 붙어 있었다(사고 480)
  · 5GHz 866Mbps 로 옮긴 뒤 = 수십 MB/s
  · GitHub 는 보통 5~20MB/s → ★시드가 1MB/s 를 못 내면 쓸 이유가 없다★

돌리기: python -X utf8 seedspeed_test.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import updater as U

OK = FAIL = 0
MB = 1048576


def chk(name, cond, extra=""):
    global OK, FAIL
    print(("✔ " if cond else "✘ ") + name + (("  " + extra) if extra else ""))
    if cond:
        OK += 1
    else:
        FAIL += 1


def slow(mb, sec):
    return U.seed_slow_reason(int(mb * MB), sec)


# ── 유예 안에서는 안 버린다 ────────────────────────────────────
chk("0초에는 안 버린다", slow(0, 0) == "")
chk("유예 안이면 0바이트여도 안 버린다", slow(0, U.SEED_GRACE_S - 0.1) == "")
chk("유예 경계에서는 아직 안 버린다", slow(0, U.SEED_GRACE_S) == "")

# ── ★실사고 재현★ 2.4GHz 무선 1.4MB/s 는 통과, 그보다 느리면 버린다 ──
chk("★2.4GHz 급 1.4MB/s 는 살려둔다★ (임계 바로 위)", slow(1.4 * 10, 10) == "",
    f"{1.4:.1f}MB/s")
chk("★0.5MB/s 는 버린다★ (73MB 에 146초)", "너무 느리다" in slow(0.5 * 10, 10))
chk("★0.99MB/s 도 버린다★ (임계 바로 아래)", "너무 느리다" in slow(9.9, 10))
chk("1.01MB/s 는 살려둔다 (임계 바로 위)", slow(10.1, 10) == "")

# ── 빠르면 언제나 통과 ─────────────────────────────────────────
chk("10MB/s 는 통과", slow(100, 10) == "")
chk("50MB/s 는 통과", slow(500, 10) == "")

# ── 전체 상한 ─────────────────────────────────────────────────
chk("★상한을 넘기면 빨라도 버린다★ (느린 게 아니라 멈춘 것일 수 있다)",
    "넘겼다" in slow(1000, U.SEED_MAX_S + 1))
chk("상한 직전은 통과", slow(1000, U.SEED_MAX_S - 1) == "")

# ── 이유 문장이 숫자를 담는가 (다음 사람이 판단할 수 있게) ────
r = slow(0.5 * 10, 10)
chk("버리는 이유에 실제 속도가 들어간다", "MB/s" in r and "0.50" in r, r)
r2 = slow(1000, U.SEED_MAX_S + 1)
chk("상한 이유에도 숫자가 들어간다", "MB/s" in r2 and "초" in r2, r2)

# ── 이상한 입력에도 안 터진다 ─────────────────────────────────
chk("elapsed 0 이어도 안 터진다", isinstance(slow(5, 0), str))
chk("elapsed None 이어도 안 터진다", isinstance(U.seed_slow_reason(0, None), str))
chk("음수 elapsed 도 안 터진다", isinstance(U.seed_slow_reason(0, -5), str))

# ── 임계값이 실측 근거와 맞는가 ───────────────────────────────
chk("하한이 1.0MB/s", abs(U.SEED_MIN_MBPS - 1.0) < 1e-9)
chk("유예가 8초", abs(U.SEED_GRACE_S - 8.0) < 1e-9)
chk("상한이 150초", abs(U.SEED_MAX_S - 150.0) < 1e-9)
chk("★읽기 타임아웃을 180초에서 줄였다★ — 그게 무한 대기의 뿌리였다",
    "timeout=(3, 30)" in open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "updater.py"),
        encoding="utf-8").read())

print(f"\n{OK}/{OK + FAIL}")
sys.exit(1 if FAIL else 0)
