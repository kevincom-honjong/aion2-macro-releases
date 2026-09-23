# -*- coding: utf-8 -*-
"""[대시보드] 보안·견고성 반증 B2 묶음(2026-09-23) 회귀 가드 — 실제 호출·실제 실행.

버그 사냥꾼이 재현한 것들을 다시는 못 무너지게 한다. ★모든 검사는 고치기 전 코드에서 FAIL★ 이었다
(r2base 로 확인). 합격 조건에 「정상 값은 그대로 통과」 를 같이 묶어, 막기만 하는 수정도 잡는다.
  SB1  NaN/Infinity 보고가 /status 500 · WS/FV 본문을 브라우저 JSON.parse 불가로 만듦
  SB2  str 끼리 hmac.compare_digest → 비ASCII 에서 TypeError(500) — 로그인·키·/license·WS·/check
  SB3  AI 계획표 slot 이 innerHTML·onchange 문자열에 그대로(XSS) + 서버가 slot 을 형 검사 안 함
  SB4  /bugs/download Referer 부분문자열 비교(CSRF) · 한글 pc_id 헤더 500 이 ★파일을 지운 뒤★ · 루프 정지
  SB5  본문 크기 상한이 다 읽은 뒤에만 · /check 깨진 JSON 500
  SB9  버그 파일명이 deleteBug('…') 문자열에 들어가 XSS
  SB12 키 없는 대시보드 요청이 키 추측 실패로 세어져 관제컴 IP 가 잠김
  SBL  /characters 회랑 null·문자 · total_kina 문자 · slot 형 섞임 · 음소거 hours · /slot_filter 키
"""
import asyncio
import json
import os
import re
import time
import html as _html

from _harness import main, db, ok, Req, FakeWS, run_all, finish, HTTPException   # noqa: E402
from test_js_b import _grab, _run_node, _DOM, _need_node                          # noqa: E402

MIN_CHECKS = 36     # 2026-09-23 실측 36 — 검사를 더하면 같이 올린다


def S(body=None, **kw):
    return Req(body, api_key=None, session=main.new_session("main"), **kw)


async def _status(coro):
    """핸들러 결과 → 상태코드(HTTPException 포함). 그 밖의 예외는 'EXC:이름'."""
    try:
        r = await coro
    except HTTPException as e:
        return e.status_code
    except Exception as e:
        return "EXC:%s" % type(e).__name__
    return getattr(r, "status_code", 200)


# ───────────────── SB2 — 비ASCII·비문자열 비교 ─────────────────

async def t_sb2_auth():
    main.TENANTS["krt"] = {"password": "지인비번", "api_key": "지인키", "expires": "", "chat_id": ""}
    main.PW_TO_TENANT["지인비번"] = "krt"
    main.KEY_TO_TENANT["지인키"] = "krt"
    main._LOGIN_FAILS.clear()
    main._KEY_FAILS.clear()
    try:
        r_ok = await _status(main.do_login(Req({"password": "harness"}, api_key=None, host="10.1.0.1"), main.Response()))
        r_kr = await _status(main.do_login(Req({"password": "지인비번"}, api_key=None, host="10.1.0.2"), main.Response()))
        r_bad = await _status(main.do_login(Req({"password": "한글틀림"}, api_key=None, host="10.1.0.3"), main.Response()))
        ok("SB2-a 비ASCII 비번 테넌트가 있어도 로그인: 맞는 비번 200·한글 비번 200·틀린 한글 401",
           (r_ok, r_kr, r_bad) == (200, 200, 401), str((r_ok, r_kr, r_bad)))
        r_list = await _status(main.do_login(Req([1], api_key=None, host="10.1.0.4"), main.Response()))
        ok("SB2-b 로그인 본문이 목록이면 500 이 아니라 401", r_list == 401, str(r_list))
        try:
            t1 = main.check_api_key(Req(api_key="\xc3\xa9bad", host="10.1.0.5"))
            t2 = main.check_api_key(Req(api_key="지인키", host="10.1.0.6"))
            res = (t1, t2)
        except Exception as e:
            res = "EXC:%s" % type(e).__name__
        ok("SB2-c check_api_key: 비ASCII 틀린 키는 None, 한글 맞는 키는 그 테넌트", res == (None, "krt"), str(res))
        out = []
        for key in (123, "한글키", ["testkey"]):
            try:
                r = await main.license_check(Req({"api_key": key, "nonce": "n"}, api_key=None, host="10.1.0.7"))
                out.append((r.status_code, json.loads(r.body)["valid"]))
            except Exception as e:
                out.append("EXC:%s" % type(e).__name__)
        try:
            r = await main.license_check(Req({"api_key": "testkey", "nonce": "n"}, api_key=None, host="10.1.0.8"))
            good = json.loads(r.body)["valid"]
        except Exception as e:
            good = "EXC:%s" % type(e).__name__
        ok("SB2-d /license: 숫자·한글·목록 키는 200 valid=false, 맞는 키는 valid=true",
           out == [(200, False)] * 3 and good is True, str((out, good)))
        ws = FakeWS(key="한글키")
        try:
            await main.macro_websocket(ws, "PC-01")
            wres = ws.closed
        except Exception as e:
            wres = "EXC:%s" % type(e).__name__
        ok("SB2-e /ws/macro 비ASCII 키는 1008 로 닫힌다(예외 아님)", wres == 1008, str(wres))
        body = b'{"exe_version": "0.0.0"}'
        rc, _ = await _asgi("POST", "/check", [body], [("content-type", "application/json"), ("x-api-key", "한글키"),
                                                       ("content-length", str(len(body)))])
        ok("SB2-f /check 비ASCII 키 헤더에 500 이 아니다(실제 앱)", rc == 200, str(rc))
    finally:
        main.TENANTS.pop("krt", None)
        main.PW_TO_TENANT.pop("지인비번", None)
        main.KEY_TO_TENANT.pop("지인키", None)
        main._KEY_FAILS.clear()
        main._LOGIN_FAILS.clear()


# ───────────────── SB12 — 키 없는 세션 요청은 추측이 아니다 ─────────────────

async def t_sb12_keyfail():
    ip = "203.0.113.77"
    main._KEY_FAILS.clear()
    sess = main.new_session("main")
    codes = set()
    for _ in range(main.KEY_MAX_FAILS + 5):
        r = await main.get_setting_ep("awakening_preset", Req(api_key=None, session=sess, host=ip))
        codes.add(r.status_code)
    ok("SB12-a 대시보드 세션 GET /setting 35번 — 200 이고 그 IP 실패 카운터가 안 오른다",
       codes == {200} and main._KEY_FAILS.get(ip) is None, str((codes, main._KEY_FAILS.get(ip))))
    ok("SB12-b 같은 IP 가 안 잠긴다(/check·WS·/license·FV 공용 문)", not main._key_probe_blocked(ip))
    for _ in range(main.KEY_MAX_FAILS):
        main.check_api_key(Req(api_key="wrong", host="203.0.113.78"))
    ok("SB12-c (역방향) 틀린 키는 여전히 세어져 잠기고, SB12-a 의 IP 는 안 잠겼다",
       main._key_probe_blocked("203.0.113.78") and not main._key_probe_blocked(ip))
    main._KEY_FAILS.clear()


# ───────────────── SB1 — NaN/Infinity ─────────────────

async def t_sb1_nan():
    body = json.loads('{"status":"hunting","efficiency":NaN,"kina_rate":Infinity,"x":[-Infinity]}')
    await main.receive_report("PC-41", Req(body))
    rc = await _status(main.all_statuses(S()))
    ok("SB1-a NaN 보고 뒤에도 GET /status 200", rc == 200, str(rc))
    rows = await main._build_full_state("main")
    ws_msg = json.dumps({"type": "state", "pcs": rows}, ensure_ascii=False)
    ok("SB1-b WS 상태 프레임에 NaN/Infinity 토큰이 없다(브라우저 JSON.parse 가능)",
       "NaN" not in ws_msg and "Infinity" not in ws_msg)
    snap = await main._fv_build_snapshot("main")
    fv = json.dumps(snap, ensure_ascii=False, default=str)
    ok("SB1-c FV 스냅샷 본문에 NaN/Infinity 가 없다", "NaN" not in fv and "Infinity" not in fv)
    await db.upsert_status("PC-42", {"status": "hunting", "e": float("nan"), "k": 5.5, "l": [float("inf"), 1]})
    got = await db.get_status("PC-42")
    ok("SB1-d 저장 자리(upsert_status)가 NaN/Inf → None, 정상 숫자는 그대로",
       got and got.get("e") is None and got.get("k") == 5.5 and got.get("l") == [None, 1], str(got))
    cbody = json.loads('{"total_kina": 5, "characters": [{"slot": 1, "name": "n", "power_power": NaN}]}')
    await main.receive_char_info("PC-43", Req(cbody))
    rc = await _status(main.get_all_characters(S()))
    ok("SB1-e 캐릭 정보에 NaN 이 와도 GET /characters 200", rc == 200, str(rc))


# ───────────────── SB3 — slot 형 검사 + AI 계획표 XSS ─────────────────

EVIL_SLOT = "<img src=x onerror=alert(document.domain)>"


async def t_sb3_server():
    rc = await _status(main.receive_char_info("PC-44", Req({"total_kina": 1, "characters": [{"slot": EVIL_SLOT}]})))
    info = await db.get_char_info("PC-44")
    ok("SB3-a /char_info slot 에 꺾쇠 문자열 → 400, 저장 안 됨", rc == 400 and info is None, str((rc, info)))
    await main.receive_char_info("PC-45", Req({"total_kina": 10, "characters": [{"slot": 1}, {"slot": 2}]}))
    rc = await _status(main.receive_char_info("PC-45", Req({"merge": True, "characters": [{"slot": "3"}]})))
    info = await db.get_char_info("PC-45")
    slots = [c.get("slot") for c in (info or {}).get("chars", [])]
    ok("SB3-b 문자열 slot \"3\" 병합 → 200, 정수 3 으로 저장·정렬 [1,2,3]", rc == 200 and slots == [1, 2, 3],
       str((rc, slots)))


def t_sb3_js():
    if not _need_node("SB3"):
        return
    src = main.HTML_DASHBOARD
    try:
        code = _grab(src, ("esc", "aiParseOdd", "aiBuildPlan", "renderAiPlan"))
        i = src.index("const AI_T = ")
        j = src.index("};\n", i)
        ai_t = src[i:j + 2]
    except Exception as e:
        ok("SB3 JS 함수를 잘라낸다", False, str(e))
        return
    js = _DOM + ai_t + "\nconst SUB_DEN_MIN = 700;\n" + code + r"""
let aiLang = 'ko', aiFilter = 'dg', aiDone = {keys: []};
function baseId(x){ return x; } function acctTagSpread(){ return ''; } function aiAcctInfo(){ return ''; }
let charTableData = [{pc_id:'PC-01', slot:%s, name:'n', power_power:1, odd_energy:'100/840'},
                     {pc_id:'PC-02', slot:2, name:'ok', power_power:1, odd_energy:'100/840'}];
renderAiPlan();
console.log(JSON.stringify({h: els['ai-body'].innerHTML}));
""" % json.dumps(EVIL_SLOT)
    try:
        o = _run_node(js)
    except Exception as e:
        ok("SB3 node 실행", False, str(e)[:300])
        return
    h = o["h"]
    ok("SB3-c AI 계획표에 slot 페이로드가 날것으로 안 들어가고, 정상 캐릭 줄은 그려진다",
       "<img" not in h and "ok" in h and h.count('type="checkbox"') == 2, re.sub(r"\s+", " ", h)[:200])
    onch = re.findall(r'onchange="([^"]*)"', h)
    ok("SB3-d 체크박스 onchange 에 데이터가 문자열로 안 박힌다(data-k 로 넘긴다)",
       onch and all(x == "aiToggleDone(this.dataset.k, this)" for x in onch)
       and _html.unescape(re.findall(r'data-k="([^"]*)"', h)[-1]) == "PC-02:2", str(onch)[:200])


# ───────────────── SB9 — 버그 파일명 ─────────────────

class _UF:
    def __init__(self, name, data):
        self.filename = name
        self._d = data
        self.asked = None

    async def read(self, n=-1):
        self.asked = n
        return self._d if n is None or n < 0 else self._d[:n]


async def t_sb9_upload():
    uf = _UF("x'-alert(document.domain)-'.png", b"\x89PNG" + b"\0" * 100)   # (윈도우는 " 를 파일명에 못 쓴다)
    try:
        r = await main.upload_bug("PC-46", Req(), uf)
        fn = json.loads(r.body)["filename"]
    except Exception as e:
        fn = "EXC:%s" % type(e).__name__
    ok("SB9-a 업로드 파일명에서 따옴표·괄호가 빠진다(.png·pc 접두사 유지)",
       not re.search(r"['()]", fn) and fn.startswith("PC-46_") and fn.endswith(".png"), fn)
    ok("SB5-a upload_bug 는 상한+1 바이트만 읽는다(통째로 안 읽음)", uf.asked == 8 * 1024 * 1024 + 1, str(uf.asked))


def t_sb9_js():
    if not _need_node("SB9"):
        return
    src = main.HTML_DASHBOARD
    try:
        code = _grab(src, ("esc", "openBugsModal", "deleteBug"))
    except Exception as e:
        ok("SB9 JS 함수를 잘라낸다", False, str(e))
        return
    fname = "PC-01_20260923_000000_x'-alert(document.domain)-'.png"
    js = _DOM + code + r"""
let bugModalPc = null; const FN = %s; const urls = [];
function baseId(x){ return x; } function downloadAndClearBugs(){} function clearBugsOf(){}
global.confirm = () => true;
global.fetch = async (u, o) => { urls.push(u); return {ok: !o, json: async () => ({bugs: [{filename: FN, size: 10}]})}; };
(async () => {
  await openBugsModal('PC-01');
  const h = els['bug-list'].innerHTML;
  const btn = (h.match(/<button[^>]*>/) || [''])[0];
  const df = (btn.match(/data-f="([^"]*)"/) || ['', ''])[1];
  const oc = (btn.match(/onclick="([^"]*)"/) || ['', ''])[1];
  const dec = s => s.replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&#61;/g, '=').replace(/&#47;/g, '/')
                    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&#96;/g, '`').replace(/&amp;/g, '&');
  await deleteBug(dec(df));
  console.log(JSON.stringify({oc: dec(oc), df: dec(df), del: urls[urls.length - 1]}));
})();
""" % json.dumps(fname)
    try:
        o = _run_node(js)
    except Exception as e:
        ok("SB9 node 실행", False, str(e)[:300])
        return
    from urllib.parse import quote
    ok("SB9-b 버그 목록 🗑 버튼 onclick 에 파일명이 JS 문자열로 안 들어간다(data-f → 그대로 삭제 URL)",
       o["oc"] == "deleteBug(this.dataset.f)" and o["df"] == fname
       and o["del"] == "/bugs/image/" + quote(fname, safe="-_.!~*'()"), str(o)[:300])


# ───────────────── SB4 — /bugs/download ─────────────────

HOST = "web-production-8d4c.up.railway.app"


def _seed_bugs(n, pc="PC-01", size=4):
    bdir = main.tenant_bugs_dir("main")
    os.makedirs(bdir, exist_ok=True)
    for i in range(n):
        with open(os.path.join(bdir, "%s_20260923_%06d_x.png" % (pc, i)), "wb") as f:
            f.write(os.urandom(size) if size > 4 else b"\x89PNG")
    main._bug_cache_bust("main")


async def t_sb4_download():
    async def _nop(*a, **k):
        return None
    _ps = main.push_state
    main.push_state = _nop
    try:
        sess = main.new_session("main")
        _seed_bugs(3, "PC-47")
        rc = await _status(main.download_bugs_zip(
            Req(api_key=None, session=sess, headers={"host": HOST, "referer": "https://%s.evil.example/" % HOST}),
            pc_id="PC-47"))
        left = len(main._list_bug_files("main", "PC-47"))
        rc2 = await _status(main.download_bugs_zip(
            Req(api_key=None, session=sess, headers={"host": HOST, "referer": "https://%s/" % HOST}), pc_id="PC-47"))
        ok("SB4-a 닮은 호스트(<우리호스트>.evil.example) Referer 는 403·파일 그대로, 진짜 Referer 는 200",
           rc == 403 and left == 3 and rc2 == 200, str((rc, left, rc2)))
        _seed_bugs(2, "가PC")
        try:
            r = await main.download_bugs_zip(
                Req(api_key=None, session=sess, headers={"host": HOST, "referer": "https://%s/" % HOST}), pc_id="가PC")
            cd = r.headers.get("content-disposition", "")
            res = (r.status_code, "filename*=UTF-8''" in cd and "%EA%B0%80PC" in cd)
        except Exception as e:
            res = "EXC:%s" % type(e).__name__
        ok("SB4-b 한글 pc_id 다운로드 200 + filename*=UTF-8'' (예전엔 파일을 지운 뒤 500)", res == (200, True), str(res))
        # 루프 정지 — 실제 스크린샷처럼 안 줄어드는 1MB × 60장
        _seed_bugs(60, "PC-48", size=1_000_000)
        gaps, stop = [], asyncio.Event()

        async def ticker():
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now
        tk = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        r = await main.download_bugs_zip(
            Req(api_key=None, session=sess, headers={"host": HOST, "referer": "https://%s/" % HOST}), pc_id="PC-48")
        stop.set()
        await tk
        import io
        import zipfile
        if hasattr(r, "body"):
            zdata = r.body
        else:                                  # 고치기 전 코드는 StreamingResponse 였다
            zdata = b"".join([c async for c in r.body_iterator])
        zf = zipfile.ZipFile(io.BytesIO(zdata))
        infos = zf.infolist()
        ok("SB4-c 60장 zip 이 이벤트 루프를 0.5초 넘게 안 세운다 + 60장 전부·STORED",
           max(gaps) < 0.5 and len(infos) == 60 and all(i.compress_type == zipfile.ZIP_STORED for i in infos),
           "max_gap=%.2fs n=%d" % (max(gaps), len(infos)))
    finally:
        main.push_state = _ps


# ───────────────── SB5 — 본문 크기 상한 (실제 ASGI 앱) ─────────────────

async def _asgi(method, path, chunks, headers=()):
    msgs = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    consumed, out = [0], {"status": None, "body": b""}

    async def receive():
        if msgs:
            m = msgs.pop(0)
            consumed[0] += len(m["body"])
            return m
        await asyncio.sleep(3600)          # 다 보냈으면 끊김 대기(실제 서버와 같음)

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "scheme": "http",
             "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 5555),
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}
    try:
        await asyncio.wait_for(main.app(scope, receive, send), 60)
    except Exception as e:                 # 고치기 전 코드는 500 을 보내고 예외를 다시 던진다
        out["status"] = out["status"] or "EXC:%s" % type(e).__name__
    return out["status"], consumed[0]


def _multipart(name, data):
    b = "----sbb2boundary"
    head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
            'Content-Type: image/png\r\n\r\n' % (b, name)).encode()
    body = head + data + ("\r\n--%s--\r\n" % b).encode()
    return body, "multipart/form-data; boundary=%s" % b


async def t_sb5_body():
    small = json.dumps({"exe_version": "0.0.0", "image_hashes": {"a%d.png" % i: "0" * 64 for i in range(2000)}}).encode()
    big = json.dumps({"exe_version": "0.0.0", "pad": "x" * (1100 * 1024)}).encode()
    st_small, _ = await _asgi("POST", "/check", [small], [("content-type", "application/json"),
                                                          ("content-length", str(len(small)))])
    st_big, used = await _asgi("POST", "/check", [big], [("content-type", "application/json"),
                                                         ("content-length", str(len(big)))])
    ok("SB5-b 무인증 /check: 해시 2000개(%dKB)는 200, 1.1MB 는 본문을 읽기 전에 413" % (len(small) // 1024),
       st_small == 200 and st_big == 413 and used == 0, str((st_small, st_big, used)))
    chunk = b" " * (256 * 1024)
    st_ch, used = await _asgi("POST", "/check", [chunk] * 12, [("content-type", "application/json")])
    ok("SB5-c 길이 없는(청크) 3MB /check 는 상한 근처에서 끊고 400/413",
       st_ch in (400, 413) and used <= 1024 * 1024 + len(chunk), str((st_ch, used)))
    png_ok, ct = _multipart("ok.png", b"\x89PNG" + b"\0" * (7 * 1024 * 1024))
    st_ok, _ = await _asgi("POST", "/bugs/PC-49", [png_ok], [("x-api-key", "testkey"), ("content-type", ct),
                                                              ("content-length", str(len(png_ok)))])
    huge = 17 * 1024 * 1024
    st_huge, used = await _asgi("POST", "/bugs/PC-49", [b"\0" * 65536], [("x-api-key", "testkey"), ("content-type", ct),
                                                                        ("content-length", str(huge))])
    ok("SB5-d 버그 PNG 7MB 는 200, Content-Length 17MB 는 한 바이트도 안 읽고 413",
       st_ok == 200 and st_huge == 413 and used == 0, str((st_ok, st_huge, used)))
    rc_bad = await _status(main.updater_check(_BadJsonReq()))
    rc_list = await _status(main.updater_check(Req([1], api_key=None)))
    ok("SB5-e /check 깨진 JSON·목록 본문은 400 (500 아님)", (rc_bad, rc_list) == (400, 400), str((rc_bad, rc_list)))
    # telegram_photo 도 상한+1 만 읽는다
    _en, _ch, _sp = main.tg_enabled, main.tenant_chat_id, main.tg_send_photo

    async def _send_photo(*a, **k):
        return 77
    main.tg_enabled, main.tenant_chat_id, main.tg_send_photo = (lambda: True), (lambda t: "1"), _send_photo
    try:
        uf = _UF("c.png", b"\x89PNG" + b"\0" * 10)
        r = await main.telegram_photo("PC-01", _FormReq(), uf)
        ok("SB5-f telegram_photo 는 상한+1 바이트만 읽고 정상 사진은 보낸다",
           uf.asked == 8 * 1024 * 1024 + 1 and json.loads(r.body).get("message_id") == 77, str(uf.asked))
    finally:
        main.tg_enabled, main.tenant_chat_id, main.tg_send_photo = _en, _ch, _sp


class _BadJsonReq(Req):
    def __init__(self):
        super().__init__(api_key=None)

    async def json(self):
        return json.loads("{not json")


class _FormReq(Req):
    async def form(self):
        return {}


# ───────────────── SBL — 낮은 것들 ─────────────────

async def t_sbl_lower():
    async def _nop():
        return None
    main._corridor_persist = _nop
    await main.receive_char_info("PC-50", Req({"total_kina": 5, "characters": [{"slot": 1, "name": "a"}]}))
    res = []
    for payload in ({"our": {"lower": 2, "middle": 1}, "slots": {"1": {"lower": None, "middle": 0}}},
                    {"our": {"lower": "2", "middle": 1}, "slots": {"1": {"lower": 1, "middle": 0}}},
                    {"our": {}, "slots": [1, 2]},
                    {"our": {"lower": 2, "middle": 1}, "slots": {"1": {"lower": 2, "middle": 1}}}):
        await main.save_corridor_progress("PC-50", Req(payload))
        try:
            r = await main.get_all_characters(S())
            row = [x for x in json.loads(r.body)["characters"] if x["pc_id"] == "PC-50"][0]
            res.append((r.status_code, row.get("corridor_progress"), row.get("corridor_full")))
        except Exception as e:
            res.append("EXC:%s" % type(e).__name__)
    ok("SBL-a /characters: 회랑 lower=null·\"2\"·slots=목록에도 200, 정상 값은 그대로 「하2/2·중1/1」",
       all(isinstance(x, tuple) and x[0] == 200 for x in res) and res[3][1:] == ("하2/2·중1/1", True), str(res))
    rc = await _status(main.save_corridor_progress("PC-50", Req([1])))
    ok("SBL-b /corridor/progress 목록 본문은 400", rc == 400, str(rc))
    main.FV_TOKEN = "tok"
    await main.receive_char_info("PC-51", Req({"total_kina": "1,234,567", "characters": [{"slot": 1}]}))
    info = await db.get_char_info("PC-51")
    try:
        r = await main.fv_kina_adjust(Req({"pc_id": "PC-51", "delta_kina": -1000, "why": {"tid": "sb2-t1"}},
                                          api_key=None, headers={"X-FV-Token": "tok"}))
        kr = (r.status_code, json.loads(r.body).get("after"))
    except Exception as e:
        kr = "EXC:%s" % type(e).__name__
    ok("SBL-c total_kina \"1,234,567\" → 정수 1234567 저장, kina_adjust 200 (after 1233567)",
       info and info["total_kina"] == 1234567 and kr == (200, 1233567), str((info and info["total_kina"], kr)))
    rc = await _status(main.receive_char_info("PC-52", Req({"total_kina": "abc", "characters": []})))
    ok("SBL-d total_kina 가 숫자가 아니면 400", rc == 400, str(rc))
    main._TG_MUTE.clear()
    got = []
    for h in ("abc", "nan", [1], "inf"):
        got.append(await _status(main.telegram_mute("PC-05", S({"hours": h}))))
    ok("SBL-e 음소거 hours 가 abc·nan·[1]·inf 면 400, 항목이 안 남는다",
       got == [400, 400, 400, 400] and not main._TG_MUTE, str((got, main._TG_MUTE)))
    _lt = time.localtime
    time.localtime = time.gmtime           # 서버(Railway)는 UTC 다
    try:
        r = await main.telegram_mute("PC-05", S({"hours": 1}))
    finally:
        time.localtime = _lt
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    exp = (_dt.now(_tz.utc) + _td(hours=1) + _td(hours=9)).strftime("%H:%M")
    exp2 = (_dt.now(_tz.utc) + _td(hours=1, seconds=-60) + _td(hours=9)).strftime("%H:%M")
    until = json.loads(r.body)["until"]
    ok("SBL-f 음소거 until 은 서버 시간대와 무관하게 KST", until in (exp, exp2), "%s vs %s" % (until, exp))
    main._TG_MUTE.clear()
    got = []
    for b in ({"filters": {"a": True}}, {"filters": [1]}, [1]):
        got.append(await _status(main.set_slot_filter("PC-01", S(b))))
    ok("SBL-g /slot_filter 숫자 아닌 키·목록 본문은 400", got == [400, 400, 400], str(got))


def test_all():
    run_all([t_sb2_auth, t_sb12_keyfail, t_sb1_nan, t_sb3_server, t_sb3_js, t_sb9_upload, t_sb9_js,
             t_sb4_download, t_sb5_body, t_sbl_lower])
    finish("test_sec_b2", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
