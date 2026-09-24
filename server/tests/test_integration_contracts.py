# -*- coding: utf-8 -*-
"""[통합 2026-09-12] 영역 간 연결 지점 — 서버 쪽 시험 (통합 세션이 만든 계약 6건).

각 영역 시험이 다 초록이어도 ★연결★ 이 깨지면 여기가 먼저 빨간불.
  I1 매크로 ack 본문 {status:"cancelled"} → 이력이 cancelled (CONTRACTS_대시보드 #1 · 매크로 report_module.notify_dropped)
  I2 FV silent_s · updater.age_s 는 항상 정수, 모름 = 10**9      (CONTRACTS_팜뷰 · 팜뷰 fvdash.group 대표 카드)
  I3 FV raw 는 ?raw=1 일 때만                                       (CONTRACTS_대시보드 #4 · 팜뷰는 raw 를 버린다)
  I4 FV pc:"all" 은 400 · 8대 이상은 confirm_fleet 필요             (CONTRACTS_대시보드 #3 · 팜뷰 fvdash.send_cmd)
  I5 /parsec/map 쓰기는 세션 로그인으로만(API 키·토큰 401)          (CONTRACTS_대시보드 #2 · 주인님 결정 2026-09-13)
  I6 순환 정보수집 상한이 6캐릭 28.5분(PC-07 실측)을 넘긴다          (SHARED_ISSUES_아이온2 #1)
  I7 팜뷰 ocr.py 가 부르는 /api/fv/ocr/* 가 서버에 전부 있다(메서드까지) (FV_API «밤 › D» · 2026-09-24 #125)
  I8 대시보드 목소리 번호 읽기(ttsText) = 팜뷰 화면 JS = 팜뷰 alarmvoice.speak_text  (#172 후속 · 2026-09-24)
"""
import os
import sys

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 29


async def t_ack_cancelled():
    cid = await db.insert_command("PC-I1", "start", {})
    r = await main.ack_cmd("PC-I1", cid, Req({"status": "cancelled", "why": "부팅 드레인(시험)"}))
    ok("I1 ack 본문 cancelled 를 200 으로 받는다", getattr(r, "status_code", 200) == 200)
    rows = await db.get_recent_commands(50)
    row = next((x for x in rows if x.get("id") == cid), None)
    ok("I1 이력 status 가 cancelled 다", row is not None and row.get("status") == "cancelled", str(row))
    # 본문 없는 옛 ack 는 그대로 acked
    cid2 = await db.insert_command("PC-I1", "stop", {})
    r2 = await main.ack_cmd("PC-I1", cid2, Req({}))
    rows = await db.get_recent_commands(50)
    row2 = next((x for x in rows if x.get("id") == cid2), None)
    ok("I1 본문 없는 ack 는 예전처럼 acked", row2 is not None and row2.get("status") == "acked", str(row2))
    # ★B-CQ2 (2026-09-23) 계약대로 뒤집었다★ — 초판은 「acked 뒤의 cancelled 는 무시」 를 못 박았는데 그게
    #   버그를 잠근 시험이었다. 매크로는 받자마자 ack 하고(report_module on_message) 그 ★뒤에★ 버리거나
    #   거부하면 cancelled/rejected 를 보낸다 — pending 만 받으면 CONTRACTS_대시보드 #1 의 ⛔ 가 한 번도 안 뜬다.
    #   이제: 매크로 통지는 acked 에서도 cancelled 로 · 대시보드 ✕(cancel_cmd) 는 여전히 pending 만.
    r3 = await main.ack_cmd("PC-I1", cid2, Req({"status": "cancelled", "why": "받은 뒤 버림(시험)"}))
    rows = await db.get_recent_commands(50)
    row3 = next((x for x in rows if x.get("id") == cid2), None)
    ok("I1 acked 뒤 매크로의 cancelled 통지는 cancelled 로 남는다(계약 #1)",
       row3 is not None and row3.get("status") == "cancelled", str(row3))
    cid3 = await db.insert_command("PC-I1", "start", {})
    await main.ack_cmd("PC-I1", cid3, Req({}))
    await main.cancel_cmd(cid3, Req(api_key=None, session=main.new_session("main")))
    rows = await db.get_recent_commands(50)
    row4 = next((x for x in rows if x.get("id") == cid3), None)
    ok("I1 대시보드 ✕ 취소는 acked 를 못 뒤집는다(pending 일 때만)",
       row4 is not None and row4.get("status") == "acked", str(row4))


def t_fv_ints():
    v = main._fv_pc_view({"pc_id": "PC-I2", "status": "idle", "_macro_silent_s": -1, "daily_progress": []})
    ok("I2 silent_s 가 -1 이면 10**9 로 나간다", v["silent_s"] == main.FV_UNKNOWN_S, str(v["silent_s"]))
    ok("I2 updater.age_s 가 없으면 10**9", v["updater"]["age_s"] == main.FV_UNKNOWN_S, str(v["updater"]))
    v2 = main._fv_pc_view({"pc_id": "PC-I2", "status": "idle", "_macro_silent_s": 7, "_updater_age_s": 3, "daily_progress": []})
    ok("I2 정상 값은 그대로 정수", v2["silent_s"] == 7 and v2["updater"]["age_s"] == 3)
    ok("I2 둘 다 int 타입", isinstance(v["silent_s"], int) and isinstance(v2["updater"]["age_s"], int))


def t_fv_raw():
    body = {"pcs": {"PC-A": {"pc_id": "PC-A", "raw": {"secret": 1}, "status": "idle"}}, "global": {}}
    r = Req(api_key=None)
    stripped = main._fv_maybe_raw(r, body)
    ok("I3 기본은 raw 가 빠진다", "raw" not in stripped["pcs"]["PC-A"], str(stripped["pcs"]["PC-A"].keys()))
    ok("I3 원본(캐시)은 안 건드린다", "raw" in body["pcs"]["PC-A"])
    r.query_params = {"raw": "1"}
    kept = main._fv_maybe_raw(r, body)
    ok("I3 ?raw=1 이면 그대로", "raw" in kept["pcs"]["PC-A"])


async def t_fv_all():
    main.FV_TOKEN = "fvsecret"
    H = {"X-FV-Token": "fvsecret"}
    r = await main.fv_command_send(Req({"cmd": "start", "pc": "all"}, api_key=None, headers=H))
    ok("I4 맨몸 pc:'all' 은 400", getattr(r, "status_code", 0) == 400, str(getattr(r, "status_code", None)))
    body = r.body.decode("utf-8", "replace") if hasattr(r, "body") else ""
    ok("I4 400 문구가 confirm_fleet 를 안내한다", "confirm_fleet" in body, body[:120])
    big = ["PC-%02d" % i for i in range(1, 9)]
    r2 = await main.fv_command_send(Req({"cmd": "start", "pc": big}, api_key=None, headers=H))
    b2 = r2.body.decode("utf-8", "replace") if hasattr(r2, "body") else ""
    ok("I4 8대 목록에 confirm_fleet 없으면 400", getattr(r2, "status_code", 0) == 400 and "confirm_fleet" in b2, b2[:120])
    r3 = await main.fv_command_send(Req({"cmd": "start", "pc": ["PC-01"]}, api_key=None, headers=H))
    b3 = r3.body.decode("utf-8", "replace") if hasattr(r3, "body") else ""
    ok("I4 1대는 confirm_fleet 없이도 400 이 아니다(모르는 PC 면 그 사유로 답한다)", "confirm_fleet" not in b3, b3[:120])


async def t_parsec_token():
    """주인님 결정 2026-09-13 — 파섹 주소록 ★쓰기★ 는 세션 로그인으로만. API 키·토큰 헤더로는 401."""
    from fastapi import HTTPException
    try:
        await main.set_parsec_map(Req({"map": {"8": "peer8"}}, api_key="testkey"))
        ok("I5 API 키로는 주소록을 못 덮는다(401)", False, "예외 없이 통과했다")
    except HTTPException as e:
        ok("I5 API 키로는 주소록을 못 덮는다(401)", e.status_code == 401, str(e.status_code))
    try:
        await main.set_parsec_map(Req({"map": {"8": "peer8"}}, api_key=None, headers={"X-Parsec-Token": "x"}))
        ok("I5 토큰 헤더 길은 없다(401)", False, "예외 없이 통과했다")
    except HTTPException as e:
        ok("I5 토큰 헤더 길은 없다(401)", e.status_code == 401, str(e.status_code))
    r = await main.set_parsec_map(Req({"map": {"8": "peer8"}}, api_key=None, session=main.new_session("main")))
    ok("I5 세션 로그인은 통과", getattr(r, "status_code", 200) == 200, str(getattr(r, "status_code", None)))
    ok("I5 서버에 PARSEC_MAP_TOKEN 길이 남아 있지 않다", not hasattr(main, "_parsec_map_token_tenant"))


def t_collect_cap():
    # _rot_collect_max 는 ★카드 목록★ 을 받고 daily_progress 길이로 캐릭 수를 센다
    cards6 = [{"pc_id": "PC-07", "daily_progress": [{"slot": i + 1} for i in range(6)]}]
    cap6 = main._rot_collect_max(cards6) if hasattr(main, "_rot_collect_max") else None
    ok("I6 6캐릭 상한이 PC-07 실측 28.5분(1710초)을 넘긴다", cap6 is not None and cap6 >= 1710, str(cap6))
    ok("I6 상한은 절대 최대(2400)를 넘지 않는다", cap6 is not None and cap6 <= main.ROT_COLLECT_HARD_MAX, str(cap6))


def t_fv_ocr_paths():
    """팜뷰 중계(farmview/ocr.py)의 경로 글자를 그대로 읽어 서버 라우트 표와 맞춘다 — 한쪽이 이름을 바꾸면 여기서 빨간불."""
    import re as _re
    import subprocess as _sp
    here = os.path.dirname(os.path.abspath(__file__))
    fv = os.path.normpath(os.path.join(here, "..", "..", "..", "farmview"))
    src = os.path.join(fv, "ocr.py")
    # ★팜뷰 저장소에 커밋된 ocr.py 만 읽는다 (2026-09-24 아이온2 반증 BLOCKER)★ — 개발컴에만 있는 미커밋 파일에 기대면
    #   깨끗한 체크아웃에서 I7 이 빨갛다(verify_all 종료 1). 추적 안 되면 건너뛰고 ★보이게 적는다★ — 대신 서버 쪽 고정 표를 본다.
    try:
        tracked = _sp.run(["git", "-C", fv, "ls-files", "--error-unmatch", "ocr.py"],
                          capture_output=True, timeout=20).returncode == 0
    except Exception:
        tracked = False
    have = set()
    for r in main.app.routes:
        for meth in getattr(r, "methods", None) or ():
            have.add((meth, getattr(r, "path", "")))
    if not tracked:
        print("  [SKIP] I7 팜뷰 farmview/ocr.py 가 팜뷰 저장소에 커밋돼 있지 않다 — 경로 글자 대조 생략(커밋되면 자동으로 다시 본다)")
        miss = sorted(u for u in _FV_OCR_ROUTES if u not in have)
        ok("I7 (ocr.py 미커밋 — 건너뜀) 팜뷰 중계가 쓰는 /api/fv/ocr 여덟 길이 서버에 있다(고정 표)", not miss, "없음: %s" % miss)
        ok("I7 (ocr.py 미커밋 — 건너뜀) 고정 표가 비지 않았다", len(_FV_OCR_ROUTES) == 8, str(len(_FV_OCR_ROUTES)))
        return
    try:
        txt = open(src, encoding="utf-8").read()
    except OSError:
        txt = ""
    used = set()
    for m in _re.finditer(r"(_get|_post|get|post)\([^\n]*?[\"'](/api/fv/ocr/[a-z_]+)[\"']", txt):
        used.add(("POST" if "post" in m.group(1) else "GET", m.group(2)))
    for m in _re.finditer(r"[\"'](/api/fv/ocr/img/)", txt):
        used.add(("GET", "/api/fv/ocr/img/{img_id}"))
    miss = sorted(u for u in used if u not in have)
    ok("I7 팜뷰 ocr.py 에서 /api/fv/ocr 경로를 읽었다(0개면 파일이 없거나 모양이 바뀐 것)", len(used) >= 7, str(sorted(used)))
    ok("I7 ★그 경로가 서버에 전부 있다(메서드까지)★", not miss, "없음: %s" % miss)


# 팜뷰 ocr.py(개발컴 미커밋 판, 2026-09-24)가 부르는 길 — ocr.py 가 커밋되면 위의 글자 대조가 대신한다
_FV_OCR_ROUTES = {("GET", "/api/fv/ocr/history"), ("GET", "/api/fv/ocr/img/{img_id}"), ("GET", "/api/fv/ocr/queue"),
                  ("GET", "/api/fv/ocr/stats"), ("POST", "/api/fv/ocr/bad"), ("POST", "/api/fv/ocr/label"),
                  ("POST", "/api/fv/ocr/skip"), ("POST", "/api/fv/ocr/undo")}


def _js_fn(src, name):
    i = src.index("function %s(" % name)
    j = src.find("\nfunction ", i + 1)
    k = src.find("\n//", i + 1)
    ends = [x for x in (j, k) if x > 0]
    return src[i:min(ends)].rstrip() if ends else src[i:]


# 아이온2 가 든 보기(#172 후속) + 팜뷰 test_tts_number_172 의 까다로운 것들 — 정답은 팜뷰 파이썬 speak_text 가 정한다
_TTS_KEYS = ["2번", "9번", "PC-22c", "PC-22c가 캡차", "계정2번", "43,000번", "2번호", "PC-09 멈춤", "관제PC-09", "PC_22c가",
             "14번, 어비스 밖으로 나갔습니다", "2번은 두번으로", "3 번개", "12번지", "세 번째", "10번째 시도", "1.5번", "12345번",
             "DESKTOP-O5SSEIK", "PC-MANIA", "2번,3번 멈춤", "1,2번", "PC 2번도", "0번", "1234번", "pc-7 과 PC-10b", "PC__09", "",
             # 전각 숫자 짝(2026-09-24 아이온2 반증 — 파이썬 \d 는 전각도 먹고 JS \d 는 ASCII 만) · 번거/번쩍(«번» 뒤 글자 갈래 미시험)
             "２번", "PC-０９ 멈춤", "１２번째", "3２번", "번거롭게 2번", "2번거롭다", "번쩍 3번", "3번쩍", "5번개", "2번 번거"]
# ★전각 숫자 짝 — 묶었다 (2026-09-25)★ 팜뷰 70e3dbd 가 ttsText 맨 앞에서 전각 ０-９ 를 ASCII 로 바꾸고, 대시보드가 그 줄을 글자 그대로
#   옮겼다. 예전엔 여기 «미해결» 로 빼 두었다(두 JS 가 '１２번째' 를 그대로, 팜뷰 파이썬은 '12번째'). 이제 전부 묶는다 — 다시 채우지 마라.
_TTS_FW_PENDING: set = set()
_TTS_FIXED = {"2번": "이 번", "9번": "구 번", "PC-22c": "이십이 번 씨", "계정2번": "계정 이 번", "43,000번": "43,000번", "2번호": "2번호"}


def t_tts_same_answer():
    """#172 후속 — 대시보드 목소리(main.HTML_DASHBOARD speak/speakLocal)도 번호를 한자어로. 팜뷰 화면 JS·팜뷰 파이썬과 ★같은 답★."""
    import types
    import json
    import re as _re
    import shutil
    import subprocess
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    fvdir = os.path.normpath(os.path.join(here, "..", "..", "..", "farmview"))
    html = main.HTML_DASHBOARD
    d_sino, d_tts = _js_fn(html, "ttsSino"), _js_fn(html, "ttsText")
    # ★팜뷰의 ★커밋된★ 파일과 묶는다 (2026-09-24)★ — 개발컴 작업본(미커밋)에 기대면 깨끗한 체크아웃과 답이 갈린다(I7 과 같은 부류).
    #   git 이 없거나 추적 안 되면 작업본 파일 그대로.
    def _fv_src(rel):
        try:
            # FV_I8_REF=<커밋> 이면 그 팜뷰 커밋과 묶는다(개발컴 팜뷰 main 이 낡았을 때 — 아이온2 합동 반증은 팜뷰 끝 커밋으로 돌린다)
            r = subprocess.run(["git", "-C", fvdir, "show", os.environ.get("FV_I8_REF", "HEAD") + ":" + rel],
                               capture_output=True, timeout=20)
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode("utf-8"), "git " + os.environ.get("FV_I8_REF", "HEAD")
        except Exception:
            pass
        return open(os.path.join(fvdir, *rel.split("/")), encoding="utf-8").read(), "작업본"
    try:
        ui, ui_from = _fv_src("ui/index.html")
        ui = ui.replace("\r\n", "\n")
        f_sino, f_tts = _js_fn(ui, "ttsSino"), _js_fn(ui, "ttsText")
    except (OSError, ValueError):
        f_sino = f_tts = None
        ui_from = "없음"
    ok("I8 대시보드 ttsSino·ttsText = 팜뷰 ui/index.html 의 것과 글자 그대로", (d_sino, d_tts) == (f_sino, f_tts),
       "대시보드 %d/%d자 · 팜뷰 %s" % (len(d_sino), len(d_tts), f_tts and len(f_tts)))
    av_src, av_from = _fv_src("alarmvoice.py")
    print("  [I8] 팜뷰 출처: ui/index.html=%s · alarmvoice.py=%s" % (ui_from, av_from))
    av = types.ModuleType("fv_alarmvoice_i8")
    av.__file__ = os.path.join(fvdir, "alarmvoice.py")
    exec(compile(av_src, av.__file__, "exec"), av.__dict__)
    want = [av.speak_text(k) for k in _TTS_KEYS]
    bad_fixed = {k: av.speak_text(k) for k, v in _TTS_FIXED.items() if av.speak_text(k) != v}
    ok("I8 팜뷰 파이썬 speak_text 가 아이온2 보기대로(2번→이 번 · PC-22c→이십이 번 씨 · 계정2번 띄움 · 43,000번·2번호 그대로)",
       not bad_fixed, str(bad_fixed))
    node = shutil.which("node")
    got = None
    if node:
        dd = tempfile.mkdtemp(prefix="i8tts_")
        p = os.path.join(dd, "t.js")
        open(p, "w", encoding="utf-8").write(d_sino + "\n" + d_tts + "\nconst K=%s;console.log(JSON.stringify(K.map(ttsText).concat([ttsText(null), ttsText(ttsText('PC-22c 2번'))])))"
                                             % json.dumps(_TTS_KEYS, ensure_ascii=False))
        r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8", timeout=60)
        shutil.rmtree(dd, ignore_errors=True)
        try:
            got = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            got = None
    diff = [(k, g, w) for k, g, w in zip(_TTS_KEYS + ["null", "두 번"], got or [], want + ["", "이십이 번 씨 이 번"]) if g != w]
    fw = [d for d in diff if d[0] in _TTS_FW_PENDING]
    diff = [d for d in diff if d[0] not in _TTS_FW_PENDING]
    if got is not None:
        print("  [I8] ★전각 숫자 짝 미해결(팜뷰 원본 몫 — 대시보드는 글자 그대로라 못 고친다)★: %s" % fw if fw else
              "  [I8] 전각 숫자 짝 맞음 — _TTS_FW_PENDING 을 비워 묶어라")
    ok("I8 ★대시보드 JS ttsText 답 = 팜뷰 파이썬 speak_text 답★(%d개 · 두 번 불러도 같다)" % len(_TTS_KEYS),
       got is not None and len(got) == len(_TTS_KEYS) + 2 and not diff, "node=%s %s" % (node, diff[:6]))
    heads = {}
    for name in ("speak", "speakLocal"):
        m = _re.search(r"function %s\([^)]*\)\{\n(.*)" % name, html)
        heads[name] = m.group(1).strip() if m else None
    ok("I8 speak·speakLocal 맨 앞이 text=ttsText(text);", all(v == "text=ttsText(text);" for v in heads.values()), str(heads))
    decl = list(_re.finditer(r"(?<![\w$.])function\s+([A-Za-z_$][\w$]*)\s*\(", html))
    owners = set()
    for m in _re.finditer(r"new SpeechSynthesisUtterance\(|new Audio\(", html):
        owners.add([x.group(1) for x in decl if x.start() < m.start()][-1])
    ok("I8 대시보드에서 말하는 자리(SpeechSynthesisUtterance·Audio)는 speak·speakLocal 안에만", owners == {"speak", "speakLocal"}, str(owners))


run_all([t_ack_cancelled, t_fv_ints, t_fv_raw, t_fv_all, t_parsec_token, t_collect_cap, t_fv_ocr_paths, t_tts_same_answer])
finish("test_integration_contracts", MIN_CHECKS)
