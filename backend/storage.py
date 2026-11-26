"""报警记录持久化（SQLite）。"""

from __future__ import annotations

import os
import sqlite3
import threading

_COLUMNS = ("id", "ts", "source", "kind", "level", "message", "value", "snapshot")


class AlarmStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alarms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                source TEXT,
                kind TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT,
                value REAL,
                snapshot TEXT
            )
            """
        )
        self._conn.commit()

    def add(self, ts, source, kind, level, message, value, snapshot) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alarms (ts, source, kind, level, message, value, snapshot) "
                "VALUES (?,?,?,?,?,?,?)",
                (ts, source, kind, level, message, float(value or 0), snapshot),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list(self, limit: int = 100, level: str | None = None) -> list[dict]:
        sql = f"SELECT {', '.join(_COLUMNS)} FROM alarms"
        args: list = []
        if level:
            sql += " WHERE level=?"
            args.append(level)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(zip(_COLUMNS, row)) for row in rows]

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM alarms")
            self._conn.commit()
