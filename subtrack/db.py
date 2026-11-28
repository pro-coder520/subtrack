"""SQLite schema and queries.

Only metadata and extracted events are stored. Email bodies never are:
the `emails` table deliberately has no column for them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from subtrack import config
from subtrack.models import BillingEvent, Email

SCHEMA_VERSION = 2

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

CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id        TEXT NOT NULL REFERENCES emails(message_id) ON DELETE CASCADE,
    merchant          TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    amount            TEXT,            -- Decimal stored as text, never float
    currency          TEXT,
    event_date        TEXT,            -- ISO YYYY-MM-DD
    next_charge_date  TEXT,            -- ISO YYYY-MM-DD
    period            TEXT,
    plan              TEXT,
    card_last4        TEXT,
    channel           TEXT NOT NULL,
    source            TEXT NOT NULL,
    confidence        REAL,
    evidence          TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_message_id ON events(message_id);
CREATE INDEX IF NOT EXISTS idx_events_merchant ON events(merchant);
"""

_UPSERT_EMAIL_SQL = """
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
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database (default ~/.subtrack/subtrack.db) and ensure the schema."""
    if path is None:
        path = config.DB_PATH
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def _email_params(
    email: Email,
    status: str,
    score: float | None,
    reasons: list[str] | None,
    extractor_version: str | None,
    processed_at: datetime | None,
) -> tuple:
    if status not in EMAIL_STATUSES:
        raise ValueError(f"Invalid status {status!r}; expected one of {EMAIL_STATUSES}")
    processed_at = processed_at or datetime.now(timezone.utc)
    return (
        email.message_id,
        email.sender,
        email.subject,
        email.received_at.astimezone(timezone.utc).isoformat(),
        score,
        json.dumps(reasons) if reasons is not None else None,
        status,
        extractor_version,
        processed_at.astimezone(timezone.utc).isoformat(),
    )


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
    params = _email_params(email, status, score, reasons, extractor_version, processed_at)
    with conn:
        conn.execute(_UPSERT_EMAIL_SQL, params)


def save_extraction(
    conn: sqlite3.Connection,
    email: Email,
    events: Sequence[BillingEvent],
    score: float | None = None,
    reasons: list[str] | None = None,
    extractor_version: str | None = None,
    processed_at: datetime | None = None,
) -> None:
    """Atomically store an extracted email row and replace its billing events."""
    params = _email_params(email, "extracted", score, reasons, extractor_version, processed_at)
    with conn:
        conn.execute(_UPSERT_EMAIL_SQL, params)
        conn.execute("DELETE FROM events WHERE message_id = ?", (email.message_id,))
        conn.executemany(
            """
            INSERT INTO events (
                message_id, merchant, event_type, amount, currency,
                event_date, next_charge_date, period, plan, card_last4,
                channel, source, confidence, evidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    email.message_id,
                    ev.merchant,
                    ev.event_type.value,
                    str(ev.amount) if ev.amount is not None else None,
                    ev.currency,
                    ev.event_date.isoformat() if ev.event_date is not None else None,
                    ev.next_charge_date.isoformat() if ev.next_charge_date is not None else None,
                    ev.period.value if ev.period is not None else None,
                    ev.plan,
                    ev.card_last4,
                    ev.channel.value,
                    ev.source.value,
                    ev.confidence,
                    ev.evidence,
                )
                for ev in events
            ],
        )


def list_events(
    conn: sqlite3.Connection, message_id: str | None = None
) -> list[tuple[str, BillingEvent]]:
    """Read stored billing events back as (message_id, BillingEvent) pairs."""
    if message_id is not None:
        rows = conn.execute(
            "SELECT * FROM events WHERE message_id = ? ORDER BY id", (message_id,)
        )
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY event_date DESC, id DESC")
    out: list[tuple[str, BillingEvent]] = []
    for r in rows:
        ev = BillingEvent(
            merchant=r["merchant"],
            event_type=r["event_type"],
            amount=r["amount"],
            currency=r["currency"],
            event_date=r["event_date"],
            next_charge_date=r["next_charge_date"],
            period=r["period"],
            plan=r["plan"],
            card_last4=r["card_last4"],
            channel=r["channel"],
            source=r["source"],
            confidence=r["confidence"],
            evidence=r["evidence"],
        )
        out.append((r["message_id"], ev))
    return out
