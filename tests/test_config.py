"""Baseline tests for cortex_slack_bridge.config."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def test_get_session_id_default(monkeypatch):
    monkeypatch.delenv("CORTEX_SESSION_ID", raising=False)
    from cortex_slack_bridge.config import get_session_id
    assert get_session_id() == "default"


def test_get_session_id_from_env(monkeypatch):
    monkeypatch.setenv("CORTEX_SESSION_ID", "abc123")
    from cortex_slack_bridge.config import get_session_id
    assert get_session_id() == "abc123"


def test_get_session_inbox_default(tmp_path, monkeypatch):
    """'default' session should use the legacy INBOX_FILE."""
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    monkeypatch.setattr("cortex_slack_bridge.config.INBOX_FILE", tmp_path / "inbox.json")
    from cortex_slack_bridge import config
    result = config.get_session_inbox("default")
    assert result == tmp_path / "inbox.json"


def test_get_session_inbox_named(tmp_path, monkeypatch):
    """Named sessions get a session-scoped inbox file."""
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    monkeypatch.setattr("cortex_slack_bridge.config.INBOX_FILE", tmp_path / "inbox.json")
    from cortex_slack_bridge import config
    result = config.get_session_inbox("sess-xyz")
    assert result == tmp_path / "inbox_sess-xyz.json"


def test_set_and_get_active_session(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    monkeypatch.setattr(
        "cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
        tmp_path / "active_session",
    )
    from cortex_slack_bridge import config
    config.set_active_session("my-session")
    assert config.get_active_session() == "my-session"


def test_get_active_session_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
        tmp_path / "active_session",
    )
    from cortex_slack_bridge import config
    assert config.get_active_session() == "default"


def test_get_bot_token_from_env(monkeypatch):
    monkeypatch.setenv("SLACK_BRIDGE_BOT_TOKEN", "xoxb-test-token")
    from cortex_slack_bridge import config
    assert config.get_bot_token() == "xoxb-test-token"


def test_get_app_token_from_env(monkeypatch):
    monkeypatch.setenv("SLACK_BRIDGE_APP_TOKEN", "xapp-test-token")
    from cortex_slack_bridge import config
    assert config.get_app_token() == "xapp-test-token"


def test_get_user_id_from_env(monkeypatch):
    monkeypatch.setenv("SLACK_BRIDGE_USER_ID", "U123TEST")
    from cortex_slack_bridge import config
    assert config.get_user_id() == "U123TEST"


def test_missing_bot_token_raises(monkeypatch):
    monkeypatch.delenv("SLACK_BRIDGE_BOT_TOKEN", raising=False)
    with patch("cortex_slack_bridge.config.keychain_get", return_value=None), \
         patch("cortex_slack_bridge.config._load_file_config", return_value={}):
        from cortex_slack_bridge import config
        try:
            config.get_bot_token()
            assert False, "Expected RuntimeError"
        except RuntimeError as e:
            assert "SLACK_BRIDGE_BOT_TOKEN" in str(e)
