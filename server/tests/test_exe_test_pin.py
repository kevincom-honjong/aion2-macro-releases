# -*- coding: utf-8 -*-
"""[대시보드] /check 시험 exe 핀 (주인님 2026-10-11 「테스트할때는 배포 하지말고 빌드해서 테스트 컴퓨터에 넣어서」)

설정 exe_test_pin = {"PC-01": {"version": "1.1.1049.t3", "sha256": "<64hex>", "until": <unix초>}}
  → /check 가 ★그 PC 한 대에만★ 내부망 시드(lan_seed)의 시험 파일을 가리킨다. 나머지는 그대로.

  P-1  핀 PC 는 핀(version·sha·시드 주소) — GitHub·/dl 주소가 아니다 · _dl_prewarm 안 불림
  P-2  핀 없는 PC 는 정식 그대로 · lan_seed 응답도 그대로
  P-3  접미사 PC-01b·PC-01e → PC-01 로 본다 · 소문자도
  P-4  만료·오입력(밀리초 시각)·깨진 JSON·나쁜 sha/version 은 전부 정식 경로 (500 이 아니다)
  P-5  렌탈 테넌트 키 · 키 없음 → 핀이 안 간다
  P-6  lan_seed 가 비면 핀을 안 준다(GitHub 주소로 폴백하지도 않는다)
  P-7  이미 그 판이면 아무것도 안 준다(정식으로 되돌리지 않는다) · 핀을 지우면 정식으로 돌아간다
  P-8  본문에 pc_id 가 없으면(지금 업데이터) 핀이 안 먹는다 — 그 사실을 시험으로 못 박는다
  P-9  이미지·업데이터 갱신은 핀과 무관하게 그대로
  P-10 /setting/exe_test_pin 은 24대 분량(~3.3KB, seed 포함 ~4.1KB)이 안 잘리고 저장된다(상한 6000)
  P-13 핀의 선택 필드 seed(사설 IPv4:포트) — 있으면 lan_seed 대신 · 모양이 틀리면 핀 무시 · lan_seed 가 비어도 seed 가 있으면 줌
"""
import json
import time

from _harness import main, db, ok, Req, run_all, finish   # noqa: E402

MIN_CHECKS = 65

SEED = "http://172.30.1.17:8766"
REL = "1.1.1048"
OLD = "1.1.1000"
PIN_VER = "1.1.1049.t3"
PIN_SHA = "ab" * 32
REL_SHA = "cd" * 32
NOW = time.time()


class _CheckReq(Req):
    base_url = "http://test/"


_VJ = {"exe": {"version": REL, "sha256": REL_SHA},
       "rental": {"version": REL, "sha256": REL_SHA},
       "images": {"a.png": "h1"}, "updater": {"version": "3.1.13", "sha256": "ee" * 32}}


def _pin(**ov):
    d = {"version": PIN_VER, "sha256": PIN_SHA, "until": NOW + 3600}
    d.update(ov)
    return d


async def _set_pin(val):
    await db.set_setting(main.ns("main", "exe_test_pin"), val if isinstance(val, str) else json.dumps(val))


async def _set_seed(v):
    await db.set_setting(main.ns("main", "lan_seed"), v)


async def check(pc_id=None, key="testkey", exe_ver=OLD, img=None, host="127.0.0.1"):
    body = {"exe_version": exe_ver, "image_hashes": img if img is not None else {"a.png": "h1"},
            "updater_version": "3.1.13", "edition": "main"}
    if pc_id is not None:
        body["pc_id"] = pc_id
    r = await main.updater_check(_CheckReq(body, api_key=key, host=host))
    return json.loads(r.body)


def _eu(d):
    return d.get("exe_update") or {}


async def t_pin():
    _ov, _pw = main._load_version_json_async, main._dl_prewarm
    _prew = []

    async def _vj():
        return json.loads(json.dumps(_VJ))
    main._load_version_json_async = _vj
    main._dl_prewarm = lambda ver, asset: _prew.append(asset)
    main.KEY_TO_TENANT["pin-rent-key"] = "pinrent"
    try:
        await _set_seed(SEED)
        await _set_pin({"PC-01": _pin()})

        # ── P-1 핀 PC
        d = await check("PC-01")
        eu = _eu(d)
        ok("P-1a 핀 PC → exe_update.version = 핀 버전", eu.get("version") == PIN_VER, str(eu))
        ok("P-1b sha256 = 핀의 것(정식 sha 가 아니다)", eu.get("sha256") == PIN_SHA, str(eu.get("sha256")))
        ok("P-1c download_url = 시드/macro-<핀>.exe", eu.get("download_url") == f"{SEED}/macro-{PIN_VER}.exe", str(eu.get("download_url")))
        ok("P-1d GitHub·/dl 중계 주소가 아니다", "github" not in str(eu.get("download_url")) and "/dl/" not in str(eu.get("download_url")))
        ok("P-1e 핀 PC 에 _dl_prewarm 안 불림(DL_RELAY 켜짐에도)", main.DL_RELAY and _prew == [], f"RELAY={main.DL_RELAY} {_prew}")
        ok("P-1f lan_seed 응답은 그대로", d.get("lan_seed") == SEED, str(d.get("lan_seed")))

        # ── P-2 핀 없는 PC
        _prew.clear()
        d2 = await check("PC-02")
        eu2 = _eu(d2)
        ok("P-2a 핀 없는 PC → 정식 버전", eu2.get("version") == REL and eu2.get("sha256") == REL_SHA, str(eu2))
        ok("P-2b 정식 경로(중계 /dl 주소) 그대로", "/dl/" in str(eu2.get("download_url")) and PIN_VER not in str(eu2.get("download_url")),
           str(eu2.get("download_url")))
        ok("P-2c 정식 경로는 _dl_prewarm 을 그대로 부른다", _prew == [f"macro-{REL}.exe"], str(_prew))
        ok("P-2d lan_seed 응답 그대로", d2.get("lan_seed") == SEED)

        # ── P-3 접미사·대소문자
        for pid in ("PC-01b", "PC-01c", "PC-01d", "PC-01e", "pc-01"):
            dd = await check(pid)
            ok("P-3 %s → PC-01 핀" % pid, _eu(dd).get("version") == PIN_VER, str(_eu(dd)))
        ok("P-3f PC-011(다른 PC, 숫자 끝)은 핀이 아니다", _eu(await check("PC-011")).get("version") == REL)
        ok("P-3g PC-10 은 PC-01 이 아니다", _eu(await check("PC-10")).get("version") == REL)

        # ── P-4 만료·오입력
        for name, val in (
            ("만료", {"PC-01": _pin(until=NOW - 1)}),
            ("밀리초 시각(7일 초과)", {"PC-01": _pin(until=(NOW + 3600) * 1000)}),
            ("until 문자열", {"PC-01": _pin(until="9999999999")}),
            ("until bool", {"PC-01": _pin(until=True)}),
            ("sha 짧음", {"PC-01": _pin(sha256="ab" * 10)}),
            ("sha 16진 아님", {"PC-01": _pin(sha256="zz" * 32)}),
            ("version 에 .t 없음(정식 이름)", {"PC-01": _pin(version="1.1.1049")}),
            ("version 경로 문자", {"PC-01": _pin(version="../x.t1")}),
            ("version 비문자", {"PC-01": _pin(version=1049)}),
            ("version 뒤에 개행", {"PC-01": _pin(version=PIN_VER + chr(10))}),
            ("sha 뒤에 개행", {"PC-01": _pin(sha256=PIN_SHA + chr(10))}),
            ("항목이 dict 아님", {"PC-01": "x"}),
            ("루트가 목록", [1, 2]),
        ):
            await _set_pin(val)
            try:
                dd = await check("PC-01")
                good = _eu(dd).get("version") == REL
            except Exception as e:
                dd, good = repr(e), False
            ok("P-4 %s → 정식 경로(500 아님)" % name, good, str(dd)[:100])
        await _set_pin("{깨진 json")
        try:
            dd = await check("PC-01")
            good = _eu(dd).get("version") == REL
        except Exception as e:
            dd, good = repr(e), False
        ok("P-4 깨진 JSON → 정식 경로(500 아님)", good, str(dd)[:100])

        # ── P-5 렌탈·키 없음
        await _set_pin({"PC-01": _pin()})
        dr = await check("PC-01", key="pin-rent-key")
        ok("P-5a 렌탈 테넌트 키 → 핀 안 감", PIN_VER not in json.dumps(dr) and _eu(dr).get("version") != PIN_VER, str(dr)[:150])
        ok("P-5b 렌탈 응답엔 시드(내부망 주소)도 안 감", "lan_seed" not in dr, str(dr.get("lan_seed")))
        dn = await check("PC-01", key=None)
        ok("P-5c 키 없음 → 핀·exe 아무것도 안 감", not dn.get("exe_update") and dn.get("notice") == "no_key_no_exe", str(dn)[:150])
        dbad = await check("PC-01", key="틀린-키", host="10.9.9.9")   # 키 추측 잠금이 시험 주소를 안 물게 다른 주소
        ok("P-5d 틀린 키 → 핀 안 감", not dbad.get("exe_update") and PIN_VER not in json.dumps(dbad), str(dbad)[:150])

        # ── P-6 lan_seed 비면
        await _set_seed("")
        d6 = await check("PC-01")
        ok("P-6a lan_seed 비면 핀 안 줌 → 정식 경로", _eu(d6).get("version") == REL and PIN_VER not in json.dumps(d6), str(_eu(d6)))
        ok("P-6b GitHub 폴백 주소를 핀에 붙이지 않는다", f"macro-{PIN_VER}" not in json.dumps(d6))
        await _set_seed(SEED)

        # ── P-7 이미 그 판 · 핀 해제
        d7 = await check("PC-01", exe_ver=PIN_VER)
        ok("P-7a 이미 시험판이면 exe_update 없음(정식으로 되돌리지 않는다)", "exe_update" not in d7, str(_eu(d7)))
        d7b = await check("PC-02", exe_ver=PIN_VER)
        ok("P-7b 핀 없는 PC 가 우연히 그 버전이면 정식으로 간다(핀은 PC 한 대 몫)", _eu(d7b).get("version") == REL, str(_eu(d7b)))
        await _set_pin({})
        d7c = await check("PC-01", exe_ver=PIN_VER)
        ok("P-7c 핀 해제 → 시험판 PC 가 정식으로 돌아간다", _eu(d7c).get("version") == REL, str(_eu(d7c)))
        await _set_pin("")
        ok("P-7d 값이 빈 문자열이어도 정식", _eu(await check("PC-01")).get("version") == REL)

        # ── P-8 pc_id 없음
        await _set_pin({"PC-01": _pin()})
        d8 = await check(None)
        ok("P-8a 본문에 pc_id 없음(지금 업데이터) → 핀이 안 먹는다", _eu(d8).get("version") == REL, str(_eu(d8)))
        weird = []
        for v in (["PC-01"], 1, {"a": 1}, "", "   "):
            try:
                weird.append(_eu(await check(v)).get("version") == REL)
            except Exception as e:
                weird.append(repr(e))
        ok("P-8b pc_id 가 목록·숫자·빈 값이어도 500 아님(정식 경로)", all(x is True for x in weird), str(weird))

        # ── P-9 이미지·업데이터는 핀과 무관
        d9 = await check("PC-01", img={})
        ok("P-9a 핀 PC 도 이미지 갱신은 그대로", [x["filename"] for x in d9.get("images_update", [])] == ["a.png"], str(d9.get("images_update")))
        d9b = await check("PC-01")
        ok("P-9b 이미지가 같으면 images_update 없음", "images_update" not in d9b)

        # ── P-1x 여러 PC 핀
        await _set_pin({"PC-01": _pin(), "PC-03": _pin(version="1.1.1049.t4", sha256="12" * 32)})
        a, b, c = await check("PC-01"), await check("PC-03"), await check("PC-05")
        ok("P-10 PC 마다 자기 핀", _eu(a).get("version") == PIN_VER and _eu(b).get("version") == "1.1.1049.t4"
           and _eu(b).get("sha256") == "12" * 32 and _eu(c).get("version") == REL, f"{_eu(a).get('version')} {_eu(b).get('version')} {_eu(c).get('version')}")

        # ── P-11 /setting 길이
        big = {f"PC-{i:02d}": _pin() for i in range(1, 25)}
        raw = json.dumps(big, separators=(",", ":"))
        S = lambda v: Req({"value": v}, api_key=None, session=main.new_session("main"))   # noqa: E731
        r = await main.set_setting_ep("exe_test_pin", S(raw))
        got = await db.get_setting(main.ns("main", "exe_test_pin"))
        ok("P-11a 24대 분량(%d자)이 안 잘리고 저장된다" % len(raw), r.status_code == 200 and got == raw and len(raw) > 100, f"{len(raw)} → {len(got or '')}")
        ok("P-11b 저장된 24대 핀이 실제로 먹는다", _eu(await check("PC-24")).get("version") == PIN_VER)
        r2 = await main.set_setting_ep("exe_test_pin", S("x" * 7000))
        got2 = await db.get_setting(main.ns("main", "exe_test_pin"))
        ok("P-11c 상한(6000)은 있다", len(got2 or "") == 6000, str(len(got2 or "")))
        big_s = {f"PC-{i:02d}": _pin(seed="http://172.30.1.17:8777") for i in range(1, 25)}
        raw_s = json.dumps(big_s, separators=(",", ":"))
        await main.set_setting_ep("exe_test_pin", S(raw_s))
        got3 = await db.get_setting(main.ns("main", "exe_test_pin"))
        ok("P-11d seed 까지 실은 24대 분량(%d자)도 안 잘린다" % len(raw_s), got3 == raw_s and len(raw_s) > 4000, f"{len(raw_s)} → {len(got3 or '')}")

        # ── P-13 핀 항목의 선택 필드 seed (운영 시드를 못 건드릴 때 시험 파일만 따로 내주는 서버)
        TSEED = "http://172.30.1.17:8777"
        await _set_seed(SEED)
        await _set_pin({"PC-01": _pin(seed=TSEED)})
        d13 = await check("PC-01")
        ok("P-13a seed 있는 핀 → download_url 이 그 seed(lan_seed 가 아니다)",
           _eu(d13).get("download_url") == f"{TSEED}/macro-{PIN_VER}.exe" and _eu(d13).get("version") == PIN_VER, str(_eu(d13)))
        ok("P-13b 응답 lan_seed 는 그대로(함대 시드를 안 바꾼다)", d13.get("lan_seed") == SEED, str(d13.get("lan_seed")))
        d13c = await check("PC-02")
        ok("P-13c 다른 PC 는 정식 그대로(핀의 seed 가 안 샌다)", _eu(d13c).get("version") == REL and TSEED not in json.dumps(d13c), str(_eu(d13c)))
        await _set_seed("")
        d13d = await check("PC-01")
        ok("P-13d lan_seed 가 비어도 핀의 seed 가 있으면 그리로 준다(운영 시드가 없는 판)",
           _eu(d13d).get("download_url") == f"{TSEED}/macro-{PIN_VER}.exe", str(_eu(d13d)))
        await _set_seed(SEED)
        await _set_pin({"PC-01": _pin(seed="")})
        ok("P-13e seed 빈 문자열 = 없음 → lan_seed", _eu(await check("PC-01")).get("download_url") == f"{SEED}/macro-{PIN_VER}.exe")
        await _set_pin({"PC-01": _pin(seed=None)})
        ok("P-13f seed null = 없음 → lan_seed", _eu(await check("PC-01")).get("download_url") == f"{SEED}/macro-{PIN_VER}.exe")
        for good in ("http://10.1.2.3:8777", "http://172.16.0.9:80", "http://172.31.255.1:65535", "http://192.168.0.5:8766"):
            await _set_pin({"PC-01": _pin(seed=good)})
            ok("P-13g 사설 대역 통과 %s" % good, _eu(await check("PC-01")).get("download_url") == f"{good}/macro-{PIN_VER}.exe")
        bad_seeds = ["http://8.8.8.8:8766", "http://172.32.0.1:8766", "http://172.15.0.1:8766", "https://10.0.0.1:8766",
                     "http://10.0.0.1", "http://10.0.0.1:8766/", "http://evil.com:8766", "http://10.0.0.1:8766@evil.com",
                     "http://evil.com:80/@10.0.0.1:8766", "http://10.0.0.1:8766" + chr(10), "http://10.0.0.1:8766/x", "ftp://10.0.0.1:21",
                     "http://127.0.0.1:8766", "http://[::1]:8766", "http://10.0.0.1:1", " http://10.0.0.1:8766", 1234, ["http://10.0.0.1:8766"]]
        bad_ok = []
        for bs in bad_seeds:
            await _set_pin({"PC-01": _pin(seed=bs)})
            try:
                dd = await check("PC-01")
                bad_ok.append(_eu(dd).get("version") == REL and "10.0.0.1" not in json.dumps(dd) and "evil" not in json.dumps(dd))
            except Exception as e:
                bad_ok.append(repr(e))
        ok("P-13h 사설 대역이 아닌/깨진 seed %d종 → 핀을 무시하고 정식 경로(500 아님)" % len(bad_seeds),
           all(x is True for x in bad_ok), str([(b, x) for b, x in zip(bad_seeds, bad_ok) if x is not True][:3]))
        await _set_seed("")
        await _set_pin({"PC-01": _pin()})
        ok("P-13i seed 도 lan_seed 도 없으면 핀 무시(예전 그대로)", _eu(await check("PC-01")).get("version") == REL)
        await _set_seed(SEED)

        # ── P-12 순수 함수
        ok("P-12a _pin_pick 만료 직전/직후", main._pin_pick(json.dumps({"PC-01": _pin(until=100)}), "PC-01", 99.0) is not None
           and main._pin_pick(json.dumps({"PC-01": _pin(until=100)}), "PC-01", 100.0) is None)
        ok("P-12b _pin_pick NaN until", main._pin_pick('{"PC-01":{"version":"1.1.1.t1","sha256":"%s","until":NaN}}' % PIN_SHA, "PC-01", 1.0) is None)
        ok("P-12c _pin_pick None·빈 PC", main._pin_pick(None, "PC-01", 1.0) is None and main._pin_pick(json.dumps({"PC-01": _pin()}), "", 1.0) is None)
    finally:
        main._load_version_json_async, main._dl_prewarm = _ov, _pw
        main.KEY_TO_TENANT.pop("pin-rent-key", None)
        await _set_pin("")


run_all([t_pin])
finish("test_exe_test_pin", MIN_CHECKS)
