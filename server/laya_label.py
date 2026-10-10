# -*- coding: utf-8 -*-
"""★라야 라벨 (2026-10-11 · 주인님 «팜뷰에 ocr 옆에 라야 학습해서 객관식 답변으로, 없으면 주관식으로»)★

라야(로컬 분류 모델, 개발컴 ops/tools/laya)가 매크로 알람 문구를 보고 «무슨 일인가» 보기를 고르고, 주인님이 팜뷰 «라야» 탭에서
객관식으로 맞다/아니다를 고르거나 맞는 보기가 없으면 직접 적는다 → 그 답이 라야의 다음 학습 데이터가 된다.
계약 정본 = updater/FV_API.md «2026-10-11 추가 — E. 라야 라벨» (= farmview/docs/FV_API.md 사본).

★main.py 에는 init 세 줄 + 씨앗 한 줄만★ (ocr_label.py 와 같은 구조):
    import laya_label as _laya_label   # noqa: E402
    _laya_label.init(app, __name__)
    (telegram_send 안) _laya_label.seed_bg(tenant, name, text)
  ★순환 import 를 피하는 법★ import 시점에 main 을 부르지 않는다. init() 이 받은 모듈 ★이름★ 으로 핸들러 안에서 sys.modules 를 늦게 찾는다(_m()).

엔드포인트
  팜뷰 (X-FV-Token · 테넌트 = FV_TENANT · 에러 {ok:false, error, err, code} = main._fv_err)
    GET  /api/fv/laya/queue?limit=30     대기열(라야 확신 낮은 것 먼저) + 보기(choices)
    POST /api/fv/laya/answer {id, category, text?, needs_human?}   저장·고치기
    POST /api/fv/laya/skip {id}          맨 뒤로
    POST /api/fv/laya/undo {}            마지막 답/건너뛰기 하나 되돌리기
    GET  /api/fv/laya/stats              대기·라벨 수·보기별·라야 일치율·«새 보기 후보»
  개발컴 (X-Api-Key → 테넌트)
    GET  /laya/labels?since=<lts>        답 달린 항목 증분(되돌려 사라진 것은 deleted:true)
    GET  /laya/items?since=<lts>&status=pending|all   예측할 문구 받아 가기(더하기만)
    POST /laya/pred {ver, items:[{id, category:{pick,conf}, needs_human:{pick,conf}}]}   예측 올리기

★지키는 것★
  · 이메일·아이디는 ★저장 전에★ 가린다(mask_text) — 원문은 어디에도 안 남는다.
  · 템플릿 키(tpl)당 최대 LAYA_TPL_MAX(3)건 — 같은 알람 200번 고르게 하지 않는다. 테넌트당 LAYA_ITEMS_MAX 상한.
  · 모든 줄에 tenant 를 적고 모든 조회를 tenant 로 가둔다. 쓰기는 한 프로세스 안에서 잠금 하나로 줄 세운다(Railway 는 인스턴스 하나).
  · 씨앗은 알람 응답을 막지 않는다 — 백그라운드, 실패는 삼킨다.
"""
import asyncio
import contextlib
import json
import math
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import database as _db
from ocr_label import read_json_capped

router = APIRouter()

# ─── 상수 ─────────────────────────────────────────────────────────────────────
LAYA_TPL_MAX = 3                 # 템플릿 키당 최대 항목 수(대기 + 답한 것)
LAYA_ITEMS_MAX = 20000           # 테넌트당 항목 상한(넘으면 새 항목을 버린다)
LAYA_TEXT_MAX = 400              # 저장하는 알람 문구 길이
LAYA_TPL_LEN = 160               # 템플릿 키 길이
LAYA_OTHER_MAX = 200             # 주관식 글자 수 상한
LAYA_UNDO_DEPTH = 50             # 되돌리기로 거슬러 갈 수 있는 수
LAYA_PRED_MAX = 2000             # /laya/pred 한 번에 올리는 항목 수
LAYA_BODY_CAP = 16 * 1024        # 사람 쪽 POST 본문 상한
LAYA_PRED_BODY_CAP = 1024 * 1024  # /laya/pred 본문 상한
LAYA_PAGE_MAX = 5000             # labels/items 한 번에 내보내는 수 상한
LAYA_OTHER_CLUSTER_MIN = 3       # «새 보기 후보» 로 올리는 같은 주관식 글 수
_LTS_STEP = 1e-5                 # lts 단조 증가 최소 간격

CATEGORIES = [
    ("routine_progress", "정상 진행 보고", "순환 시작·완료, 정보수집 끝, 전환 완료, 일일 초기화"),
    ("hunt_stopped", "사냥 멈춤", "사망, 어비스 밖, 큐브 꽉참, 진행도 못 읽음, 타겟 불가"),
    ("account_switch_fail", "계정 전환·로그인 실패", "본컴 런처 전환 실패·보류, 원격 크롬 로그인 실패"),
    ("connect_or_stream_fail", "접속·스트리밍 실패", "슬롯 접속 초과, 파섹 대기·재인증, 스트림 끊김"),
    ("ui_unreachable", "화면 요소 못 찾음", "패널 안 열림, 클릭 안 먹음, 버튼 못 찾음"),
    ("missing_config_or_template", "설정·템플릿 없음", "계정줄 템플릿 없음, PIN 미설정·길이 틀림"),
    ("dungeon_or_corridor_result", "던전·회랑 결과", "던전·악몽·회랑 끝남/중단 + 결과 숫자"),
    ("command_rejected", "명령 거부됨", "다른 명령·정보수집 중이라 원격 명령 거부"),
]
CATEGORY_KEYS = [c[0] for c in CATEGORIES]
OTHER_KEY = "other"
_KST = timezone(timedelta(hours=9))

_MAIN_NAME = "main"
_BG: set = set()                 # 씨앗 백그라운드 과제(참조 유지)
_DROPPED = {"tpl": 0, "cap": 0, "empty": 0}   # 서버가 뜬 뒤 못 쌓은 수


class LayaError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


def init(app, main_module_name: str = "main") -> None:
    global _MAIN_NAME
    _MAIN_NAME = main_module_name
    app.include_router(router)


def _m():
    mod = sys.modules.get(_MAIN_NAME)
    if mod is None:
        import importlib
        mod = importlib.import_module(_MAIN_NAME)
    return mod


def _dbp() -> str:
    return _db.DB_PATH


def _busy_s() -> float:
    return float(getattr(_db, "DB_BUSY_S", 30.0))


# ★DB 는 동기 sqlite3 를 실행기 스레드에서 쓴다 (2026-10-11)★ — aiosqlite 연결은 스레드를 하나씩 물고 있어, 씨앗 과제가
#   이벤트 루프 종료로 중간에 취소되면 그 스레드가 남아 프로세스가 끝나지 않았다(시험이 종료 때 멈춤). 실행기 호출은 취소돼도
#   끝까지 돌고 연결을 닫는다. 쓰기는 프로세스 안 잠금 하나로 줄 세운다(루프가 여럿이어도 — 시험).
_DB_LOCK = threading.Lock()


def _connect():
    c = sqlite3.connect(_dbp(), timeout=_busy_s(), isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


_DDL = ["""
    CREATE TABLE IF NOT EXISTS laya_item (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant    TEXT NOT NULL,
        text      TEXT NOT NULL,
        pc        TEXT NOT NULL DEFAULT '',
        t         REAL NOT NULL,
        tpl       TEXT NOT NULL,
        pred_cat  TEXT,
        pred_conf REAL,
        pred_nh   INTEGER,
        pred_nh_conf REAL,
        pred_ver  TEXT,
        pred_at   REAL,
        status    TEXT NOT NULL DEFAULT 'pending',
        a_cat     TEXT,
        a_text    TEXT,
        a_nh      INTEGER,
        a_at      REAL,
        a_pick    TEXT,
        a_conf    REAL,
        had_label INTEGER NOT NULL DEFAULT 0,
        skipped_at REAL,
        lts       REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_laya_item_q ON laya_item(tenant, status)",
    "CREATE INDEX IF NOT EXISTS ix_laya_item_tpl ON laya_item(tenant, tpl)",
    "CREATE INDEX IF NOT EXISTS ix_laya_item_lts ON laya_item(tenant, lts)",
    """
    CREATE TABLE IF NOT EXISTS laya_hist (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant   TEXT NOT NULL,
        item_id  INTEGER NOT NULL,
        action   TEXT NOT NULL,
        prev     TEXT NOT NULL,
        at       REAL NOT NULL,
        undone   INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS ix_laya_hist ON laya_hist(tenant, undone, id)"]
_INITED: set = set()             # 표를 만든 DB 경로


def _ensure(c) -> None:
    p = _dbp()
    if p in _INITED:
        return
    c.execute("PRAGMA journal_mode=WAL")
    for q in _DDL:
        c.execute(q)
    _INITED.add(p)


@contextlib.contextmanager
def _tx(c):
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


async def _run(fn, *args):
    """fn(conn, *args) 를 실행기 스레드에서 — 잠금 안에서 연결을 열고 닫는다."""
    def call():
        with _DB_LOCK:
            c = _connect()
            try:
                _ensure(c)
                return fn(c, *args)
            finally:
                c.close()
    return await asyncio.get_running_loop().run_in_executor(None, call)


# ─── 마스킹 · 템플릿 ──────────────────────────────────────────────────────────
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_ID_FIELD = re.compile(r"(?<![A-Za-z])(아이디|이메일|email|e-mail|login|id)(\s*[:=：]\s*)([^\s,;)\]】]+)", re.I)
_QUOTED_TOKEN = re.compile(r"""(['"‘’“”`])([A-Za-z0-9._\-]{4,})\1""")
_LONG_DIGITS = re.compile(r"\d{9,}")
_PHONE = re.compile(r"\b0\d{1,2}-\d{3,4}-\d{4}\b")


def mask_text(s) -> str:
    """알람 문구에서 이메일·아이디를 가린다. 줄바꿈·제어문자는 빈칸. 앞뒤 공백 걷고 LAYA_TEXT_MAX 자로 자른다."""
    t = unicodedata.normalize("NFC", str(s or ""))
    t = _CTRL.sub(" ", t)
    t = _EMAIL.sub("<email>", t)
    t = _ID_FIELD.sub(lambda m: m.group(1) + m.group(2) + "<id>", t)
    t = _PHONE.sub("<num>", t)

    def _q(m):
        tok = m.group(2)
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            return m.group(1) + "<id>" + m.group(1)
        return m.group(0)
    t = _QUOTED_TOKEN.sub(_q, t)
    t = _LONG_DIGITS.sub("<num>", t)
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    return t[:LAYA_TEXT_MAX]


def make_tpl(masked: str) -> str:
    """템플릿 키 — 숫자·PC 번호·시각을 지워 «같은 알람»을 한 키로 묶는다(마스킹 뒤 문구에서)."""
    s = str(masked or "").lower()
    s = re.sub(r"pc-\d+[a-z]?", "pc-#", s)
    s = re.sub(r"\d{1,2}:\d{2}(?::\d{2})?", "<time>", s)
    s = re.sub(r"\d+(?:[.,]\d+)*", "#", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:LAYA_TPL_LEN]


# ─── 값 검사 ──────────────────────────────────────────────────────────────────
def _err(code: int, msg: str):
    raise LayaError(code, msg)


def _i64(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and -(2 ** 63) <= v <= 2 ** 63 - 1


def _id(b: dict) -> int:
    v = b.get("id")
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        v = int(v.strip())
    if not _i64(v):
        raise LayaError(400, "id 는 정수여야 합니다")
    return v


def _norm_text(v) -> str:
    s = unicodedata.normalize("NFC", str(v or ""))
    s = _CTRL.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _bool(v, what: str):
    """true/false(또는 yes/no 문자열) → bool. None → None. 그 밖은 400."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "yes", "y", "예"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("false", "no", "n", "아니오"):
        return False
    raise LayaError(400, "%s 는 true/false 여야 합니다" % what)


def _conf(v):
    """0~1 숫자(문자열·bool·NaN 은 아님) → float, 아니면 None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if math.isnan(f) or math.isinf(f) or f < 0.0 or f > 1.0:
        return None
    return f


def _limit(v, default: int, hi: int) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise LayaError(400, "limit 는 정수여야 합니다")
    return max(1, min(hi, n))


def _since(v) -> float:
    if v in (None, ""):
        return 0.0
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        raise LayaError(400, "since 는 숫자여야 합니다")
    if math.isnan(f) or math.isinf(f):
        raise LayaError(400, "since 는 숫자여야 합니다")
    return f


def _next_lts(c, tenant: str) -> float:
    last = float(c.execute("SELECT COALESCE(MAX(lts), 0) FROM laya_item WHERE tenant=?", (tenant,)).fetchone()[0])
    return max(time.time(), last + _LTS_STEP)


# ─── 항목 모양 ────────────────────────────────────────────────────────────────
_COLS = ("id, text, pc, t, tpl, pred_cat, pred_conf, pred_nh, pred_nh_conf, pred_ver, status, "
         "a_cat, a_text, a_nh, a_at, a_pick, a_conf, lts, had_label, skipped_at")


def _laya_of(r) -> "dict | None":
    cat = {"pick": r["pred_cat"], "conf": r["pred_conf"]} if r["pred_cat"] is not None else None
    nh = {"pick": bool(r["pred_nh"]), "conf": r["pred_nh_conf"]} if r["pred_nh"] is not None else None
    if cat is None and nh is None:
        return None
    return {"category": cat, "needs_human": nh, "ver": r["pred_ver"]}


def _answer_of(r) -> "dict | None":
    if r["status"] != "answered":
        return None
    return {"category": r["a_cat"], "text": r["a_text"],
            "needs_human": None if r["a_nh"] is None else bool(r["a_nh"]),
            "at": r["a_at"], "laya_pick": r["a_pick"], "laya_conf": r["a_conf"]}


def _item(r) -> dict:
    return {"id": r["id"], "kind": "alarm", "text": r["text"], "pc": r["pc"], "t": r["t"], "tpl": r["tpl"],
            "laya": _laya_of(r), "answer": _answer_of(r)}


def choices() -> dict:
    cat = [{"key": k, "ko": ko, "desc": d} for k, ko, d in CATEGORIES]
    cat.append({"key": OTHER_KEY, "ko": "해당 없음 — 직접 적기", "desc": "맞는 보기가 없으면 직접 적는다(1~%d자)" % LAYA_OTHER_MAX,
                "free": True})
    return {"category": cat,
            "needs_human": [{"key": "yes", "ko": "예", "value": True}, {"key": "no", "ko": "아니오", "value": False}]}


# ─── 씨앗 ────────────────────────────────────────────────────────────────────
def _seed_sync(c, tenant: str, masked: str, tpl: str, pcs: str) -> str:
    with _tx(c):
        if c.execute("SELECT COUNT(*) FROM laya_item WHERE tenant=? AND tpl=?", (tenant, tpl)).fetchone()[0] >= LAYA_TPL_MAX:
            _DROPPED["tpl"] += 1
            return "tpl_full"
        if c.execute("SELECT COUNT(*) FROM laya_item WHERE tenant=?", (tenant,)).fetchone()[0] >= LAYA_ITEMS_MAX:
            _DROPPED["cap"] += 1
            return "cap"
        c.execute("INSERT INTO laya_item(tenant, text, pc, t, tpl, lts) VALUES(?,?,?,?,?,?)",
                  (tenant, masked, pcs, time.time(), tpl, _next_lts(c, tenant)))
    return "added"


async def seed_alarm(tenant: str, pc, text) -> str:
    """알람 문구 하나를 대기열에 넣는다. 돌려주는 값: 'added' | 'empty' | 'tpl_full' | 'cap'."""
    masked = mask_text(text)
    if not masked:
        _DROPPED["empty"] += 1
        return "empty"
    return await _run(_seed_sync, tenant, masked, make_tpl(masked), _norm_text(pc)[:40])


async def _seed_safe(tenant, pc, text) -> None:
    try:
        await seed_alarm(tenant, pc, text)
    except Exception as e:          # 씨앗 실패가 알람 전송에 번지지 않게
        print("[라야] 씨앗 실패(무시): %r" % (e,), flush=True)


def seed_bg(tenant: str, pc, text) -> None:
    """telegram_send 가 부른다 — 응답을 안 기다린다. 이벤트 루프가 없으면(동기 시험) 조용히 건너뛴다."""
    try:
        t = asyncio.ensure_future(_seed_safe(tenant, pc, text))
    except RuntimeError:
        return
    _BG.add(t)
    t.add_done_callback(_BG.discard)


# ─── 핵심 함수 (사람 쪽) ──────────────────────────────────────────────────────
def _pending_count(c, tenant: str) -> int:
    return c.execute("SELECT COUNT(*) FROM laya_item WHERE tenant=? AND status='pending'", (tenant,)).fetchone()[0]


def _queue_sync(c, tenant: str, lim: int) -> dict:
    rows = c.execute(
        "SELECT " + _COLS + " FROM laya_item WHERE tenant=? AND status='pending' "
        "ORDER BY CASE WHEN skipped_at IS NOT NULL THEN 2 WHEN pred_cat IS NOT NULL THEN 0 ELSE 1 END, "
        "CASE WHEN skipped_at IS NULL AND pred_cat IS NOT NULL THEN pred_conf END ASC, "
        "CASE WHEN skipped_at IS NOT NULL THEN skipped_at END ASC, t ASC, id ASC LIMIT ?", (tenant, lim)).fetchall()
    return {"pending": _pending_count(c, tenant), "items": [_item(r) for r in rows], "choices": choices(), "now": time.time()}


async def queue_core(tenant: str, lim: int) -> dict:
    return await _run(_queue_sync, tenant, lim)


def _snap(r) -> dict:
    """되돌리기용 — 항목의 답·건너뛰기 상태 스냅샷."""
    return {"status": r["status"], "a_cat": r["a_cat"], "a_text": r["a_text"], "a_nh": r["a_nh"], "a_at": r["a_at"],
            "a_pick": r["a_pick"], "a_conf": r["a_conf"], "skipped_at": r["skipped_at"]}


def _hist_put(c, tenant: str, item_id: int, action: str, prev: dict, now: float) -> None:
    c.execute("INSERT INTO laya_hist(tenant, item_id, action, prev, at) VALUES(?,?,?,?,?)",
              (tenant, item_id, action, json.dumps(prev, ensure_ascii=False), now))
    # 거슬러 갈 수 있는 깊이만 남긴다
    c.execute("DELETE FROM laya_hist WHERE tenant=? AND id NOT IN (SELECT id FROM laya_hist WHERE tenant=? ORDER BY id DESC LIMIT ?)",
              (tenant, tenant, LAYA_UNDO_DEPTH))


def _get(c, tenant: str, iid: int):
    return c.execute("SELECT " + _COLS + " FROM laya_item WHERE id=? AND tenant=?", (iid, tenant)).fetchone()


def _answer_sync(c, tenant: str, iid: int, cat, text, has_nh: bool, nh) -> dict:
    with _tx(c):
        r = _get(c, tenant, iid)
        if not r:
            raise LayaError(404, "없는 항목입니다")
        now = time.time()
        prev_answer = _answer_of(r)
        if cat is None:
            if r["status"] != "answered":
                raise LayaError(400, "category 가 필요합니다")
            if not has_nh:
                raise LayaError(400, "category 또는 needs_human 이 필요합니다")
            new_cat, new_text = r["a_cat"], r["a_text"]
        else:
            new_cat, new_text = cat, text
        new_nh = (None if nh is None else int(nh)) if has_nh else r["a_nh"]
        pick, conf = (r["a_pick"], r["a_conf"]) if r["status"] == "answered" else (r["pred_cat"], r["pred_conf"])
        _hist_put(c, tenant, iid, "answer", _snap(r), now)
        c.execute("UPDATE laya_item SET status='answered', a_cat=?, a_text=?, a_nh=?, a_at=?, a_pick=?, a_conf=?, "
                  "had_label=1, skipped_at=NULL, lts=? WHERE id=?",
                  (new_cat, new_text, new_nh, now, pick, conf, _next_lts(c, tenant), iid))
        pending = _pending_count(c, tenant)
    ans = {"category": new_cat, "text": new_text, "needs_human": None if new_nh is None else bool(new_nh),
           "at": now, "laya_pick": pick, "laya_conf": conf}
    return {"ok": True, "id": iid, "answer": ans, "prev": prev_answer, "pending": pending}


async def answer_core(tenant: str, b: dict) -> dict:
    iid = _id(b)
    cat = b.get("category")
    has_nh = "needs_human" in b
    nh = _bool(b.get("needs_human"), "needs_human") if has_nh else None
    if cat is not None and not isinstance(cat, str):
        raise LayaError(400, "category 는 보기 key 문자열이어야 합니다")
    if cat is not None and cat not in CATEGORY_KEYS and cat != OTHER_KEY:
        raise LayaError(400, "알 수 없는 보기입니다: %s" % cat[:40])
    text = None
    if cat == OTHER_KEY:
        text = _norm_text(b.get("text"))
        if not text:
            raise LayaError(400, "직접 적기(other)는 text 가 필요합니다")
        if len(text) > LAYA_OTHER_MAX:
            raise LayaError(400, "text 는 %d자 이하여야 합니다" % LAYA_OTHER_MAX)
    return await _run(_answer_sync, tenant, iid, cat, text, has_nh, nh)


def _skip_sync(c, tenant: str, iid: int) -> dict:
    with _tx(c):
        r = _get(c, tenant, iid)
        if not r:
            raise LayaError(404, "없는 항목입니다")
        if r["status"] != "pending":
            raise LayaError(409, "대기 중인 항목이 아닙니다")
        now = time.time()
        _hist_put(c, tenant, iid, "skip", _snap(r), now)
        c.execute("UPDATE laya_item SET skipped_at=? WHERE id=?", (now, iid))
        pending = _pending_count(c, tenant)
    return {"ok": True, "id": iid, "pending": pending}


async def skip_core(tenant: str, b: dict) -> dict:
    return await _run(_skip_sync, tenant, _id(b))


def _undo_sync(c, tenant: str) -> dict:
    with _tx(c):
        row = c.execute(
            "SELECT h.id, h.item_id, h.action, h.prev FROM laya_hist h JOIN laya_item i ON i.id=h.item_id "
            "WHERE h.tenant=? AND h.undone=0 ORDER BY h.id DESC LIMIT 1", (tenant,)).fetchone()
        if not row:
            raise LayaError(404, "되돌릴 것이 없습니다")
        hid, iid, action, prev_s = row
        p = json.loads(prev_s)
        # had_label 은 그대로(1) — 개발컴이 이미 받아 갔을 수 있어 «deleted» 가 나가야 한다
        c.execute("UPDATE laya_item SET status=?, a_cat=?, a_text=?, a_nh=?, a_at=?, a_pick=?, a_conf=?, skipped_at=?, lts=? "
                  "WHERE id=?", (p["status"], p["a_cat"], p["a_text"], p["a_nh"], p["a_at"], p["a_pick"], p["a_conf"],
                                 p["skipped_at"], _next_lts(c, tenant), iid))
        c.execute("UPDATE laya_hist SET undone=1 WHERE id=?", (hid,))
        r = _get(c, tenant, iid)
        pending = _pending_count(c, tenant)
    return {"ok": True, "id": iid, "action": action, "answer": _answer_of(r) if r else None, "pending": pending}


async def undo_core(tenant: str) -> dict:
    return await _run(_undo_sync, tenant)


def _norm_other(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip().casefold()


def _stats_sync(c, tenant: str, day0: float) -> dict:
    def one(sql, args=()):
        return c.execute(sql, args).fetchall()
    pending = one("SELECT COUNT(*) FROM laya_item WHERE tenant=? AND status='pending'", (tenant,))[0][0]
    skipped = one("SELECT COUNT(*) FROM laya_item WHERE tenant=? AND status='pending' AND skipped_at IS NOT NULL", (tenant,))[0][0]
    predicted = one("SELECT COUNT(*) FROM laya_item WHERE tenant=? AND status='pending' AND pred_cat IS NOT NULL", (tenant,))[0][0]
    labeled = one("SELECT COUNT(*) FROM laya_item WHERE tenant=? AND status='answered'", (tenant,))[0][0]
    today = one("SELECT COUNT(*) FROM laya_item WHERE tenant=? AND status='answered' AND a_at>=?", (tenant, day0))[0][0]
    by_cat = {k: n for k, n in one("SELECT a_cat, COUNT(*) FROM laya_item WHERE tenant=? AND status='answered' GROUP BY a_cat", (tenant,))}
    nh = {"yes": 0, "no": 0, "unset": 0}
    for v, n in one("SELECT a_nh, COUNT(*) FROM laya_item WHERE tenant=? AND status='answered' GROUP BY a_nh", (tenant,)):
        nh["unset" if v is None else ("yes" if v else "no")] += n
    allr = one("SELECT COUNT(*), COALESCE(SUM(a_pick=a_cat),0) FROM laya_item "
               "WHERE tenant=? AND status='answered' AND a_pick IS NOT NULL", (tenant,))[0]
    rec = one("SELECT a_pick=a_cat FROM laya_item WHERE tenant=? AND status='answered' AND a_pick IS NOT NULL "
              "ORDER BY a_at DESC, id DESC LIMIT 50", (tenant,))
    others = [r[0] for r in one("SELECT a_text FROM laya_item WHERE tenant=? AND status='answered' AND a_cat='other' "
                                "AND a_text IS NOT NULL", (tenant,))]
    ver = one("SELECT pred_ver FROM laya_item WHERE tenant=? AND pred_at IS NOT NULL ORDER BY pred_at DESC LIMIT 1", (tenant,))
    n_all, m_all = int(allr[0]), int(allr[1])
    n_rec, m_rec = len(rec), sum(1 for r in rec if r[0])

    def rate(n, m):
        return None if n == 0 else round(m / n, 4)
    groups: dict = {}
    for s in others:
        k = _norm_other(s)
        if not k:
            continue
        g = groups.setdefault(k, {"text": s, "count": 0})
        g["count"] += 1
    clusters = sorted((g for g in groups.values() if g["count"] >= LAYA_OTHER_CLUSTER_MIN),
                      key=lambda g: (-g["count"], g["text"]))[:20]
    return {"pending": pending, "labeled": labeled, "labeled_today": today, "skipped": skipped,
            "by_category": by_cat, "needs_human": nh,
            "agree": {"recent50": {"n": n_rec, "match": m_rec, "rate": rate(n_rec, m_rec)},
                      "all": {"n": n_all, "match": m_all, "rate": rate(n_all, m_all)}},
            "other_clusters": clusters, "predicted": predicted, "unpredicted": pending - predicted,
            "ver": ver[0][0] if ver else None, "dropped": dict(_DROPPED), "now": time.time()}


async def stats_core(tenant: str) -> dict:
    day0 = datetime.now(_KST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return await _run(_stats_sync, tenant, day0)


# ─── 핵심 함수 (개발컴 쪽) ────────────────────────────────────────────────────
def _labels_sync(c, tenant: str, since: float, lim: int) -> dict:
    rows = c.execute("SELECT " + _COLS + " FROM laya_item WHERE tenant=? AND had_label=1 AND lts>? ORDER BY lts ASC LIMIT ?",
                     (tenant, since, lim + 1)).fetchall()
    more = len(rows) > lim
    rows = rows[:lim]
    out = []
    for r in rows:
        base = {"id": r["id"], "text": r["text"], "pc": r["pc"], "t": r["t"], "tpl": r["tpl"], "lts": r["lts"]}
        if r["status"] == "answered":
            base.update({"category": r["a_cat"], "other_text": r["a_text"],
                         "needs_human": None if r["a_nh"] is None else bool(r["a_nh"]), "at": r["a_at"]})
        else:
            base.update({"category": None, "other_text": None, "needs_human": None, "at": None, "deleted": True})
        out.append(base)
    return {"labels": out, "cursor": rows[-1]["lts"] if rows else since, "more": more, "now": time.time()}


async def labels_core(tenant: str, since: float, lim: int) -> dict:
    return await _run(_labels_sync, tenant, since, lim)


def _items_sync(c, tenant: str, since: float, status: str, lim: int) -> dict:
    sql = "SELECT " + _COLS + " FROM laya_item WHERE tenant=? AND lts>?"
    if status == "pending":
        sql += " AND status='pending'"
    rows = c.execute(sql + " ORDER BY lts ASC LIMIT ?", (tenant, since, lim + 1)).fetchall()
    more = len(rows) > lim
    rows = rows[:lim]
    return {"items": [_item(r) for r in rows], "cursor": rows[-1]["lts"] if rows else since, "more": more, "now": time.time()}


async def items_core(tenant: str, since: float, status: str, lim: int) -> dict:
    if status not in ("pending", "all"):
        raise LayaError(400, "status 는 pending 또는 all 이어야 합니다")
    return await _run(_items_sync, tenant, since, status, lim)


def _pred_sync(c, tenant: str, ver: str, good: list) -> tuple:
    updated, unknown = 0, []
    now = time.time()
    with _tx(c):
        for iid, pick, cf, nh_pick, nh_conf in good:
            cur = c.execute("UPDATE laya_item SET pred_cat=?, pred_conf=?, pred_nh=?, pred_nh_conf=?, pred_ver=?, pred_at=? "
                            "WHERE id=? AND tenant=?",
                            (pick, cf, None if nh_pick is None else int(nh_pick), nh_conf, ver, now, iid, tenant))
            if cur.rowcount:
                updated += 1
            else:
                unknown.append(iid)
    return updated, unknown


async def pred_core(tenant: str, b: dict) -> dict:
    ver = _norm_text(b.get("ver"))[:40]
    if not ver:
        raise LayaError(400, "ver 가 필요합니다")
    items = b.get("items")
    if not isinstance(items, list):
        raise LayaError(400, "items 는 목록이어야 합니다")
    if len(items) > LAYA_PRED_MAX:
        raise LayaError(400, "items 는 한 번에 %d개까지입니다" % LAYA_PRED_MAX)
    good, invalid = [], []
    for it in items:
        iid = it.get("id") if isinstance(it, dict) else None
        if not _i64(iid):
            invalid.append(iid if isinstance(iid, (int, str)) and not isinstance(iid, bool) else None)
            continue
        c = it.get("category")
        pick = c.get("pick") if isinstance(c, dict) else None
        cf = _conf(c.get("conf")) if isinstance(c, dict) else None
        if (pick not in CATEGORY_KEYS and pick != OTHER_KEY) or cf is None:
            invalid.append(iid)
            continue
        nh = it.get("needs_human")
        nh_pick = nh_conf = None
        if isinstance(nh, dict):
            try:
                nh_pick = _bool(nh.get("pick"), "needs_human.pick")
            except LayaError:
                invalid.append(iid)
                continue
            nh_conf = _conf(nh.get("conf"))
            if nh_pick is None or nh_conf is None:
                invalid.append(iid)
                continue
        elif nh is not None:
            invalid.append(iid)
            continue
        good.append((iid, pick, cf, nh_pick, nh_conf))
    updated, unknown = await _run(_pred_sync, tenant, ver, good)
    return {"ok": True, "updated": updated, "unknown": unknown, "invalid": invalid}


# ─── 라우터 ──────────────────────────────────────────────────────────────────
async def _body(request: Request, cap: int = LAYA_BODY_CAP) -> dict:
    return await read_json_capped(request, cap, _err)


async def _fv_call(request: Request, fn):
    m = _m()
    g = m._fv_guard(request)              # 토큰부터 — 본문·쿼리 오류보다 401/404 가 먼저
    if g is not None:
        return g
    try:
        res = await fn(m.FV_TENANT)
    except LayaError as e:
        return m._fv_err(e.code, e.msg)
    return m._fv_json(request, res)


async def _key_call(request: Request, fn):
    tenant = _m()._require_api_key(request)
    try:
        res = await fn(tenant)
    except LayaError as e:
        raise HTTPException(status_code=e.code, detail=e.msg)
    return JSONResponse(res, headers={"Cache-Control": "no-store"})


async def _fv_queue(t, limit):
    return await queue_core(t, _limit(limit, 30, 100))


@router.get("/api/fv/laya/queue")
async def fv_laya_queue(request: Request, limit: str = "30"):
    return await _fv_call(request, lambda t: _fv_queue(t, limit))


async def _fv_answer(t, request):
    return await answer_core(t, await _body(request))


@router.post("/api/fv/laya/answer")
async def fv_laya_answer(request: Request):
    return await _fv_call(request, lambda t: _fv_answer(t, request))


async def _fv_skip(t, request):
    return await skip_core(t, await _body(request))


@router.post("/api/fv/laya/skip")
async def fv_laya_skip(request: Request):
    return await _fv_call(request, lambda t: _fv_skip(t, request))


async def _fv_undo(t, request):
    await _body(request)          # 본문은 {} — 형식·크기만 검사
    return await undo_core(t)


@router.post("/api/fv/laya/undo")
async def fv_laya_undo(request: Request):
    return await _fv_call(request, lambda t: _fv_undo(t, request))


@router.get("/api/fv/laya/stats")
async def fv_laya_stats(request: Request):
    return await _fv_call(request, stats_core)


async def _k_labels(t, since, limit):
    return await labels_core(t, _since(since), _limit(limit, 1000, LAYA_PAGE_MAX))


@router.get("/laya/labels")
async def laya_labels(request: Request, since: str = "0", limit: str = "1000"):
    return await _key_call(request, lambda t: _k_labels(t, since, limit))


async def _k_items(t, since, status, limit):
    return await items_core(t, _since(since), status, _limit(limit, 1000, LAYA_PAGE_MAX))


@router.get("/laya/items")
async def laya_items(request: Request, since: str = "0", status: str = "pending", limit: str = "1000"):
    return await _key_call(request, lambda t: _k_items(t, since, status, limit))


async def _k_pred(t, request):
    return await pred_core(t, await _body(request, LAYA_PRED_BODY_CAP))


@router.post("/laya/pred")
async def laya_pred(request: Request):
    return await _key_call(request, lambda t: _k_pred(t, request))
