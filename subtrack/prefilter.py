"""Prefilter: cheap keyword and amount scoring to drop junk before Gemini.

Hard privacy rule (non-negotiable): OTPs and password-reset emails are
always dropped so they are never sent to any external API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from subtrack.models import Email

# Minimum score required to send an email to extraction.
KEEP_THRESHOLD: float = 2.0


@dataclass(frozen=True)
class PrefilterResult:
    """Prefilter decision with numeric score and human-readable reasons."""

    keep: bool
    score: float
    reasons: list[str]


# ---------------------------------------------------------------------------
# Hard drop: OTPs and password resets (privacy rule)
# ---------------------------------------------------------------------------

_OTP_RESET_SUBJECT_RE = re.compile(
    r"""
    \botp\b
    | one[- ]time\s+(?:password|passcode|code|pin)\b
    | \b(?:verification|security|login|sign[- ]?in|auth(?:entication)?|2fa|two[- ]factor)\s+code\b
    | \bpasscode\b
    | \bpassword\s+reset\b
    | \b(?:reset|forgot|change|recover)\s+(?:your\s+)?password\b
    | \bset\s+a\s+new\s+password\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# High-precision body phrases checked in the first 1,500 chars so generic
# footer links ("Forgot your password?") on real receipts don't trigger.
_OTP_RESET_BODY_RE = re.compile(
    r"""
    \byour\s+(?:one[- ]time\s+(?:password|passcode|code|pin)|otp
        |verification\s+code|security\s+code|login\s+code|sign[- ]?in\s+code
        |authentication\s+code|2fa\s+code|passcode)\s*(?:is\b|:)
    | \buse\s+(?:this\s+|the\s+)?(?:code|otp|passcode)\s+to\s+(?:verify|sign\s+in|log\s+in|complete)
    | \bdo\s+not\s+share\s+this\s+(?:otp|code|passcode)\b
    | \b(?:requested|received\s+a\s+request)\s+to\s+reset\s+your\s+password\b
    | \bclick\s+(?:the\s+(?:link|button)\s+below\s+)?to\s+reset\s+your\s+password\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_otp_or_password_reset(email: Email) -> bool:
    """Return True if the email is an OTP, verification code, or password reset."""
    if _OTP_RESET_SUBJECT_RE.search(email.subject):
        return True
    if _OTP_RESET_BODY_RE.search(email.text[:1500]):
        return True
    return False


# ---------------------------------------------------------------------------
# Bank and card alerts (Domain rule)
# ---------------------------------------------------------------------------

# Debit/transaction alerts from banks or payment switches.
# Card-decline alerts ("card was declined", "payment declined") are NOT
# filtered out by this rule — they are always scored normally so rare,
# high-priority declines are never dropped even for smaller merchants.
_BANK_ALERT_SUBJECT_RE = re.compile(
    r"""
    \b(?:debit|credit|transaction)\s+alert\b
    | \bcard\s+transaction\b
    | \bsuccessful\s+transaction\b
    | \baccount\s+debit(?:ed)?\b
    | \b(?:pos|nip)\s+(?:transaction|transfer|debit)\b
    | \bdebited\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_CARD_DECLINE_RE = re.compile(
    r"\b(?:card\s+(?:was\s+)?declined|payment\s+declined|transaction\s+declined)\b",
    re.IGNORECASE,
)

# Boilerplate/footer noise that must never count as an online service.
_URL_OR_EMAIL_RE = re.compile(r"(?:https?://\S+|www\.\S+|\S+@\S+)", re.IGNORECASE)

_FOOTER_CUTOFF_RE = re.compile(
    r"""
    \b(?:disclaimer|confidentiality\s+notice|thank\s+you\s+for\s+banking
        |thank\s+you\s+for\s+choosing|for\s+(?:enquiries|inquiries))\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_FOOTER_LINE_RE = re.compile(
    r"""
    \b(?:internet\s+banking|mobile\s+banking|online\s+banking
        |download\s+(?:our|the)\s+(?:mobile\s+)?app
        |available\s+on\s+(?:the\s+)?app\s+store
        |get\s+it\s+on\s+google\s+play
        |customer\s+care|contact\s+cent(?:er|re)|call\s+cent(?:er|re)
        |never\s+share\s+your\s+(?:pin|password|otp)
        |do\s+not\s+reply|automated\s+(?:email|message)
        |follow\s+us\s+on|unsubscribe)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Known online services (international SaaS/streaming/cloud/AI + Nigeria).
# Apple/Google patterns require billing/product qualifiers so generic
# "Google Play" / "Apple App Store" app-download badges in footers don't match.
_ONLINE_SERVICES: list[tuple[str, re.Pattern[str]]] = [
    ("netflix", re.compile(r"\bnetflix\b", re.IGNORECASE)),
    ("spotify", re.compile(r"\bspotify\b", re.IGNORECASE)),
    ("openai", re.compile(r"\b(?:openai|chatgpt)\b", re.IGNORECASE)),
    ("anthropic", re.compile(r"\b(?:anthropic|claude\.ai|claude\s+pro)\b", re.IGNORECASE)),
    ("apple", re.compile(r"\b(?:apple\.com/bill|apple\.com|itunes|icloud|apple\s+(?:music|one|tv|arcade))\b", re.IGNORECASE)),
    ("google", re.compile(r"\b(?:google\s*\*|google\s+(?:play|one|cloud|workspace|storage)|youtube)\b", re.IGNORECASE)),
    ("aws", re.compile(r"\b(?:aws|amazon\s+web\s+services)\b", re.IGNORECASE)),
    ("amazon_prime", re.compile(r"\b(?:amazon\s+prime|prime\s+video|audible)\b", re.IGNORECASE)),
    ("microsoft", re.compile(r"\b(?:microsoft\s*(?:365)?|office\s*365|xbox|azure)\b", re.IGNORECASE)),
    ("dstv", re.compile(r"\bdstv\b", re.IGNORECASE)),
    ("gotv", re.compile(r"\bgotv\b", re.IGNORECASE)),
    ("showmax", re.compile(r"\bshowmax\b", re.IGNORECASE)),
    ("startimes", re.compile(r"\bstartimes\b", re.IGNORECASE)),
    ("starlink", re.compile(r"\bstarlink\b", re.IGNORECASE)),
    ("spectranet", re.compile(r"\bspectranet\b", re.IGNORECASE)),
    ("github", re.compile(r"\bgithub\b", re.IGNORECASE)),
    ("gitlab", re.compile(r"\bgitlab\b", re.IGNORECASE)),
    ("cursor", re.compile(r"\bcursor\b", re.IGNORECASE)),
    ("vercel", re.compile(r"\bvercel\b", re.IGNORECASE)),
    ("railway", re.compile(r"\brailway\b", re.IGNORECASE)),
    ("netlify", re.compile(r"\bnetlify\b", re.IGNORECASE)),
    ("render", re.compile(r"\brender\.com\b", re.IGNORECASE)),
    ("heroku", re.compile(r"\bheroku\b", re.IGNORECASE)),
    ("digitalocean", re.compile(r"\bdigitalocean\b", re.IGNORECASE)),
    ("cloudflare", re.compile(r"\bcloudflare\b", re.IGNORECASE)),
    ("supabase", re.compile(r"\bsupabase\b", re.IGNORECASE)),
    ("sentry", re.compile(r"\bsentry\b", re.IGNORECASE)),
    ("coderabbit", re.compile(r"\bcoderabbit\b", re.IGNORECASE)),
    ("jetbrains", re.compile(r"\bjetbrains\b", re.IGNORECASE)),
    ("replit", re.compile(r"\breplit\b", re.IGNORECASE)),
    ("adobe", re.compile(r"\badobe\b", re.IGNORECASE)),
    ("figma", re.compile(r"\bfigma\b", re.IGNORECASE)),
    ("canva", re.compile(r"\bcanva\b", re.IGNORECASE)),
    ("notion", re.compile(r"\bnotion\b", re.IGNORECASE)),
    ("linear", re.compile(r"\blinear\.app\b", re.IGNORECASE)),
    ("slack", re.compile(r"\bslack\b", re.IGNORECASE)),
    ("zoom", re.compile(r"\bzoom\.(?:us|com)\b|\bzoom\s+video\b", re.IGNORECASE)),
    ("discord", re.compile(r"\bdiscord\b", re.IGNORECASE)),
    ("dropbox", re.compile(r"\bdropbox\b", re.IGNORECASE)),
    ("1password", re.compile(r"\b1password\b", re.IGNORECASE)),
    ("bitwarden", re.compile(r"\bbitwarden\b", re.IGNORECASE)),
    ("proton", re.compile(r"\bproton(?:\s*(?:mail|vpn|pass|unlimited))?\b", re.IGNORECASE)),
    ("nordvpn", re.compile(r"\bnordvpn\b", re.IGNORECASE)),
    ("expressvpn", re.compile(r"\bexpressvpn\b", re.IGNORECASE)),
    ("linkedin", re.compile(r"\blinkedin\b", re.IGNORECASE)),
    ("scribd", re.compile(r"\bscribd\b", re.IGNORECASE)),
    ("medium", re.compile(r"\bmedium\.com\b|\bmedium\s+membership\b", re.IGNORECASE)),
    ("substack", re.compile(r"\bsubstack\b", re.IGNORECASE)),
    ("duolingo", re.compile(r"\bduolingo\b", re.IGNORECASE)),
    ("coursera", re.compile(r"\bcoursera\b", re.IGNORECASE)),
    ("udemy", re.compile(r"\budemy\b", re.IGNORECASE)),
    ("grammarly", re.compile(r"\bgrammarly\b", re.IGNORECASE)),
    ("chess.com", re.compile(r"\bchess\.com\b", re.IGNORECASE)),
    ("playstation", re.compile(r"\bplaystation\b|\bps\s*plus\b", re.IGNORECASE)),
    ("crunchyroll", re.compile(r"\bcrunchyroll\b", re.IGNORECASE)),
    ("twitch", re.compile(r"\btwitch\b", re.IGNORECASE)),
    ("namecheap", re.compile(r"\bnamecheap\b", re.IGNORECASE)),
    ("godaddy", re.compile(r"\bgodaddy\b", re.IGNORECASE)),
    ("porkbun", re.compile(r"\bporkbun\b", re.IGNORECASE)),
    ("paddle", re.compile(r"\bpaddle\.(?:net|com)\b", re.IGNORECASE)),
    ("fastspring", re.compile(r"\bfastspring\b", re.IGNORECASE)),
    ("lemonsqueezy", re.compile(r"\blemonsqueezy\b", re.IGNORECASE)),
]


def is_bank_or_card_alert(email: Email) -> bool:
    """Return True for bank/switch debit or transaction alerts (excluding declines)."""
    if _CARD_DECLINE_RE.search(email.subject) or _CARD_DECLINE_RE.search(email.text[:500]):
        return False
    return bool(_BANK_ALERT_SUBJECT_RE.search(email.subject))


def _strip_bank_footer_noise(text: str) -> str:
    """Remove bank footer boilerplate, URLs and email addresses before matching."""
    cut = _FOOTER_CUTOFF_RE.search(text)
    if cut:
        text = text[: cut.start()]
    kept_lines: list[str] = []
    for line in text.splitlines():
        if _FOOTER_LINE_RE.search(line):
            continue
        cleaned = _URL_OR_EMAIL_RE.sub(" ", line)
        kept_lines.append(cleaned)
    return "\n".join(kept_lines)


def find_online_service(email: Email) -> str | None:
    """Return the first known online service named in the alert body/subject, or None."""
    cleaned = _strip_bank_footer_noise(f"{email.subject}\n{email.text}")
    for name, pattern in _ONLINE_SERVICES:
        if pattern.search(cleaned):
            return name
    return None


# ---------------------------------------------------------------------------
# Amount detection
# ---------------------------------------------------------------------------

# Matches currency amounts ($, ₦, £, €, USD, NGN, GBP, EUR, or N1,000 / N500)
# while ignoring discount phrases like "$30 off" or "₦500 cashback".
# _NUM_END prevents regex backtracking mid-number (so "$30 off" can't match "$3")
# while still allowing trailing sentence punctuation ("$20.00." or "₦4,500,").
_NUM_END = r"(?!(?:\d|,\d|\.\d))"
_DISCOUNT_SUFFIX = r"(?!\s*(?:off\b|discount\b|cashback\b|credit\b|bonus\b|voucher\b|coupon\b|savings?\b))"

_AMOUNT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "symbol",
        re.compile(
            r"(?:[$₦£€])\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
            + _NUM_END
            + _DISCOUNT_SUFFIX,
            re.IGNORECASE,
        ),
    ),
    (
        "iso_prefix",
        re.compile(
            r"\b(?:USD|NGN|GBP|EUR)\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
            + _NUM_END
            + _DISCOUNT_SUFFIX,
            re.IGNORECASE,
        ),
    ),
    (
        "iso_suffix",
        re.compile(
            r"\b(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?\s?(?:USD|NGN|GBP|EUR)\b"
            + _DISCOUNT_SUFFIX,
            re.IGNORECASE,
        ),
    ),
    (
        # Nigerian N-prefix: N1,000, N 4,500.00, or N500 (at least 3 digits or comma-grouped,
        # not followed by K/M/B like share prices "N3.20K").
        "naira_n",
        re.compile(
            r"(?<![A-Za-z])N\s?(?:\d{1,3}(?:,\d{3})+|\d{3,})(?:\.\d{1,2})?"
            + _NUM_END
            + r"(?![A-Za-z])"
            + _DISCOUNT_SUFFIX,
        ),
    ),
]


def has_amount(text: str) -> str | None:
    """Return the first non-discount currency amount found in text, or None."""
    for _, pattern in _AMOUNT_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0).strip()
    return None


# ---------------------------------------------------------------------------
# Keyword & penalty patterns
# ---------------------------------------------------------------------------

# Urgent billing events (+4.0): high-value signals that should survive
# List-Unsubscribe (-2.0) unless outweighed by marketing penalties.
_URGENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "payment_failed",
        re.compile(
            r"""
            \bpayment\s+(?:failed|declined|was\s+unsuccessful|unsuccessful|issue|problem)\b
            | \bcharge\s+(?:failed|declined)\b
            | \bcard\s+(?:was\s+)?declined\b
            | \btransaction\s+declined\b
            | \b(?:unable|couldn['’]t|could\s+not)\s+to?\s*process\s+your\s+payment\b
            | \bupdate\s+your\s+(?:payment|billing)\b
            | \bbilling\s+problem\b
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
    (
        "trial",
        re.compile(
            r"""
            \btrial\s+(?:ends|will\s+end|has\s+ended|is\s+ending|ending)\b
            | \btrial\s+plan\s+alert\b
            | \bstarted\s+a\s+(?:free\s+)?trial\b
            | \bwelcome\s+to\s+your\s+(?:[\w.-]+\s+){0,3}trial\b
            | \byour\s+(?:[\w.-]+\s+){0,3}trial\s+(?:ends|expires|will\s+end|has\s+ended|is\s+ending)\b
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
    (
        "cancelled",
        re.compile(
            r"""
            \bsubscription\s+(?:has\s+been\s+|was\s+|is\s+|will\s+be\s+)?cancell?ed\b
            | \b(?:have|you['’]ve)\s+cancell?ed\b
            | \b(?:membership|subscription)\s+(?:has\s+)?ended\b
            | \bsubscription\s+benefits\s+are\s+ending\b
            | \bplan\s+(?:has\s+been\s+|was\s+|will\s+be\s+)?cancell?ed\b
            | \bbilling\s+account\b.*\bclosed\b
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
    (
        "price_change",
        re.compile(
            r"""
            \bprice\s+(?:increase|change|update)\b
            | \bchanges\s+to\s+your\s+(?:[\w.-]+\s+){0,3}(?:subscription|plan|membership)\b
            | \bupdating\s+our\s+prices\b
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
]

# Strong transactional billing signals in the subject (+2.5).
_STRONG_SUBJECT_BILLING_RE = re.compile(
    r"""
    \breceipt\b
    | \binvoice\b
    | \bpayment\s+(?:confirmation|received|successful)\b
    | \border\s+confirmation\b
    | \bsubscription\s+confirmation\b
    | \b(?:has\s+)?renewed\b
    | \brenews\b
    | \brenewal\b
    | \brenew\s+your\b
    | \byour\s+(?:[\w.-]+\s+){0,2}(?:plan|subscription|membership)\b
    | \brefund\b
    | \bupgraded\s+to\s+a\s+paid\b
    | \bbilling\s+statement\b
    | \bconfirmation\s+of\s+(?:payment|[\w+]+\s+membership)\b
    | \bdues\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Broader billing words in the subject (+1.0, only when no strong match).
_WEAK_SUBJECT_BILLING_RE = re.compile(
    r"\b(?:subscription|membership|billing|payment|plan|order|purchase)\b",
    re.IGNORECASE,
)

# Transactional billing terms in the body (+1.0 for 1 distinct hit, +1.5 for 2+).
_BODY_BILLING_TERMS: list[tuple[str, re.Pattern[str]]] = [
    ("receipt", re.compile(r"\breceipt\b", re.IGNORECASE)),
    ("invoice", re.compile(r"\binvoice\b", re.IGNORECASE)),
    ("charged", re.compile(r"\b(?:charged|billed)\b", re.IGNORECASE)),
    ("renew", re.compile(r"\b(?:auto-?renew\w*|renews|renewal|next\s+billing|billing\s+(?:date|cycle))\b", re.IGNORECASE)),
    ("payment_method", re.compile(r"\b(?:payment\s+method|card\s+ending|paid\s+with)\b", re.IGNORECASE)),
    ("total", re.compile(r"\b(?:order\s+total|amount\s+paid|total\s+paid|subtotal|amount\s+due)\b", re.IGNORECASE)),
    ("refund", re.compile(r"\brefund\b", re.IGNORECASE)),
    ("subscription", re.compile(r"\b(?:subscription|membership)\b", re.IGNORECASE)),
]

# Marketing / promo / newsletter-opt-in penalties.
_MARKETING_RULES: list[tuple[str, float, re.Pattern[str]]] = [
    (
        "discount_offer",
        -3.0,
        re.compile(
            r"""
            \b\d{1,2}%\s*off\b
            | (?:[$₦£€]|USD|NGN)\s?\d+(?:,\d{3})*\s*off\b
            | \bhalf[- ]price\b
            | \bsave\s+\d{1,2}%\b
            | \b\d{1,2}%\s+discount\b
            | \bdiscount\s+on\s+
            | \bpromo\s+code\b
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
    (
        "urgency_promo",
        -3.0,
        re.compile(
            r"""
            \blimited\s+time(?:\s+offer)?\b
            | \bfinal\s+hours\b
            | \blast\s+chance\b
            | \blast\s+call\b
            | \bhurry\b
            | \bclaim\s+your\b
            | \bspecial\s+offer\b
            | \bblack\s+friday\b
            | \bcyber\s+monday\b
            | \bsingles['’]\s+day\b
            | \b(?:give|gift)\s+the\s+(?:gift|magic)\b
            | \bwin\s+a\b
            | \bdeploy\s+now\b
            | \bunlocks\s+extra\s+savings\b
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
    (
        "trial_promo",
        -3.0,
        re.compile(
            r"""
            \bget\s+your\s+free\s+trial\b
            | \bwant\s+another\s+(?:\d+[- ]day\s+)?free\s+trial\b
            | \bstart\s+your\s+free\s+trial\b
            | \bfree\s+trial\s*[:|!]
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
    (
        "newsletter_optin",
        -4.0,
        re.compile(
            r"""
            \b(?:please\s+)?confirm\s+(?:your\s+)?subscription\b
            | \bchoose\s+a\s+subscription\s+plan\b
            | \bhas\s+added\s+you\s+to\s+their\s+email\s+list\b
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
    ),
]


def score_email(email: Email) -> PrefilterResult:
    """Score an email and decide whether to send it to LLM extraction."""
    if is_otp_or_password_reset(email):
        return PrefilterResult(
            keep=False,
            score=-100.0,
            reasons=["hard_drop:otp_or_password_reset"],
        )

    if is_bank_or_card_alert(email):
        service = find_online_service(email)
        if service:
            return PrefilterResult(
                keep=True,
                score=4.0,
                reasons=[f"alert_online_service:{service}(+4)"],
            )
        return PrefilterResult(
            keep=False,
            score=-3.0,
            reasons=["alert_non_online(-3)"],
        )

    score = 0.0
    reasons: list[str] = []
    combined = f"{email.subject}\n{email.text}"

    # 1. Urgent billing events (failed payment, trial ending, cancelled, price change)
    urgent_hits: list[str] = []
    for name, pattern in _URGENT_RULES:
        if pattern.search(email.subject) or pattern.search(email.text[:2000]):
            urgent_hits.append(name)
    if urgent_hits:
        pts = 4.0
        score += pts
        reasons.append(f"urgent:{','.join(urgent_hits)}(+{pts:g})")

    # 2. Billing keywords in subject
    strong_m = _STRONG_SUBJECT_BILLING_RE.search(email.subject)
    if strong_m:
        score += 2.5
        reasons.append(f"subject_billing:{strong_m.group(0).lower()}(+2.5)")
    else:
        weak_m = _WEAK_SUBJECT_BILLING_RE.search(email.subject)
        if weak_m:
            score += 1.0
            reasons.append(f"subject_weak:{weak_m.group(0).lower()}(+1)")

    # 3. Billing keywords in body
    body_hits = [name for name, pat in _BODY_BILLING_TERMS if pat.search(email.text[:4000])]
    if len(body_hits) >= 2:
        score += 1.5
        reasons.append(f"body_billing:{','.join(body_hits[:3])}(+1.5)")
    elif len(body_hits) == 1:
        score += 1.0
        reasons.append(f"body_billing:{body_hits[0]}(+1)")

    # 4. Amount pattern (non-discount currency amount)
    amt = has_amount(combined[:4000])
    if amt:
        score += 2.0
        reasons.append(f"amount:{amt}(+2)")

    # 5. List-Unsubscribe penalty
    if email.list_unsubscribe:
        score -= 2.0
        reasons.append("list_unsubscribe(-2)")

    # 6. Marketing / newsletter penalties (capped at -6.0 total)
    marketing_total = 0.0
    for name, penalty, pattern in _MARKETING_RULES:
        if pattern.search(combined[:3000]):
            applied = max(penalty, -6.0 - marketing_total)
            if applied < 0:
                marketing_total += applied
                score += applied
                reasons.append(f"marketing:{name}({applied:g})")

    return PrefilterResult(
        keep=score >= KEEP_THRESHOLD,
        score=score,
        reasons=reasons,
    )
