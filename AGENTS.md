# Agent Guidelines — cortex-code-cli-slack-bridge

## What This Repo Is

Bidirectional Slack DM bridge for Cortex Code CLI. Lets you get notifications and steer your AI coding agent from your phone via Slack.

**Org:** iamontheinet (public repo)

## Architecture

```
cortex_slack_bridge/
  bridge.py          ← Socket Mode bot (slack-bolt), multi-session routing
  config.py          ← Token priority: env var > macOS Keychain > config.json
  notify.py          ← Notification helpers
bin/
  coco-bridge        ← CLI entrypoint (send, history, setup-keychain, clear-keychain, pause, resume, stop)
skill/
  SKILL.md           ← Cortex Code skill definition (must be copied to ~/.snowflake/cortex/skills/slack-bridge/)
config.json.example  ← Template for Slack tokens
```

Note: `coco-browser` is NOT in this repo. It lives at `~/.snowflake/cortex/bin/coco-browser`
as a global Cortex Code utility, independent of the Slack bridge.

## Critical Rules

1. **File-based IPC.** The bridge communicates with Cortex Code sessions via inbox files (one per session). Do not replace with sockets, Redis, or any other transport -- the simplicity is intentional.
2. **Polling architecture has two modes -- do not merge them:**
   - **Normal mode:** `*/1` cron, double-read pattern (read, sleep 30s, read again) = ~30s effective latency
   - **Pause mode:** `*/5` cron, single read, only watching for `resume` keywords = ~5min latency
3. **Stop/disable clears the inbox AND deletes the cron entirely.** It does not just pause.
4. **Token storage priority:** env var > macOS Keychain > config.json. The Keychain integration (`keychain_get`/`keychain_set`/`keychain_delete` in config.py) is macOS-specific.
5. **Message history** is a JSONL append-only audit log. Never truncate or rotate it automatically.
6. **Blog file lives in `~/Apps/blogs/slack-bridge.md`**, NOT in this repo. Do not create or commit blog files here.

## coco-bridge CLI Commands

| Command | What it does |
|---|---|
| `send <message>` | Send a message to Slack |
| `history [N]` | Show last N messages from audit log |
| `setup-keychain` | Store Slack tokens in macOS Keychain |
| `clear-keychain` | Remove tokens from Keychain |
| `pause` | Switch to 5-min heartbeat cron |
| `resume` | Restore normal 1-min double-read cron |
| `stop` | Clear inbox + delete cron entirely |

## Slack App

- App name: "Dashs CoCo Remote" (dashlocoforcoco)
- Uses Socket Mode (no public URL needed)
- Messages queue safely during pause -- nothing is lost

## Auth

- SSH remote (id_rsa key for iamontheinet)
- `gh auth login` directly for GitHub CLI (no cortex secret injection)
