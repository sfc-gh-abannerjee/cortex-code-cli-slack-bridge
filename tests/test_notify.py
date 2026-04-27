"""Baseline tests for cortex_slack_bridge.notify."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from cortex_slack_bridge.notify import (
    MSG_TYPE_COLORS,
    MSG_TYPE_ICONS,
    _pop_confirmation,
    _read_inbox,
    _write_inbox,
)


# ---------------------------------------------------------------------------
# MSG_TYPE_COLORS / ICONS structure
# ---------------------------------------------------------------------------

def test_msg_type_colors_has_required_keys():
    assert set(MSG_TYPE_COLORS.keys()) == {"status", "success", "warning", "error"}


def test_msg_type_colors_are_hex():
    for key, color in MSG_TYPE_COLORS.items():
        assert color.startswith("#"), f"{key} color should be a hex string"
        assert len(color) == 7, f"{key} color should be 7-char hex"


def test_msg_type_icons_matches_colors():
    assert set(MSG_TYPE_ICONS.keys()) == set(MSG_TYPE_COLORS.keys())


# ---------------------------------------------------------------------------
# Inbox helpers
# ---------------------------------------------------------------------------

def test_read_inbox_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr("cortex_slack_bridge.notify.get_session_inbox",
                        lambda sid=None: tmp_path / "inbox_missing.json")
    assert _read_inbox("test-session") == []


def test_write_and_read_inbox(tmp_path, monkeypatch):
    inbox_path = tmp_path / "inbox_test.json"
    monkeypatch.setattr("cortex_slack_bridge.notify.get_session_inbox",
                        lambda sid=None: inbox_path)
    monkeypatch.setattr("cortex_slack_bridge.notify.ensure_dirs", lambda: None)

    entries = [{"type": "reply", "text": "hello"}]
    _write_inbox(entries, "test-session")
    result = _read_inbox("test-session")
    assert result == entries


def test_pop_confirmation_found(tmp_path, monkeypatch):
    inbox_path = tmp_path / "inbox_test.json"
    monkeypatch.setattr("cortex_slack_bridge.notify.get_session_inbox",
                        lambda sid=None: inbox_path)
    monkeypatch.setattr("cortex_slack_bridge.notify.ensure_dirs", lambda: None)

    entries = [
        {"type": "confirmation", "confirmation_id": "dep-1", "response": "approved"},
        {"type": "reply", "text": "hi"},
    ]
    _write_inbox(entries, "s1")
    result = _pop_confirmation("dep-1", session_id="s1")
    assert result is not None
    assert result["response"] == "approved"
    # Should be removed from inbox
    remaining = _read_inbox("s1")
    assert len(remaining) == 1
    assert remaining[0]["type"] == "reply"


def test_pop_confirmation_not_found(tmp_path, monkeypatch):
    inbox_path = tmp_path / "inbox_test.json"
    monkeypatch.setattr("cortex_slack_bridge.notify.get_session_inbox",
                        lambda sid=None: inbox_path)
    monkeypatch.setattr("cortex_slack_bridge.notify.ensure_dirs", lambda: None)

    _write_inbox([], "s2")
    result = _pop_confirmation("nonexistent", session_id="s2")
    assert result is None


# ---------------------------------------------------------------------------
# send_message — basic path (no msg_type)
# ---------------------------------------------------------------------------

def _make_mock_client(channel_id="C123", msg_ts="111.222"):
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": channel_id}}
    client.chat_postMessage.return_value = MagicMock(data={"ts": msg_ts, "ok": True})
    return client


def test_send_message_basic(monkeypatch, tmp_path):
    mock_client = _make_mock_client()
    monkeypatch.setenv("SLACK_BRIDGE_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_BRIDGE_USER_ID", "U123")
    monkeypatch.setenv("CORTEX_SESSION_ID", "sess-basic")
    monkeypatch.setattr("cortex_slack_bridge.notify.HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                        tmp_path / "active_session")
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)

    with patch("cortex_slack_bridge.notify.WebClient", return_value=mock_client):
        from cortex_slack_bridge.notify import send_message
        send_message("Hello world")

    mock_client.chat_postMessage.assert_called_once()
    call_kwargs = mock_client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C123"
    assert "Hello world" in call_kwargs["text"]


def test_send_message_plain_uses_blocks(monkeypatch, tmp_path):
    """Plain messages use Block Kit blocks (no attachment wrapping)."""
    mock_client = _make_mock_client()
    monkeypatch.setenv("SLACK_BRIDGE_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_BRIDGE_USER_ID", "U123")
    monkeypatch.setenv("CORTEX_SESSION_ID", "sess-plain")
    monkeypatch.setattr("cortex_slack_bridge.notify.HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                        tmp_path / "active_session")
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)

    with patch("cortex_slack_bridge.notify.WebClient", return_value=mock_client):
        from cortex_slack_bridge.notify import send_message
        send_message("Plain message")

    call_kwargs = mock_client.chat_postMessage.call_args.kwargs
    assert "blocks" in call_kwargs
    assert call_kwargs["blocks"][0]["type"] == "section"


# ---------------------------------------------------------------------------
# Feature 1: color-coded attachments (TDD — must pass after fix)
# ---------------------------------------------------------------------------

def test_send_message_with_msg_type_uses_color_attachment(monkeypatch, tmp_path):
    """msg_type messages must use attachments with color, not bare blocks."""
    mock_client = _make_mock_client()
    monkeypatch.setenv("SLACK_BRIDGE_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_BRIDGE_USER_ID", "U123")
    monkeypatch.setenv("CORTEX_SESSION_ID", "sess-color")
    monkeypatch.setattr("cortex_slack_bridge.notify.HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                        tmp_path / "active_session")
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)

    with patch("cortex_slack_bridge.notify.WebClient", return_value=mock_client):
        from cortex_slack_bridge.notify import send_message
        send_message("Build done", msg_type="success")

    call_kwargs = mock_client.chat_postMessage.call_args.kwargs
    # Must have attachments (not bare blocks) for the color stripe to appear
    assert "attachments" in call_kwargs, "msg_type must use attachments for color stripe"
    assert len(call_kwargs["attachments"]) == 1
    attachment = call_kwargs["attachments"][0]
    assert attachment["color"] == MSG_TYPE_COLORS["success"]
    assert "blocks" in attachment
    # Top-level blocks should NOT be set (color would be invisible)
    assert "blocks" not in call_kwargs or call_kwargs.get("blocks") is None


def test_send_message_msg_type_color_values(monkeypatch, tmp_path):
    """Each msg_type produces the correct color in the attachment."""
    for msg_type, expected_color in MSG_TYPE_COLORS.items():
        mock_client = _make_mock_client()
        monkeypatch.setenv("SLACK_BRIDGE_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_BRIDGE_USER_ID", "U123")
        monkeypatch.setenv("CORTEX_SESSION_ID", f"sess-{msg_type}")
        monkeypatch.setattr("cortex_slack_bridge.notify.HISTORY_FILE",
                            tmp_path / "history.jsonl")
        monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                            tmp_path / "active_session")
        monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)

        with patch("cortex_slack_bridge.notify.WebClient", return_value=mock_client):
            from cortex_slack_bridge.notify import send_message
            send_message(f"Test {msg_type}", msg_type=msg_type)

        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert call_kwargs["attachments"][0]["color"] == expected_color, \
            f"Wrong color for msg_type={msg_type}"


# ---------------------------------------------------------------------------
# Feature 4: reply threading (TDD — must pass after implementation)
# ---------------------------------------------------------------------------

def test_send_message_with_thread_ts(monkeypatch, tmp_path):
    """When thread_ts is passed, chat_postMessage receives it for threading."""
    mock_client = _make_mock_client()
    monkeypatch.setenv("SLACK_BRIDGE_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_BRIDGE_USER_ID", "U123")
    monkeypatch.setenv("CORTEX_SESSION_ID", "sess-thread")
    monkeypatch.setattr("cortex_slack_bridge.notify.HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                        tmp_path / "active_session")
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)

    with patch("cortex_slack_bridge.notify.WebClient", return_value=mock_client):
        from cortex_slack_bridge.notify import send_message
        send_message("Reply in thread", thread_ts="9876543210.111111")

    call_kwargs = mock_client.chat_postMessage.call_args.kwargs
    assert call_kwargs.get("thread_ts") == "9876543210.111111"


def test_send_message_without_thread_ts_no_thread(monkeypatch, tmp_path):
    """Without thread_ts, chat_postMessage should NOT receive a thread_ts."""
    mock_client = _make_mock_client()
    monkeypatch.setenv("SLACK_BRIDGE_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_BRIDGE_USER_ID", "U123")
    monkeypatch.setenv("CORTEX_SESSION_ID", "sess-nothread")
    monkeypatch.setattr("cortex_slack_bridge.notify.HISTORY_FILE", tmp_path / "history.jsonl")
    monkeypatch.setattr("cortex_slack_bridge.config.ACTIVE_SESSION_FILE",
                        tmp_path / "active_session")
    monkeypatch.setattr("cortex_slack_bridge.config.BRIDGE_DIR", tmp_path)

    with patch("cortex_slack_bridge.notify.WebClient", return_value=mock_client):
        from cortex_slack_bridge.notify import send_message
        send_message("Top-level message")

    call_kwargs = mock_client.chat_postMessage.call_args.kwargs
    assert call_kwargs.get("thread_ts") is None
