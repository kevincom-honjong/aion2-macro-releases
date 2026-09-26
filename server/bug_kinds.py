# -*- coding: utf-8 -*-
"""#271 버그 스샷 종류 표 — ★정본★ (2026-09-27 주인님 「버그 이미지 쓸모없이 쌓인다」 · 아이온2 결정).

매크로 config.bug_route 가 이 표로 올릴지·PC 에 둘지를 정하고, 대시보드 서버는 같은 표를 거울로 두어
보관(retention) 무리를 정한다 — 대시보드 게이트가 두 표를 글자 대조한다. ★표준 라이브러리만 import★(서버가 그대로 읽게).

무리(이름은 고정 — 바꾸면 서버 거울·게이트가 같이 바뀌어야 한다):
  upload-harvest  수확 재료(계정줄·프로필 카드·드롭다운) — plrow_harvest/plrow_autofill/profcard_fill 이 읽는다
  upload-learn    OCR 비교 크롭 — 서버가 OCR 큐로 복사한다
  upload-incident 드물게 나는 사고·진단 — alarmshot/bugpull/bugshots·알람 문구가 가리킨다
  local-learn     학습 크롭(ocrlearn) — 라벨 곁장부가 PC 에만 있다 → bugs_local/ocrlearn
  local           나머지(많이 나는 일상 종류) — bugs_local, 내부망 GET /bugs_local/list·get 으로 꺼낸다
★표의 규칙★ 드물게 나는 사고·진단 종류는 올린다(30분 중복 거름) · 많이 나는 일상 종류는 PC 에만.
  새 종류를 올리려면 ops 의 어느 도구·알람 문구가 그 파일을 읽는지 같이 적는다.
판정은 tag(파일명 `{pc}_{YYYYMMDD}_{HHMMSS}_{tag}.png` 의 tag)에 fnmatch(대소문자 구분), ★TABLE 위에서부터 첫 일치★.
"""
import fnmatch
import re

CLASSES = ("upload-harvest", "upload-learn", "upload-incident", "local-learn", "local")

TABLE = (
    ("upload-harvest", ("plrowfull", "plrowlist", "plrowmiss*", "autorow*", "profcard*", "plrow_*_CAND",
                        "*flyout*", "*dropdown-fail*")),
    ("upload-learn", ("ocrdiff_*", "oddfail_*")),
    ("upload-incident", ("*teleport_timeout*", "*_fail*", "*-fail*", "*unknown_screen*", "*stuck*",
                         "corridor_portal_enter_fail",
                         "cview-*",                          # 사람 명령 chrome_view — 올리는 것이 그 명령의 결과물
                         # 드문 진단: ops alarmshot FAIL_PAT · plrow_sim/plrow_dist · 대시보드 판매 검수(sale.py _snap) · 알람 문구
                         "*-warn", "tour_nogame", "captcha_missed_fraud", "*dropdown-miss", "*MISS*",
                         "*switch-pick*", "*_dd*", "*ddregion*", "lc10-switch*", "sale??_*",
                         "hostacct_ambiguous", "switch-stream-dropped-to-list", "switch-parsec-maint-banner",
                         "reconnect_f5cap", "nightmare_combat_timeout", "exitlag_taskmgr_harvest", "chip1-*")),
    ("local-learn", ("ocrlearn_*",)),
)

# 30분 같은 tag 중복 거름에서 빠지는 것 — 사람 명령(cview) · 자기 예산이 있는 학습 크롭(ocrlearn)
NODEDUP = ("cview-*", "ocrlearn_*")

_FN_RE = re.compile(r"^(.+?)_(\d{8})_(\d{6})_(.+)\.png$", re.IGNORECASE)


def classify(tag) -> str:
    """tag → CLASSES 중 하나. 위에서부터 첫 일치, 없으면 "local"."""
    t = str(tag)
    for cls, pats in TABLE:
        if any(fnmatch.fnmatchcase(t, p) for p in pats):
            return cls
    return "local"


def uploads(tag) -> bool:
    return classify(tag).startswith("upload-")


def nodedup(tag) -> bool:
    return any(fnmatch.fnmatchcase(str(tag), p) for p in NODEDUP)


def split_name(fn):
    """`{pc}_{YYYYMMDD}_{HHMMSS}_{tag}.png` → (pc, tag). 모양이 다르면 (None, 확장자 뗀 이름)."""
    import os
    b = os.path.basename(str(fn))
    m = _FN_RE.match(b)
    return (m.group(1), m.group(4)) if m else (None, b.rsplit(".", 1)[0])
