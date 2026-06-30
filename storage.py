import os
import sqlite3
import asyncio
import logging
import time
from uuid import uuid4

logger = logging.getLogger("webchat-api")

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "conversations.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_tables():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT,
                model TEXT,
                created_at REAL,
                updated_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                role TEXT,
                content TEXT,
                model TEXT,
                created_at REAL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv_id ON messages(conversation_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at)")
        conn.commit()
    finally:
        conn.close()


async def init_db():
    await asyncio.to_thread(_init_tables)
    logger.info("Database initialized at %s", DB_PATH)


async def save_conversation(conv_id: str, title: str, model: str) -> dict:
    now = time.time()

    def _save():
        conn = _get_conn()
        try:
            existing = conn.execute("SELECT id FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE conversations SET title = ?, model = ?, updated_at = ? WHERE id = ?",
                    (title, model, now, conv_id),
                )
            else:
                conn.execute(
                    "INSERT INTO conversations (id, title, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (conv_id, title, model, now, now),
                )
            conn.commit()
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    result = await asyncio.to_thread(_save)
    logger.info("Saved conversation %s", conv_id)
    return result


async def list_conversations() -> list:
    def _list():
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_list)


async def get_conversation(conv_id: str) -> dict | None:
    def _get():
        conn = _get_conn()
        try:
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def save_message(msg_id: str | None, conv_id: str, role: str, content: str, model: str | None = None) -> dict:
    if msg_id is None:
        msg_id = str(uuid4())
    now = time.time()

    def _save():
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content, model, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (msg_id, conv_id, role, content, model, now),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conv_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    result = await asyncio.to_thread(_save)
    logger.info("Saved message %s in conversation %s", msg_id, conv_id)
    return result


async def get_messages(conv_id: str) -> list:
    def _get():
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conv_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_get)


async def delete_conversation(conv_id: str) -> bool:
    def _delete():
        conn = _get_conn()
        try:
            cursor = conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    result = await asyncio.to_thread(_delete)
    if result:
        logger.info("Deleted conversation %s", conv_id)
    else:
        logger.warning("Conversation %s not found for deletion", conv_id)
    return result


async def clear_all_conversations():
    def _clear():
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM conversations")
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_clear)
    logger.info("Cleared all conversations from database")


async def search_conversations(query: str) -> list:
    def _search():
        conn = _get_conn()
        try:
            pattern = f"%{query}%"
            rows = conn.execute(
                """
                SELECT DISTINCT c.* FROM conversations c
                LEFT JOIN messages m ON c.id = m.conversation_id
                WHERE c.title LIKE ? OR m.content LIKE ?
                ORDER BY c.updated_at DESC
                """,
                (pattern, pattern),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_search)


async def list_conversations_older_than(days: int) -> list:
    """查询超过指定天数未更新的对话列表 [(id, model, updated_at), ...]。"""
    threshold = time.time() - days * 86400

    def _query():
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT id, model, updated_at FROM conversations WHERE updated_at < ?",
                (threshold,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    return await asyncio.to_thread(_query)
