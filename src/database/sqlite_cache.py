"""Lightweight SQLite cache for AkShare responses.

The cache keeps AkShare column names intact by storing each dataset as a
dedicated table. Tables are created lazily with the column names detected
from the first inserted record, and new columns are appended automatically
if AkShare adds more fields in the future.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd


ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _utcnow() -> str:
    return datetime.utcnow().strftime(ISO_FORMAT)


def _infer_sql_type(value: Any) -> str:
    if isinstance(value, (int, np.integer)):
        return "INTEGER"
    if isinstance(value, (float, np.floating)):
        return "REAL"
    return "TEXT"


def _normalize(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (np.generic,)):
        return value.item()
    if pd.isna(value):  # type: ignore[arg-type]
        return None
    return value


def _quote_identifier(name: str) -> str:
    return f'"{name}"'


class AkshareSQLiteCache:
    """Simple SQLite-backed cache with insert-or-update semantics."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(database_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")

    # ------------------------------------------------------------------
    # Schema helpers
    # ------------------------------------------------------------------
    def _table_columns(self, table: str) -> Dict[str, str]:
        cursor = self._conn.execute(f'PRAGMA table_info("{table}");')
        return {row[1]: row[2] for row in cursor.fetchall()}

    def _ensure_table(
        self,
        table: str,
        sample_record: Dict[str, Any],
        key_columns: Sequence[str],
    ) -> None:
        columns = self._table_columns(table)
        if not columns:
            col_defs: List[str] = []
            for column, value in sample_record.items():
                sql_type = _infer_sql_type(value)
                col_defs.append(f'"{column}" {sql_type}')
            pk_clause = (
                f" ,PRIMARY KEY ({', '.join(_quote_identifier(col) for col in key_columns)})"
                if key_columns
                else ""
            )
            create_sql = (
                f'CREATE TABLE IF NOT EXISTS "{table}" ('
                + ", ".join(col_defs)
                + pk_clause
                + ");"
            )
            self._conn.execute(create_sql)
            self._conn.commit()
            return

        # Add missing columns on-the-fly.
        for column, value in sample_record.items():
            if column not in columns:
                sql_type = _infer_sql_type(value)
                self._conn.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type};'
                )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def upsert_records(
        self,
        table: str,
        records: Iterable[Dict[str, Any]],
        key_columns: Sequence[str],
    ) -> None:
        payload: List[Dict[str, Any]] = []
        for record in records:
            normalized = {
                column: _normalize(value) for column, value in record.items()
            }
            if "缓存时间" not in normalized:
                normalized["缓存时间"] = _utcnow()
            payload.append(normalized)

        if not payload:
            return

        sample = payload[0]
        self._ensure_table(table, sample, key_columns)

        columns = list(sample.keys())
        quoted_columns = ", ".join(_quote_identifier(col) for col in columns)
        placeholders = ", ".join(["?"] * len(columns))
        conflict_clause = (
            f"({', '.join(_quote_identifier(col) for col in key_columns)})"
            if key_columns
            else ""
        )
        update_clause = ", ".join(
            f"{_quote_identifier(col)}=excluded.{_quote_identifier(col)}"
            for col in columns
            if col not in key_columns
        )

        sql = f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})'
        if key_columns:
            sql += f" ON CONFLICT {conflict_clause} DO UPDATE SET {update_clause}"

        rows = [[record[col] for col in columns] for record in payload]
        self._conn.executemany(sql, rows)
        self._conn.commit()

    def fetch_records(
        self,
        table: str,
        filters: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if not self._table_columns(table):
            return []

        clauses: List[str] = []
        params: List[Any] = []
        if filters:
            for column, value in filters.items():
                clauses.append(f'"{column}" = ?')
                params.append(value)
        if ttl_seconds is not None:
            threshold = (datetime.utcnow() - timedelta(seconds=ttl_seconds)).strftime(
                ISO_FORMAT
            )
            clauses.append('"缓存时间" >= ?')
            params.append(threshold)

        sql = f'SELECT * FROM "{table}"'
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {limit}"

        cursor = self._conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def delete_records(
        self,
        table: str,
        filters: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._table_columns(table):
            return
        clauses: List[str] = []
        params: List[Any] = []
        if filters:
            for column, value in filters.items():
                clauses.append(f'"{column}" = ?')
                params.append(value)
        sql = f'DELETE FROM "{table}"'
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        self._conn.execute(sql, params)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
