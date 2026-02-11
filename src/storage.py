from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any


class Storage:
    """SQLite persistence for alerts."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def init_db(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def insert_alert(self, symbol: str, alert_type: str, payload: dict[str, Any]) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO alerts (symbol, alert_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (symbol, alert_type, json.dumps(payload, ensure_ascii=True), created_at),
            )
            self._conn.commit()

    def get_latest_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, symbol, alert_type, payload_json, created_at
                FROM alerts
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "id": row["id"],
                    "symbol": row["symbol"],
                    "alert_type": row["alert_type"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
            )
        return results

    def insert_event(self, symbol: str, event_type: str, data: dict[str, Any]) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO events (symbol, type, data_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (symbol, event_type, json.dumps(data, ensure_ascii=True), created_at),
            )
            self._conn.commit()

    def get_latest_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, symbol, type, data_json, created_at
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "id": row["id"],
                    "symbol": row["symbol"],
                    "type": row["type"],
                    "data": json.loads(row["data_json"]),
                    "created_at": row["created_at"],
                }
            )
        return results

    def get_latest_events_by_type(self, event_type: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, symbol, type, data_json, created_at
                FROM events
                WHERE type = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (event_type, limit),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "id": row["id"],
                    "symbol": row["symbol"],
                    "type": row["type"],
                    "data": json.loads(row["data_json"]),
                    "created_at": row["created_at"],
                }
            )
        return results

    def close(self) -> None:
        with self._lock:
            self._conn.close()
