# -*- coding: utf-8 -*-
"""info.txt 폼4 이관 ★방어 하네스★ (사고 196 적대 리뷰가 실측으로 잡은 것들)

★왜 별도 파일인가★
  info_form4_test.py 는 "정상 파일이 잘 이관되나" 를 본다(행복 경로).
  이 파일은 ★손대면 안 되는 파일에 손을 안 대나★ 를 본다 — 리뷰가 잡은 사고들이다.
  둘 다 통과해야 배포한다.

★여기 있는 케이스는 전부 실제로 함대에 도달 가능한 것들이다★
  · CP949: 메모장 "ANSI 저장" 한 번. 매뉴얼이 "메모장으로 열어 적으세요" 라고 안내한다.
  · info_form 없음: 렌탈 setup ZIP 의 info.txt 에 그 줄이 없고, 매뉴얼 양식 4종도 마찬가지.

실행: python -X utf8 info_form4_guard_test.py
"""
import io
import os
import shutil
import tempfile

import updater as U

BASE = """==================  이 PC 설정  ==================
info_form=3
pc_id=PC-20
control_api_key=aion2_secret_2026
token=tok_abcdef123456

==================  계정 1  (본계정)  ==================
계정1_플랫폼=NC
계정1_아이디=acc1@example.com
계정1_비번=PW-ONE-1!
계정1_서버=이슈타르
계정1_PIN=111111
계정1_캐릭수=22
계정1_캐릭1=슬기찬솔
계정1_캐릭2=다운다운

==================  계정 2  ==================
계정2_아이디=acc2@example.com
계정2_비번=PW-TWO-2!
계정2_캐릭수=16
"""

_fails = []


def chk(cond, msg):
    print(("  OK   " if cond else "  ★실패★ ") + msg)
    if not cond:
        _fails.append(msg)


def run_migration(text, encoding="utf-8", form_line=None):
    """임시 파일에 text 를 쓰고 ★진짜 ensure_info_txt★ 를 돌린다. 결과 kv 를 돌려준다."""
    if form_line is not None:
        text = text.replace("info_form=3\n", form_line)
    d = tempfile.mkdtemp(prefix="infoform_")
    p = os.path.join(d, "info.txt")
    with io.open(p, "w", encoding=encoding, newline="") as f:
        f.write(text)
    old_path = U.INFO_TXT
    U.INFO_TXT = p
    try:
        U.ensure_info_txt()
        with io.open(p, encoding="utf-8-sig", errors="replace") as f:
            after = f.read()
    finally:
        U.INFO_TXT = old_path
        shutil.rmtree(d, ignore_errors=True)
    return U._read_kv(after), after


def main():
    print("── 1. ★CP949(ANSI 저장) 파일도 값이 안 깨진다★ ───────────")
    print("     예전엔 utf-8-sig+errors=replace 하나뿐이라 한글 키가 U+FFFD 로 깨져")
    print("     계정 칸이 통째로 비었는데 ★검사는 통과★ 했다(깨진 값이 [기타]에 남아서).")
    print("     이제 cp949 폴백이 제대로 읽으므로 ★중단이 아니라 정상 이관★ 이 정답이다.")
    kv, _ = run_migration(BASE, encoding="cp949")
    chk(kv.get("info_form") == U.INFO_FORM, "cp949 파일도 정상 이관됨(폼 승격)")
    chk("계정5_PIN" in kv, "cp949 파일도 계정5 칸을 받음")
    chk(kv.get("계정1_아이디") == "acc1@example.com", "계정1_아이디 살아있음")
    chk(kv.get("계정1_캐릭수") == "22", "계정1_캐릭수 22 그대로 (15 로 안 떨어짐)")
    chk(kv.get("계정1_캐릭1") == "슬기찬솔", "캐릭 이름 살아있음")

    print("\n── 2. ★info_form 을 못 읽으면 캐릭수를 환산하지 않는다★ ──")
    print("     (예전엔 _src_form=1 로 떨어져 22→32, 16→26 = 전 계정 한 칸씩 밀림)")
    for label, line in (("줄 자체가 없음", ""),
                        ("빈 값",        "info_form=\n"),
                        ("숫자 아님",     "info_form=abc\n"),
                        ("소수점",       "info_form=3.0\n")):
        kv, _ = run_migration(BASE, form_line=line)
        ok = kv.get("계정1_캐릭수") == "22" and kv.get("계정2_캐릭수") == "16"
        chk(ok, "%-12s → 캐릭수 %s/%s (22/16 이어야 함)"
            % (label, kv.get("계정1_캐릭수"), kv.get("계정2_캐릭수")))

    print("\n── 3. ★폼1 진짜 옛 파일은 환산이 살아 있어야 한다★ ───────")
    print("     (방어를 넣다가 정상 기능을 죽이면 그게 더 큰 사고다)")
    old1 = BASE.replace("info_form=3\n", "info_form=1\n")
    kv, _ = run_migration(old1)
    chk(kv.get("계정1_캐릭수") == "32" and kv.get("계정2_캐릭수") == "26",
        "폼1 → 환산 정상 (22→%s, 16→%s / 32·26 이어야 함)"
        % (kv.get("계정1_캐릭수"), kv.get("계정2_캐릭수")))

    print("\n── 4. ★정상 폼3 파일은 계정5 칸을 받는다★ ────────────────")
    kv, body = run_migration(BASE)
    chk(kv.get("info_form") == U.INFO_FORM, f"info_form={kv.get('info_form')}")
    chk("계정5_PIN" in kv, "계정5_PIN 칸 생성")
    chk(kv.get("계정1_캐릭수") == "22", "캐릭수 재환산 없음")
    chk(kv.get("token") == "tok_abcdef123456", "그 PC 만 아는 값 보존")
    chk("=  계정 5  =" in body or "계정 5" in body, "계정 5 구역 헤더")

    print("\n── 5. ★멱등★ 두 번 돌려도 그대로 ────────────────────────")
    kv2, _ = run_migration(body)
    chk(kv2.get("계정1_캐릭수") == "22", "재실행해도 캐릭수 불변")
    chk(kv2.get("info_form") == U.INFO_FORM, "재실행해도 폼 그대로")

    print("\n" + "=" * 60)
    print("결과: " + ("전부 통과" if not _fails else f"★{len(_fails)}건 실패★"))
    for m in _fails:
        print("   -", m)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
