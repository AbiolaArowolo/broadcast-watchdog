// File:         offline_diagnosis_test.mjs
// Purpose:      Proves, in a real Chrome on a phone-sized screen, that the offline placeholder tells a dead INTERNET from a dead
//               SERVER. This is the case that was wrong in the field: a phone on Wi-Fi with no uplink was told "your connection
//               is up, but the server did not answer", when the server was fine and its own internet was down.
//               The local test listener is stopped (so every attempt to reach "the server" fails) and the connectivity checks
//               are then made to fail or succeed, one scenario at a time:
//                 A. the checks fail   -> "No internet connection", server shown as Unknown (it cannot be known), never "Your internet works"
//                 B. the checks pass   -> "Cannot reach the Watchdog server", "Your internet works", server shown as Not answering
//                 C. the device is offline -> "This device is offline"
//               It also proves the checks send nothing (no cookies, no referrer, no body), the removed note is gone, the ClarixTech
//               brand is on the page, and that the page returns to the dashboard by itself when the server comes back.
//               Starts and stops its OWN local test listener; never touches production.
// Inputs:       argv[2] = output folder for screenshots.
// Outputs:      One JSON result line on stdout (every check is a true/false), screenshots in the output folder. Exit code 1 if any check fails.
// Dependencies: playwright-core (installed beside this file), local Chrome, python + uvicorn + the listener source.
import { chromium } from 'playwright-core';
import { spawn } from 'node:child_process';
import path from 'node:path';

const out = process.argv[2];
const PORT = 8433;
const BASE = `http://127.0.0.1:${PORT}`;
const LISTENER = process.env.LISTENER_DIR || 'C:/ops/broadcast-watchdog/core-listener';
const PROBE_PATTERN = /gstatic\.com\/generate_204|cloudflare\.com\/cdn-cgi\/trace/;
const checks = {};
const seenProbes = [];

function startServer() {
  return spawn('python', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(PORT)], {
    cwd: LISTENER,
    env: { ...process.env, WATCHDOG_SHARED_TOKEN: 'test-token', WATCHDOG_DB_PATH: path.join(out, 'listener.db'),
      WATCHDOG_EXPECTED_INSTANCES: '{"MAIN":["usa","caribbean"],"BACKUP-SERVER":["usa","caribbean"]}' },
    stdio: 'ignore',
  });
}
async function waitUp(ms = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try { const r = await fetch(BASE + '/offline.html'); if (r.ok) return true; } catch { /* not up yet */ }
    await new Promise(r => setTimeout(r, 300));
  }
  return false;
}
async function beat() {
  const headers = { 'X-Watchdog-Token': 'test-token', 'Content-Type': 'application/json' };
  await fetch(BASE + '/heartbeat', { method: 'POST', headers, body: JSON.stringify({ hostname: 'SITE-A', expected_interval_seconds: 30, status: 'decoding', current_item: 'x.mp4' }) });
}
async function killServer(child) {
  await new Promise(resolve => { const k = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' }); k.on('exit', resolve); });
  await new Promise(r => setTimeout(r, 800));
}
const text = (page, selector) => page.locator(selector).first().textContent().catch(() => null);
// A missing element (an older page without the connection chain) reads as null, so the check fails cleanly instead of crashing the run.
const stateOf = (page, selector) => page.locator(selector).getAttribute('data-state', { timeout: 1500 }).catch(() => null);
async function chain(page) {
  return {
    device: await stateOf(page, '#node-device'),
    internet: await stateOf(page, '#node-internet'),
    server: await stateOf(page, '#node-server'),
    serverText: await text(page, '#state-server'),
  };
}
async function waitForState(page, expected, ms = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if ((await text(page, '#state')) === expected) return true;
    await page.waitForTimeout(250);
  }
  return false;
}

let server = startServer();
if (!(await waitUp())) { console.log(JSON.stringify({ error: 'local server did not start' })); process.exit(1); }
await beat();

const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, serviceWorkers: 'allow' });
const page = await context.newPage();

// record every connectivity check the page makes, whatever the scenario does with it
await context.route(PROBE_PATTERN, async route => {
  const request = route.request();
  seenProbes.push({ url: request.url(), method: request.method(), headers: request.headers(), body: request.postData() });
  await route.fallback();
});

// warm up: load the dashboard once so the service worker installs and caches the offline screen
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 30000 });
await page.evaluate(async () => { await navigator.serviceWorker.ready; });
await page.waitForTimeout(1500);

await killServer(server);   // from here on "the server" cannot answer

// ---- Scenario A: the server is down AND the internet checks fail (the field case) ----
await context.unroute(PROBE_PATTERN);
await context.route(PROBE_PATTERN, async route => {
  const request = route.request();
  seenProbes.push({ url: request.url(), method: request.method(), headers: request.headers(), body: request.postData() });
  await route.abort('internetdisconnected');
});
await page.goto(BASE + '/offline.html', { timeout: 20000 });   // since 2026-10-13 a visit to / opens the saved app shell; the placeholder is reached directly
checks.A_headline_says_no_internet = await waitForState(page, 'No internet connection');
const a = await chain(page);
checks.A_device_ok = a.device === 'ok';
checks.A_internet_fail = a.internet === 'fail';
checks.A_server_unknown_not_blamed = a.server === 'unknown' && a.serverText === 'Unknown';
const aWhy = (await text(page, '#why')) || '';
checks.A_never_claims_the_internet_works = !/Your internet works/i.test(aWhy) && !/connection is up/i.test(aWhy);
checks.A_explains_the_server_cannot_be_checked = /cannot be checked until it is back/.test(aWhy);
await page.screenshot({ path: path.join(out, 'A-no-internet.png') });

// ---- Scenario B: the server is down but the internet checks pass ----
await context.unroute(PROBE_PATTERN);
await context.route(PROBE_PATTERN, async route => {
  const request = route.request();
  seenProbes.push({ url: request.url(), method: request.method(), headers: request.headers(), body: request.postData() });
  await route.fulfill({ status: 204, body: '' });
});
await page.locator('#retry').click();   // "Try now": an immediate attempt
checks.B_headline_says_server_not_answering = await waitForState(page, 'Cannot reach the Watchdog server');
const b = await chain(page);
checks.B_internet_ok = b.internet === 'ok';
checks.B_server_fail = b.server === 'fail' && b.serverText === 'Not answering';
checks.B_says_the_internet_works = /Your internet works/.test((await text(page, '#why')) || '');
await page.screenshot({ path: path.join(out, 'B-server-not-answering.png') });

// ---- Scenario C: the device itself is offline ----
await context.setOffline(true);
checks.C_headline_says_device_offline = await waitForState(page, 'This device is offline');
const c = await chain(page);
checks.C_device_fail = c.device === 'fail';
await page.screenshot({ path: path.join(out, 'C-device-offline.png') });
await context.setOffline(false);

// ---- Brand and the removed note ----
const body = (await page.locator('body').textContent()) || '';
checks.removed_note_is_gone = !/Status unknown while this screen is showing/.test(body) && !/support@client.example/.test(body) && !/Alarm emails are sent/.test(body);
checks.brand_is_shown = /CLARIXTECH GROUP/.test(body) && /Resilient systems for broadcast, automation, and field infrastructure\./.test(body);
const link = page.locator('a.brand-link');
const attr = name => link.getAttribute(name, { timeout: 1500 }).catch(() => null);
checks.brand_link_goes_to_clarixtech = (await attr('href')) === 'https://corp.example' && (await attr('rel')) === 'noopener noreferrer' && (await attr('target')) === '_blank';
checks.no_chrome_error_page = (await page.locator('#main-frame-error').count()) === 0;

// ---- The checks themselves send nothing ----
checks.probes_were_made = seenProbes.length >= 2;
checks.probes_hit_only_the_two_public_addresses = seenProbes.every(p => PROBE_PATTERN.test(p.url));
checks.probes_are_plain_gets_without_body = seenProbes.every(p => p.method === 'GET' && !p.body);
checks.probes_carry_no_cookies_or_referrer = seenProbes.every(p => !p.headers['cookie'] && !p.headers['referer']);
checks.probes_never_carry_the_access_key = seenProbes.every(p => !JSON.stringify(p.headers).includes('test-token') && !p.url.includes('test-token'));

// ---- Recovery: the server returns and the page goes back to the dashboard by itself ----
server = startServer();
await waitUp(30000);
await beat();
const t0 = Date.now();
let back = false;
while (Date.now() - t0 < 60000) {
  if (await page.locator('#endpoint-grid').count().catch(() => 0)) { back = true; break; }
  await page.waitForTimeout(500);
}
checks.returns_to_the_dashboard_by_itself = back;

await browser.close();
await killServer(server);
const failed = Object.entries(checks).filter(([, ok]) => !ok).map(([name]) => name);
console.log(JSON.stringify({ passed: Object.keys(checks).length - failed.length, total: Object.keys(checks).length, failed, probesSeen: seenProbes.length, checks }, null, 1));
process.exit(failed.length ? 1 : 0);
