"""Tests for subtrack.extract (redaction, truncation, parsing, retries – all mocked)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors
from google.genai import types

from subtrack.extract import (
    MAX_EMAIL_CHARS,
    PROMPT_VERSION,
    RESPONSE_SCHEMA,
    ExtractionError,
    QuotaExhaustedError,
    RateLimiter,
    extract_events,
    extractor_version,
    is_daily_quota_error,
    parse_extraction_response,
    prepare_email_for_llm,
    redact_sender,
    redact_text,
)
from subtrack.gmail_client import parse_message
from subtrack.models import Channel, Email, EventType, Period, Source


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


def _fake_response(text: str | None, finish_reason=types.FinishReason.STOP):
    candidate = SimpleNamespace(finish_reason=finish_reason)
    return SimpleNamespace(text=text, candidates=[candidate])


def _api_error(code: int, message: str, status: str = "RESOURCE_EXHAUSTED", extra_details=None):
    err_obj: dict = {"code": code, "message": message, "status": status}
    if extra_details is not None:
        err_obj["details"] = extra_details
    return genai_errors.APIError(code, {"error": err_obj})


class _FakeGeminiClient:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestParseExtractionResponse:
    def test_zero_events_is_valid(self):
        resp = _fake_response(json.dumps({"events": []}))
        assert parse_extraction_response(resp) == []

    def test_multiple_events_and_lenient_optional_fields(self):
        payload = {
            "events": [
                {
                    "merchant": "Netflix",
                    "event_type": "charge",
                    "amount": "7000.00",
                    "currency": "NGN",
                    "event_date": "2026-03-01",
                    "next_charge_date": "2026-04-01",
                    "period": "monthly",
                    "card_last4": "4417",
                    "channel": "card",
                    "source": "merchant",
                    "confidence": 0.95,
                    "evidence": "Charged NGN 7,000.00",
                },
                {
                    "merchant": "Netflix",
                    "event_type": "price_change",
                    "amount": "8500",
                    "currency": "NGN",
                    "event_date": "not-a-date",  # bad optional -> None
                    "period": "every-other-tuesday",  # bad optional -> None
                    "channel": "unknown",
                    "source": "merchant",
                },
                {
                    "merchant": "Bogus",
                    "event_type": "marketing_blast",  # invalid -> dropped
                },
            ]
        }
        events = parse_extraction_response(_fake_response(json.dumps(payload)))
        assert len(events) == 2
        assert events[0].event_type is EventType.CHARGE
        assert events[0].amount == Decimal("7000.00")
        assert events[0].period is Period.MONTHLY
        assert events[0].channel is Channel.CARD
        assert events[0].source is Source.MERCHANT
        assert events[0].event_date == date(2026, 3, 1)
        assert events[1].event_type is EventType.PRICE_CHANGE
        assert events[1].amount == Decimal("8500")
        assert events[1].event_date is None
        assert events[1].period is None

    @pytest.mark.parametrize(
        "resp",
        [
            _fake_response(""),
            _fake_response("   "),
            _fake_response(None),
            _fake_response('{"events": [{"merchant": "Net'),  # cut-off JSON
            _fake_response('{"events": []}', finish_reason=types.FinishReason.MAX_TOKENS),
            _fake_response('{"events": []}', finish_reason=types.FinishReason.SAFETY),
            _fake_response("[]"),
            _fake_response('{"other": []}'),
        ],
    )
    def test_empty_truncated_or_malformed_raises_extraction_error(self, resp):
        with pytest.raises(ExtractionError):
            parse_extraction_response(resp)


class TestRateLimiter:
    def test_spaces_consecutive_calls(self):
        now = [100.0]
        sleeps: list[float] = []

        def fake_sleep(s: float) -> None:
            sleeps.append(s)
            now[0] += s

        limiter = RateLimiter(rpm=10.0, sleep=fake_sleep, monotonic=lambda: now[0])
        limiter.wait()  # first call: no sleep
        assert sleeps == []
        now[0] += 2.0
        limiter.wait()  # 2s elapsed out of 6s -> sleeps 4s
        assert sleeps == [pytest.approx(4.0)]
        now[0] += 10.0
        limiter.wait()  # >6s elapsed -> no sleep
        assert len(sleeps) == 1


class TestExtractEvents:
    def test_sends_redacted_text_and_json_schema(self):
        email = make_email(
            text="Hi Jane Doe, card 4111-1111-1111-4417 charged $10.99. Email: jane@example.com"
        )
        ok_json = json.dumps({
            "events": [{
                "merchant": "Streamco",
                "event_type": "charge",
                "amount": "10.99",
                "currency": "USD",
                "card_last4": "4417",
            }]
        })
        client = _FakeGeminiClient([_fake_response(ok_json)])

        events = extract_events(
            email,
            client,
            model="gemini-3.5-flash-lite",
            extra_redact_terms=["Jane Doe"],
            sleep=lambda _: None,
        )
        assert len(events) == 1
        assert events[0].amount == Decimal("10.99")
        assert extractor_version("gemini-3.5-flash-lite") == f"gemini-3.5-flash-lite:{PROMPT_VERSION}"

        call = client.calls[0]
        assert call["model"] == "gemini-3.5-flash-lite"
        assert "Jane Doe" not in call["contents"]
        assert "4111" not in call["contents"]
        assert "jane@example.com" not in call["contents"]
        assert "****4417" in call["contents"]
        assert call["config"].response_mime_type == "application/json"
        assert call["config"].response_schema == RESPONSE_SCHEMA

    def test_transient_429_backs_off_and_retries(self):
        err_429 = _api_error(
            429,
            "Rate limit exceeded, try again shortly.",
            extra_details=[{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}],
        )
        client = _FakeGeminiClient([err_429, _fake_response('{"events": []}')])
        sleeps: list[float] = []

        events = extract_events(
            make_email(), client, model="gemini-3.5-flash-lite", sleep=sleeps.append
        )
        assert events == []
        assert sleeps == [7.0]
        assert len(client.calls) == 2

    def test_daily_quota_429_stops_immediately(self):
        err_daily = _api_error(
            429,
            "Quota exceeded for quota metric 'GenerateRequestsPerDayPerProjectPerModel'.",
        )
        assert is_daily_quota_error(err_daily) is True
        client = _FakeGeminiClient([err_daily])
        sleeps: list[float] = []

        with pytest.raises(QuotaExhaustedError):
            extract_events(
                make_email(), client, model="gemini-3.5-flash-lite", sleep=sleeps.append
            )
        assert sleeps == []
        assert len(client.calls) == 1

    def test_persistent_429_raises_quota_exhausted_after_retries(self):
        errs = [_api_error(429, "Too many requests") for _ in range(4)]
        client = _FakeGeminiClient(errs)
        sleeps: list[float] = []

        with pytest.raises(QuotaExhaustedError):
            extract_events(
                make_email(),
                client,
                model="gemini-3.5-flash-lite",
                sleep=sleeps.append,
                max_429_retries=2,
                base_429_backoff=3.0,
            )
        assert sleeps == [3.0, 6.0]
        assert len(client.calls) == 3

    def test_503_retries_then_succeeds(self):
        err_503 = _api_error(503, "Service unavailable", status="UNAVAILABLE")
        client = _FakeGeminiClient([err_503, _fake_response('{"events": []}')])
        sleeps: list[float] = []

        events = extract_events(
            make_email(), client, model="gemini-3.5-flash-lite", sleep=sleeps.append
        )
        assert events == []
        assert sleeps == [2.0]

    def test_non_retryable_api_error_raises_extraction_error(self):
        err_400 = _api_error(400, "Bad request", status="INVALID_ARGUMENT")
        client = _FakeGeminiClient([err_400])

        with pytest.raises(ExtractionError, match="HTTP 400"):
            extract_events(make_email(), client, model="gemini-3.5-flash-lite", sleep=lambda _: None)
