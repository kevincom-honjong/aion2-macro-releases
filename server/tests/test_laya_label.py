# -*- coding: utf-8 -*-
"""[대시보드] 라야 라벨 (2026-10-11 주인님 «라야 학습 객관식, 없으면 주관식») — 서버 laya_label.py.

  M  마스킹(이메일·아이디) · 템플릿 키
  S  씨앗: 키당 3건 상한 · /telegram/send 후크(마스킹 후 저장, 은퇴 id 제외)
  Q  /api/fv/laya/queue 순서(라야 확신 낮은 것 → 예측 없음 → 건너뛴 것) · choices · 인증
  A  answer(보기·other 텍스트 필수·고치기·needs_human 만 고치기) · skip · undo 스택
  T  stats(일치율 스냅샷·새 보기 후보) · /laya/labels 증분·deleted · /laya/pred · /laya/items · 테넌트 격리

    cd updater/server && python -X utf8 tests/test_laya_label.py
"""
import asyncio
import json
import os
import shutil
import tempfile

import concurrent.futures
import urllib.parse

from fastapi import HTTPException

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402
import laya_label as LL                                    # noqa: E402

MIN_CHECKS = 60
KEY = "testkey"
TOK = "fvsecret-laya"
HERE = os.path.dirname(os.path.abspath(__file__))


class _Store:
    """DB 를 잠깐 새것으로(다른 시험과 안 섞이게)."""

    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix="laya_")
        self.saved = (db.DB_PATH, main.FV_TOKEN, main.FV_TENANT)
        db.DB_PATH = os.path.join(self.d, "s.db")
        main.FV_TOKEN, main.FV_TENANT = TOK, "main"
        return self

    def __exit__(self, *e):
        db.DB_PATH, main.FV_TOKEN, main.FV_TENANT = self.saved
        shutil.rmtree(self.d, ignore_errors=True)


_ROUTES = {
    ("get", "/api/fv/laya/queue"): LL.fv_laya_queue, ("post", "/api/fv/laya/answer"): LL.fv_laya_answer,
    ("post", "/api/fv/laya/skip"): LL.fv_laya_skip, ("post", "/api/fv/laya/undo"): LL.fv_laya_undo,
    ("get", "/api/fv/laya/stats"): LL.fv_laya_stats, ("get", "/laya/labels"): LL.laya_labels,
    ("get", "/laya/items"): LL.laya_items, ("post", "/laya/pred"): LL.laya_pred,
}


class _Resp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data
        self.text = json.dumps(data, ensure_ascii=False)

    def json(self):
        return self._d


def _call(method, url, headers, json_body=None, data=None):
    """★TestClient 를 안 쓴다★ — 이 개발컴은 Crawl4AI 설치로 httpx 0.28 이 들어와 starlette 0.36 의 TestClient 가 못 뜬다
    (다른 TestClient 시험도 같다). 핸들러를 진짜로 부른다 — 라우트 등록은 D-2 가 따로 본다. 별도 스레드·새 루프."""
    path, _, qs = url.partition("?")
    q = {k: v[-1] for k, v in urllib.parse.parse_qs(qs).items()}
    fn = _ROUTES[(method, path)]
    body = data if data is not None else json_body
    req = Req(body, api_key=None, headers=headers)

    async def go():
        try:
            r = await fn(req, **q)
        except HTTPException as e:
            return _Resp(e.status_code, {"detail": e.detail})
        return _Resp(r.status_code, json.loads(bytes(r.body)))
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(lambda: asyncio.run(go())).result()


def FV(method, url, **kw):
    return _call(method, url, {"X-FV-Token": TOK}, kw.get("json"), kw.get("data"))


def KX(method, url, **kw):
    return _call(method, url, {"X-Api-Key": KEY}, kw.get("json"), kw.get("data"))


def NOAUTH(method, url, headers=None):
    return _call(method, url, headers or {})


async def seed(text, pc="PC-01", tenant="main"):
    return await LL.seed_alarm(tenant, pc, text)


def ids(resp):
    return [i["id"] for i in resp.json()["items"]]


# ── M ───────────────────────────────────────────────────────────────────────
async def t_mask():
    m = LL.mask_text
    ok("M-1 이메일을 가린다", m("로그인 실패 kevin.kim+x@gmail.com 확인") == "로그인 실패 <email> 확인", m("로그인 실패 kevin.kim+x@gmail.com 확인"))
    ok("M-2 «아이디: 값» · «id=값» 의 값을 가린다(필드 이름은 남는다)",
       m("아이디: abc123 로그인") == "아이디: <id> 로그인" and m("ID=zzz_9 실패") == "ID=<id> 실패", m("아이디: abc123 로그인"))
    ok("M-3 따옴표 안의 «글자+숫자 섞인 4자↑» 토큰만 가린다 — 계정 'a' 는 그대로",
       m("계정 'kevin1234' 전환 실패") == "계정 '<id>' 전환 실패" and m("계정 'a' 전환 실패") == "계정 'a' 전환 실패", m("계정 'kevin1234' 전환 실패"))
    ok("M-4 9자리↑ 숫자·전화번호를 가린다(키나 쉼표 숫자는 그대로)",
       m("전화 010-1234-5678 / 1234567890") == "전화 <num> / <num>" and m("키나 1,234,567") == "키나 1,234,567", m("전화 010-1234-5678 / 1234567890"))
    ok("M-5 줄바꿈·제어문자는 빈칸, 400자로 자른다", m("a\r\nb\x00c") == "a b c" and len(m("가" * 900)) == LL.LAYA_TEXT_MAX)
    t1 = LL.make_tpl("PC-07 | 캐릭 3/12 완료 12:30:45 — 1,234 키나")
    t2 = LL.make_tpl("PC-21b | 캐릭 5/12 완료 03:04 — 99 키나")
    ok("M-6 템플릿 키: 숫자·PC 번호·시각을 지워 같은 알람을 한 키로", t1 == t2, "%r %r" % (t1, t2))
    ok("M-7 다른 문구는 다른 키", LL.make_tpl("🚨 사망") != LL.make_tpl("🚨 캡차"))


# ── S ───────────────────────────────────────────────────────────────────────
async def t_seed():
    with _Store():
        r = [await seed("⚠️ 계정 전환(%d) 실패" % i) for i in range(5)]
        ok("S-1 ★같은 템플릿은 3건까지만★", r == ["added", "added", "added", "tpl_full", "tpl_full"], str(r))
        ok("S-2 다른 템플릿은 따로 센다", await seed("🚨 사망 알림") == "added")
        ok("S-3 마스킹 뒤 빈 문구는 안 쌓는다", await seed("   \r\n ") == "empty")
        q = FV("get", "/api/fv/laya/queue").json()
        ok("S-4 대기 4건(3 + 1)", q["pending"] == 4 and len(q["items"]) == 4, str(q["pending"]))
        # 후크
        main.TELEGRAM_BOT_TOKEN = "T"
        main.TENANTS.setdefault("main", {})["chat_id"] = "12345"
        main._TG_MUTE.clear()
        txt = "⚠️ 로그인 실패 who@example.com 계정 'abc12345' 확인 요망"
        r = await main.telegram_send("PC-77", Req({"text": txt}))
        if LL._BG:
            await asyncio.gather(*list(LL._BG), return_exceptions=True)
        got = [i for i in FV("get", "/api/fv/laya/queue?limit=100").json()["items"] if i["pc"] == "PC-77"]
        ok("S-5 ★/telegram/send 가 알람을 씨앗으로 쌓는다(마스킹 후·원문 이메일 없음)★",
           json.loads(bytes(r.body)).get("ok") is True and len(got) == 1
           and "who@example.com" not in got[0]["text"] and "<email>" in got[0]["text"] and "abc12345" not in got[0]["text"], str(got))
        ok("S-5b 항목 모양: kind·pc·t·tpl·laya null·answer null",
           got and got[0]["kind"] == "alarm" and got[0]["laya"] is None and got[0]["answer"] is None
           and isinstance(got[0]["t"], float) and got[0]["tpl"], str(got))
        n_before = FV("get", "/api/fv/laya/queue?limit=100").json()["pending"]
        main.RETIRED_PCS.add(main.ns("main", "PC-88"))
        try:
            await main.telegram_send("PC-88", Req({"text": "🚨 은퇴한 PC 알람 하드", "hard": True}))
            if LL._BG:
                await asyncio.gather(*list(LL._BG), return_exceptions=True)
        finally:
            main.RETIRED_PCS.discard(main.ns("main", "PC-88"))
        ok("S-6 은퇴 id 의 알람은 씨앗이 아니다", FV("get", "/api/fv/laya/queue?limit=100").json()["pending"] == n_before)
        main.TELEGRAM_BOT_TOKEN = ""
        cap, LL.LAYA_ITEMS_MAX = LL.LAYA_ITEMS_MAX, n_before
        try:
            ok("S-7 테넌트 상한이면 새 항목을 버린다", await seed("새로운 모양 알람 zzz") == "cap")
        finally:
            LL.LAYA_ITEMS_MAX = cap


# ── Q ───────────────────────────────────────────────────────────────────────
async def t_queue_auth():
    with _Store():
        await seed("알람 가")
        r = NOAUTH("get", "/api/fv/laya/queue")
        ok("Q-1 토큰 없으면 401", r.status_code == 401 and r.json().get("ok") is False, "%s %s" % (r.status_code, r.text[:80]))
        r = NOAUTH("get", "/api/fv/laya/queue", {"X-FV-Token": "wrong"})
        ok("Q-2 틀린 토큰 401(에러 모양 = _fv_err)", r.status_code == 401 and r.json().get("code") == 401 and "err" in r.json())
        r = NOAUTH("get", "/laya/labels")
        ok("Q-3 API 키 없으면 403(개발컴 경로)", r.status_code == 403, str(r.status_code))
        ok("Q-4 limit 가 정수가 아니면 400 · 범위 밖은 자른다",
           FV("get", "/api/fv/laya/queue?limit=abc").status_code == 400 and FV("get", "/api/fv/laya/queue?limit=0").status_code == 200)
        q = FV("get", "/api/fv/laya/queue").json()
        cat = q["choices"]["category"]
        ok("Q-5 choices: 보기 8 + other(free) · needs_human 예/아니오",
           [c["key"] for c in cat[:-1]] == LL.CATEGORY_KEYS and cat[-1]["key"] == "other" and cat[-1].get("free") is True
           and [c["value"] for c in q["choices"]["needs_human"]] == [True, False] and all(c["ko"] and c["desc"] for c in cat), str(cat)[:200])
        ok("Q-6 now · pending", isinstance(q["now"], float) and q["pending"] == 1)


async def t_order():
    with _Store():
        for i, w in enumerate(["가나", "다라", "마바", "사아", "자차"]):
            await seed("서로 다른 알람 %s" % w)
        base = FV("get", "/api/fv/laya/queue").json()["items"]
        a, b, c, d, e = [i["id"] for i in base]
        up = KX("post", "/laya/pred", json={"ver": "v1", "items": [
            {"id": b, "category": {"pick": "hunt_stopped", "conf": 0.9}},
            {"id": c, "category": {"pick": "hunt_stopped", "conf": 0.2}},
            {"id": d, "category": {"pick": "routine_progress", "conf": 0.5}}]})
        ok("Q-7 예측 올리기 200", up.status_code == 200 and up.json()["updated"] == 3, up.text)
        order = ids(FV("get", "/api/fv/laya/queue"))
        ok("Q-8 ★라야 확신 낮은 것 먼저(c 0.2 → d 0.5 → b 0.9) → 예측 없는 것(오래된 순 a, e)★", order == [c, d, b, a, e], str(order))
        ok("Q-8b 건너뛴 것은 맨 뒤", FV("post", "/api/fv/laya/skip", json={"id": c}).status_code == 200
           and ids(FV("get", "/api/fv/laya/queue")) == [d, b, a, e, c], str(ids(FV("get", "/api/fv/laya/queue"))))
        ok("Q-8c limit 로 자른다", len(ids(FV("get", "/api/fv/laya/queue?limit=2"))) == 2)


# ── A ───────────────────────────────────────────────────────────────────────
async def t_answer():
    with _Store():
        await seed("알람 하나")
        await seed("알람 둘 서로 다른")
        i1, i2 = ids(FV("get", "/api/fv/laya/queue"))
        r = FV("post", "/api/fv/laya/answer", json={"id": i1, "category": "hunt_stopped"})
        b = r.json()
        ok("A-1 보기 하나로 답한다 → answer·pending 1", r.status_code == 200 and b["ok"] is True and b["answer"]["category"] == "hunt_stopped"
           and b["answer"]["text"] is None and b["answer"]["needs_human"] is None and b["prev"] is None and b["pending"] == 1, r.text)
        r = FV("post", "/api/fv/laya/answer", json={"id": i2, "category": "other"})
        ok("A-2 ★other 인데 text 없으면 400★", r.status_code == 400 and r.json()["ok"] is False, r.text)
        ok("A-2b other + 공백뿐인 text 도 400", FV("post", "/api/fv/laya/answer", json={"id": i2, "category": "other", "text": "  \n "}).status_code == 400)
        ok("A-2c other 201자는 400", FV("post", "/api/fv/laya/answer", json={"id": i2, "category": "other", "text": "가" * 201}).status_code == 400)
        r = FV("post", "/api/fv/laya/answer", json={"id": i2, "category": "other", "text": " 우리가 모르는  새 종류 "})
        ok("A-3 other + text → 저장(공백 정리)·needs_human 같이", r.status_code == 200 and r.json()["answer"]["text"] == "우리가 모르는 새 종류"
           and r.json()["answer"]["category"] == "other", r.text)
        ok("A-4 알 수 없는 보기 400 · 없는 id 404 · id 없음 400 · category 없음(대기 항목) 400",
           FV("post", "/api/fv/laya/answer", json={"id": i1, "category": "zzz"}).status_code == 400
           and FV("post", "/api/fv/laya/answer", json={"id": 99999, "category": "hunt_stopped"}).status_code == 404
           and FV("post", "/api/fv/laya/answer", json={"category": "hunt_stopped"}).status_code == 400
           and FV("post", "/api/fv/laya/answer", json={"id": i1}).status_code == 400)
        r = FV("post", "/api/fv/laya/answer", json={"id": i1, "category": "routine_progress", "needs_human": True})
        b = r.json()
        ok("A-5 ★고치기 = 같은 id 다시 보내기(prev 에 이전 답)★", r.status_code == 200 and b["answer"]["category"] == "routine_progress"
           and b["prev"]["category"] == "hunt_stopped" and b["answer"]["needs_human"] is True, r.text)
        r = FV("post", "/api/fv/laya/answer", json={"id": i1, "needs_human": False})
        ok("A-6 답 달린 항목은 needs_human 만 고칠 수 있다(보기는 그대로)", r.status_code == 200 and r.json()["answer"]["category"] == "routine_progress"
           and r.json()["answer"]["needs_human"] is False, r.text)
        r = FV("post", "/api/fv/laya/answer", json={"id": i1, "category": "hunt_stopped"})
        ok("A-6b needs_human 을 생략하면 이전 값 유지", r.json()["answer"]["needs_human"] is False, r.text)
        ok("A-6c needs_human 이 true/false 가 아니면 400", FV("post", "/api/fv/laya/answer", json={"id": i1, "needs_human": "아마"}).status_code == 400)
        ok("A-7 본문이 JSON 객체가 아니면 400", FV("post", "/api/fv/laya/answer", data="nope").status_code == 400)


async def t_skip_undo():
    with _Store():
        for w in ("가", "나", "다"):
            await seed("서로 다른 알람 %s%s" % (w, w * 3))
        a, b, c = ids(FV("get", "/api/fv/laya/queue"))
        ok("U-1 되돌릴 게 없으면 404", FV("post", "/api/fv/laya/undo", json={}).status_code == 404)
        ok("U-2 skip → 맨 뒤", FV("post", "/api/fv/laya/skip", json={"id": a}).json()["pending"] == 3
           and ids(FV("get", "/api/fv/laya/queue")) == [b, c, a])
        FV("post", "/api/fv/laya/answer", json={"id": b, "category": "hunt_stopped"})
        ok("U-3 답한 항목을 skip 하면 409 · 없는 id 404", FV("post", "/api/fv/laya/skip", json={"id": b}).status_code == 409
           and FV("post", "/api/fv/laya/skip", json={"id": 99999}).status_code == 404)
        r = FV("post", "/api/fv/laya/undo", json={}).json()
        ok("U-4 ★undo = 마지막 답을 되돌린다(대기로 복귀)★", r["ok"] and r["id"] == b and r["action"] == "answer" and r["answer"] is None
           and r["pending"] == 3, str(r))
        r = FV("post", "/api/fv/laya/undo", json={}).json()
        ok("U-5 그다음 undo = 건너뛰기를 되돌린다(a 가 원래 자리 — 맨 앞)", r["id"] == a and r["action"] == "skip"
           and ids(FV("get", "/api/fv/laya/queue")) == [a, b, c], str(r))
        ok("U-6 더 되돌릴 게 없으면 404", FV("post", "/api/fv/laya/undo", json={}).status_code == 404)
        # 고치기 되돌리기
        FV("post", "/api/fv/laya/answer", json={"id": c, "category": "hunt_stopped"})
        FV("post", "/api/fv/laya/answer", json={"id": c, "category": "command_rejected"})
        r = FV("post", "/api/fv/laya/undo", json={}).json()
        ok("U-7 ★고치기를 되돌리면 이전 답으로★", r["answer"]["category"] == "hunt_stopped", str(r))
        ok("U-8 undo 본문이 객체가 아니어도 400(모양 검사)", FV("post", "/api/fv/laya/undo", json=[1]).status_code == 400)


# ── T ───────────────────────────────────────────────────────────────────────
async def t_stats_labels_pred():
    with _Store():
        txts = ["스탯 알람 %s" % ("가나다라마바사아자차카타파하"[i] * 3) for i in range(8)]
        for t in txts:
            await seed(t)
        it = FV("get", "/api/fv/laya/queue?limit=100").json()["items"]
        d = [i["id"] for i in it]
        s0 = FV("get", "/api/fv/laya/stats").json()
        ok("T-1 처음 stats: 대기 8·라벨 0·일치율 null", s0["pending"] == 8 and s0["labeled"] == 0 and s0["agree"]["all"]["rate"] is None
           and s0["agree"]["recent50"]["n"] == 0 and s0["predicted"] == 0 and s0["unpredicted"] == 8, str(s0))
        # 예측: d0 hunt(맞음), d1 hunt(틀림 → routine 답), d2 예측 없음
        KX("post", "/laya/pred", json={"ver": "v1", "items": [
            {"id": d[0], "category": {"pick": "hunt_stopped", "conf": 0.8}, "needs_human": {"pick": True, "conf": 0.7}},
            {"id": d[1], "category": {"pick": "hunt_stopped", "conf": 0.6}}]})
        FV("post", "/api/fv/laya/answer", json={"id": d[0], "category": "hunt_stopped", "needs_human": True})
        FV("post", "/api/fv/laya/answer", json={"id": d[1], "category": "routine_progress"})
        FV("post", "/api/fv/laya/answer", json={"id": d[2], "category": "command_rejected"})
        s = FV("get", "/api/fv/laya/stats").json()
        ok("T-2 ★일치율: 예측 있던 답 2건 중 1건(0.5) — 예측 없던 답은 안 센다★",
           s["agree"]["all"] == {"n": 2, "match": 1, "rate": 0.5} and s["agree"]["recent50"]["n"] == 2, str(s["agree"]))
        ok("T-3 라벨 3 · 오늘 3 · 보기별 · needs_human 집계",
           s["labeled"] == 3 and s["labeled_today"] == 3 and s["by_category"] == {"hunt_stopped": 1, "routine_progress": 1, "command_rejected": 1}
           and s["needs_human"] == {"yes": 1, "no": 0, "unset": 2} and s["pending"] == 5 and s["ver"] == "v1", str(s))
        # 예측을 새로 올려도 답의 스냅샷은 그대로(과거 일치율이 안 바뀐다)
        KX("post", "/laya/pred", json={"ver": "v2", "items": [{"id": d[1], "category": {"pick": "routine_progress", "conf": 0.99}}]})
        s = FV("get", "/api/fv/laya/stats").json()
        ok("T-4 ★모델을 다시 올려도 이미 단 답의 일치 판정은 안 바뀐다(스냅샷)★", s["agree"]["all"]["match"] == 1 and s["ver"] == "v2", str(s["agree"]))
        # 새 보기 후보
        for k in range(3):
            await seed("후보 알람 %s번째 종류가 달라야 해요 %s" % (k, "ㄱㄴㄷ"[k] * 5))
        its = [i for i in FV("get", "/api/fv/laya/queue?limit=100").json()["items"] if i["text"].startswith("후보")]
        for i, w in zip(its, ["새 보기", "새  보기", "다른 글"]):
            FV("post", "/api/fv/laya/answer", json={"id": i["id"], "category": "other", "text": w})
        s = FV("get", "/api/fv/laya/stats").json()
        ok("T-5 주관식 «새 보기 후보» 는 같은 글(공백 정규화) 3건 이상만 — 2건이면 안 뜬다", s["other_clusters"] == [], str(s["other_clusters"]))
        extra = await seed("후보 알람 마지막 종류가 또 달라야 해요 ㅋㅋㅋ")
        last = [i for i in FV("get", "/api/fv/laya/queue?limit=100").json()["items"] if i["text"].startswith("후보 알람 마지막")][0]
        FV("post", "/api/fv/laya/answer", json={"id": last["id"], "category": "other", "text": "새 보기"})
        s = FV("get", "/api/fv/laya/stats").json()
        ok("T-6 3건이 되면 떠오른다", s["other_clusters"] == [{"text": "새 보기", "count": 3}], str(s["other_clusters"]))
        # labels 증분
        L = KX("get", "/laya/labels").json()
        ok("T-7 /laya/labels: 답 달린 항목 전부 · category·other_text·needs_human·lts",
           len(L["labels"]) == s["labeled"] and all(x["lts"] > 0 and x["category"] for x in L["labels"]) and L["more"] is False, str(L)[:300])
        row = [x for x in L["labels"] if x["id"] == d[0]][0]
        ok("T-7b 행 모양", row["category"] == "hunt_stopped" and row["needs_human"] is True and row["other_text"] is None and row["text"] == txts[0]
           and row["pc"] == "PC-01" and row["tpl"], str(row))
        cur = L["cursor"]
        ok("T-8 since=cursor 면 새 것이 없다", KX("get", "/laya/labels?since=%r" % cur).json()["labels"] == [])
        FV("post", "/api/fv/laya/answer", json={"id": d[3], "category": "ui_unreachable"})
        L2 = KX("get", "/laya/labels?since=%r" % cur).json()
        ok("T-9 증분: 새로 단 답만", [x["id"] for x in L2["labels"]] == [d[3]] and L2["cursor"] > cur, str(L2)[:200])
        FV("post", "/api/fv/laya/undo", json={})
        L3 = KX("get", "/laya/labels?since=%r" % L2["cursor"]).json()
        ok("T-10 ★되돌려 사라진 답은 deleted:true 로 한 번 나온다★",
           len(L3["labels"]) == 1 and L3["labels"][0]["id"] == d[3] and L3["labels"][0]["deleted"] is True
           and L3["labels"][0]["category"] is None, str(L3))
        ok("T-10b 한 번도 답 안 한 항목은 labels 에 안 나온다", d[7] not in [x["id"] for x in KX("get", "/laya/labels").json()["labels"]])
        ok("T-11 limit·more", len(KX("get", "/laya/labels?limit=2").json()["labels"]) == 2 and KX("get", "/laya/labels?limit=2").json()["more"] is True)
        ok("T-11b since 가 숫자가 아니면 400", KX("get", "/laya/labels?since=abc").status_code == 400)
        # items
        P = KX("get", "/laya/items").json()
        ok("T-12 /laya/items 기본 = 대기만 · laya 예측 포함", d[0] not in [x["id"] for x in P["items"]] and all(x["answer"] is None for x in P["items"])
           and len(P["items"]) == s["pending"], str(len(P["items"])))
        A = KX("get", "/laya/items?status=all").json()
        ok("T-12b status=all 은 답한 것도", d[0] in [x["id"] for x in A["items"]] and KX("get", "/laya/items?status=zz").status_code == 400)
        # pred 검증
        r = KX("post", "/laya/pred", json={"ver": "v3", "items": [
            {"id": d[4], "category": {"pick": "nope", "conf": 0.5}},
            {"id": d[5], "category": {"pick": "hunt_stopped", "conf": 1.5}},
            {"id": d[6], "category": {"pick": "other", "conf": "0.5"}},
            {"id": 987654, "category": {"pick": "hunt_stopped", "conf": 0.5}},
            {"id": d[7], "category": {"pick": "hunt_stopped", "conf": 0.5}, "needs_human": {"pick": "아마", "conf": 0.5}},
            {"id": d[7], "category": {"pick": "other", "conf": 0.4}, "needs_human": {"pick": False, "conf": 0.4}}]}).json()
        ok("T-13 ★pred 검증: 틀린 보기·범위 밖 conf·문자열 conf·틀린 needs_human 은 그 항목만 invalid, 모르는 id 는 unknown★",
           r["updated"] == 1 and r["unknown"] == [987654] and sorted(r["invalid"]) == sorted([d[4], d[5], d[6], d[7]]), str(r))
        ok("T-13b ver 없으면 400 · items 가 목록 아니면 400 · 2001개 400",
           KX("post", "/laya/pred", json={"items": []}).status_code == 400 and KX("post", "/laya/pred", json={"ver": "x", "items": {}}).status_code == 400
           and KX("post", "/laya/pred", json={"ver": "x", "items": [{"id": 1}] * 2001}).status_code == 400)
        q = FV("get", "/api/fv/laya/queue?limit=100").json()["items"]
        got = [i for i in q if i["id"] == d[7]][0]
        ok("T-14 큐 항목에 laya 예측이 실린다(category·needs_human·ver)",
           got["laya"]["category"] == {"pick": "other", "conf": 0.4} and got["laya"]["needs_human"] == {"pick": False, "conf": 0.4}
           and got["laya"]["ver"] == "v3", str(got))


async def t_isolation():
    with _Store():
        await seed("메인 알람", tenant="main")
        await seed("남의 알람", tenant="other")
        mine = FV("get", "/api/fv/laya/queue").json()
        other_id = [i for i in range(1, 20) if FV("post", "/api/fv/laya/answer", json={"id": i, "category": "hunt_stopped"}).status_code == 200]
        ok("I-1 ★다른 테넌트 항목은 안 보이고 답도 못 단다(404)★", [i["text"] for i in mine["items"]] == ["메인 알람"] and len(other_id) == 1, str(other_id))
        ok("I-2 개발컴 labels/items 도 자기 테넌트만", [x["text"] for x in KX("get", "/laya/items?status=all").json()["items"]] == ["메인 알람"])
        ok("I-3 stats 도 자기 테넌트만", FV("get", "/api/fv/laya/stats").json()["pending"] + FV("get", "/api/fv/laya/stats").json()["labeled"] == 1)


async def t_contract_doc():
    p = os.path.normpath(os.path.join(HERE, "..", "..", "FV_API.md"))
    q = os.path.normpath(os.path.join(HERE, "..", "..", "..", "farmview", "docs", "FV_API.md"))
    s = open(p, encoding="utf-8").read()
    need = ["/api/fv/laya/queue", "/api/fv/laya/answer", "/api/fv/laya/skip", "/api/fv/laya/undo", "/api/fv/laya/stats",
            "/laya/labels", "/laya/items", "/laya/pred", "E. 라야 라벨"]
    ok("D-1 FV_API.md 절 E 가 모든 경로를 적는다", all(n in s for n in need), str([n for n in need if n not in s]))
    routes = {r.path for r in main.app.routes}
    paths = ["/api/fv/laya/queue", "/api/fv/laya/answer", "/api/fv/laya/skip", "/api/fv/laya/undo", "/api/fv/laya/stats",
             "/laya/labels", "/laya/items", "/laya/pred"]
    ok("D-2 적힌 경로가 전부 실제 라우트다", all(x in routes for x in paths), str([x for x in paths if x not in routes]))
    try:
        same = s == open(q, encoding="utf-8").read()
    except OSError:
        same = True      # 팜뷰 사본이 없는 체크아웃(대시보드 단독) — verify_all 이 둘을 diff 한다
    ok("D-3 팜뷰 사본과 동일", same)


def test_all():
    run_all([t_mask, t_seed, t_queue_auth, t_order, t_answer, t_skip_undo, t_stats_labels_pred, t_isolation, t_contract_doc])
    finish("test_laya_label", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
