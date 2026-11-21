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
