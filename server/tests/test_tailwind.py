# -*- coding: utf-8 -*-
"""Tailwind 를 미리 뽑은 CSS 로 — Play CDN 이 돌아오지 않고, 새 클래스를 쓰면 다시 뽑게 한다.

2026-09-23 반응속도: 두 화면이 cdn.tailwindcss.com(브라우저 안 JIT)을 기다렸다. 같은 생성기로 뽑은
static/tw.css 를 </head> 앞에 인라인 → 렌더 비교(1400·837 폭, 요소 9,088 × 속성 546, 로그인 20 × 542)
차이 0, 첫 화면 FCP 920→710ms(차가운 캐시)·252→144ms(따뜻한 캐시).

    cd updater/server && python -X utf8 tests/test_tailwind.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import main, ok, run_all, finish   # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tw_build   # noqa: E402

MIN_CHECKS = 8
CDN_TAG = '<script src="https://cdn.tailwindcss.com"'


def t_built_css():
    css = open(main.TW_CSS_PATH, encoding="utf-8").read()
    ok("T-1 static/tw.css 가 있고 Tailwind 산출물이다(--tw-·preflight)",
       "--tw-" in css and "box-sizing:border-box" in css.replace(" ", "") and len(css) > 20000, "%d자" % len(css))
    for name in ("HTML_LOGIN", "HTML_DASHBOARD"):
        h = getattr(main, name)
        head = h[:h.index("</head>")]
        ok("T-2 %s: Play CDN 스크립트가 없다" % name, CDN_TAG not in h)
        # Play CDN 은 <style> 을 head ★맨 끝★ 에 붙였다 — 같은 자리여야 페이지 자체 <style> 과의 우선순위가 같다
        last = head.rstrip().rsplit("<", 1)[-1]
        ok("T-3 %s: 빌드 CSS 가 head 의 맨 끝(= CDN 이 붙이던 자리)" % name,
           '<style id="tw-built">' in head and head.rstrip().endswith("</style>")
           and head.rindex('<style id="tw-built">') > head.rindex("</style>", 0, head.rindex('<style id="tw-built">')),
           last[:40])


def t_tokens_covered():
    have = set(open(tw_build.TOKENS_PATH, encoding="utf-8").read().split())
    now = tw_build.page_tokens()
    new = sorted(now - have)
    ok("T-4 HTML 의 클래스 후보 낱말이 전부 tw_tokens.txt 안에 있다 — 새 클래스를 썼으면 `python tw_build.py`",
       not new, "새 낱말 %d개: %s" % (len(new), new[:15]))


def t_no_dynamic_classes():
    # 'bg-'+색 · `text-${c}-400` 처럼 ★조립한★ 클래스는 후보 낱말에 안 잡혀 CSS 에서 빠진다.
    #   지금 있는 것은 전부 id 조립(card-·stack-·pcmd-·pc-arrow-·cm-)뿐 — 새로 생기면 빨간불.
    src = tw_build.raw_html(main.HTML_DASHBOARD) + tw_build.raw_html(main.HTML_LOGIN)
    hits = set(re.findall(r"([a-z]+-(?:[a-z0-9]+-)*)(?:\$\{|'\s*\+)", src))
    allowed = {"card-", "stack-", "pcmd-bar-", "pcmd-chip-", "pc-arrow-", "cm-acct-", "cm-cardonly-", "ai-kina-h-"}
    ok("T-5 조립한 클래스 이름이 없다(있으면 빌드 CSS 에서 빠진다)", hits <= allowed, str(sorted(hits - allowed)))


def t_fallback():
    orig = main.TW_CSS_PATH
    try:
        main.TW_CSS_PATH = os.path.join(os.path.dirname(orig), "없는파일.css")
        h = main._tw_inline("<head><!--TW_CSS--></head>")
    finally:
        main.TW_CSS_PATH = orig
    ok("T-6 tw.css 가 없으면 예전 Play CDN 으로 돌아간다(화면이 벌거벗지 않는다)", CDN_TAG in h and "tailwind.config" in h, h[:80])


def test_all():
    run_all([t_built_css, t_tokens_covered, t_no_dynamic_classes, t_fallback])
    finish("test_tailwind", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
