// File:         standby_strip_test.mjs
// Purpose:      Proves, in a real Chrome, the strip that tells a viewer which copy of the dashboard they are looking at (standby spec 3.1), using the listener's OWN local
//               copy as a real STANDBY watching a FAKE Core whose health page this test switches off and on:
//                 1. standby, Core answering: the strip is visible, blue, and says the page is a quiet second copy that sends no alerts.
//                 2. Core stops answering: the strip turns red and says the standby is alerting (it follows the live stream, no reload).
//                 3. Core answers again: back to blue.
//                 4. The topology view shows the same strip.
//                 5. A listener with no role (Core itself) shows no strip at all, and the word STANDBY appears nowhere on its page.
// Inputs:       argv[2] = output folder (work files and screenshots).
// Outputs:      One JSON result on stdout (every check true/false); exit code 1 if any check fails.
// Dependencies: playwright-core (installed beside this file), local Chrome, python + uvicorn + the listener source.
import { chromium } from 'playwright-core';
import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const out = process.argv[2];
const PORT = Number(process.env.TEST_PORT || 8441);
const LISTENER = process.env.LISTENER_DIR || 'C:/ops/broadcast-watchdog/core-listener';
const KEY = 'strip-test-shared-key-0123456789abcdef';
const SENDER_KEY = 'strip-test-main-key-0000000000000000';
const checks = {};
fs.mkdirSync(out, { recursive: true });

let coreMode = 'ok';
const core = http.createServer((req, res) => {
  if (coreMode === 'ok') { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('{"status":"ok","database":"ok","uptime_s":1}'); }
  else { res.writeHead(503, { 'Content-Type': 'application/json' }); res.end('{"status":"degraded"}'); }
});
await new Promise(resolve => core.listen(0, '127.0.0.1', resolve));
const corePort = core.address().port;

function startServer(port, name, extra) {
  const db = path.join(out, `${name}.db`);
  fs.rmSync(db, { force: true });                                  // a database left by an earlier run would carry its alarms into this one
  return spawn('python', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port), '--no-proxy-headers'], {
    cwd: LISTENER,
    env: { ...process.env, WATCHDOG_SHARED_TOKEN: KEY, WATCHDOG_DB_PATH: db, WATCHDOG_LISTENER_STARTUP_GRACE_SECONDS: '600',
      WATCHDOG_EXPECTED_INSTANCES: '{"MAIN": ["usa", "caribbean"], "BACKUP-SERVER": ["usa", "caribbean"]}', ...extra },
    stdio: 'ignore',
  });
}
async function waitUp(base, ms = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try { const r = await fetch(base + '/offline.html'); if (r.ok) return true; } catch { /* not up yet */ }
    await new Promise(r => setTimeout(r, 300));
  }
  return false;
}
async function killServer(child) {      // by process id only: never by image name (that would also close the user's own browser)
  await new Promise(resolve => { const k = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' }); k.on('exit', resolve); });
}
async function waitFor(fn, ms) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (await fn()) return true; await new Promise(r => setTimeout(r, 400)); }
  return false;
}
const stripState = (page) => page.evaluate(() => {
  const el = document.getElementById('standby-strip');
  return el ? { hidden: el.hidden, height: el.offsetHeight, text: el.textContent, cls: el.className, children: el.children.length } : null;
});

const standbyBase = `http://127.0.0.1:${PORT}`;
const plainBase = `http://127.0.0.1:${PORT + 1}`;
const standby = startServer(PORT, 'standby', {
  WATCHDOG_ROLE: 'standby', WATCHDOG_PRIMARY_URL: `http://127.0.0.1:${corePort}/health`, WATCHDOG_PRIMARY_PROBE_SECONDS: '5', WATCHDOG_PRIMARY_PROBE_TIMEOUT_SECONDS: '2',
  WATCHDOG_STANDBY_FAILURES_TO_DOWN: '1', WATCHDOG_STANDBY_SUCCESSES_TO_UP: '1', WATCHDOG_STANDBY_MIN_DOWN_SECONDS: '0',
  WATCHDOG_SENDER_AUTH: 'per-sender', WATCHDOG_SENDER_KEY_MAIN: SENDER_KEY,
});
const plain = startServer(PORT + 1, 'plain', {});
checks.both_listeners_started = (await waitUp(standbyBase)) && (await waitUp(plainBase));

const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
await page.goto(standbyBase + '/', { waitUntil: 'domcontentloaded' });
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await page.evaluate(() => { window.__survived = 11; });                              // a reload would wipe this

// 1. Core answering
checks.standby_strip_shows_while_core_answers = await waitFor(async () => { const s = await stripState(page); return s && !s.hidden && s.height > 0 && /Core is answering/.test(s.text); }, 20000);
let s = await stripState(page);
checks.strip_is_blue_and_says_no_alerts = !!s && /standby-up/.test(s.cls) && /sends no alerts/.test(s.text);
checks.strip_holds_text_only = !!s && s.children === 0;
await page.screenshot({ path: path.join(out, 'standby-answering.png') });

// 2. Core stops answering
coreMode = 'down';
checks.strip_turns_red_when_core_stops = await waitFor(async () => { const t = await stripState(page); return t && /standby-down/.test(t.cls) && /STANDBY is alerting/.test(t.text); }, 40000);
s = await stripState(page);
checks.strip_names_the_takeover = !!s && /stopped answering/.test(s.text) && /Studio alerts now come from this copy/.test(s.text);
await page.screenshot({ path: path.join(out, 'standby-alerting.png') });
checks.page_was_not_reloaded_by_the_change = (await page.evaluate(() => window.__survived)) === 11;

// 3. Core answers again
coreMode = 'ok';
checks.strip_returns_to_blue = await waitFor(async () => { const t = await stripState(page); return t && /standby-up/.test(t.cls) && /Core is answering/.test(t.text); }, 40000);

// 4. the topology view
const topo = await context.newPage();
await topo.goto(standbyBase + '/topology', { waitUntil: 'domcontentloaded' });
await topo.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
checks.topology_view_shows_the_strip_too = await waitFor(async () => { const t = await stripState(topo); return t && !t.hidden && t.height > 0 && /STANDBY/.test(t.text); }, 20000);
await topo.screenshot({ path: path.join(out, 'standby-topology.png') });

// 5. Core itself (no role)
const plainPage = await context.newPage();
await plainPage.goto(plainBase + '/', { waitUntil: 'domcontentloaded' });
await plainPage.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await new Promise(r => setTimeout(r, 7000));                                         // past at least one stream frame
const p = await stripState(plainPage);
checks.no_role_means_no_strip = !!p && p.hidden === true && p.height === 0;
checks.the_word_standby_is_not_visible_on_cores_page = !(await plainPage.evaluate(() => /STANDBY/.test(document.body.innerText)));
await plainPage.screenshot({ path: path.join(out, 'plain-no-strip.png') });

await browser.close();
await killServer(standby);
await killServer(plain);
core.close();
const failed = Object.entries(checks).filter(([, ok]) => !ok).map(([name]) => name);
console.log(JSON.stringify({ passed: Object.keys(checks).length - failed.length, total: Object.keys(checks).length, failed, checks }, null, 2));
process.exit(failed.length ? 1 : 0);
