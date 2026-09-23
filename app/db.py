"""SQLite persistence for analysis requests and results."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS analyses (
    fingerprint      TEXT PRIMARY KEY,
    operation_name   TEXT,
    operation_type   TEXT,
    status           TEXT NOT NULL,
    total_cost       TEXT,
    budget           INTEGER,
    within_budget    INTEGER,
    request_json     TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL,
    hit_count        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_analyses_created_at ON analyses(created_at);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AnalysisStore:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.executescript(_SCHEMA_SQL)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #

    def save(
        self,
        fingerprint: str,
        request: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        now = _utcnow()
        operation = result.get("operation") or {}
        summary = result.get("summary") or {}
        total_cost = summary.get("total_cost")
        within_budget = summary.get("within_budget")
        row = {
            "fingerprint": fingerprint,
            "operation_name": operation.get("name"),
            "operation_type": operation.get("type"),
            "status": result.get("status", "error"),
            # Stored as TEXT: worst-case costs can exceed 64-bit integers.
            "total_cost": str(total_cost) if total_cost is not None else None,
            "budget": summary.get("budget", request.get("budget")),
            "within_budget": (
                None if within_budget is None else int(bool(within_budget))
            ),
            "request_json": json.dumps(request, ensure_ascii=False),
            "result_json": json.dumps(result, ensure_ascii=False),
            "now": now,
        }
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO analyses (
                    fingerprint, operation_name, operation_type, status,
                    total_cost, budget, within_budget,
                    request_json, result_json,
                    created_at, last_accessed_at, hit_count
                )
                VALUES (
                    :fingerprint, :operation_name, :operation_type, :status,
                    :total_cost, :budget, :within_budget,
                    :request_json, :result_json,
                    :now, :now, 0
                )
                ON CONFLICT(fingerprint) DO UPDATE SET
                    operation_name   = excluded.operation_name,
                    operation_type   = excluded.operation_type,
                    status           = excluded.status,
                    total_cost       = excluded.total_cost,
                    budget           = excluded.budget,
                    within_budget    = excluded.within_budget,
                    request_json     = excluded.request_json,
                    result_json      = excluded.result_json,
                    last_accessed_at = excluded.last_accessed_at
                """,
                row,
            )

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM analyses WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        if row is None:
            return None
        return {
            "summary": self._summary(row),
            "request": json.loads(row["request_json"]),
            "result": json.loads(row["result_json"]),
        }

    def list(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where_sql = ""
        params: list[Any] = []
        if status:
            where_sql = "WHERE status = ?"
            params.append(status)
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM analyses {where_sql}", params
            ).fetchone()["c"]
            rows = self._conn.execute(
                f"""
                SELECT * FROM analyses {where_sql}
                ORDER BY created_at DESC, fingerprint ASC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [self._summary(row) for row in rows], total

    def record_hit(self, fingerprint: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE analyses
                   SET hit_count = hit_count + 1,
                       last_accessed_at = ?
                 WHERE fingerprint = ?
                """,
                (_utcnow(), fingerprint),
            )

    def delete(self, fingerprint: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM analyses WHERE fingerprint = ?", (fingerprint,)
            )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------ #

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "fingerprint": row["fingerprint"],
            "operation_name": row["operation_name"],
            "operation_type": row["operation_type"],
            "status": row["status"],
            "total_cost": (
                int(row["total_cost"]) if row["total_cost"] is not None else None
            ),
            "budget": row["budget"],
            "within_budget": (
                None if row["within_budget"] is None else bool(row["within_budget"])
            ),
            "created_at": row["created_at"],
            "last_accessed_at": row["last_accessed_at"],
            "hit_count": row["hit_count"],
        }
