# -*- coding: utf-8 -*-
"""[대시보드] #276 (2026-09-27 주인님) — 운영정책 이용 제한 계정 슬롯을 「정지(OUT)」로 빼고 순환에 절대 안 넣는다.

  N  슬롯 이름 읽기(PC-01#2 = PC-01b, 계정1 = 접미사 없음)
  S  부팅 복원: 설정이 한 번도 안 쓰였으면 #276 대상 10슬롯을 심는다 · 한 번 쓰이면 설정이 정본(빈 값이면 다시 안 심음)
  C  카드: 정지 슬롯 카드는 계정 칸을 비우고 status=no_account + banned · 같은 PC 형제 카드엔 banned_slots
  R  순환(완주·작업)은 정지 슬롯을 안 고른다 · 순환 송신(_rot_send)도 한 번 더 거부
  G  사람 길(_dispatch_macro_command = 대시보드·FV·ops): 전환 3종은 409 · acct_tour 는 정지 슬롯을 빼고 보낸다
  E  관리 엔드포인트(세션) · 매크로가 묻는 곳(X-Api-Key) · /rotate 에 정지 목록
  J  화면: 정지 카드는 「정지(OUT)」(cardCfg) · 계정 탭에 OUT

    cd updater/server && python -X utf8 tests/test_banned_accts.py
"""
import json
import os
import shutil
import subprocess
import tempfile

from fastapi.testclient import TestClient

from _harness import main, db, ok, run_all, finish, HTTPException   # noqa: E402

MIN_CHECKS = 61
C = TestClient(main.app, raise_server_exceptions=False)
M = main
IDS = {"1": "id1", "2": "id2", "3": "id3"}
NAMES = {"1": ["x1"], "2": ["x2"], "3": ["x3"]}


def _sess(method, path, **kw):
    C.cookies.set("session", main.new_session("main"))
    try:
        return C.request(method, path, **kw)
    finally:
        C.cookies.clear()


class _Keep:
    """BANNED_ACCTS 를 시험 앞뒤로 보존."""
    def __enter__(self):
        self.o = set(M.BANNED_ACCTS)
        M.BANNED_ACCTS.clear()
        return self

    def __exit__(self, *a):
        M.BANNED_ACCTS.clear()
        M.BANNED_ACCTS.update(self.o)


async def t_names():
    f = M._ban_card_id
    ok("N-1 PC-01#2 → PC-01b · PC-04#1 → PC-04 · PC-22#3 → PC-22c", (f("PC-01#2"), f("PC-04#1"), f("PC-22#3"))
       == ("PC-01b", "PC-04", "PC-22c"))
    ok("N-2 카드 id 그대로도 받는다(PC-22c · PC-15)", (f("PC-22c"), f("PC-15")) == ("PC-22c", "PC-15"))
    ok("N-3 못 읽으면 빈 값(#9 범위 밖 · #a · 번호 없는 이름 · 빈 값 · 카드에 없는 PC-1)",
       (f("PC-01#9"), f("PC-01#a"), f("PC-"), f(""), f("PC-01#0"), f("PC-1#2")) == ("", "", "", "", "", ""))
    ok("N-4 #276 대상 10슬롯 = 원문 그대로(PC-01#2 PC-04#1 PC-10#3 PC-12#2 PC-15#1 PC-16#1 PC-17#2 PC-22#1~3)",
       sorted(M.BANNED_SEED_276) == sorted(f(x) for x in ("PC-01#2", "PC-04#1", "PC-10#3", "PC-12#2", "PC-15#1",
                                                           "PC-16#1", "PC-17#2", "PC-22#1", "PC-22#2", "PC-22#3")))
    ok("N-5 전환 목표 번호: chrome_label·label·acct_label(숫자·「계정N」 포함)·acct_no",
       [M._cmd_target_acct(a) for a in ({"chrome_label": "b"}, {"label": "3"}, {"acct_label": "계정2"},
                                        {"acct_no": 4}, {"acct_no": "x"}, {})] == [2, 3, 2, 4, 0, 0])


async def t_seed():
    with _Keep():
        async with db.aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("DELETE FROM settings WHERE key='banned_accts'")
            await c.commit()
        await M._banned_restore()
        v = await db.get_setting("banned_accts")
        ok("S-1 설정이 없으면 #276 10슬롯을 심는다(메모리)", M._banned_list("main") == sorted(M.BANNED_SEED_276),
           str(M._banned_list("main")))
        ok("S-2 심은 값을 저장한다(다음 부팅엔 설정이 정본)", sorted(v.split(",")) == sorted(M.BANNED_SEED_276), str(v))
        M.BANNED_ACCTS.clear()
        await db.set_setting("banned_accts", "")
        await M._banned_restore()
        ok("S-3 ★빈 값으로 저장돼 있으면 다시 심지 않는다★(주인님이 다 풀었을 때)", M._banned_list("main") == [])
        await db.set_setting("banned_accts", "PC-30b,PC-31")
        await M._banned_restore()
        ok("S-4 저장값을 그대로 복원", M._banned_list("main") == ["PC-30b", "PC-31"])
        await db.set_setting("banned_accts", "")


async def t_cards():
    with _Keep():
        await db.upsert_status("PC-40", {"pc_id": "PC-40", "status": "hunting", "character": "주캐",
                                         "acct_id": "main1", "acct_num": 1, "map": "m"})
        await db.upsert_status("PC-40b", {"pc_id": "PC-40b", "status": "idle", "character": "부캐",
                                          "acct_id": "sub2", "acct_num": 2})
        await db.upsert_status("PC-41", {"pc_id": "PC-41", "status": "idle", "character": "c41"})
        M.BANNED_ACCTS.add(M.ns("main", "PC-40"))
        rows = {r["pc_id"]: r for r in await M._build_full_state("main")}
        a, b, o = rows.get("PC-40") or {}, rows.get("PC-40b") or {}, rows.get("PC-41") or {}
        ok("C-1 정지 슬롯(계정1) 카드: status=no_account · banned · 라벨 「정지(OUT)」",
           a.get("status") == "no_account" and a.get("banned") is True and a.get("status_label") == "정지(OUT)", str(a)[:200])
        ok("C-2 정지 카드의 계정 칸(캐릭·아이디·맵)은 비운다", (a.get("character"), a.get("acct_id"), a.get("map")) == ("", "", ""))
        ok("C-3 같은 PC 의 계정2 카드는 값 그대로(부캐 · no_account 아님) + banned_slots=[1]",
           b.get("status") != "no_account" and b.get("character") == "부캐" and b.get("banned_slots") == [1]
           and not b.get("banned"), str(b)[:200])
        ok("C-4 정지와 무관한 PC 는 표식 없음", "banned_slots" not in o and not o.get("banned") and o.get("status") != "no_account",
           str(o)[:200])
        M.BANNED_ACCTS.clear()
        rows = {r["pc_id"]: r for r in await M._build_full_state("main")}
        await db.upsert_status("PC-42", {"pc_id": "PC-42", "status": "idle", "character": "c42", "acct_id": "z"})
        await db.upsert_updater_status("PC-42", {"pc_id": "PC-42", "macro_state": "running", "updater_version": "3.1.13"})
        M.BANNED_ACCTS.add(M.ns("main", "PC-42"))
        rows = {r["pc_id"]: r for r in await M._build_full_state("main")}
        q = rows.get("PC-42") or {}
        ok("C-6 ★매크로가 지금 정지 슬롯에 있으면 status 는 그대로★(명령·순환 탈출) · banned · 계정 칸은 비움",
           q.get("status") not in ("no_account", "offline", None) and q.get("banned") is True and q.get("character") == "",
           str(q)[:200])
        ok("C-7 서버 합계 제외(_fv_pc_excluded)가 정지 슬롯을 뺀다", M._fv_pc_excluded("main", "PC-42") is True
           and M._fv_pc_excluded("main", "PC-41") is False)
        await db.delete_status("PC-42")
        M.BANNED_ACCTS.discard(M.ns("main", "PC-42"))
        rows = {r["pc_id"]: r for r in await M._build_full_state("main")}
        ok("C-5 해제하면 다음 읽기부터 원래 값(주캐 · 표식 없음 · no_account 아님)",
           rows["PC-40"].get("character") == "주캐" and rows["PC-40"].get("status") != "no_account"
           and not rows["PC-40"].get("banned") and "banned_slots" not in rows["PC-40"], str(rows["PC-40"])[:200])
        for p in ("PC-40", "PC-40b", "PC-41"):
            await db.delete_status(p)


def _card(pid, status="idle"):
    return {"pc_id": pid, "status": status, "acct_ids": IDS, "acct_names": NAMES, "daily_progress": []}


async def t_rotation():
    with _Keep():
        cards = [_card("PC-20")]
        ok("R-0 기준: 정지 없으면 계정1 다음은 계정2", M._rot_next_acct(cards, cards[0])[0] == 2)
        M.BANNED_ACCTS.add(M.ns("main", "PC-20b"))
        ok("R-1 완주 순환은 정지 계정2 를 건너뛰고 계정3", M._rot_next_acct(cards, cards[0])[0] == 3)
        ok("R-2 작업 순환도 계정3", M._rot_next_acct_task(cards, cards[0], {"tvisit": []})[0] == 3)
        M.BANNED_ACCTS.add(M.ns("main", "PC-20c"))
        ok("R-3 남은 계정이 전부 정지면 갈 곳 없음(0)", M._rot_next_acct(cards, cards[0])[0] == 0
           and M._rot_next_acct_task(cards, cards[0], {"tvisit": []})[0] == 0)
        ok("R-4 _rot_acct_excluded 가 계정1 정지(카드 id 에 접미사 없음)도 잡는다",
           (M.BANNED_ACCTS.add(M.ns("main", "PC-21")) or M._rot_acct_excluded("main", _card("PC-21b"), 1)) is True)
        before = len(await db.get_pending_commands(M.ns("main", "PC-20")) or [])
        r = await M._rot_send("main", "PC-20", "switch_launcher", {"acct_no": 2, "chrome_label": "b"})
        ok("R-5 순환 송신도 정지 슬롯이면 False(보내지 않는다)", r is False)
        after = len(await db.get_pending_commands(M.ns("main", "PC-20")) or [])
        ok("R-6 거부된 순환 송신은 명령 큐에 행을 안 만든다", before == after, "%s → %s" % (before, after))


async def t_rot_guard():
    said, stopped = [], []
    o_say, o_stop, o_save = M._rot_say, M._rot_stop, M._rot_save

    async def _say(t, pc, text, **kw):
        said.append(text)

    async def _stop(t, base, msg, st=None):
        stopped.append(msg)

    async def _save(force=False):
        pass
    M._rot_say, M._rot_stop, M._rot_save = _say, _stop, _save
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        for stage in ("hunting", "tasking", "switching"):
            stopped.clear()
            st = {"stage": stage, "since": M._rot_now(), "armed_at": M._rot_now() - 9999, "day": M._kst_today_key(),
                  "full": False, "hops": 0, "visits": {}, "sent_at": M._rot_now()}
            if stage == "tasking":
                st["task"] = "corridor"
            key = M.ns("main", "PC-20")
            M._ROT[key] = st
            M._ROT_STEP_CUR.update(key=key, st=st)
            pcs = [{"pc_id": "PC-20", "status": "idle", "last_active": now_iso, "acct_ids": IDS,
                    "acct_names": NAMES, "daily_progress": [], "banned": True}]
            try:
                await M._rot_step_pc("main", "PC-20", st, pcs)
            except Exception as e:                   # noqa: BLE001
                stopped.append("EXC " + str(e))
            finally:
                M._ROT_STEP_CUR.update(key=None, st=None)
                M._ROT.pop(key, None)
            hit = any("정지(OUT)" in m for m in stopped)
            if stage == "switching":
                ok("RG-3 전환 중(switching)엔 가드가 안 끼어든다(정지 카드에서 떠나는 중일 수 있다)", not hit, str(stopped))
            else:
                ok("RG-%s %s 단계에서 매크로가 정지 슬롯에 있으면 ★조용히 멈추지 않고★ 세우고 알린다" %
                   ("1" if stage == "hunting" else "2", stage), hit, str(stopped))
    finally:
        M._rot_say, M._rot_stop, M._rot_save = o_say, o_stop, o_save


async def t_guard():
    with _Keep():
        M.BANNED_ACCTS.update({M.ns("main", "PC-20b"), M.ns("main", "PC-20")})
        g = M._ban_check_cmd
        for i, a in enumerate(({"acct_no": 2}, {"chrome_label": "b"}, {"label": "계정2"}, {"acct_label": "계정1"})):
            ok("G-1.%d 전환 목표가 정지 슬롯이면 거부 %s" % (i, a), bool(g("main", "PC-20c", "switch_launcher", a)[1]))
        ok("G-2 switch_account 도 같은 규칙 · set_account(카드 계정 선언)는 막지 않는다",
           bool(g("main", "PC-20", "switch_account", {"label": "b"})[1])
           and g("main", "PC-20", "set_account", {"label": "a"})[1] == "")
        ok("G-2b ★acct_no 와 라벨이 어긋나도★ 어느 하나라도 정지면 거부(acct_no=2·chrome_label=c / acct_no=3·label=a)",
           bool(g("main", "PC-20", "switch_launcher", {"acct_no": 2, "chrome_label": "c"})[1])
           and bool(g("main", "PC-20", "switch_launcher", {"acct_no": 3, "label": "a"})[1]))
        ok("G-2c 목표 번호 없는 전환(acct_index 만)은 정지 슬롯이 있는 PC 에선 거부 · 없는 PC 에선 통과",
           bool(g("main", "PC-20", "switch_launcher", {"acct_index": 1})[1])
           and g("main", "PC-33", "switch_launcher", {"acct_index": 1})[1] == "")
        a, why = g("main", "PC-20c", "find_host", {"adopt": True})
        ok("G-2d find_host(목록 없음)는 ★지금 계정 먼저★ + 정지 슬롯 뺀 나머지(계정3 에서 → [3,4,5])",
           why == "" and a.get("accounts") == [3, 4, 5] and a.get("adopt") is True, str(a))
        a, why = g("main", "PC-20", "find_host", {})
        ok("G-2e 지금 계정이 정지 슬롯이어도 맨 앞(이미 로그인 = 로그인 0회) · 다른 정지 슬롯은 뺀다([1,3,4,5])",
           why == "" and a.get("accounts") == [1, 3, 4, 5], str(a))
        a, why = g("main", "PC-20c", "find_host", {"accounts": [2, 3]})
        ok("G-2f find_host 에 목록이 오면 그 목록에서만 정지 슬롯을 뺀다([2,3] → [3])", a.get("accounts") == [3], str(a))
        a, why = g("main", "PC-20", "switch_launcher", {"acct_no": 3, "chrome_label": "c"})
        ok("G-3 정지 아닌 계정3 으로는 그대로 간다", why == "" and a == {"acct_no": 3, "chrome_label": "c"})
        a, why = g("main", "PC-20", "acct_tour", {"accounts": [1, 2, 3], "task": "collect"})
        ok("G-4 acct_tour 는 정지 슬롯을 빼고 보낸다([1,2,3] → [3]) · 다른 칸은 그대로",
           why == "" and a == {"accounts": [3], "task": "collect"}, str(a))
        a, why = g("main", "PC-20", "acct_tour", {"task": "collect"})
        ok("G-5 ★accounts 가 없으면(매크로 기본 = 전부) 정지를 뺀 목록을 명시★", why == "" and a["accounts"] == [3, 4, 5], str(a))
        a, why = g("main", "PC-20", "acct_tour", {"accounts": ["b", "a"]})
        ok("G-6 라벨로 온 순회도 걸러 전부 정지면 거부", bool(why))
        ok("G-7 정지 없는 PC·다른 명령은 손대지 않는다", g("main", "PC-33", "switch_launcher", {"acct_no": 2}) == ({"acct_no": 2}, "")
           and g("main", "PC-20", "start", {"x": 1}) == ({"x": 1}, ""))
        try:
            await M._dispatch_macro_command("main", "PC-20c", "switch_launcher", {"acct_no": 2, "chrome_label": "b"})
            code = None
        except HTTPException as e:
            code = e.status_code
        ok("G-8 사람 길(_dispatch_macro_command)은 409 로 거부", code == 409, str(code))
        r = _sess("POST", "/command/PC-20c", json={"command": "switch_launcher", "args": {"acct_no": 1, "chrome_label": "a"}})
        ok("G-9 대시보드 /command 도 409(정지 계정1)", r.status_code == 409, "%s %s" % (r.status_code, r.text[:160]))


async def t_delivery():
    """D  정지 등록 ★전에★ 큐에 든 명령 — 배달(폴링) 때 다시 본다."""
    with _Keep():
        nspc = M.ns("main", "PC-50c")
        i1 = await db.insert_command(nspc, "switch_launcher", {"acct_no": 2, "chrome_label": "b"})
        i2 = await db.insert_command(nspc, "acct_tour", {"accounts": [1, 2, 3], "task": "collect"})
        M.BANNED_ACCTS.add(M.ns("main", "PC-50b"))
        r = C.get("/command/PC-50c", headers={"X-Api-Key": "testkey"})
        j = r.json() if r.status_code == 200 else {}
        ok("D-1 정지 전에 큐에 든 계정2 전환은 배달 안 함 — 다음 명령(acct_tour)이 나온다",
           j.get("id") == i2 and j.get("command") == "acct_tour", str(j)[:200])
        ok("D-2 acct_tour 는 배달 때도 정지 슬롯을 뺀다([1,2,3] → [1,3])", (j.get("args") or {}).get("accounts") == [1, 3],
           str(j.get("args"))[:160])
        async with db.aiosqlite.connect(db.DB_PATH) as c:
            async with c.execute("SELECT status FROM commands WHERE id=?", (i1,)) as cur:
                row = await cur.fetchone()
        ok("D-3 배달 안 한 명령은 큐에서 취소(다시 안 나온다)", row is not None and row[0] != "pending", str(row))
        logs = await db.get_logs(nspc, limit=20)
        ok("D-4 사유는 그 PC 로그에(#276)", any("#276" in str(l.get("message")) and str(i1) in str(l.get("message")) for l in logs),
           str([l.get("message") for l in logs][-2:])[:200])
        src = open(M.__file__, encoding="utf-8").read()
        ok("D-5 WS 재접속 드레인도 같은 배달 문(_drop_undeliverable)을 부른다",
           "if await _drop_undeliverable(tenant, pc_id, _p):" in src and src.count("await _drop_undeliverable(") >= 3)


async def t_endpoints():
    with _Keep():
        r = C.post("/admin/banned_accts", json={"accts": ["PC-30#2"]})
        ok("E-1 세션 없이 등록 불가(401)", r.status_code == 401, str(r.status_code))
        r = _sess("POST", "/admin/banned_accts", json={"accts": ["PC-30#2", "PC-bad#x"]})
        ok("E-2 못 읽는 항목이 하나라도 있으면 400 · 아무것도 안 바뀜", r.status_code == 400 and M._banned_list("main") == [],
           r.text[:120])
        r = _sess("POST", "/admin/banned_accts", json={"accts": ["PC-30#2", "PC-30#1", "PC-31c"]})
        ok("E-3 등록 → 목록", r.status_code == 200 and r.json().get("banned") == ["PC-30", "PC-30b", "PC-31c"], r.text[:160])
        ok("E-4 설정에 저장(서버 재시작에도 남는다)",
           sorted((await db.get_setting("banned_accts") or "").split(",")) == ["PC-30", "PC-30b", "PC-31c"])
        r = C.get("/banned_accts/PC-30b", headers={"X-Api-Key": "testkey"})
        ok("E-5 매크로(X-Api-Key)가 묻는 곳 — 물리 PC 기준 slots·labels",
           r.status_code == 200 and r.json() == {"pc": "PC-30", "slots": [1, 2], "labels": ["a", "b"], "label": "정지(OUT)"},
           r.text[:160])
        ok("E-6 키 없이 묻기는 401", C.get("/banned_accts/PC-30").status_code == 401)
        r = _sess("GET", "/rotate")
        ok("E-7 /rotate 응답에 정지 목록(banned)", r.status_code == 200 and r.json().get("banned") == ["PC-30", "PC-30b", "PC-31c"],
           r.text[:160])
        r = _sess("DELETE", "/admin/banned_accts/PC-30%232")
        ok("E-8 해제(PC-30#2) → 목록에서 빠지고 저장", r.status_code == 200 and r.json().get("banned") == ["PC-30", "PC-31c"]
           and "PC-30b" not in (await db.get_setting("banned_accts") or ""), r.text[:160])
        ok("E-9 /setting/banned_accts POST 는 막힌다(서버 관리 키 — 잘린 값으로 덮지 않게)",
           _sess("POST", "/setting/banned_accts", json={"value": ""}).status_code in (400, 403, 409))
        ok("E-10 목록 조회(세션)", _sess("GET", "/admin/banned_accts").json() == {"banned": ["PC-30", "PC-31c"], "label": "정지(OUT)"})
        await db.set_setting("banned_accts", "")


def _obj(src, head):
    i = src.index(head)
    d, k = 0, src.index("{", i)
    while True:
        c = src[k]
        d += (c == "{") - (c == "}")
        k += 1
        if d == 0:
            return src[i:k] + ";"


def _fn(src, name):
    i = src.index("function " + name + "(")
    d, k = 0, src.index("{", i)
    while True:
        c = src[k]
        d += (c == "{") - (c == "}")
        k += 1
        if d == 0:
            return src[i:k]


async def t_js():
    src = main.HTML_DASHBOARD
    node = shutil.which("node")
    o, err = None, "node 없음"
    if node:
        js = _obj(src, "const STATUS_CFG = {") + "\n" + _fn(src, "cardCfg") + r"""
console.log(JSON.stringify({
  ban: cardCfg({status:'no_account', banned:true}).label,
  noacc: cardCfg({status:'no_account'}).label,
  hunt: cardCfg({status:'hunting'}).label,
  off: cardCfg({}).label, on: cardCfg({status:'no_account', banned:true}).online}));
"""
        d = tempfile.mkdtemp(prefix="banjs_")
        p = os.path.join(d, "t.js")
        open(p, "w", encoding="utf-8").write(js)
        rr = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
        try:
            o = json.loads(rr.stdout.strip().splitlines()[-1])
        except Exception:
            err = rr.stderr[-300:]
    ok("J-1 정지 카드 = 「정지(OUT)」 · 그냥 계정없음은 「계정 없음」 그대로 · 오프라인 취급(online:false)",
       o is not None and o["ban"] == "정지(OUT)" and o["noacc"] == "계정 없음" and o["hunt"] not in ("정지(OUT)", "계정 없음")
       and o["off"] == "오프라인" and o["on"] is False, str(o or err))
    ok("J-2 카드·메뉴 머리가 cardCfg 로 모양을 고른다(두 곳)", src.count("const cfg = cardCfg(pc);") == 1
       and src.count("const cfg=cardCfg(pc);") == 1)
    ok("J-3 계정 탭: 정지 슬롯이면 카드가 없어도 OUT 표시·줄긋기",
       "const out = (s.top.banned_slots||[]).includes(k);" in src and "(out ? ' acct-tab-out' : '')" in src
       and '<i class="tout">OUT</i>' in src and ".acct-tab-out{" in src)


def test_all():
    run_all([t_names, t_seed, t_cards, t_rotation, t_rot_guard, t_guard, t_delivery, t_endpoints, t_js])
    finish("test_banned_accts", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
