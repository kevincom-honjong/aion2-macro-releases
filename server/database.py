"""database.py — aiosqlite 기반 SQLite 헬퍼"""
import aiosqlite
import os
import json
from datetime import datetime, timezone, timedelta

DB_PATH = os.getenv("DB_PATH", "/data/macro_control.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# 같은 PC의 사망을 이 초 이내 중복 기록하지 않음(사망→부활 처리 중 상태 오가며 중복 방지).
DEATH_DEBOUNCE_SEC = 60


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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS char_info (
                pc_id        TEXT PRIMARY KEY,
                total_kina   INTEGER DEFAULT 0,
                chars        TEXT DEFAULT '[]',
                collected_at TEXT NOT NULL
            )
        """)
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


async def delete_pc_all_data(pc_id: str) -> None:
    """pc_id 관련 모든 테이블 데이터 삭제 (완전 제거)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pc_status        WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM updater_status   WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM commands         WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM updater_commands WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM logs             WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM char_info        WHERE pc_id=?", (pc_id,))
        await db.execute("DELETE FROM death_events      WHERE pc_id=?", (pc_id,))
        await db.commit()


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


async def get_pending_command(pc_id: str, all_key: str = "all") -> dict | None:
    """pc_id 또는 브로드캐스트(all_key) 명령 중 가장 오래된 pending 항목 반환.
    all_key: 테넌트 스코프된 'all' 키(예: 't::all') — 리터럴 'all' 고정은 테넌트 우회라 제거(2026-07-26)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, pc_id, command, args, created_at
            FROM commands
            WHERE (pc_id=? OR pc_id=?) AND status='pending'
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
    return {
        "id": row["id"],
        "command": row["command"],
        "args": args,
        "created_at": row["created_at"],
    }


async def ack_command(cmd_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE commands SET status='acked', updated_at=? WHERE id=?",
            (_now(), cmd_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def cancel_command(cmd_id: int) -> bool:
    """pending 상태의 명령을 cancelled로 변경 (이미 acked면 취소 불가)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE commands SET status='cancelled', updated_at=? WHERE id=? AND status='pending'",
            (_now(), cmd_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_recent_commands(limit: int = 20, ns_prefix: "str | None" = None) -> list[dict]:
    """ns_prefix: None=전체(호환), ""=main(네임스페이스 없는 행만), "t"=해당 테넌트("t::%") 행만.
    테넌트별 최근 N건을 정확히 주기 위함 — 전역 창에서 필터하면 타 테넌트 폭주 시 내역이 비어 보임(2026-07-26)."""
    where, params = "", []
    if ns_prefix == "":
        where = "WHERE pc_id NOT LIKE '%::%'"
    elif ns_prefix:
        where = "WHERE pc_id LIKE ?"
        params.append(ns_prefix + "::%")
    params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM commands {where} ORDER BY id DESC LIMIT ?", params
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


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
    """그 PC의 대기 요청을 모두 소거 — 코드를 받았으면 남은 후보에서 빼야 오라우팅이 없다."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM telegram_map WHERE pc_id=?", (pc_id,))
        await db.commit()


async def get_updater_command_pc(cmd_id: int) -> "str | None":
    """업데이터 명령 id의 pc_id(저장 키) 단건 조회 — 소유 테넌트 판정용(2026-07-26)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT pc_id FROM updater_commands WHERE id=?", (cmd_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


# ── 로그 ─────────────────────────────────────────────────────────────────────

async def insert_log(pc_id: str, level: str, message: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs(pc_id, level, message, created_at) VALUES(?,?,?,?)",
            (pc_id, level, message, _now()),
        )
        await db.commit()
    # 오래된 로그 자동 정리 (PC당 최대 3000개 — 스팸 로그는 클라에서 서버 전송 제외하므로
    # 중요 이벤트만 3000개면 몇 시간~며칠치 보존됨)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM logs WHERE pc_id=? AND id NOT IN (
                SELECT id FROM logs WHERE pc_id=? ORDER BY id DESC LIMIT 3000
            )
            """,
            (pc_id, pc_id),
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


async def get_pending_updater_command(pc_id: str, all_key: str = "all") -> dict | None:
    """all_key: 테넌트 스코프된 'all' 키 — 리터럴 고정은 테넌트 우회라 제거(2026-07-26)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, pc_id, command, args, created_at
            FROM updater_commands
            WHERE (pc_id=? OR pc_id=?) AND status='pending'
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
    return {"id": row["id"], "command": row["command"], "args": args, "created_at": row["created_at"]}


async def ack_updater_command(cmd_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE updater_commands SET status='acked', updated_at=? WHERE id=?",
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


# ── 캐릭터 세부정보 ───────────────────────────────────────────────────────────

async def upsert_char_info(pc_id: str, total_kina: int, chars: list, merge: bool = False,
                           collected_at: str | None = None) -> list:
    """캐릭터 정보 저장. merge=True면 기존 chars에 slot 기준으로 병합(단일 캐릭 수집용 —
    한 슬롯만 보내도 나머지 슬롯이 안 지워짐). total_kina=0이면 기존값 유지.
    collected_at: 매크로가 준 '전체수집 시각'을 그대로 저장(2026-07-25) — 예전엔 저장 때마다
    _now()로 덮어써 부팅 재전송/단일수집에도 시각이 갱신되는 거짓말이 됐음. None이면 기존값
    유지(단일수집·시각 미제공 재전송), 기존값도 없으면 _now().
    반환: 최종 저장된 chars 리스트(WS 브로드캐스트용)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = None
        if merge or not collected_at:
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
            for c in chars:
                if isinstance(c, dict):
                    by_slot[c.get("slot")] = c
            chars = [by_slot[k] for k in sorted(by_slot, key=lambda x: (x is None, x))]
            if not total_kina:
                total_kina = existing_kina
        if not collected_at:
            collected_at = (row["collected_at"] if row else None) or _now()
        await db.execute(
            "INSERT OR REPLACE INTO char_info(pc_id, total_kina, chars, collected_at) VALUES(?,?,?,?)",
            (pc_id, total_kina, json.dumps(chars, ensure_ascii=False), collected_at),
        )
        await db.commit()
    return chars


async def get_all_char_info() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pc_id, total_kina, chars, collected_at FROM char_info ORDER BY pc_id"
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
