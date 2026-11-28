"""Configuration: paths under ~/.subtrack and environment variables."""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths – all data lives under ~/.subtrack/, never in the repo
# ---------------------------------------------------------------------------

DATA_DIR: Path = Path.home() / ".subtrack"
DB_PATH: Path = DATA_DIR / "subtrack.db"
CREDENTIALS_PATH: Path = DATA_DIR / "credentials.json"
TOKEN_PATH: Path = DATA_DIR / "token.json"
REDACT_PATH: Path = DATA_DIR / "redact.txt"

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_DEFAULT_LOOKBACK = "18m"
DEFAULT_GEMINI_RPM: float = 10.0


def get_model() -> str:
    """Return the Gemini model name from SUBTRACK_MODEL or the default."""
    return os.environ.get("SUBTRACK_MODEL", _DEFAULT_MODEL)


def get_lookback() -> str:
    """Return the Gmail lookback window from SUBTRACK_LOOKBACK or '18m'."""
    return os.environ.get("SUBTRACK_LOOKBACK", _DEFAULT_LOOKBACK)


def get_gemini_api_key() -> str | None:
    """Return GEMINI_API_KEY if set, else None."""
    return os.environ.get("GEMINI_API_KEY")


def get_gemini_rpm() -> float:
    """Return max Gemini requests per minute from SUBTRACK_GEMINI_RPM or 10.0."""
    raw = os.environ.get("SUBTRACK_GEMINI_RPM")
    if raw is None or not raw.strip():
        return DEFAULT_GEMINI_RPM
    try:
        rpm = float(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid SUBTRACK_GEMINI_RPM {raw!r}: must be a positive number") from exc
    if rpm <= 0:
        raise ValueError(f"Invalid SUBTRACK_GEMINI_RPM {raw!r}: must be positive")
    return rpm


def get_redact_terms() -> list[str]:
    """Load personal terms (name, address fragments) to mask before calling Gemini.

    Reads non-empty, non-comment lines from ~/.subtrack/redact.txt (if present)
    plus comma-separated terms from SUBTRACK_REDACT_TERMS. Returned longest-first
    so full names/addresses are masked before shorter parts.
    """
    terms: list[str] = []
    if REDACT_PATH.exists():
        for line in REDACT_PATH.read_text(encoding="utf-8").splitlines():
            cleaned = line.strip()
            if cleaned and not cleaned.startswith("#"):
                terms.append(cleaned)

    env_raw = os.environ.get("SUBTRACK_REDACT_TERMS", "")
    for part in env_raw.split(","):
        cleaned = part.strip()
        if cleaned:
            terms.append(cleaned)

    # Deduplicate case-insensitively while keeping longest first.
    seen: set[str] = set()
    unique: list[str] = []
    for term in sorted(terms, key=len, reverse=True):
        key = term.lower()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique


def ensure_data_dir() -> None:
    """Create ~/.subtrack/ if it doesn't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
