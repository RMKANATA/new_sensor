"""センサデータ 送信キューおよび送信状態管理モジュール (SQLite)

【サーバー仕様と重複送信防止の重要性】
- サーバー側はタイムスタンプのチェックおよび重複排除を行っていません。
- したがって、クライアント側で「同一タイムスタンプのデータを絶対に2回送信しない」ことを
  厳格に保証する必要があります。

【本モジュールの役割】
1. post_state.json による送信済みタイムスタンプの永続化
2. pending.db (SQLite) による送信待ちデータのトランザクション付きキューイング
3. UNIQUE 制約によるキュー内重複登録の完全防止
"""

import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from common import atomic_write_json

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATE_FILE = BASE_DIR / "post_state.json"
DB_PATH = DATA_DIR / "pending.db"

logger = logging.getLogger("new_sensor.send_queue")


def get_db_connection() -> sqlite3.Connection:
    """SQLite 接続を取得し、テーブルが存在しない場合は初期化します。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode = WAL;")  # 突発電源断に強く並行読み出しに強い
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT UNIQUE NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            last_error TEXT
        );
    """)
    conn.commit()
    return conn


def load_post_state() -> Dict[str, Any]:
    """ディスク上の post_state.json から送信済み状態を読み込みます。"""
    if not STATE_FILE.exists():
        return {"last_posted_timestamp": None, "last_posted_at": None}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"Failed to read {STATE_FILE}: {e}")

    return {"last_posted_timestamp": None, "last_posted_at": None}


def save_post_state(last_posted_timestamp: str) -> None:
    """送信に成功したタイムスタンプを post_state.json にアトミック保存します。"""
    data = {
        "last_posted_timestamp": last_posted_timestamp,
        "last_posted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        atomic_write_json(STATE_FILE, data)
    except Exception as e:
        logger.error(f"Failed to save {STATE_FILE}: {e}")


class SendQueue:
    """送信待ちキュー（SQLite）および重複送信防止コントローラ"""

    def __init__(self, max_rows: int = 10080):
        self.max_rows = max_rows
        # 起動時に送信済みタイムスタンプを復元
        state = load_post_state()
        self._last_posted_timestamp: Optional[str] = state.get("last_posted_timestamp")
        logger.info(f"SendQueue initialized. Restored last_posted_timestamp: '{self._last_posted_timestamp}'")

    @property
    def last_posted_timestamp(self) -> Optional[str]:
        return self._last_posted_timestamp

    def is_already_posted(self, timestamp: str) -> bool:
        """指定されたタイムスタンプが既に送信成功済みか判定します。"""
        if not timestamp:
            return True
        if self._last_posted_timestamp is not None and timestamp <= self._last_posted_timestamp:
            return True
        return False

    def mark_as_posted(self, timestamp: str) -> None:
        """送信成功時にタイムスタンプを記録し、永続化します。"""
        if not timestamp:
            return
        if self._last_posted_timestamp is None or timestamp > self._last_posted_timestamp:
            self._last_posted_timestamp = timestamp
            save_post_state(timestamp)

        # キューにあれば削除
        self.remove(timestamp)

    def enqueue(self, timestamp: str, payload: dict) -> bool:
        """未送信ペイロードをキューに登録します。
        既に送信済みタイムスタンプである場合や、既にキューに存在する場合は登録しません。
        """
        if self.is_already_posted(timestamp):
            logger.info(f"Skipping enqueue: timestamp '{timestamp}' is already posted.")
            return False

        payload_json = json.dumps(payload, ensure_ascii=False)
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")

        try:
            with get_db_connection() as conn:
                # UNIQUE 制約により重複登録は無視される
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR IGNORE INTO pending_queue (timestamp, payload, created_at, attempts)
                    VALUES (?, ?, ?, 0);
                """, (timestamp, payload_json, now_str))
                inserted = cursor.rowcount > 0

                # 上限行数管理（古すぎる未送信データを古い順に破棄）
                cursor.execute("SELECT count(*) FROM pending_queue;")
                count = cursor.fetchone()[0]
                if count > self.max_rows:
                    overflow = count - self.max_rows
                    cursor.execute("""
                        DELETE FROM pending_queue
                        WHERE id IN (SELECT id FROM pending_queue ORDER BY id ASC LIMIT ?);
                    """, (overflow,))
                    logger.warning(f"Queue exceeded max {self.max_rows} rows. Dropped oldest {overflow} entries.")

                conn.commit()
                if inserted:
                    logger.info(f"[Queue] Enqueued timestamp '{timestamp}' (Queue size: {count})")
                return inserted

        except Exception as e:
            logger.error(f"[Queue] Failed to enqueue: {e}")
            return False

    def fetch_pending(self, limit: int = 30) -> List[Tuple[int, str, dict]]:
        """送信待ちのデータを古い順（タイムスタンプ昇順）に取得します。
        :return: [(id, timestamp, payload_dict), ...]
        """
        results = []
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, timestamp, payload
                    FROM pending_queue
                    ORDER BY id ASC
                    LIMIT ?;
                """, (limit,))
                for row_id, ts, payload_str in cursor.fetchall():
                    try:
                        payload = json.loads(payload_str)
                        results.append((row_id, ts, payload))
                    except json.JSONDecodeError:
                        logger.error(f"[Queue] Corrupted JSON in queue (id={row_id}). Deleting.")
                        conn.execute("DELETE FROM pending_queue WHERE id = ?;", (row_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"[Queue] Failed to fetch pending items: {e}")

        return results

    def remove(self, timestamp: str) -> bool:
        """キューから指定タイムスタンプのレコードを削除します。"""
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM pending_queue WHERE timestamp = ?;", (timestamp,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"[Queue] Failed to remove item '{timestamp}': {e}")
            return False

    def record_attempt_failure(self, timestamp: str, error_msg: str) -> None:
        """再送失敗時の試行回数とエラーメッセージを記録します。"""
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE pending_queue
                    SET attempts = attempts + 1, last_error = ?
                    WHERE timestamp = ?;
                """, (str(error_msg)[:200], timestamp))
                conn.commit()
        except Exception as e:
            logger.error(f"[Queue] Failed to record attempt failure: {e}")

    def get_pending_count(self) -> int:
        """現在のキュー残件数を返します。"""
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT count(*) FROM pending_queue;")
                return cursor.fetchone()[0]
        except Exception:
            return 0
