"""Tests for subtrack.db (in-memory / temp SQLite only)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from subtrack import config, db
from subtrack.models import BillingEvent, Channel, Email, EventType, Period, Source

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


class TestEventsTable:
    def test_events_schema_and_amount_stored_as_text(self, conn):
        cols = {r["name"]: r["type"] for r in conn.execute("PRAGMA table_info(events)")}
        assert cols["amount"] == "TEXT"
        assert set(cols) == {
            "id", "message_id", "merchant", "event_type", "amount", "currency",
            "event_date", "next_charge_date", "period", "plan", "card_last4",
            "channel", "source", "confidence", "evidence",
        }

    def test_save_and_list_events_round_trips_decimal(self, conn):
        ev1 = BillingEvent(
            merchant="Netflix",
            event_type=EventType.CHARGE,
            amount=Decimal("7000.50"),
            currency="NGN",
            event_date=date(2026, 3, 1),
            next_charge_date=date(2026, 4, 1),
            period=Period.MONTHLY,
            plan="Premium",
            card_last4="4417",
            channel=Channel.CARD,
            source=Source.MERCHANT,
            confidence=0.95,
            evidence="Total NGN 7,000.50",
        )
        ev2 = BillingEvent(
            merchant="Netflix",
            event_type=EventType.PRICE_CHANGE,
            amount=Decimal("8500.00"),
            currency="NGN",
        )
        db.save_extraction(
            conn,
            make_email("m1"),
            [ev1, ev2],
            score=5.5,
            reasons=["amount:NGN 7,000.50(+2)"],
            extractor_version="gemini-3.5-flash-lite:v1",
        )

        # Verify SQLite stores amount as exact text, never float
        raw_types = [
            (r["amount"], r["t"])
            for r in conn.execute("SELECT amount, typeof(amount) AS t FROM events ORDER BY id")
        ]
        assert raw_types == [("7000.50", "text"), ("8500.00", "text")]

        loaded = db.list_events(conn, message_id="m1")
        assert len(loaded) == 2
        assert loaded[0] == ("m1", ev1)
        assert loaded[1] == ("m1", ev2)
        assert isinstance(loaded[0][1].amount, Decimal)

        # Email row is marked extracted with version and still holds no body
        email_row = conn.execute("SELECT * FROM emails WHERE message_id = 'm1'").fetchone()
        assert email_row["status"] == "extracted"
        assert email_row["extractor_version"] == "gemini-3.5-flash-lite:v1"
        assert all(BODY not in str(v) for v in tuple(email_row))

    def test_re_saving_email_replaces_old_events_without_duplicates(self, conn):
        email = make_email("m1")
        db.save_extraction(
            conn,
            email,
            [BillingEvent(merchant="Old", event_type=EventType.CHARGE, amount=Decimal("10"))],
            extractor_version="m:v1",
        )
        db.save_extraction(
            conn,
            email,
            [BillingEvent(merchant="New", event_type=EventType.CHARGE, amount=Decimal("20"))],
            extractor_version="m:v2",
        )
        loaded = db.list_events(conn, message_id="m1")
        assert len(loaded) == 1
        assert loaded[0][1].merchant == "New"
        assert loaded[0][1].amount == Decimal("20")

    def test_saving_zero_events_marks_email_extracted(self, conn):
        db.save_extraction(conn, make_email("m_empty"), [], extractor_version="m:v1")
        assert db.processed_ids(conn) == {"m_empty"}
        assert db.list_events(conn, message_id="m_empty") == []
