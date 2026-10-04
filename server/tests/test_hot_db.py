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

MIN_CHECKS = 25
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


def test_all():
    run_all([t_routing_on, t_routing_off, t_empty_hot_start, t_fallback, t_report, t_no_cross_file_sql])
    finish("test_hot_db", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
