"""Shared test helpers.

Fixtures in tests/fixtures/ are synthetic emails written in a readable form
(plain text bodies plus a charset). `build_gmail_message` turns them into the
JSON shape returned by Gmail's users.messages.get(format="full"), including
base64url-encoded bodies, so tests exercise the real decoding path.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _b64url(raw: bytes) -> str:
    # Gmail often omits padding; strip it so the decoder must cope.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _build_part(spec: dict) -> dict:
    mime = spec["mimeType"]
    if "parts" in spec:
        return {
            "partId": "",
            "mimeType": mime,
            "filename": "",
            "headers": [{"name": "Content-Type", "value": f'{mime}; boundary="b"'}],
            "body": {"size": 0},
            "parts": [_build_part(p) for p in spec["parts"]],
        }

    charset = spec.get("charset")
    content_type = mime + (f'; charset="{charset}"' if charset else "")
    headers = [{"name": "Content-Type", "value": content_type}]
    if "disposition" in spec:
        headers.append({"name": "Content-Disposition", "value": spec["disposition"]})

    if "attachmentId" in spec:
        body = {"attachmentId": spec["attachmentId"], "size": 1234}
    else:
        raw = spec["text"].encode(spec.get("encode_as") or charset or "utf-8")
        body = {"data": _b64url(raw), "size": len(raw)}

    return {
        "partId": "",
        "mimeType": mime,
        "filename": spec.get("filename", ""),
        "headers": headers,
        "body": body,
    }


def build_gmail_message(spec: dict) -> dict:
    payload = _build_part(spec["payload"])
    top_headers = [{"name": k, "value": v} for k, v in spec["headers"].items()]
    payload["headers"] = top_headers + payload["headers"]
    return {
        "id": spec["id"],
        "threadId": spec.get("threadId", spec["id"]),
        "internalDate": spec["internalDate"],
        "labelIds": [],
        "snippet": "",
        "payload": payload,
    }


def load_fixture(name: str) -> dict:
    spec = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return build_gmail_message(spec)


@pytest.fixture()
def gmail_message():
    """Factory: gmail_message("plain_only") -> Gmail API message dict."""
    return load_fixture


# ---------------------------------------------------------------------------
# Fake Gmail service (no network)
# ---------------------------------------------------------------------------


class _Request:
    def __init__(self, result, log, call):
        self._result = result
        self._log = log
        self._call = call

    def execute(self, num_retries: int = 0):
        self._log.append((*self._call, num_retries))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Messages:
    def __init__(self, svc: "FakeGmailService"):
        self._svc = svc

    def list(self, userId, q, maxResults=100, pageToken=None):
        assert userId == "me"
        pages = self._svc.pages.get(q, [{}])
        index = int(pageToken) if pageToken else 0
        page = dict(pages[index])
        if index + 1 < len(pages):
            page["nextPageToken"] = str(index + 1)
        call = ("list", q, pageToken, maxResults)
        return _Request(page, self._svc.calls, call)

    def get(self, userId, id, format=None):
        assert userId == "me"
        call = ("get", id, format)
        return _Request(self._svc.messages[id], self._svc.calls, call)


class _Users:
    def __init__(self, svc):
        self._svc = svc

    def messages(self):
        return _Messages(self._svc)


class FakeGmailService:
    """Mimics service.users().messages().list/get(...).execute().

    pages:    query -> list of page dicts ({"messages": [{"id": ...}]}).
              pageTokens are generated automatically.
    messages: message id -> full message dict (or an Exception to raise).
    calls:    log of (kind, ..., num_retries) tuples for assertions.
    """

    def __init__(self, pages=None, messages=None):
        self.pages = pages or {}
        self.messages = messages or {}
        self.calls: list[tuple] = []

    def users(self):
        return _Users(self)


def ids(*names: str) -> dict:
    """Build a list-page dict from message ids."""
    return {"messages": [{"id": n, "threadId": n} for n in names]}
