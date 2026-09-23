"""SQLite 持久化：按请求指纹缓存分析结果。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_cache (
    fingerprint    TEXT PRIMARY KEY,
    status         TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    result_json    TEXT NOT NULL,
    operation_name TEXT,
    total_cost     REAL,
    budget         REAL,
    over_budget    INTEGER NOT NULL DEFAULT 0,
    hit_count      INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_cache_created ON analysis_cache(created_at);
"""


class Cache:
    """进程内唯一的 SQLite 缓存（check_same_thread + 互斥锁）。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.db_path
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- 内部 helpers --------------------------------------------------
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = json.loads(row["result_json"])
        return {
            "fingerprint": row["fingerprint"],
            "status": row["status"],
            "result": data,
            "operation_name": row["operation_name"],
            "total_cost": row["total_cost"],
            "budget": row["budget"],
            "over_budget": bool(row["over_budget"]),
            "hit_count": row["hit_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ---- 公共 API ------------------------------------------------------
    def get(self, fingerprint: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM analysis_cache WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE analysis_cache SET hit_count = hit_count + 1, "
                "updated_at = ? WHERE fingerprint = ?",
                (time.time(), fingerprint),
            )
            self._conn.commit()
            entry = self._row_to_dict(row)
            entry["hit_count"] += 1
            return entry

    def put(
        self,
        fingerprint: str,
        status: str,
        result: dict[str, Any],
        *,
        operation_name: str | None = None,
        total_cost: float | None = None,
        budget: float | None = None,
        over_budget: bool = False,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO analysis_cache (
                    fingerprint, status, result_json, operation_name,
                    total_cost, budget, over_budget, hit_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    status = excluded.status,
                    result_json = excluded.result_json,
                    operation_name = excluded.operation_name,
                    total_cost = excluded.total_cost,
                    budget = excluded.budget,
                    over_budget = excluded.over_budget,
                    updated_at = excluded.updated_at
                """,
                (
                    fingerprint,
                    status,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    operation_name,
                    total_cost,
                    budget,
                    1 if over_budget else 0,
                    now,
                    now,
                ),
            )
            self._conn.commit()

    def list_entries(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM analysis_cache ORDER BY created_at DESC "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def delete(self, fingerprint: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM analysis_cache WHERE fingerprint = ?",
                (fingerprint,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM analysis_cache"
            ).fetchone()[0]
            hits = self._conn.execute(
                "SELECT COALESCE(SUM(hit_count), 0) FROM analysis_cache"
            ).fetchone()[0]
            over = self._conn.execute(
                "SELECT COUNT(*) FROM analysis_cache WHERE over_budget = 1"
            ).fetchone()[0]
        return {
            "db_path": self.db_path,
            "entries": total,
            "cache_hits": hits,
            "over_budget_entries": over,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


cache = Cache()
