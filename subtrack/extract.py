"""LLM extraction: privacy redaction, truncation, prompt, schema, and Gemini client.

Hard privacy rules enforced here before any text ever leaves the machine:
- Refuse OTP / password-reset emails even if called directly.
- Mask every run of 8+ digits (cards, accounts, phone numbers) down to last 4,
  while leaving dates and currency amounts intact.
- Mask email addresses, street addresses, and user-supplied personal terms
  (name, address fragments from ~/.subtrack/redact.txt / SUBTRACK_REDACT_TERMS).
- Truncate outbound email text to ~6,000 characters.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError

from subtrack import config
from subtrack.models import BillingEvent, Channel, Email, EventType, Period, Source
from subtrack.prefilter import is_otp_or_password_reset

PROMPT_VERSION: str = "v1"
MAX_EMAIL_CHARS: int = 6000
MAX_OUTPUT_TOKENS: int = 2048


class ExtractionError(Exception):
    """Raised when an extraction call fails or returns incomplete/unparseable output."""


class QuotaExhaustedError(Exception):
    """Raised when Gemini daily quota is exhausted so sync can stop cleanly."""


def extractor_version(model: str) -> str:
    """Version stamp recorded on emails: combines model name and prompt version."""
    return f"{model}:{PROMPT_VERSION}"

# ---------------------------------------------------------------------------
# Redaction patterns
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")

# Labeled address lines, e.g. "Billing Address: 12 Example Rd, Lagos"
_LABELED_ADDRESS_RE = re.compile(
    r"(?im)^((?:billing|shipping|mailing|street|home|residential)\s+address|address)\s*:\s*.+$"
)

# Generic street address phrases (Nigerian + international suffixes).
_STREET_ADDRESS_RE = re.compile(
    r"""
    (?:\b(?:No\.?\s*|Plot\s+|Flat\s+|Apt\.?\s*|Suite\s+|Block\s+|Unit\s+))?
    \b\d{1,5}[A-Za-z]?(?:,\s*|\s+)
    [A-Za-z0-9'.-]+(?:\s+[A-Za-z0-9'.-]+){0,4}\s+
    (?:Street|St\.?|Road|Rd\.?|Avenue|Ave\.?|Boulevard|Blvd\.?|Close|Crescent|Cres\.?
      |Lane|Ln\.?|Drive|Dr\.?|Way|Estate|Court|Ct\.?|Place|Pl\.?|Terrace
      |Parkway|Pkwy\.?|Highway|Hwy\.?|Expressway|Layout|Quarters)\b
    (?:[,\s]+(?:Apt\.?|Suite|Unit|Floor|Fl\.?|Flat|Block|Phase)\s*[A-Za-z0-9-]+)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Contiguous run of 8+ digits (account numbers, unformatted cards, phones).
_CONTIGUOUS_DIGITS_RE = re.compile(r"(?<!\d)(\d{8,})(?!\d)")

# Formatted card / phone numbers with spaces, hyphens, or parentheses, e.g.:
#   4111-1111-1111-4417, 4111 1111 1111 4417, +234 803 123 4567, (415) 555-0199
_FORMATTED_NUM_RE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)|\d{2,4})(?:[\s-]+\d{2,5}){2,4}(?!\d)"
)

_DATE_SHAPES_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4})$")


def _mask_digits_to_last4(match: re.Match[str]) -> str:
    raw = match.group(0)
    if _DATE_SHAPES_RE.match(raw.strip()):
        return raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 8:
        return raw
    return f"****{digits[-4:]}"


def _mask_extra_terms(text: str, terms: Sequence[str]) -> str:
    for term in sorted((t.strip() for t in terms if t and t.strip()), key=len, reverse=True):
        if len(term) < 2:
            continue
        escaped = re.escape(term)
        # Single alphanumeric word -> use word boundaries so "Ade" doesn't corrupt "Upgrade".
        if re.fullmatch(r"[A-Za-z0-9_]+", term):
            pattern = re.compile(rf"\b{escaped}\b", re.IGNORECASE)
        else:
            pattern = re.compile(escaped, re.IGNORECASE)
        text = pattern.sub("[REDACTED]", text)
    return text


def redact_text(text: str, extra_terms: Sequence[str] = ()) -> str:
    """Mask PII before sending text to Gemini.

    Order matters:
    1. User-supplied personal terms (name, address fragments)
    2. Email addresses -> [EMAIL]
    3. Street addresses -> [ADDRESS]
    4. Formatted card/phone numbers & contiguous 8+ digit runs -> ****<last4>
    """
    text = _mask_extra_terms(text, extra_terms)
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _LABELED_ADDRESS_RE.sub(r"\1: [ADDRESS]", text)
    text = _STREET_ADDRESS_RE.sub("[ADDRESS]", text)
    text = _FORMATTED_NUM_RE.sub(_mask_digits_to_last4, text)
    text = _CONTIGUOUS_DIGITS_RE.sub(lambda m: f"****{m.group(1)[-4:]}", text)
    return text


def redact_sender(sender: str, extra_terms: Sequence[str] = ()) -> str:
    """Mask the sender's email address while preserving the sender domain for merchant identification."""
    def _replace_email(match: re.Match[str]) -> str:
        domain = match.group(1).lower()
        return f"[EMAIL] (domain: {domain})"

    masked = _mask_extra_terms(sender, extra_terms)
    masked = _EMAIL_RE.sub(_replace_email, masked)
    masked = _FORMATTED_NUM_RE.sub(_mask_digits_to_last4, masked)
    masked = _CONTIGUOUS_DIGITS_RE.sub(lambda m: f"****{m.group(1)[-4:]}", masked)
    return masked


def prepare_email_for_llm(
    email: Email,
    extra_terms: Sequence[str] = (),
    max_chars: int = MAX_EMAIL_CHARS,
) -> str:
    """Build the redacted, length-capped email payload for Gemini.

    Raises ValueError if the email looks like an OTP or password reset.
    """
    if is_otp_or_password_reset(email):
        raise ValueError("Refusing to send OTP or password-reset email to LLM")

    header = (
        f"From: {redact_sender(email.sender, extra_terms)}\n"
        f"Date: {email.received_at.date().isoformat()}\n"
        f"Subject: {redact_text(email.subject, extra_terms)}\n\n"
    )
    body = redact_text(email.text, extra_terms)
    budget = max(0, max_chars - len(header))
    if len(body) > budget:
        marker = "\n[truncated]"
        cut = max(0, budget - len(marker))
        body = body[:cut].rstrip() + marker
    return header + body


# ---------------------------------------------------------------------------
# Prompt and Gemini response schema (simple, separate from BillingEvent)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You extract billing and subscription events from personal emails.

Rules:
- One email may contain zero, one, or several events. If the email is not a real
  billing event (for example a promo, newsletter, investment transfer, or general
  notice), return an empty events list.
- For App Store and Google Play emails, set `merchant` to the app or service
  being billed (for example "Google One", "YouTube", "Chess.com"), not "Apple"
  or "Google Play", and set `channel` to `apple` or `google_play` and `source`
  to `app_store`.
- For payment processor receipts (Paystack, Stripe, Korapay, Remita, Squad, etc.),
  set `merchant` to the business being paid, not the processor.
- Keep `amount` (plain decimal string, no currency symbol or commas) and
  `currency` (3-letter ISO code like USD, NGN, GBP, EUR) exactly as shown. Never
  convert currencies.
- For `price_change`, set `amount` to the new price.
- Set `period` only when the billing cycle is explicitly stated or unmistakable;
  otherwise leave it null.
- Use YYYY-MM-DD for `event_date` and `next_charge_date`. If `event_date` is not
  explicitly stated in the body, use the email's Date header.
- `evidence` must be a short verbatim quote (under 20 words) from the email.
"""

# Simple OpenAPI-subset schema kept separate from BillingEvent (CLAUDE.md).
RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    required=["events"],
    properties={
        "events": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                required=["merchant", "event_type"],
                properties={
                    "merchant": types.Schema(type=types.Type.STRING),
                    "event_type": types.Schema(
                        type=types.Type.STRING,
                        enum=[e.value for e in EventType],
                    ),
                    "amount": types.Schema(type=types.Type.STRING, nullable=True),
                    "currency": types.Schema(type=types.Type.STRING, nullable=True),
                    "event_date": types.Schema(type=types.Type.STRING, nullable=True),
                    "next_charge_date": types.Schema(type=types.Type.STRING, nullable=True),
                    "period": types.Schema(
                        type=types.Type.STRING,
                        enum=[p.value for p in Period],
                        nullable=True,
                    ),
                    "plan": types.Schema(type=types.Type.STRING, nullable=True),
                    "card_last4": types.Schema(type=types.Type.STRING, nullable=True),
                    "channel": types.Schema(
                        type=types.Type.STRING,
                        enum=[c.value for c in Channel],
                        nullable=True,
                    ),
                    "source": types.Schema(
                        type=types.Type.STRING,
                        enum=[s.value for s in Source],
                        nullable=True,
                    ),
                    "confidence": types.Schema(type=types.Type.NUMBER, nullable=True),
                    "evidence": types.Schema(type=types.Type.STRING, nullable=True),
                },
            ),
        ),
    },
)


# ---------------------------------------------------------------------------
# Response parsing (strict on truncation/malformed JSON, lenient on events)
# ---------------------------------------------------------------------------


def _finish_reason_name(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None)
    if not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return None
    return getattr(reason, "name", str(reason)).upper()


def parse_extraction_response(response: Any) -> list[BillingEvent]:
    """Parse a Gemini generate_content response into validated BillingEvents.

    Empty, truncated (finish_reason != STOP), or unparseable responses raise
    ExtractionError rather than being mistaken for "no events".
    Within a valid response, events with an invalid event_type or empty
    merchant are dropped, while bad optional fields become None.
    """
    reason = _finish_reason_name(response)
    if reason is not None and reason != "STOP":
        raise ExtractionError(f"Incomplete model response (finish_reason={reason})")

    raw_text = getattr(response, "text", None)
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ExtractionError("Empty response from model")

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"Unparseable JSON response: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ExtractionError("Response JSON must be an object with an 'events' list")

    events: list[BillingEvent] = []
    for item in payload["events"]:
        if not isinstance(item, dict):
            continue
        try:
            events.append(BillingEvent.model_validate(item))
        except ValidationError:
            # Invalid event_type or empty merchant -> drop just this event.
            continue
    return events


# ---------------------------------------------------------------------------
# Rate limiting, backoff, and Gemini API call
# ---------------------------------------------------------------------------

_DAILY_QUOTA_RE = re.compile(
    r"per\s*_?\s*day|daily|requestsperday|\brpd\b|generate_requests_per_model_per_day",
    re.IGNORECASE,
)
_RETRY_DELAY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*s")


class RateLimiter:
    """Simple spacing throttle to stay within a requests-per-minute budget."""

    def __init__(
        self,
        rpm: float,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if rpm <= 0:
            raise ValueError("rpm must be positive")
        self.min_interval = 60.0 / rpm
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call: float | None = None

    def wait(self) -> None:
        now = self._monotonic()
        if self._last_call is not None:
            remaining = self.min_interval - (now - self._last_call)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_call = now


def is_daily_quota_error(exc: genai_errors.APIError) -> bool:
    """Return True if an HTTP 429 APIError indicates daily quota exhaustion."""
    haystack = f"{exc.message or ''} {exc.details or ''}"
    return bool(_DAILY_QUOTA_RE.search(haystack))


def _retry_delay_from_error(exc: genai_errors.APIError) -> float | None:
    """Extract RetryInfo retryDelay seconds from APIError.details if present."""
    details = exc.details
    if not isinstance(details, dict):
        return None
    err = details.get("error")
    if not isinstance(err, dict):
        return None
    for entry in err.get("details") or []:
        if isinstance(entry, dict) and "retryDelay" in entry:
            m = _RETRY_DELAY_RE.search(str(entry["retryDelay"]))
            if m:
                return min(float(m.group(1)), 65.0)
    return None


def make_client(api_key: str | None = None) -> genai.Client:
    """Build a google-genai Client from GEMINI_API_KEY."""
    key = api_key or config.get_gemini_api_key()
    if not key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Export it in your environment before running sync."
        )
    return genai.Client(api_key=key)


def extract_events(
    email: Email,
    client: Any,
    model: str,
    extra_redact_terms: Sequence[str] = (),
    rate_limiter: RateLimiter | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_429_retries: int = 3,
    base_429_backoff: float = 5.0,
) -> list[BillingEvent]:
    """Redact an email, call Gemini in JSON mode, and return validated BillingEvents.

    - Throttles via `rate_limiter` if provided.
    - Retries transient HTTP 429 (per-minute limits) with exponential backoff.
    - Raises QuotaExhaustedError on daily quota exhaustion (or persistent 429).
    - Raises ExtractionError on API failures or empty/truncated/unparseable output.
    """
    payload = prepare_email_for_llm(email, extra_terms=extra_redact_terms)
    gen_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        temperature=0.0,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )

    retries_429 = 0
    retries_5xx = 0
    while True:
        if rate_limiter is not None:
            rate_limiter.wait()
        try:
            response = client.models.generate_content(
                model=model,
                contents=payload,
                config=gen_config,
            )
            return parse_extraction_response(response)
        except genai_errors.APIError as exc:
            code = exc.code
            if code == 429:
                if is_daily_quota_error(exc):
                    raise QuotaExhaustedError(
                        f"Gemini daily quota exhausted: {exc.message or exc.status}"
                    ) from exc
                if retries_429 >= max_429_retries:
                    raise QuotaExhaustedError(
                        f"Gemini HTTP 429 persisted after {max_429_retries} retries: "
                        f"{exc.message or exc.status}"
                    ) from exc
                delay = max(
                    base_429_backoff * (2 ** retries_429),
                    _retry_delay_from_error(exc) or 0.0,
                )
                retries_429 += 1
                sleep(delay)
                continue
            if code in (500, 502, 503, 504) and retries_5xx < 2:
                delay = 2.0 * (2 ** retries_5xx)
                retries_5xx += 1
                sleep(delay)
                continue
            raise ExtractionError(
                f"Gemini API error (HTTP {code} {exc.status}): {exc.message}"
            ) from exc
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError(f"Unexpected extraction failure: {exc}") from exc
