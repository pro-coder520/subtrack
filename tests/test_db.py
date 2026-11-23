"""Tests for subtrack.db (in-memory / temp SQLite only)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from subtrack import config, db
from subtrack.models import Email

BODY = "SECRET BODY TEXT that must never reach the database"


def make_email(message_id: str = "m1", **kw) -> Email:
    fields = dict(
        message_id=message_id,
        thread_id="t1",
        sender="Example <billing@example.com>",
        subject="Your receipt",
        received_at=datetime(2026, 3, 1, 9, 15, tzinfo=timezone.utc),
        list_unsubscribe=False,
        text=BODY,
    )
    fields.update(kw)
    return Email(**fields)


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


class TestSchema:
    def test_emails_columns_are_exactly_the_allowed_ones(self, conn):
        # Privacy guard: CLAUDE.md allows only these. No body/text/html column.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(emails)")}
        assert cols == {
            "message_id", "sender", "subject", "received_at",
            "prefilter_score", "prefilter_reasons", "status",
            "extractor_version", "processed_at",
        }

    def test_schema_version(self, conn):
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION

    def test_connect_is_idempotent(self, tmp_path):
        path = tmp_path / "sub" / "t.db"
        db.connect(path).close()
        c = db.connect(path)  # second open must not fail or wipe data
        assert c.execute("SELECT count(*) FROM emails").fetchone()[0] == 0
        c.close()

    def test_default_path_comes_from_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DB_PATH", tmp_path / "x" / "subtrack.db")
        db.connect().close()
        assert (tmp_path / "x" / "subtrack.db").exists()

    def test_check_constraint_rejects_bad_status(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO emails (message_id, sender, subject, received_at, status, processed_at)"
                " VALUES ('x', '', '', '', 'pending', '')"
            )


class TestUpsert:
    def test_insert_and_read_back(self, conn):
        when = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)
        db.upsert_email(conn, make_email(), "skipped", score=-2.5,
                        reasons=["marketing:-3", "amount:+1"], processed_at=when)
        row = conn.execute("SELECT * FROM emails").fetchone()
        assert row["message_id"] == "m1"
        assert row["sender"] == "Example <billing@example.com>"
        assert row["received_at"] == "2026-03-01T09:15:00+00:00"
        assert row["prefilter_score"] == -2.5
        assert json.loads(row["prefilter_reasons"]) == ["marketing:-3", "amount:+1"]
        assert row["status"] == "skipped"
        assert row["extractor_version"] is None
        assert row["processed_at"] == "2026-03-02T10:00:00+00:00"

    def test_body_text_is_never_stored(self, conn):
        db.upsert_email(conn, make_email(), "extracted", extractor_version="v1")
        for row in conn.execute("SELECT * FROM emails"):
            assert all(BODY not in str(v) for v in tuple(row))

    def test_upsert_updates_existing_row(self, conn):
        db.upsert_email(conn, make_email(), "error")
        db.upsert_email(conn, make_email(), "extracted", extractor_version="v2")
        rows = conn.execute("SELECT status, extractor_version FROM emails").fetchall()
        assert [tuple(r) for r in rows] == [("extracted", "v2")]

    def test_invalid_status_raises(self, conn):
        with pytest.raises(ValueError):
            db.upsert_email(conn, make_email(), "pending")


class TestProcessedIds:
    def test_empty(self, conn):
        assert db.processed_ids(conn) == set()

    def test_skipped_and_extracted_count_error_does_not(self, conn):
        db.upsert_email(conn, make_email("s"), "skipped")
        db.upsert_email(conn, make_email("x"), "extracted")
        db.upsert_email(conn, make_email("e"), "error")
        assert db.processed_ids(conn) == {"s", "x"}

    def test_recheck_skipped_only_excludes_extracted(self, conn):
        db.upsert_email(conn, make_email("s"), "skipped")
        db.upsert_email(conn, make_email("x"), "extracted")
        db.upsert_email(conn, make_email("e"), "error")
        assert db.processed_ids(conn, recheck_skipped=True) == {"x"}
