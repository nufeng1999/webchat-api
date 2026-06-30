"""
wsession 共享映射表 — (model, wsession) → web 对话实例 ID

用于精准复用 web 上的对话实例，只在 main 退出时清理。
"""
import re
import threading
import time
import sqlite3
from typing import Optional

from config import BASE_DIR, CONFIG
from storage import DB_PATH

_wsession_map: dict[str, str] = {}  # "model:wsession" -> conversation_id
_lock = threading.RLock()

DEFAULT_WSESSION = "default"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_table():
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wsession_mappings (
                model TEXT NOT NULL,
                wsession TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (model, wsession)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _make_key(model: str, wsession: str) -> str:
    return f"{model}:{wsession}"


def extract_wsession(messages) -> Optional[str]:
    """从 messages 中提取第一条 [wsession: xxx] 格式的值。"""
    for m in messages:
        if getattr(m, 'role', None) == 'system':
            content = getattr(m, 'content', '') or ''
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        content = item.get("text", "")
                        break
                else:
                    continue
            match = re.search(r'\[wsession:\s*([^\]]+)\]', content)
            if match:
                return match.group(1).strip()
    return None


def resolve_wsession(messages) -> str:
    """提取 wsession，如果没有则返回 DEFAULT_WSESSION。"""
    return extract_wsession(messages) or DEFAULT_WSESSION


def get_conversation_id(model: str, wsession: str) -> Optional[str]:
    with _lock:
        return _wsession_map.get(_make_key(model, wsession))


def set_conversation_id(model: str, wsession: str, conversation_id: str):
    with _lock:
        _wsession_map[_make_key(model, wsession)] = conversation_id
    _persist_to_db(model, wsession, conversation_id)


def remove_by_conversation_id(model: str, conversation_id: str) -> Optional[str]:
    """通过 conversation_id 反查并清除映射，返回 wsession。"""
    prefix = f"{model}:"
    with _lock:
        for k, v in list(_wsession_map.items()):
            if v == conversation_id and k.startswith(prefix):
                wsession = k[len(prefix):]
                del _wsession_map[k]
                _delete_from_db(model, wsession)
                return wsession
        return None


def clear_all():
    with _lock:
        _wsession_map.clear()
    _clear_db()


def load_from_db():
    """启动时从 DB 加载映射到内存。"""
    _ensure_table()
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM wsession_mappings").fetchall()
        with _lock:
            _wsession_map.clear()
            for row in rows:
                key = _make_key(row["model"], row["wsession"])
                _wsession_map[key] = row["conversation_id"]
        import logging
        logging.getLogger("webchat-api").info(f"[wsession] loaded {len(rows)} mappings from DB")
    finally:
        conn.close()


def cleanup_old_mappings(days: int) -> list[str]:
    """清除 updated_at 超过 days 天的映射，返回被移除的 conversation_id 列表。"""
    threshold = time.time() - days * 86400
    _ensure_table()
    conn = _get_conn()
    removed_ids = []
    try:
        rows = conn.execute(
            "SELECT model, wsession, conversation_id FROM wsession_mappings WHERE updated_at < ?",
            (threshold,)
        ).fetchall()
        for row in rows:
            removed_ids.append(row["conversation_id"])
            key = _make_key(row["model"], row["wsession"])
            with _lock:
                _wsession_map.pop(key, None)
        if removed_ids:
            conn.execute("DELETE FROM wsession_mappings WHERE updated_at < ?", (threshold,))
            conn.commit()
            import logging
            logging.getLogger("webchat-api").info(f"[wsession] cleaned up {len(removed_ids)} mappings older than {days} days")
    finally:
        conn.close()
    return removed_ids


def remove_mapping_by_conv_id(conversation_id: str):
    """按 conversation_id 从内存和 DB 中移除映射（不限 model）。"""
    with _lock:
        keys_to_del = [k for k, v in _wsession_map.items() if v == conversation_id]
        for k in keys_to_del:
            del _wsession_map[k]
    if keys_to_del:
        _ensure_table()
        conn = _get_conn()
        try:
            conn.execute("DELETE FROM wsession_mappings WHERE conversation_id = ?", (conversation_id,))
            conn.commit()
        finally:
            conn.close()


def _persist_to_db(model: str, wsession: str, conversation_id: str):
    _ensure_table()
    conn = _get_conn()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO wsession_mappings (model, wsession, conversation_id, updated_at)
            VALUES (?, ?, ?, ?)
        """, (model, wsession, conversation_id, time.time()))
        conn.commit()
    finally:
        conn.close()


def _delete_from_db(model: str, wsession: str):
    _ensure_table()
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM wsession_mappings WHERE model = ? AND wsession = ?", (model, wsession))
        conn.commit()
    finally:
        conn.close()


def _clear_db():
    _ensure_table()
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM wsession_mappings")
        conn.commit()
    finally:
        conn.close()
