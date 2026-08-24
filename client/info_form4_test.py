# -*- coding: utf-8 -*-
"""info.txt 폼 3 → 4 이관 하네스 (사고 196 — 계정5 칸이 파일에 안 생긴다)

★무엇을 확인하나★
  1. 계정5 구역이 ★제대로 된 섹션★ 으로 생기나 (지금은 낱줄로 파일 끝에 붙어 있다)
  2. 옛 값이 하나도 안 사라지나 (pc_id·API키·캐릭이름·사람이 손으로 적은 것)
  3. ★캐릭수가 다시 환산되지 않나★ — 폼2 파일을 재환산하면 16→26 이 되어
     함대가 한 칸씩 밀린 캐릭을 누른다(updater.py:1407 주석이 경고하는 바로 그것)
  4. 모르는 칸이 [ 기타 ] 로 보존되나

실행: python -X utf8 info_form4_test.py
"""
import re
import updater as U

# ── 함대 실물을 흉내낸 폼3 파일 ────────────────────────────────────
#   계정1~4 는 섹션으로, 계정5 는 ★set_info 가 끝에 낱줄로 붙인 상태★
BEFORE = """==================  이 PC 설정  ==================
info_form=3
pc_id=PC-20
control_api_key=aion2_secret_2026
token=tok_abcdef123456
lan_prefix=172.30.1.
screenshot_key=ctrl+q

------  거의 안 씀 (비워두세요)  ------
lan_ip=
gemini_api_key=AIzaTESTKEY

==================  계정 1  (본계정)  ==================
계정1_플랫폼=NC
계정1_아이디=acc1@example.com
계정1_비번=PW-ONE-1!
계정1_이메일=acc1@example.com
계정1_휴대폰=010-1111-1111
계정1_서버=이슈타르
계정1_PIN=111111
계정1_캐릭수=22
계정1_캐릭1=슬기찬솔
계정1_캐릭2=다운다운

==================  계정 2  ==================
계정2_플랫폼=NC
계정2_아이디=acc2@example.com
계정2_비번=PW-TWO-2!
계정2_서버=지켈
계정2_PIN=222222
계정2_캐릭수=12
계정2_캐릭1=둘째캐릭

==================  계정 3  ==================
계정3_아이디=acc3@example.com
계정3_비번=PW-THREE-3!
계정3_캐릭수=15

==================  계정 4  ==================
계정4_아이디=acc4@example.com
계정4_비번=PW-FOUR-4!

------  기타 (예전 칸 - 지우지 마세요)  ------
사람이_손으로_적은칸=이건보존돼야함

[ 채우는 법 ]
info_form        건드리지 마세요
계정5_플랫폼=NC
계정5_아이디=acc5@example.com
계정5_비번=PW-FIVE-5!
계정5_이메일=acc5@example.com
계정5_휴대폰=010-5555-5555
계정5_서버=네자칸
"""


def main():
    kv_before = U._read_kv(BEFORE)
    body, conv, leftover = U._build_new_info(kv_before)
    kv_after = U._read_kv(body)
    fails = []

    def chk(cond, msg):
        print(("  OK   " if cond else "  ★실패★ ") + msg)
        if not cond:
            fails.append(msg)

    print("── 1. 계정5 섹션이 생겼나 ─────────────────────────────")
    chk(re.search(r"=+\s*계정 5\s*=+", body) is not None,
        "'====  계정 5  ====' 구역 헤더")
    for k in ("계정5_플랫폼", "계정5_아이디", "계정5_비번", "계정5_이메일",
              "계정5_휴대폰", "계정5_서버", "계정5_PIN", "계정5_캐릭수", "계정5_캐릭1"):
        chk(k in kv_after, f"{k} 칸 존재")
    chk(kv_after.get("계정5_아이디") == "acc5@example.com", "계정5_아이디 값 유지")
    chk(kv_after.get("계정5_서버") == "네자칸", "계정5_서버 값 유지")
    # ★PIN 은 비어 있어야 한다 — 아직 아무도 안 넣었으니 '빈 칸' 이 정답★
    chk(kv_after.get("계정5_PIN", "x") == "", "계정5_PIN 은 빈 칸으로 생성(주인님이 채울 자리)")

    print("\n── 2. 옛 값이 하나도 안 사라졌나 ──────────────────────")
    conv_old = {o for o, _ in conv.values()}
    newvals = set(kv_after.values())
    # ★info_form 은 이관 표식이라 바뀌는 게 정상 — 본체와 같은 규칙으로 제외★
    missing = [k for k, v in kv_before.items()
               if k != "info_form" and v and v not in newvals and v not in conv_old]
    chk(not missing, f"소실된 값 없음 (소실: {missing})")
    for k in ("pc_id", "control_api_key", "token", "계정1_캐릭1", "계정1_캐릭2",
              "gemini_api_key", "사람이_손으로_적은칸"):
        chk(kv_after.get(k) == kv_before.get(k), f"{k} 그대로")

    print("\n── 3. ★캐릭수 재환산이 없나 (제일 위험한 것)★ ────────")
    for k in ("계정1_캐릭수", "계정2_캐릭수", "계정3_캐릭수"):
        chk(kv_after.get(k) == kv_before.get(k),
            f"{k} {kv_before.get(k)} → {kv_after.get(k)} (변하면 안 됨)")
    chk(not conv, f"환산 기록 비어 있음 (conv={conv})")

    print("\n── 4. 폼 번호 / 기타 보존 ────────────────────────────")
    chk(kv_after.get("info_form") == U.INFO_FORM, f"info_form={kv_after.get('info_form')}")
    chk("사람이_손으로_적은칸" in kv_after, "모르는 칸 보존")

    print("\n" + "=" * 58)
    print("결과: " + ("전부 통과" if not fails else f"★{len(fails)}건 실패★"))
    if fails:
        for m in fails:
            print("   -", m)
    print("\n[생성된 계정5 구역]")
    m = re.search(r"(=+\s*계정 5\s*=+.*?)(?:\n\n|\Z)", body, re.S)
    print(m.group(1) if m else "(없음)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
