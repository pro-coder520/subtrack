"""Tests for subtrack.config."""

import os
from pathlib import Path
from unittest import mock

from subtrack import config


class TestPaths:
    def test_data_dir_is_under_home(self):
        assert config.DATA_DIR == Path.home() / ".subtrack"

    def test_db_path(self):
        assert config.DB_PATH == config.DATA_DIR / "subtrack.db"

    def test_credentials_path(self):
        assert config.CREDENTIALS_PATH == config.DATA_DIR / "credentials.json"

    def test_token_path(self):
        assert config.TOKEN_PATH == config.DATA_DIR / "token.json"


class TestEnvVars:
    def test_get_model_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert config.get_model() == "gemini-3.5-flash-lite"

    def test_get_model_override(self):
        with mock.patch.dict(os.environ, {"SUBTRACK_MODEL": "gemini-3.8-flash"}):
            assert config.get_model() == "gemini-3.8-flash"

    def test_get_lookback_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert config.get_lookback() == "18m"

    def test_get_lookback_override(self):
        with mock.patch.dict(os.environ, {"SUBTRACK_LOOKBACK": "6m"}):
            assert config.get_lookback() == "6m"

    def test_get_gemini_api_key_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert config.get_gemini_api_key() is None

    def test_get_gemini_api_key_set(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-123"}):
            assert config.get_gemini_api_key() == "test-key-123"


class TestEnsureDataDir:
    def test_creates_directory(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / ".subtrack"
        monkeypatch.setattr(config, "DATA_DIR", fake_dir)
        config.ensure_data_dir()
        assert fake_dir.is_dir()

    def test_idempotent(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / ".subtrack"
        monkeypatch.setattr(config, "DATA_DIR", fake_dir)
        config.ensure_data_dir()
        config.ensure_data_dir()  # Should not raise
        assert fake_dir.is_dir()
