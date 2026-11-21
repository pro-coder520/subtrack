"""Tests for MIME parsing in subtrack.gmail_client (synthetic fixtures only)."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from subtrack.gmail_client import (
    PLAIN_MIN_CHARS,
    decode_part_body,
    extract_text,
    html_to_text,
    normalize_whitespace,
    parse_message,
    strip_zero_width,
)


def _b64(s: str, enc: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(s.encode(enc)).decode().rstrip("=")


class TestDecodePartBody:
    def test_utf8_without_padding(self):
        assert decode_part_body(_b64("₦4,500"), "utf-8") == "₦4,500"

    def test_url_safe_alphabet(self):
        # bytes chosen so standard base64 would contain '+' and '/'
        raw = bytes([0xFB, 0xFF, 0xBF])
        data = base64.urlsafe_b64encode(raw).decode()
        assert "-" in data or "_" in data
        assert decode_part_body(data, "latin-1") == raw.decode("latin-1")

    def test_missing_charset_defaults_to_utf8(self):
        assert decode_part_body(_b64("€5"), None) == "€5"

    def test_unknown_charset_falls_back_to_utf8(self):
        assert decode_part_body(_b64("€5"), "x-bogus") == "€5"

    def test_bad_bytes_are_replaced_not_raised(self):
        data = base64.urlsafe_b64encode(b"ok \xff\xfe end").decode()
        out = decode_part_body(data, "utf-8")
        assert out.startswith("ok ") and out.endswith(" end")
        assert "\ufffd" in out


class TestCleaning:
    def test_strip_zero_width(self):
        assert strip_zero_width("a\u200bb\u200cc\u200dd\u2060e\ufefff\u00adg\u034fh") == "abcdefgh"

    def test_normalize_whitespace(self):
        text = "  Hello\u00a0\u00a0world \t\r\n\n\n\nLine  two  \n"
        assert normalize_whitespace(text) == "Hello world\n\nLine two"

    def test_html_to_text_drops_script_style_head(self):
        html = (
            "<html><head><title>T</title><style>p{}</style></head>"
            "<body><script>x=1</script><p>Visible</p></body></html>"
        )
        assert html_to_text(html) == "Visible"

    def test_html_to_text_keeps_inline_on_one_line(self):
        assert html_to_text("<p>Total: <b>$9.99</b> today</p>") == "Total: $9.99 today"

    def test_html_to_text_separates_cells_and_blocks(self):
        html = "<table><tr><td>Plan</td><td>Pro</td></tr><tr><td>Total</td><td>$5</td></tr></table>"
        assert html_to_text(html) == "Plan Pro\nTotal $5"

    def test_html_br_becomes_newline(self):
        assert html_to_text("<div>Next billing:<br>1 April</div>") == "Next billing:\n1 April"


class TestExtractText:
    def test_plain_only(self, gmail_message):
        text = extract_text(gmail_message("plain_only")["payload"])
        assert "Plan: Premium Individual" in text
        assert "Amount: USD 10.99" in text
        assert "\r" not in text

    def test_short_plain_uses_html(self, gmail_message):
        text = extract_text(gmail_message("short_plain_with_html")["payload"])
        assert "View this email" not in text
        assert "Hi Test User," in text
        assert "Your membership has renewed." in text
        assert "Total NGN 4,400.00" in text
        assert "Next billing date:\n1 April 2026" in text
        # script, style and head contents are gone
        assert "tracking" not in text
        assert "color:red" not in text
        assert "Receipt" not in text

    def test_long_plain_wins_over_html(self, gmail_message):
        text = extract_text(gmail_message("long_plain_with_html")["payload"])
        assert len(text) >= PLAIN_MIN_CHARS
        assert text.startswith("PLAIN VERSION")
        assert "HTML VERSION" not in text

    def test_nested_parts_and_attachments_ignored(self, gmail_message):
        text = extract_text(gmail_message("nested_with_attachment")["payload"])
        assert "Amount paid $20.00" in text
        assert "ATTACHMENT TEXT" not in text

    def test_latin1_charset(self, gmail_message):
        text = extract_text(gmail_message("latin1")["payload"])
        assert text == "Your Café Club membership costs £4.99 per month."

    def test_cp1252_charset(self, gmail_message):
        text = extract_text(gmail_message("cp1252")["payload"])
        assert text == "We received €9.99 for “Pro” — thank you."

    def test_no_charset(self, gmail_message):
        text = extract_text(gmail_message("no_charset")["payload"])
        assert text == "You paid ₦4,500 for Example TV Compact."

    def test_unknown_charset(self, gmail_message):
        text = extract_text(gmail_message("unknown_charset")["payload"])
        assert text == "Amount due: ₦1,200"

    def test_zero_width_padding_does_not_count_toward_plain_length(self, gmail_message):
        # The raw plain part is >200 chars, but almost all of it is invisible
        # padding. After stripping, it is a stub, so the HTML part is used.
        text = extract_text(gmail_message("zero_width")["payload"])
        assert "ends on 5 March 2026" in text
        assert "$7.99/month" in text
        assert not any(c in text for c in "\u200b\u200c\u200d\u2060\ufeff\u00ad\u034f")

    def test_empty_payload(self):
        assert extract_text({"mimeType": "multipart/mixed", "parts": []}) == ""


class TestParseMessage:
    def test_fields(self, gmail_message):
        email = parse_message(gmail_message("short_plain_with_html"))
        assert email.message_id == "m_short_plain_html"
        assert email.thread_id == "m_short_plain_html"
        assert email.sender == "Streamco <info@mailer.streamco.example>"
        assert email.subject == "Your Streamco membership has renewed"
        assert email.received_at == datetime(2026, 3, 1, 9, 15, tzinfo=timezone.utc)
        assert email.list_unsubscribe is True
        assert "NGN 4,400.00" in email.text

    def test_no_list_unsubscribe(self, gmail_message):
        assert parse_message(gmail_message("plain_only")).list_unsubscribe is False

    def test_subject_zero_width_stripped(self, gmail_message):
        assert parse_message(gmail_message("zero_width")).subject == "Your trial ends soon"

    def test_non_ascii_sender(self, gmail_message):
        assert parse_message(gmail_message("latin1")).sender == "Café Club <club@cafe.example>"

    def test_encoded_word_subject_is_decoded(self, gmail_message):
        msg = gmail_message("plain_only")
        for h in msg["payload"]["headers"]:
            if h["name"] == "Subject":
                h["value"] = "=?UTF-8?B?" + base64.b64encode("Reçu €5".encode()).decode() + "?="
        assert parse_message(msg).subject == "Reçu €5"

    def test_missing_subject_and_from(self):
        msg = {
            "id": "x",
            "internalDate": "0",
            "payload": {"mimeType": "text/plain", "headers": [], "body": {"data": _b64("hi")}},
        }
        email = parse_message(msg)
        assert email.subject == "" and email.sender == ""
        assert email.text == "hi"
