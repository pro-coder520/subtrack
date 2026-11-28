"""Tests for BillingEvent and enums in subtrack.models."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from subtrack.models import BillingEvent, Channel, EventType, Period, Source


class TestBillingEventValid:
    def test_full_valid_event(self):
        ev = BillingEvent(
            merchant="Netflix",
            event_type="charge",
            amount="7,000.00",
            currency="ngn",
            event_date="2026-03-01",
            next_charge_date="2026-04-01",
            period="monthly",
            plan="Premium",
            card_last4="4417",
            channel="card",
            source="merchant",
            confidence=0.95,
            evidence="Total charged: NGN 7,000.00",
        )
        assert ev.merchant == "Netflix"
        assert ev.event_type is EventType.CHARGE
        assert ev.amount == Decimal("7000.00")
        assert isinstance(ev.amount, Decimal)
        assert ev.currency == "NGN"
        assert ev.event_date == date(2026, 3, 1)
        assert ev.next_charge_date == date(2026, 4, 1)
        assert ev.period is Period.MONTHLY
        assert ev.plan == "Premium"
        assert ev.card_last4 == "4417"
        assert ev.channel is Channel.CARD
        assert ev.source is Source.MERCHANT
        assert ev.confidence == 0.95
        assert ev.evidence == "Total charged: NGN 7,000.00"

    def test_minimal_event_defaults(self):
        ev = BillingEvent(merchant="Spotify", event_type="cancelled")
        assert ev.event_type is EventType.CANCELLED
        assert ev.amount is None
        assert ev.currency is None
        assert ev.event_date is None
        assert ev.next_charge_date is None
        assert ev.period is None
        assert ev.plan is None
        assert ev.card_last4 is None
        assert ev.channel is Channel.UNKNOWN
        assert ev.source is Source.OTHER
        assert ev.confidence is None
        assert ev.evidence is None

    def test_extra_fields_are_ignored(self):
        ev = BillingEvent(merchant="OpenAI", event_type="charge", unexpected="hello")
        assert ev.merchant == "OpenAI"


class TestStrictRequiredFields:
    @pytest.mark.parametrize("bad_type", ["invoice", "unknown", "", None, 123])
    def test_invalid_event_type_raises(self, bad_type):
        with pytest.raises(ValidationError):
            BillingEvent(merchant="Netflix", event_type=bad_type)

    @pytest.mark.parametrize("bad_merchant", ["", "   ", None, 123])
    def test_invalid_merchant_raises(self, bad_merchant):
        with pytest.raises(ValidationError):
            BillingEvent(merchant=bad_merchant, event_type="charge")


class TestLenientOptionalValidators:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (10.99, Decimal("10.99")),
            (20, Decimal("20")),
            ("4,400.50", Decimal("4400.50")),
            (Decimal("0.00"), Decimal("0.00")),
            ("free", None),
            ("$10", None),
            (-5, None),
            (float("nan"), None),
            (float("inf"), None),
            (True, None),
            ("", None),
        ],
    )
    def test_amount_is_always_decimal_or_none(self, raw, expected):
        ev = BillingEvent(merchant="X", event_type="charge", amount=raw)
        assert ev.amount == expected
        assert ev.amount is None or isinstance(ev.amount, Decimal)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("usd", "USD"),
            (" NGN ", "NGN"),
            ("$", None),
            ("Naira", None),
            ("US", None),
            ("123", None),
            (123, None),
            ("", None),
        ],
    )
    def test_currency(self, raw, expected):
        ev = BillingEvent(merchant="X", event_type="charge", currency=raw)
        assert ev.currency == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-03-01", date(2026, 3, 1)),
            ("2026-03-01T10:00:00Z", date(2026, 3, 1)),
            (datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc), date(2026, 3, 1)),
            ("March 1, 2026", None),
            ("2026-02-30", None),
            ("not-a-date", None),
            (True, None),
            (12345, None),
        ],
    )
    def test_dates(self, raw, expected):
        ev = BillingEvent(
            merchant="X",
            event_type="renewal_notice",
            event_date=raw,
            next_charge_date=raw,
        )
        assert ev.event_date == expected
        assert ev.next_charge_date == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("MONTHLY", Period.MONTHLY),
            ("semiannual", Period.SEMIANNUAL),
            ("biweekly", None),
            ("every 2 months", None),
            (30, None),
        ],
    )
    def test_period(self, raw, expected):
        ev = BillingEvent(merchant="X", event_type="charge", period=raw)
        assert ev.period is expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("4417", "4417"),
            ("****4417", "4417"),
            ("xxxx 0012", "0012"),
            ("12345", None),
            ("123", None),
            ("Visa", None),
            (True, None),
        ],
    )
    def test_card_last4(self, raw, expected):
        ev = BillingEvent(merchant="X", event_type="charge", card_last4=raw)
        assert ev.card_last4 == expected

    def test_bad_channel_and_source_fall_back(self):
        ev = BillingEvent(
            merchant="X",
            event_type="charge",
            channel="crypto",
            source="whisper",
        )
        assert ev.channel is Channel.UNKNOWN
        assert ev.source is Source.OTHER

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (0.0, 0.0),
            (1.0, 1.0),
            ("0.8", 0.8),
            (1.5, None),
            (-0.1, None),
            (float("nan"), None),
            ("high", None),
            (True, None),
        ],
    )
    def test_confidence(self, raw, expected):
        ev = BillingEvent(merchant="X", event_type="charge", confidence=raw)
        assert ev.confidence == expected

    def test_evidence_trimmed_and_capped(self):
        ev = BillingEvent(merchant="X", event_type="charge", evidence="  " + "a" * 500)
        assert ev.evidence == "a" * 300
        assert BillingEvent(merchant="X", event_type="charge", evidence="   ").evidence is None
