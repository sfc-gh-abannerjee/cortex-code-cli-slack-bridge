#!/usr/bin/env node
/**
 * stealth-daemon.js — Drop-in replacement for browser_daemon with navigator.webdriver patched.
 *
 * Exposes the same Unix socket protocol as browser_daemon so coco-browser can use it
 * transparently. The key difference: context.addInitScript patches navigator.webdriver=false
 * before any page script runs, which passes Cloudflare Turnstile and similar challenges.
 *
 * Required env vars:
 *   CORTEX_DIR               Path to the active cortex installation directory
 *   AGENT_BROWSER_SOCKET_DIR Socket/PID file directory (default: /tmp)
 *
 * Optional env vars:
 *   AGENT_BROWSER_USER_AGENT Custom user agent string
 */
'use strict';

const net  = require('net');
const fs   = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// Locate playwright-core bundled with Cortex
// ---------------------------------------------------------------------------

const cortexDir = process.env.CORTEX_DIR;
if (!cortexDir) {
    process.stderr.write('CORTEX_DIR env var is required\n');
    process.exit(1);
}

const pwCorePath = path.join(cortexDir, 'browser_daemon', 'node_modules', 'playwright-core');
let chromium;
try {
    ({ chromium } = require(pwCorePath));
} catch (e) {
    process.stderr.write(`Failed to load playwright-core from ${pwCorePath}: ${e.message}\n`);
    process.exit(1);
}

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

const socketDir  = process.env.AGENT_BROWSER_SOCKET_DIR || '/tmp';
const socketPath = path.join(socketDir, 'default.sock');
const pidPath    = path.join(socketDir, 'default.pid');

// Write PID immediately so coco-browser's wait loop can detect us
fs.writeFileSync(pidPath, String(process.pid));

// ---------------------------------------------------------------------------
// Browser state
// ---------------------------------------------------------------------------

let browser, context, page;

async function ensureBrowser() {
    if (page) return page;

    const ua = process.env.AGENT_BROWSER_USER_AGENT ||
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

    browser = await chromium.launch({
        headless: false,
        args: [
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--disable-infobars',
            '--disable-dev-shm-usage',
        ],
    });

    context = await browser.newContext({ userAgent: ua });

    // Patch navigator.webdriver BEFORE any page script runs
    await context.addInitScript(() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        // Remove CDP-specific window keys that Cloudflare checks for
        const cdcKeys = Object.getOwnPropertyNames(window).filter(k => k.startsWith('cdc_'));
        cdcKeys.forEach(k => { try { delete window[k]; } catch {} });
    });

    page = await context.newPage();
    return page;
}

// ---------------------------------------------------------------------------
// Command handlers — protocol matches browser_daemon
// ---------------------------------------------------------------------------

async function handleCommand(cmd) {
    const p = await ensureBrowser();

    switch (cmd.action) {
        case 'navigate': {
            await p.goto(cmd.url, {
                waitUntil: cmd.waitUntil || 'domcontentloaded',
                timeout:   cmd.timeout  || 30000,
            });
            return { url: p.url(), title: await p.title() };
        }

        case 'evaluate': {
            // Pass script as argument and use eval() so any expression works
            const result = await p.evaluate(s => { return eval(s); }, cmd.script); // eslint-disable-line no-eval
            return { result };
        }

        case 'screenshot': {
            const dest = cmd.path || path.join(socketDir, 'screenshot.png');
            await p.screenshot({ path: dest, fullPage: cmd.fullPage || false });
            return { path: dest };
        }

        case 'snapshot': {
            const snap = await p.accessibility.snapshot();
            return { snapshot: snap };
        }

        case 'innertext': {
            const sel  = cmd.selector || 'body';
            const text = await p.innerText(sel);
            return { text };
        }

        case 'wait': {
            await p.waitForTimeout(cmd.timeout || 1000);
            return { waited: cmd.timeout || 1000 };
        }

        case 'launch':
            // browser_daemon receives explicit launch commands — we auto-launch, so just ack
            return { status: 'already launched' };

        default:
            throw new Error(`Unknown action: ${cmd.action}`);
    }
}

// ---------------------------------------------------------------------------
// Unix socket server
// ---------------------------------------------------------------------------

// Remove stale socket file from a previous run
try { fs.unlinkSync(socketPath); } catch {}

const server = net.createServer(socket => {
    let buf = '';

    socket.on('data', data => {
        buf += data.toString();
        while (true) {
            const idx = buf.indexOf('\n');
            if (idx === -1) break;
            const line = buf.substring(0, idx).trim();
            buf  = buf.substring(idx + 1);
            if (!line) continue;

            let cmd;
            try { cmd = JSON.parse(line); } catch {
                socket.write(JSON.stringify({ id: '?', error: 'Bad JSON' }) + '\n');
                continue;
            }

            handleCommand(cmd)
                .then(data   => socket.write(JSON.stringify({ id: cmd.id, success: true, data })   + '\n'))
                .catch(err   => socket.write(JSON.stringify({ id: cmd.id, error: err.message })     + '\n'));
        }
    });

    socket.on('error', () => {});
});

server.listen(socketPath, () => {
    try { fs.chmodSync(socketPath, 0o600); } catch {}
    process.stderr.write(`Stealth daemon listening on ${socketPath} (PID ${process.pid})\n`);
});

server.on('error', err => {
    process.stderr.write(`Server error: ${err.message}\n`);
    process.exit(1);
});

// ---------------------------------------------------------------------------
// Graceful shutdown
// ---------------------------------------------------------------------------

async function shutdown() {
    if (browser) { try { await browser.close(); } catch {} }
    try { fs.unlinkSync(socketPath); } catch {}
    try { fs.unlinkSync(pidPath);    } catch {}
    process.exit(0);
}

process.on('SIGTERM', shutdown);
process.on('SIGINT',  shutdown);
