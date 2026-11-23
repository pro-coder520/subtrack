"""Tests for `sync --dry-run` (fake Gmail service, temp DB, no network)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from unittest import mock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from subtrack import cli, config, db
from subtrack.gmail_client import build_queries, parse_message
from subtrack.models import Email
from tests.conftest import FakeGmailService, ids, load_fixture

LOOKBACK = "18m"
Q = build_queries(LOOKBACK)


def _service(id_order, extra_pages=None, broken=None):
    """Fake service: Q[0] returns `id_order`, Q[1] repeats the first id."""
    names = {
        "m_plain_only": "plain_only",
        "m_short_plain_html": "short_plain_with_html",
        "m_latin1": "latin1",
        "m_promo_discount": "promo_discount",
        "m_bank_local": "bank_alert_local",
        "m_bank_online": "bank_alert_online",
    }
    messages = {mid: load_fixture(names[mid]) for mid in id_order if mid in names}
    for mid in broken or []:
        messages[mid] = HttpError(httplib2.Response({"status": 404}), b"Not Found")
    pages = {Q[0]: [ids(*id_order)], Q[1]: [ids(id_order[0])]}
    pages.update(extra_pages or {})
    return FakeGmailService(pages=pages, messages=messages)


def _run(service, processed=frozenset(), limit=None, sleep=None):
    out, err = io.StringIO(), io.StringIO()
    sleeps: list[float] = []
    scored = cli.run_dry_sync(
        service,
        set(processed),
        LOOKBACK,
        limit,
        out=out,
        err=err,
        sleep=sleep if sleep is not None else sleeps.append,
    )
    return scored, out.getvalue(), err.getvalue(), sleeps


class TestRunDrySync:
    def test_lists_keep_and_skip_with_reasons(self):
        svc = _service(["m_plain_only", "m_promo_discount", "m_bank_local"])
        scored, out, _, _ = _run(svc)
        assert len(scored) == 3
        assert "--- KEPT (1) ---" in out
        assert "--- SKIPPED (2) ---" in out
        assert "KEEP" in out
        assert "Example Music <billing@music.example.com>" in out
        assert "Your Example Music receipt" in out
        assert "SKIP" in out
        assert "marketing:" in out
        assert "alert_non_online(-3)" in out
        assert "3 candidates checked: 1 kept, 2 skipped. Dry run: nothing was saved." in out

    def test_shows_hits_per_query_and_dedupes(self):
        svc = _service(["m_plain_only", "m_latin1"])
        _, out, _, _ = _run(svc)
        assert f"    2  {Q[0]}" in out
        assert f"    1  {Q[1]}" in out
        assert f"    0  {Q[2]}" in out
        assert "2 unique messages, 0 already processed, fetching 2." in out

    def test_never_prints_body_text(self):
        svc = _service(["m_plain_only"])
        _, out, err, _ = _run(svc)
        body = parse_message(load_fixture("plain_only")).text
        for line in body.splitlines():
            if line.strip():
                assert line not in out and line not in err

    def test_skips_already_processed(self):
        svc = _service(["m_plain_only", "m_latin1"])
        scored, out, _, _ = _run(svc, processed={"m_plain_only"})
        assert [e.message_id for e, _ in scored] == ["m_latin1"]
        assert "1 already processed, fetching 1." in out
        fetched = [c[1] for c in svc.calls if c[0] == "get"]
        assert fetched == ["m_latin1"]

    def test_limit(self):
        svc = _service(["m_plain_only", "m_latin1", "m_short_plain_html"])
        scored, out, _, _ = _run(svc, limit=2)
        assert [e.message_id for e, _ in scored] == ["m_plain_only", "m_latin1"]
        assert "fetching 2." in out
        assert len([c for c in svc.calls if c[0] == "get"]) == 2

    def test_throttles_between_fetches(self):
        svc = _service(["m_plain_only", "m_latin1", "m_short_plain_html"])
        _, _, _, sleeps = _run(svc)
        assert sleeps == [cli.FETCH_DELAY_SECONDS, cli.FETCH_DELAY_SECONDS]

    def test_single_message_does_not_sleep(self):
        svc = _service(["m_plain_only"])
        _, _, _, sleeps = _run(svc)
        assert sleeps == []

    def test_fetch_error_is_reported_and_skipped(self):
        svc = _service(["m_plain_only", "m_gone"], broken=["m_gone"])
        scored, out, err, _ = _run(svc)
        assert [e.message_id for e, _ in scored] == ["m_plain_only"]
        assert "Could not fetch m_gone: HTTP 404" in err
        assert "1 candidates checked: 1 kept, 0 skipped, 1 failed to fetch." in out

    def test_fetch_error_prints_google_reason(self):
        svc = _service(["m_plain_only", "m_throttled"])
        body = json.dumps({
            "error": {
                "code": 403,
                "message": "User-rate limit exceeded.",
                "errors": [{"reason": "userRateLimitExceeded"}],
            }
        }).encode("utf-8")
        svc.messages["m_throttled"] = HttpError(
            httplib2.Response({"status": 403}), body
        )
        scored, _, err, _ = _run(svc)
        assert [e.message_id for e, _ in scored] == ["m_plain_only"]
        assert (
            "Could not fetch m_throttled: HTTP 403 (userRateLimitExceeded): "
            "User-rate limit exceeded."
        ) in err

    def test_format_line_collapses_newlines_and_tabs(self):
        email = Email(
            message_id="m1",
            thread_id="t1",
            sender="Acme\r\n\tBilling <a@acme.example>",
            subject="Line one\nLine\ttwo",
            received_at=datetime(2026, 3, 1, 9, 15, tzinfo=timezone.utc),
            list_unsubscribe=False,
            text="",
        )
        line = cli._format_line(email)
        assert "\n" not in line and "\r" not in line and "\t" not in line
        assert "Acme Billing <a@acme.example>" in line
        assert "Line one Line two" in line

    def test_no_results(self):
        svc = FakeGmailService(pages={})
        scored, out, _, sleeps = _run(svc)
        assert scored == []
        assert sleeps == []
        assert "0 unique messages" in out
        assert "0 candidates checked: 0 kept, 0 skipped." in out


class TestCmdSync:
    def test_without_dry_run_exits_with_message(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["sync"])
        assert exc.value.code == 2
        assert "--dry-run" in capsys.readouterr().err

    @pytest.mark.parametrize("bad", ["0", "-3", "abc"])
    def test_limit_must_be_positive_int(self, bad):
        with pytest.raises(SystemExit):
            cli.main(["sync", "--dry-run", "--limit", bad])

    def test_dry_run_does_not_create_database(self, tmp_path, monkeypatch):
        db_path = tmp_path / ".subtrack" / "subtrack.db"
        monkeypatch.setattr(config, "DB_PATH", db_path)
        svc = _service(["m_plain_only"])
        with (
            mock.patch.object(cli, "authenticate", return_value="creds"),
            mock.patch.object(cli, "get_service", return_value=svc),
        ):
            cli.main(["sync", "--dry-run"])
        assert not db_path.exists()

    def test_dry_run_reads_processed_ids_and_writes_nothing(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "subtrack.db"
        monkeypatch.setattr(config, "DB_PATH", db_path)
        conn = db.connect(db_path)
        db.upsert_email(conn, parse_message(load_fixture("plain_only")), "extracted")
        conn.close()

        svc = _service(["m_plain_only", "m_latin1"])
        with (
            mock.patch.object(cli, "authenticate", return_value="creds"),
            mock.patch.object(cli, "get_service", return_value=svc),
        ):
            cli.main(["sync", "--dry-run", "--limit", "5"])

        out = capsys.readouterr().out
        assert "1 already processed" in out
        assert "Café Club membership" in out
        conn = db.connect(db_path)
        assert conn.execute("SELECT count(*) FROM emails").fetchone()[0] == 1
        conn.close()

    def test_recheck_skipped_fetches_previously_skipped_emails(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "subtrack.db"
        monkeypatch.setattr(config, "DB_PATH", db_path)
        conn = db.connect(db_path)
        db.upsert_email(conn, parse_message(load_fixture("plain_only")), "extracted")
        db.upsert_email(conn, parse_message(load_fixture("promo_discount")), "skipped")
        conn.close()

        svc = _service(["m_plain_only", "m_promo_discount", "m_latin1"])
        with (
            mock.patch.object(cli, "authenticate", return_value="creds"),
            mock.patch.object(cli, "get_service", return_value=svc),
            mock.patch.object(cli.time, "sleep"),
        ):
            cli.main(["sync", "--dry-run", "--recheck-skipped"])

        out = capsys.readouterr().out
        assert "1 already processed, fetching 2." in out
        fetched = [c[1] for c in svc.calls if c[0] == "get"]
        assert fetched == ["m_promo_discount", "m_latin1"]

    def test_uses_lookback_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "DB_PATH", tmp_path / "none.db")
        monkeypatch.setenv("SUBTRACK_LOOKBACK", "3m")
        svc = FakeGmailService(pages={})
        with (
            mock.patch.object(cli, "authenticate", return_value="creds"),
            mock.patch.object(cli, "get_service", return_value=svc),
        ):
            cli.main(["sync", "--dry-run"])
        queries = {c[1] for c in svc.calls if c[0] == "list"}
        assert queries and all(q.startswith("newer_than:3m ") for q in queries)
