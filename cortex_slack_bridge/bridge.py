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
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from cortex_slack_bridge.config import (
    HISTORY_FILE,
    LOG_FILE,
    PID_FILE,
    ensure_dirs,
    get_active_session,
    find_any_tmux_session,
    get_app_token,
    get_bot_token,
    get_session_inbox,
    get_tmux_session,
    get_user_id,
    set_active_session,
    set_last_ts,
    set_tmux_session,
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
# tmux relay — forward Slack DMs into a running tmux+cortex session
# ---------------------------------------------------------------------------

def _relay_to_tmux(tmux_name: str, text: str):
    """Send text to a running tmux session as keyboard input.

    Uses the list form so text is passed directly to tmux without shell
    interpretation (no injection risk). Text and Enter are sent as separate
    calls to avoid race conditions with a freshly initializing terminal.

    Uses DEVNULL for all stdio to avoid creating pipes — pipe creation via
    capture_output=True triggers a fork+pipe setup that can leave the
    WebSocket send buffer in a degraded state on macOS, causing Bolt's
    ack() to silently fail for subsequent slash commands.
    """
    devnull = subprocess.DEVNULL
    try:
        log.info("TMUX_START name=%s text=%r", tmux_name, text[:40])
        check = subprocess.run(
            ["/opt/homebrew/bin/tmux", "has-session", "-t", tmux_name],
            stdin=devnull, stdout=devnull, stderr=devnull,
        )
        if check.returncode == 0:
            subprocess.run(
                ["/opt/homebrew/bin/tmux", "send-keys", "-t", tmux_name, text],
                stdin=devnull, stdout=devnull, stderr=devnull,
            )
            time.sleep(0.5)
            subprocess.run(
                ["/opt/homebrew/bin/tmux", "send-keys", "-t", tmux_name, "Enter"],
                stdin=devnull, stdout=devnull, stderr=devnull,
            )
            log.info("Relayed DM to tmux session %s", tmux_name)
        else:
            log.warning("tmux session %s not found — skipping relay", tmux_name)
    except Exception as e:
        log.warning("tmux relay failed: %s", e)


# ---------------------------------------------------------------------------
# Background relay poller — reads inbox and relays new entries to tmux.
#
# Runs in a dedicated daemon thread every 1 second, independent of Bolt's
# ThreadPoolExecutor. This sidesteps the Bolt-thread-specific issue where
# file I/O silently breaks for single-connection (×1 delivery) messages.
# ---------------------------------------------------------------------------

def _relay_poller(stop_event: threading.Event, relayed_ts: set) -> None:
    """Poll the active session inbox and relay new reply entries to tmux."""
    log.info("Relay poller started")
    while not stop_event.is_set():
        try:
            sid = get_active_session()
            inbox_path = get_session_inbox(sid)
            if inbox_path.exists():
                try:
                    with open(inbox_path) as f:
                        entries = json.load(f)
                    if isinstance(entries, list):
                        for entry in entries:
                            if entry.get("type") != "reply":
                                continue
                            ts = entry.get("ts", "")
                            if not ts or ts in relayed_ts:
                                continue
                            relayed_ts.add(ts)
                            text = entry.get("text", "")
                            reply_thread_ts = entry.get("reply_thread_ts") or ts
                            tmux_name = get_tmux_session(sid) or find_any_tmux_session()
                            if tmux_name:
                                log.info("POLLER relay ts=%s tmux=%s text=%r",
                                         ts[:12], tmux_name, text[:40])
                                # Set last_ts BEFORE relaying so coco-bridge send
                                # threads correctly even if CoCo responds faster
                                # than the relay subprocess (which sleeps 0.5s).
                                set_last_ts(sid, reply_thread_ts)
                                log.info("POLLER set_last_ts=%s", reply_thread_ts[:12])
                                _relay_to_tmux(tmux_name, text)
                            else:
                                log.warning("POLLER no tmux session for sid=%s", sid[:8])
                except (json.JSONDecodeError, OSError) as exc:
                    log.debug("POLLER inbox read error: %s", exc)
        except Exception as exc:
            log.warning("POLLER loop error: %s", exc)
        stop_event.wait(timeout=1.0)
    log.info("Relay poller stopped")




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
    _seen_ts: set[str] = set()
    _seen_ts_lock = threading.Lock()  # guard dedup to prevent concurrent relay

    # Slash command dedup: Socket Mode delivers events on both WebSocket
    # connections simultaneously. Track (user, command) → last_processed_time
    # and skip the second delivery within a 2-second window.
    _seen_slash: dict = {}
    _seen_slash_lock = threading.Lock()

    def _is_duplicate_slash(user_id: str, command: str) -> bool:
        key = (user_id, command)
        now = time.time()
        with _seen_slash_lock:
            if now - _seen_slash.get(key, 0) < 2.0:
                return True
            _seen_slash[key] = now
            return False

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

        # Deduplicate: Socket Mode can deliver the same event twice.
        # Lock makes the check-and-add atomic so two concurrent deliveries
        # of the same ts can't both slip through.
        with _seen_ts_lock:
            if ts in _seen_ts:
                return
            _seen_ts.add(ts)

        log.info("DM received from %s: %s", user, text[:80])

        # For thread replies, store the parent ts so coco-notify --thread
        # replies into the correct Slack thread. For top-level DMs, use the
        # message ts itself. We store this on the inbox entry so set_last_ts
        # can be called from the poller thread (Bolt-thread file I/O is broken
        # for single-connection deliveries).
        reply_thread_ts = event.get("thread_ts") or ts

        _append_inbox({
            "type": "reply",
            "text": text,
            "user": user,
            "ts": ts,
            "reply_thread_ts": reply_thread_ts,
            "received_at": time.time(),
        })

        # Relay and set_last_ts are both handled by _relay_poller (started in
        # main()). Doing either in Bolt's ThreadPoolExecutor causes silent file
        # I/O failures for single-connection deliveries.

        # Use client.chat_postMessage directly — say() triggers Bolt's assistant
        # context store for thread replies and throws KeyError: 'channel_id'.
        # Mirror the user's thread context: if they replied in a thread, ack there.
        if channel:
            try:
                # Brief pause so Slack's servers have committed the parent
                # message before we reference its ts as thread_ts. Without
                # this, chat_postMessage can return ok:true but silently post
                # standalone when called within ~ms of event delivery.
                time.sleep(0.15)
                ack_thread_ts = event.get("thread_ts") or ts
                resp = client.chat_postMessage(
                    channel=channel,
                    thread_ts=ack_thread_ts,
                    text="Message sent to CoCo CLI. Awaiting response... please wait :dash_board:",
                )
                if not resp.get("ok"):
                    log.warning("Ack postMessage not ok: %s ts=%s", resp.get("error"), ack_thread_ts)
            except Exception as e:
                log.warning("Failed to send ack message: %s", e)

    # --- Global error handler — route Bolt exceptions to our FileHandler ------
    @app.error
    def global_error_handler(error, logger):
        """Catch any exception from a listener and log it via our FileHandler."""
        log.error("BOLT LISTENER ERROR: %s", error, exc_info=error)

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
    def handle_status(ack, respond, body):
        """Respond to /coco-status with bridge health info."""
        user = body.get("user_id", "")
        if user != target_user:
            ack(text="Unauthorized.")
            return
        # ack() sends over the WebSocket, which can silently fail when Slack
        # closes the connection mid-flight (TCP send buffer accepts the write
        # but the kernel drops it on the subsequent RST). A zero-width-space
        # body prevents "app did not respond" when the WebSocket IS healthy.
        # respond() sends via HTTP POST to response_url — a fresh HTTPS
        # connection, immune to WebSocket state — so the status always arrives.
        ack(text="\u200b")
        respond(text=_build_status_response())

    # --- /coco-launch slash command -------------------------------------------
    @app.command("/coco-launch")
    def handle_launch(ack, respond, body, client):
        """Launch a new headless Cortex Code session in a tmux window.

        Usage: /coco-launch [path]
        If path is omitted, defaults to the user's home directory.
        """
        ack()  # ack immediately before any work
        if _is_duplicate_slash(body.get("user_id", ""), "/coco-launch"):
            return
        user = body.get("user_id", "")
        if user != target_user:
            respond(text="Unauthorized.")
            return

        raw_path = body.get("text", "").strip() or "~"
        expanded = os.path.expanduser(raw_path)

        if not os.path.isdir(expanded):
            client.chat_postMessage(
                channel=body["channel_id"],
                text=f"Path not found: `{raw_path}`",
            )
            return

        sid = str(uuid.uuid4())
        tmux_name = f"coco-{sid[:8]}"

        try:
            subprocess.run(
                ["/opt/homebrew/bin/tmux", "new-session", "-d", "-s", tmux_name, "-c", expanded],
                check=True,
            )
            subprocess.run(
                ["/opt/homebrew/bin/tmux", "send-keys", "-t", tmux_name,
                 f"CORTEX_SESSION_ID={sid} cortex --bypass", "Enter"],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            log.error("Failed to create tmux session: %s", e)
            client.chat_postMessage(
                channel=body["channel_id"],
                text=f"Failed to launch session: `{e}`",
            )
            return

        set_active_session(sid)
        set_tmux_session(sid, tmux_name)
        log.info("Launched tmux session %s for sid %s in %s", tmux_name, sid, expanded)

        client.chat_postMessage(
            channel=body["channel_id"],
            text=(
                f"*Session launched* in `{raw_path}`\n"
                f"• tmux: `{tmux_name}`\n"
                f"• Resume on Mac: run `cortex resume` and pick this session\n"
                f"Send messages here to interact with Cortex Code."
            ),
        )

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

    # Pre-populate relay_seen with any inbox entries that already exist,
    # so we don't re-relay messages from before this startup.
    relay_seen: set = set()
    try:
        sid = get_active_session()
        existing_inbox = get_session_inbox(sid)
        if existing_inbox.exists():
            with open(existing_inbox) as f:
                existing_entries = json.load(f)
            if isinstance(existing_entries, list):
                for e in existing_entries:
                    t = e.get("ts", "")
                    if t:
                        relay_seen.add(t)
            log.info("Pre-populated relay_seen with %d existing inbox entries", len(relay_seen))
    except Exception as exc:
        log.warning("Could not pre-populate relay_seen: %s", exc)

    relay_stop = threading.Event()
    relay_thread = threading.Thread(
        target=_relay_poller,
        args=(relay_stop, relay_seen),
        name="relay-poller",
        daemon=True,
    )
    relay_thread.start()

    try:
        handler.start()  # blocks until interrupted
    except KeyboardInterrupt:
        log.info("Shutting down bridge.")
    finally:
        relay_stop.set()
        if PID_FILE.exists():
            PID_FILE.unlink()


if __name__ == "__main__":
    main()
