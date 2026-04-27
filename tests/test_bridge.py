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


# ---------------------------------------------------------------------------
# Feature 5: /status slash command helper (TDD — must pass after implementation)
# ---------------------------------------------------------------------------

def test_build_status_response_contains_session(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                        tmp_path / "active_session")
    pid_file = tmp_path / "bridge.pid"
    pid_file.write_text("99999")
    monkeypatch.setattr("cortex_slack_bridge.bridge.PID_FILE", pid_file)
    (tmp_path / "active_session").write_text("sess-status-test")

    from cortex_slack_bridge.bridge import _build_status_response
    text = _build_status_response()
    assert "sess-status-test" in text


def test_build_status_response_contains_pid(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                        tmp_path / "active_session")
    pid_file = tmp_path / "bridge.pid"
    pid_file.write_text("54321")
    monkeypatch.setattr("cortex_slack_bridge.bridge.PID_FILE", pid_file)
    (tmp_path / "active_session").write_text("sess-pid-test")

    from cortex_slack_bridge.bridge import _build_status_response
    text = _build_status_response()
    assert "54321" in text


def test_build_status_response_no_pid_file(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                        tmp_path / "active_session")
    monkeypatch.setattr("cortex_slack_bridge.bridge.PID_FILE", tmp_path / "bridge.pid")
    # No PID file — should not raise

    from cortex_slack_bridge.bridge import _build_status_response
    text = _build_status_response()
    assert isinstance(text, str)
    assert len(text) > 0


# ---------------------------------------------------------------------------
# Feature 4: handle_dm deduplication and client.chat_postMessage ack
# ---------------------------------------------------------------------------

def _make_dm_event(ts="1111.0001", user="U_TARGET", text="hello", channel="DM_CHAN"):
    return {"user": user, "ts": ts, "text": text, "channel": channel, "subtype": None}


def test_handle_dm_deduplicates_same_ts(tmp_path, monkeypatch):
    """_seen_ts set in create_app closure deduplicates duplicate Socket Mode deliveries.
    Verify the mechanism: _append_inbox itself does NOT dedup (that's the handler's job).
    """
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)
    monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                        tmp_path / "active_session")
    monkeypatch.setattr("cortex_slack_bridge.config.HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr("cortex_slack_bridge.config.INBOX_FILE", tmp_path / "inbox.json")
    (tmp_path / "active_session").write_text("sess-dedup")

    # _append_inbox itself does NOT dedup — two calls = two entries
    from cortex_slack_bridge.bridge import _append_inbox, _read_inbox
    _append_inbox({"type": "reply", "text": "hello", "user": "U_TARGET",
                   "ts": "1111.0001", "received_at": 0.0}, session_id="sess-dedup")
    _append_inbox({"type": "reply", "text": "hello", "user": "U_TARGET",
                   "ts": "1111.0001", "received_at": 0.0}, session_id="sess-dedup")
    result = _read_inbox("sess-dedup")
    # Two entries written (dedup happens at the handler layer, not here)
    assert len(result) == 2

    # Verify dedup is applied at handler layer by inspecting the source
    import inspect
    from cortex_slack_bridge import bridge as bmod
    src = inspect.getsource(bmod.create_app)
    assert "_seen_ts" in src
    assert "if ts in _seen_ts" in src


def test_handle_dm_uses_client_not_say(tmp_path, monkeypatch):
    """handle_dm must not use say() — it should call client.chat_postMessage."""
    import ast, inspect
    from cortex_slack_bridge import bridge as bmod
    src = inspect.getsource(bmod.create_app)
    # 'say' must not appear as a function argument in handle_dm
    assert "def handle_dm(event, say" not in src
    assert "client.chat_postMessage" in src
