"""Slack Socket Mode bridge — sidecar bot for Cortex Code.

Listens for DMs and interactive button clicks, writes responses to inbox.json
so Cortex Code can pick them up via cron polling.

Usage:
    coco-bridge start          # via shell wrapper
    python -m cortex_slack_bridge.bridge   # direct
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from cortex_slack_bridge.config import (
    HISTORY_FILE,
    LOG_FILE,
    PID_FILE,
    ensure_dirs,
    get_active_session,
    get_app_token,
    get_bot_token,
    get_session_inbox,
    get_user_id,
    set_last_ts,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("cortex-slack-bridge")

# ---------------------------------------------------------------------------
# Inbox helpers — simple JSON append with no external deps
# ---------------------------------------------------------------------------

def _read_inbox(session_id: str | None = None) -> list[dict]:
    """Read the current inbox entries for a session."""
    inbox = get_session_inbox(session_id or get_active_session())
    if not inbox.exists():
        return []
    try:
        with open(inbox) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _log_history(entry: dict, direction: str):
    """Append a JSONL line to the audit history. Never raises."""
    try:
        record = {**entry, "direction": direction, "logged_at": time.time()}
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # history logging must never break core functionality


def _append_inbox(entry: dict, session_id: str | None = None):
    """Append a message to the session's inbox (atomic-ish via temp file)."""
    ensure_dirs()
    sid = session_id or get_active_session()
    inbox = get_session_inbox(sid)
    entries = _read_inbox(sid)
    entries.append(entry)
    tmp = inbox.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    tmp.replace(inbox)
    log.info("Wrote inbox entry: %s -> session %s", entry.get("type", "unknown"), sid)
    _log_history(entry, "inbound")


# ---------------------------------------------------------------------------
# /coco-status slash command helper
# ---------------------------------------------------------------------------

def _build_status_response() -> str:
    """Build the text payload for the /coco-status slash command response."""
    sid = get_active_session()

    pid = "unknown"
    if PID_FILE.exists():
        try:
            pid = PID_FILE.read_text().strip()
        except OSError:
            pass

    uptime = ""
    if PID_FILE.exists():
        try:
            elapsed = int(time.time() - PID_FILE.stat().st_mtime)
            h, rem = divmod(elapsed, 3600)
            m, s = divmod(rem, 60)
            uptime = f" (up {h}h {m}m {s}s)"
        except OSError:
            pass

    return (
        f"*Cortex Code Slack Bridge*\n"
        f"• PID: `{pid}`{uptime}\n"
        f"• Active session: `{sid}`\n"
        f"• Inbox: `~/.cortex-slack-bridge/inbox_{sid}.json`"
    )


# ---------------------------------------------------------------------------
# Slack App setup
# ---------------------------------------------------------------------------

def create_app() -> App:
    """Create and configure the Slack Bolt app."""
    app = App(token=get_bot_token())
    target_user = get_user_id()
    _seen_ts: set[str] = set()  # dedup Socket Mode duplicate deliveries

    # --- DM listener -----------------------------------------------------------
    @app.event("message")
    def handle_dm(event, client):
        """Capture DMs from the target user and write to inbox."""
        # Only process messages from our user (ignore bot's own messages)
        user = event.get("user")
        subtype = event.get("subtype")
        if subtype or user != target_user:
            return

        text = event.get("text", "")
        ts = event.get("ts", "")
        channel = event.get("channel", "")

        # Deduplicate: Socket Mode can deliver the same event twice
        if ts in _seen_ts:
            return
        _seen_ts.add(ts)

        log.info("DM received from %s: %s", user, text[:80])

        sid = get_active_session()
        _append_inbox({
            "type": "reply",
            "text": text,
            "user": user,
            "ts": ts,
            "received_at": time.time(),
        })

        # Persist ts so coco-notify --thread can reply under this message
        if ts:
            set_last_ts(sid, ts)

        # Use client.chat_postMessage directly — say() triggers Bolt's assistant
        # context store for thread replies and throws KeyError: 'channel_id'
        if channel:
            try:
                client.chat_postMessage(
                    channel=channel,
                    text="Message sent to CoCo CLI. Awaiting response... please wait :dash_board:",
                )
            except Exception as e:
                log.warning("Failed to send ack message: %s", e)

    # --- Button action handlers ------------------------------------------------
    @app.action("confirm_approve")
    def handle_approve(ack, body, client):
        """Handle Approve button click."""
        ack()
        user = body.get("user", {}).get("id", "")
        if user != target_user:
            log.warning("Approve ignored — unauthorized user %s", user)
            return
        action_id = _extract_confirmation_id(body)
        session_id = _extract_session_id(body, client)
        log.info("Approve clicked by %s for confirmation %s (session %s)", user, action_id, session_id)

        _append_inbox({
            "type": "confirmation",
            "confirmation_id": action_id,
            "response": "approved",
            "user": user,
            "received_at": time.time(),
        }, session_id=session_id)

        # Update the original message to show the result
        _update_confirmation_message(client, body, "Approved ✓")

    @app.action("confirm_deny")
    def handle_deny(ack, body, client):
        """Handle Deny button click."""
        ack()
        user = body.get("user", {}).get("id", "")
        if user != target_user:
            log.warning("Deny ignored — unauthorized user %s", user)
            return
        action_id = _extract_confirmation_id(body)
        session_id = _extract_session_id(body, client)
        log.info("Deny clicked by %s for confirmation %s (session %s)", user, action_id, session_id)

        _append_inbox({
            "type": "confirmation",
            "confirmation_id": action_id,
            "response": "denied",
            "user": user,
            "received_at": time.time(),
        }, session_id=session_id)

        _update_confirmation_message(client, body, "Denied ✗")

    # --- /coco-status slash command -------------------------------------------
    @app.command("/coco-status")
    def handle_status(ack, body, client):
        """Respond to /coco-status with bridge health info."""
        user = body.get("user_id", "")
        if user != target_user:
            ack(text="Unauthorized.")
            return
        ack(text=_build_status_response())

    return app


def _extract_confirmation_id(body: dict) -> str:
    """Pull the confirmation_id from the button's block_id."""
    actions = body.get("actions", [])
    if actions:
        # block_id is set to "confirm_{id}" in notify.py
        block_id = actions[0].get("block_id", "")
        if block_id.startswith("confirm_"):
            return block_id[len("confirm_"):]
    return "unknown"


def _extract_session_id(body: dict, client) -> str | None:
    """Extract the session_id from the original message's metadata.

    Slack includes metadata on the message when sent via chat_postMessage
    with the metadata parameter. For button actions, the original message
    is in body["message"].
    """
    message = body.get("message", {})
    metadata = message.get("metadata", {})
    if metadata.get("event_type") == "cortex_bridge":
        payload = metadata.get("event_payload", {})
        sid = payload.get("session_id")
        if sid:
            return sid
    return None  # falls back to active_session in _append_inbox


def _update_confirmation_message(client, body: dict, result_text: str):
    """Replace the confirmation buttons with a result summary."""
    channel = body.get("channel", {}).get("id", "")
    ts = body.get("message", {}).get("ts", "")
    original_text = body.get("message", {}).get("text", "Confirmation")

    if channel and ts:
        try:
            client.chat_update(
                channel=channel,
                ts=ts,
                text=f"{original_text}\n\n*{result_text}*",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{original_text}\n\n*{result_text}*",
                        },
                    }
                ],
            )
        except Exception as e:
            log.warning("Failed to update confirmation message: %s", e)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    """Start the Socket Mode bridge."""
    ensure_dirs()

    # Write PID for the shell wrapper's stop command
    PID_FILE.write_text(str(__import__("os").getpid()))

    # Add file handler now that dirs exist
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)

    log.info("Starting Cortex Code Slack Bridge (Socket Mode)...")
    log.info("Active session: %s", get_active_session())
    log.info("PID:   %s", PID_FILE.read_text().strip())

    app = create_app()
    handler = SocketModeHandler(app, get_app_token())

    try:
        handler.start()  # blocks until interrupted
    except KeyboardInterrupt:
        log.info("Shutting down bridge.")
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()


if __name__ == "__main__":
    main()
