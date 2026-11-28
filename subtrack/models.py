"""Core data types: Email, enums, and leniently validated BillingEvent."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


@dataclass(frozen=True)
class Email:
    """A fetched email, held in memory only.

    `text` is the cleaned body. It is used for prefiltering and extraction
    and must never be written to the database.
    """

    message_id: str
    thread_id: str
    sender: str
    subject: str
    received_at: datetime  # from Gmail internalDate, UTC
    list_unsubscribe: bool
    text: str


# ---------------------------------------------------------------------------
# Billing event enums
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    CHARGE = "charge"
    RENEWAL_NOTICE = "renewal_notice"
    TRIAL_STARTED = "trial_started"
    TRIAL_ENDING = "trial_ending"
    PAYMENT_FAILED = "payment_failed"
    CANCELLED = "cancelled"
    PRICE_CHANGE = "price_change"
    REFUND = "refund"


class Period(str, Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    YEARLY = "yearly"


class Channel(str, Enum):
    CARD = "card"
    APPLE = "apple"
    GOOGLE_PLAY = "google_play"
    PAYPAL = "paypal"
    CARRIER = "carrier"
    UNKNOWN = "unknown"


class Source(str, Enum):
    MERCHANT = "merchant"
    APP_STORE = "app_store"
    BANK_ALERT = "bank_alert"
    CARD_ALERT = "card_alert"
    OTHER = "other"


# ---------------------------------------------------------------------------
# BillingEvent (lenient on optional fields, strict on merchant & event_type)
# ---------------------------------------------------------------------------

_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:$|T|\s)")
_CARD_LAST4_RE = re.compile(r"^(?:[\*xX•#\s-]*)?(\d{4})$")
_MAX_EVIDENCE_CHARS = 300


class BillingEvent(BaseModel):
    """One billing fact extracted from an email.

    Validators are intentionally lenient on optional fields: a malformed
    optional value becomes None (or unknown/other for channel/source) rather
    than discarding the whole event. Invalid `event_type` or empty `merchant`
    still raises ValidationError so that bogus event is dropped.
    """

    model_config = ConfigDict(extra="ignore")

    merchant: str
    event_type: EventType
    amount: Decimal | None = None
    currency: str | None = None
    event_date: date | None = None
    next_charge_date: date | None = None
    period: Period | None = None
    plan: str | None = None
    card_last4: str | None = None
    channel: Channel = Channel.UNKNOWN
    source: Source = Source.OTHER
    confidence: float | None = None
    evidence: str | None = None

    @field_validator("merchant", mode="before")
    @classmethod
    def _validate_merchant(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("merchant must be a string")
        cleaned = re.sub(r"\s+", " ", v).strip()
        if not cleaned:
            raise ValueError("merchant cannot be empty")
        return cleaned

    @field_validator("event_type", mode="before")
    @classmethod
    def _validate_event_type(cls, v: Any) -> EventType:
        if isinstance(v, EventType):
            return v
        if isinstance(v, str):
            return EventType(v.strip().lower())
        raise ValueError(f"invalid event_type: {v!r}")

    @field_validator("amount", mode="before")
    @classmethod
    def _validate_amount(cls, v: Any) -> Decimal | None:
        if v is None or isinstance(v, bool):
            return None
        try:
            if isinstance(v, Decimal):
                dec = v
            elif isinstance(v, (int, float)):
                dec = Decimal(str(v))
            elif isinstance(v, str):
                cleaned = v.strip().replace(",", "")
                if not cleaned:
                    return None
                dec = Decimal(cleaned)
            else:
                return None
        except InvalidOperation:
            return None
        if not dec.is_finite() or dec < 0:
            return None
        return dec

    @field_validator("currency", mode="before")
    @classmethod
    def _validate_currency(cls, v: Any) -> str | None:
        if not isinstance(v, str):
            return None
        code = v.strip().upper()
        return code if _ISO_CURRENCY_RE.match(code) else None

    @field_validator("event_date", "next_charge_date", mode="before")
    @classmethod
    def _validate_date(cls, v: Any) -> date | None:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        if not isinstance(v, str):
            return None
        m = _ISO_DATE_PREFIX_RE.match(v.strip())
        if not m:
            return None
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None

    @field_validator("period", mode="before")
    @classmethod
    def _validate_period(cls, v: Any) -> Period | None:
        if v is None:
            return None
        if isinstance(v, Period):
            return v
        if not isinstance(v, str):
            return None
        try:
            return Period(v.strip().lower())
        except ValueError:
            return None

    @field_validator("plan", mode="before")
    @classmethod
    def _validate_plan(cls, v: Any) -> str | None:
        if not isinstance(v, str):
            return None
        cleaned = re.sub(r"\s+", " ", v).strip()
        return cleaned or None

    @field_validator("card_last4", mode="before")
    @classmethod
    def _validate_card_last4(cls, v: Any) -> str | None:
        if v is None or isinstance(v, bool):
            return None
        s = str(v).strip()
        m = _CARD_LAST4_RE.match(s)
        return m.group(1) if m else None

    @field_validator("channel", mode="before")
    @classmethod
    def _validate_channel(cls, v: Any) -> Channel:
        if isinstance(v, Channel):
            return v
        if isinstance(v, str):
            try:
                return Channel(v.strip().lower())
            except ValueError:
                return Channel.UNKNOWN
        return Channel.UNKNOWN

    @field_validator("source", mode="before")
    @classmethod
    def _validate_source(cls, v: Any) -> Source:
        if isinstance(v, Source):
            return v
        if isinstance(v, str):
            try:
                return Source(v.strip().lower())
            except ValueError:
                return Source.OTHER
        return Source.OTHER

    @field_validator("confidence", mode="before")
    @classmethod
    def _validate_confidence(cls, v: Any) -> float | None:
        if v is None or isinstance(v, bool):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f) or not (0.0 <= f <= 1.0):
            return None
        return f

    @field_validator("evidence", mode="before")
    @classmethod
    def _validate_evidence(cls, v: Any) -> str | None:
        if not isinstance(v, str):
            return None
        cleaned = re.sub(r"\s+", " ", v).strip()
        if not cleaned:
            return None
        return cleaned[:_MAX_EVIDENCE_CHARS]
