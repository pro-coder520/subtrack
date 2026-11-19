"""Configuration: paths under ~/.subtrack and environment variables."""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths – all data lives under ~/.subtrack/, never in the repo
# ---------------------------------------------------------------------------

DATA_DIR: Path = Path.home() / ".subtrack"
DB_PATH: Path = DATA_DIR / "subtrack.db"
CREDENTIALS_PATH: Path = DATA_DIR / "credentials.json"
TOKEN_PATH: Path = DATA_DIR / "token.json"

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_DEFAULT_LOOKBACK = "18m"


def get_model() -> str:
    """Return the Gemini model name from SUBTRACK_MODEL or the default."""
    return os.environ.get("SUBTRACK_MODEL", _DEFAULT_MODEL)


def get_lookback() -> str:
    """Return the Gmail lookback window from SUBTRACK_LOOKBACK or '18m'."""
    return os.environ.get("SUBTRACK_LOOKBACK", _DEFAULT_LOOKBACK)


def get_gemini_api_key() -> str | None:
    """Return GEMINI_API_KEY if set, else None."""
    return os.environ.get("GEMINI_API_KEY")


def ensure_data_dir() -> None:
    """Create ~/.subtrack/ if it doesn't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
