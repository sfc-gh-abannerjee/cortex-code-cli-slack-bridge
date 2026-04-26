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
  coco-browser       ← Persistent browser session CLI (navigate/evaluate/text/screenshot chain)
skill/
  SKILL.md           ← Cortex Code skill definition (must be copied to ~/.snowflake/cortex/skills/slack-bridge/)
config.json.example  ← Template for Slack tokens
```

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

## coco-browser CLI Commands

`cortex browser` CLI starts a fresh Playwright MCP server on every invocation (hardcoded in the cortex binary), so page state is never shared between commands. `coco-browser` fixes this by running cortex's built-in `browser_daemon` as a persistent background process and routing all commands to it over a Unix socket.

Data dir: `~/.cortex-slack-bridge/browser/`

| Command | What it does |
|---|---|
| `start` | Start the browser daemon (persists across commands) |
| `stop` | Stop the daemon |
| `status` | Check if daemon is running |
| `navigate <url>` | Navigate to URL |
| `evaluate <js>` | Evaluate JavaScript in page context |
| `text` | Get full visible page text |
| `screenshot [path]` | Save screenshot (default: `~/.cortex-slack-bridge/browser/screenshot.png`) |
| `snapshot` | Get accessibility tree |
| `logs` | Tail the daemon log |

**Usage pattern:**
```bash
coco-browser start
coco-browser navigate "https://example.com"
coco-browser evaluate "document.title"   # sees the navigated page
coco-browser text                         # full page text
coco-browser screenshot /tmp/page.png
coco-browser stop
```

## Slack App

- App name: "Dashs CoCo Remote" (dashlocoforcoco)
- Uses Socket Mode (no public URL needed)
- Messages queue safely during pause -- nothing is lost

## Auth

- SSH remote (id_rsa key for iamontheinet)
- `gh auth login` directly for GitHub CLI (no cortex secret injection)
