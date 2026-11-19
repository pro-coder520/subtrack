"""Tests for subtrack.gmail_client – all mocked, no network calls."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from subtrack import gmail_client


@pytest.fixture()
def fake_dirs(tmp_path, monkeypatch):
    """Redirect config paths to a temp directory."""
    data_dir = tmp_path / ".subtrack"
    data_dir.mkdir()
    monkeypatch.setattr(gmail_client, "TOKEN_PATH", data_dir / "token.json")
    monkeypatch.setattr(
        gmail_client, "CREDENTIALS_PATH", data_dir / "credentials.json"
    )
    # Also patch ensure_data_dir so it doesn't create ~/.subtrack for real
    monkeypatch.setattr(gmail_client, "ensure_data_dir", lambda: None)
    return data_dir


def _make_creds_stub(valid: bool = True, expired: bool = False,
                     refresh_token: str | None = "rt"):
    """Return a mock Credentials object."""
    creds = mock.MagicMock()
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    creds.to_json.return_value = json.dumps({"token": "fake"})
    return creds


class TestAuthenticate:
    def test_reuses_valid_token(self, fake_dirs):
        """When a valid token file exists, return it without any flow."""
        creds = _make_creds_stub(valid=True)
        with mock.patch.object(
            gmail_client.Credentials,
            "from_authorized_user_file",
            return_value=creds,
        ):
            (fake_dirs / "token.json").write_text("{}")
            result = gmail_client.authenticate()
        assert result is creds

    def test_refreshes_expired_token(self, fake_dirs):
        """When the token is expired with a refresh token, refresh it."""
        creds = _make_creds_stub(valid=False, expired=True, refresh_token="rt")
        # After refresh(), valid becomes True
        def do_refresh(request):
            creds.valid = True

        creds.refresh.side_effect = do_refresh

        with mock.patch.object(
            gmail_client.Credentials,
            "from_authorized_user_file",
            return_value=creds,
        ):
            (fake_dirs / "token.json").write_text("{}")
            result = gmail_client.authenticate()

        creds.refresh.assert_called_once()
        assert result is creds
        # Token should be saved
        assert (fake_dirs / "token.json").read_text() == '{"token": "fake"}'

    def test_refresh_error_triggers_browser_flow(self, fake_dirs):
        """When refresh raises RefreshError, fall back to browser flow."""
        from google.auth.exceptions import RefreshError

        expired_creds = _make_creds_stub(
            valid=False, expired=True, refresh_token="rt"
        )
        expired_creds.refresh.side_effect = RefreshError("token expired")

        new_creds = _make_creds_stub(valid=True)

        mock_flow = mock.MagicMock()
        mock_flow.run_local_server.return_value = new_creds

        with (
            mock.patch.object(
                gmail_client.Credentials,
                "from_authorized_user_file",
                return_value=expired_creds,
            ),
            mock.patch.object(
                gmail_client.InstalledAppFlow,
                "from_client_secrets_file",
                return_value=mock_flow,
            ),
        ):
            (fake_dirs / "token.json").write_text("{}")
            (fake_dirs / "credentials.json").write_text("{}")
            result = gmail_client.authenticate()

        assert result is new_creds
        mock_flow.run_local_server.assert_called_once_with(port=0)

    def test_no_token_runs_browser_flow(self, fake_dirs):
        """First run with no token file → browser consent flow."""
        new_creds = _make_creds_stub(valid=True)
        mock_flow = mock.MagicMock()
        mock_flow.run_local_server.return_value = new_creds

        with mock.patch.object(
            gmail_client.InstalledAppFlow,
            "from_client_secrets_file",
            return_value=mock_flow,
        ):
            (fake_dirs / "credentials.json").write_text("{}")
            result = gmail_client.authenticate()

        assert result is new_creds

    def test_no_credentials_file_exits(self, fake_dirs):
        """If credentials.json is missing, exit with an error."""
        with pytest.raises(SystemExit):
            gmail_client.authenticate()


class TestGetProfile:
    def test_returns_email(self):
        """get_profile returns the emailAddress from the API response."""
        mock_service = mock.MagicMock()
        mock_service.users().getProfile().execute.return_value = {
            "emailAddress": "test@example.com"
        }
        assert gmail_client.get_profile(mock_service) == "test@example.com"
