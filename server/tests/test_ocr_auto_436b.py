# -*- coding: utf-8 -*-
"""[대시보드] #436-b (2026-10-11 아이온2 «OCR 큐 325→4 — 쌓이는 것에서 개선») — 자동 닫기 규칙 넷.

사람이 라벨 준 이력 1,895건(/ocr/history 전량)을 규칙에 다시 넣어 본 결과: 닫힌 묶음 중 사람 라벨과 다른 것 0 ·
«나쁨» 으로 찍힌 묶음 중 닫힌 것 0.

  D-*  던전 이름(read_dungeon_key · dungeon_key): «죽은 자의 침실(극한|절망|지옥|파멸)» 만, 정식 표기는 사람 라벨 그대로
  O-*  각성전 목표: 정확 일치 + 공백 무시 + «거의 같음»(문장부호 걷고 편집거리 ≤2, 글자 수 ±1, 가장 가까운 문구 하나뿐)
  P-*  진행도 %: 제미나이와 로컬이 수로 같을 때만(12% == 12.0), 한 쪽이 비면 사람 몫
  M-*  맵 이름: 성소 감시초소 · 파괴된 잔해 · 죽은 자의 침실 4난이도 · 잊힌 저장소 5난이도 추가(예전 16개 그대로)
  S-*  맵 이름 스냅(#436-c): 띄어쓰기·한 글자 오독 → 알려진 이름, 동률(엘듄/엘룬)·두 글자↑ 는 사람 몫
  F-*  DB 흐름: 제출 → auto · 쓸기(auto_sweep) · 다른 답이 붙으면 pending 으로 되돌림

    cd updater/server && python -X utf8 tests/test_ocr_auto_436b.py
"""
import base64
import hashlib

import aiosqlite
from fastapi.testclient import TestClient

from _harness import main, db, ok, run_all, finish   # noqa: E402
import ocr_label as OL                                # noqa: E402

MIN_CHECKS = 51
KEY = "testkey"
C = TestClient(main.app, raise_server_exceptions=False, follow_redirects=False)
PNG = b"\x89PNG\r\n\x1a\n"
_N = [0]

DK, DK2 = "awakening_py__read_dungeon_key", "awakening_py__dungeon_key"
OBJ, DIF = "awakening_py__read_objective", "awakening_py__read_diff_label"
PRG, MAP = "analytics_py__progress_loop", "analytics_py__map_loop"
OBJ_RAW, DK_RAW, PRG_RAW, MAP_RAW = ("awakening.py:_read_objective", "awakening.py:_read_dungeon_key",
                                     "analytics.py:_progress_loop", "analytics.py:_map_loop")


def far(i):
    return hashlib.sha256(("f436b-%d" % i).encode()).hexdigest()[:16]


def sub(site_raw, gem, dh, local=""):
    _N[0] += 1
    h = hashlib.sha256(("i436b%d" % _N[0]).encode()).digest()
    body = {"pc_id": "PC-436B", "site": site_raw, "prompt": "p", "img_b64": base64.b64encode(PNG + (h * 9)[:256]).decode(),
            "gemini_answer": gem, "local_answer": local, "dhash": dh, "ts": 1790000000.0,
            "prompt_sha1": "a1b2c3d4e5f6", "sha1": hashlib.sha1(("k436b%d" % _N[0]).encode()).hexdigest()}
    r = C.post("/ocr/submit", json=body, headers={"X-Api-Key": KEY})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    return r.json()


async def _st(cid):
    async with aiosqlite.connect(db.DB_PATH) as c:
        cur = await c.execute("SELECT status, label FROM ocr_cluster WHERE id=?", (cid,))
        return tuple(await cur.fetchone())


def L(site, gem, local=""):
    return OL.auto_label_rows(site, [(gem, local)])


def t_dungeon():
    for d in ("극한", "절망", "지옥", "파멸"):
        want = "죽은 자의 침실(%s)" % d
        ok("D-1 %s → 사람 라벨 그대로의 표기" % want, L(DK, want) == want and L(DK2, want) == want, str(L(DK, want)))
    ok("D-2 공백·전각 괄호 차이는 같은 이름(정식 표기로)", L(DK, "죽은자의침실(극한)") == "죽은 자의 침실(극한)"
       and L(DK, "죽은 자의 침실 (극한)") == "죽은 자의 침실(극한)" and L(DK, "죽은 자의 침실（절망）") == "죽은 자의 침실(절망)")
    ok("D-3 사람 라벨 이력에 없는 난이도(쉬움·보통·어려움)와 난이도 없는 이름은 사람 몫",
       all(L(DK, "죽은 자의 침실(%s)" % d) is None for d in ("쉬움", "보통", "어려움")) and L(DK, "죽은 자의 침실") is None)
    ok("D-4 오독·다른 던전·빈 답·«텍스트 없음» 은 사람 몫(퍼지 없음)",
       all(L(DK, g) is None for g in ("죽은 자의 침식(극한)", "잊힌 저장소(극한)", "", "텍스트 없음", "죽은 자의 침실(극한)▼")))
    ok("D-5 묶음의 답이 갈리면 사람 몫", OL.auto_label_rows(DK, [("죽은 자의 침실(극한)", ""), ("죽은 자의 침실(절망)", "")]) is None)
    ok("D-6 난이도 7이름 라벨(read_diff_label)은 예전 그대로 — 퍼지가 안 번진다", L(DIF, "극한") == "극한" and L(DIF, "극한▼") is None
       and L(DIF, "극하") is None and L(DIF, "보통") == "보통")


def t_objective():
    P1, P2, P3 = "방안의몬스터를모두처치", "다음방의입구를열기", "보스를처치하기"
    ok("O-1 정확(공백 무시)은 예전 그대로", L(OBJ, "방 안의 몬스터를 모두 처치") == P1 and L(OBJ, "보스를 처치하기") == P3
       and L(OBJ, "다음 방의 입구를 열기") == P2)
    seen = {"보스를 처치하라": P3, "보스를 처치하자": P3, "보스를 처치하라!": P3, "보스 처치하기!": P3, "보스를 처치하기!": P3,
            "다음 방의 입구권 얻기": P2, "다음 방의 입구로 열기": P2, "다음 방의 입구를 열어": P2, "보스를 처치하기 ▼": P3,
            "보스를 처치하라!!": P3, "보스를 처치하기...": P3, "「보스를 처치하기」": P3}
    ok("O-2 ★이력에서 제미나이가 한두 글자 바꿔 읽은 꼴 전부 닫힌다(%d종)★" % len(seen), all(L(OBJ, g) == w for g, w in seen.items()),
       str([(g, L(OBJ, g)) for g, w in seen.items() if L(OBJ, g) != w]))
    far_ans = ["지지하기", "텍스트 없음", "", "   ", "스마트폰으로 즐기는", "로그인", "구독중", "내일은 맑음",
               "이 이미지에는 한글 텍스트가 포함되어 있지 않습니다. 이미지는 어둡고 흐릿한 배경만을 보여주고 있습니다."]
    ok("O-3 ★어느 문구와도 먼 답(빈 크롭에 지어낸 글·«없음» 류)은 하나도 안 닫힌다★", all(L(OBJ, g) is None for g in far_ans),
       str([(g, L(OBJ, g)) for g in far_ans if L(OBJ, g) is not None]))
    ok("O-4 말이 덧붙은 다른 문장(글자 수 +2)은 거리가 2여도 안 닫힌다", L(OBJ, "보스를 처치하기 시작") is None
       and L(OBJ, "방 안의 몬스터를 모두 처치하라") is None)
    ok("O-5 거리 3 은 안 닫힌다(글자 수가 같아도 셋이 틀리면 사람 몫 · 4자 줄임도)", L(OBJ, "보수를 처지하라") is None
       and L(OBJ, "보스처치") is None and L(OBJ, "보스를처치") is None and L(OBJ, "보스를 처지하라") == "보스를처치하기")
    ok("O-6 두 문구가 섞인 답은 사람 몫", L(OBJ, "방안의몬스터를모두처치 / 보스를처치하기") is None)
    ok("O-7 묶음 안에서 문구가 갈리면 사람 몫 · 같은 문구로 모이면 닫힌다",
       OL.auto_label_rows(OBJ, [("보스를 처치하라", ""), ("다음 방의 입구를 열기", "")]) is None
       and OL.auto_label_rows(OBJ, [("보스를 처치하라", ""), ("보스를 처치하기", "")]) == P3)
    ok("O-8 퍼지는 객관식 목록(각성전 목표)에만 — 난이도·던전 이름은 글자 그대로(맵은 따로 스냅 규칙)",
       set(OL.OCR_AUTO_FUZZY) == {OBJ} and L(DIF, "보퉁") is None and L(DK, "죽은 자의 침식(극한)") is None)
    ok("O-9 편집거리 동률(가장 가까운 문구가 둘)이면 사람 몫", OL._loose_pick.__doc__ is not None and _tie_is_human())


def _tie_is_human():
    """문구 두 개가 같은 거리인 가짜 사이트로 동률 규칙을 직접 시험한다(실제 문구 셋은 서로 멀어 동률이 안 난다)."""
    site = "x_fake__obj"
    OL.OCR_AUTO_SITES[site] = frozenset(("가나다라마", "가나다라바"))
    OL.OCR_AUTO_LOOSE[site] = ("가나다라마", "가나다라바")
    OL.OCR_AUTO_FUZZY[site] = 2
    try:
        return (OL._loose_pick(site, "가나다라사") is None and OL._loose_pick(site, "가나다라마") == "가나다라마"
                and OL._loose_pick(site, "가나다라마사") == "가나다라마")
    finally:
        OL.OCR_AUTO_SITES.pop(site, None)
        OL.OCR_AUTO_LOOSE.pop(site, None)
        OL.OCR_AUTO_FUZZY.pop(site, None)


def t_progress():
    ok("P-1 «12%» == «12.0» → «12%»", L(PRG, "12%", "12.0") == "12%")
    ok("P-2 «12» == «12.0», «12.0%» == «12»", L(PRG, "12", "12.0") == "12%" and L(PRG, "12.0%", "12") == "12%")
    ok("P-3 소수도 같으면 닫는다 «45.67%»", L(PRG, "45.67", "45.67%") == "45.67%" and L(PRG, "0%", "0.0") == "0%" and L(PRG, "100%", "100.0") == "100%")
    ok("P-4 수가 다르면 사람 몫", L(PRG, "12%", "13.0") is None and L(PRG, "28%", "2.8") is None)
    ok("P-5 한 쪽이 비면 사람 몫(로컬 미독·제미나이 빈 답)", L(PRG, "12%", "") is None and L(PRG, "", "12.0") is None
       and L(PRG, "12%", None) is None and L(PRG, None, None) is None)
    ok("P-6 수가 아닌 답·쉼표·범위 밖은 사람 몫", L(PRG, "12%!", "12") is None and L(PRG, "1,5", "15") is None
       and L(PRG, "101%", "101.0") is None and L(PRG, "-5", "-5") is None and L(PRG, "abc", "abc") is None)
    ok("P-7 묶음의 이미지가 같은 수여야 닫힌다", OL.auto_label_rows(PRG, [("12%", "12.0"), ("12", "12.0")]) == "12%"
       and OL.auto_label_rows(PRG, [("12%", "12.0"), ("13%", "13.0")]) is None
       and OL.auto_label_rows(PRG, [("12%", "12.0"), ("12%", "")]) is None)
    ok("P-8 gemini 답만 보는 옛 auto_label 은 숫자 자리를 안 닫는다(로컬 없이는 못 판단)", OL.auto_label(PRG, ["12%"]) is None)
    ok("P-9 자동 닫기 사이트에 속한다(쓸기 대상) · 이름 목록은 비어 있다", PRG in OL.OCR_AUTO_SITES and not OL.OCR_AUTO_SITES[PRG])


def t_map():
    base = ["홍옥의 섬", "어비스 회랑", "라 미렌 요새 남쪽 잔해"]
    new = ["성소 감시초소", "파괴된 잔해", "죽은 자의 침실(극한)", "죽은 자의 침실(절망)", "죽은 자의 침실(지옥)", "죽은 자의 침실(파멸)"]
    ok("M-1 예전 이름 그대로 닫힌다", all(L(MAP, n) == n for n in base))
    ok("M-2 ★추가한 6개 이름이 닫힌다★", all(L(MAP, n) == n for n in new), str([n for n in new if L(MAP, n) != n]))
    ok("M-3 ★띄어쓰기만 다르면 같은 이름(스냅 0편집)★", L(MAP, "성소감시초소") == "성소 감시초소" and L(MAP, "홍옥의섬") == "홍옥의 섬"
       and L(MAP, "홍옥의  섬") == "홍옥의 섬" and L(MAP, "죽은자의침실(극한)") == "죽은 자의 침실(극한)")
    ok("M-4 ★잊힌 저장소 다섯 난이도가 닫힌다(사람 라벨 71건, 제미나이 늘 글자 그대로)★",
       all(L(MAP, "잊힌 저장소(%s)" % d) == "잊힌 저장소(%s)" % d for d in ("절망", "극한", "파멸", "어려움", "지옥")))
    ok("M-5 추가분은 상수 하나로 관리 · 총 27개(예전 16 + 추가 11)", len(OL.OCR_AUTO_MAP_EXTRA) == 11 and len(OL.OCR_AUTO_SITES[MAP]) == 27)


def t_snap():
    seen = {"라 미렌 요세 남쪽 잔해": "라 미렌 요새 남쪽 잔해", "라 미렌 요세 북쪽 잔해": "라 미렌 요새 북쪽 잔해", "베르테론 요세 폐허": "베르테론 요새 폐허",
            "루쓰레인 구릉지": "루브레인 구릉지", "홍옥의 성": "홍옥의 섬", "아을라우 부락": "아울라우 부락", "파괴된 잔애": "파괴된 잔해"}
    ok("S-1 ★이력에서 제미나이가 한 글자 바꿔 읽은 꼴(%d종)이 알려진 이름으로 닫힌다★" % len(seen), all(L(MAP, g) == w for g, w in seen.items()),
       str([(g, L(MAP, g)) for g, w in seen.items() if L(MAP, g) != w]))
    ties = ["엘둔강 중류", "엘딘강 중류", "엘뒨강 중류", "엘툰강 중류"]
    ok("S-2 ★엘듄강/엘룬강 둘 다 1편집이면 사람 몫(스냅 목록만 보면 한쪽으로 잘못 닫혔다)★", all(L(MAP, g) is None for g in ties),
       str([(g, L(MAP, g)) for g in ties]))
    ok("S-3 기준 목록에만 있는 이름은 정확히 맞아도 안 닫는다(근거 없음)", L(MAP, "엘룬강 중류") is None and L(MAP, "다르타스 평원 서부") is None
       and L(MAP, "큐브") is None and L(MAP, "불멸의 섬") is None)
    ok("S-4 두 글자 이상 다르면 사람 몫(낭떠러지·진영·꼬리·빈 답·영문·두 이름)",
       all(L(MAP, g) is None for g in ("갈라진 낭떠러지", "라미렌 요새 북쪽 진영", "홍옥의 섬입니다", "", "Hongok", "홍옥의 섬 / 정령의 섬")))
    ok("S-5 묶음 안에서 이름이 갈리면 사람 몫 · 같은 이름으로 모이면 닫힌다",
       OL.auto_label_rows(MAP, [("라 미렌 요세 남쪽 잔해", ""), ("홍옥의 섬", "")]) is None
       and OL.auto_label_rows(MAP, [("라 미렌 요세 남쪽 잔해", ""), ("라 미렌 요새 남쪽 잔해", "")]) == "라 미렌 요새 남쪽 잔해")
    ok("S-6 스냅은 맵 이름 자리에만 — 다른 닫힌 목록은 글자 그대로", OL.OCR_AUTO_SNAP == {MAP} and L(DK, "죽은 자의 침실(극한)") == "죽은 자의 침실(극한)"
       and L(DIF, "극하") is None and L(DK, "죽은 자의 침식(극한)") is None)


async def t_flow():
    r = sub(DK_RAW, "죽은 자의 침실(극한)", far(1))
    ok("F-1 던전 이름 제출 → auto(정식 표기)", (await _st(r["cluster"])) == ("auto", "죽은 자의 침실(극한)"), str(r))
    r = sub(OBJ_RAW, "보스를 처치하라", far(2))
    ok("F-2 오독 «보스를 처치하라» 제출 → auto(보스를처치하기)", (await _st(r["cluster"])) == ("auto", "보스를처치하기"), str(r))
    r2 = sub(OBJ_RAW, "지지하기", far(3))
    ok("F-3 먼 답은 pending(사람 대기열)", (await _st(r2["cluster"]))[0] == "pending", str(r2))
    r3 = sub(PRG_RAW, "12%", far(4), local="12.0")
    ok("F-4 진행도: 제미나이·로컬이 수로 같으면 auto(«12%»)", (await _st(r3["cluster"])) == ("auto", "12%"), str(r3))
    r4 = sub(PRG_RAW, "12%", far(5), local="")
    r5 = sub(PRG_RAW, "12%", far(6), local="13.0")
    ok("F-5 로컬이 비었거나 다르면 pending", (await _st(r4["cluster"]))[0] == "pending" and (await _st(r5["cluster"]))[0] == "pending",
       "%s %s" % (r4, r5))
    r6 = sub(MAP_RAW, "성소 감시초소", far(7))
    ok("F-6 새 맵 이름 제출 → auto", (await _st(r6["cluster"])) == ("auto", "성소 감시초소"), str(r6))
    near = "%016x" % (int(far(4), 16) ^ 1)
    n2 = sub(PRG_RAW, "12%", near, local="14.0")
    ok("F-7 ★auto 묶음에 어긋난 수가 붙으면 같은 묶음이 pending 으로★",
       n2["cluster"] == r3["cluster"] and (await _st(r3["cluster"]))[0] == "pending", "%s %s" % (n2, await _st(r3["cluster"])))
    r7 = sub(MAP_RAW, "라 미렌 요세 남쪽 잔해", far(9))
    ok("F-6b 한 글자 오독 제출 → auto(알려진 이름으로)", (await _st(r7["cluster"])) == ("auto", "라 미렌 요새 남쪽 잔해"), str(r7))
    r8 = sub(MAP_RAW, "엘둔강 중류", far(10))
    ok("F-6c 동률(엘듄강·엘룬강) 오독은 pending", (await _st(r8["cluster"]))[0] == "pending", str(r8))
    # 쓸기: 대기 중인 묶음을 규칙으로 닫는다(배포 때 한 번 도는 경로)
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE ocr_cluster SET status='pending', label=NULL, labeled_at=NULL WHERE id IN (?,?)",
                        (r["cluster"], r6["cluster"]))
        await c.commit()
    dry = await OL.auto_sweep(dry=True)
    st_dry = [(await _st(r["cluster"]))[0], (await _st(r6["cluster"]))[0]]
    ok("F-8 쓸기 dry 는 세기만 하고 바꾸지 않는다", dry["closed"] >= 2 and st_dry == ["pending", "pending"], "%s %s" % (dry, st_dry))
    real = await OL.auto_sweep()
    ok("F-9 쓸기: 대기 묶음이 새 규칙으로 닫힌다", (await _st(r["cluster"])) == ("auto", "보스를처치하기")
       and (await _st(r6["cluster"])) == ("auto", "성소 감시초소") and real["closed"] >= 2, "%s" % real)
    ok("F-10 쓸기는 먼 답 묶음을 안 닫는다", (await _st(r2["cluster"]))[0] == "pending" and (await _st(r4["cluster"]))[0] == "pending")
    # 사람이 한 번이라도 손댄 묶음은 안 건드린다
    h = sub(OBJ_RAW, "보스를 처치하자", far(8))
    async with aiosqlite.connect(db.DB_PATH) as c:
        await c.execute("UPDATE ocr_cluster SET status='pending', label=NULL, labeled_at=NULL WHERE id=?", (h["cluster"],))
        await c.execute("INSERT INTO ocr_hist(tenant, cluster_id, prev_status, prev_label, new_status, new_label, at) "
                        "VALUES('main', ?, 'pending', NULL, 'labeled', 'x', 1.0)", (h["cluster"],))
        await c.commit()
    await OL.auto_sweep()
    ok("F-11 사람이 손댄 적 있는 묶음은 쓸기가 안 건드린다", (await _st(h["cluster"]))[0] == "pending")


run_all([t_dungeon, t_objective, t_progress, t_map, t_snap, t_flow])
finish("test_ocr_auto_436b", MIN_CHECKS)
