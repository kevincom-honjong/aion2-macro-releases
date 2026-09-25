# -*- coding: utf-8 -*-
"""★OCR 라벨링 (2026-09-23 · 주인님 장부 #108)★

매크로가 ★제미나이에 보낼 이미지★ 를 그 순간 서버로도 보낸다 → 주인님이 관제컴(또는 폰)의
/ocr/label 화면에서 정답을 치고 Enter → 라벨이 쌓인다 → 매크로가 /ocr/labels 로 끌어가 쓴다.
[잘못된 이미지] 는 「나쁨」으로 찍고 다음으로 간다.

★main.py 에는 세 줄만 붙인다(MAIN_INCLUDE.diff)★ — 라우터·표·화면은 전부 이 파일.
    import ocr_label as _ocr_label   # noqa: E402
    _ocr_label.init(app, __name__)
  ★순환 import 를 피하는 법★ 이 모듈은 import 시점에 main 을 부르지 않는다. init() 이 받은 모듈
  ★이름★ 으로 핸들러 안에서 sys.modules 를 늦게 찾는다(_m()). 그래서 main 이 끝까지 정의된 뒤의
  인증 도우미(_require_api_key·_require_session·check_session)·ns·clean_pc_id 를 그대로 쓰고,
  시험이 main.X 를 바꿔 끼워도 그대로 따라간다.

엔드포인트 (★계약 정본 = CONTRACT_OCR.md 하나★ — 2026-09-24 두 형식: A 이 파일 처음 모양 · B 매크로 lc/ocr_label.py, 시험 tests/test_ocr_dual.py)
  매크로 (X-Api-Key → 테넌트)
    POST /ocr/submit            이미지 한 장 {pc_id, site, prompt, img_b64, gemini_answer, local_answer, dhash, ts}
    GET  /ocr/labels?since=N&epoch=E   라벨 증분 {labels, near, bad, cleared, cleared_near, cursor, more, reset, epoch}
    GET  /ocr/labels?since=<실수 ts>   (형식 B) {labels:[{site, prompt_sha1, sha1, label, ts, deleted, dhash, phash}], since, more, mode:"ts"}
  대시보드 (세션 쿠키)
    GET  /ocr/label             라벨링 화면(로그인 안 했으면 /login 으로)
    GET  /ocr/queue             대기열(묶음 대표 한 장씩, ×N)
    GET  /ocr/img/{id}          이미지 파일
    POST /ocr/label             {id, label} 저장·고치기 / {id, bad:true} 나쁨
    POST /ocr/skip              {id} 나중에(대기열 맨 뒤로)
    POST /ocr/undo              마지막 한 번 되돌리기
    GET  /ocr/history           최근 라벨(고치기용)
    GET  /ocr/stats             사이트별 대기·라벨·나쁨 + 제미나이/로컬 불일치율
  팜뷰 (X-FV-Token · 테넌트 = FV_TENANT · 에러 {ok:false, error, err, code} = main._fv_err) — 2026-09-23 (밤) 장부 #125
    ★같은 저장소·같은 핵심 함수★ (정본 = updater/FV_API.md «2026-09-23 (밤) 추가 › D»)
    GET  /api/fv/ocr/queue?limit=   GET /api/fv/ocr/img/{id}   POST /api/fv/ocr/label {id, text}
    POST /api/fv/ocr/bad {id}       POST /api/fv/ocr/skip {id}  POST /api/fv/ocr/undo {}
    GET  /api/fv/ocr/history?limit= GET /api/fv/ocr/stats

★지키는 것★
  · 캡차 이미지는 ★절대★ 안 받는다 — site 원문을 자르기 ★전에★ 정규화(NFKC·casefold·결합부호 제거·
    키릴/그리스 닮은꼴 → 라틴·글자/숫자 아닌 것 전부 제거)한 뒤 captcha·캡차·캡챠 가 ★어디든★ 있으면 400
    (recaptcha·hCaptcha·login_captcha 포함, 반증 R5). 이미지를 디코드조차 안 한다.
  · 라벨은 ★DB 에만★ 둔다. 파일 이름에 라벨을 넣지 않는다(옛 사고: 파일명 경로가 한글을 x 로 바꿨다).
    파일 이름은 sha1 이고, site 폴더도 ASCII 가 아니면 site 의 해시로 바꾼다. 테넌트 폴더도 소문자
    ASCII 안전 이름이 아니면 해시(@@<해시>) — 한글 테넌트 둘이 한 폴더(@___)로 접히지 않게(반증 R6).
  · 크기는 ★디코드 전에★ 본다 — Content-Length·★읽으면서 센 바이트★(OCR_BODY_CAP, 청크 전송도, 반증 R9)
    → base64 길이 → 디코드 뒤 실제 바이트. 사람 쪽 POST 본문은 OCR_SMALL_BODY_CAP.
  · 모든 줄에 tenant 를 적고 모든 조회를 tenant 로 가둔다(렌탈 테넌트가 본판 라벨을 못 본다).
    ★비우기도 제출한 테넌트 것만★ 지운다(반증 R7).
  · 쓰기는 한 프로세스 안에서 잠금 하나로 줄 세운다(Railway 는 인스턴스 하나).
  · id 는 부호 있는 64비트 안만 — 밖이면 본문 id 400 · 경로 이미지 id 404(sqlite OverflowError 500 금지, 반증 R4).
  · 속도 상한 키 = 테넌트 + ★정규화한 기본 pc id★(대소문자·구두점·계정 접미사 b~z 무시) + 테넌트 합 상한(반증 R8).

★묶음(near-dup)★ 같은 site 에서 dhash 해밍 거리 ≤ OCR_NEAR_MAX 면 가장 가까운 묶음 ★대표★ 에 붙는다
  (대표하고만 잰다 — 사슬로 번져 묶음이 떠내려가지 않게). 라벨 하나가 묶음 전체에 붙는다.
  ★주인님이 실제로 본 것은 대표 한 장뿐★ — /ocr/labels 의 labels(정답)에는 ★대표의 sha1★ 만 나가고,
  나머지 구성원은 near(dhash → 라벨) 힌트로만 나간다(반증 R1: 12 ↔ 13 은 dhash 거리 1 일 수 있다).
  /ocr/submit 응답도 대표가 아니면 label_match:"near" 로 알린다.

★DB 세대(epoch)★ 표를 처음 만들 때 난수 하나를 ocr_meta 에 적는다. /ocr/labels 가 돌려주고 매크로가
  since 와 같이 보낸다. since>0 인데 epoch 가 없거나 다르면 reset:true 로 처음부터(반증 R2: 새 DB 의
  커서가 옛 커서를 이미 넘으면 「커서가 앞이다」로는 못 알아챈다).

★디스크 상한과 비우는 순서★ (반증 R7 — ★제출한 테넌트의 것만★ 지운다. 남의 줄·파일은 절대 안 건드린다)
  1) 그 테넌트 합계 + 새 파일 > OCR_DISK_CAP → 그 테넌트의 ★라벨이 한 번도 안 된(대기) 묶음★ 을
     오래된 것부터 통째로 지운다(파일·줄). ★ocr_hist 가 있는(한 번이라도 라벨·나쁨 된) 묶음은 안 지운다★
     — 되돌리기로 대기에 돌아간 묶음이 지워지면 매크로가 cleared 를 영영 못 받는다(반증 R3).
  2) 그래도 넘으면 → 그 테넌트 ★나쁨★ 묶음의 이미지 파일만 지운다(줄은 남겨 bad 목록은 유지).
  3) ★전체★ 합계 + 새 파일 > OCR_DISK_HARD_CAP 일 때만 → 그 테넌트의 ★라벨 된★ 이미지 파일을 오래된 것부터
     (대표가 아닌 것 먼저, 그다음 대표). ★줄·라벨은 절대 안 지운다★ — 매크로가 받는 지식은 그대로다.
     그 테넌트 것을 다 지워도 HARD_CAP 을 넘으면 ★아무것도 안 지우고★ 507.
  즉 라벨 된 이미지는 CAP~HARD_CAP 사이에서는 지워지지 않는다. 비운 이미지가 정확히 다시 오면 파일을 되살린다.
"""
import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import sys
import time
import unicodedata
import zlib
from datetime import datetime, timezone
from collections import deque

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

import database as _db

router = APIRouter()

# ─── 상수 (2026-09-23) ─────────────────────────────────────────────────────────
OCR_DIR = os.getenv("OCR_DIR") or os.path.join(os.path.dirname(_db.DB_PATH) or ".", "ocr")
OCR_IMG_MAX = 512 * 1024                          # 한 장 상한(디코드 뒤 바이트)
OCR_B64_MAX = (OCR_IMG_MAX * 4) // 3 + 1024       # base64 글자 수 상한(디코드 전에 거른다)
OCR_BODY_CAP = OCR_B64_MAX + 64 * 1024            # /ocr/submit 본문 전체 상한(Content-Length · 읽으면서 센 바이트)
OCR_SMALL_BODY_CAP = 16 * 1024                    # 사람 쪽 POST(label·skip·bad·undo) 본문 상한 — {id, label} 이 전부다
OCR_DISK_CAP = int(os.getenv("OCR_DISK_CAP_MB", "500")) * 1024 * 1024        # ★테넌트마다★ 대기·나쁨을 비우는 선
OCR_DISK_HARD_CAP = int(os.getenv("OCR_DISK_HARD_CAP_MB", "1000")) * 1024 * 1024  # ★전체 합★ 라벨 된 것까지 비우는 선(못 비우면 507)
OCR_RATE_PER_MIN = 30                             # PC 한 대(정규화한 기본 pc id)당 1분 제출 상한 → 429
OCR_RATE_TENANT_PER_MIN = 300                     # 테넌트 한 곳 합계 1분 상한 → 429 (pc id 를 바꿔 끼워도 여기서 막힌다)
OCR_NEAR_MAX = 4                                  # dhash 해밍 거리 ≤ 이 값이면 같은 묶음
OCR_NEAR_MAX_256 = 0                              # ★256비트 dhash(매크로 lc/ocr_label.hashes, 2026-09-24 두 형식)★ 는 같은 값만 묶는다 —
                                                  #   매크로 리뷰 실측 «한 자리 차이가 4~5비트»(lc/ocr_label.py 머리) → 4 로 묶으면 다른 숫자가 한 묶음이 된다.
                                                  #   묶음 문턱 실측 전(§D) — 0 은 «근접 묶기 안 함»이지 새 임계가 아니다
OCR_CLUSTER_ROWS_MAX = 64                         # ★묶음 하나의 이미지 줄 상한(v4 반증 2차)★ — 넘으면 새 줄·파일 없이 가장
                                                  #   가까운 구성원의 count 만 올린다. 라벨 된 묶음은 비우기 1단계가 안 지우고
                                                  #   3단계는 파일만 지워 줄이 영원히 쌓였다(최악 하루 ~43만 줄·1GB).
OCR_LABEL_MAX = 200                               # 라벨 글자 수 상한
OCR_PROMPT_MAX = 2000
OCR_ANSWER_MAX = 200
OCR_LABELS_PAGE = 5000                            # /ocr/labels 한 번에 내보내는 줄 수(넘으면 more=true)
OCR_SUGGEST_MAX = 200                             # 자동완성 후보(사이트당)

_PNG = b"\x89PNG\r\n\x1a\n"
_JPG = b"\xff\xd8\xff"
_SITE_SAFE = re.compile(r"[^A-Za-z0-9가-힣_\-]")
_DHASH_RE = re.compile(r"^(?:[0-9a-f]{16}|[0-9a-f]{64})\Z")   # 64비트(16자리) · 256비트(64자리 — 매크로 hashes) 둘 다
_PHASH_RE = re.compile(r"^[0-9a-f]{16}\Z")
_PSHA_RE = re.compile(r"^[0-9a-f]{6,40}\Z")                     # 매크로 prompt_sha1 = sha1(prompt)[:12]
_CSHA_RE = re.compile(r"^[0-9a-f]{40}\Z")                       # 매크로 sha1 = sha1(img_b64 ★문자열★)
OCR_SITE_RAW_MAX = 200
OCR_BAD_LABEL = "bad image"
# ★자동 닫기 (2026-09-25 주인님 #218 «OCR 판별 — 홍옥의 섬 계속 올라오네»)★ — 맵 이름(analytics.py:_map_loop)은 답이 16개로 닫혀 있다.
#   묶음의 ★모든 이미지★ 제미나이 답(앞뒤 공백만 걷고)이 ★한 이름과 글자 그대로 같으면★ 사람 대기열에 안 올리고 status "auto" 로 닫는다.
#   · auto 는 대기열·기록(history)·매크로 라벨(/ocr/labels)에 안 나간다 — 사람이 본 정답이 아니다(감사용 표시일 뿐).
#   · 사람이 한 번이라도 손댄 묶음(ocr_hist 줄 있음)은 안 건드린다. 다른 답이 붙으면 auto → pending 으로 되돌린다.
#   · 사람이 auto 묶음에 라벨·나쁨을 주면 그대로 된다(label_core 는 어떤 상태든 받는다).
OCR_AUTO_SITES = {
    "analytics_py__map_loop": frozenset((
        "홍옥의 섬", "정령의 섬", "베르테론 요새 폐허", "드라나 가공구역", "루브레인 구릉지", "환영신의 정원", "엘듄강 중류",
        "어비스 회랑", "데바 생체 연구기지", "갈라진 남쪽 추락지", "갈라진 북쪽 추락지", "라 미렌 요새 남쪽 잔해",
        "라 미렌 요새 북쪽 잔해", "붉은 가시 왕관섬", "영원의 섬", "아울라우 부락")),
}
# ★믿을 만한 자리 자동 닫기 (2026-09-25 주인님 #228 «OCR 80개 찼다»)★ — read_server_kina_open 이 사람 라벨 240·불일치 0 인데
#   대기 80 중 40 을 차지했다. 사람 라벨이 OCR_TRUST_MIN_LABELED 이상이고 ★최근 OCR_TRUST_WINDOW 장★(사람이 라벨 준 묶음의
#   이미지 중 제미나이 답이 있는 것, 라벨 시각 최신순 — /ocr/stats 불일치율과 같은 비교 norm_answer)이 전부 일치하는 site 는
#   ★새 묶음★ 을 status "auto"(label = 제미나이 답) 로 닫는다. 단 OCR_TRUST_SPOT_EVERY 번째마다 1건은 대기로 남긴다(표본 검사).
#   표본에 사람이 다른 답을 주면 그 줄이 최근 창에 들어가 ★창이 다시 깨끗해질 때까지 저절로 꺼진다★(따로 끄는 상태가 없다).
#   auto 는 사람 라벨이 아니라서 창에 안 들어간다(스스로 믿음을 키우지 않는다). 맵 이름 site(OCR_AUTO_SITES)는 위 규칙만.
OCR_TRUST_MIN_LABELED = 200       # 그 site 의 사람 라벨 묶음 수 하한
OCR_TRUST_WINDOW = 200            # 최근 비교 이미지 수 — 이만큼 있고 불일치 0 이어야 한다
OCR_TRUST_SPOT_EVERY = 20         # n 번째마다 1건은 사람 대기열로(표본)                     # ts 방식 행에서 «나쁨» 의 라벨(매크로 BAD_LABELS)
# ★프롬프트 예시값 교체(매크로 1.1.1006, 2026-09-24)로 prompt_sha1[:12] 가 바뀌었다★ — 함대는 옛·새 판이 섞여 돈다.
#   ts 방식 행을 ★옛·새 두 열쇠로 다 내보낸다★(같은 그림 sha1·같은 라벨) — 어느 판이 보낸 라벨이든 어느 판이든 적중.
#   1804·1808·1827 세 키나 칸은 새 판에서 한 프롬프트(62fd6da2e23d)로 합쳐졌다 — 칸 구분은 그림 sha1 이 한다.
#   정본: src/SHARED_ISSUES_아이온2.md «2026-09-24 — 매크로→대시보드: 제미나이 프롬프트 예시값 교체» 표(시험 test_ocr_dual A-*)
PROMPT_SHA1_OLD_NEW = (
    ("3374b731e613", "0f0a0ce55c1a"),
    ("df5e30ca2379", "3f5c4a5e7c8a"),
    ("7f304249dd85", "6a3ad486f392"),
    ("5fef62316fe9", "33a4e0bdfb94"),
    ("dbbb20628ec7", "07d5fa8dd6ef"),
    ("27e3c02c338e", "2418d6a41d0d"),
    ("29aaec1d43eb", "ed36edfc2245"),
    ("c1d1e609f3a6", "ac33c39e5915"),
    ("5bc6cef0911c", "5dc56ee218d9"),
    ("3fe15b029f60", "441da031ad7f"),
    ("1f58164c266d", "62fd6da2e23d"),
    ("8430599954af", "62fd6da2e23d"),
    ("44a66f2d988e", "62fd6da2e23d"),
    ("a805e9a73a83", "e160f18ebb18"),
    ("c4dc183aad5c", "8f65f1643114"),
    ("7f3c7b8e46c0", "6c2387e70a32"),
    ("5905ec7f6ed4", "23ddcd01b0da"),
    ("1f1ca9cf73fa", "95751516f8bf"),
    ("d305be10bd60", "6a1d170cb01f"),
    ("7649cbf459af", "1b16148f62ef"),
    ("ba59cd48acd3", "5141ebd09687"),     # awakening.py:294 (반증 2차 — 정간고수 → 가나다라, 실제 메아리 PC-06/07/09)
)
_PSHA_ALIAS: dict = {}
for _o, _n in PROMPT_SHA1_OLD_NEW:
    _PSHA_ALIAS.setdefault(_o, set()).add(_n)
    _PSHA_ALIAS.setdefault(_n, set()).add(_o)
del _o, _n


def psha_aliases(p: str) -> list:
    """p 와 같은 프롬프트의 다른 판 열쇠들(p 자신 빼고, 정렬). 새 키 62fd6da2e23d 는 옛 키 셋을 다 준다."""
    return sorted(_PSHA_ALIAS.get((p or "")[:12], set()) - {p})
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_CAPTCHA_WORDS = ("captcha", "캡차", "캡챠")      # 정규화한 site 의 ★어디든★ 있으면 거부(반증 R5)
# 키릴·그리스·IPA 닮은꼴 → 라틴(casefold 뒤에 쓴다 — 대문자는 이미 소문자로 접혀 있다)
_CONFUSABLE = str.maketrans({
    "а": "a", "с": "c", "р": "p", "е": "e", "о": "o", "х": "x", "т": "t", "һ": "h", "у": "y", "к": "k",
    "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ӏ": "l", "в": "b", "м": "m", "н": "h",
    "α": "a", "ο": "o", "τ": "t", "ρ": "p", "κ": "k", "ι": "i", "ν": "v", "χ": "x", "ε": "e", "ϲ": "c", "ς": "c",
    "ɑ": "a", "ı": "i", "ɡ": "g", "ʜ": "h", "ᴄ": "c", "ᴘ": "p", "ᴛ": "t", "ᴀ": "a",
})
_HANGUL_FILLERS = "ᅟᅠㅤﾠ"     # 한글 채움 문자 — 글자(Lo)지만 보이지 않는다
_I64_MIN, _I64_MAX = -(2 ** 63), 2 ** 63 - 1     # sqlite INTEGER 범위(밖이면 OverflowError → 500 이었다, 반증 R4)
_TENANT_SAFE = re.compile(r"^[a-z0-9_\-]{1,32}$")
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}

_MAIN_NAME = "main"
_RATE: dict = {}                 # (tenant, 기본 pc) · ("@tenant", tenant) → deque[시각]
_LOCKS: dict = {}                # 이벤트 루프 → asyncio.Lock (시험이 루프를 여럿 쓴다)
_INITED: set = set()             # 표를 만든 DB 경로
_INIT_LOCKS: dict = {}           # 이벤트 루프 → ensure_tables 전용 asyncio.Lock (_lock 과 따로 — _lock 안에서 불려도 안 막히게)


# ─── 연결 ────────────────────────────────────────────────────────────────────
def init(app, main_module_name: str = "main") -> None:
    """main.py 가 부른다 — 라우터를 붙이고 main 모듈 ★이름★ 만 기억한다(순환 import 없음)."""
    global _MAIN_NAME
    _MAIN_NAME = main_module_name
    app.include_router(router)


def _m():
    """main 모듈(늦게 찾기). init 전(시험이 이 파일만 부를 때)이면 main 을 import 한다."""
    mod = sys.modules.get(_MAIN_NAME)
    if mod is None:
        import importlib
        mod = importlib.import_module(_MAIN_NAME)
    return mod


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lk = _LOCKS.get(loop)
    if lk is None:
        if len(_LOCKS) > 8:
            _LOCKS.clear()
        lk = _LOCKS[loop] = asyncio.Lock()
    return lk


def _dbp() -> str:
    return _db.DB_PATH


async def ensure_tables() -> None:
    """★표는 처음 쓸 때 만든다★ — main 의 lifespan 을 안 건드리려고(main.py 수정 최소).
    ★한 번에 하나만 (2026-09-24 아이온2 반증)★ — 옛 표(/data) 위에서 첫 요청 8개가 동시에 오면 ALTER ADD COLUMN 이
    겹쳐 «duplicate column name» 500 이 하나씩 났다(«database is locked» 경합도 같은 자리). 루프마다 잠금 + 잠금 안에서
    다시 확인 + ALTER 의 «duplicate column» 은 무시(다른 프로세스가 먼저 더한 판)."""
    p = _dbp()
    if p in _INITED:
        return
    loop = asyncio.get_running_loop()
    lk = _INIT_LOCKS.get(loop)
    if lk is None:
        if len(_INIT_LOCKS) > 8:
            _INIT_LOCKS.clear()
        lk = _INIT_LOCKS[loop] = asyncio.Lock()
    async with lk:
        if p in _INITED:
            return
        await _ensure_tables_once(p)


async def _ensure_tables_once(p: str) -> None:
    async with aiosqlite.connect(p) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ocr_cluster (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant     TEXT NOT NULL,
                site       TEXT NOT NULL,
                rep_img    INTEGER,
                rep_dhash  TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                label      TEXT,
                qorder     REAL NOT NULL,
                created    REAL NOT NULL,
                labeled_at REAL
            )""")
        await db.execute("CREATE INDEX IF NOT EXISTS ix_ocr_cluster_q ON ocr_cluster(tenant, status, qorder)")
        await db.execute("CREATE INDEX IF NOT EXISTS ix_ocr_cluster_site ON ocr_cluster(tenant, site)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ocr_img (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant     TEXT NOT NULL,
                site       TEXT NOT NULL,
                sha1       TEXT NOT NULL,
                dhash      TEXT NOT NULL,
                cluster_id INTEGER NOT NULL,
                pc_id      TEXT,
                prompt     TEXT,
                gemini     TEXT,
                local      TEXT,
                ts         REAL,
                created    REAL NOT NULL,
                last_seen  REAL,
                count      INTEGER NOT NULL DEFAULT 1,
                nbytes     INTEGER NOT NULL DEFAULT 0,
                relpath    TEXT,
                mime       TEXT,
                on_disk    INTEGER NOT NULL DEFAULT 1,
                seq        INTEGER NOT NULL DEFAULT 0,
                UNIQUE(tenant, site, sha1)
            )""")
        await db.execute("CREATE INDEX IF NOT EXISTS ix_ocr_img_seq ON ocr_img(tenant, seq)")
        await db.execute("CREATE INDEX IF NOT EXISTS ix_ocr_img_cl ON ocr_img(cluster_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS ix_ocr_img_disk ON ocr_img(on_disk, created)")
        await db.execute("CREATE INDEX IF NOT EXISTS ix_ocr_img_tdisk ON ocr_img(tenant, on_disk)")
        # ★두 형식(2026-09-24)★ 더하기만 — 옛 줄은 빈칸(NULL). site_raw = 매크로가 보낸 site 원문("파일.py:함수"),
        #   prompt_sha1·csha1 = 매크로 열쇠, phash = 매크로 근접 섀도용, dhash_bits = 64|256, lts = ts 방식 커서(라벨이 바뀐 서버 시각)
        cur = await db.execute("PRAGMA table_info(ocr_img)")
        have = {r[1] for r in await cur.fetchall()}
        for col, typ in (("site_raw", "TEXT"), ("prompt_sha1", "TEXT"), ("csha1", "TEXT"), ("phash", "TEXT"),
                         ("dhash_bits", "INTEGER"), ("lts", "REAL")):
            if col not in have:
                try:
                    await db.execute("ALTER TABLE ocr_img ADD COLUMN %s %s" % (col, typ))
                except aiosqlite.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
        await db.execute("CREATE INDEX IF NOT EXISTS ix_ocr_img_lts ON ocr_img(tenant, lts)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ocr_hist (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant      TEXT NOT NULL,
                cluster_id  INTEGER NOT NULL,
                prev_status TEXT, prev_label TEXT,
                new_status  TEXT, new_label  TEXT,
                at          REAL NOT NULL,
                undone      INTEGER NOT NULL DEFAULT 0
            )""")
        await db.execute("CREATE INDEX IF NOT EXISTS ix_ocr_hist_t ON ocr_hist(tenant, undone, id)")
        # ★커서는 줄 번호의 최대값이 아니라 따로 센다★ — 지운 줄의 번호가 다시 쓰이면 매크로가 놓친다.
        await db.execute("CREATE TABLE IF NOT EXISTS ocr_seq (tenant TEXT PRIMARY KEY, v INTEGER NOT NULL)")
        # ★DB 세대(epoch)★ — 표와 함께 ★한 번만★ 만드는 난수. 이미 있으면 그대로(OR IGNORE).
        #   DB 가 새로 생기면 값이 바뀌고, 옛 epoch 를 든 매크로는 reset 을 받는다(반증 R2).
        await db.execute("CREATE TABLE IF NOT EXISTS ocr_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        await db.execute("INSERT OR IGNORE INTO ocr_meta(k, v) VALUES('epoch', ?)", (secrets.token_hex(8),))
        await db.commit()
    _INITED.add(p)
    # ★#218★ 이미 대기 중인 맵 이름 묶음도 같은 규칙으로 닫는다 — 프로세스(배포)마다 한 번. 실패해도 표는 쓴다.
    try:
        r = await auto_sweep()
        if r["closed"]:
            print(f"[ocr] 자동 닫기 쓸기: {r['closed']}/{r['checked']} 묶음 {r['by_label']}", flush=True)
    except Exception as e:
        print(f"[ocr] 자동 닫기 쓸기 실패(무시): {type(e).__name__}: {e}", flush=True)


async def _epoch(db) -> str:
    cur = await db.execute("SELECT v FROM ocr_meta WHERE k='epoch'")
    r = await cur.fetchone()
    return r[0] if r else ""


# ─── 작은 도우미 ─────────────────────────────────────────────────────────────
def _bad(code: int, msg: str):
    raise HTTPException(status_code=code, detail=msg)


def _s(v, cap: int) -> str:
    """문자열 칸 — 문자열이 아니면 빈칸, 제어문자 제거, 상한 자르기."""
    if not isinstance(v, str):
        return ""
    return _CTRL.sub("", v)[:cap]


def site_safe(site) -> str:
    """site → 저장 키. 영문·숫자·한글·_- 만 남기고 앞뒤 _- 를 걷는다(경로 문자는 전부 _ 가 된다)."""
    s = unicodedata.normalize("NFKC", site if isinstance(site, str) else "").strip()
    return _SITE_SAFE.sub("_", s)[:48].strip("_-")


def captcha_norm(site) -> str:
    """캡차 판정용 정규화 — ★원문 전체를, 자르기 전에★(반증 R5: 48자 자르기 뒤에 숨은 captcha).
    NFKD(전각→반각·악센트 분리) → 결합부호 제거 → NFC(한글 다시 조합) → casefold → 닮은꼴 → 라틴
    → 글자(L*)·숫자(N*) 아닌 것 전부 제거(영폭·공백·구두점·밑줄). 한글 채움 문자도 뺀다."""
    s = unicodedata.normalize("NFKD", site if isinstance(site, str) else "")
    s = "".join(ch for ch in s if not unicodedata.category(ch).startswith("M"))
    s = unicodedata.normalize("NFC", s).casefold().translate(_CONFUSABLE)
    return "".join(ch for ch in s if unicodedata.category(ch)[0] in "LN" and ch not in _HANGUL_FILLERS)


def is_captcha_site(site) -> bool:
    """★캡차는 절대 안 받는다★ — 정규화한 원문의 ★어디든★ captcha·캡차·캡챠 가 있으면 참
    (recaptcha·hCaptcha·login_captcha·키릴 닮은꼴·「캡 차」 전부)."""
    n = captcha_norm(site)
    return any(w in n for w in _CAPTCHA_WORDS)


def _tenant_dir(tenant: str) -> str:
    """테넌트 폴더 이름. 소문자 ASCII 안전 이름이면 @이름, 아니면 @@<sha1 20자>.
    ★한글·대문자·기호 테넌트는 해시★ — 예전엔 기호를 _ 로 바꿔 「홍길동」「김철수」가 둘 다 @___ 였다
    (남의 파일을 지웠다, 반증 R6). 대문자도 해시로 — Windows·macOS 는 대소문자만 다른 폴더를 하나로 본다.
    안전 이름에는 '@' 가 못 들어가므로 @@해시 와 절대 안 겹친다."""
    if _TENANT_SAFE.match(tenant or "") and tenant.upper() not in _WIN_RESERVED:
        return "@" + tenant
    return "@@" + hashlib.sha1((tenant or "").encode("utf-8")).hexdigest()[:20]


def _site_dir(tenant: str, site: str) -> str:
    """디스크 폴더(상대). ★라벨은 절대 안 들어간다★. 한글 site 는 해시로(파일시스템 인코딩 사고 방지)."""
    d = site if site.isascii() else "u" + hashlib.sha1(site.encode("utf-8")).hexdigest()[:16]
    if d.upper() in _WIN_RESERVED:
        d = "s_" + d
    if tenant != "main":        # main 은 접두사 없음(ns() 와 같은 규칙) · '@' 는 site 에 못 들어간다
        return os.path.join(_tenant_dir(tenant), d)
    return d


def _abs(relpath: str) -> str:
    """상대 경로 → 절대. ★OCR_DIR 밖이면 거부★(경로 순회 방어의 마지막 줄)."""
    root = os.path.realpath(OCR_DIR)
    p = os.path.realpath(os.path.join(root, relpath or ""))
    if os.path.commonpath([root, p]) != root or p == root:
        raise ValueError("OCR_DIR 밖 경로")
    return p


def _norm_label(v) -> str:
    """NFC · 서식 문자(Cf — 영폭 공백·ZWJ·BOM·방향 표시)와 한글 채움 문자 제거 · 앞뒤 공백 제거.
    ★보이지 않는 글자만 있는 라벨은 빈 라벨(400)★ — 예전엔 "\\u200b" 가 라벨로 저장됐다."""
    s = unicodedata.normalize("NFC", _s(v, OCR_LABEL_MAX * 2))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf" and ch not in _HANGUL_FILLERS)
    return s.strip()[:OCR_LABEL_MAX]


def norm_answer(s) -> str:
    """불일치율 비교용 — 전각/반각·대소문자·공백 차이는 같은 답으로 본다."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s if isinstance(s, str) else "")).lower()


def hamming(a: str, b: str) -> int:
    """길이(64·256비트)가 다르면 아주 먼 거리 — 두 형식이 한 묶음에 섞이지 않는다(2026-09-24)."""
    if not a or not b or len(a) != len(b):
        return 1 << 20
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def near_max(dh: str) -> int:
    return OCR_NEAR_MAX_256 if len(dh or "") == 64 else OCR_NEAR_MAX


def _rate_dq(key, now: float) -> deque:
    dq = _RATE.get(key)
    if dq is None:
        if len(_RATE) > 2000:
            for k in [k for k, d in _RATE.items() if not d or now - d[-1] > 60]:
                _RATE.pop(k, None)
        dq = _RATE[key] = deque()
    while dq and now - dq[0] > 60:
        dq.popleft()
    return dq


def _rate_hit(key, now: float, limit: int = None) -> bool:
    """True = 상한 초과. 1분 미끄럼 창."""
    dq = _rate_dq(key, now)
    if len(dq) >= (OCR_RATE_PER_MIN if limit is None else limit):
        return True
    dq.append(now)
    return False


def base_pc(pc) -> str:
    """속도 상한용 기본 pc id — NFKC·casefold·글자/숫자 아닌 것 제거·숫자 뒤 계정 접미사(b~z) 하나 제거.
    PC-22 · pc-22 · PC-22d · 'PC-22.' 가 전부 'pc22' (반증 R8: 대소문자·접미사를 바꿔 끼우면 상한이 무한이었다)."""
    s = unicodedata.normalize("NFKC", pc if isinstance(pc, str) else "").casefold()
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] in "LN")
    return re.sub(r"(?<=[0-9])[b-z]$", "", s)


def _rate_key(tenant: str, pc) -> tuple:
    return (tenant, base_pc(pc))


def _rate_check(tenant: str, pc, now: float):
    """PC(기본 id) 한 대 · 테넌트 합 둘 다 본다. 넘은 쪽의 한국어 사유를 돌려주고, 안 넘었으면 둘 다에 한 번 센다
    (거절된 제출은 어느 창에도 안 쌓인다)."""
    dp, dt = _rate_dq(_rate_key(tenant, pc), now), _rate_dq(("@tenant", tenant), now)
    if len(dp) >= OCR_RATE_PER_MIN:
        return "OCR 제출이 너무 잦습니다(PC 당 분당 %d장)" % OCR_RATE_PER_MIN
    if len(dt) >= OCR_RATE_TENANT_PER_MIN:
        return "OCR 제출이 너무 잦습니다(테넌트 합 분당 %d장)" % OCR_RATE_TENANT_PER_MIN
    dp.append(now)
    dt.append(now)
    return None


def _i64(v) -> bool:
    return _I64_MIN <= v <= _I64_MAX


async def _next_seq(db, tenant: str, n: int = 1) -> int:
    """tenant 커서를 n 칸 올리고 ★첫 번호★ 를 준다."""
    cur = await db.execute("SELECT v FROM ocr_seq WHERE tenant=?", (tenant,))
    row = await cur.fetchone()
    v = row[0] if row else 0
    await db.execute("INSERT OR REPLACE INTO ocr_seq(tenant, v) VALUES(?, ?)", (tenant, v + n))
    return v + 1


async def _bump_members(db, tenant: str, cluster_id: int) -> None:
    """묶음의 라벨·상태가 바뀌었다 → 구성원마다 새 커서 번호(매크로 증분 동기화)."""
    cur = await db.execute("SELECT id FROM ocr_img WHERE tenant=? AND cluster_id=? ORDER BY id", (tenant, cluster_id))
    ids = [r[0] for r in await cur.fetchall()]
    if not ids:
        return
    first = await _next_seq(db, tenant, len(ids))
    lts = await _next_lts(db, tenant)
    for i, iid in enumerate(ids):
        await db.execute("UPDATE ocr_img SET seq=?, lts=? WHERE id=?", (first + i, lts, iid))


async def _next_lts(db, tenant: str) -> float:
    """ts 방식 커서 — 서버 시각이되 ★테넌트마다 늘 앞선 값보다 크다★(ocr_meta 'lts:<tenant>').
    같은 시각에 두 번 바뀌어도, 시계가 뒤로 가도, 매크로의 since(받은 최대 ts) 뒤에 새 변화가 숨지 않는다."""
    k = "lts:" + tenant
    cur = await db.execute("SELECT v FROM ocr_meta WHERE k=?", (k,))
    r = await cur.fetchone()
    try:
        prev = float(r[0]) if r else 0.0
    except (TypeError, ValueError):
        prev = 0.0
    v = max(time.time(), prev + 1e-3)
    await db.execute("INSERT OR REPLACE INTO ocr_meta(k, v) VALUES(?, ?)", (k, repr(v)))
    return v


def _rm(relpath: str) -> None:
    try:
        os.remove(_abs(relpath))
    except (OSError, ValueError):
        pass


async def _disk_used(db, tenant: str = None) -> int:
    """on_disk 바이트 합 — tenant 를 주면 그 테넌트 것만, 안 주면 전체."""
    if tenant is None:
        cur = await db.execute("SELECT COALESCE(SUM(nbytes),0) FROM ocr_img WHERE on_disk=1")
    else:
        cur = await db.execute("SELECT COALESCE(SUM(nbytes),0) FROM ocr_img WHERE tenant=? AND on_disk=1", (tenant,))
    return int((await cur.fetchone())[0])


def auto_label(site: str, answers) -> "str | None":
    """자동 닫기 판정 — site 가 OCR_AUTO_SITES 에 있고, 답이 ★하나 이상★ 이며 ★전부★ strip 뒤 같은 한 이름이면 그 이름.
    빈 답·다른 답이 하나라도 섞이면 None(사람이 본다)."""
    names = OCR_AUTO_SITES.get(site)
    if not names:
        return None
    got = {(a if isinstance(a, str) else "").strip() for a in answers}
    if len(got) != 1:
        return None
    (one,) = got
    return one if one in names else None


async def site_trusted(db, tenant: str, site: str) -> dict:
    """#228 — {"trusted", "labeled", "compared", "disagree"}. compared/disagree 는 최근 OCR_TRUST_WINDOW 장 기준."""
    cur = await db.execute("SELECT COUNT(*) FROM ocr_cluster WHERE tenant=? AND site=? AND status='labeled'", (tenant, site))
    labeled = int((await cur.fetchone())[0])
    out = {"trusted": False, "labeled": labeled, "compared": 0, "disagree": 0}
    if labeled < OCR_TRUST_MIN_LABELED:
        return out
    # ★순서 = 그 판단이 «생긴» 때 — MAX(라벨 시각, 이미지 도착)★ (#228 반증 1) 옛 라벨 묶음에 오늘 붙은 오독 이미지가
    #   옛 라벨 시각에 묻혀 창에 안 들어오던 것. ★나쁨(bad)★ 묶음의 답 있는 이미지도 창에 넣고 불일치로 센다(반증 4 — 표본을
    #   «잘못된 이미지» 로 찍었는데 제미나이가 답을 지어냈으면 믿으면 안 된다). 라벨 수 하한은 사람 라벨(labeled)만.
    cur = await db.execute(
        "SELECT i.gemini, c.label, c.status FROM ocr_img i JOIN ocr_cluster c ON c.id=i.cluster_id "
        "WHERE i.tenant=? AND c.site=? AND c.status IN ('labeled','bad') AND TRIM(COALESCE(i.gemini,''))<>'' "
        "ORDER BY MAX(COALESCE(c.labeled_at, 0), COALESCE(i.created, 0)) DESC, i.id DESC", (tenant, site))
    while out["compared"] < OCR_TRUST_WINDOW:
        rows = await cur.fetchmany(OCR_TRUST_WINDOW)
        if not rows:
            break
        for gm, lb, cst in rows:
            if not norm_answer(gm):
                continue                            # 공백만 있는 답 — stats 와 같이 분모에서 뺀다
            out["compared"] += 1
            if cst == "bad" or norm_answer(gm) != norm_answer(lb):
                out["disagree"] += 1
            if out["compared"] >= OCR_TRUST_WINDOW:
                break
    out["trusted"] = out["compared"] >= OCR_TRUST_WINDOW and out["disagree"] == 0
    return out


async def _trust_spot(db, tenant: str, site: str) -> bool:
    """#228 표본 셈 — 믿는 site 의 새 묶음마다 1 올리고 OCR_TRUST_SPOT_EVERY 번째면 True(사람 대기열로). 재배포에도 이어진다."""
    k = "trust_n:%s:%s" % (tenant, site)
    cur = await db.execute("SELECT v FROM ocr_meta WHERE k=?", (k,))
    r = await cur.fetchone()
    try:
        n = int(r[0]) + 1 if r else 1
    except (TypeError, ValueError):
        n = 1
    await db.execute("INSERT INTO ocr_meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(n)))
    return n % OCR_TRUST_SPOT_EVERY == 0


async def _trust_eval(db, tenant: str, cid: int, site: str, status: str, label, now: float, new: bool) -> str:
    """#228 — 맵 이름이 아닌 site. 새 대기 묶음 → 믿는 site 면 auto(표본 제외) · auto 묶음에 다른 답이 붙으면 대기로."""
    if status == "auto":
        cur = await db.execute("SELECT gemini FROM ocr_img WHERE cluster_id=? AND tenant=?", (cid, tenant))
        if all(norm_answer(_norm_label(g)) == norm_answer(label) for (g,) in await cur.fetchall()):   # auto 라벨과 같은 정규화(반증 5)
            return ""
        await db.execute("UPDATE ocr_cluster SET status='pending', label=NULL, labeled_at=NULL WHERE id=?", (cid,))
        return "reopen"
    if not new:
        return ""                                   # 이미 사람 대기열에 있던 묶음은 그대로(사람이 보던 것)
    cur = await db.execute("SELECT gemini FROM ocr_img WHERE cluster_id=? AND tenant=?", (cid, tenant))
    gms = [g for (g,) in await cur.fetchall()]
    lab = _norm_label(gms[0]) if len(gms) == 1 and isinstance(gms[0], str) else ""
    if not lab or not norm_answer(lab):
        return ""                                   # 답 없는 제출은 사람 몫
    if not (await site_trusted(db, tenant, site))["trusted"]:
        return ""
    if await _trust_spot(db, tenant, site):
        return "spot"                               # 표본 — 대기로 남긴다
    await db.execute("UPDATE ocr_cluster SET status='auto', label=?, labeled_at=? WHERE id=?", (lab, now, cid))
    return "auto"


async def _auto_eval(db, tenant: str, cid: int, now: float, new: bool = False) -> str:
    """묶음 하나를 다시 본다 → "auto"(대기 → 자동 닫음) · "reopen"(자동 → 대기) · "spot"(#228 표본) · ""(그대로).
    commit 은 부르는 쪽. new = 이 제출이 묶음을 새로 만들었다(#228 은 새 묶음만 닫는다)."""
    cur = await db.execute("SELECT site, status, label FROM ocr_cluster WHERE id=? AND tenant=?", (cid, tenant))
    r = await cur.fetchone()
    if not r or r[1] not in ("pending", "auto"):
        return ""
    if r[0] not in OCR_AUTO_SITES:
        cur = await db.execute("SELECT 1 FROM ocr_hist WHERE cluster_id=? LIMIT 1", (cid,))
        if await cur.fetchone():
            return ""                               # 사람이 손댄 묶음 — 사람 몫
        return await _trust_eval(db, tenant, cid, r[0], r[1], r[2], now, new)
    cur = await db.execute("SELECT 1 FROM ocr_hist WHERE cluster_id=? LIMIT 1", (cid,))
    if await cur.fetchone():
        return ""                                   # 사람이 손댄 묶음 — 사람 몫
    cur = await db.execute("SELECT gemini FROM ocr_img WHERE cluster_id=? AND tenant=?", (cid, tenant))
    lab = auto_label(r[0], [x[0] for x in await cur.fetchall()])
    if lab and r[1] == "pending":
        await db.execute("UPDATE ocr_cluster SET status='auto', label=?, labeled_at=? WHERE id=?", (lab, now, cid))
        return "auto"
    if not lab and r[1] == "auto":
        await db.execute("UPDATE ocr_cluster SET status='pending', label=NULL, labeled_at=NULL WHERE id=?", (cid,))
        return "reopen"
    if lab and r[1] == "auto":
        cur = await db.execute("SELECT label FROM ocr_cluster WHERE id=?", (cid,))
        if (await cur.fetchone())[0] != lab:        # 전부 같은 다른 이름으로 바뀐 경우(대표만 있던 묶음) — 라벨을 맞춘다
            await db.execute("UPDATE ocr_cluster SET label=?, labeled_at=? WHERE id=?", (lab, now, cid))
    return ""


async def auto_sweep(tenant: str = None, dry: bool = False) -> dict:
    """지금 대기 중인 자동 닫기 사이트 묶음을 같은 규칙으로 닫는다(tenant=None 이면 전 테넌트).
    dry=True 면 아무것도 안 바꾸고 셈만. → {"checked", "closed", "by_label": {이름: n}}"""
    await ensure_tables()
    out = {"checked": 0, "closed": 0, "by_label": {}}
    sites = sorted(OCR_AUTO_SITES)
    q = ("SELECT c.id, c.tenant, c.site FROM ocr_cluster c WHERE c.status='pending' AND c.site IN (%s)"
         % ",".join("?" * len(sites)))
    args = list(sites)
    if tenant is not None:
        q += " AND c.tenant=?"
        args.append(tenant)
    async with _lock():
        async with aiosqlite.connect(_dbp()) as db:
            cur = await db.execute(q + " ORDER BY c.id", args)
            rows = await cur.fetchall()
            now = time.time()
            for cid, ten, site in rows:
                out["checked"] += 1
                cur = await db.execute("SELECT 1 FROM ocr_hist WHERE cluster_id=? LIMIT 1", (cid,))
                if await cur.fetchone():
                    continue
                cur = await db.execute("SELECT gemini FROM ocr_img WHERE cluster_id=? AND tenant=?", (cid, ten))
                lab = auto_label(site, [x[0] for x in await cur.fetchall()])
                if not lab:
                    continue
                out["closed"] += 1
                out["by_label"][lab] = out["by_label"].get(lab, 0) + 1
                if not dry:
                    await db.execute("UPDATE ocr_cluster SET status='auto', label=?, labeled_at=? WHERE id=? AND status='pending'",
                                     (lab, now, cid))
            if not dry:
                await db.commit()
    return out


async def evict_for(db, tenant: str, need: int) -> dict:
    """새 파일 need 바이트 자리를 ★tenant 자기 것만 지워서★ 만든다. 순서는 머리 주석 「디스크 상한과 비우는 순서」.
    out["full"] = True 면 전체 HARD_CAP 을 못 지킨다 → 호출부가 507 (그때 3단계는 아무것도 안 지운다)."""
    used = await _disk_used(db, tenant)             # 1·2단계 = 테넌트 몫(OCR_DISK_CAP)
    total = await _disk_used(db)                    # 3단계 = 전체(OCR_DISK_HARD_CAP)
    out = {"pending_clusters": 0, "bad_files": 0, "labeled_files": 0, "full": False}   # bad_files 에는 자동 닫은(auto) 파일도 센다
    # 1) 대기 묶음 — 오래된 것부터 통째로. ★라벨·나쁨 기록(ocr_hist)이 한 번이라도 있는 묶음은 뺀다(반증 R3)★
    while used + need > OCR_DISK_CAP:
        cur = await db.execute(
            "SELECT c.id FROM ocr_cluster c WHERE c.tenant=? AND c.status='pending' "
            "AND NOT EXISTS (SELECT 1 FROM ocr_hist h WHERE h.cluster_id=c.id) ORDER BY c.created, c.id LIMIT 20", (tenant,))
        cids = [r[0] for r in await cur.fetchall()]
        if not cids:
            break
        for cid in cids:
            cur = await db.execute("SELECT relpath, nbytes, on_disk FROM ocr_img WHERE cluster_id=? AND tenant=?", (cid, tenant))
            for rel, nb, od in await cur.fetchall():
                if od:
                    _rm(rel)
                    used -= int(nb or 0)
                    total -= int(nb or 0)
            await db.execute("DELETE FROM ocr_img WHERE cluster_id=? AND tenant=?", (cid, tenant))
            await db.execute("DELETE FROM ocr_cluster WHERE id=? AND tenant=?", (cid, tenant))
            out["pending_clusters"] += 1
            if used + need <= OCR_DISK_CAP:
                break
    # 2) 나쁨·자동 닫음(auto, #218) — 파일만(줄은 남긴다). auto 는 사람이 안 본 기계 판정이라 라벨 된 것보다 먼저 비운다
    while used + need > OCR_DISK_CAP:
        cur = await db.execute(
            "SELECT i.id, i.relpath, i.nbytes FROM ocr_img i JOIN ocr_cluster c ON c.id=i.cluster_id "
            "WHERE i.tenant=? AND i.on_disk=1 AND c.status IN ('bad','auto') ORDER BY i.created, i.id LIMIT 50", (tenant,))
        rows = await cur.fetchall()
        if not rows:
            break
        for iid, rel, nb in rows:
            _rm(rel)
            await db.execute("UPDATE ocr_img SET on_disk=0 WHERE id=?", (iid,))
            used -= int(nb or 0)
            total -= int(nb or 0)
            out["bad_files"] += 1
            if used + need <= OCR_DISK_CAP:
                break
    # 3) 라벨 된 것 — ★전체가 HARD_CAP 을 넘을 때만★, ★이 테넌트 것만★, 파일만(줄·라벨은 영구)
    if total + need > OCR_DISK_HARD_CAP:
        cur = await db.execute(
            "SELECT COALESCE(SUM(i.nbytes),0) FROM ocr_img i JOIN ocr_cluster c ON c.id=i.cluster_id "
            "WHERE i.tenant=? AND i.on_disk=1 AND c.status='labeled'", (tenant,))
        if total + need - int((await cur.fetchone())[0]) > OCR_DISK_HARD_CAP:
            out["full"] = True                      # 다 지워도 모자란다 → 괜히 지우지 않는다
            return out
    while total + need > OCR_DISK_HARD_CAP:
        cur = await db.execute(
            "SELECT i.id, i.relpath, i.nbytes FROM ocr_img i JOIN ocr_cluster c ON c.id=i.cluster_id "
            "WHERE i.tenant=? AND i.on_disk=1 AND c.status='labeled' "
            "ORDER BY (CASE WHEN c.rep_img=i.id THEN 1 ELSE 0 END), i.created, i.id LIMIT 50", (tenant,))
        rows = await cur.fetchall()
        if not rows:
            out["full"] = True
            break
        for iid, rel, nb in rows:
            _rm(rel)
            await db.execute("UPDATE ocr_img SET on_disk=0 WHERE id=?", (iid,))
            total -= int(nb or 0)
            out["labeled_files"] += 1
            if total + need <= OCR_DISK_HARD_CAP:
                break
    return out


# ─── 매크로: 제출 ────────────────────────────────────────────────────────────
async def read_json_capped(request: Request, cap: int, err):
    """본문을 ★읽으면서 세다가★ cap 을 넘는 순간 413 — Content-Length 가 없어도(청크 전송, 반증 R9).
    err(code, msg) 가 예외를 던진다(제출은 HTTPException, 사람 쪽은 OcrError).
    JSON 이 아니거나(깨짐·비UTF8·깊이 폭탄) 객체가 아니면 400."""
    try:
        if int(request.headers.get("content-length") or 0) > cap:
            err(413, "본문이 너무 큽니다")
    except ValueError:
        pass
    stream = getattr(request, "stream", None)
    if stream is None:                  # 시험 발판 Req(_harness) — 본문이 이미 dict 다
        try:
            body = await request.json()
        except Exception:
            err(400, "JSON 본문이 필요합니다")
    else:
        buf = bytearray()
        async for chunk in stream():
            buf += chunk
            if len(buf) > cap:
                err(413, "본문이 너무 큽니다")
        try:
            body = json.loads(bytes(buf))
        except Exception:               # ValueError·UnicodeDecodeError·RecursionError(깊이 10만)
            err(400, "JSON 본문이 필요합니다")
    if not isinstance(body, dict):
        err(400, "JSON 객체가 필요합니다")
    return body


def _write_file(rel: str, raw: bytes) -> None:
    path = _abs(rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, path)


@router.post("/ocr/submit")
async def ocr_submit(request: Request):
    m = _m()
    tenant = m._require_api_key(request)
    # ① 본문 크기 — Content-Length 로 먼저, 청크 전송은 읽으면서 센다
    body = await read_json_capped(request, OCR_BODY_CAP, _bad)
    raw_pc = body.get("pc_id") if isinstance(body.get("pc_id"), str) else ""
    pc = m.clean_pc_id(raw_pc).strip()
    if not pc:
        _bad(400, "pc_id 가 필요합니다")
    raw_site = body.get("site")
    # ② ★캡차는 절대 안 받는다★ — 다른 어떤 검사보다 먼저, ★원문 전체★ 로(이미지를 디코드조차 안 한다)
    if is_captcha_site(raw_site):
        _bad(400, "캡차 이미지는 받지 않습니다")
    site = site_safe(raw_site)
    if not site:
        _bad(400, "site 가 필요합니다(영문·숫자·한글·_-)")
    # ③ 속도 상한 — PC(정규화한 기본 id) 한 대 · 테넌트 합
    why = _rate_check(tenant, raw_pc[:128], time.time())     # ★소독 전 원문★으로 — 전각 ＰＣ－２２ 가 소독 뒤 '_____' 로 새 창을 얻지 않게
    if why:
        _bad(429, why)
    dh = body.get("dhash")
    dh = dh.strip().lower() if isinstance(dh, str) else ""
    if not _DHASH_RE.match(dh):
        _bad(400, "dhash 는 16자리(64비트) 또는 64자리(256비트) 16진수여야 합니다")
    b64 = body.get("img_b64")
    if not isinstance(b64, str) or not b64:
        _bad(400, "img_b64 가 필요합니다")
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[-1]
    # ④ 디코드 전에 길이로 거른다
    if len(b64) > OCR_B64_MAX:
        _bad(413, "이미지가 너무 큽니다(최대 %dKB)" % (OCR_IMG_MAX // 1024))
    b64_sent = body.get("img_b64")               # 매크로 sha1 = sha1(img_b64 ★보낸 문자열 그대로★)
    try:
        raw = base64.b64decode(b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        _bad(400, "img_b64 가 올바른 base64 가 아닙니다")
    if len(raw) > OCR_IMG_MAX:
        _bad(413, "이미지가 너무 큽니다(최대 %dKB)" % (OCR_IMG_MAX // 1024))
    ts = body.get("ts")
    ts = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts == ts and abs(ts) < 1e11 else None
    return await submit_core(tenant, pc, site, raw, dh, ts, _s(body.get("prompt"), OCR_PROMPT_MAX),
                             _s(body.get("gemini_answer"), OCR_ANSWER_MAX), _s(body.get("local_answer"), OCR_ANSWER_MAX),
                             extra=submit_extra(body, raw_site, b64_sent))


def _hexf(v, rx) -> str:
    v = v.strip().lower() if isinstance(v, str) else ""
    return v if rx.match(v) else ""


def submit_extra(body: dict, raw_site, b64_sent) -> dict:
    """매크로 형식(lc/ocr_label.after) 칸 — ★틀려도 거부하지 않고 빈칸★(옛 형식 제출도 그대로 200).
    site_raw = site 원문(제어문자만 뺀다 — 매크로 열쇠와 글자 하나 안 다르게) · csha1 = 매크로 sha1, 없으면 서버가 같은 식으로 잰다."""
    csha = _hexf(body.get("sha1"), _CSHA_RE)
    if not csha and isinstance(b64_sent, str):
        csha = hashlib.sha1(b64_sent.encode("utf-8", "ignore")).hexdigest()
    return {"site_raw": _s(raw_site, OCR_SITE_RAW_MAX), "prompt_sha1": _hexf(body.get("prompt_sha1"), _PSHA_RE),
            "csha1": csha, "phash": _hexf(body.get("phash"), _PHASH_RE)}


async def submit_core(tenant: str, pc: str, site: str, raw: bytes, dh: str, ts, prompt: str, gem: str, loc: str,
                      err=None, extra=None) -> dict:
    """★제출 핵심(2026-09-24 떼어 냄 — 동작 그대로)★ 검사(캡차·속도·dhash·크기)를 ★마친★ 이미지 한 장을 저장소에 넣는다.
    /ocr/submit 과 /bugs 씨앗(seed_bugs_core·seed_one)이 ★같은 길★ 을 탄다 — 정확 일치·묶음·줄 상한·비우기·507 가 같다.
    err(code, msg) 가 실패를 던진다(기본 _bad = HTTPException · 씨앗은 _ocr_err = OcrError)."""
    err = err or _bad
    ex = extra or {}
    ex = {k: (ex.get(k) or "") for k in ("site_raw", "prompt_sha1", "csha1", "phash")}
    if raw.startswith(_PNG):
        ext, mime = "png", "image/png"
    elif raw.startswith(_JPG):
        ext, mime = "jpg", "image/jpeg"
    else:
        err(400, "PNG 나 JPEG 만 받습니다")
    sha = hashlib.sha1(raw).hexdigest()
    now = time.time()

    await ensure_tables()
    async with _lock():
        async with aiosqlite.connect(_dbp()) as db:
            # ⑤ 정확 일치 — count 만 올린다(대기열에 새로 안 넣는다)
            cur = await db.execute(
                "SELECT i.id, i.cluster_id, i.on_disk, i.relpath FROM ocr_img i "
                "JOIN ocr_cluster c ON c.id=i.cluster_id WHERE i.tenant=? AND i.site=? AND i.sha1=?", (tenant, site, sha))
            row = await cur.fetchone()
            if row:
                iid, cid, od, rel0 = row
                if not od:
                    # 비우기로 파일만 지워진 이미지가 똑같이 다시 왔다 → ★파일을 되살린다★(자리는 이 테넌트 것에서).
                    #   전체가 가득이면 되살리기만 건너뛴다 — 정확 일치 count 는 그대로 센다(507 아님).
                    ev = await evict_for(db, tenant, len(raw))
                    if not ev["full"]:
                        try:
                            _write_file(rel0, raw)
                            await db.execute("UPDATE ocr_img SET on_disk=1 WHERE id=?", (iid,))
                        except (OSError, ValueError):
                            pass              # 되살리기 실패는 조용히 — count 는 그대로 올린다
                await db.execute("UPDATE ocr_img SET count=count+1, last_seen=? WHERE id=?", (now, iid))
                await _fill_extra(db, tenant, iid, cid, ex)
                await db.commit()
                return await _submit_reply(db, tenant, iid, cid, "exact", None)
            # ⑦ 비슷한 것 — 가장 가까운 묶음 대표(거리 같으면 먼저 생긴 묶음)
            async def _nearest():
                cur_ = await db.execute("SELECT id, rep_dhash FROM ocr_cluster WHERE tenant=? AND site=?", (tenant, site))
                b, bd = None, near_max(dh) + 1     # 길이가 다른 대표는 hamming 이 1<<20 — 안 묶인다
                for cid_, rdh in await cur_.fetchall():
                    d = hamming(dh, rdh)
                    if d < bd or (d == bd and b is not None and cid_ < b):
                        b, bd = cid_, d
                return b, bd
            best, bestd = await _nearest()
            # ⑦-b ★묶음 줄 상한★ — 꽉 찬 묶음에는 새 줄·파일을 안 만들고 가장 가까운 구성원에 센다(v4 반증 2차).
            #   응답은 그 구성원 줄 — 대표여도 label_match 는 "near"(이 이미지는 사람이 본 그 한 장이 아니다, 반증 R1).
            #   ★자리 만들기(⑥)보다 먼저★(v4 델타 반증) — 파일을 안 쓰는 제출이 비우기를 돌려 남의 대기 묶음을 지우면 안 된다.
            if best is not None:
                cur = await db.execute("SELECT id, dhash FROM ocr_img WHERE cluster_id=? AND tenant=?", (best, tenant))
                mem = await cur.fetchall()
                if len(mem) >= OCR_CLUSTER_ROWS_MAX:
                    mid = min(mem, key=lambda r: (hamming(dh, r[1]), r[0]))[0]
                    await db.execute("UPDATE ocr_img SET count=count+1, last_seen=? WHERE id=?", (now, mid))
                    await db.commit()
                    rep_ = await _submit_reply(db, tenant, mid, best, "near", bestd)
                    if rep_.get("label_match") == "exact":
                        rep_["label_match"] = "near"
                    rep_["capped"] = True
                    return rep_
            # ⑥ 새 파일 자리를 만든다 → ★묶음은 다시 찾는다★(찾아 둔 대기 묶음이 비우기에 지워지면 고아 줄이 된다)
            ev = await evict_for(db, tenant, len(raw))
            if ev["full"]:
                await db.commit()             # 1·2단계에서 지운 줄은 파일과 짝을 맞춰 둔다
                err(507, "OCR 저장 공간이 가득 찼습니다(전체 상한)")
            best, bestd = await _nearest()
            # ⑧ 파일 → 줄
            rel = os.path.join(_site_dir(tenant, site), "%s.%s" % (sha, ext))
            _write_file(rel, raw)
            try:
                if best is None:
                    cur = await db.execute("SELECT COALESCE(MAX(qorder), 0) FROM ocr_cluster WHERE tenant=? AND status='pending'",
                                           (tenant,))
                    q = max(now, float((await cur.fetchone())[0]) + 1e-3)
                    cur = await db.execute(
                        "INSERT INTO ocr_cluster(tenant, site, rep_dhash, status, qorder, created) VALUES(?,?,?,?,?,?)",
                        (tenant, site, dh, "pending", q, now))
                    cid, kind = cur.lastrowid, "new"
                else:
                    cid, kind = best, "near"
                cur = await db.execute(
                    "INSERT INTO ocr_img(tenant, site, sha1, dhash, cluster_id, pc_id, prompt, gemini, local, ts, created,"
                    " last_seen, count, nbytes, relpath, mime, on_disk, seq, site_raw, prompt_sha1, csha1, phash, dhash_bits)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,1,0,?,?,?,?,?)",
                    (tenant, site, sha, dh, cid, pc, prompt, gem, loc, ts, now, now, len(raw), rel, mime,
                     ex["site_raw"], ex["prompt_sha1"], ex["csha1"], ex["phash"], len(dh) * 4))
                iid = cur.lastrowid
                if kind == "new":
                    await db.execute("UPDATE ocr_cluster SET rep_img=? WHERE id=?", (iid, cid))
                else:
                    cur = await db.execute("SELECT status FROM ocr_cluster WHERE id=?", (cid,))
                    if (await cur.fetchone())[0] in ("labeled", "bad"):   # 이미 답이 있는 묶음 → 매크로에 바로 나간다(near 로)
                        await db.execute("UPDATE ocr_img SET seq=? WHERE id=?", (await _next_seq(db, tenant), iid))
                await _auto_eval(db, tenant, cid, now, kind == "new")   # ★#218★ 맵 이름 · ★#228★ 믿을 만한 site 는 사람 대기열에 안 올린다
                await db.commit()
            except Exception:
                _rm(rel)
                raise
            return await _submit_reply(db, tenant, iid, cid, kind, bestd if kind == "near" else None)




async def _fill_extra(db, tenant, iid, cid, ex) -> None:
    """정확 일치 재제출 — 비어 있던 매크로 칸만 채운다(있던 값은 안 바꾼다: 같은 그림·다른 프롬프트면 ★처음 열쇠★ 가 남는다).
    채웠고 묶음에 답이 있으면 ts 커서를 올린다 — 옛 형식으로 먼저 온 대표에 라벨이 있어도 매크로가 받아 간다."""
    cur = await db.execute("SELECT site_raw, prompt_sha1, csha1, phash FROM ocr_img WHERE id=?", (iid,))
    old = await cur.fetchone()
    new = [o if o else ex[k] for o, k in zip(old, ("site_raw", "prompt_sha1", "csha1", "phash"))]
    if list(old) == new:
        return
    await db.execute("UPDATE ocr_img SET site_raw=?, prompt_sha1=?, csha1=?, phash=? WHERE id=?", (*new, iid))
    cur = await db.execute("SELECT status FROM ocr_cluster WHERE id=? AND tenant=?", (cid, tenant))
    r = await cur.fetchone()
    if r and r[0] in ("labeled", "bad"):
        await db.execute("UPDATE ocr_img SET lts=? WHERE id=?", (await _next_lts(db, tenant), iid))


async def _submit_reply(db, tenant, iid, cid, kind, dist):
    """label_match = 이 라벨이 ★이 이미지에★ 붙은 방식. "exact" = 이 이미지가 묶음 대표(주인님이 본 바로 그 한 장)
    · "near" = 대표가 아니다(비슷해서 묶였을 뿐 — 힌트로만, 반증 R1) · null = 라벨 없음.
    대표가 아닌 구성원이 정확히 다시 와도(dup:"exact") label_match 는 "near" 이고 near_dist 는 대표와의 거리."""
    cur = await db.execute("SELECT c.status, c.label, c.rep_img, c.rep_dhash, "
                           "(SELECT COALESCE(SUM(count),0) FROM ocr_img WHERE cluster_id=c.id) "
                           "FROM ocr_cluster c WHERE c.id=? AND c.tenant=?", (cid, tenant))
    st, lb, rep, rdh, n = await cur.fetchone()
    cur = await db.execute("SELECT count, dhash FROM ocr_img WHERE id=?", (iid,))
    cnt, idh = await cur.fetchone()
    is_rep = rep == iid
    if dist is None and not is_rep and rdh and idh:
        dist = hamming(idh, rdh)
    return {"ok": True, "id": iid, "cluster": cid, "dup": kind, "near_dist": dist, "status": st,
            "label": lb if st == "labeled" else None,
            "label_match": ("exact" if is_rep else "near") if st == "labeled" else None,
            "count": cnt, "cluster_count": n}


# ─── /bugs 씨앗 (2026-09-24 아이온2 요청 · 주인님 #125 «팜뷰 탭이 뜨자마자 바로 판별») ───────────────
# 매크로가 예전부터 /bugs 로 올리던 ★로컬 OCR 불일치 크롭★ 을 판별 큐에 넣는다(이미지 파일은 복사 — /bugs 정리와 무관).
#   ocrdiff_<항목>_L<로컬>_G<제미나이>.png  (info_collector 감사 불일치 — 값은 config._LEARN_TAG_MAP 로 적힌 파일명 글자)
#   ocrdiff_<항목>.png                       (config 섀도 불일치 — 값 없음)
#   oddfail_narrow.png / oddfail_wide.png   (오드 실패 증거 — 좁은 = odd_energy, 넓은 = odd_energy_wide: 자른 폭이 달라 묶음을 나눈다)
# 서버 파일 이름 = {pc}_{YYYYMMDD}_{HHMMSS}_{매크로 이름}. ★같은 그림(sha1)이 이미 큐에 있으면 건너뛴다(count 도 안 올린다)★ —
#   씨앗은 몇 번 돌려도 같다. dHash 는 서버가 직접 잰다(Pillow 없음 — 순수 파이썬 PNG 디코드, _png_gray).
# 파일명에서 되읽은 로컬·제미나이 값은 ★힌트★ 다(모르는 글자는 매크로가 'x' 로 적었다 → '?'). prompt 칸에 출처를 남긴다.
SEED_TAGS = ("ocrdiff_", "oddfail_")
SEED_MAX_FILES = 5000                             # 한 번에 훑는 파일 수 상한
SEED_MAX_PIXELS = 4_000_000                       # PNG 한 장 픽셀 상한(압축 폭탄 방어 — 크롭은 수만 픽셀)
_SEED_NAME = re.compile(r"^(?P<pc>.+?)_(?P<d>\d{8})_(?P<t>\d{6})_(?P<orig>.+)\.png$")
_SEED_DIFF = re.compile(r"^ocrdiff_(?P<name>.+?)_L(?P<l>[^_]*)_G(?P<g>[^_]*)$")
_UNTAG = {"c": ",", "s": "/", "p": "%", "d": ".", "l": "(", "r": ")", "u": "+", "m": "-", "k": "K", "g": "M", "x": "?"}
_SEED_DONE: set = set()                           # (프로세스) 큐 첫 조회 때 자동 씨앗을 이미 돌린 테넌트
_SEED_BUSY: set = set()
# ★첫 조회가 씨앗을 기다리는 상한(초)★ (2026-09-24 아이온2 반증: 크롭 2,000장 = 34.8초인데 팜뷰 ocr.py 시간제한 10초)
#   넘으면 씨앗은 뒤에서 계속 돌고 큐는 그때까지 들어간 것만 바로 준다. 적은 장수는 상한 안에 끝나 동작이 예전과 같다.
SEED_FIRST_WAIT_S = 3.0
_SEED_TASKS: dict = {}                            # 테넌트 → 뒤에서 도는 씨앗 과제(참조를 쥐어 GC 에 안 먹히게)


def _untag(v: str) -> str:
    """매크로 파일명 글자 → 값(힌트). 'empty' = 빈 값. 숫자는 그대로, 알려진 글자는 _LEARN_TAG_MAP 거꾸로, 나머지는 그대로."""
    if v == "empty":
        return ""
    return "".join(_UNTAG.get(c, c) for c in v)[:OCR_ANSWER_MAX]


def seed_parse(fname: str):
    """서버 /bugs 파일 이름 → dict(pc, ts, site, gem, loc) 또는 None(씨앗 대상 아님·캡차)."""
    m = _SEED_NAME.match(fname or "")
    if not m:
        return None
    orig = m.group("orig")
    i = min([orig.find(t) for t in SEED_TAGS if t in orig] or [-1])
    if i < 0:
        return None
    tag = orig[i:]
    gem = loc = ""
    if tag.startswith("oddfail_"):
        rest = tag[len("oddfail_"):]
        site = "odd_energy" if rest == "narrow" else "odd_energy_" + rest
    else:
        d = _SEED_DIFF.match(tag)
        if d:
            site, loc, gem = d.group("name"), _untag(d.group("l")), _untag(d.group("g"))
        else:
            site = tag[len("ocrdiff_"):]
    if is_captcha_site(site) or is_captcha_site(orig):
        return None
    site = site_safe(site)
    if not site:
        return None
    try:
        ts = datetime.strptime(m.group("d") + m.group("t"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        ts = None
    return {"pc": m.group("pc"), "ts": ts, "site": site, "gem": gem, "loc": loc}


def _png_gray(raw: bytes):
    """PNG → (w, h, 회색 바이트 목록) 또는 None. 8비트 비인터레이스 회색·RGB·회색+알파·RGBA·팔레트만(매크로 크롭은 회색)."""
    if not raw.startswith(_PNG):
        return None
    pos, idat, ihdr, plte = 8, [], None, None
    try:
        while pos + 8 <= len(raw):
            n = int.from_bytes(raw[pos:pos + 4], "big")
            typ = raw[pos + 4:pos + 8]
            data = raw[pos + 8:pos + 8 + n]
            pos += 12 + n
            if typ == b"IHDR":
                ihdr = data
            elif typ == b"PLTE":
                plte = data
            elif typ == b"IDAT":
                idat.append(data)
            elif typ == b"IEND":
                break
        if not ihdr or len(ihdr) < 13:
            return None
        w, h = int.from_bytes(ihdr[0:4], "big"), int.from_bytes(ihdr[4:8], "big")
        depth, ctype, inter = ihdr[8], ihdr[9], ihdr[12]
        bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
        if depth != 8 or inter != 0 or bpp is None or w <= 0 or h <= 0 or w * h > SEED_MAX_PIXELS:
            return None
        if ctype == 3 and not plte:
            return None
        stride = w * bpp
        dz = zlib.decompressobj()
        flat = dz.decompress(b"".join(idat), (stride + 1) * h + 1)
        if len(flat) < (stride + 1) * h:
            return None
        out = bytearray(w * h)
        prev = bytearray(stride)
        for y in range(h):
            f = flat[y * (stride + 1)]
            line = bytearray(flat[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
            if f == 1:
                for x in range(bpp, stride):
                    line[x] = (line[x] + line[x - bpp]) & 255
            elif f == 2:
                for x in range(stride):
                    line[x] = (line[x] + prev[x]) & 255
            elif f == 3:
                for x in range(stride):
                    line[x] = (line[x] + (((line[x - bpp] if x >= bpp else 0) + prev[x]) >> 1)) & 255
            elif f == 4:
                for x in range(stride):
                    a_ = line[x - bpp] if x >= bpp else 0
                    b_ = prev[x]
                    c_ = prev[x - bpp] if x >= bpp else 0
                    pa, pb, pc_ = abs(b_ - c_), abs(a_ - c_), abs(a_ + b_ - 2 * c_)
                    line[x] = (line[x] + (a_ if pa <= pb and pa <= pc_ else b_ if pb <= pc_ else c_)) & 255
            elif f != 0:
                return None
            row = y * w
            if ctype in (0, 4):
                out[row:row + w] = line[0::bpp]
            elif ctype == 3:
                for x in range(w):
                    k = line[x] * 3
                    r_, g_, b2 = plte[k:k + 3] if k + 3 <= len(plte) else (0, 0, 0)
                    out[row + x] = (r_ * 299 + g_ * 587 + b2 * 114) // 1000
            else:
                for x in range(w):
                    k = x * bpp
                    out[row + x] = (line[k] * 299 + line[k + 1] * 587 + line[k + 2] * 114) // 1000
            prev = line
        return w, h, out
    except (zlib.error, ValueError, IndexError, TypeError):
        return None


def png_dhash(raw: bytes):
    """PNG → 64비트 dHash(16자리 16진) 또는 None. 9x8 로 칸 평균(면적) 축소 → 각 줄에서 오른쪽이 더 밝으면 1."""
    g = _png_gray(raw)
    if g is None:
        return None
    w, h, px = g
    cells = []
    for cy in range(8):
        y0, y1 = cy * h // 8, max(cy * h // 8 + 1, (cy + 1) * h // 8)
        for cx in range(9):
            x0, x1 = cx * w // 9, max(cx * w // 9 + 1, (cx + 1) * w // 9)
            tot = cnt = 0
            for yy in range(y0, min(y1, h)):
                r0 = yy * w
                seg = px[r0 + x0:r0 + min(x1, w)]
                tot += sum(seg)
                cnt += len(seg)
            cells.append(tot / cnt if cnt else 0.0)
    v = 0
    for cy in range(8):
        for cx in range(8):
            v = (v << 1) | (1 if cells[cy * 9 + cx + 1] > cells[cy * 9 + cx] else 0)
    return "%016x" % v


async def seed_one(tenant: str, bdir: str, fname: str) -> str:
    """/bugs 파일 한 장 → 큐. 돌려주는 값: added · exists · skip:<까닭> · full."""
    info = seed_parse(fname)
    if info is None:
        return "skip:대상아님"
    path = os.path.join(bdir, fname)
    try:
        if os.path.getsize(path) > OCR_IMG_MAX:
            return "skip:큼"
        with open(path, "rb") as fh:
            raw = fh.read(OCR_IMG_MAX + 1)
    except OSError:
        return "skip:못읽음"
    if len(raw) > OCR_IMG_MAX or not raw.startswith(_PNG):
        return "skip:PNG아님"
    sha = hashlib.sha1(raw).hexdigest()
    await ensure_tables()
    async with aiosqlite.connect(_dbp()) as db:
        cur = await db.execute("SELECT 1 FROM ocr_img WHERE tenant=? AND site=? AND sha1=?", (tenant, info["site"], sha))
        if await cur.fetchone():
            return "exists"
    dh = png_dhash(raw)
    if dh is None:
        return "skip:디코드못함"
    pc = _m().clean_pc_id(info["pc"]).strip() or "PC-?"
    try:
        r = await submit_core(tenant, pc, info["site"], raw, dh, info["ts"], ("seed:/bugs " + fname)[:OCR_PROMPT_MAX],
                              info["gem"], info["loc"], err=_ocr_err)
    except OcrError as e:
        return "full" if e.code == 507 else "skip:%d" % e.code
    return "exists" if r.get("dup") == "exact" else "added"


async def seed_bugs_core(tenant: str) -> dict:
    """그 테넌트 /bugs 폴더의 ocrdiff_*·oddfail_* 전부 → 큐(이름순 = 오래된 것부터). 몇 번 돌려도 같다."""
    bdir = _m().tenant_bugs_dir(tenant)
    res = {"ok": True, "scanned": 0, "added": 0, "exists": 0, "skipped": {}, "full": False}
    if tenant in _SEED_BUSY:
        res.update(ok=False, busy=True)
        return res
    _SEED_BUSY.add(tenant)
    try:
        try:
            names = sorted(f for f in os.listdir(bdir) if f.endswith(".png") and any(t in f for t in SEED_TAGS))
        except OSError:
            names = []
        for f in names[:SEED_MAX_FILES]:
            res["scanned"] += 1
            r = await seed_one(tenant, bdir, f)
            if r == "full":
                res["full"] = True
                break
            if r in ("added", "exists"):
                res[r] += 1
            else:
                k = r.split(":", 1)[-1]
                res["skipped"][k] = res["skipped"].get(k, 0) + 1
        res["more"] = len(names) > SEED_MAX_FILES
    finally:
        _SEED_BUSY.discard(tenant)
    _SEED_DONE.add(tenant)
    return res


async def seed_upload_hook(tenant: str, bdir: str, fname: str) -> None:
    """main.upload_bug 가 부른다 — 새로 올라온 ocrdiff_·oddfail_ 한 장을 곧바로 큐에. 실패는 조용히(업로드를 막지 않는다)."""
    if not any(t in (fname or "") for t in SEED_TAGS):
        return
    try:
        await seed_one(tenant, bdir, fname)
    except Exception as e:
        print(f"[OCR] 씨앗(업로드) 실패(무시) {fname}: {e.__class__.__name__}: {e}", flush=True)


async def _auto_seed_run(tenant: str) -> None:
    try:
        await seed_bugs_core(tenant)
    except Exception as e:
        _SEED_DONE.add(tenant)
        print(f"[OCR] 자동 씨앗 실패(무시) {tenant}: {e.__class__.__name__}: {e}", flush=True)


async def _auto_seed(tenant: str) -> None:
    """큐를 처음 볼 때(프로세스·테넌트마다 한 번) 씨앗을 돌린다 — 탭이 뜨자마자 판별할 거리가 있게. 실패는 조용히.
    ★SEED_FIRST_WAIT_S 까지만 기다린다★ — 넘으면 과제는 뒤에서 계속 돌고 이 조회는 바로 돌아간다(다음 조회는 기다리지 않는다)."""
    if tenant in _SEED_DONE:
        return
    task = _SEED_TASKS.get(tenant)
    if task is not None and not task.done():
        try:
            if task.get_loop() is asyncio.get_running_loop():
                return                               # 이미 뒤에서 돈다 — 겹쳐 띄우지 않는다
        except RuntimeError:
            pass
    task = _SEED_TASKS[tenant] = asyncio.ensure_future(_auto_seed_run(tenant))
    task.add_done_callback(lambda t, k=tenant: _SEED_TASKS.pop(k, None) if _SEED_TASKS.get(k) is t else None)
    await asyncio.wait({task}, timeout=SEED_FIRST_WAIT_S)


# ─── 매크로: 라벨 증분 ────────────────────────────────────────────────────────
def is_ts_mode(since, mode) -> bool:
    """★ts 방식(매크로 lc/ocr_label_net.poll_now)★ 알아보기 — mode=ts, 또는 since 가 실수 글자("0.0"·"1790164002.41"·"1e-05").
    매크로 since 는 파이썬 float 이라 requests 가 늘 '.' 이나 'e' 를 붙여 보낸다. 커서 방식(정수)은 그대로."""
    if isinstance(mode, str) and mode.strip().lower() == "ts":
        return True
    s = str(since).strip().lower()
    return "." in s or "e" in s or s.lstrip("+-") in ("inf", "nan", "infinity")


def _ts_row(lts, site, psha, csha, dh, ph, st, lb) -> dict:
    r = {"site": site, "prompt_sha1": psha, "sha1": csha, "ts": lts, "dhash": dh or "", "phash": ph or "", "status": st}
    if st == "labeled":
        r.update(label=lb or "", deleted=False)
    elif st == "bad":
        r.update(label=OCR_BAD_LABEL, deleted=False)
    else:                                   # 되돌리기로 대기에 돌아감 → 매크로는 그 열쇠를 지운다
        r.update(label="", deleted=True)
    return r


_TS_SQL = ("SELECT i.lts, COALESCE(NULLIF(i.site_raw,''), i.site), i.prompt_sha1, i.csha1, i.dhash, i.phash, c.status, c.label "
           "FROM ocr_img i JOIN ocr_cluster c ON c.id=i.cluster_id "
           "WHERE i.tenant=? AND c.rep_img=i.id AND COALESCE(i.prompt_sha1,'')<>'' AND COALESCE(i.csha1,'')<>'' AND ")


async def labels_ts_core(tenant: str, since_f: float) -> dict:
    """{labels:[{site, prompt_sha1, sha1, label, ts, deleted, dhash, phash, status}], since, more, mode:"ts", epoch}.
    ★묶음 대표 줄만★(반증 R1 — 사람이 본 그 한 장만 정답) · 매크로 열쇠(prompt_sha1·sha1)가 있는 줄만.
    ts = 라벨이 바뀐 서버 시각(_next_lts, 테넌트마다 늘 증가) — 매크로는 받은 최대 ts 를 since 로 다시 보낸다.
    한 쪽(5,000줄)을 넘으면 마지막 ts 가 쪽 경계에 걸리지 않게 자른다(같은 ts 가 다음 쪽으로 새지 않게)."""
    await ensure_tables()
    async with aiosqlite.connect(_dbp()) as db:
        ep = await _epoch(db)
        cur = await db.execute(_TS_SQL + "i.lts>? ORDER BY i.lts, i.id LIMIT ?", (tenant, since_f, OCR_LABELS_PAGE + 1))
        rows = await cur.fetchall()
        more = len(rows) > OCR_LABELS_PAGE
        if more:
            last = rows[OCR_LABELS_PAGE - 1][0]
            keep = rows[:OCR_LABELS_PAGE]
            if rows[OCR_LABELS_PAGE][0] == last:
                keep = [r for r in keep if r[0] != last]
                if not keep:                # 한 ts 가 한 쪽보다 크다 → 그 ts 는 통째로
                    cur = await db.execute(_TS_SQL + "i.lts=? ORDER BY i.id", (tenant, last))
                    keep = await cur.fetchall()
            rows = keep
    out = []
    for r in rows:
        row = _ts_row(*r)
        out.append(row)
        for alt in psha_aliases(row["prompt_sha1"]):   # 옛·새 판 열쇠 — 행을 하나 더(같은 ts·라벨·deleted)
            out.append(dict(row, prompt_sha1=alt, alias_of=row["prompt_sha1"]))
    return {"labels": out, "since": rows[-1][0] if rows else since_f, "more": more, "mode": "ts", "epoch": ep}


@router.get("/ocr/labels")
async def ocr_labels(request: Request, since: str = "0", epoch: str = "", mode: str = ""):
    """{labels:{site:{sha1:label}}, near:{site:{dhash:label}}, bad:{site:{sha1:dhash}},
        cleared:{site:[sha1]}, cleared_near:{site:[dhash]}, cursor, more, reset, epoch}.
    since=0 이면 처음부터 전부(OCR_LABELS_PAGE 씩). 매크로는 받은 cursor·epoch 를 저장해 다음에 둘 다 보낸다.
    reset=true 면 매크로는 가진 라벨을 ★통째로 버리고★ 이 응답으로 다시 채운다.
    ★labels 에는 묶음 대표의 sha1 만★(주인님이 본 바로 그 이미지) — 다른 구성원은 near 힌트로만(반증 R1)."""
    m = _m()
    tenant = m._require_api_key(request)
    if is_ts_mode(since, mode):             # ★두 형식(2026-09-24)★ 매크로 lc/ocr_label 방식 — 아래 커서 방식은 그대로
        try:
            since_f = float(str(since).strip() or "0")
            if not (0.0 <= since_f < 1e11):     # nan 은 비교가 전부 거짓 → 여기서 걸린다
                raise ValueError
        except ValueError:
            _bad(400, "since(ts 방식)는 0 이상의 유한한 수여야 합니다")
        return JSONResponse(await labels_ts_core(tenant, since_f))
    try:
        since_i = int(str(since).strip() or "0")
        if since_i < 0 or since_i > _I64_MAX:
            raise ValueError
    except ValueError:
        _bad(400, "since 는 0 이상의 정수여야 합니다")
    ep_in = epoch.strip() if isinstance(epoch, str) else ""
    await ensure_tables()
    async with aiosqlite.connect(_dbp()) as db:
        ep = await _epoch(db)
        cur = await db.execute("SELECT v FROM ocr_seq WHERE tenant=?", (tenant,))
        r = await cur.fetchone()
        top = r[0] if r else 0
        # ★처음부터 다시 주는 두 경우★ ① 매크로 커서가 서버보다 앞(서버 DB 가 새로 생김)
        #   ② since>0 인데 epoch 가 없거나 다르다 — 새 DB 의 커서가 옛 커서를 이미 넘었으면 ①로는 못 알아챈다(반증 R2)
        reset = since_i > top or (since_i > 0 and ep_in != ep)
        if reset:
            since_i = 0
        cur = await db.execute(
            "SELECT i.seq, i.site, i.sha1, i.dhash, c.status, c.label, (c.rep_img = i.id) "
            "FROM ocr_img i JOIN ocr_cluster c ON c.id=i.cluster_id "
            "WHERE i.tenant=? AND i.seq>? ORDER BY i.seq LIMIT ?", (tenant, since_i, OCR_LABELS_PAGE + 1))
        rows = await cur.fetchall()
    more = len(rows) > OCR_LABELS_PAGE
    rows = rows[:OCR_LABELS_PAGE]
    labels, near, bad, cleared, cleared_near = {}, {}, {}, {}, {}
    for seq, site, sha, dh, st, lb, is_rep in rows:
        if st == "labeled":
            if is_rep:                          # 대표(=주인님이 본 것)와 그 정확한 재제출만 정답
                labels.setdefault(site, {})[sha] = lb
            near.setdefault(site, {})[dh] = lb  # 구성원 전부(대표 포함)는 dhash 힌트
        elif st == "bad":
            bad.setdefault(site, {})[sha] = dh
        else:                                   # 되돌리기로 대기에 돌아간 것 — 매크로는 sha1·dhash 둘 다 지운다(반증 R10)
            cleared.setdefault(site, []).append(sha)
            cleared_near.setdefault(site, []).append(dh)
    cursor = rows[-1][0] if more else max(top, since_i)
    return JSONResponse({"labels": labels, "near": near, "bad": bad, "cleared": cleared, "cleared_near": cleared_near,
                         "cursor": cursor, "more": more, "reset": reset, "epoch": ep})


# ─── 대시보드 · 팜뷰 공용 핵심 (★SQL 은 여기 한 벌뿐★) ─────────────────────────
# 2026-09-23 (밤) 장부 #125 — 팜뷰 «OCR» 탭이 같은 저장소를 쓴다(FV_API.md «2026-09-23 (밤) 추가 › D»).
#   핵심 함수는 tenant 를 받고, 실패는 OcrError(code, msg) 로 던진다. 겉옷은 둘:
#     _web_call — 세션 → 테넌트, OcrError → HTTPException({"detail"})      (대시보드 /ocr/*)
#     _fv_call  — main._fv_guard → FV_TENANT, OcrError → main._fv_err({ok:false,error,err,code})  (/api/fv/ocr/*)
#   그래서 어느 쪽에서 단 라벨이든 같은 줄·같은 되돌리기 기록에 쌓인다.
class OcrError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


async def _web_call(request: Request, fn):
    tenant = _m()._require_session(request)
    try:
        return await fn(tenant)
    except OcrError as e:
        raise HTTPException(status_code=e.code, detail=e.msg)


async def _fv_call(request: Request, fn):
    m = _m()
    g = m._fv_guard(request)              # ★토큰부터★ — 본문·쿼리 오류보다 401/404 가 먼저
    if g is not None:
        return g
    try:
        res = await fn(m.FV_TENANT)
    except OcrError as e:
        return m._fv_err(e.code, e.msg)
    if isinstance(res, Response):          # 이미지 파일
        return res
    return m._fv_json(request, res)


def _limit(v, default: int, hi: int, strict: bool) -> int:
    """strict(팜뷰) = 정수가 아니면 400 · 대시보드 화면 = 기본값으로. 범위는 둘 다 1~hi 로 자른다."""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        if strict:
            raise OcrError(400, "limit 는 정수여야 합니다")
        n = default
    return max(1, min(hi, n))


def _ocr_err(code: int, msg: str):
    raise OcrError(code, msg)


async def _body(request: Request) -> dict:
    """사람 쪽 POST 본문 — OCR_SMALL_BODY_CAP 까지만 읽는다(청크 전송도 읽으면서 센다, 반증 R9 같은 부류)."""
    return await read_json_capped(request, OCR_SMALL_BODY_CAP, _ocr_err)


def _cid(b: dict) -> int:
    """본문 id — 부호 있는 64비트 밖이면 400(sqlite 가 OverflowError 로 500 을 냈다, 반증 R4)."""
    v = b.get("id")
    try:
        if v is None or isinstance(v, bool):
            raise ValueError
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise OcrError(400, "id(정수)가 필요합니다")
    if not _i64(n):
        raise OcrError(400, "id(정수)가 범위 밖입니다")
    return n


async def _cluster_items(db, tenant, where, args, order, limit):
    cur = await db.execute(
        "SELECT c.id, c.rep_img, c.site, c.status, c.label, c.created, c.labeled_at, "
        "       (SELECT COALESCE(SUM(count),0) FROM ocr_img WHERE cluster_id=c.id), "
        "       (SELECT COUNT(*) FROM ocr_img WHERE cluster_id=c.id), "
        "       r.prompt, r.gemini, r.local, r.pc_id, r.on_disk "
        "FROM ocr_cluster c LEFT JOIN ocr_img r ON r.id=c.rep_img "
        "WHERE c.tenant=? AND " + where + " ORDER BY " + order + " LIMIT ?", (tenant, *args, limit))
    items = []
    for (cid, rep, site, st, lb, cr, la, hits, mem, pr, gm, lc, pc, od) in await cur.fetchall():
        c2 = await db.execute("SELECT id FROM ocr_img WHERE cluster_id=? AND on_disk=1 AND id<>? ORDER BY id DESC LIMIT 6",
                              (cid, rep or 0))
        items.append({"id": cid, "img": rep if od else None, "site": site, "status": st, "label": lb,
                      "created": cr, "at": la, "count": hits, "members": mem, "prompt": pr or "",
                      "gemini": gm or "", "local": lc or "", "pc": pc or "",
                      "thumbs": [r[0] for r in await c2.fetchall()]})
    return items


async def queue_core(tenant: str, lim: int) -> dict:
    await ensure_tables()
    async with aiosqlite.connect(_dbp()) as db:
        items = await _cluster_items(db, tenant, "c.status='pending'", (), "c.qorder, c.id", lim)
        cur = await db.execute("SELECT COUNT(*) FROM ocr_cluster WHERE tenant=? AND status='pending'", (tenant,))
        pending = (await cur.fetchone())[0]
        suggest = {}
        for site in sorted({it["site"] for it in items}):
            cur = await db.execute("SELECT label, COUNT(*) n FROM ocr_cluster WHERE tenant=? AND site=? AND status='labeled' "
                                   "GROUP BY label ORDER BY n DESC, label LIMIT ?", (tenant, site, OCR_SUGGEST_MAX))
            suggest[site] = [r[0] for r in await cur.fetchall()]
    return {"pending": pending, "items": items, "suggest": suggest, "now": time.time()}


async def img_core(tenant: str, img_id) -> FileResponse:
    try:
        iid = int(str(img_id))
    except ValueError:
        raise OcrError(404, "이미지 없음")
    if not _i64(iid):                       # 2^70 같은 값 → sqlite OverflowError(500) 대신 404(반증 R4)
        raise OcrError(404, "이미지 없음")
    await ensure_tables()
    async with aiosqlite.connect(_dbp()) as db:
        cur = await db.execute("SELECT relpath, mime, on_disk FROM ocr_img WHERE id=? AND tenant=?", (iid, tenant))
        row = await cur.fetchone()
    if not row or not row[2]:
        raise OcrError(404, "이미지 없음")
    try:
        path = _abs(row[0])
    except ValueError:
        raise OcrError(404, "이미지 없음")
    if not os.path.isfile(path):
        raise OcrError(404, "이미지 없음")
    return FileResponse(path, media_type=row[1] or "application/octet-stream",
                        headers={"Cache-Control": "private, max-age=86400"})


async def label_core(tenant: str, cid: int, new_st: str, new_lb) -> dict:
    """저장(라벨 된 것에 다시 보내면 고치기) · 나쁨. 되돌리기 기록(ocr_hist)에 한 줄."""
    if new_st == "labeled" and not new_lb:
        raise OcrError(400, "라벨이 비었습니다")
    await ensure_tables()
    async with _lock():
        async with aiosqlite.connect(_dbp()) as db:
            cur = await db.execute("SELECT status, label FROM ocr_cluster WHERE id=? AND tenant=?", (cid, tenant))
            row = await cur.fetchone()
            if not row:
                raise OcrError(404, "없는 묶음입니다(지워졌거나 다른 테넌트)")
            now = time.time()
            await db.execute("INSERT INTO ocr_hist(tenant, cluster_id, prev_status, prev_label, new_status, new_label, at) "
                             "VALUES(?,?,?,?,?,?,?)", (tenant, cid, row[0], row[1], new_st, new_lb, now))
            await db.execute("UPDATE ocr_cluster SET status=?, label=?, labeled_at=? WHERE id=?", (new_st, new_lb, now, cid))
            await _bump_members(db, tenant, cid)
            await db.commit()
            cur = await db.execute("SELECT COUNT(*) FROM ocr_img WHERE cluster_id=?", (cid,))
            mem = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM ocr_cluster WHERE tenant=? AND status='pending'", (tenant,))
            pending = (await cur.fetchone())[0]
    return {"ok": True, "id": cid, "status": new_st, "label": new_lb, "prev_status": row[0], "prev_label": row[1],
            "members": mem, "pending": pending}


async def skip_core(tenant: str, cid: int) -> dict:
    """나중에 — 대기열 맨 뒤로. 대기 아닌 묶음이면 409."""
    await ensure_tables()
    async with _lock():
        async with aiosqlite.connect(_dbp()) as db:
            cur = await db.execute("SELECT status FROM ocr_cluster WHERE id=? AND tenant=?", (cid, tenant))
            row = await cur.fetchone()
            if not row:
                raise OcrError(404, "없는 묶음입니다")
            if row[0] != "pending":
                raise OcrError(409, "대기 중인 묶음이 아닙니다")
            cur = await db.execute("SELECT COALESCE(MAX(qorder), 0) FROM ocr_cluster WHERE tenant=? AND status='pending'",
                                   (tenant,))
            q = float((await cur.fetchone())[0]) + 1.0
            await db.execute("UPDATE ocr_cluster SET qorder=? WHERE id=?", (q, cid))
            await db.commit()
    return {"ok": True, "id": cid}


async def undo_core(tenant: str) -> dict:
    """이 테넌트의 마지막 저장/나쁨/고치기 하나(웹·팜뷰 어느 쪽에서 했든)를 되돌린다.
    대기로 돌아가면 대기열 ★맨 앞★ 으로(바로 다시 친다)."""
    await ensure_tables()
    async with _lock():
        async with aiosqlite.connect(_dbp()) as db:
            cur = await db.execute(
                "SELECT h.id, h.cluster_id, h.prev_status, h.prev_label FROM ocr_hist h JOIN ocr_cluster c ON c.id=h.cluster_id "
                "WHERE h.tenant=? AND h.undone=0 ORDER BY h.id DESC LIMIT 1", (tenant,))
            row = await cur.fetchone()
            if not row:
                raise OcrError(404, "되돌릴 것이 없습니다")
            hid, cid, pst, plb = row
            pst = pst or "pending"
            if pst == "auto":
                # ★#218 반증★ 자동 닫음 묶음에 라벨 → 되돌리기는 ★대기(맨 앞)★ 로 — auto 로 돌려놓으면 ocr_hist 가 생긴 auto 라
                #   대기열·기록에 안 나오고 자동 판정도 다시 안 봐 ★영영 숨는다★. 사람이 되돌렸으면 사람이 다시 본다.
                pst, plb = "pending", None
            now = time.time()
            if pst == "pending":
                cur = await db.execute("SELECT COALESCE(MIN(qorder), ?) FROM ocr_cluster WHERE tenant=? AND status='pending'",
                                       (now, tenant))
                q = float((await cur.fetchone())[0]) - 1.0
                await db.execute("UPDATE ocr_cluster SET status='pending', label=NULL, labeled_at=NULL, qorder=? WHERE id=?",
                                 (q, cid))
            else:
                await db.execute("UPDATE ocr_cluster SET status=?, label=?, labeled_at=? WHERE id=?", (pst, plb, now, cid))
            await db.execute("UPDATE ocr_hist SET undone=1 WHERE id=?", (hid,))
            await _bump_members(db, tenant, cid)
            await db.commit()
    return {"ok": True, "id": cid, "status": pst, "label": plb if pst == "labeled" else None}


async def history_core(tenant: str, lim: int) -> dict:
    await ensure_tables()
    async with aiosqlite.connect(_dbp()) as db:
        items = await _cluster_items(db, tenant, "c.status IN ('labeled','bad')", (), "c.labeled_at DESC, c.id DESC", lim)
    return {"items": items}


async def stats_core(tenant: str) -> dict:
    """사이트별 대기·라벨·나쁨(묶음 수) + 불일치율(라벨 된 ★이미지★ 한 장씩, 답이 빈 것은 분모에서 뺀다)."""
    await ensure_tables()
    sites: dict = {}

    def S(site):
        return sites.setdefault(site, {"pending": 0, "labeled": 0, "bad": 0, "auto": 0, "images": 0, "hits": 0,
                                       "gemini_compared": 0, "gemini_disagree": 0, "gemini_disagree_rate": None,
                                       "local_compared": 0, "local_disagree": 0, "local_disagree_rate": None})
    async with aiosqlite.connect(_dbp()) as db:
        cur = await db.execute("SELECT site, status, COUNT(*) FROM ocr_cluster WHERE tenant=? GROUP BY site, status", (tenant,))
        for site, st, n in await cur.fetchall():
            if st in ("pending", "labeled", "bad", "auto"):      # auto = #218 자동 닫음(사람 라벨 아님 — 불일치율에도 안 넣는다)
                S(site)[st] = n
        cur = await db.execute("SELECT site, COUNT(*), COALESCE(SUM(count),0) FROM ocr_img WHERE tenant=? GROUP BY site", (tenant,))
        for site, n, h in await cur.fetchall():
            S(site)["images"], S(site)["hits"] = n, h
        cur = await db.execute("SELECT i.site, i.gemini, i.local, c.label FROM ocr_img i JOIN ocr_cluster c ON c.id=i.cluster_id "
                               "WHERE i.tenant=? AND c.status='labeled'", (tenant,))
        for site, gm, lc, lb in await cur.fetchall():
            d, want = S(site), norm_answer(lb)
            for k, v in (("gemini", gm), ("local", lc)):
                if norm_answer(v):
                    d[k + "_compared"] += 1
                    if norm_answer(v) != want:
                        d[k + "_disagree"] += 1
        used = await _disk_used(db)
    for d in sites.values():
        for k in ("gemini", "local"):
            if d[k + "_compared"]:
                d[k + "_disagree_rate"] = round(d[k + "_disagree"] / d[k + "_compared"], 4)
    return {"sites": sites, "disk_bytes": used, "disk_cap": OCR_DISK_CAP, "disk_hard_cap": OCR_DISK_HARD_CAP}


# ─── 대시보드 화면 · JSON (세션) ─────────────────────────────────────────────
@router.get("/ocr/label", response_class=HTMLResponse)
async def ocr_label_page(request: Request):
    if not _m().check_session(request):
        return RedirectResponse("/login")
    return HTMLResponse(PAGE_HTML, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                                            "Pragma": "no-cache", "Expires": "0"})


async def _queue_seeded(t, limit):
    await _auto_seed(t)
    return await queue_core(t, limit)


@router.get("/ocr/queue")
async def ocr_queue(request: Request, limit: str = "30"):
    return await _web_call(request, lambda t: _queue_seeded(t, _limit(limit, 30, 100, False)))


@router.post("/ocr/seed_bugs")
async def ocr_seed_bugs(request: Request):
    """/bugs 의 ocrdiff_*·oddfail_* 를 큐로(몇 번 눌러도 같다)."""
    return await _web_call(request, seed_bugs_core)


@router.get("/ocr/img/{img_id}")
async def ocr_img(img_id: str, request: Request):
    return await _web_call(request, lambda t: img_core(t, img_id))


async def _web_label(t, request):
    b = await _body(request)
    cid = _cid(b)
    if b.get("bad") is True:                    # 화면은 {id, bad:true} 로 나쁨을 보낸다
        return await label_core(t, cid, "bad", None)
    return await label_core(t, cid, "labeled", _norm_label(b.get("label")))


@router.post("/ocr/label")
async def ocr_label_set(request: Request):
    """{id, label} = 저장·고치기 · {id, bad:true} = 잘못된 이미지."""
    return await _web_call(request, lambda t: _web_label(t, request))


async def _skip_from(t, request):
    return await skip_core(t, _cid(await _body(request)))


@router.post("/ocr/skip")
async def ocr_skip(request: Request):
    return await _web_call(request, lambda t: _skip_from(t, request))


@router.post("/ocr/undo")
async def ocr_undo(request: Request):
    return await _web_call(request, undo_core)


@router.get("/ocr/history")
async def ocr_history(request: Request, limit: str = "30"):
    return await _web_call(request, lambda t: history_core(t, _limit(limit, 30, 200, False)))


@router.get("/ocr/stats")
async def ocr_stats(request: Request):
    return await _web_call(request, stats_core)


# ─── 팜뷰 (/api/fv/ocr/* — X-FV-Token, 테넌트 = FV_TENANT, 에러 = main._fv_err) ─────────
@router.get("/api/fv/ocr/queue")
async def fv_ocr_queue(request: Request, limit: str = "30"):
    return await _fv_call(request, lambda t: _queue_seeded(t, _limit(limit, 30, 100, True)))


@router.post("/api/fv/ocr/seed_bugs")
async def fv_ocr_seed_bugs(request: Request):
    """{} → {ok, scanned, added, exists, skipped:{까닭:n}, full, more}"""
    return await _fv_call(request, seed_bugs_core)


@router.get("/api/fv/ocr/img/{img_id}")
async def fv_ocr_img(img_id: str, request: Request):
    return await _fv_call(request, lambda t: img_core(t, img_id))


async def _fv_label(t, request):
    b = await _body(request)
    return await label_core(t, _cid(b), "labeled", _norm_label(b.get("text")))


@router.post("/api/fv/ocr/label")
async def fv_ocr_label(request: Request):
    """{id, text} — 이미 라벨 된 묶음이면 고치기."""
    return await _fv_call(request, lambda t: _fv_label(t, request))


async def _fv_bad(t, request):
    return await label_core(t, _cid(await _body(request)), "bad", None)


@router.post("/api/fv/ocr/bad")
async def fv_ocr_bad(request: Request):
    return await _fv_call(request, lambda t: _fv_bad(t, request))


@router.post("/api/fv/ocr/skip")
async def fv_ocr_skip(request: Request):
    return await _fv_call(request, lambda t: _skip_from(t, request))


@router.post("/api/fv/ocr/undo")
async def fv_ocr_undo(request: Request):
    return await _fv_call(request, undo_core)


@router.get("/api/fv/ocr/history")
async def fv_ocr_history(request: Request, limit: str = "30"):
    return await _fv_call(request, lambda t: history_core(t, _limit(limit, 30, 200, True)))


@router.get("/api/fv/ocr/stats")
async def fv_ocr_stats(request: Request):
    return await _fv_call(request, stats_core)


# ─── 화면 (★Tailwind 를 안 쓴다★ — 이 화면 전용 작은 CSS. tw_build 후보 낱말·tw.css 와 무관해
#     test_tailwind 를 흔들지 않고, 폰에서도 36KB 를 덜 받는다. 색은 대시보드 어두운 판 #0b1120·#818cf8)
# ★데이터는 전부 textContent·value·src 로만 넣는다 — innerHTML 에 사용자 값 0곳★
# ★순수 함수는 /*PURE-BEGIN*/ ~ /*PURE-END*/ 사이 — tests/test_ocr_label.py 가 node 로 잘라 돌린다★
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OCR 라벨</title>
<style>
:root{--bg:#0b1120;--card:#111827;--line:#1f2937;--tx:#e5e7eb;--mut:#9ca3af;--acc:#818cf8;--ok:#22c55e;--bad:#ef4444;--warn:#f59e0b}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--tx);font-family:"Pretendard","Malgun Gothic","Apple SD Gothic Neo",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;padding:10px 16px;background:var(--bg);border-bottom:1px solid var(--line)}
header a{color:var(--acc);text-decoration:none;font-size:14px}
header b{font-size:16px}
.grow{flex:1}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;background:#1e293b;font-size:13px;white-space:nowrap}
.pill.acc{background:#312e81;color:#c7d2fe}
.pill.warn{background:#78350f;color:#fde68a}
.mut{color:var(--mut);font-size:13px}
label.opt{font-size:13px;color:var(--mut);display:flex;gap:4px;align-items:center}
main{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:16px;padding:16px;max-width:1400px;margin:0 auto}
@media (max-width:900px){main{grid-template-columns:minmax(0,1fr)}}
section,aside{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;min-width:0}
.meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px}
.prompt{color:var(--mut);font-size:13px;margin-bottom:10px;white-space:pre-wrap;word-break:break-word;max-height:4.8em;overflow:auto}
.imgbox{display:flex;justify-content:center;align-items:center;min-height:120px;background:#020617;border-radius:8px;padding:10px;overflow:hidden}
.imgbox img{image-rendering:pixelated;max-width:100%;height:auto}
.thumbs{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.thumbs img{height:32px;border:1px solid var(--line);border-radius:4px;image-rendering:pixelated;background:#020617}
.hint{margin:10px 0 8px;color:var(--mut);font-size:13px;word-break:break-all}
.hint span{color:#cbd5e1}
#ans{width:100%;font-size:22px;padding:12px 14px;border-radius:10px;border:2px solid #334155;background:#020617;color:#fff;outline:none}
#ans:focus{border-color:var(--acc)}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
button{font:inherit;font-size:14px;padding:9px 14px;border-radius:9px;border:1px solid #334155;background:#1e293b;color:var(--tx);cursor:pointer}
button:hover{border-color:var(--acc)}
button.pri{background:#4338ca;border-color:#4338ca}
button.danger{background:#7f1d1d;border-color:#991b1b}
.keys{margin-top:10px}
#empty{padding:40px 10px;text-align:center;color:var(--mut)}
aside h3{margin:0 0 8px;font-size:14px}
#hist{list-style:none;margin:0;padding:0;max-height:60vh;overflow:auto}
#hist li{display:flex;gap:8px;align-items:center;padding:6px;border-radius:8px;cursor:pointer;border:1px solid transparent}
#hist li:hover{border-color:#334155;background:#0f172a}
#hist img{height:26px;max-width:110px;object-fit:contain;image-rendering:pixelated;background:#020617;border-radius:3px}
#hist .lb{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#hist .lb.bad{color:#fca5a5}
#stats{margin-top:12px;overflow:auto}
#stats table{border-collapse:collapse;font-size:12px;width:100%}
#stats th,#stats td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:right;white-space:nowrap}
#stats th:first-child,#stats td:first-child{text-align:left}
#toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#1e293b;border:1px solid #334155;padding:8px 14px;border-radius:10px;font-size:14px;opacity:0;transition:opacity .2s;pointer-events:none;max-width:90vw}
#toast.on{opacity:1}
@media (max-width:600px){main{padding:10px}#ans{font-size:20px}header{padding:8px 12px}}
</style></head>
<body>
<header>
  <a href="/">← 대시보드</a>
  <b>OCR 라벨</b>
  <span id="qcount" class="pill acc">대기 -</span>
  <span id="net" class="mut"></span>
  <span class="grow"></span>
  <label class="opt"><input type="checkbox" id="opt-sound"> 새 이미지 소리</label>
  <label class="opt"><input type="checkbox" id="opt-badge"> 탭 제목 배지</label>
  <button type="button" id="btn-stats">통계</button>
</header>
<main>
  <section>
    <div id="empty" hidden>대기 중인 이미지가 없습니다 — 새 이미지가 오면 새로고침 없이 바로 뜹니다</div>
    <div id="item" hidden>
      <div class="meta"><span id="site" class="pill"></span><span id="mult" class="pill acc"></span><span id="editing" class="pill warn" hidden>지난 라벨 고치는 중 (Esc 취소)</span><span id="pc" class="mut"></span></div>
      <div id="prompt" class="prompt"></div>
      <div class="imgbox" id="imgbox"><img id="img" alt="OCR 이미지"></div>
      <div id="thumbs" class="thumbs"></div>
      <div class="hint">Gemini: <span id="gem"></span> · 로컬: <span id="loc"></span></div>
      <input id="ans" list="dl" enterkeyhint="done" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="정답 입력 후 Enter">
      <datalist id="dl"></datalist>
      <div class="btns">
        <button type="button" id="btn-save" class="pri">저장 (Enter)</button>
        <button type="button" id="btn-bad" class="danger">잘못된 이미지</button>
        <button type="button" id="btn-skip">나중에 (Esc)</button>
        <button type="button" id="btn-undo">되돌리기</button>
      </div>
      <div class="keys mut">Enter 저장·다음 · Esc 나중에(맨 뒤로) · 빈 칸에서 Tab = Gemini 답 채우기 · 빈 칸에서 Ctrl+Z = 마지막 되돌리기</div>
    </div>
  </section>
  <aside>
    <h3>최근 라벨 — 눌러서 고치기</h3>
    <ol id="hist"></ol>
    <div id="stats" hidden></div>
  </aside>
</main>
<div id="toast"></div>
<script>
/*PURE-BEGIN*/
function decideKey(ev, value, hint) {
  const v = String(value == null ? '' : value);
  const k = ev.key;
  if (k === 'Enter') {
    if (ev.isComposing) return 'save_after_compose';
    return v.trim() ? 'save' : 'none';
  }
  if (ev.isComposing) return 'none';
  if (k === 'Escape' || k === 'Esc') return 'skip';
  if (k === 'Tab' && !ev.shiftKey && !v && hint) return 'fill';
  if ((ev.ctrlKey || ev.metaKey) && (k === 'z' || k === 'Z') && !v) return 'undo';
  return 'none';
}
function mergeQueue(curId, incoming, done, now) {
  const out = [];
  for (const it of incoming || []) {
    const t = done[it.id];
    if (t && now - t < 15000) continue;
    out.push(it);
  }
  if (curId != null) {
    const i = out.findIndex(function (x) { return x.id === curId; });
    if (i > 0) out.unshift(out.splice(i, 1)[0]);
  }
  return out;
}
function queueGrew(prev, next) {
  return prev >= 0 && next > prev;
}
function pctText(r) {
  return (r == null) ? '-' : (Math.round(r * 1000) / 10) + '%';
}
/*PURE-END*/

const $ = function (id) { return document.getElementById(id); };
const S = {items: [], edit: null, done: {}, pending: -1, composeSave: false, sound: false, badge: false, ac: null, shown: null};

function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

let toastTimer = null;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { t.classList.remove('on'); }, 1800);
}

async function api(method, url, body) {
  try {
    const opt = {method: method, credentials: 'same-origin', cache: 'no-store', headers: {}};
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const r = await fetch(url, opt);
    if (r.status === 401) { location.href = '/login'; return {ok: false, status: 401, j: null, err: '로그인 필요'}; }
    let j = null;
    try { j = await r.json(); } catch (e) { j = null; }
    $('net').textContent = '';
    return {ok: r.ok, status: r.status, j: j, err: (j && j.detail) ? String(j.detail) : ('HTTP ' + r.status)};
  } catch (e) {
    $('net').textContent = '연결 끊김 — 다시 시도 중';
    return {ok: false, status: 0, j: null, err: '연결 끊김'};
  }
}

function current() { return S.edit || S.items[0] || null; }

function fitImage() {
  const img = $('img');
  const nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) return;
  const cw = Math.max(100, $('imgbox').clientWidth - 20);
  const s = Math.max(1, Math.min(8, cw / nw, (window.innerHeight * 0.45) / nh));
  img.style.width = Math.round(nw * s) + 'px';
}

function setDatalist(site) {
  const dl = $('dl');
  while (dl.firstChild) dl.removeChild(dl.firstChild);
  const arr = (S.suggest && S.suggest[site]) || [];
  for (const lb of arr) {
    const o = document.createElement('option');
    o.value = String(lb);
    dl.appendChild(o);
  }
}

function render() {
  const it = current();
  $('empty').hidden = !!it;
  $('item').hidden = !it;
  if (!it) { S.shown = null; return; }
  $('mult').textContent = '×' + it.count + (it.members > 1 ? ' (' + it.members + '장 묶음)' : '');
  const key = (S.edit ? 'e' : 'q') + it.id;
  if (S.shown === key) return;
  S.shown = key;
  $('site').textContent = it.site;
  $('pc').textContent = it.pc ? ('보낸 PC ' + it.pc) : '';
  $('prompt').textContent = it.prompt || '';
  $('gem').textContent = it.gemini || '-';
  $('loc').textContent = it.local || '-';
  $('editing').hidden = !S.edit;
  const img = $('img');
  img.style.width = '';
  if (it.img != null) { img.src = '/ocr/img/' + encodeURIComponent(String(it.img)); img.alt = 'OCR 이미지'; }
  else { img.removeAttribute('src'); img.alt = '이미지 파일이 비워졌습니다(디스크 상한)'; }
  const th = $('thumbs');
  while (th.firstChild) th.removeChild(th.firstChild);
  for (const tid of it.thumbs || []) {
    const t = document.createElement('img');
    t.src = '/ocr/img/' + encodeURIComponent(String(tid));
    t.alt = '같은 묶음';
    th.appendChild(t);
  }
  setDatalist(it.site);
  const ans = $('ans');
  ans.value = S.edit ? (S.edit.status === 'labeled' ? (S.edit.label || '') : '') : '';
  ans.focus();
  if (S.edit) ans.select();
  const nx = S.items[1];
  if (nx && nx.img != null) { const p = new Image(); p.src = '/ocr/img/' + encodeURIComponent(String(nx.img)); }
}

function setCount() {
  $('qcount').textContent = '대기 ' + (S.pending < 0 ? '-' : S.pending);
  document.title = (S.badge && S.pending > 0) ? ('(' + S.pending + ') OCR 라벨') : 'OCR 라벨';
}

function beep() {
  try {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return;
    S.ac = S.ac || new Ctor();
    const o = S.ac.createOscillator(), g = S.ac.createGain();
    o.frequency.value = 880;
    g.gain.value = 0.06;
    o.connect(g); g.connect(S.ac.destination);
    o.start(); o.stop(S.ac.currentTime + 0.12);
  } catch (e) {}
}

async function poll() {
  const r = await api('GET', '/ocr/queue?limit=30');
  if (r.ok && r.j) {
    const cur = S.items[0] ? S.items[0].id : null;
    S.items = mergeQueue(cur, r.j.items, S.done, Date.now());
    S.suggest = Object.assign(S.suggest || {}, r.j.suggest || {});
    if (queueGrew(S.pending, r.j.pending) && S.sound) beep();
    S.pending = r.j.pending;
    setCount();
    render();
  }
}

async function save(kind) {
  const it = current();
  if (!it) return;
  const ans = $('ans');
  const val = ans.value.trim();
  let body;
  if (kind === 'bad') body = {id: it.id, bad: true};
  else { if (!val) return; body = {id: it.id, label: val}; }
  const wasEdit = !!S.edit;
  if (wasEdit) S.edit = null;
  else { S.done[it.id] = Date.now(); S.items.shift(); S.pending = Math.max(0, S.pending - 1); setCount(); }
  S.shown = null;
  render();
  const r = await api('POST', '/ocr/label', body);
  if (!r.ok) {
    toast('저장 실패: ' + r.err);
    if (!wasEdit && r.status !== 404) { delete S.done[it.id]; S.items.unshift(it); S.shown = null; render(); $('ans').value = val; }
    return;
  }
  toast(kind === 'bad' ? '잘못된 이미지로 표시' : ('저장: ' + val + (it.members > 1 ? ' (' + it.members + '장)' : '')));
  loadHist();
}

async function skip() {
  if (S.edit) { S.edit = null; S.shown = null; render(); return; }
  const it = S.items[0];
  if (!it) return;
  S.items.push(S.items.shift());
  S.shown = null;
  render();
  const r = await api('POST', '/ocr/skip', {id: it.id});
  if (!r.ok && r.status !== 404 && r.status !== 409) toast('나중에 실패: ' + r.err);
}

async function undo() {
  const r = await api('POST', '/ocr/undo', {});
  if (!r.ok) { toast(r.status === 404 ? '되돌릴 것이 없습니다' : ('되돌리기 실패: ' + r.err)); return; }
  delete S.done[r.j.id];
  S.edit = null;
  S.shown = null;
  toast(r.j.status === 'pending' ? '되돌림 — 다시 입력하세요' : ('되돌림 — ' + (r.j.label || r.j.status)));
  await poll();
  const i = S.items.findIndex(function (x) { return x.id === r.j.id; });
  if (i > 0) { S.items.unshift(S.items.splice(i, 1)[0]); S.shown = null; render(); }
  loadHist();
}

async function loadHist() {
  const r = await api('GET', '/ocr/history?limit=30');
  if (!r.ok || !r.j) return;
  const ol = $('hist');
  while (ol.firstChild) ol.removeChild(ol.firstChild);
  for (const h of r.j.items) {
    const li = document.createElement('li');
    if (h.img != null) {
      const im = document.createElement('img');
      im.src = '/ocr/img/' + encodeURIComponent(String(h.img));
      im.alt = '';
      li.appendChild(im);
    }
    const lb = document.createElement('span');
    lb.className = 'lb' + (h.status === 'bad' ? ' bad' : '');
    lb.textContent = h.status === 'bad' ? '잘못된 이미지' : h.label;
    li.appendChild(lb);
    const meta = document.createElement('span');
    meta.className = 'mut';
    meta.textContent = h.site + (h.count > 1 ? ' ×' + h.count : '');
    li.appendChild(meta);
    li.addEventListener('click', function () { S.edit = h; S.shown = null; render(); });
    ol.appendChild(li);
  }
}

async function loadStats() {
  const box = $('stats');
  const r = await api('GET', '/ocr/stats');
  if (!r.ok || !r.j) { box.textContent = '통계 실패: ' + r.err; return; }
  while (box.firstChild) box.removeChild(box.firstChild);
  const tb = document.createElement('table');
  const head = ['사이트', '대기', '라벨', '나쁨', 'Gemini 불일치', '로컬 불일치'];
  const tr0 = document.createElement('tr');
  for (const h of head) { const th = document.createElement('th'); th.textContent = h; tr0.appendChild(th); }
  tb.appendChild(tr0);
  for (const site of Object.keys(r.j.sites).sort()) {
    const d = r.j.sites[site];
    const cells = [site, d.pending, d.labeled, d.bad,
      pctText(d.gemini_disagree_rate) + ' (' + d.gemini_compared + ')',
      pctText(d.local_disagree_rate) + ' (' + d.local_compared + ')'];
    const tr = document.createElement('tr');
    for (const c of cells) { const td = document.createElement('td'); td.textContent = String(c); tr.appendChild(td); }
    tb.appendChild(tr);
  }
  box.appendChild(tb);
  const p = document.createElement('div');
  p.className = 'mut';
  p.textContent = '디스크 ' + Math.round(r.j.disk_bytes / 1048576) + 'MB / ' + Math.round(r.j.disk_cap / 1048576) + 'MB';
  box.appendChild(p);
}

function onKey(ev) {
  const it = current();
  const act = decideKey(ev, $('ans').value, it ? it.gemini : '');
  if (act === 'none') return;
  // ★한글 입력 중 Enter 는 막지 않는다★ — 막으면 마지막 글자 조합이 깨질 수 있다. 조합이 끝나면(compositionend) 저장
  if (act === 'save_after_compose') { S.composeSave = true; return; }
  ev.preventDefault();
  if (act === 'save') save('label');
  else if (act === 'skip') skip();
  else if (act === 'fill') { $('ans').value = it.gemini; $('ans').select(); }
  else if (act === 'undo') undo();
}

function boot() {
  S.sound = lsGet('ocr_sound') === '1';
  S.badge = lsGet('ocr_badge') !== '0';
  $('opt-sound').checked = S.sound;
  $('opt-badge').checked = S.badge;
  $('opt-sound').addEventListener('change', function (e) { S.sound = e.target.checked; lsSet('ocr_sound', S.sound ? '1' : '0'); if (S.sound) beep(); });
  $('opt-badge').addEventListener('change', function (e) { S.badge = e.target.checked; lsSet('ocr_badge', S.badge ? '1' : '0'); setCount(); });
  $('btn-stats').addEventListener('click', function () { const b = $('stats'); b.hidden = !b.hidden; if (!b.hidden) loadStats(); });
  $('btn-save').addEventListener('click', function () { save('label'); });
  $('btn-bad').addEventListener('click', function () { save('bad'); });
  $('btn-skip').addEventListener('click', function () { skip(); });
  $('btn-undo').addEventListener('click', function () { undo(); });
  $('img').addEventListener('load', fitImage);
  window.addEventListener('resize', fitImage);
  const ans = $('ans');
  ans.addEventListener('keydown', onKey);
  ans.addEventListener('compositionend', function () {
    if (!S.composeSave) return;
    S.composeSave = false;
    setTimeout(function () { if ($('ans').value.trim()) save('label'); }, 0);
  });
  poll();
  loadHist();
  setInterval(poll, 2000);
}
boot();
</script>
</body></html>
"""
