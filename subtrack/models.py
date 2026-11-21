"""Core data types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
