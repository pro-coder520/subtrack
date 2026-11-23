"""SQLite schema and queries.

Only metadata and extracted events are stored. Email bodies never are:
the `emails` table deliberately has no column for them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from subtrack import config
from subtrack.models import Email

SCHEMA_VERSION = 1

EMAIL_STATUSES = ("skipped", "extracted", "error")

# Statuses that count as "already processed": a normal sync skips these IDs.
# `error` is not here, so failed emails are retried on the next sync.
PROCESSED_STATUSES = ("skipped", "extracted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    message_id        TEXT PRIMARY KEY,
    sender            TEXT NOT NULL,
    subject           TEXT NOT NULL,
    received_at       TEXT NOT NULL,   -- ISO 8601, UTC
    prefilter_score   REAL,
    prefilter_reasons TEXT,            -- JSON list of strings
    status            TEXT NOT NULL CHECK (status IN ('skipped', 'extracted', 'error')),
    extractor_version TEXT,
    processed_at      TEXT NOT NULL    -- ISO 8601, UTC
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database (default ~/.subtrack/subtrack.db) and ensure the schema."""
    if path is None:
        path = config.DB_PATH
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def processed_ids(
    conn: sqlite3.Connection, recheck_skipped: bool = False
) -> set[str]:
    """IDs a sync should not fetch again.

    By default, both 'skipped' and 'extracted' count as processed (only 'error'
    is retried). When recheck_skipped=True, only 'extracted' IDs are skipped so
    previously skipped emails are fetched and scored again.
    """
    statuses = ("extracted",) if recheck_skipped else PROCESSED_STATUSES
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT message_id FROM emails WHERE status IN ({placeholders})",
        statuses,
    )
    return {r["message_id"] for r in rows}


def upsert_email(
    conn: sqlite3.Connection,
    email: Email,
    status: str,
    score: float | None = None,
    reasons: list[str] | None = None,
    extractor_version: str | None = None,
    processed_at: datetime | None = None,
) -> None:
    """Insert or update an email's metadata. The body (`email.text`) is never stored."""
    if status not in EMAIL_STATUSES:
        raise ValueError(f"Invalid status {status!r}; expected one of {EMAIL_STATUSES}")
    processed_at = processed_at or datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO emails (message_id, sender, subject, received_at,
                            prefilter_score, prefilter_reasons, status,
                            extractor_version, processed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            sender = excluded.sender,
            subject = excluded.subject,
            received_at = excluded.received_at,
            prefilter_score = excluded.prefilter_score,
            prefilter_reasons = excluded.prefilter_reasons,
            status = excluded.status,
            extractor_version = excluded.extractor_version,
            processed_at = excluded.processed_at
        """,
        (
            email.message_id,
            email.sender,
            email.subject,
            email.received_at.astimezone(timezone.utc).isoformat(),
            score,
            json.dumps(reasons) if reasons is not None else None,
            status,
            extractor_version,
            processed_at.astimezone(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
