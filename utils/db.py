import os
import sqlite3
from typing import Optional, Dict

ROOT = os.path.dirname(os.path.dirname(__file__))
DB_DIR = os.path.join(ROOT, 'database')
DB_PATH = os.path.join(DB_DIR, 'warera_users.db')


def _connect():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the users table and indexes if they don't exist."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_username TEXT,
                display_name TEXT,
                api_id TEXT UNIQUE
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_discord_username ON users(discord_username)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_display_name ON users(display_name)")
        conn.commit()


def save_user(discord_username: str | None, display_name: str | None, api_id: str) -> None:
    """Insert or update a user mapping.

    Keeps the latest discord/display names for a given `api_id`.
    """
    if api_id is None:
        return
    with _connect() as conn:
        cur = conn.cursor()
        # Use UPSERT semantics on api_id (unique)
        cur.execute(
            """
            INSERT INTO users (discord_username, display_name, api_id)
            VALUES (?, ?, ?)
            ON CONFLICT(api_id) DO UPDATE SET
                discord_username=excluded.discord_username,
                display_name=excluded.display_name
            """,
            (discord_username, display_name, api_id),
        )
        conn.commit()


def find_api_id_by_display_name(display_name: str) -> Optional[str]:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT api_id FROM users WHERE display_name = ? LIMIT 1", (display_name,))
        row = cur.fetchone()
        return row['api_id'] if row else None


def find_api_id_by_discord_username(discord_username: str) -> Optional[str]:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT api_id FROM users WHERE discord_username = ? LIMIT 1", (discord_username,))
        row = cur.fetchone()
        return row['api_id'] if row else None


def get_record_by_api_id(api_id: str) -> Optional[Dict]:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT discord_username, display_name, api_id FROM users WHERE api_id = ? LIMIT 1", (api_id,))
        row = cur.fetchone()
        return dict(row) if row else None
