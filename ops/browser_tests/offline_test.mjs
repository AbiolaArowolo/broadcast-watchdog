// File:         offline_test.mjs
// Purpose:      End-to-end proof, in a real Chrome on a phone-sized screen, of the loading screen and of what the app does when the server cannot be
//               reached. Since 2026-10-13 the service worker keeps the SHELL of the app (the page itself, which holds no data), so:
//               (1) the loading screen shows real connection steps while the stream is slow, then leaves on the first frame;
//               (2) the worker has saved exactly the offline placeholder and the two page shells, and nothing that could hold status data;
//               (3) with the server stopped, opening / or /topology still OPENS THE APP from the saved shell (not Chrome's error page, not the
//                   offline screen), which then says plainly that there is no live data, instead of a blocking "cannot reach" screen;
//               (4) the offline placeholder is still there for a direct visit;
//               (5) when the server returns, the open app fills in BY ITSELF, with no reload.
//               Starts and stops its OWN local test listener; never touches production.
// Inputs:       argv[2] = output folder for screenshots.
// Outputs:      One JSON result on stdout (every check is true/false); exit code 1 if any check fails.
// Dependencies: playwright-core (installed beside this file), local Chrome, python + uvicorn + the listener source.
import { chromium } from 'playwright-core';
import { spawn } from 'node:child_process';
import path from 'node:path';

const out = process.argv[2];
const PORT = Number(process.env.TEST_PORT || 8431);
const BASE = `http://127.0.0.1:${PORT}`;
const LISTENER = process.env.LISTENER_DIR || 'C:/ops/broadcast-watchdog/core-listener';
const checks = {};

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
  const inst = f => ({ feed: f, launcher_present: true, engine_present: true, advance_alarm_confirmed: true, advancing: true,
    advance_age_seconds: 0, advance_confirmed_seconds: 300, engine_uptime_seconds: 9000, engine_cpu_cores: 1.2 });
  for (const body of [
    { hostname: 'SITE-A', expected_interval_seconds: 30, status: 'decoding' },
    { hostname: 'SITE-B', expected_interval_seconds: 300, current_item: 'PETA HEN WUT 24.mp4' },
    { hostname: 'MAIN', expected_interval_seconds: 10, instance_count: 2, instances: [inst('usa'), inst('caribbean')], activity_monitor: { version: 1 } },
    { hostname: 'BACKUP-SERVER', expected_interval_seconds: 10, instance_count: 2, instances: [inst('usa'), inst('caribbean')], activity_monitor: { version: 1 } },
  ]) await fetch(BASE + '/heartbeat', { method: 'POST', headers, body: JSON.stringify(body) }).catch(() => null);
}
async function killServer(child) {
  await new Promise(resolve => { const k = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' }); k.on('exit', resolve); });
  await new Promise(r => setTimeout(r, 800));
}
async function waitFor(fn, ms = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (await fn()) return true; await new Promise(r => setTimeout(r, 250)); }
  return false;
}
const text = (page, selector) => page.locator(selector).first().innerText({ timeout: 1500 }).catch(() => null);

let server = startServer();
if (!(await waitUp())) { console.log(JSON.stringify({ error: 'local server did not start' })); process.exit(1); }
await beat();

const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, serviceWorkers: 'allow' });
const page = await context.newPage();

// (1) slow stream: the loading screen must show the real steps, then leave
await page.route('**/stream*', async route => { await new Promise(r => setTimeout(r, 4500)); await route.continue(); });
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1200);
checks.loading_screen_shows_a_real_connection_step = /Opening a secure connection/.test((await text(page, '#loader-step')) || '');
checks.loading_screen_is_visible_while_connecting = (await page.locator('#preloader:not(.done)').count()) === 1;
await page.screenshot({ path: path.join(out, '1-loading-screen.png') });
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 30000 });
checks.loading_screen_leaves_on_the_first_frame = true;
checks.loading_screen_ends_on_live = await waitFor(async () => (await text(page, '#loader-step')) === 'Live', 8000);   // read once it had settled: on a busy machine the text lags the class by a moment
// this listener is started with NO zone setting, and this browser is in another zone: Core's clock must come out as New York by default
checks.core_clock_is_new_york_by_default = /E[DS]T/.test((await text(page, '#clock-core')) || '');
await page.unroute('**/stream*');
await page.waitForTimeout(800);
await page.screenshot({ path: path.join(out, '2-dashboard-phone.png') });

// (2) what the service worker has saved
await page.evaluate(async () => { await navigator.serviceWorker.ready; });
await page.waitForTimeout(2000);
const saved = await page.evaluate(async () => {
  const names = await caches.keys();
  const entries = [];
  for (const name of names) { const c = await caches.open(name); for (const k of await c.keys()) entries.push(new URL(k.url).pathname); }
  return { names, entries: entries.sort() };
});
checks.worker_is_active = await page.evaluate(async () => !!(await navigator.serviceWorker.ready).active);
checks.exactly_one_cache_and_it_is_the_current_one = saved.names.length === 1 && saved.names[0] === 'watchdog-shell-v3';
checks.saved_the_placeholder_and_the_two_shells_only = JSON.stringify(saved.entries) === JSON.stringify(['/', '/offline.html', '/topology']);
checks.nothing_that_could_hold_status_data_is_saved = !saved.entries.some(p => /stream|status|heartbeat/.test(p));

// (3) the server is gone: the app still OPENS from the saved shell and says so itself
await killServer(server);
const t0 = Date.now();
let opened = true;
try { await page.goto(BASE + '/', { timeout: 20000 }); } catch (e) { opened = false; }
checks.app_opens_from_the_saved_shell_with_the_server_down = opened && (await page.locator('#banner').count()) === 1;
checks.opened_quickly_not_after_a_long_wait = (Date.now() - t0) < 12000;
checks.not_chromes_error_page = (await page.locator('#main-frame-error').count()) === 0;
checks.not_the_blocking_offline_screen = (await page.locator('#node-server').count()) === 0;
checks.says_no_live_data_by_itself = await waitFor(async () => /Live feed lost/.test((await text(page, '#banner')) || ''), 25000);
checks.explains_that_nothing_has_arrived_yet = /No live data has arrived yet/.test((await text(page, '#banner')) || '');
checks.indicator_says_no_live_data = /no live data/i.test((await text(page, '#live-text')) || '');
checks.the_loading_screen_does_not_trap_the_user = await page.evaluate(() => document.getElementById('preloader').classList.contains('done'));
await page.screenshot({ path: path.join(out, '3-app-from-saved-shell-no-server.png') });
await page.goto(BASE + '/topology', { timeout: 20000 }).catch(() => null);
checks.topology_also_opens_from_its_saved_shell = (await page.locator('#topology-view').count()) === 1;
await page.goto(BASE + '/', { timeout: 20000 }).catch(() => null);

// (4) a direct visit to the placeholder still works
await page.goto(BASE + '/offline.html', { timeout: 20000 }).catch(() => null);
checks.the_placeholder_is_still_there_for_a_direct_visit = (await page.locator('#node-server').count()) === 1;
await page.screenshot({ path: path.join(out, '4-offline-placeholder.png') });
await page.goto(BASE + '/', { timeout: 20000 }).catch(() => null);
await page.waitForSelector('#banner', { timeout: 10000 });

// (5) the server returns: the open app fills in by itself, with no reload
await page.evaluate(() => { window.__sameDocument = true; });
server = startServer();
await waitUp(30000);
await beat();
checks.app_fills_in_by_itself_when_the_server_returns = await waitFor(async () => /All six playout endpoints are healthy|critical alarm|need review/.test((await text(page, '#banner')) || ''), 60000);
checks.it_did_so_without_a_reload = await page.evaluate(() => window.__sameDocument === true);
checks.counters_show_real_numbers_again = await waitFor(async () => (await text(page, '#counter-healthy .counter-value b')) === '6', 10000);
await page.screenshot({ path: path.join(out, '5-recovered-phone.png') });

await browser.close();
await killServer(server);
const failed = Object.entries(checks).filter(([, ok]) => !ok).map(([name]) => name);
console.log(JSON.stringify({ passed: Object.keys(checks).length - failed.length, total: Object.keys(checks).length, failed, saved, checks }, null, 1));
process.exit(failed.length ? 1 : 0);
