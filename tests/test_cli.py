"""Tests for subtrack.cli – Gmail calls are mocked."""

from unittest import mock

import pytest

from subtrack import cli


def test_no_command_prints_help_and_exits(capsys):
    with mock.patch("sys.argv", ["subtrack"]):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_auth_prints_email(capsys):
    with (
        mock.patch("sys.argv", ["subtrack", "auth"]),
        mock.patch.object(cli, "authenticate", return_value="creds") as auth,
        mock.patch.object(cli, "get_service", return_value="svc") as svc,
        mock.patch.object(cli, "get_profile", return_value="me@example.com"),
    ):
        cli.main()
    auth.assert_called_once()
    svc.assert_called_once_with("creds")
    assert capsys.readouterr().out.strip() == "me@example.com"
