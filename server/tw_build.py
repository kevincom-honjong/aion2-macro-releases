# -*- coding: utf-8 -*-
"""Tailwind CSS 를 미리 뽑아 static/tw.css 로 둔다 (2026-09-23 반응속도 — Play CDN 제거).

왜: 예전엔 두 화면(로그인·대시보드)이 cdn.tailwindcss.com(브라우저 안 JIT, 약 400KB JS)을 받아
    ★브라우저에서★ 클래스를 감시·생성했다 — 첫 화면이 스크립트 다운로드·실행을 기다렸고,
    처음 쓰는 클래스는 한 프레임 뒤에 칠해졌다(main.py 의 .srv-chip 주석, 600ms 번쩍임).
어떻게: 두 HTML 에서 클래스 후보 낱말을 전부 뽑아(extract_tokens) 숨은 div 하나에 달고,
    ★같은 Play CDN★ 을 로컬 크로미움(playwright)에서 돌려 생긴 <style> 을 그대로 떠 온다.
    같은 생성기·같은 설정(darkMode:'class')이라 규칙이 같다. 후보는 넉넉히(JS 낱말까지) —
    요소에 안 붙는 클래스의 규칙은 아무것도 안 칠한다.
지키는 시험: tests/test_tailwind.py — HTML 의 후보 낱말이 static/tw_tokens.txt 의 부분집합이어야
    한다(새 클래스를 쓰면 빨간불 → 이 파일을 다시 돌린다).

    cd updater/server && python -X utf8 tw_build.py        # static/tw.css · static/tw_tokens.txt 갱신
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
CSS_PATH = os.path.join(STATIC, "tw.css")
TOKENS_PATH = os.path.join(STATIC, "tw_tokens.txt")
CDN = "https://cdn.tailwindcss.com"

_SPLIT = re.compile(r"[\s\"'`<>=;{}()\\,]+")
_OK = re.compile(r"^-?!?[a-z@][a-z0-9:/\[\]\.\-_#%!]*$")


def extract_tokens(html: str) -> set:
    """클래스가 될 수 있는 낱말 전부(넉넉하게). 임의값은 괄호 없는 모양만 쓴다(현재 8종 전부 그렇다)."""
    out = set()
    for t in _SPLIT.split(html):
        t = t.strip(".:")
        if 1 < len(t) <= 80 and _OK.match(t):
            out.add(t)
    return out


def raw_html(html: str) -> str:
    """서빙용 HTML 에서 끼워 넣은 CSS 를 뺀 원문(그 CSS 의 선택자를 후보로 다시 읽지 않게)."""
    return re.sub(r'<style id="tw-built">.*?</style>', "<!--TW_CSS-->", html, count=1, flags=re.S)


def page_tokens() -> set:
    sys.path.insert(0, HERE)
    import main  # noqa: E402
    return extract_tokens(raw_html(main.HTML_LOGIN)) | extract_tokens(raw_html(main.HTML_DASHBOARD))


def build() -> int:
    from playwright.sync_api import sync_playwright
    toks = sorted(page_tokens())
    html = ("<!DOCTYPE html><html class=\"dark\"><head><script src=\"%s\"></script>"
            "<script>tailwind.config={darkMode:'class'}</script></head><body>"
            "<div id=\"all\" style=\"display:none\" class=\"%s\"></div></body></html>") % (CDN, " ".join(toks))
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(html, wait_until="load")
        pg.wait_for_function("() => [...document.head.querySelectorAll('style')].some(s => s.textContent.includes('--tw-'))",
                             timeout=30000)
        pg.wait_for_timeout(1500)       # 생성이 한 번 더 도는 틈
        css = pg.evaluate("() => [...document.head.querySelectorAll('style')].map(s => s.textContent).join('\\n')")
        b.close()
    if "--tw-" not in css or len(css) < 5000:
        print("✘ 생성된 CSS 가 이상하다 (%d자)" % len(css))
        return 1
    os.makedirs(STATIC, exist_ok=True)
    open(CSS_PATH, "w", encoding="utf-8", newline="\n").write(css.strip() + "\n")
    open(TOKENS_PATH, "w", encoding="utf-8", newline="\n").write("\n".join(toks) + "\n")
    print("✔ static/tw.css %d자 · 후보 낱말 %d개" % (len(css), len(toks)))
    return 0


if __name__ == "__main__":
    sys.exit(build())
