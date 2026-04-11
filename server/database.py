"""database.py — aiosqlite 기반 SQLite 헬퍼"""
import aiosqlite
import os
import json
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "/data/macro_control.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


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
        await db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ── PC 상태 ─────────────────────────────────────────────────────────────────

async def upsert_status(pc_id: str, data: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO pc_status(pc_id, data, updated_at) VALUES(?,?,?)",
            (pc_id, json.dumps(data, ensure_ascii=False), _now()),
        )
        await db.commit()


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


async def get_pending_command(pc_id: str) -> dict | None:
    """pc_id 또는 'all' 명령 중 가장 오래된 pending 항목 반환"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, pc_id, command, args, created_at
            FROM commands
            WHERE (pc_id=? OR pc_id='all') AND status='pending'
            ORDER BY id ASC LIMIT 1
            """,
            (pc_id,),
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


async def get_recent_commands(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── 로그 ─────────────────────────────────────────────────────────────────────

async def insert_log(pc_id: str, level: str, message: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs(pc_id, level, message, created_at) VALUES(?,?,?,?)",
            (pc_id, level, message, _now()),
        )
        await db.commit()
    # 오래된 로그 자동 정리 (PC당 최대 500개)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM logs WHERE pc_id=? AND id NOT IN (
                SELECT id FROM logs WHERE pc_id=? ORDER BY id DESC LIMIT 500
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


async def get_pending_updater_command(pc_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, pc_id, command, args, created_at
            FROM updater_commands
            WHERE (pc_id=? OR pc_id='all') AND status='pending'
            ORDER BY id ASC LIMIT 1
            """,
            (pc_id,),
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

async def upsert_char_info(pc_id: str, total_kina: int, chars: list) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO char_info(pc_id, total_kina, chars, collected_at) VALUES(?,?,?,?)",
            (pc_id, total_kina, json.dumps(chars, ensure_ascii=False), _now()),
        )
        await db.commit()


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
