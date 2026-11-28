"""Tests for subtrack.extract (redaction, truncation, parsing, retries – all mocked)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from subtrack.extract import (
    MAX_EMAIL_CHARS,
    prepare_email_for_llm,
    redact_sender,
    redact_text,
)
from subtrack.gmail_client import parse_message
from subtrack.models import Email


def make_email(**kw) -> Email:
    fields = dict(
        message_id="m1",
        thread_id="t1",
        sender="Streamco <billing@streamco.example>",
        subject="Your monthly receipt",
        received_at=datetime(2026, 3, 1, 9, 15, tzinfo=timezone.utc),
        list_unsubscribe=False,
        text="Charged $10.99 on 2026-03-01.",
    )
    fields.update(kw)
    return Email(**fields)


class TestRedaction:
    def test_masks_contiguous_8_plus_digits_to_last4(self):
        text = "Account 0123456789 paid card 4111111111114417 ref 12345678"
        out = redact_text(text)
        assert out == "Account ****6789 paid card ****4417 ref ****5678"

    def test_masks_formatted_cards_and_phones_to_last4(self):
        text = (
            "Card: 4111-1111-1111-4417 or 4111 2222 3333 9876. "
            "Phone: +234 803 123 4567 or 0803-555-0199 or (415) 555-2671."
        )
        out = redact_text(text)
        assert "****4417" in out
        assert "****9876" in out
        assert "****4567" in out
        assert "****0199" in out
        assert "****2671" in out
        for secret in ("4111", "2222", "3333", "803", "415"):
            assert secret not in out

    def test_preserves_dates_amounts_and_short_numbers(self):
        text = "Paid NGN 150,000.00 ($100.50) on 2026-03-01 (next: 01-04-2026) invoice #1234567"
        assert redact_text(text) == text

    def test_masks_email_addresses(self):
        text = "Sent to jane.doe+billing@example.com from billing@service.co.uk"
        out = redact_text(text)
        assert out == "Sent to [EMAIL] from [EMAIL]"
        assert "@" not in out

    def test_masks_street_and_labeled_addresses(self):
        text = (
            "Deliver to Plot 14, Admiralty Way, Lekki.\n"
            "Also 22 Herbert Macaulay Road and 450 Sunset Blvd, Apt 4B.\n"
            "Billing Address: 10 Bourdillon Close, Ikoyi, Lagos\n"
            "Amount: $20.00"
        )
        out = redact_text(text)
        assert "Admiralty Way" not in out
        assert "Herbert Macaulay" not in out
        assert "Sunset Blvd" not in out
        assert "Bourdillon Close" not in out
        assert "Billing Address: [ADDRESS]" in out
        assert "Amount: $20.00" in out

    def test_masks_user_supplied_terms_safely(self):
        text = "Hi Adewale Johnson, thanks Adewale! Upgrade your plan today."
        out = redact_text(text, extra_terms=["Adewale Johnson", "Adewale", "Ade"])
        assert "Adewale" not in out
        assert "Johnson" not in out
        # Single-word boundary ensures "Ade" does not mangle "Upgrade"
        assert "Upgrade your plan today." in out

    def test_redact_sender_removes_email_keeps_domain(self):
        out = redact_sender("Streamco <billing@account.streamco.example>")
        assert "@" not in out
        assert "Streamco" in out
        assert "account.streamco.example" in out

    def test_prepare_email_truncates_to_max_chars(self):
        email = make_email(text="Word " * 2000)  # 10,000 chars
        payload = prepare_email_for_llm(email, max_chars=MAX_EMAIL_CHARS)
        assert len(payload) <= MAX_EMAIL_CHARS
        assert payload.endswith("[truncated]")
        assert "Date: 2026-03-01" in payload

    def test_prepare_email_refuses_otp_and_password_reset(self, gmail_message):
        for name in ("otp_email", "password_reset"):
            with pytest.raises(ValueError, match="OTP or password-reset"):
                prepare_email_for_llm(parse_message(gmail_message(name)))
