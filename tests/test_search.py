"""Tests for search/fetch in subtrack.gmail_client (fake service, no network)."""

from __future__ import annotations

import pytest

from subtrack.gmail_client import (
    BASE_QUERIES,
    NUM_RETRIES,
    build_queries,
    fetch_message,
    search_message_ids,
)
from tests.conftest import FakeGmailService, ids


class TestBuildQueries:
    def test_every_query_gets_lookback(self):
        queries = build_queries("18m")
        assert len(queries) == len(BASE_QUERIES) == 6
        assert all(q.startswith("newer_than:18m ") for q in queries)

    def test_lookback_is_configurable(self):
        assert build_queries("90d")[0] == "newer_than:90d category:purchases"

    def test_no_hardcoded_lookback_left_in_base_queries(self):
        assert not any("newer_than" in q for q in BASE_QUERIES)

    @pytest.mark.parametrize("bad", ["", "18", "m18", "18 m", "18m OR in:anywhere", "1.5y"])
    def test_rejects_invalid_lookback(self, bad):
        with pytest.raises(ValueError):
            build_queries(bad)


class TestSearchMessageIds:
    def test_merges_and_dedupes_in_first_seen_order(self):
        svc = FakeGmailService(pages={
            "q1": [ids("a", "b", "c")],
            "q2": [ids("c", "d", "a")],
            "q3": [ids("e")],
        })
        result, hits = search_message_ids(svc, ["q1", "q2", "q3"])
        assert result == ["a", "b", "c", "d", "e"]
        assert hits == {"q1": 3, "q2": 3, "q3": 1}

    def test_follows_all_pages(self):
        svc = FakeGmailService(pages={"q": [ids("a", "b"), ids("c"), ids("d")]})
        result, hits = search_message_ids(svc, ["q"])
        assert result == ["a", "b", "c", "d"]
        assert hits == {"q": 4}
        tokens = [c[2] for c in svc.calls if c[0] == "list"]
        assert tokens == [None, "1", "2"]

    def test_empty_result_has_no_messages_key(self):
        svc = FakeGmailService(pages={"q": [{"resultSizeEstimate": 0}]})
        assert search_message_ids(svc, ["q"]) == ([], {"q": 0})

    def test_uses_max_page_size_and_retries(self):
        svc = FakeGmailService(pages={"q": [ids("a")]})
        search_message_ids(svc, ["q"])
        kind, q, token, max_results, retries = svc.calls[0]
        assert (kind, q, max_results, retries) == ("list", "q", 500, NUM_RETRIES)


class TestFetchMessage:
    def test_fetches_full_format_with_retries(self, gmail_message):
        msg = gmail_message("plain_only")
        svc = FakeGmailService(messages={"m_plain_only": msg})
        assert fetch_message(svc, "m_plain_only") is msg
        assert svc.calls == [("get", "m_plain_only", "full", NUM_RETRIES)]
