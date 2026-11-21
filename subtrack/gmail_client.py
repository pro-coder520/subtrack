"""Gmail OAuth and API client.

Handles the OAuth 2.0 flow for a Desktop app and provides helpers
to build a Gmail service and fetch the authenticated user's profile.
"""

from __future__ import annotations

import base64
import re
import sys
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message

from bs4 import BeautifulSoup
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource

from subtrack.config import CREDENTIALS_PATH, TOKEN_PATH, ensure_data_dir
from subtrack.models import Email

# gmail.readonly — the only scope we ever request
SCOPES: list[str] = ["https://www.googleapis.com/auth/gmail.readonly"]


def authenticate() -> Credentials:
    """Run or refresh the OAuth flow and persist the token.

    - If a valid token exists on disk, reuse it.
    - If the token is expired but has a refresh token, refresh it.
    - If refresh fails (e.g. 7-day expiry in Testing mode), re-run
      the browser-based consent flow.
    - If no token exists at all, run the consent flow from scratch.

    Returns the valid Credentials object.
    """
    ensure_data_dir()
    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except RefreshError:
            # Token refresh failed (e.g. 7-day expiry in Testing mode).
            # Fall through to the browser flow.
            pass

    # Fresh consent flow
    if not CREDENTIALS_PATH.exists():
        print(
            f"Error: OAuth client file not found at {CREDENTIALS_PATH}\n"
            "Download it from the Google Cloud Console and place it there.",
            file=sys.stderr,
        )
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_PATH), SCOPES
    )
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    return creds


def _save_token(creds: Credentials) -> None:
    """Write credentials to the token file."""
    TOKEN_PATH.write_text(creds.to_json())


def get_service(creds: Credentials) -> Resource:
    """Build and return a Gmail API service object."""
    return build("gmail", "v1", credentials=creds)


def get_profile(service: Resource) -> str:
    """Return the authenticated user's email address."""
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


# ---------------------------------------------------------------------------
# Search and fetch
# ---------------------------------------------------------------------------

# Narrow queries from CLAUDE.md. Each one is prefixed with
# newer_than:{SUBTRACK_LOOKBACK} by build_queries, so we never walk the
# whole mailbox.
BASE_QUERIES: list[str] = [
    "category:purchases",
    'subject:(receipt OR invoice OR "payment confirmation" OR "payment received")',
    'subject:(subscription OR renewal OR renews OR "your plan" OR membership OR billing)',
    'subject:(trial OR "trial ends")',
    'subject:("payment failed" OR "payment declined" OR "update your payment" OR declined)',
    'subject:("debit alert" OR "transaction alert" OR "card transaction" OR debited)',
]

# Retries with exponential backoff, handled by googleapiclient, on HTTP 429,
# 5xx, and 403 rateLimitExceeded / userRateLimitExceeded.
NUM_RETRIES = 3

_LOOKBACK_RE = re.compile(r"^\d+[dmy]$")


def build_queries(lookback: str) -> list[str]:
    """Return the search queries, each limited to the lookback window.

    `lookback` uses Gmail's newer_than syntax, e.g. "18m", "90d", "2y".
    """
    if not _LOOKBACK_RE.match(lookback):
        raise ValueError(
            f"Invalid lookback {lookback!r}: use a number followed by d, m or y (e.g. 18m)"
        )
    return [f"newer_than:{lookback} {q}" for q in BASE_QUERIES]


def search_message_ids(
    service: Resource, queries: list[str]
) -> tuple[list[str], dict[str, int]]:
    """Run each query across all result pages and merge the message IDs.

    Returns (ids, hits): ids are de-duplicated, keeping the order in which
    they were first seen; hits maps each query to its raw result count.
    """
    seen: dict[str, None] = {}  # dict keeps insertion order
    hits: dict[str, int] = {}
    for query in queries:
        count = 0
        page_token: str | None = None
        while True:
            kwargs = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = (
                service.users().messages().list(**kwargs)
                .execute(num_retries=NUM_RETRIES)
            )
            for m in resp.get("messages", []):
                count += 1
                seen.setdefault(m["id"], None)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        hits[query] = count
    return list(seen), hits


def fetch_message(service: Resource, message_id: str) -> dict:
    """Fetch one message with its full MIME payload."""
    return (
        service.users().messages()
        .get(userId="me", id=message_id, format="full")
        .execute(num_retries=NUM_RETRIES)
    )


# ---------------------------------------------------------------------------
# MIME -> clean text
# ---------------------------------------------------------------------------

# Below this many characters, the plain-text part is treated as a stub
# ("View this email in your browser") and the HTML part is used instead.
PLAIN_MIN_CHARS = 200

# Zero-width characters, plus soft hyphen (U+00AD) and combining grapheme
# joiner (U+034F), which marketing emails use as invisible preheader padding.
_INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\u034f")
)

_BLOCK_TAGS = [
    "p", "div", "tr", "li", "ul", "ol", "table", "section", "article",
    "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
]
_CELL_TAGS = ["td", "th"]


def strip_zero_width(text: str) -> str:
    """Remove zero-width and other invisible padding characters."""
    return text.translate(_INVISIBLE)


def normalize_whitespace(text: str) -> str:
    """Strip invisibles, turn NBSP into spaces, collapse runs of blanks."""
    text = strip_zero_width(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):  # keep at most one blank line in a row
            out.append(ln)
    return "\n".join(out).strip()


def decode_part_body(data: str, charset: str | None) -> str:
    """Decode a Gmail base64url body using the part's charset.

    Falls back to UTF-8 when the charset is missing or unknown; undecodable
    bytes become U+FFFD rather than raising.
    """
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    """Convert HTML to readable text, keeping line breaks at block boundaries."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for tag in soup.find_all(_CELL_TAGS):
        tag.insert_after(" ")
    # Every block boundary adds newlines, so adjacent blocks would leave blank
    # lines everywhere. Drop them: layout spacing carries no billing meaning
    # and wastes the ~6,000-character LLM budget.
    text = normalize_whitespace(soup.get_text())
    return "\n".join(ln for ln in text.splitlines() if ln)


def _part_headers(part: dict) -> dict[str, str]:
    """Lower-cased header name -> value (first occurrence wins)."""
    headers: dict[str, str] = {}
    for h in part.get("headers", []):
        headers.setdefault(h["name"].lower(), h["value"])
    return headers


def _charset_of(part: dict) -> str | None:
    content_type = _part_headers(part).get("content-type")
    if not content_type:
        return None
    msg = Message()
    msg["Content-Type"] = content_type
    return msg.get_content_charset()


def _is_attachment(part: dict) -> bool:
    if part.get("filename"):
        return True
    disposition = _part_headers(part).get("content-disposition", "")
    return disposition.lower().startswith("attachment")


def extract_text(payload: dict) -> str:
    """Walk the MIME tree and return clean body text.

    Uses the plain-text part unless it is shorter than PLAIN_MIN_CHARS (after
    cleaning), in which case the HTML part is converted and used. Attachments
    are ignored and never downloaded.
    """
    plains: list[str] = []
    htmls: list[str] = []

    def walk(part: dict) -> None:
        if part.get("parts"):
            for child in part["parts"]:
                walk(child)
            return
        if _is_attachment(part):
            return
        data = part.get("body", {}).get("data")
        if not data:
            return
        mime = part.get("mimeType", "").lower()
        if mime == "text/plain":
            plains.append(decode_part_body(data, _charset_of(part)))
        elif mime == "text/html":
            htmls.append(decode_part_body(data, _charset_of(part)))

    walk(payload)

    plain = normalize_whitespace("\n".join(plains))
    if len(plain) >= PLAIN_MIN_CHARS or not htmls:
        return plain
    html_text = html_to_text("\n".join(htmls))
    return html_text or plain


def _decode_header_value(value: str) -> str:
    """Decode RFC 2047 encoded-words if present (defensive)."""
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def parse_message(msg: dict) -> Email:
    """Turn a users.messages.get(format="full") response into an Email."""
    payload = msg.get("payload", {})
    headers = _part_headers(payload)
    received_at = datetime.fromtimestamp(
        int(msg["internalDate"]) / 1000, tz=timezone.utc
    )
    return Email(
        message_id=msg["id"],
        thread_id=msg.get("threadId", ""),
        sender=strip_zero_width(_decode_header_value(headers.get("from", ""))).strip(),
        subject=strip_zero_width(_decode_header_value(headers.get("subject", ""))).strip(),
        received_at=received_at,
        list_unsubscribe="list-unsubscribe" in headers,
        text=extract_text(payload),
    )
