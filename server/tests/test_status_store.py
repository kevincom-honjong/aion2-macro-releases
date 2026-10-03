# -*- coding: utf-8 -*-
"""[대시보드] #432-d — 상태 보고는 메모리가 정본, DB 에는 모아서 늦게 (2026-10-04).

실측: Railway 볼륨 쓰기/fsync 처리량이 한 자리(~100~200KB/s)라 상태 보고마다 커밋하면 서로를 기다렸다
(ws_msg_status 평균 0.2~0.9초 · 최대 19초). 고친 것: upsert_status 는 메모리에 쓰고 바뀐 행만 PC_FLUSH_S 마다 한 트랜잭션.
"""
import asyncio
import json
import os
import sqlite3
import tempfile

from _harness import main, ok, Req, run_all, finish   # noqa: E402
import database as D                                   # noqa: E402

MIN_CHECKS = 22


def _rows(sql, args=()):
    c = sqlite3.connect(D.DB_PATH)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


class _Fresh:
    def __enter__(self):
        self.old = (D.DB_PATH, D.PC_FLUSH_S, D.PC_PERSIST_MAX_S)
        D.DB_PATH = os.path.join(tempfile.mkdtemp(), "s.db")
        D.PC_FLUSH_S, D.PC_PERSIST_MAX_S = 10.0, 60.0
        return self

    def __exit__(self, *a):
        D.DB_PATH, D.PC_FLUSH_S, D.PC_PERSIST_MAX_S = self.old
        D._ST_MEM.clear()
        D._ST_DIRTY.clear()


async def t_store():
    with _Fresh():
        await D.init_db()
        await D.upsert_status("PC-A", {"status": "idle", "last_active": "2026-10-04T00:00:01"})
        ok("S432d-a ★보고는 DB 를 안 건드린다 — 표에 행이 아직 없다★", _rows("SELECT COUNT(*) FROM pc_status")[0][0] == 0)
        st = await D.get_status("PC-A")
        al = await D.get_all_statuses()
        ok("S432d-b 그래도 읽기(get_status·get_all_statuses)는 방금 보고를 본다",
           st and st["status"] == "idle" and [x["pc_id"] if "pc_id" in x else x.get("status") for x in al] and al[0]["status"] == "idle" and "_updated_at" in al[0], str(al)[:200])

        real = D.aiosqlite.connect
        n = [0]

        def counting(*a, **k):
            n[0] += 1
            return real(*a, **k)

        for i in range(10):
            await D.upsert_status("PC-%d" % i, {"status": "hunting", "i": i})
        D.aiosqlite.connect = counting
        try:
            w = await D.flush_statuses()
        finally:
            D.aiosqlite.connect = real
        ok("S432d-c ★11개 행을 연결 1번·커밋 1번으로 저장한다★", w == 11 and n[0] == 1 and _rows("SELECT COUNT(*) FROM pc_status")[0][0] == 11, "%s %s" % (w, n[0]))
        ok("S432d-d 저장 뒤 dirty 비어 있고 flush 통계가 센다", not D._ST_DIRTY and D.ST_STATS["flushes"] >= 1 and D.ST_STATS["rows"] >= 11, str(D.st_stats()))

        # 안 바뀐 보고(last_active 만 다름)는 dirty 로 안 올린다
        before = D.ST_STATS["unchanged_skipped"]
        await D.upsert_status("PC-A", {"status": "idle", "last_active": "2026-10-04T00:00:31"})
        ok("S432d-e last_active 만 바뀐 보고는 저장 대상이 아니다(건너뜀 +1) — 그래도 메모리는 새 값",
           D.ST_STATS["unchanged_skipped"] == before + 1 and "PC-A" not in D._ST_DIRTY
           and (await D.get_status("PC-A"))["last_active"] == "2026-10-04T00:00:31")
        await D.upsert_status("PC-A", {"status": "paused", "last_active": "2026-10-04T00:01:01"})
        ok("S432d-f ★값이 바뀐 보고(일시정지)는 다음 flush 에 저장 대상★", "PC-A" in D._ST_DIRTY)
        await D.flush_statuses()
        row = json.loads(_rows("SELECT data FROM pc_status WHERE pc_id='PC-A'")[0][0])
        ok("S432d-g 저장된 행이 최신 값(paused)", row["status"] == "paused", str(row))
        D.PC_PERSIST_MAX_S = 0.0
        await D.upsert_status("PC-A", {"status": "paused", "last_active": "2026-10-04T00:01:31"})
        ok("S432d-h 안 바뀌어도 PC_PERSIST_MAX_S 가 지나면 저장 대상(last_active 가 영영 안 저장되지 않게)", "PC-A" in D._ST_DIRTY)
        D.PC_PERSIST_MAX_S = 60.0
        await D.flush_statuses()

        # 사망 전환은 바로 DB + death_events
        await D.upsert_status("PC-A", {"status": "dead"})
        ok("S432d-i ★사망 전환은 바로 DB 에(행 + death_events 1건) — flush 를 기다리지 않는다★",
           json.loads(_rows("SELECT data FROM pc_status WHERE pc_id='PC-A'")[0][0])["status"] == "dead"
           and _rows("SELECT COUNT(*) FROM death_events WHERE pc_id='PC-A'")[0][0] == 1 and "PC-A" not in D._ST_DIRTY)
        await D.upsert_status("PC-A", {"status": "dead"})
        ok("S432d-j 계속 dead 인 반복 보고는 사망 이벤트를 또 만들지 않는다", _rows("SELECT COUNT(*) FROM death_events WHERE pc_id='PC-A'")[0][0] == 1)

        # 삭제
        await D.delete_status("PC-3")
        ok("S432d-k delete_status 는 메모리·DB 둘 다 지운다(되살아나지 않는다)",
           await D.get_status("PC-3") is None and _rows("SELECT COUNT(*) FROM pc_status WHERE pc_id='PC-3'")[0][0] == 0)
        await D.upsert_status("PC-4x", {"status": "idle"})
        await D.delete_pc_all_data("PC-4x")
        ok("S432d-l delete_pc_all_data 도 메모리의 미저장 행을 지운다", await D.get_status("PC-4x") is None and "PC-4x" not in D._ST_DIRTY)

        # 저장 실패 → 다시 dirty
        await D.upsert_status("PC-F", {"status": "idle"})

        def boom(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        D.aiosqlite.connect = boom
        try:
            w = await D.flush_statuses()
        finally:
            D.aiosqlite.connect = real
        ok("S432d-m ★저장이 실패하면 행을 잃지 않고 다시 dirty 로(통계에 에러)★", w == 0 and "PC-F" in D._ST_DIRTY and D.ST_STATS["last_err"], str(D.st_stats()))
        w = await D.flush_statuses()
        ok("S432d-n 다음 바퀴에 저장된다", w >= 1 and D.ST_STATS["last_err"] is None and "PC-F" not in D._ST_DIRTY)

        # 덤프에 미저장 행
        await D.upsert_status("PC-DUMP", {"status": "idle", "x": 1})
        dump = await D.get_pc_dump("PC-DUMP")
        ok("S432d-o get_pc_dump 는 먼저 flush 해서 미저장 행을 싣는다", len(dump["pc_status"]) == 1, str(dump["pc_status"])[:120])

        # 재시작: 메모리 비우면 DB 만 남는다 = 마지막 flush 까지
        await D.upsert_status("PC-LOST", {"status": "idle"})
        D._ST_MEM.clear()
        D._ST_DIRTY.clear()
        ok("S432d-p 재시작 흉내: flush 안 한 보고만 잃고 저장된 행은 그대로",
           await D.get_status("PC-LOST") is None and (await D.get_status("PC-A")) is not None)

    with _Fresh():
        await D.init_db()
        D.PC_FLUSH_S = 0
        await D.upsert_status("PC-WT", {"status": "idle"})
        ok("S432d-q PC_FLUSH_S=0 이면 예전처럼 즉시 DB(되돌림 손잡이)", _rows("SELECT COUNT(*) FROM pc_status WHERE pc_id='PC-WT'")[0][0] == 1 and "PC-WT" not in D._ST_MEM)
        D.PC_FLUSH_S = 0.05
        await D.upsert_status("PC-LOOP", {"status": "idle"})
        t = asyncio.ensure_future(D.status_flush_loop())
        await asyncio.sleep(0.4)
        t.cancel()
        try:
            await t
        except BaseException:
            pass
        ok("S432d-r 일꾼이 주기적으로 flush 한다", _rows("SELECT COUNT(*) FROM pc_status WHERE pc_id='PC-LOOP'")[0][0] == 1)


async def t_diag():
    import inspect
    ok("S432d-s /diag/perf 에 status_store", '"status_store"' in inspect.getsource(main.diag_perf))
    ok("S432d-t 종료 때 마지막 flush", "flush_statuses" in inspect.getsource(main.lifespan))
    s = main.new_session("main")
    r = await main.diag_volume(Req(None, api_key=None, session=s))
    ok("S432d-u /diag/volume: 볼륨·tmp 에서 1MB fsync, 4KB×20 fsync 를 잰다",
       all(k in r["volume"] for k in ("w1mb_fsync_ms", "w4kb_fsync_x20_ms")) and r["tmp"].get("w1mb_fsync_ms") is not None, str(r)[:300])
    leftovers = [f for f in os.listdir(os.path.dirname(D.DB_PATH) or ".") if f.startswith(".diag_vol_")]
    ok("S432d-v 진단 임시 파일은 남지 않는다", not leftovers, str(leftovers))
    try:
        await main.diag_volume(Req(None, api_key=None, session=None))
        no = False
    except Exception as e:
        no = getattr(e, "status_code", None) == 401
    ok("S432d-w 세션 없으면 401", no)


def test_all():
    run_all([t_store, t_diag])
    finish("test_status_store", MIN_CHECKS)


if __name__ == "__main__":
    test_all()
