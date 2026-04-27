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


# ---------------------------------------------------------------------------
# Feature 4: thread_ts per-session storage (TDD — must pass after implementation)
# ---------------------------------------------------------------------------

def test_get_last_ts_returns_none_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    from cortex_slack_bridge import config
    result = config.get_last_ts("no-such-session")
    assert result is None


def test_set_and_get_last_ts_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    from cortex_slack_bridge import config
    config.set_last_ts("thread-sess", "1234567890.123456")
    result = config.get_last_ts("thread-sess")
    assert result == "1234567890.123456"


def test_set_last_ts_overwrites_previous(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    from cortex_slack_bridge import config
    config.set_last_ts("overwrite-sess", "111.000")
    config.set_last_ts("overwrite-sess", "222.000")
    assert config.get_last_ts("overwrite-sess") == "222.000"


# ---------------------------------------------------------------------------
# Feature 7: multi-session registry (TDD — must pass after implementation)
# ---------------------------------------------------------------------------

def test_register_and_get_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    from cortex_slack_bridge import config
    config.register_session("sess-a", "Session A")
    config.register_session("sess-b", "Session B")
    sessions = config.get_sessions()
    ids = [s["session_id"] for s in sessions]
    assert "sess-a" in ids
    assert "sess-b" in ids


def test_register_session_stores_label(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    from cortex_slack_bridge import config
    config.register_session("sess-labeled", "My cool session")
    sessions = config.get_sessions()
    match = next((s for s in sessions if s["session_id"] == "sess-labeled"), None)
    assert match is not None
    assert match["label"] == "My cool session"


def test_register_session_idempotent(tmp_path, monkeypatch):
    """Registering the same session twice should not create duplicates."""
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    from cortex_slack_bridge import config
    config.register_session("dupe-sess", "Dupe")
    config.register_session("dupe-sess", "Dupe Updated")
    sessions = config.get_sessions()
    matching = [s for s in sessions if s["session_id"] == "dupe-sess"]
    assert len(matching) == 1
    assert matching[0]["label"] == "Dupe Updated"


def test_get_sessions_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    from cortex_slack_bridge import config
    result = config.get_sessions()
    assert result == []
