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

import re
from collections.abc import Sequence

from subtrack.models import Email
from subtrack.prefilter import is_otp_or_password_reset

MAX_EMAIL_CHARS: int = 6000

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
