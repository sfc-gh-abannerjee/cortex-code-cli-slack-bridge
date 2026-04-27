"""Baseline tests for cortex_slack_bridge.bridge helpers."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _extract_confirmation_id
# ---------------------------------------------------------------------------

def test_extract_confirmation_id_standard():
    from cortex_slack_bridge.bridge import _extract_confirmation_id
    body = {"actions": [{"block_id": "confirm_deploy-42"}]}
    assert _extract_confirmation_id(body) == "deploy-42"


def test_extract_confirmation_id_no_actions():
    from cortex_slack_bridge.bridge import _extract_confirmation_id
    assert _extract_confirmation_id({}) == "unknown"


def test_extract_confirmation_id_non_confirm_block():
    from cortex_slack_bridge.bridge import _extract_confirmation_id
    body = {"actions": [{"block_id": "some_other_block"}]}
    assert _extract_confirmation_id(body) == "unknown"


# ---------------------------------------------------------------------------
# _extract_session_id
# ---------------------------------------------------------------------------

def test_extract_session_id_from_metadata():
    from cortex_slack_bridge.bridge import _extract_session_id
    body = {
        "message": {
            "metadata": {
                "event_type": "cortex_bridge",
                "event_payload": {"session_id": "sess-abc"},
            }
        }
    }
    result = _extract_session_id(body, MagicMock())
    assert result == "sess-abc"


def test_extract_session_id_missing_metadata():
    from cortex_slack_bridge.bridge import _extract_session_id
    body = {"message": {}}
    result = _extract_session_id(body, MagicMock())
    assert result is None


def test_extract_session_id_wrong_event_type():
    from cortex_slack_bridge.bridge import _extract_session_id
    body = {
        "message": {
            "metadata": {
                "event_type": "other_event",
                "event_payload": {"session_id": "sess-xyz"},
            }
        }
    }
    result = _extract_session_id(body, MagicMock())
    assert result is None


# ---------------------------------------------------------------------------
# _append_inbox / _read_inbox (bridge-level)
# ---------------------------------------------------------------------------

def test_append_and_read_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                        tmp_path / "active_session")
    monkeypatch.setattr("cortex_slack_bridge.config.INBOX_FILE", tmp_path / "inbox.json")
    monkeypatch.setattr("cortex_slack_bridge.config.HISTORY_FILE", tmp_path / "history.jsonl")

    from cortex_slack_bridge.bridge import _append_inbox, _read_inbox

    entry = {"type": "reply", "text": "hello from Slack", "user": "U123"}
    _append_inbox(entry, session_id="test-sess")

    result = _read_inbox("test-sess")
    assert len(result) == 1
    assert result[0]["text"] == "hello from Slack"


def test_append_inbox_multiple_entries(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                        tmp_path / "active_session")
    monkeypatch.setattr("cortex_slack_bridge.config.INBOX_FILE", tmp_path / "inbox.json")
    monkeypatch.setattr("cortex_slack_bridge.config.HISTORY_FILE", tmp_path / "history.jsonl")

    from cortex_slack_bridge.bridge import _append_inbox, _read_inbox

    _append_inbox({"type": "reply", "text": "first"}, session_id="multi-sess")
    _append_inbox({"type": "reply", "text": "second"}, session_id="multi-sess")

    result = _read_inbox("multi-sess")
    assert len(result) == 2
    assert result[1]["text"] == "second"


# ---------------------------------------------------------------------------
# User filter guard — create_app registers handler that ignores non-target users
# ---------------------------------------------------------------------------

def test_create_app_requires_tokens(monkeypatch):
    """create_app() should fail gracefully when no tokens configured."""
    monkeypatch.delenv("SLACK_BRIDGE_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BRIDGE_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BRIDGE_USER_ID", raising=False)
    with patch("cortex_slack_bridge.config.keychain_get", return_value=None), \
         patch("cortex_slack_bridge.config._load_file_config", return_value={}):
        from cortex_slack_bridge import bridge as bridge_mod
        with pytest.raises(RuntimeError):
            bridge_mod.create_app()
