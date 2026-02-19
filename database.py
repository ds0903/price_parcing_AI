"""
database.py — PostgreSQL schema manager.

Run directly to create or update all tables:
    python database.py

Tables are created with IF NOT EXISTS — safe to run multiple times.
New columns are added with ALTER TABLE ... ADD COLUMN IF NOT EXISTS — existing
data is never lost.
"""

import asyncio
import logging
from urllib.parse import urlparse, urlunparse

import asyncpg

from config import DATABASE_URL

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


# ------------------------------------------------------------------ #
#  Auto-create database                                                #
# ------------------------------------------------------------------ #

async def ensure_database_exists() -> None:
    """Connect to the 'postgres' system DB and create our DB if it doesn't exist."""
    parsed = urlparse(DATABASE_URL)
    db_name = parsed.path.lstrip("/")

    # Build URL pointing to the default 'postgres' maintenance database
    system_url = urlunparse(parsed._replace(path="/postgres"))

    conn = await asyncpg.connect(system_url)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )
        if not exists:
            # CREATE DATABASE cannot run inside a transaction block
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            logger.info("Database '%s' created.", db_name)
        else:
            logger.info("Database '%s' already exists.", db_name)
    finally:
        await conn.close()

# ------------------------------------------------------------------ #
#  Connection pool                                                     #
# ------------------------------------------------------------------ #

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ------------------------------------------------------------------ #
#  Schema migrations (idempotent — safe to run many times)            #
# ------------------------------------------------------------------ #

_CREATE_SCHEDULED_TASKS = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    user_id          BIGINT PRIMARY KEY,
    chat_id          BIGINT  NOT NULL,
    interval_minutes INT     NOT NULL,
    query            TEXT    NOT NULL,
    platform         TEXT    NOT NULL DEFAULT 'prom',
    created_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
"""

_CREATE_USER_SETTINGS = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id     BIGINT PRIMARY KEY,
    platform    TEXT NOT NULL DEFAULT 'prom',
    output_mode TEXT NOT NULL DEFAULT 'chat',
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
"""

# List of (table, column, definition) — add new columns here as the project grows
_ADD_COLUMNS: list[tuple[str, str, str]] = [
    ("scheduled_tasks", "platform", "TEXT NOT NULL DEFAULT 'prom'"),
]


async def init_db() -> None:
    """Create tables and add missing columns. Safe to call on every bot start."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_CREATE_SCHEDULED_TASKS)
        logger.info("Table scheduled_tasks — OK")

        await conn.execute(_CREATE_USER_SETTINGS)
        logger.info("Table user_settings — OK")

        for table, column, definition in _ADD_COLUMNS:
            await conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition};"
            )
            logger.info("Column %s.%s — OK", table, column)

    logger.info("Database schema is up to date.")


# ------------------------------------------------------------------ #
#  User settings CRUD  (memory cache → PostgreSQL)                    #
# ------------------------------------------------------------------ #

_DEFAULT_SETTINGS: dict = {"platforms": ["prom"], "output_mode": "chat"}
_settings_cache: dict[int, dict] = {}


async def get_user_settings(user_id: int) -> dict:
    """Return settings from cache; load from DB on first access.
    'platforms' is always a list[str] (e.g. ['prom', 'rozetka']).
    """
    if user_id in _settings_cache:
        return _settings_cache[user_id]

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT platform, output_mode FROM user_settings WHERE user_id = $1",
            user_id,
        )
    if row:
        settings = {
            "platforms": [p for p in row["platform"].split(",") if p],
            "output_mode": row["output_mode"],
        }
    else:
        settings = {
            "platforms": list(_DEFAULT_SETTINGS["platforms"]),
            "output_mode": _DEFAULT_SETTINGS["output_mode"],
        }
    _settings_cache[user_id] = settings
    return settings


async def save_user_settings(user_id: int, **kwargs) -> None:
    """Update one or more settings fields and persist to DB.
    Pass 'platforms' as list[str].
    """
    settings = await get_user_settings(user_id)
    settings.update(kwargs)
    _settings_cache[user_id] = settings

    platforms_str = ",".join(settings.get("platforms") or ["prom"])
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_settings (user_id, platform, output_mode)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET
                platform    = EXCLUDED.platform,
                output_mode = EXCLUDED.output_mode,
                updated_at  = NOW()
            """,
            user_id, platforms_str, settings["output_mode"],
        )


# ------------------------------------------------------------------ #
#  Scheduled tasks CRUD                                               #
# ------------------------------------------------------------------ #

async def save_schedule(user_id: int, chat_id: int, interval: int, query: str, platform: str = "prom") -> None:
    """Insert or update a scheduled task for the user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO scheduled_tasks (user_id, chat_id, interval_minutes, query, platform)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id) DO UPDATE SET
                chat_id          = EXCLUDED.chat_id,
                interval_minutes = EXCLUDED.interval_minutes,
                query            = EXCLUDED.query,
                platform         = EXCLUDED.platform,
                created_at       = NOW()
            """,
            user_id, chat_id, interval, query, platform,
        )


async def delete_schedule(user_id: int) -> None:
    """Remove a scheduled task for the user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM scheduled_tasks WHERE user_id = $1", user_id
        )


async def get_all_schedules() -> list[dict]:
    """Return all scheduled tasks (used on bot startup to restore tasks)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, chat_id, interval_minutes, query, platform FROM scheduled_tasks"
        )
        return [dict(row) for row in rows]


# ------------------------------------------------------------------ #
#  Run as script: python database.py                                  #
# ------------------------------------------------------------------ #

async def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set in .env")
        return
    try:
        await ensure_database_exists()
        await init_db()
        print("Done. Database and all tables are ready.")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
