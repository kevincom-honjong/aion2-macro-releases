"""database.py — aiosqlite 기반 SQLite 헬퍼"""
import aiosqlite
import asyncio
import os
import json
from datetime import datetime, timezone, timedelta

DB_PATH = os.getenv("DB_PATH", "/data/macro_control.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# 같은 PC의 사망을 이 초 이내 중복 기록하지 않음(사망→부활 처리 중 상태 오가며 중복 방지).
DEATH_DEBOUNCE_SEC = 60
CMD_KEEP_DAYS = 30   # ★B-DB7 (2026-09-23)★ 끝난 명령 행 보관 일수(init_db 가 부팅 때 정리)


def _finite(v):
    """★NaN/Infinity → None (2026-09-23)★ — json.loads 는 맨 NaN 토큰을 받아들인다. 한 PC 보고의
    NaN 이 저장되면 GET /status 가 테넌트 통째로 500, WS·FV 본문은 브라우저 JSON.parse 가 깨졌다.
    저장하는 자리(자원 쪽)에서 걸러낸다."""
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite(x) for x in v]
    return v


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pc_status (
                pc_id      TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS commands (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                pc_id      TEXT NOT NULL,
                command    TEXT NOT NULL,
                args       TEXT DEFAULT '{}',
                status     TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                pc_id      TEXT NOT NULL,
                level      TEXT DEFAULT 'info',
                message    TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cmd_pc_status ON commands(pc_id, status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_logs_pc ON logs(pc_id, id)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS updater_status (
                pc_id      TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS updater_commands (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                pc_id      TEXT NOT NULL,
                command    TEXT NOT NULL,
                args       TEXT DEFAULT '{}',
                status     TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ucmd_pc_status ON updater_commands(pc_id, status)"
        )
        # ★팜뷰 id → 업데이터 행 (2026-09-23 배포 반증 2차 F1·F2)★ — ack 를 메모리의 «PC 당 최신 1칸» 으로만
        #   맞추면 사람이 다시 누르거나(칸이 덮임) 재배포하면(칸이 비움) ok:true ack 가 버려지고, 90초 뒤 회수가
        #   ★같은 명령을 업데이터가 한 번 더★ 돌렸다. 집는 순간 여기 적고 ack 는 이 표로 찾는다.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS updater_fv_claim (
                fv_id   INTEGER PRIMARY KEY,
                ucmd_id INTEGER NOT NULL,
                at      TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS char_info (
                pc_id        TEXT PRIMARY KEY,
                total_kina   INTEGER DEFAULT 0,
                chars        TEXT DEFAULT '[]',
                collected_at TEXT NOT NULL
            )
        """)
        # 팜뷰 «팔린 만큼 줄인다»(CONTRACTS_팜뷰 2026-09-13) — 매니아 거래 1건 = 행 1개. tid 가 PK 라 두 번 못 뺀다.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kina_adjust (
                tid     TEXT PRIMARY KEY,
                pc_id   TEXT NOT NULL,
                delta   INTEGER NOT NULL,
                before  INTEGER NOT NULL,
                after   INTEGER NOT NULL,
                why     TEXT DEFAULT '{}',
                at      TEXT NOT NULL
            )
        """)
        # ★카드별 장부 인덱스 (2026-09-24 #114 FV8)★ — get_all_char_info 가 카드마다 «장부 행이 있나» 를 묻는다(매 상태 빌드).
        #   장부는 지우지 않으므로(kina_adjust = 원장) 인덱스 없이는 갈수록 느려진다. 1350·1371 의 카드별 조회도 같이 탄다.
        await db.execute("CREATE INDEX IF NOT EXISTS idx_kina_adjust_pc ON kina_adjust(pc_id, at)")
        # ★창고키나 판독 시각·순번 (2026-09-23 P0 v3, SHARED_ISSUES_대시보드 KF1)★ — 매크로가 kina_read_at·kina_seq 를
        #   보내면 카드마다 마지막으로 받아들인 판독을 적는다. 이보다 옛 판독(재전송·보내기 실패 뒤 늦게 온 값)은
        #   창고키나를 안 덮는다. char_info 에 칸을 더하지 않고 표를 따로 둔다(ALTER 없이 옛 볼륨 DB 그대로).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kina_read (
                pc_id    TEXT PRIMARY KEY,
                seq      INTEGER,
                read_at  TEXT,
                read_srv TEXT,
                seen     TEXT DEFAULT '[]'
            )
        """)
        # read_srv·seen 은 배포 반증 D(2026-09-23 밤)에 더했다 — 그 전 판으로 만든 표(개발 DB)에도 칸을 붙인다
        for _col in ("read_srv TEXT", "seen TEXT DEFAULT '[]'"):
            try:
                await db.execute(f"ALTER TABLE kina_read ADD COLUMN {_col}")
            except Exception:
                pass
        # ★PC 시계 어긋남 표본 (v2 반증 1부 A, 2026-09-24)★ — 카드별 [[서버 epoch, 서버−PC 초], …] 최근 몇 개.
        #   재전송(보낸 시각을 모르는 본문)에만 쓰고, 최근·여러 개·서로 맞을 때만 쓴다(main._kina_sent_at). 재배포에도 남게 표로.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pc_clock (
                pc_id   TEXT PRIMARY KEY,
                samples TEXT DEFAULT '[]'
            )
        """)
        # ★업데이터에 내준 시각 (v2 반증 1부 B)★ — 폴링이 행을 내주고 ack 가 사라진 사이 같은 종류를 또 누르면 그 행이
        #   superseded 로 적혔다(실제로는 돌았다 — 재시작 두 번인데 내역은 «안 돎»). 내준 행은 덮지 않는다.
        try:
            await db.execute("ALTER TABLE updater_commands ADD COLUMN handed_at TEXT")
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS nightmare_progress (
                pc_id      TEXT NOT NULL,
                slot       INTEGER NOT NULL,
                tab        TEXT DEFAULT '몽충I',
                bosses     TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (pc_id, slot)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS slot_filters (
                pc_id   TEXT PRIMARY KEY,
                filters TEXT DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS death_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                pc_id      TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_death_pc_time ON death_events(pc_id, created_at)"
        )
        # ★logs(created_at) 인덱스 (2026-09-11 전수조사)★ — FarmView 이벤트 폴링이
        #   `WHERE created_at > ? ORDER BY id ASC LIMIT ?` 인데 인덱스가 없어
        #   EXPLAIN QUERY PLAN 실측 ★SCAN logs★ 였다(144,000행에서 28.8ms/호출).
        #   맞는 행이 표의 맨 끝(최신 id)이라 LIMIT 조기종료도 거의 안 걸렸다.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at, id)"
        )
        # ★B-DB7 (2026-09-23) 명령 이벤트 시각 인덱스★ — get_commands_since 가 커서와 같은
        #   기준(이벤트 시각 = COALESCE(updated_at, created_at))으로 자르고 정렬해야 잘림 경계가
        #   맞는다(B-FV5). 식 인덱스라 쿼리의 식과 ★글자 그대로★ 같아야 탄다.
        # ★B-DB7 의 명령 표 정리·idx_cmd_at 은 부팅 뒤 백그라운드로 옮겼다 (2026-09-23 배포 반증 #6)★ —
        #   init_db 안에서 하면 큰 표(30만 행 15초·100만 행 62초 실측)에서 Railway healthcheckTimeout(10초)을 넘겨
        #   배포가 실패한다. maintain_command_tables() 가 lifespan 뒤에 나눠서 한다.
        # ★텔레그램 단일봇 중계(2026-07-28): 서버가 보낸 메시지 id → 어느 PC의 요청인지.
        #   사용자가 그 메시지에 '답장'하면 이 표로 PC를 특정해 명령 큐에 코드를 꽂는다.
        #   ※메모리 dict로 두면 Railway 재배포 때 통째로 날아가 답장이 미아가 된다 → DB.★
        await db.execute("""
            CREATE TABLE IF NOT EXISTS telegram_map (
                message_id INTEGER PRIMARY KEY,
                pc_id      TEXT NOT NULL,
                chat_id    TEXT NOT NULL,
                kind       TEXT DEFAULT 'captcha',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tgmap_chat_time ON telegram_map(chat_id, created_at)"
        )
        await db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ── PC 상태 ─────────────────────────────────────────────────────────────────

async def upsert_status(pc_id: str, data: dict) -> None:
    data = _finite(data)
    async with aiosqlite.connect(DB_PATH) as db:
        # dead 전환(edge) 감지용으로 이전 상태를 먼저 읽는다.
        prev_status = None
        prev_exists = False
        async with db.execute("SELECT data FROM pc_status WHERE pc_id=?", (pc_id,)) as cur:
            row = await cur.fetchone()
        if row is not None:
            prev_exists = True
            try:
                prev_status = (json.loads(row[0]) or {}).get("status")
            except Exception:
                prev_status = None
        await db.execute(
            "INSERT OR REPLACE INTO pc_status(pc_id, data, updated_at) VALUES(?,?,?)",
            (pc_id, json.dumps(data, ensure_ascii=False), _now()),
        )
        # non-dead → dead 전환일 때만 사망 이벤트 1건 기록.
        #   · 반복 "dead" 보고(어비스 사망 유지·30초 자동보고)는 prev==dead라 중복 안 됨.
        #   · prev_exists 요구: 재배포로 DB 비운 뒤 이미 죽은 PC가 "dead"로 재푸시할 때
        #     prev=None인 걸 사망으로 오탐하지 않도록(첫 보고가 dead인 정상 시나리오는 없음).
        if prev_exists and prev_status != "dead" and data.get("status") == "dead":
            # 디바운스: 같은 PC가 최근 DEATH_DEBOUNCE_SEC 내 이미 사망 기록됐으면 스킵.
            #   사망→부활 처리 중 상태가 dead↔hunting/abyss로 잠깐 오가며(30초 자동보고 개입,
            #   부활 실패 재감지 등) 같은 사망이 여러 전환으로 중복 기록되는 것을 방지.
            #   실제 연속 사망은 부활+이동(일반 ~80s+) 또는 Delete 재시작(어비스 수 분)이 필요해
            #   60초보다 훨씬 길므로, 진짜 사망을 합칠 위험 없이 중복만 제거.
            debounce_cut = (datetime.now(timezone.utc)
                            - timedelta(seconds=DEATH_DEBOUNCE_SEC)).strftime("%Y-%m-%dT%H:%M:%S")
            async with db.execute(
                "SELECT 1 FROM death_events WHERE pc_id=? AND created_at >= ? LIMIT 1",
                (pc_id, debounce_cut),
            ) as cur:
                dup = await cur.fetchone()
            if dup is None:
                await db.execute(
                    "INSERT INTO death_events(pc_id, created_at) VALUES(?,?)", (pc_id, _now())
                )
                # 테이블 비대화 방지 — 6시간 지난 이벤트 정리(30분 집계엔 넉넉).
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S")
                await db.execute("DELETE FROM death_events WHERE created_at < ?", (cutoff,))
        await db.commit()


async def get_death_counts_since(cutoff_iso: str) -> dict[str, int]:
    """cutoff_iso(UTC ISO) 이후 pc_id별 사망 이벤트 수."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pc_id, COUNT(*) FROM death_events WHERE created_at >= ? GROUP BY pc_id",
            (cutoff_iso,),
        ) as cur:
            rows = await cur.fetchall()
    return {r[0]: r[1] for r in rows}


async def get_all_death_events() -> list[dict]:
    """[진단용] 모든 death_events (pc_id, created_at) 최신순."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pc_id, created_at FROM death_events ORDER BY created_at DESC LIMIT 500"
        ) as cur:
            rows = await cur.fetchall()
    return [{"pc_id": r[0], "created_at": r[1]} for r in rows]


async def get_all_statuses() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pc_id, data, updated_at FROM pc_status ORDER BY pc_id"
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for row in rows:
        try:
            d = json.loads(row["data"])
        except Exception:
            d = {}
        d["_updated_at"] = row["updated_at"]
        result.append(d)
    return result


async def delete_status(pc_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pc_status WHERE pc_id=?", (pc_id,))
        await db.commit()


async def delete_pc_all_data(pc_id: str, purge_all: bool = False) -> None:
    """pc_id 관련 모든 테이블 데이터 삭제 (완전 제거). purge_all=True(은퇴)면 슬롯 필터·악몽 진행까지."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pc_status        WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM updater_status   WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM commands         WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM updater_commands WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM logs             WHERE pc_id=?", (pc_id,))
        # ★업데이터 로그는 별도 키(".upd" 접미사)에 쌓인다 (2026-08-20)★
        #   같은 logs 표를 쓰지만 pc_id 가 "PC-01.upd" 라서 위 줄로는 안 지워진다.
        #   빼먹으면 PC 를 지워도 그 PC 의 업데이터 로그만 유령으로 남는다.
        await db.execute("DELETE FROM logs WHERE pc_id=?", (pc_id + ".upd",))
        await db.execute("DELETE FROM char_info        WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM kina_read        WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM pc_clock         WHERE pc_id=?", (pc_id,))   # 시계 표본도(v3 델타 반증 #6)
        await db.execute("DELETE FROM death_events      WHERE pc_id=?", (pc_id,))
        # ★B-DB9 (2026-09-23)★ slot_filters·nightmare_progress 는 ★은퇴(purge_all=True)일 때만★ 지운다 —
        #   카드 삭제는 「멈춘 카드 청소」로도 쓰여 매크로가 곧 같은 id 로 재보고하는데(실측 31개 중 5개 부활),
        #   그때 매크로 메모리의 슬롯 필터·주간 진행과 서버 값이 어긋나면 안 된다. 은퇴는 영구라 다 지운다.
        # ★B-DB9 보강 (2026-09-23 병합 반증) 텔레그램 답장 경로도 같은 규칙★ — 초판은 카드 삭제마다 지워,
        #   캡차를 기다리던 PC 가 같은 id 로 되살아나도 사진에 단 답장이 「이미 처리됐거나 만료」로 버려졌다.
        #   은퇴 때는 지우지 않고 ★처리됨(kind='done')★ 으로 묻는다 — 행이 사라지면 그 사진에 단 답장이
        #   「모르는 메시지」가 되어 _tg_handle_update 의 「대기 하나」 추론으로 ★다른 PC★ 에 갈 수 있다(B-TG1).
        #   'done' 행은 대기 후보(tg_map_recent, kind='captcha')에서 빠지고 48시간 정리(tg_map_put)로 사라진다.
        if purge_all:
            await db.execute("UPDATE telegram_map SET kind='done' WHERE pc_id=?", (pc_id,))
            await db.execute("DELETE FROM slot_filters       WHERE pc_id=?", (pc_id,))
            await db.execute("DELETE FROM nightmare_progress WHERE pc_id=?", (pc_id,))
        await db.commit()
        _char_info_bump()                  # ★커밋 뒤·연결 안★ — FV 스냅샷 폴백 세대(char_info_gen)


# ★카드 삭제 전 백업용 읽기 전용 덤프 (2026-09-22, 사고 —)★ delete_pc_all_data 가 지우는
#   7개 표 + 안 지우는 nightmare_progress·slot_filters(은퇴 때만 지움, B-DB9) 까지 그 pc_id 행 전부를 그대로 반환한다.
#   쓰기 없음 — 삭제 여부와 무관하게 언제나 안전하게 부를 수 있다. logs 는 보존 상한(3000/PC)
#   보다 넉넉한 5000 으로 잘라 무제한 테이블 사고(2026-09-11류)를 막는다.
async def get_pc_dump(pc_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async def _all(sql: str, params: tuple) -> list[dict]:
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]

        return {
            "pc_id": pc_id,
            "pc_status": await _all(
                "SELECT pc_id, data, updated_at FROM pc_status WHERE pc_id=?", (pc_id,)),
            "updater_status": await _all(
                "SELECT pc_id, data, updated_at FROM updater_status WHERE pc_id=?", (pc_id,)),
            "commands": await _all(
                "SELECT id, pc_id, command, args, status, created_at, updated_at "
                "FROM commands WHERE pc_id=? ORDER BY id", (pc_id,)),
            "updater_commands": await _all(
                "SELECT id, pc_id, command, args, status, created_at, updated_at "
                "FROM updater_commands WHERE pc_id=? ORDER BY id", (pc_id,)),
            "logs": await _all(
                "SELECT id, pc_id, level, message, created_at FROM logs "
                "WHERE pc_id=? ORDER BY id DESC LIMIT 5000", (pc_id,)),
            # ★업데이터 로그는 ".upd" 접미사 키(delete_pc_all_data 와 짝)★
            "logs_upd": await _all(
                "SELECT id, pc_id, level, message, created_at FROM logs "
                "WHERE pc_id=? ORDER BY id DESC LIMIT 5000", (pc_id + ".upd",)),
            "char_info": await _all(
                "SELECT pc_id, total_kina, chars, collected_at FROM char_info WHERE pc_id=?",
                (pc_id,)),
            "death_events": await _all(
                "SELECT id, pc_id, created_at FROM death_events WHERE pc_id=? ORDER BY id",
                (pc_id,)),
            "nightmare_progress": await _all(
                "SELECT pc_id, slot, tab, bosses, updated_at FROM nightmare_progress "
                "WHERE pc_id=? ORDER BY slot", (pc_id,)),
            "slot_filters": await _all(
                "SELECT pc_id, filters FROM slot_filters WHERE pc_id=?", (pc_id,)),
        }


async def get_status(pc_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT data FROM pc_status WHERE pc_id=?", (pc_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data"])
    except Exception:
        return {}


# ── 명령 큐 ─────────────────────────────────────────────────────────────────

async def insert_command(pc_id: str, command: str, args: dict) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO commands(pc_id, command, args, status, created_at) VALUES(?,?,?,?,?)",
            (pc_id, command, json.dumps(args, ensure_ascii=False), "pending", _now()),
        )
        await db.commit()
        return cur.lastrowid


COMMAND_MAX_AGE_SEC = 900          # 15분 — 이보다 오래된 대기 명령은 배달하지 않는다
# ★만료 면제(2026-08-07 리뷰 major)★ — '지금 실행하라'가 아니라 '이 설정으로 맞춰라'인 명령은
#   늦게 도착해도 맞다. 특히 set_slot_filter는 매크로가 부팅 시 서버에서 다시 읽어오는 경로가
#   아예 없어서, 만료시키면 대시보드는 슬롯 3 비활성인데 매크로는 계속 그 캐릭을 도는
#   ★영구 불일치★가 된다(재동기화 기회 없음).
#   ★B-CQ1 (2026-09-23) set_info 도 면제★ — 매크로는 set_info 를 부팅 때도 지키는(유지) 설정으로 다루는데
#   서버만 15분에 expired 로 걷어 ★15분 넘게 꺼져 있던 PC 에 계정 정보 변경이 조용히 사라졌다★.
COMMAND_NO_EXPIRE = ("set_slot_filter", "set_info")


async def get_pending_command(pc_id: str, all_key: str = "all") -> dict | None:
    """가장 오래된 pending 한 건. (몸통은 아래 get_pending_commands)"""
    rows = await get_pending_commands(pc_id, all_key, limit=1)
    return rows[0] if rows else None


async def get_pending_commands(pc_id: str, all_key: str = "all",
                               limit: int = 1) -> list[dict]:
    """pc_id 또는 브로드캐스트(all_key) 명령 중 오래된 순으로 최대 limit 건.

    ★★여러 건을 주는 이유 (2026-09-10 주인님 지시)★★
      WS 가 끊겼다 붙을 때 서버가 밀린 명령을 ★한 건만★ 보내고 나머지는 매크로
      폴링을 기다리게 했다. 그 폴링 간격은 15초(WS 끊긴 걸 아는 동안)에서
      60초(자기는 붙어 있다고 믿는 동안)다. 서버가 굳었다 풀리는 구간마다
      그 지연이 그대로 사람 눈에 「대시보드가 느리다」로 보였다.
      → 재접속은 밀린 것을 다 준다. 상한은 호출부가 정한다(한 주기에 쏟아붓지 않기).

    all_key: 테넌트 스코프된 'all' 키(예: 't::all') — 리터럴 'all' 고정은 테넌트 우회라 제거(2026-07-26).

    ★유효기간 15분(2026-08-07)★ — 예전엔 나이 제한이 없어서, 꺼져 있던 PC가 몇 시간 뒤 다시
    붙는 순간 그때의 '정지/종료' 같은 명령이 뒤늦게 실행됐다(실제로 PC-16의 exit 2건이 몇 시간째
    큐에 남아 있었다). 매크로가 평상시에도 HTTP로 명령을 확인하게 바뀌면서(반쯤 죽은 WS 대비)
    이 위험이 전 함대로 넓어져 함께 막는다. 지난 명령은 expired로 표시해 큐에서 걷어낸다."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=COMMAND_MAX_AGE_SEC)
              ).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        _keep = ",".join("?" for _ in COMMAND_NO_EXPIRE)
        # ★만료시킬 게 있을 때만 쓴다 (2026-09-11 전수조사)★
        #   초판은 조회 때마다 무조건 UPDATE+commit 이라 ★순수 조회인데 쓰기 락★ 을
        #   잡았다. 매크로가 명령을 확인할 때마다(24대 × 15~60초 + 재접속마다) 그랬다.
        #   SQLite 쓰기는 직렬화되므로 로그 쓰기와 같은 큐에서 다툰다.
        #   ★만료 규칙 자체는 그대로다★ — 먼저 세어 보고 있을 때만 UPDATE 한다.
        async with db.execute(
            "SELECT 1 FROM commands "
            f"WHERE (pc_id=? OR pc_id=?) AND status='pending' AND created_at < ? "
            f"AND command NOT IN ({_keep}) LIMIT 1",
            (pc_id, all_key, cutoff, *COMMAND_NO_EXPIRE),
        ) as _cur:
            _has_old = await _cur.fetchone() is not None
        if _has_old:
            await db.execute(
                "UPDATE commands SET status='expired', updated_at=? "
                f"WHERE (pc_id=? OR pc_id=?) AND status='pending' AND created_at < ? "
                f"AND command NOT IN ({_keep})",
                (_now(), pc_id, all_key, cutoff, *COMMAND_NO_EXPIRE),
            )
            await db.commit()
        async with db.execute(
            """
            SELECT id, pc_id, command, args, created_at
            FROM commands
            WHERE (pc_id=? OR pc_id=?) AND status='pending'
            ORDER BY id ASC LIMIT ?
            """,
            (pc_id, all_key, max(1, int(limit))),
        ) as cur:
            rows = await cur.fetchall()
    out = []
    for row in rows:
        try:
            args = json.loads(row["args"])
        except Exception:
            args = {}
        out.append({
            "id": row["id"],
            "command": row["command"],
            "args": args,
            "created_at": row["created_at"],
        })
    return out


async def ack_command(cmd_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            # ★되돌리지 않는다 (2026-09-11 전수조사)★ — 초판은 `WHERE id=?` 뿐이라
            #   사람이 ✕ 로 취소한 명령이나 만료된 명령이 뒤늦은 ack 하나로
            #   ★acked 로 뒤집혀 취소한 흔적이 이력에서 사라졌다.★
            #   pending 일 때만 ack 으로 올린다(cancel_command 와 같은 규칙, §A12).
            "UPDATE commands SET status='acked', updated_at=? "
            "WHERE id=? AND status='pending'",
            (_now(), cmd_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def cancel_command(cmd_id: int, allow_acked: bool = False) -> bool:
    """pending 상태의 명령을 cancelled로 변경 (이미 acked면 취소 불가)

    ★B-CQ2 (2026-09-23) allow_acked=True 는 ★매크로 통지★ 전용★ — 매크로는 받자마자 ack 하고
      그 뒤에 버리거나 거부하면 cancelled/rejected 를 보낸다. 그 통지는 acked 에서도 내린다.
      대시보드 ✕ 취소는 기본값(pending 만) 그대로 — expired·cancelled 는 어느 쪽도 안 뒤집는다."""
    _from = "('pending','acked')" if allow_acked else "('pending')"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE commands SET status='cancelled', updated_at=? WHERE id=? AND status IN {_from}",
            (_now(), cmd_id),
        )
        await db.commit()
        return cur.rowcount > 0


def _like_prefix(s: str) -> str:
    """LIKE 패턴에서 리터럴로 쓸 접두사 — `\\`·`%`·`_` 를 막는다 (2026-09-11)."""
    return (s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


async def get_recent_commands(limit: int = 20, ns_prefix: "str | None" = None) -> list[dict]:
    """ns_prefix: None=전체(호환), ""=main(네임스페이스 없는 행만), "t"=해당 테넌트("t::%") 행만.
    테넌트별 최근 N건을 정확히 주기 위함 — 전역 창에서 필터하면 타 테넌트 폭주 시 내역이 비어 보임(2026-07-26)."""
    where, params = "", []
    if ns_prefix == "":
        where = "WHERE pc_id NOT LIKE '%::%'"
    elif ns_prefix:
        # ★ESCAPE 를 붙인다 (2026-09-11)★ — 테넌트 이름에 `_`(임의 1글자)나 `%` 가
        #   들어 있으면 남의 테넌트 행까지 잡아 ★LIMIT 20 창을 먹는다.★
        #   내용은 하류 필터가 걸러 새지 않지만, 그 테넌트 이력이 비어 보인다 —
        #   이 함수를 만든 이유(2026-07-26)가 정확히 그 증상이었다.
        where = "WHERE pc_id LIKE ? ESCAPE '\\'"
        params.append(_like_prefix(ns_prefix) + "::%")
    params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM commands {where} ORDER BY id DESC LIMIT ?", params
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def latest_command_after(pc_ids: list, commands: tuple, after: str) -> "dict | None":
    """pc_ids(네임스페이스 붙은 카드 id) 중 하나에 after(UTC «%Y-%m-%dT%H:%M:%S») ★뒤★ 들어온 commands 중
    가장 새 것 {id, pc_id, command, created_at} (없으면 None). 순환 전환 상한을 새 전환 명령 시각부터 다시 잴 때 쓴다
    (main._rot_switch_age, 2026-09-24 PC-21). idx_cmd_pc_status(pc_id, …) 를 탄다.
    ★버려진 명령은 안 센다★(v4 반증 BRK-3) — cancelled(사람의 ✕ 취소, 또는 매크로가 버렸다/거부했다고 ack 로 알린 것 —
    notify_dropped·ACK_DROP_STATUSES 는 cancel_command 로 cancelled 가 된다)와 배달조차 안 된 expired 는 아무 전환도
    시작하지 않았다. 그걸로 상한을 다시 재면 명령 하나당 20분씩 그물이 늦어진다.
    ★못 거르는 것(v4 반증 F4)★ 매크로의 «이전 명령 처리 중 → 'X' 거부»(loot.py 538)는 받자마자 ack 한 뒤라 행이 acked 로
    남는다 — 그 명령도 여기선 센다(한 번, 최대 20분 늦게 ⛔). 거르려면 로그 대조가 필요하다."""
    ids = [str(x) for x in (pc_ids or []) if x][:16]
    cmds = [str(x) for x in (commands or []) if x][:8]
    if not ids or not cmds:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT id, pc_id, command, created_at FROM commands WHERE pc_id IN ({','.join('?' for _ in ids)}) "
            f"AND command IN ({','.join('?' for _ in cmds)}) AND created_at > ? "
            f"AND status NOT IN ('cancelled','expired') ORDER BY id DESC LIMIT 1",
            (*ids, *cmds, str(after or ""))) as cur:
            r = await cur.fetchone()
    return dict(r) if r else None


async def get_command_pc(cmd_id: int) -> "str | None":
    """명령 id의 pc_id(저장 키) 단건 조회 — 소유 테넌트 판정용(2026-07-26)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT pc_id FROM commands WHERE id=?", (cmd_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    """전역 설정 KV (각성 난이도 프리셋 등, 2026-07-26). key는 호출측에서 테넌트 스코프."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES(?,?,?)",
            (key, value, _now()),
        )
        await db.commit()


async def get_setting(key: str) -> "str | None":
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


# ── 텔레그램 답장 매핑 ───────────────────────────────────────────────────────

async def tg_map_put(message_id: int, pc_id: str, chat_id: str, kind: str = "captcha") -> None:
    """서버가 보낸 텔레그램 메시지 id에 요청 PC를 붙여둔다(답장 라우팅용)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO telegram_map(message_id, pc_id, chat_id, kind, created_at)"
            " VALUES(?,?,?,?,?)",
            (int(message_id), pc_id, str(chat_id), kind, _now()),
        )
        # 오래된 매핑 정리 — 표가 무한히 자라지 않게(답장은 길어야 몇 시간 안에 온다)
        await db.execute(
            "DELETE FROM telegram_map WHERE created_at < ?",
            ((datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S"),),
        )
        await db.commit()


async def tg_map_get(message_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT message_id, pc_id, chat_id, kind, created_at FROM telegram_map WHERE message_id=?",
            (int(message_id),),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def tg_map_recent(chat_id: str, within_sec: int, kind: str = "captcha") -> list[dict]:
    """해당 채팅에서 최근 within_sec 안에 보낸 요청들.
    ★답장 없이 코드만 보냈을 때 대상 추론용 — 후보가 정확히 1건일 때만 쓴다(호출측 판단).★"""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=int(within_sec))
              ).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT message_id, pc_id, chat_id, kind, created_at FROM telegram_map"
            " WHERE chat_id=? AND kind=? AND created_at>=? ORDER BY message_id DESC",
            (str(chat_id), kind, cutoff),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def tg_map_delete_pc(pc_id: str) -> None:
    """그 PC의 대기 요청을 모두 소거 — 코드를 받았으면 남은 후보에서 빼야 오라우팅이 없다.
    ★B-TG1 보강 (2026-09-23 병합 반증) 지우지 않고 kind='done' 으로 묻는다★ — 대기 후보(kind='captcha')
    에서는 똑같이 빠지지만, 그 사진에 다시 단 답장을 「처리된 사진」 으로 알아본다(행이 없으면 봇의
    안내문·expect_reply 없는 알림 같은 ★처음부터 모르는 메시지★ 와 구별이 안 된다). 48시간 뒤 정리."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE telegram_map SET kind='done' WHERE pc_id=?", (pc_id,))
        await db.commit()


async def get_updater_command_pc(cmd_id: int) -> "str | None":
    """업데이터 명령 id의 pc_id(저장 키) 단건 조회 — 소유 테넌트 판정용(2026-07-26)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT pc_id FROM updater_commands WHERE id=?", (cmd_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


# ── 로그 ─────────────────────────────────────────────────────────────────────

# ★로그 정리 빈도 (2026-09-11)★ — 키(pc_id)마다 이만큼 쌓일 때마다 한 번 훑는다.
#   초판은 ★매 삽입마다★ 훑어서 지울 게 없어도 쓰기 트랜잭션+fsync 를 냈다.
LOG_PRUNE_EVERY = 200
LOG_KEEP_PER_PC = 3000       # 키(pc_id)당 보관 줄수 — 스팸은 클라에서 걸러 중요 이벤트만 오므로 며칠치
_LOG_SINCE_PRUNE: dict = {}
LOG_SINCE_PRUNE_MAX = 2000


async def insert_log(pc_id: str, level: str, message: str,
                     created_at: str | None = None) -> None:
    """created_at: 클라이언트가 그 줄을 ★실제로 찍은 시각★(UTC ISO). None이면 수신 시각.

    ★왜 필요한가 (2026-08-20, 업데이터 원격 로그)★
      업데이터는 로그를 20초마다 배치로 모아 보내고, 서버가 죽어 있으면 최대 5분까지
      백오프하며 쌓아둔다. 수신 시각으로 기록하면 그 배치가 전부 '방금' 찍힌 것처럼
      뭉쳐 보여서 ★사고 순서를 못 읽는다★. 클라가 준 시각을 그대로 쓴다.
      기본값 None 이라 기존 호출부(매크로 /log/, 서버 내부 기록)는 전부 무영향.
    """
    # ══════════════════════════════════════════════════════════════════════
    # ★★연결 하나·커밋 하나 (2026-09-11 전수조사)★★
    #   초판은 ①INSERT 용 연결 ②정리용 연결을 ★따로★ 열고 커밋도 두 번 했다.
    #   실측 ★4.52ms/줄★ 이고 업데이터 배치 50줄이면 연결 100·커밋 100 이 직렬이다.
    #   Railway 볼륨은 네트워크 디스크이고 synchronous=FULL 이라 ★커밋 횟수가 곧 비용★.
    #
    #   ★정리 빈도만 낮춘다 — 3000줄 상한의 뜻은 그대로다★
    #   초판은 지울 게 0건이어도 매번 DELETE 트랜잭션을 열었다(=fsync). 이제
    #   LOG_PRUNE_EVERY 줄마다 한 번만 훑고, 그때 3000을 넘은 만큼 자른다.
    #   즉 최대 3000+LOG_PRUNE_EVERY-1 줄까지 잠깐 넘칠 수 있다 — 보존은 늘고
    #   줄어들지 않으므로 「몇 시간~며칠치」라는 원래 약속은 유지된다.
    # ══════════════════════════════════════════════════════════════════════
    # ★B-DB8 (2026-09-24 #114)★ 처음 보는 pc_id(재시작·칸 버림 뒤)는 ★첫 줄에 정리★ 한다. 초판은 0 부터 셌다 —
    #   카운터는 메모리라 재배포마다 0 이 되고, 재배포 사이에 200줄을 못 채우는 PC(업데이터만 도는 PC·은퇴 직전 PC)는
    #   ★영영 한 번도 정리되지 않아★ 3000줄 상한이 뜻을 잃었다. 첫 줄 정리 = 재시작마다 PC 당 DELETE 한 번(idx_logs_pc).
    _n = _LOG_SINCE_PRUNE.get(pc_id, LOG_PRUNE_EVERY - 1) + 1
    _prune = _n >= LOG_PRUNE_EVERY
    # ★카운터 칸 상한 (2026-09-23 B2)★ — pc_id 마다 한 칸씩 늘기만 했다. 넘치면 먼저 들어온 칸부터
    #   버린다(카운터일 뿐 — 버린 PC 는 다음 정리가 최대 LOG_PRUNE_EVERY 줄 늦어질 뿐이다).
    if pc_id not in _LOG_SINCE_PRUNE and len(_LOG_SINCE_PRUNE) >= LOG_SINCE_PRUNE_MAX:
        for _k in list(_LOG_SINCE_PRUNE)[:len(_LOG_SINCE_PRUNE) - LOG_SINCE_PRUNE_MAX + 1]:
            _LOG_SINCE_PRUNE.pop(_k, None)
    _LOG_SINCE_PRUNE[pc_id] = 0 if _prune else _n
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs(pc_id, level, message, created_at) VALUES(?,?,?,?)",
            (pc_id, level, message, created_at or _now()),
        )
        if _prune:
            await db.execute(
                """
                DELETE FROM logs WHERE pc_id=? AND id NOT IN (
                    SELECT id FROM logs WHERE pc_id=? ORDER BY id DESC LIMIT ?
                )
                """,
                (pc_id, pc_id, LOG_KEEP_PER_PC),
            )
        await db.commit()


# ── 업데이터 상태 ─────────────────────────────────────────────────────────────

async def upsert_updater_status(pc_id: str, data: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO updater_status(pc_id, data, updated_at) VALUES(?,?,?)",
            (pc_id, json.dumps(data, ensure_ascii=False), _now()),
        )
        await db.commit()


async def get_all_updater_statuses() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pc_id, data, updated_at FROM updater_status ORDER BY pc_id"
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for row in rows:
        try:
            d = json.loads(row["data"])
        except Exception:
            d = {}
        d["_updated_at"] = row["updated_at"]
        result.append(d)
    return result


# ── 업데이터 명령 큐 ──────────────────────────────────────────────────────────

async def insert_updater_command(pc_id: str, command: str, args: dict) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO updater_commands(pc_id, command, args, status, created_at) VALUES(?,?,?,?,?)",
            (pc_id, command, json.dumps(args, ensure_ascii=False), "pending", _now()),
        )
        await db.commit()
        return cur.lastrowid


# ★B-DB3 (2026-09-23) 업데이터 큐 유효기간·덮어쓰기★ — 매크로 큐(COMMAND_MAX_AGE_SEC)와 같은 기계.
#   예전엔 나이 제한이 없어서 며칠 꺼져 있던 PC 가 돌아오는 순간 그때 눌린 restart/update 를
#   ★뒤늦게, 쌓인 순서대로 전부★ 실행했다(사냥 중 재시작). 지난 것은 expired, 같은 PC 에 더 새
#   상태변경 명령이 있으면 옛 것은 superseded 로 ★표시해 남긴다★(조용히 지우지 않는다 —
#   /updater/commands/recent 에서 보인다). screenshot 은 상태를 안 바꾸므로 덮어쓰기 대상 밖.
UPDATER_COMMAND_MAX_AGE_SEC = 600      # 10분
UPDATER_NO_SUPERSEDE = ("screenshot",)
# ★팜뷰가 집고 ack 를 안 준 명령 (2026-09-23 배포 반증 #1)★ — 팜뷰 macro_act 는 20초 상한 + 폴링 10초라
#   90초 안에 ack 가 없으면 팜뷰가 죽었거나 ack 가 길을 잃은 것. 업데이터 큐로 되돌린다(안 그러면 10분 뒤 만료로 사라진다).
FV_CLAIM_ACK_SEC = 90
# 되살릴 때 ★같은 종류의 더 새 명령★ 이 이 상태면 옛 것은 되살리지 않고 superseded (배포 반증 2차 F3 —
#   옛 restart 가 새 restart 뒤에 돌았다). pending/acked/fv_claimed/fv_done = 새 것이 이미 가는 중이거나 돌았다.
_NEWER_ALIVE = ("pending", "acked", "fv_claimed", "fv_done", "handed_noack")   # handed_noack = 업데이터가 받아 갔다


async def _has_newer_same_kind(db, row_id: int, pc_id: str, command: str) -> bool:
    ph = ",".join("?" for _ in _NEWER_ALIVE)
    async with db.execute(
            f"SELECT 1 FROM updater_commands WHERE pc_id=? AND command=? AND id>? AND status IN ({ph}) LIMIT 1",
            (pc_id, command, row_id, *_NEWER_ALIVE)) as cur:
        return await cur.fetchone() is not None


def _older_same_kind(rows: list) -> list:
    """★같은 종류의 옛 명령만 덮는다 (2026-09-23 배포 반증 #2)★ — 예전엔 종류를 안 보고 최신 1건만 남겨
    update 뒤 restart(또는 start)를 누르면 ★update 가 조용히 사라졌다★(주인님 동작 유실). 같은 명령의
    중복(update+update, restart+restart)만 옛 것을 superseded 로. 다른 종류는 순서대로 다 나간다.
    rows: id 오름차순 [{id, command}]."""
    last = {}
    for r in rows:
        last[r["command"]] = r["id"]
    return [r["id"] for r in rows if last[r["command"]] != r["id"]]


async def maintain_command_tables(batch: int = 2000, pause: float = 0.05) -> dict:
    """★B-DB7 (2026-09-23) 명령 표 정리 — 부팅 뒤 백그라운드★ — commands·updater_commands 는 지우는 곳이 0곳이라
    무한정 쌓였다. CMD_KEEP_DAYS 보다 오래된 ★끝난★ 행만(pending 은 안 지운다 — set_slot_filter 처럼 만료 면제인
    것이 있다) batch 행씩 나눠 지우고 사이사이 쉰다 — 쓰기 잠금을 오래 잡아 보고·명령이 막히지 않게.
    kina_adjust 는 ★장부★(tid PK)라 건드리지 않는다. 다 지운 뒤(표가 작아진 뒤) idx_cmd_at 을 만든다.
    돌려주는 값 {"commands": 지운 수, "updater_commands": 지운 수, "index": True}."""
    _cut = (datetime.now(timezone.utc) - timedelta(days=CMD_KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    out = {"commands": 0, "updater_commands": 0, "index": False}
    for tbl in ("commands", "updater_commands"):
        while True:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    f"DELETE FROM {tbl} WHERE rowid IN (SELECT rowid FROM {tbl} "
                    f"WHERE status != 'pending' AND created_at < ? LIMIT ?)", (_cut, batch))
                n = cur.rowcount or 0
                await db.commit()
            out[tbl] += n
            if n < batch:
                break
            await asyncio.sleep(pause)
    async with aiosqlite.connect(DB_PATH) as db:
        # ★B-DB7 명령 이벤트 시각 인덱스★ — get_commands_since 가 커서와 같은 기준(이벤트 시각 =
        #   COALESCE(updated_at, created_at))으로 자르고 정렬해야 잘림 경계가 맞는다(B-FV5). 식 인덱스라
        #   쿼리의 식과 ★글자 그대로★ 같아야 탄다. 없어도 결과는 같다(느릴 뿐).
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_cmd_at ON commands(COALESCE(updated_at, created_at), id)"
        )
        await db.commit()
    out["index"] = True
    return out


async def get_pending_updater_command(pc_id: str, all_key: str = "all",
                                      unknown_out: "list | None" = None,
                                      fv_quiet: bool = True) -> dict | None:
    """all_key: 테넌트 스코프된 'all' 키 — 리터럴 고정은 테넌트 우회라 제거(2026-07-26).
    unknown_out: 팜뷰가 집고 ack 가 없어 fv_unknown 으로 바꾼 행 {id, command} 를 담는다(main 이 PC 로그에 남긴다).
    fv_quiet: 팜뷰가 한동안 목록·ack 를 안 불렀을 때만 True.
    ★팜뷰가 집은 명령은 어떤 경우에도 업데이터에 주지 않는다 (2026-09-23 배포 반증 3차 #1)★ — 팜뷰가 실행한 뒤
    관제컴 무선이 끊겨 ack 만 못 왔을 수 있다(조용함 ≠ 안 갔다). 조용하고 90초 지났거나 유효기간 10분이 지나면
    fv_unknown(«실행됐는지 모름», 대시보드 내역에 빨갛게)으로 둔다. 팜뷰가 돌아와 ack 를 다시 보내면 거기서 끝난다."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=UPDATER_COMMAND_MAX_AGE_SEC)
              ).strftime("%Y-%m-%dT%H:%M:%S")
    ack_cut = (datetime.now(timezone.utc) - timedelta(seconds=FV_CLAIM_ACK_SEC)
               ).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # ★B-DB3★ 먼저 읽기만 하고, 걷어낼 게 있을 때만 쓴다(매크로 큐 2026-09-11 과 같은 이유 —
        #   폴링마다 쓰기 락을 잡지 않는다). 대기 행은 PC 당 몇 건뿐이라 파이썬에서 가른다.
        async with db.execute(
            "SELECT id, pc_id, command, status, created_at, updated_at, handed_at FROM updater_commands "
            "WHERE (pc_id=? OR pc_id=?) AND status IN ('pending','fv_claimed') ORDER BY id ASC",
            (pc_id, all_key),
        ) as _cur:
            _rows = [dict(r) for r in await _cur.fetchall()]
        # 팜뷰가 집고 ack 가 없다 — (팜뷰 조용 + 90초) 또는 유효기간 10분 지남 → fv_unknown. 업데이터엔 안 준다.
        _unknown = [r for r in _rows if r["status"] == "fv_claimed" and r["pc_id"] == pc_id
                    and ((fv_quiet and str(r["updated_at"] or "") < ack_cut) or str(r["created_at"] or "") < cutoff)]
        _pend = [r for r in _rows if r["status"] == "pending"]
        _expired = [r["id"] for r in _pend if str(r["created_at"] or "") < cutoff]
        _live = [r for r in _pend if r["id"] not in _expired and r["pc_id"] == pc_id
                 and r["command"] not in UPDATER_NO_SUPERSEDE]
        # 같은 PC 키(브로드캐스트 행은 남의 PC 몫이라 안 건드린다)의 ★같은 종류★ 상태변경 명령은 최신 1건만
        #   ★이미 업데이터에 내준 행(handed_at, ack 전)은 덮지 않는다 (v2 반증 1부 B)★ — ack 가 사라진 사이 다시 누르면
        #   돈 명령이 superseded(«안 돎»)로 적혔다. 내준 행은 남겨 두어 늦은 ack 가 acked 로 적는다.
        _superseded = [i for i in _older_same_kind(_live) if not next(
            (r["handed_at"] for r in _live if r["id"] == i), None)]
        if _expired or _superseded or _unknown:
            _ts = _now()
            if _unknown:
                cur = await db.execute(
                    f"UPDATE updater_commands SET status='fv_unknown', updated_at=? "
                    f"WHERE status='fv_claimed' AND id IN ({','.join('?' for _ in _unknown)})",
                    (_ts, *[r["id"] for r in _unknown]))
                if unknown_out is not None and cur.rowcount:
                    unknown_out.extend({"id": r["id"], "command": r["command"]} for r in _unknown)
            if _expired:
                await db.execute(
                    # 업데이터에 내준 행(ack 만 사라짐)은 «만료(안 가져감)» 가 아니다 → handed_noack(v2 델타 반증 #3)
                    f"UPDATE updater_commands SET status=CASE WHEN handed_at IS NULL THEN 'expired' ELSE 'handed_noack' END, "
                    f"updated_at=? WHERE status='pending' AND id IN ({','.join('?' for _ in _expired)})",
                    (_ts, *_expired))
            if _superseded:
                await db.execute(
                    f"UPDATE updater_commands SET status='superseded', updated_at=? "
                    f"WHERE status='pending' AND id IN ({','.join('?' for _ in _superseded)})",
                    (_ts, *_superseded))
            await db.commit()
        # ★팜뷰가 같은 PC·같은 종류를 아직 돌리는 중이면(fv_claimed — ack 전) 그 뒤에 누른 것은 업데이터에 안 준다
        #   (배포 반증 3차 H8 — 팜뷰 restart X 가 도는 사이 업데이터가 Y 를 받아 두 길이 겹쳤다). 그 행은 건너뛰고
        #   다음 행을 준다(큐 머리를 막지 않는다). Y 는 팜뷰 목록(PC 당 최신)에 있어 팜뷰가 X 뒤에 차례로 돌린다.
        #   X 가 끝나면(fv_done·fv_failed) 또는 «모름»(fv_unknown, 팜뷰 조용 90초·10분)이면 풀린다.
        async with db.execute(
            """
            SELECT id, pc_id, command, args, created_at
            FROM updater_commands u
            WHERE (pc_id=? OR pc_id=?) AND status='pending'
              AND NOT EXISTS (SELECT 1 FROM updater_commands f
                              WHERE f.pc_id=u.pc_id AND f.command=u.command AND f.status='fv_claimed' AND f.id<u.id)
            ORDER BY id ASC LIMIT 1
            """,
            (pc_id, all_key),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    try:
        args = json.loads(row["args"])
    except Exception:
        args = {}
    return {"id": row["id"], "pc_id": row["pc_id"], "command": row["command"], "args": args, "created_at": row["created_at"]}


async def sweep_fv_claims(fv_quiet: bool) -> list:
    """★팜뷰가 집고 ack 가 없는 행을 모든 PC 에서 «모름» 으로 (배포 반증 3차 H7)★ — 이 판정이 업데이터 폴링
    (get_pending_updater_command) 안에만 있어서, 업데이터가 죽은 PC(팜뷰가 유일한 길인 경우)는 fv_claimed 로 영영
    남았다. 규칙은 같다: (팜뷰 조용 + FV_CLAIM_ACK_SEC) 또는 유효기간 지남. 한 행씩 바꿔 바꾼 행만 돌려준다
    [{id, pc_id, command}] — 호출부가 그 PC 로그에 한 줄씩(겹친 폴링과 두 번 안 적는다)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=UPDATER_COMMAND_MAX_AGE_SEC)).strftime("%Y-%m-%dT%H:%M:%S")
    ack_cut = (datetime.now(timezone.utc) - timedelta(seconds=FV_CLAIM_ACK_SEC)).strftime("%Y-%m-%dT%H:%M:%S")
    out = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, pc_id, command, created_at, updated_at FROM updater_commands WHERE status='fv_claimed'") as cur:
            rows = await cur.fetchall()
        for r in rows:
            if (fv_quiet and str(r[4] or "") < ack_cut) or str(r[3] or "") < cutoff:
                cur = await db.execute("UPDATE updater_commands SET status='fv_unknown', updated_at=? "
                                       "WHERE id=? AND status='fv_claimed'", (_now(), r[0]))
                if cur.rowcount:
                    out.append({"id": r[0], "pc_id": r[1], "command": r[2]})
        await db.commit()
    return out


async def recent_updater_commands(limit: int = 60) -> list[dict]:
    """★업데이터 명령 큐를 밖에서 볼 수 있게 (2026-08-22 사고 146)★

    주인님: "내가 업데이트를 눌러도 뭐 업데이트를 안 하는데 우짜냐 이거"
    그때 확인할 수단이 ★하나도 없었다★:
      · 대시보드 명령이력(/commands/recent)은 ★매크로 큐★ 만 읽는다
      · 업데이터 큐(updater_commands)에는 조회 엔드포인트가 아예 없었다
      · 업데이터 원격 로그는 3.1.6 기능인데 함대는 전원 3.1.5 → 전 PC 0줄
    → '눌렀다' / '갔다' / '됐다' 가 전부 구분 불가능했다(§A2 그 자체).
    이 함수 + GET /updater/commands/recent 가 최소한 ★'갔다'★ 를 보이게 한다.

    ★테넌트 필터는 여기서 하지 않는다★ — main 테넌트는 접두어가 ★없어서★
    LIKE 'prefix%' 로 좁히면 prefix 가 빈 문자열이 되어 ★남의 테넌트까지 전부★ 걸린다.
    (초판이 그렇게 짰다가 스스로 잡았다.) 호출부가 ns_of() 로 걸러 쓴다.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, pc_id, command, args, status, created_at, updated_at
            FROM updater_commands
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    out = []
    for r in rows:
        try:
            args = json.loads(r["args"])
        except Exception:
            args = {}
        out.append({
            "id": r["id"], "pc_id": r["pc_id"], "command": r["command"],
            "args": args, "status": r["status"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        })
    return out


UPDATER_BUSY_SEC = 90     # 업데이터가 같은 종류를 받아 간 뒤 이 시간 안의 새 누름은 업데이터 몫(main UPDCMD_UP_BUSY_SEC 와 같은 값)


async def claim_updater_command_for_fv(cmd_id: int, fv_id: "int | None" = None,
                                       busy_ids: "tuple | list" = (), busy_out: "list | None" = None) -> bool:
    """★팜뷰가 집으면 업데이터 큐에서 뺀다 (2026-09-23 팜뷰 반증 #1)★ — 같은 update 가 두 큐에 들어가
    업데이터(10초 폴링)와 팜뷰(에이전트 kill+restart)가 ★둘 다★ 실행할 수 있었다(사고 499 모양).
    pending 이고 유효기간(UPDATER_COMMAND_MAX_AGE_SEC) 안일 때만 fv_claimed 로 바꾼다 — 업데이터 폴링
    (get_pending_updater_command)은 pending 만 주므로 더는 안 나간다. False = 업데이터가 이미 가져갔거나
    (acked) 지났거나 덮였다 → 팜뷰도 실행하지 않는다.
    busy_ids: 업데이터 폴링이 이미 내준(ack 전, 아직 pending) 행 id — 옛 행 덮기에서 뺀다(배포 반증 3차 #3:
    업데이터가 실행 중인 행이 superseded 로 잘못 적혔다).
    busy_out: 업데이터가 같은 PC·같은 종류를 UPDATER_BUSY_SEC 안에 받아 ack 했으면 집지 않고 여기에 True 를 담는다
    (호출부는 팜뷰 목록에서 빼지 않고 다음 폴링에 다시 본다 — 그 사이 업데이터가 받아 가면 거기서 빠진다, H1·H5)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=UPDATER_COMMAND_MAX_AGE_SEC)
              ).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT pc_id, command, handed_at FROM updater_commands WHERE id=?", (cmd_id,)) as cur:
            r = await cur.fetchone()
        if not r or r[2]:
            # 없거나 ★이미 업데이터에 내준 행★(v2 반증 1부 B — 재배포로 메모리 소유가 비어도 DB 로 안다)
            await db.execute("ROLLBACK")
            return False
        # ★두 번 누름 (2026-09-23 반증 B1)★ — 같은 PC 에 ★같은 종류★ 의 더 새 명령이 대기 중이면 그게 이긴다(B-DB3 과
        #   같은 규칙, 배포 반증 #2 — 다른 종류는 덮지 않는다). 옛 대기 명령은 superseded 로 — 안 그러면 팜뷰가 새 것을,
        #   업데이터가 옛 것을 실행했다.
        async with db.execute(
            "SELECT 1 FROM updater_commands WHERE pc_id=? AND status='pending' AND id>? AND command=?",
            (r[0], cmd_id, r[1])) as cur:
            newer = await cur.fetchone()
        if newer:
            await db.execute("ROLLBACK")
            return False
        _bcut = (datetime.now(timezone.utc) - timedelta(seconds=UPDATER_BUSY_SEC)).strftime("%Y-%m-%dT%H:%M:%S")
        async with db.execute(
            "SELECT 1 FROM updater_commands WHERE pc_id=? AND command=? AND id<? AND "
            "((status='acked' AND updated_at >= ?) OR (status IN ('pending','handed_noack') AND handed_at >= ?))",
            (r[0], r[1], cmd_id, _bcut, _bcut)) as cur:
            busy = await cur.fetchone()
        if busy:
            await db.execute("ROLLBACK")
            if busy_out is not None:
                busy_out.append(True)
            return False
        cur = await db.execute(
            "UPDATE updater_commands SET status='fv_claimed', updated_at=? "
            "WHERE id=? AND status='pending' AND created_at >= ?",
            (_now(), cmd_id, cutoff))
        ok = cur.rowcount > 0
        if ok and fv_id is not None:
            await db.execute("INSERT OR REPLACE INTO updater_fv_claim(fv_id, ucmd_id, at) VALUES(?,?,?)",
                             (int(fv_id), cmd_id, _now()))
            _old = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
            await db.execute("DELETE FROM updater_fv_claim WHERE at < ?", (_old,))
        if ok:
            _busy = [int(x) for x in busy_ids if isinstance(x, int)]
            _nb = (f" AND id NOT IN ({','.join('?' for _ in _busy)})" if _busy else "")
            await db.execute(
                "UPDATE updater_commands SET status='superseded', updated_at=? "
                "WHERE pc_id=? AND status='pending' AND id<? AND command=? AND handed_at IS NULL" + _nb,
                (_now(), r[0], cmd_id, r[1], *_busy))
        await db.commit()
        return ok


async def release_updater_command_from_fv(cmd_id: int) -> bool:
    """★팜뷰가 실패하면 업데이터 큐로 되돌린다 (2026-09-23 배포 반증 #1)★ — 팜뷰는 설정에 없는 PC(404)·nobulk·
    에이전트 없는 PC 에서 늘 실패한다. 예전엔 fv_failed 로 끝나 ★그 PC 의 업데이트 절반이 사라졌다★. 팜뷰가
    `reached: true`(에이전트에 닿았다 — 두 번 실행 위험)라고 명시한 실패만 되돌리지 않는다(호출부 fv_updcmd_ack).
    유효기간이 지났으면 안 되돌린다(False — 호출부가 fv_failed 로 남긴다)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=UPDATER_COMMAND_MAX_AGE_SEC)
              ).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT pc_id, command FROM updater_commands WHERE id=? AND status IN ('fv_claimed','fv_unknown')",
                              (cmd_id,)) as cur:
            r = await cur.fetchone()
        if r and await _has_newer_same_kind(db, cmd_id, r[0], r[1]):
            # 같은 종류의 더 새 명령이 이미 가는 중·돌았다 → 옛 것은 되살리지 않는다(배포 반증 2차 F3)
            await db.execute("UPDATE updater_commands SET status='superseded', updated_at=? WHERE id=?", (_now(), cmd_id))
            await db.commit()
            return False
        cur = await db.execute(
            "UPDATE updater_commands SET status='pending', updated_at=? "
            "WHERE id=? AND status IN ('fv_claimed','fv_unknown') AND created_at >= ?", (_now(), cmd_id, cutoff))
        await db.commit()
        return cur.rowcount > 0


async def finish_updater_command_fv(cmd_id: int, ok) -> bool:
    """팜뷰 ack — fv_claimed 행에 결과를 남긴다(fv_done / fv_failed). 실패는 호출부가 먼저
    release_updater_command_from_fv 로 업데이터에 돌려주고, 못 돌려줄 때(만료·reached:true)만 여기로 온다."""
    # ★팜뷰가 쥔 행(fv_claimed·fv_unknown)만 끝낸다 (배포 반증 3차 #5)★ — pending 을 fv_done 으로 바꾸면 업데이터
    #   폴링이 await 사이에 들고 있던 그 행을 «끝난 명령» 인데도 내줄 수 있었다. 팜뷰가 집은 명령은 업데이터에
    #   회수되지 않으므로(#1) pending 인 짝 행은 «안-감 확실» 로 이미 업데이터 몫이다 — 건드리지 않는다.
    _from = ("fv_claimed", "fv_unknown")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE updater_commands SET status=?, updated_at=? WHERE id=? AND status IN ({','.join('?' for _ in _from)})",
            ("fv_done" if ok else "fv_failed", _now(), cmd_id, *_from))
        await db.commit()
        return cur.rowcount > 0


async def open_fv_claims() -> list:
    """팜뷰가 집었는데 아직 끝나지 않은 명령(fv_claimed, 유효기간 10분 안) [{fv_id, ucmd_id, pc_id, command, at}]
    ★fv_unknown 은 다시 세우지 않는다 (반증 v2 #2)★ — «다시 보내지 않았습니다» 라고 PC 로그에 적은 명령이다. 늦은 ack 는
    목록 없이도 id 로 짝 행을 찾아 닫는다(updater_command_for_fv_id).
    — ★재배포 뒤 팜뷰 목록을 다시 세운다 (배포 반증 3차 #2)★. 메모리 목록이 비면 팜뷰가 그 id 를 다시 못 봐 ack 를
    못 보내고, 행은 fv_claimed 로 10분 뒤 아무도 안 한 채 만료됐다. 다시 보이면 팜뷰는 UPD_DONE 으로 실행은 건너뛰고
    기억한 결과로 ack 만 다시 보낸다(fvdash pull_updcmd)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=UPDATER_COMMAND_MAX_AGE_SEC)
              ).strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT f.fv_id, u.id, u.pc_id, u.command, f.at FROM updater_fv_claim f "
                "JOIN updater_commands u ON u.id=f.ucmd_id "
                "WHERE u.status='fv_claimed' AND u.created_at >= ? ORDER BY f.fv_id", (cutoff,)) as cur:
            rows = await cur.fetchall()
    return [{"fv_id": r[0], "ucmd_id": r[1], "pc_id": r[2], "command": r[3], "at": r[4]} for r in rows]


async def mark_updater_handed(cmd_id: int) -> None:
    """업데이터에 ★실제로 내준★ 행에 내준 시각을 적는다 — 처음 한 번만(ack 전까지 매 폴링 다시 준다).
    get_pending 안에서 적지 않는다: 거기서 고른 행을 호출부가 안 줄 수도 있다(팜뷰가 먼저 집음) — 그러면 아무도 못 받았다
    (test_fv_found U-13, v2 반증 1부 B 첫 판)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE updater_commands SET handed_at=? WHERE id=? AND handed_at IS NULL AND status='pending'",
                         (_now(), cmd_id))
        await db.commit()


async def pc_clock_get(pc_id: str) -> list:
    """카드의 PC 시계 표본 [[서버 epoch, 서버−PC 초], …] (없으면 [])."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT samples FROM pc_clock WHERE pc_id=?", (pc_id,)) as cur:
            r = await cur.fetchone()
    try:
        v = json.loads(r[0]) if r else []
        return [[float(a), float(b)] for a, b in v][-20:] if isinstance(v, list) else []
    except Exception:
        return []


async def pc_clock_put(pc_id: str, samples: list) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO pc_clock(pc_id, samples) VALUES(?,?)", (pc_id, json.dumps(samples)))
        await db.commit()


async def updater_command_for_fv_id(fv_id: int) -> "dict | None":
    """팜뷰 ack 의 id → 짝 업데이터 행 {id, pc_id, command, status}. 모르면 None(옛 서버가 집은 것·2일 지난 것)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT u.id, u.pc_id, u.command, u.status FROM updater_fv_claim f "
                "JOIN updater_commands u ON u.id=f.ucmd_id WHERE f.fv_id=?", (int(fv_id),)) as cur:
            r = await cur.fetchone()
    return {"id": r[0], "pc_id": r[1], "command": r[2], "status": r[3]} if r else None


async def updater_command_status(cmd_id: int) -> "str | None":
    """한 행의 상태만(업데이터 폴링이 집은 직후 다시 본다 — 배포 반증 3차 #5)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT status FROM updater_commands WHERE id=?", (cmd_id,)) as cur:
            r = await cur.fetchone()
    return r[0] if r else None


async def supersede_updater_command(cmd_id: int) -> bool:
    """대기 중인 한 행을 superseded 로(업데이터가 받을 수 없는 옛 명령이 큐 머리를 막지 않게, 배포 반증 2차 F1b).
    ★업데이터에 이미 내준 행(handed_at)은 superseded(«안 돎») 가 아니라 handed_noack(«받아감 — ack 없음, 돌았을 수 있음»)★
    (v3 델타 반증 #3 — 팜뷰가 같은 종류 새 명령을 집으면 폴링이 이 옛 행을 치우는데, ack 가 끝내 안 오면 돈 명령이 «안 돎»
    으로 남았다). 늦은 ack 는 여전히 acked 로 적는다(ack_updater_command)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE updater_commands SET status=CASE WHEN handed_at IS NULL THEN 'superseded' "
                               "ELSE 'handed_noack' END, updated_at=? WHERE id=? AND status='pending'",
                               (_now(), cmd_id))
        await db.commit()
        return cur.rowcount > 0


async def ack_updater_command(cmd_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            # ★B-DB3★ pending 일 때만 — 늦은 ack 하나가 expired/superseded 흔적을 acked 로 뒤집지 않게
            #   (매크로 큐 ack_command 2026-09-11 과 같은 규칙, §A12).
            #   ★단 업데이터에 내준 행(handed_at)은 뒤집는다 (v2 반증 1부 B)★ — 받아서 돈 명령이다. 그 사이 팜뷰가 같은 종류
            #   새 명령을 집어 옛 행이 superseded 가 됐거나(updater_poll_command 의 _updcmd_fv_newer) 10분이 지나 expired 가 돼도
            #   늦은 ack 가 «돌았다» 를 적는다(안 적으면 돈 재시작이 «안 돎» 으로 남는다).
            "UPDATE updater_commands SET status='acked', updated_at=? WHERE id=? AND "
            "(status='pending' OR (status IN ('superseded','expired','handed_noack') AND handed_at IS NOT NULL))",
            (_now(), cmd_id),
        )
        await db.commit()
        return cur.rowcount > 0


# ── 로그 ─────────────────────────────────────────────────────────────────────

async def get_logs(pc_id: str, limit: int = 1000) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT level, message, created_at FROM logs WHERE pc_id=? ORDER BY id DESC LIMIT ?",
            (pc_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in reversed(rows)]


# ── FarmView 연동: 시각 이후 로그를 ★한 방에★ (2026-09-08 신설, 스키마 불변) ──────
#   get_logs 는 PC 하나씩이라 72대를 훑으면 쿼리가 72번이다. FarmView 는 주기적으로
#   "이 시각 이후 전부" 를 묻기 때문에 그 모양이 필요하다. ★읽기 전용 · 새 표 없음.★
#   ns_prefix: "" 면 main 테넌트(접두사 없음)를 뜻하지 않는다 — 전부를 준다.
#   호출부가 split_ns 로 걸러 쓴다(main 은 접두사가 없어 LIKE 로 못 좁힌다).

async def get_logs_since(since: str, limit: int = 500,
                         pc_id: "str | None" = None, until: "str | None" = None,
                         ns_prefix: "str | None" = None) -> list[dict]:
    """created_at > since 인 로그를 오래된 순으로. pc_id 를 주면 그 PC 만.

    ★★정렬을 커서와 맞췄다 (2026-09-11 전수조사 · 실측으로 정정)★★
      초판은 ★커서는 `created_at`, 정렬은 `id`★ 라 둘이 어긋나 있었다. 그러면
      ① SQLite 가 인덱스를 못 쓰고 표를 처음부터 훑는다 ② 잘림 경계에서 행이 샌다.

      ★실측 (144,000행, 개발컴)★ — FarmView 가 20초마다 부르는 그 쿼리다.
        인덱스 없음  · ORDER BY id          →  SCAN logs      ★8.96ms★
        인덱스 있음  · ORDER BY id          →  SCAN logs      ★9.31ms★ (인덱스가 무용)
        인덱스 있음  · ORDER BY created_at  →  SEARCH INDEX   ★0.05ms★  (약 180배)
      ★즉 인덱스만 달면 아무 소용이 없었다★ — 정렬까지 커서와 같은 칼럼으로 맞춰야
      비로소 인덱스를 탄다. (`idx_logs_created(created_at, id)`)

      ★무엇이 달라지나★ 잘림(LIMIT)이 걸릴 때 「다음 200줄」의 기준이
      삽입순(id)에서 ★시각순(created_at)★ 으로 바뀐다. 커서가 원래 `created_at`
      이므로 이쪽이 앞뒤가 맞는다 — 같은 시각 안에서는 id 로 다시 정렬해 안정적이다.
    """
    # ★B-FV2·B-FV5 (2026-09-23)★ until = 위 끝(★미만★) — 아직 안 닫힌 초의 행을 내주지 않으려고
    #   (fv_events 가 정한다). ns_prefix = 테넌트 거르기를 SQL 에서(get_recent_commands 와 같은 뜻:
    #   "" = main(접두사 없는 행)만, "t" = "t::%" 만) — 남의 테넌트 행이 LIMIT 을 먹어 커서가
    #   멈추던 것을 막는다. 둘 다 None 이면 SQL 이 예전과 글자까지 같다(시험 ①-b 가 그 SQL 을 본다).
    if until is not None or ns_prefix is not None:
        where, params = ["created_at > ?"], [since]
        if until is not None:
            where.append("created_at < ?"); params.append(until)
        if pc_id:
            where.append("pc_id = ?"); params.append(pc_id)
        if ns_prefix == "":
            where.append("pc_id NOT LIKE '%::%'")
        elif ns_prefix:
            where.append("pc_id LIKE ? ESCAPE '\\'"); params.append(_like_prefix(ns_prefix) + "::%")
        params.append(limit)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, pc_id, level, message, created_at FROM logs WHERE "
                + " AND ".join(where) + " ORDER BY created_at ASC, id ASC LIMIT ?", params,
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if pc_id:
            sql = ("SELECT id, pc_id, level, message, created_at FROM logs "
                   "WHERE created_at > ? AND pc_id = ? ORDER BY created_at ASC, id ASC LIMIT ?")
            params = (since, pc_id, limit)
        else:
            sql = ("SELECT id, pc_id, level, message, created_at FROM logs "
                   "WHERE created_at > ? ORDER BY created_at ASC, id ASC LIMIT ?")
            params = (since, limit)
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_commands_since(since: str, limit: int = 500, until: "str | None" = None,
                             ns_prefix: "str | None" = None) -> list[dict]:
    """이벤트 시각(updated_at 이 있으면 그것, 없으면 created_at)이 since 이후인 매크로 명령을 ★그 시각순★ 으로.

    ★B-FV5 (2026-09-23)★ 예전엔 ORDER BY id 였다 — 커서(이벤트 시각)와 정렬이 달라 LIMIT 에
    걸리면 ★어디까지 받았는지★ 를 말할 수 없었다(옛 id 가 방금 ack 되면 시각은 최신인데 앞줄에 선다).
    이제 get_logs_since 와 같은 규칙: 커서 = 정렬 = 이벤트 시각, 같은 시각 안에서는 id.
    WHERE 는 예전과 같은 뜻이다(updated_at ≥ created_at 이므로 「둘 중 하나가 since 뒤」 =
    「COALESCE 가 since 뒤」). 식 인덱스 idx_cmd_at 을 탄다(식이 글자 그대로 같아야 한다).
    until(미만)·ns_prefix 는 get_logs_since 와 같은 뜻."""
    _at = "COALESCE(updated_at, created_at)"
    where, params = [f"{_at} > ?"], [since]
    if until is not None:
        where.append(f"{_at} < ?"); params.append(until)
    if ns_prefix == "":
        where.append("pc_id NOT LIKE '%::%'")
    elif ns_prefix:
        where.append("pc_id LIKE ? ESCAPE '\\'"); params.append(_like_prefix(ns_prefix) + "::%")
    params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, pc_id, command, args, status, created_at, updated_at "
            "FROM commands WHERE " + " AND ".join(where) + f" ORDER BY {_at} ASC, id ASC LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── 캐릭터 세부정보 ───────────────────────────────────────────────────────────

def _ca_utc(ca) -> "str | None":
    """매크로 collected_at → 장부 at 과 같은 꼴(UTC '%Y-%m-%dT%H:%M:%S'). 못 읽으면 None.
    매크로 report_module._now() 도 UTC 이 꼴이다. 오프셋(+09:00·Z)이 붙어 오면 UTC 로 옮긴다."""
    if not ca or not isinstance(ca, str):
        return None
    try:
        d = datetime.fromisoformat(ca.strip().replace("Z", "+00:00"))
        if d.tzinfo is not None:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        if d.year < 2000:
            return None                      # 엉터리 시각 — 리눅스 strftime 은 1000 년 미만 %Y 를 0 으로 안 채워 strptime 이 죽었다(반증 v2 #6)
        return d.strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, OverflowError):      # 0001-01-01+09:00 같은 끝값은 astimezone 이 넘친다(반증 v2 #4)
        return None


KINA_REPLAY_MAX_AGE_H = 72     # 이보다 오래된 판매는 재전송 판정에 안 쓴다(반증 ②의 상한, 아래 독스트링)
# ★창고키나 값·표식의 범위 (2026-09-23 밤 배포 반증 D #2·#3)★ — 2^63 이상은 SQLite 가 OverflowError(500)였고,
#   2099 년 판독 시각·10^18 순번 하나가 저장되면 그 뒤 진짜 판독이 전부 «옛것» 이 돼 창고키나가 조용히 멈췄다.
KINA_MAX = 10 ** 15            # 창고키나 상한(1000조) — 넘으면 받는 자리에서 400
KINA_SEQ_MAX = 2 ** 53         # kina_seq 상한 — 넘으면 400

# ★char_info 세대 (2026-09-24 아이온2 — FV 스냅샷 폴백·#201 부류)★ char_info·kina_adjust 를 쓰는 곳 넷(upsert_char_info ·
#   adjust_char_kina · heal_reverted_kina · delete_pc_all_data)이 ★커밋 직후★ 올린다. main.fv_snapshot 은 조립 ★전★ 세대를 적어 두고
#   세대가 달라졌으면 3초 캐시도 120초 폴백도 안 준다 — 판매 차감·새 판독을 건너 옛 창고키나를 내지 않게.
#   자원 쪽에서 센다(A11) — 호출부마다 캐시를 비우던 방식은 새 쓰기 자리가 생기면 빠진다(스스로 막는 시험: test_fv_snap_fallback).
#   ★연결 안·커밋 직후★ 인 까닭: 커밋 뒤 연결 닫기가 예외를 내도(→ kina_adjust 503) 세대는 이미 올라가 있다.
_CHAR_INFO_GEN = [0]


def char_info_gen() -> int:
    return _CHAR_INFO_GEN[0]


def _char_info_bump() -> None:
    _CHAR_INFO_GEN[0] += 1
KINA_SEQ_JUMP_MAX = 10 ** 6    # 한 번에 이만큼 넘게 뛴 순번은 믿지 않는다(순번 없이 시각으로만 가른다)
KINA_FUTURE_S = 300            # 판독 시각이 보낸 시각(sent_at, 없으면 서버 수신)보다 이만큼 넘게 뒤면 판독을 버린다
KINA_TIE_S = 10                # 서버 시계로 옮긴 두 판독이 이만큼 안쪽이면 순번(있으면)이 순서를 정한다(두 전송의 지연 차이만큼 흔들린다)
KINA_SEEN_KEEP = 50            # 카드마다 기억하는 받은 판독 시각 수(재전송 판정 — 시계가 뒤로 뛰어도)
_TSF = "%Y-%m-%dT%H:%M:%S"


def kina_read_judge(read_at, sent_at, now: "datetime | None" = None) -> "tuple[str | None, str | None, str]":
    """판독 표식 → (PC 판독 시각 raw, 서버 시계로 옮긴 판독 시각 srv, 버린 까닭). 버리면 srv 는 None.
    ★PC 시계를 판매 순서 판정에 그대로 믿지 않는다 (배포 반증 D #4)★ — PC 시계가 10분 빠르면 판매 ★전★ 판독이
    판매 ★뒤★ 로 보여 재전송 때 차감이 되살아났다(1000, 맞는 값 700). 매크로가 보낸 시각(sent_at, 같은 PC 시계)이
    오면 서버 수신 시각과의 차이만큼 옮긴다: srv = raw + (서버 now − sent_at). 없으면 raw 그대로. 어느 쪽이든
    ★서버가 받은 시각보다 뒤일 수 없다★(판독은 보내기 전에 일어났다) → min(…, now). 그래서 저장되는 srv 는 미래가
    될 수 없고 판독 하나가 카드를 영영 얼리지 못한다(#3)."""
    raw = _ca_utc(read_at)
    if raw is None:
        return None, None, ""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    rd = datetime.strptime(raw, _TSF)
    sent = _ca_utc(sent_at)
    sd = datetime.strptime(sent, _TSF) if sent else None
    try:
        if rd > (sd or now) + timedelta(seconds=KINA_FUTURE_S):
            return raw, None, ("판독 시각이 보낸 시각보다 뒤" if sd else "판독 시각이 서버 시각보다 뒤") + f"({raw})"
        srv = min(rd + ((now - sd) if sd else timedelta(0)), now)
    except OverflowError:                  # 9999-12-31 같은 끝값
        return raw, None, f"판독 시각을 옮길 수 없음({raw}, sent_at {sent})"
    if srv < now - timedelta(hours=KINA_REPLAY_MAX_AGE_H):
        return raw, None, f"판독이 {KINA_REPLAY_MAX_AGE_H}시간보다 옛것({srv.strftime(_TSF)})"
    return raw, srv.strftime(_TSF), ""


async def _kina_ledger_replay(db, pc_id: str, received: int) -> tuple[int, list]:
    """★창고키나 재전송이 팜뷰 차감을 되돌리던 것 (2026-09-23 팜뷰방 진단, 5건 전부 되돌려짐)★
    매크로는 부팅·WS 재연결·15분 안 재수집(info_collector._resend_last_full, 사고 594)마다 ★옛 창고키나★ 를
    다시 보낸다 — 그 뒤 kina_adjust 로 뺀 만큼이 되살아났다. 이 함수는 ★판독 시각 표식(kina_read_at)이 없는
    옛 매크로★ 에만 쓰는 판정이다(표식이 있으면 upsert_char_info 가 판독 시각으로 정확히 가른다).
    규칙: 받은 값 == 최근 KINA_REPLAY_MAX_AGE_H 시간 안 판매의 before → 재전송으로 보고 그 판매(같은 before 중
    ★첫★ 것)부터 before==앞 after 로 ★바로 이어지는★ 판매와, 되돌려진 값(before==받은 값)에서 뺀 판매를 차례로
    적용한 끝 값. 둘 다 아니면(사슬이 끊겼다) 거기서 멈춘다.
    ★왜 시각(collected_at)으로 안 가르나★ (v2 가 그랬다가 반증에 깨짐):
      · _resend_last_full 은 옛 값을 ★새 시각★ 으로 보낸다(merge 아님 → send_char_info 가 _now()) → 시각 규칙은
        판매를 그대로 되살렸다 = 창고에 없는 키나가 보인다(팔 수 없는 것을 팔게 된다).
      · 창고키나 새 판독(사고 569)은 ★새 값 + 옛 시각★ 이라 시각만으로는 반대로 틀린다.
    ★틀리는 쪽을 고른 이유★: 값 규칙이 틀리면 ★덜★ 보인다(판매 뒤 새로 읽은 값이 우연히 옛 before 와 같을 때 —
      넘겨준 뒤 읽고 거래가 닫힌 판·둥근 값으로 다시 채운 판, 반증 F1·F2). 없는 키나를 파는 것보다 안전하고,
      72시간 상한이 그 기간을 묶는다(그 뒤 새 판독은 그대로 들어간다). 매크로가 kina_read_at 을 보내면 둘 다 닫힌다.
    · 멱등: 같은 값을 몇 번 다시 보내도 같은 after. 장부는 안 건드린다(새 행 없음 → 두 번 빼는 일 없음).
    반환 (적용할 값, 다시 적용한 tid 목록)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=KINA_REPLAY_MAX_AGE_H)).strftime("%Y-%m-%dT%H:%M:%S")
    async with db.execute(
        "SELECT tid, before, after FROM kina_adjust WHERE pc_id=? AND at >= ? ORDER BY at, rowid", (pc_id, cutoff)
    ) as cur:
        rows = [(r[0], int(r[1]), int(r[2])) for r in await cur.fetchall()]
    start = next((i for i, (_t, before, _a) in enumerate(rows) if before == received), None)
    if start is None:
        return received, []
    val, used = rows[start][2], [rows[start][0]]
    for tid, before, after in rows[start + 1:]:
        if before == val:                       # 사슬 — 앞 판매 뒤 값에서 이어진 판매
            val = after
        elif before == received:                # 되돌려진 값에서 뺀 판매(옛 서버가 남긴 모양, 반증 ①) — 그 몫도 뺀다
            val = max(0, val + (after - before))
        else:
            break                               # 사슬이 끊겼다(그 사이 새 판독) — 뒤의 우연한 같은 before 에 다시 잇지 않는다
        used.append(tid)
    return val, used


async def _kina_after_read(db, pc_id: str, read_at: str) -> tuple[int, list]:
    """판독 시각 read_at(UTC) ★뒤에★ 기록된 차감의 합과 tid — 그 판독은 이 판매들을 모른다."""
    async with db.execute(
        "SELECT tid, delta FROM kina_adjust WHERE pc_id=? AND at > ? ORDER BY at, rowid", (pc_id, read_at)
    ) as cur:
        rows = [(r[0], int(r[1])) for r in await cur.fetchall()]
    return sum(d for _t, d in rows), [t for t, _d in rows]


async def _kina_effective_now(db, pc_id: str) -> "tuple[int, int, list] | None":
    """카드의 ★지금 저장값★ 이 장부 차감 전 값으로 되돌려진 채인지 보고 사슬 끝 값을 준다.
    (저장값, 적용할 값, tid) — 되돌려진 게 아니면 tid 가 빈 목록. 카드 행이 없으면 None.
    판독 시각 표식(kina_read)이 있는 카드는 옛 판독을 안 받으므로 되돌림이 없다 — 그대로 둔다."""
    async with db.execute("SELECT total_kina FROM char_info WHERE pc_id=?", (pc_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    cur_v = int(row[0] or 0)
    async with db.execute("SELECT 1 FROM kina_read WHERE pc_id=? AND read_at IS NOT NULL", (pc_id,)) as cur:
        seq_mode = await cur.fetchone() is not None
    if seq_mode or not cur_v:
        return cur_v, cur_v, []
    eff, used = await _kina_ledger_replay(db, pc_id, cur_v)
    return cur_v, eff, used


async def _kina_seq_stale(db, pc_id: str, seq: int) -> bool:
    """순번만 온 판독이 마지막으로 받아들인 순번 이하인가(한 번에 너무 뛴 순번은 «모름» = False)."""
    async with db.execute("SELECT seq FROM kina_read WHERE pc_id=?", (pc_id,)) as cur:
        r = await cur.fetchone()
    if not r or r[0] is None:
        return False
    return int(seq) <= int(r[0])


async def heal_reverted_kina() -> list[dict]:
    """★부팅 한 번 — 이미 되돌려진 카드를 바로 고친다 (2026-09-23 P0 v3)★
    옛 서버가 재전송으로 덮어 판매 전 값(before)에 머문 카드를 다음 재전송을 기다리지 않고 사슬 끝 값으로.
    판정은 재전송 판정과 같은 함수(_kina_ledger_replay). 두 번 돌려도 같다(고친 값은 before 가 아니다).
    반환: 고친 카드 [{pc_id, before, after, tids}] — 호출부가 그 PC 로그에 한 줄씩 남긴다(A2)."""
    out = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT pc_id FROM kina_adjust") as cur:
            pcs = [r[0] for r in await cur.fetchall()]
        for pc in pcs:
            await db.execute("BEGIN IMMEDIATE")
            got = await _kina_effective_now(db, pc)
            if got and got[2] and got[1] != got[0]:
                await db.execute("UPDATE char_info SET total_kina=? WHERE pc_id=?", (got[1], pc))
                out.append({"pc_id": pc, "before": got[0], "after": got[1], "tids": got[2]})
            await db.commit()
            _char_info_bump()                  # ★커밋 뒤·연결 안★ — FV 스냅샷 폴백 세대(char_info_gen)
    return out


async def upsert_char_info(pc_id: str, total_kina: int, chars: list, merge: bool = False,
                           collected_at: str | None = None, replay_out: dict | None = None,
                           kina_seq=None, kina_read_at: str | None = None, sent_at: str | None = None,
                           sent_learned: bool = False, fresh_send: bool = False) -> list:
    """캐릭터 정보 저장. merge=True면 기존 chars에 slot 기준으로 병합(단일 캐릭 수집용 —
    한 슬롯만 보내도 나머지 슬롯이 안 지워짐). total_kina=0이면 기존값 유지.
    collected_at: 매크로가 준 '전체수집 시각'을 그대로 저장(2026-07-25) — 예전엔 저장 때마다
    _now()로 덮어써 부팅 재전송/단일수집에도 시각이 갱신되는 거짓말이 됐음. None이면 기존값
    유지(단일수집·시각 미제공 재전송), 기존값도 없으면 _now().
    반환: 최종 저장된 chars 리스트(WS 브로드캐스트용)."""
    chars = _finite(chars)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # 장부(kina_adjust)와 한 트랜잭션 — adjust_char_kina 가 사이에 끼면 그 차감을 덮어 쓴다
        await db.execute("BEGIN IMMEDIATE")
        try:
            received_kina = int(total_kina or 0)
        except (TypeError, ValueError, OverflowError):
            received_kina = 0
        if not 0 <= received_kina <= KINA_MAX:     # 받는 자리(main)가 400 으로 막는다 — 여기는 마지막 방어(모름 = 0)
            received_kina = 0
            total_kina = 0
        row = None
    # ★키나 보존은 merge 여부와 무관하다 (2026-09-11 전수조사)★
    #   초판은 이 조건이 False 면 기존 행을 아예 안 읽어서, 정상 전체수집에서
    #   키나만 못 읽은 판이 ★기존 창고키나를 0 으로 덮었다.★ 독스트링은
    #   「total_kina=0이면 기존값 유지」라고 약속하는데 그 갈래에서만 안 지켜졌다.
        if merge or not collected_at or not total_kina:
            async with db.execute(
                "SELECT total_kina, chars, collected_at FROM char_info WHERE pc_id=?", (pc_id,)
            ) as cur:
                row = await cur.fetchone()
        if merge:
            existing = []
            existing_kina = 0
            if row:
                try:
                    existing = json.loads(row["chars"]) or []
                except Exception:
                    existing = []
                existing_kina = row["total_kina"] or 0
            by_slot = {c.get("slot"): c for c in existing if isinstance(c, dict)}
            # ★B-JS1 (2026-09-23) 병합된 캐릭엔 ★그 캐릭만의 수집 시각★ 을 찍는다★ — 행의
            #   collected_at 은 「전체수집 시각」 으로 남기 때문에(2026-07-25 규약), 리셋 뒤 단일수집으로
            #   들어온 새 값(표층 00:00:00·각성 0)이 리셋 ★이전★ 값으로 보정돼 14:00:00·3/3 으로 떴다.
            #   서버 수신 시각(_now, UTC)을 쓴다 — 매크로 시계는 PC 마다 어긋난다. 안 찍힌 옛 캐릭은
            #   읽는 쪽이 행 시각으로 폴백한다(/characters · _fv_ticket_reset_aware).
            #   ★B-JS1 보강 (2026-09-23 병합 반증) 값이 ★실제로 바뀐★ 캐릭만 새 시각을 받는다★ — 매크로의
            #   merge 는 한 슬롯만 보내지 않는다: 창고키나(info_collector 사고 569)·단일수집·각성 0 기록
            #   (awakening._mark_slot_done)·표층(surface_zero.record)·부팅/WS 재연결 재전송(_push_local_char_info,
            #   로컬 json 에 merge=True 가 남아 있다) 이 전부 ★로컬 스냅샷 통째로★ merge=True 를 보낸다.
            #   통째로 찍으면 수요일 05시 리셋 전 값이 「방금 읽은 값」 이 돼 리셋 보정(/characters·_fv_char_agg)이
            #   꺼졌다(각성 3→0). 안 바뀐 캐릭은 옛 행 그대로(자기 시각이 있으면 그것, 없으면 행 시각 폴백).
            #   ★못 막는 것★: 리셋 전후 값이 같은 새 판독(각성 0→0·표층 00:00:00→00:00:00)은 서버가 옛 값과
            #   가를 수 없다 — 매크로가 「이 슬롯을 방금 읽었다」 표식을 보내야 한다(B-JS1 이전 동작과 같음).
            _merge_ts = _now()
            for c in chars:
                if isinstance(c, dict):
                    _old = by_slot.get(c.get("slot"))
                    _new = {k: v for k, v in c.items() if k != "collected_at"}
                    if isinstance(_old, dict) and                             {k: v for k, v in _old.items() if k != "collected_at"} == _new:
                        continue
                    by_slot[c.get("slot")] = dict(_new, collected_at=_merge_ts)
            # ★옛 행에 문자열 slot 이 섞여 있어도 정렬이 안 터지게 (2026-09-23)★
            chars = [by_slot[k] for k in sorted(
                by_slot, key=lambda x: (x is None, not isinstance(x, int), x if isinstance(x, int) else str(x)))]
            if not total_kina:
                total_kina = existing_kina
        # ★merge 가 아니어도 0 이면 기존값을 지킨다 (2026-09-11)★
        #   (merge 갈래는 위에서 이미 채웠으므로 여기서는 그대로 통과한다)
        if not total_kina and row is not None:
            try:
                total_kina = row["total_kina"] or 0
            except Exception:
                pass
        # ★받은 창고키나가 차감 전 값의 재전송이면 장부를 다시 적용한다(_kina_ledger_replay)★
        #   (0 → 기존값 유지 갈래에서 온 값은 이미 저장값이라 건드리지 않는다)
        # ★판독 표식이 오면 그것으로 가른다 (KF1, P0 v3)★ — kina_read_at(UTC, 창고를 ★실제로 읽은★ 시각 — 재전송·
        #   병합·15분 재수집에도 로컬에서 그대로 따라온다)·kina_seq(카드별 단조 증가, 선택). 마지막으로 받아들인
        #   판독보다 옛 것(재전송·보내기 실패 뒤 늦게 온 판독)은 창고키나를 안 덮는다(캐릭 정보는 그대로 저장).
        #   새 판독이면 그 판독 ★뒤★ 기록된 판매만큼 빼서 저장한다(판독이 모르는 판매, 반증 ③).
        #   표식이 없는 옛 매크로는 장부 사슬 판정(_kina_ledger_replay).
        _rd, _rd_srv, _rd_bad = kina_read_judge(kina_read_at, sent_at)
        if _rd_bad and sent_learned:
            # ★배운 어긋남은 버리는 근거가 못 된다 (v2 반증 1부 A 2b)★ — 시계를 NTP 로 고친 뒤 옛 어긋남으로 옮기면 진짜
            #   판독이 «보낸 시각보다 뒤» 로 버려져 창고키나가 얼었다. 모르면 PC 시계를 믿는다(버리지 않는다).
            _rd, _rd_srv, _rd_bad = kina_read_judge(kina_read_at, None)
        _seq = kina_seq if isinstance(kina_seq, int) and not isinstance(kina_seq, bool) \
            and 0 <= kina_seq < KINA_SEQ_MAX else None
        if received_kina and _rd_bad:
            # 판독 시각을 믿을 수 없다 → 창고키나를 안 덮는다(캐릭 정보는 저장), 표식도 안 적는다 — 증거는 PC 로그(호출부)
            async with db.execute("SELECT total_kina FROM char_info WHERE pc_id=?", (pc_id,)) as cur:
                _cur = await cur.fetchone()
            if _cur is not None:
                total_kina = int(_cur[0] or 0)
            if replay_out is not None:
                replay_out.update(received=received_kina, applied=total_kina, tids=[], stale=True, why=_rd_bad)
        elif received_kina and _rd_srv is not None:
            async with db.execute("SELECT seq, read_at, read_srv, seen FROM kina_read WHERE pc_id=?", (pc_id,)) as cur:
                _kr = await cur.fetchone()
            _old_seq, _old_rd, _old_srv = (_kr[0], _kr[1], _kr[2]) if _kr else (None, None, None)
            try:
                _seen = [x for x in json.loads(_kr[3] or "[]") if isinstance(x, str)] if _kr else []
            except Exception:
                _seen = []
            if _old_rd and _old_rd not in _seen:
                _seen.append(_old_rd)
            if _seq is not None and _old_seq is not None and _seq > int(_old_seq) + KINA_SEQ_JUMP_MAX:
                _seq = None                         # 한 번에 너무 뛴 순번 — 믿지 않는다(반증 D #3 순번 독)
            _both_seq = _seq is not None and _old_seq is not None
            # ★새 판독인가 (배포 반증 D)★ — 판독은 PC 판독 시각(raw)으로 식별한다(매크로는 새로 읽을 때만 새 raw,
            #   재전송은 같은 raw) → 이미 받은 raw 는 옛것(시계가 뒤로 뛴 뒤의 재전송도, K-8). 같은 raw 가 ★마지막★
            #   판독이면 순번이 커졌을 때만 새것(같은 초에 두 번 읽음 — 뮤턴트 <=→<). 처음 보는 raw 는 서버 시계로 옮긴
            #   시각(srv)이 가른다; 마지막 판독과 KINA_TIE_S 안쪽이면 순번(둘 다 있을 때), 없으면 (srv, raw) 순.
            #   순번만으로 정하지 않는다 — 로컬 파일을 잃어 순번이 1 로 돌아간 카드에선 옛 판독의 순번이 더 크다(K-5d).
            if _rd in _seen:
                _stale = not (_rd == _old_rd and _both_seq and _seq > int(_old_seq))
            elif not fresh_send and _both_seq and _seq < int(_old_seq) and _old_rd and _rd < _old_rd:
                # 순번도 판독 시각도 마지막보다 옛것 — 기억(seen 50개)에서 밀려난 옛 판독의 재전송(v2 반증 1부 3c·3d).
                #   둘 다 옛것일 때만 — 순번이 1 로 돌아간 카드(K-5c)는 판독 시각이 새것이라 여기 안 걸린다.
                #   방금 보낸 새 전송(fresh_send — 시계 표본 셋이 맞음)은 제외: 순번이 1 로 돌아가고 시계가 뒤로 고쳐진 카드의
                #   새 판독을 다 버렸다(반증 에이전트 #6). 시계가 막 고쳐졌으면 표본이 맞을 때까지(새 전송 셋) 이 규칙이 남는다.
                _stale = True
            elif _old_srv is not None:
                _gap = (datetime.strptime(_rd_srv, _TSF) - datetime.strptime(_old_srv, _TSF)).total_seconds()
                if abs(_gap) <= KINA_TIE_S and _both_seq:
                    _stale = _seq <= int(_old_seq)
                else:
                    _stale = (_rd_srv, _rd) <= (_old_srv, _old_rd or "")
            elif _old_rd is not None:
                _stale = _rd <= _old_rd               # read_srv 없던 판이 남긴 행(개발 DB)
            else:
                _stale = False
            if _stale:
                async with db.execute("SELECT total_kina FROM char_info WHERE pc_id=?", (pc_id,)) as cur:
                    _cur = await cur.fetchone()
                if _cur is not None:
                    total_kina = int(_cur[0] or 0)
                    if replay_out is not None and total_kina != received_kina:
                        replay_out.update(received=received_kina, applied=total_kina, tids=[], stale=True)
            else:
                # 판독이 모르는 판매 = 서버 시계로 옮긴 판독 시각 ★뒤★ 기록된 판매(반증 ③·D #4)
                _d, _tids = await _kina_after_read(db, pc_id, _rd_srv)
                total_kina = max(0, received_kina + _d)
                if _tids and replay_out is not None:
                    replay_out.update(received=received_kina, applied=total_kina, tids=_tids)
                _seen = ([x for x in _seen if x != _rd] + [_rd])[-KINA_SEEN_KEEP:]
                await db.execute("INSERT OR REPLACE INTO kina_read(pc_id, seq, read_at, read_srv, seen) VALUES(?,?,?,?,?)",
                                 (pc_id, _seq if _seq is not None else _old_seq, _rd, _rd_srv,
                                  json.dumps(_seen)))
        elif received_kina and _rd is None and _seq is not None and await _kina_seq_stale(db, pc_id, _seq):
            # 판독 시각 없이 순번만 온 옛 판독(재전송) — 창고키나를 안 덮는다. 새 순번이면 아래 장부 사슬로.
            async with db.execute("SELECT total_kina FROM char_info WHERE pc_id=?", (pc_id,)) as cur:
                _cur = await cur.fetchone()
            if _cur is not None:
                total_kina = int(_cur[0] or 0)
                if replay_out is not None and total_kina != received_kina:
                    replay_out.update(received=received_kina, applied=total_kina, tids=[], stale=True)
        elif received_kina:
            _eff, _used = await _kina_ledger_replay(db, pc_id, received_kina)
            if _used:
                if replay_out is not None:
                    replay_out.update(received=received_kina, applied=_eff, tids=_used)
                total_kina = _eff
        if not collected_at:
            collected_at = (row["collected_at"] if row else None) or _now()
        await db.execute(
            "INSERT OR REPLACE INTO char_info(pc_id, total_kina, chars, collected_at) VALUES(?,?,?,?)",
            (pc_id, total_kina, json.dumps(chars, ensure_ascii=False), collected_at),
        )
        await db.commit()
        _char_info_bump()                  # ★커밋 뒤·연결 안★ — FV 스냅샷 폴백 세대(char_info_gen)
    return chars


async def get_all_char_info() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            # ★kina_ledger (2026-09-24 #114 FV8)★ — 창고키나 0 이 «진짜 0» 인지 가르는 증거. 매크로가 보낸 0 은 기존값을
            #   안 덮으므로(upsert_char_info «total_kina=0이면 기존값 유지») 저장된 0 은 ★장부 차감으로만★ 생긴다 —
            #   장부 행이 없으면 «한 번도 못 읽음» 의 기본값 0 이다.
            #   ★before > 0 인 행만 (f868aa6 반증 FV8 가장자리)★ — 한 번도 못 읽은 카드(저장 0)에 판매를 적으면
            #   before=0·after=0 행이 생겨 그 0 이 «앎» 으로 읽혔다. 아는 값에서 뺀 행이어야 0 도 앎이다.
            "SELECT c.pc_id, c.total_kina, c.chars, c.collected_at, "
            "EXISTS(SELECT 1 FROM kina_adjust k WHERE k.pc_id = c.pc_id AND k.before > 0) AS kina_ledger "
            "FROM char_info c ORDER BY c.pc_id"
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for row in rows:
        try:
            chars = json.loads(row["chars"])
        except Exception:
            chars = []
        result.append({
            "pc_id": row["pc_id"],
            "total_kina": row["total_kina"],
            "chars": chars,
            "collected_at": row["collected_at"],
            "kina_ledger": bool(row["kina_ledger"]),
        })
    return result


async def get_char_info(pc_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pc_id, total_kina, chars, collected_at FROM char_info WHERE pc_id=?", (pc_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    try:
        chars = json.loads(row["chars"])
    except Exception:
        chars = []
    return {
        "pc_id": row["pc_id"],
        "total_kina": row["total_kina"],
        "chars": chars,
        "collected_at": row["collected_at"],
    }


async def adjust_char_kina(pc_id: str, tid: str, delta: int, why: dict) -> dict | None:
    """창고 키나를 delta 만큼 옮긴다(팜뷰 «팔린 만큼 줄인다», 2026-09-13).
    · char_info 행이 없으면 None(→ 404). collected_at 은 ★안 건드린다★ — 수집 시각은 매크로 것.
    · 같은 tid 는 두 번 빼지 않는다 → {"dup": True, before=after=지금 값}.
    · after = max(0, before + delta). BEGIN IMMEDIATE 로 읽기-쓰기를 한 트랜잭션에."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        # ★되돌려진 채인 카드에 새 판매가 오면 먼저 장부를 다시 적용한다 (2026-09-23 P0 v3 반증 ①)★ — 예전엔
        #   저장값(되돌려진 before)에서 바로 빼서 새 행의 before 가 옛 행의 before 와 같아졌고, 다음 재전송이
        #   새 행만 따라가 ★첫 판매가 영영 사라졌다★(1000→800 되돌림 → 1000→900 → 재전송 1000 = 900, 맞는 값 700).
        got = await _kina_effective_now(db, pc_id)
        if got is None:
            await db.execute("ROLLBACK")
            return None
        stored, before, healed = got
        async with db.execute("SELECT pc_id FROM kina_adjust WHERE tid=?", (tid,)) as cur:
            seen = await cur.fetchone()
        if seen:
            await db.execute("ROLLBACK")
            if seen[0] != pc_id:
                # ★다른 카드에 이미 쓴 tid (배포 반증 D)★ — 예전엔 dup:true·ok:true 로 답해 팜뷰가 «뺐다» 로 적었는데
                #   이 카드에선 아무것도 안 뺐다. 두 번 빼지도 «됐다» 고 하지도 않고 거절한다(호출부 409).
                return {"pc_id": pc_id, "before": before, "after": before, "conflict": seen[0]}
            return {"pc_id": pc_id, "before": before, "after": before, "dup": True}
        after = max(0, before + int(delta))
        if after > KINA_MAX:
            await db.execute("ROLLBACK")          # 2^63 근처에서 SQLite OverflowError(500)였다 (반증 D #2)
            return {"pc_id": pc_id, "before": before, "after": before, "too_big": True}
        await db.execute(
            "INSERT INTO kina_adjust(tid, pc_id, delta, before, after, why, at) VALUES(?,?,?,?,?,?,?)",
            (tid, pc_id, int(delta), before, after, json.dumps(why or {}, ensure_ascii=False), _now()))
        await db.execute("UPDATE char_info SET total_kina=? WHERE pc_id=?", (after, pc_id))
        await db.commit()
        _char_info_bump()                  # ★커밋 뒤·연결 안★ — FV 스냅샷 폴백 세대(char_info_gen)
    res = {"pc_id": pc_id, "before": before, "after": after, "dup": False}
    if healed:
        res["healed"] = {"stored": stored, "tids": healed}     # 호출부가 로그줄로 남긴다(A2)
    return res


async def kina_adjust_row(tid: str) -> dict | None:
    """장부 한 줄(tid) — dup 재전송 때 원래 차감 값으로 로그줄을 되살리는 데 쓴다(2026-09-24 아이온2 #201 후속)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT tid, pc_id, delta, before, after, why, at FROM kina_adjust WHERE tid=?", (tid,)) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["why"] = json.loads(d.get("why") or "{}") or {}
    except Exception:
        d["why"] = {}
    return d


async def log_has(pc_id: str, needle: str) -> bool:
    """그 PC 로그에 needle 글자가 든 줄이 있나(instr — LIKE 의 %·_ 이스케이프가 필요 없다). 없으면 False."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM logs WHERE pc_id=? AND instr(message, ?) > 0 LIMIT 1", (pc_id, needle)) as cur:
            return (await cur.fetchone()) is not None


# ── 악몽 진행 상태 ──────────────────────────────────────────────────────────

async def upsert_nightmare_progress(pc_id: str, slot: int, tab: str, bosses: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO nightmare_progress(pc_id, slot, tab, bosses, updated_at) VALUES(?,?,?,?,?)",
            (pc_id, slot, tab, json.dumps(bosses, ensure_ascii=False), _now()),
        )
        await db.commit()


async def get_nightmare_progress(pc_id: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pc_id, slot, tab, bosses, updated_at FROM nightmare_progress WHERE pc_id=? ORDER BY slot",
            (pc_id,)
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for row in rows:
        try:
            bosses = json.loads(row["bosses"])
        except Exception:
            bosses = {}
        result.append({
            "pc_id": row["pc_id"],
            "slot": row["slot"],
            "tab": row["tab"],
            "bosses": bosses,
            "updated_at": row["updated_at"],
        })
    return result


async def get_all_nightmare_progress() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pc_id, slot, tab, bosses, updated_at FROM nightmare_progress ORDER BY pc_id, slot"
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for row in rows:
        try:
            bosses = json.loads(row["bosses"])
        except Exception:
            bosses = {}
        result.append({
            "pc_id": row["pc_id"],
            "slot": row["slot"],
            "tab": row["tab"],
            "bosses": bosses,
            "updated_at": row["updated_at"],
        })
    return result


# ── 슬롯 필터 (캐릭별 활성화/비활성화) ────────────────────────────────────────

async def upsert_slot_filters(pc_id: str, filters: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO slot_filters(pc_id, filters) VALUES(?,?)",
            (pc_id, json.dumps(filters)),
        )
        await db.commit()


async def get_slot_filters(pc_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT filters FROM slot_filters WHERE pc_id=?", (pc_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["filters"])
    except Exception:
        return {}


async def get_all_slot_filters() -> dict:
    """pc_id -> filters dict 전체 반환"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT pc_id, filters FROM slot_filters") as cur:
            rows = await cur.fetchall()
    result = {}
    for row in rows:
        try:
            result[row["pc_id"]] = json.loads(row["filters"])
        except Exception:
            result[row["pc_id"]] = {}
    return result
