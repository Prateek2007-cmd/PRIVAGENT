"""Persistent SQLite storage for multi-turn chats, messages, and session memory."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

import os
import sys

DB_DIR = Path(__file__).resolve().parents[1] / "data"


def get_default_db_path() -> Path:
    env_override = os.environ.get("PRIVAGENT_DB_PATH")
    if env_override:
        return Path(env_override)
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return DB_DIR / "test_privagent.db"
    return DB_DIR / "privagent.db"


DEFAULT_DB_PATH = DB_DIR / "privagent.db"


def _get_db_path(override_path: Optional[str] = None) -> str:
    if override_path:
        return override_path
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return str(get_default_db_path())


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize SQLite tables if they do not exist."""
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                evidence TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_memory (
                chat_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (chat_id, key),
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
            CREATE INDEX IF NOT EXISTS idx_chats_updated_at ON chats(updated_at DESC);
            """
        )


def create_chat(title: Optional[str] = None, db_path: Optional[str] = None) -> dict[str, Any]:
    chat_id = uuid.uuid4().hex
    display_title = (title or "New Mission").strip()
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO chats (id, title, status) VALUES (?, ?, 'active')",
            (chat_id, display_title),
        )
    return get_chat(chat_id, db_path=db_path) or {"id": chat_id, "title": display_title}


def list_chats(db_path: Optional[str] = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, title, created_at, updated_at, status
            FROM chats
            ORDER BY updated_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_chat(chat_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at, status FROM chats WHERE id = ?",
            (chat_id,),
        ).fetchone()
        if not row:
            return None
        chat = dict(row)
        chat["messages"] = get_messages(chat_id, limit=50, db_path=db_path)
        chat["memory"] = get_chat_memory(chat_id, db_path=db_path)
        return chat


def delete_chat(chat_id: str, db_path: Optional[str] = None) -> bool:
    with get_connection(db_path) as conn:
        cur = conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        return cur.rowcount > 0


def update_chat_title(chat_id: str, title: str, db_path: Optional[str] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE chats SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (title.strip(), chat_id),
        )


def touch_chat(chat_id: str, db_path: Optional[str] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE chats SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (chat_id,),
        )


def add_message(
    chat_id: str,
    role: str,
    content: str,
    evidence: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    msg_id = uuid.uuid4().hex
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO messages (id, chat_id, role, content, evidence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (msg_id, chat_id, role, content, evidence),
        )
        conn.execute(
            "UPDATE chats SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (chat_id,),
        )
    return {
        "id": msg_id,
        "chat_id": chat_id,
        "role": role,
        "content": content,
        "evidence": evidence,
    }


def get_messages(chat_id: str, limit: int = 20, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, chat_id, role, content, evidence, created_at
            FROM messages
            WHERE chat_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def set_chat_memory(chat_id: str, key: str, value: str, db_path: Optional[str] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO chat_memory (chat_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value
            """,
            (chat_id, key.strip(), str(value).strip()),
        )


def get_chat_memory(chat_id: str, db_path: Optional[str] = None) -> dict[str, str]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM chat_memory WHERE chat_id = ?",
            (chat_id,),
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}


def delete_chat_memory(chat_id: str, key: str, db_path: Optional[str] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "DELETE FROM chat_memory WHERE chat_id = ? AND key = ?",
            (chat_id, key),
        )


def get_user_preferences(db_path: Optional[str] = None) -> dict[str, str]:
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM user_preferences ORDER BY key ASC;").fetchall()
        return {r["key"]: r["value"] for r in rows}


def set_user_preference(key: str, value: str, db_path: Optional[str] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_preferences (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key.strip().lower(), str(value).strip()),
        )
        conn.commit()


def delete_user_preference(key: str, db_path: Optional[str] = None) -> bool:
    with get_connection(db_path) as conn:
        cur = conn.execute("DELETE FROM user_preferences WHERE key = ?;", (key.strip().lower(),))
        conn.commit()
        return cur.rowcount > 0
