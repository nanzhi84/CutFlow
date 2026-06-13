"""Content-addressed SQLite archive for OceanEngine offline imports.

Two responsibilities:

* **File-level dedupe** — a file is identified by its sha256. Re-importing the
  same bytes (even from a different path) is a full skip.
* **Row-level dedupe** — each normalized row carries a content fingerprint;
  the archive records which fingerprints have already produced an observation so
  partial re-imports (a file that grew by a few rows) only emit the new rows.

The archive is a plain local ``.sqlite3`` file living inside the connector tree;
it never reaches a paid provider and needs no secrets.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = "oceanengine_offline_v1"

_SCHEMA_SQL = """
create table if not exists imported_files (
  file_sha256 text primary key,
  source_page text not null,
  first_archived_path text not null,
  row_count integer not null,
  imported_at text not null default current_timestamp
);

create table if not exists imported_rows (
  row_fingerprint text primary key,
  file_sha256 text not null,
  source_page text not null,
  imported_at text not null default current_timestamp
);

create index if not exists idx_imported_rows_file on imported_rows(file_sha256);
"""


class ImportArchive:
    """Tracks already-imported files and rows to make re-imports idempotent."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    def file_already_imported(self, file_sha256: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select 1 from imported_files where file_sha256 = ? limit 1",
                (file_sha256,),
            ).fetchone()
            return row is not None

    def seen_row_fingerprints(self, fingerprints: list[str]) -> set[str]:
        """Return the subset of ``fingerprints`` already recorded."""

        if not fingerprints:
            return set()
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in fingerprints)
            rows = conn.execute(
                f"select row_fingerprint from imported_rows where row_fingerprint in ({placeholders})",
                fingerprints,
            ).fetchall()
            return {row["row_fingerprint"] for row in rows}

    def record_import(
        self,
        *,
        file_sha256: str,
        source_page: str,
        archived_path: str,
        row_count: int,
        row_fingerprints: list[str],
    ) -> None:
        """Persist a completed import (file + its new row fingerprints)."""

        with self._connect() as conn:
            conn.execute(
                """
                insert into imported_files (file_sha256, source_page, first_archived_path, row_count)
                values (?, ?, ?, ?)
                on conflict(file_sha256) do update set row_count = excluded.row_count
                """,
                (file_sha256, source_page, archived_path, row_count),
            )
            conn.executemany(
                """
                insert or ignore into imported_rows (row_fingerprint, file_sha256, source_page)
                values (?, ?, ?)
                """,
                [(fp, file_sha256, source_page) for fp in row_fingerprints],
            )
