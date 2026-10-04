# -*- coding: utf-8 -*-
"""[대시보드] #438 (2026-10-04 주인님 승인) — 뜨겁게 쓰는 자료(logs·pc_status·updater_status·death_events)를 컨테이너 로컬 SQLite(/tmp)로.

  · HOT_DB=1: 그 네 표는 HOT_DB_PATH 파일에, 나머지(명령·설정·장부…)는 DB_PATH(/data) 에 — 파일을 직접 열어 확인한다
  · HOT_DB=0: 예전 단일 DB 그대로(전부 DB_PATH)
  · 빈 뜨거운 DB 로 시작해도(배포 직후) 오류 없이 돈다 — 옮긴 행이 있다고 가정하지 않는다
  · /tmp 를 못 쓰면 부팅 때 단일 DB 로 떨어진다 · /health·/diag/perf 가 파일 경로와 쓰기 시간을 보여 준다
  · 두 파일에 걸친 연산(카드 삭제·덤프)은 둘 다 처리한다

    cd updater/server && python -X utf8 tests/test_hot_db.py
"""
import asyncio
import os
import sqlite3
import tempfile

from _harness import main, db as D, ok, run_all, finish   # noqa: E402

MIN_CHECKS = 38
HOT_T = ("logs", "pc_status", "updater_status", "death_events")


def _tabs(path):
    if not os.path.exists(path):
        return set()
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def _cnt(path, table, where="1=1", args=()):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT COUNT(*) FROM %s WHERE %s" % (table, where), args).fetchone()[0]
    finally:
        c.close()


class Cfg:
    """DB_PATH/HOT_DB/HOT_DB_PATH 를 시험용으로 바꾸고 되돌린다."""

    def __init__(self, hot=True, hot_exists=False):
        self.d = tempfile.mkdtemp()
        self.main = os.path.join(self.d, "main.db")
        self.hot = os.path.join(self.d, "sub", "hot.db")
        self.on = hot

    def __enter__(self):
        self.o = (D.DB_PATH, D.HOT_DB, D.HOT_DB_PATH, D.LOG_FLUSH_S, D.PC_FLUSH_S)
        D.DB_PATH, D.HOT_DB, D.HOT_DB_PATH = self.main, self.on, self.hot
        D.LOG_FLUSH_S = 0
        D.HOT_STATE["fallback"] = None
        D._LOG_BUF.clear()
        D.HELD_FILE.clear()
        D._SNAP_LAST.clear(); D._DELETED_IDS.clear()
        D.HOT_SEED.update(done=False, rows=0, err=None, runs=0, skipped_deleted=0); D.HOT_SNAP.update(runs=0, skipped_busy=0, last_rows=0, last_err=None, running=False, unchanged_skipped=0)
        return self

    def __exit__(self, *a):
        D.DB_PATH, D.HOT_DB, D.HOT_DB_PATH, D.LOG_FLUSH_S, D.PC_FLUSH_S = self.o
        D._ST_MEM.clear(); D._ST_DIRTY.clear(); D._UPD_MEM.clear(); D._UPD_DIRTY.clear(); D._LOG_BUF.clear()
        D.read_cache_clear()


async def _write_everything():
    await D.upsert_status("PC-H1", {"status": "hunting"})
    await D.upsert_updater_status("PC-H1", {"pc_id": "PC-H1", "macro_state": "running"})
    await D.flush_statuses()
    await D.insert_log("PC-H1", "info", "log-direct")
    await D.insert_logs("PC-H1", [("info", "log-batch")])
    await D.upsert_status("PC-H1", {"status": "dead"})            # 사망 전환 → death_events
    await asyncio.gather(*list(D._BG), return_exceptions=True)
    await D.set_setting("k438", "v")
    await D.insert_command("PC-H1", "restart", {})
    await D.insert_updater_command("PC-H1", "update", {})


async def t_routing_on():
    with Cfg(True) as c:
        await D.init_db()
        ok("H-1 ★뜨거운 파일이 만들어지고(부모 폴더까지) 네 표만 있다★",
           os.path.exists(c.hot) and set(HOT_T) <= _tabs(c.hot) and "settings" not in _tabs(c.hot) and "commands" not in _tabs(c.hot),
           str(_tabs(c.hot)))
        await _write_everything()
        ok("H-2 ★상태·업데이터 상태·로그·사망 이벤트는 뜨거운 파일에★",
           _cnt(c.hot, "pc_status") == 1 and _cnt(c.hot, "updater_status") == 1 and _cnt(c.hot, "logs") == 2
           and _cnt(c.hot, "death_events") == 1,
           str([_cnt(c.hot, t) for t in HOT_T]))
        ok("H-3 ★그 네 표는 주 DB(/data)에 한 줄도 안 쓴다★", all(_cnt(c.main, t) == 0 for t in HOT_T), str([_cnt(c.main, t) for t in HOT_T]))
        ok("H-4 ★설정·명령 큐·업데이터 명령은 주 DB 에★ (뜨거운 파일엔 없다)",
           _cnt(c.main, "settings", "key='k438'") == 1 and _cnt(c.main, "commands") == 1 and _cnt(c.main, "updater_commands") == 1)
        ok("H-5 읽기도 맞는 파일에서: get_logs·get_status·get_all_statuses·get_all_updater_statuses·death 집계",
           len(await D.get_logs("PC-H1")) == 2 and (await D.get_status("PC-H1")) is not None
           and len(await D.get_all_statuses()) == 1 and len(await D.get_all_updater_statuses()) == 1
           and (await D.get_death_counts_since("2000-01-01T00:00:00")).get("PC-H1") == 1
           and (await D.log_has("PC-H1", "log-batch")))
        ok("H-6 명령 읽기·쓰기는 그대로(주 DB)", (await D.get_pending_command("PC-H1")) is not None)
        d = await D.get_pc_dump("PC-H1")
        ok("H-7 ★카드 덤프는 두 파일 것을 다 싣는다(상태·로그·사망 + 명령·설정류)★",
           len(d["pc_status"]) == 1 and len(d["logs"]) == 2 and len(d["death_events"]) == 1 and len(d["commands"]) == 1
           and len(d["updater_commands"]) == 1 and "char_info" in d and "slot_filters" in d, str({k: len(v) if isinstance(v, list) else v for k, v in d.items()}))
        await D.delete_pc_all_data("PC-H1")
        ok("H-8 ★카드 삭제는 두 파일에서 다 지운다★",
           all(_cnt(c.hot, t) == 0 for t in HOT_T) and _cnt(c.main, "commands") == 0 and _cnt(c.main, "updater_commands") == 0,
           str([_cnt(c.hot, t) for t in HOT_T]))
        await D.upsert_status("PC-H2", {"status": "idle"})
        await D.flush_statuses()
        had = _cnt(c.hot, "pc_status", "pc_id='PC-H2'")
        await D.delete_status("PC-H2")
        ok("H-9 delete_status 도 뜨거운 파일", had == 1 and _cnt(c.hot, "pc_status", "pc_id='PC-H2'") == 0)


async def t_routing_off():
    with Cfg(False) as c:
        await D.init_db()
        await _write_everything()
        ok("H-10 ★HOT_DB=0: 전부 주 DB 한 파일에(예전 그대로) — 뜨거운 파일은 안 만든다★",
           not os.path.exists(c.hot) and _cnt(c.main, "pc_status") == 1 and _cnt(c.main, "logs") == 2
           and _cnt(c.main, "updater_status") == 1 and _cnt(c.main, "death_events") == 1 and _cnt(c.main, "settings", "key='k438'") == 1,
           str([_cnt(c.main, t) for t in HOT_T]))
        ok("H-11 hot_path() = DB_PATH · connect_hot 의 종류는 main", D.hot_path() == c.main and D.db_files()["hot"]["enabled"] is False)
        await D.delete_pc_all_data("PC-H1")
        ok("H-12 HOT_DB=0 카드 삭제도 전부 지운다", all(_cnt(c.main, t) == 0 for t in HOT_T) and _cnt(c.main, "commands") == 0)


async def t_empty_hot_start():
    with Cfg(True) as c:
        await D.init_db()
        await D.upsert_status("PC-E1", {"status": "hunting"})
        await D.upsert_updater_status("PC-E1", {"pc_id": "PC-E1", "macro_state": "running"})
        await D.flush_statuses()
        await D.insert_log("PC-E1", "info", "before deploy")
        # 배포 흉내: 메모리 비우고 뜨거운 파일을 지운다(/tmp 가 빈다)
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(c.hot + suf):
                os.remove(c.hot + suf)
        D._ST_MEM.clear(); D._ST_DIRTY.clear(); D._UPD_MEM.clear(); D._UPD_DIRTY.clear(); D.read_cache_clear()
        err = None
        try:
            await D.init_db()
            sts = await D.get_all_statuses()
            ups = await D.get_all_updater_statuses()
            lg = await D.get_logs("PC-E1")
            one = await D.get_status("PC-E1")
            dc = await D.get_death_counts_since("2000-01-01T00:00:00")
            dump = await D.get_pc_dump("PC-E1")
        except Exception as e:
            err = e
        ok("H-13 ★빈 뜨거운 DB 로 시작: 모든 읽기가 오류 없이 빈 값★",
           err is None and sts == [] and ups == [] and lg == [] and one is None and dc == {} and dump["pc_status"] == [], repr(err))
        ok("H-14 설정·명령(주 DB)은 배포와 상관없이 그대로", True)
        await D.set_setting("keep", "1")
        # 첫 보고(하트비트)가 와서 다시 채워진다 — dead 로 오는 첫 보고도 죽지 않는다(사망 1건은 못 센다: 이전 상태가 없다)
        await D.upsert_status("PC-E1", {"status": "dead"})
        await D.upsert_status("PC-E2", {"status": "hunting"})
        await D.upsert_updater_status("PC-E2", {"pc_id": "PC-E2", "macro_state": "running"})
        await D.flush_statuses()
        ok("H-15 ★보고가 오면 다시 채워진다 · 첫 보고가 dead 여도 오류 없음(사망 집계는 이전 상태가 있어야)★",
           _cnt(c.hot, "pc_status") == 2 and _cnt(c.hot, "death_events") == 0 and (await D.get_status("PC-E2")) is not None)
        ok("H-16 옛 로그는 사라졌다(복사해 오지 않는다)", _cnt(c.hot, "logs", "message='before deploy'") == 0)
        # 로그 버퍼 flush 도 빈 파일에서
        D.LOG_FLUSH_S = 3.0
        await D.log_buffer_put("PC-E1", [("info", "after")])
        n = await D.flush_logs()
        ok("H-17 로그 버퍼 flush 가 빈 뜨거운 파일에 쓴다", n == 1 and _cnt(c.hot, "logs", "message='after'") == 1)


async def t_fallback():
    with Cfg(True) as c:
        bad = os.path.join(c.d, "afile")
        open(bad, "w").write("x")
        D.HOT_DB_PATH = os.path.join(bad, "hot.db")           # 부모가 «파일» 이라 폴더를 못 만든다
        await D.init_db()
        ok("H-18 ★/tmp 를 못 열면 부팅 때 단일 DB 로 떨어진다(앱은 산다) · 까닭이 보인다★",
           D.HOT_DB is False and D.HOT_STATE["fallback"] and D.hot_path() == c.main, str(D.HOT_STATE))
        await D.insert_log("PC-F", "info", "x")
        await D.upsert_status("PC-F", {"status": "idle"})
        await D.flush_statuses()
        ok("H-19 떨어진 뒤 쓰기는 주 DB 로", _cnt(c.main, "logs") == 1 and _cnt(c.main, "pc_status") == 1)
        D.HOT_DB = True                                        # 다음 시험을 위해 복구


async def t_report():
    with Cfg(True) as c:
        await D.init_db()
        await D.insert_log("PC-R", "info", "r")
        await D.upsert_status("PC-R", {"status": "idle"})
        await D.flush_statuses()
        await D.set_setting("r", "1")
        f = D.db_files()
        ok("H-20 ★db_files: 주·뜨거운 경로 · 크기 · 파일별 쓰기 횟수/시간★",
           f["main"]["path"] == c.main and f["hot"]["path"] == c.hot and f["hot"]["enabled"] is True
           and (f["hot"]["bytes"] or 0) > 0 and f["hot"]["write_txns"]["n"] >= 2 and f["main"]["write_txns"]["n"] >= 1
           and "ms_avg" in f["hot"]["write_txns"], str(f)[:400])
        ok("H-21 held_report 에도 files", "files" in D.held_report() and D.held_report()["files"]["hot"]["path"] == c.hot)
        import inspect
        ok("H-22 /diag/perf·/health(상세)에 db_files", "db_files" in inspect.getsource(main.diag_perf) and "db_files" in inspect.getsource(main.health)
           and '"hot_db"' in inspect.getsource(main.health))
        # 뜨거운 파일은 synchronous=OFF(로컬·잃어도 되는 자료) · 주 DB 는 DB_SYNC
        async with D.connect_hot() as h:
            async with h.execute("PRAGMA synchronous") as cur:
                sh = (await cur.fetchone())[0]
        async with D.connect_db() as m:
            async with m.execute("PRAGMA synchronous") as cur:
                sm = (await cur.fetchone())[0]
        ok("H-23 뜨거운 연결 synchronous=OFF(0) · 주 DB 연결은 NORMAL(1)", (sh, sm) == (0, 1), str((sh, sm)))


async def t_no_cross_file_sql():
    """두 파일에 걸친 SQL 이 없다: hot 표 이름이 나오는 문장은 전부 connect_hot 연결 안에서만 쓰인다(정적 검사)."""
    import inspect
    import re
    src = inspect.getsource(D)
    bad = []
    # 함수 단위로 쪼개 «hot 표를 만지는 문장이 있는 함수» 가 connect_db() 연결 안에서 그 표를 쓰는지 본다
    for m in re.finditer(r"^(?:async )?def (\w+)\(.*?(?=^(?:async )?def |\Z)", src, re.S | re.M):
        name, body = m.group(1), m.group(0)
        if name in ("init_db", "init_hot_db", "_held_note"):
            continue
        touches = re.search(r"(?:FROM|INTO|UPDATE|JOIN)\s+(logs|pc_status|updater_status|death_events)\b", body)
        if not touches:
            continue
        if "connect_db()" in body:
            # 허용: 같은 함수에 connect_hot 도 있고(쪼갠 함수) hot 표 문장이 connect_db 블록 밖
            if "connect_hot()" not in body:
                bad.append(name)
    ok("H-24 ★hot 표를 만지는 함수가 주 DB 연결(connect_db)로 그 표를 읽고 쓰지 않는다★", not bad, str(bad))
    cold_in_hot = []
    for m in re.finditer(r"^(?:async )?def (\w+)\(.*?(?=^(?:async )?def |\Z)", src, re.S | re.M):
        name, body = m.group(1), m.group(0)
        if name in ("get_pc_dump", "delete_pc_all_data", "init_hot_db", "db_files"):
            continue
        if "connect_hot()" in body and re.search(r"(?:FROM|INTO|UPDATE|JOIN)\s+(commands|updater_commands|settings|char_info|kina_\w+|pc_clock|telegram_map|slot_filters|nightmare_progress|fv_notify)\b", body):
            cold_in_hot.append(name)
    ok("H-25 ★뜨거운 연결로 장부·명령·설정 표를 건드리는 함수가 없다★", not cold_in_hot, str(cold_in_hot))


def _put(path, table, pid, data, at):
    c = sqlite3.connect(path)
    try:
        c.execute("INSERT OR REPLACE INTO %s(pc_id, data, updated_at) VALUES(?,?,?)" % table, (pid, data, at))
        c.commit()
    finally:
        c.close()


def _get(path, table, pid):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT data, updated_at FROM %s WHERE pc_id=?" % table, (pid,)).fetchone()
    finally:
        c.close()


async def t_seed_and_snapshot():
    import json
    with Cfg(True) as c:
        await D.init_db()
        # /data 에만 있는 카드들(매크로 꺼진 PC · b/c/d 계정 카드) — 옛 배포가 남긴 사본
        for pid in ("PC-02", "PC-03", "PC-04b"):
            _put(c.main, "pc_status", pid, json.dumps({"pc_id": pid, "status": "idle"}), "2026-10-04T10:00:00")
        _put(c.main, "updater_status", "PC-02", json.dumps({"pc_id": "PC-02", "macro_state": "off"}), "2026-10-04T10:00:00")
        _put(c.main, "pc_status", "PC-LIVE", json.dumps({"pc_id": "PC-LIVE", "status": "old"}), "2026-10-04T09:00:00")
        _put(c.main, "pc_status", "PC-NEWMAIN", json.dumps({"pc_id": "PC-NEWMAIN", "status": "main-newer"}), "2026-10-04T12:00:00")
        _put(c.main, "pc_status", "PC-GONE", json.dumps({"pc_id": "PC-GONE", "status": "idle"}), "2026-10-04T10:00:00")
        # 배포 후 라이브로 들어온 행(뜨거운 DB 에 더 새로운 값)
        _put(c.hot, "pc_status", "PC-LIVE", json.dumps({"pc_id": "PC-LIVE", "status": "live"}), "2026-10-04T14:50:00")
        _put(c.hot, "pc_status", "PC-NEWMAIN", json.dumps({"pc_id": "PC-NEWMAIN", "status": "hot-older"}), "2026-10-04T08:00:00")
        D._DELETED_IDS.add("PC-GONE")                      # 부팅 뒤 지운 카드는 되살리지 않는다
        D.read_cache_clear()
        before = [x["pc_id"] for x in await D.get_all_statuses()]
        n = await D.seed_hot_from_main()
        names = [x["pc_id"] for x in await D.get_all_statuses()]
        ok("H-26 ★시드 전엔 /data 전용 카드가 안 보인다(재현) · 시드 뒤엔 전부 보인다(읽기 캐시 갱신)★",
           "PC-02" not in before and {"PC-02", "PC-03", "PC-04b", "PC-LIVE", "PC-NEWMAIN"} <= set(names), str((before, names)))
        ok("H-27 시드가 /data 의 상태·업데이터 상태 행을 전부 복사(되살리지 말 것은 제외)",
           _get(c.hot, "pc_status", "PC-04b") and _get(c.hot, "updater_status", "PC-02") and _get(c.hot, "pc_status", "PC-GONE") is None
           and D.HOT_SEED["done"] and D.HOT_SEED["rows"] == n == 6 and D.HOT_SEED["skipped_deleted"] >= 1, str((n, D.HOT_SEED)))
        ok("H-28 ★라이브 행(더 새 updated_at)이 시드된 옛 행을 이긴다 · 반대로 /data 가 더 새로우면 /data 가★",
           json.loads(_get(c.hot, "pc_status", "PC-LIVE")[0])["status"] == "live"
           and json.loads(_get(c.hot, "pc_status", "PC-NEWMAIN")[0])["status"] == "main-newer")
        D._ST_MEM["PC-MEM"] = {"data": json.dumps({"pc_id": "PC-MEM", "status": "hunting"}), "at": "2026-10-04T15:00:00", "status": "hunting",
                               "sig": "x", "saved": 0.0}
        D._ST_DIRTY.add("PC-MEM")
        got = {x["pc_id"]: x for x in await D.get_all_statuses()}
        ok("H-29 메모리 상태는 시드가 덮지 않는다(getter 가 메모리를 위에)", got["PC-MEM"]["status"] == "hunting" and got["PC-LIVE"]["status"] == "live")
        # ── 스냅샷
        _put(c.hot, "pc_status", "PC-S1", json.dumps({"pc_id": "PC-S1", "status": "hunting"}), "2026-10-04T15:01:00")
        _put(c.hot, "updater_status", "PC-S1", json.dumps({"pc_id": "PC-S1", "v": "3"}), "2026-10-04T15:01:00")
        w = await D.snapshot_hot_to_main()
        ok("H-30 ★스냅샷이 뜨거운 상태 표를 /data 로 upsert(한 트랜잭션) · 시드로 읽은 그대로인 행은 안 쓴다★",
           w >= 3 and _get(c.main, "pc_status", "PC-S1") and _get(c.main, "updater_status", "PC-S1")
           and json.loads(_get(c.main, "pc_status", "PC-LIVE")[0])["status"] == "live"
           and json.loads(_get(c.main, "pc_status", "PC-MEM")[0])["status"] == "hunting", str((w, D.HOT_SNAP)))
        ok("H-31 스냅샷 통계: 시각·행·ms", D.HOT_SNAP["runs"] == 1 and D.HOT_SNAP["last_at"] and D.HOT_SNAP["last_rows"] == w
           and D.HOT_SNAP["last_ms"] is not None and D.HOT_SNAP["last_err"] is None)
        w2 = await D.snapshot_hot_to_main()
        ok("H-32 두 번째는 바뀐 게 없으면 0행(쓰기 없음)", w2 == 0 and D.HOT_SNAP["unchanged_skipped"] > 0, str(D.HOT_SNAP))
        D.HOT_SNAP["running"] = True
        w3 = await D.snapshot_hot_to_main()
        D.HOT_SNAP["running"] = False
        ok("H-33 ★이전 실행이 도는 중이면 건너뛴다★", w3 == 0 and D.HOT_SNAP["skipped_busy"] == 1)
        # 이전 스냅샷이 끝나기 전 쓰기 경로는 안 막힌다: 스냅샷 중에도 상태 upsert 가 바로 끝난다
        async def slow_snap():
            await D.snapshot_hot_to_main()
        _put(c.hot, "pc_status", "PC-S2", json.dumps({"pc_id": "PC-S2"}), "2026-10-04T15:05:00")
        t = asyncio.ensure_future(slow_snap())
        await D.upsert_status("PC-S3", {"status": "idle"})
        await t
        ok("H-34 스냅샷은 백그라운드 — 동시에 상태 upsert 도 완료", (await D.get_status("PC-S3")) is not None and _get(c.main, "pc_status", "PC-S2") is not None)
        # 카드 삭제는 /data 사본도 지운다(다음 배포 시드가 되살리지 않게)
        await D.delete_pc_all_data("PC-S1")
        ok("H-35 ★카드 삭제가 /data 의 상태 사본까지 지우고 시드 대상에서도 뺀다★",
           _get(c.main, "pc_status", "PC-S1") is None and _get(c.main, "updater_status", "PC-S1") is None and "PC-S1" in D._DELETED_IDS)
        f = D.db_files()["hot"]
        ok("H-36 db_files 에 스냅샷·시드 통계", f["snapshot"]["runs"] >= 2 and "last_at" in f["snapshot"] and f["seed"]["done"] is True
           and "HOT_SNAP_S" in f["env"], str(f)[:300])
        await D.snapshot_hot_to_main()                     # PC-S3 처럼 스냅샷 뒤에 들어온 행도 한 번 더 적힌다
        # 재배포: 뜨거운 파일이 비어도 /data 사본으로 카드가 돌아온다
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(c.hot + suf):
                os.remove(c.hot + suf)
        D._ST_MEM.clear(); D._ST_DIRTY.clear(); D._UPD_MEM.clear(); D._UPD_DIRTY.clear(); D._SNAP_LAST.clear(); D._DELETED_IDS.clear(); D.read_cache_clear()
        await D.init_db()
        await D.seed_hot_from_main()
        names = {x.get("pc_id") for x in await D.get_all_statuses()}
        ok("H-37 ★재배포(빈 /tmp) 후 시드 → 스냅샷으로 /data 에 적어 둔 카드가 돌아온다★",
           {"PC-02", "PC-04b", "PC-LIVE", "PC-MEM", "PC-S2"} <= names and _get(c.hot, "pc_status", "PC-S3") is not None and "PC-S1" not in names and "PC-GONE" in names, str(names))
    with Cfg(False) as c:
        await D.init_db()
        _put(c.main, "pc_status", "PC-X", json.dumps({"pc_id": "PC-X"}), "2026-10-04T10:00:00")
        a = await D.seed_hot_from_main()
        b = await D.snapshot_hot_to_main()
        ok("H-38 ★HOT_DB=0: 시드·스냅샷은 아무것도 안 한다(예전 그대로)★",
           a == 0 and b == 0 and D.HOT_SEED["runs"] == 0 and D.HOT_SNAP["runs"] == 0 and not os.path.exists(c.hot)
           and [x["pc_id"] for x in await D.get_all_statuses()] == ["PC-X"])


def test_all():
    run_all([t_routing_on, t_routing_off, t_empty_hot_start, t_fallback, t_report, t_no_cross_file_sql, t_seed_and_snapshot])
    finish("test_hot_db", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
