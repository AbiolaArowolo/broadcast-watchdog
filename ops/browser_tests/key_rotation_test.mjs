// File:         key_rotation_test.mjs
// Purpose:      Proves, in a real Chrome, that changing the sender key does not blind a dashboard that is already open (a wall screen, an installed app)
//               and does not drop a sender, using the listener's OWN local copy and three restarts of it:
//                 1. Before: the key is KEY_A, one page is open and live, senders post with KEY_A.
//                 2. The window: the listener is restarted with KEY_B as the sender key and KEY_A kept as the previous key. The open page keeps working
//                    with the view key it already holds, senders on either key are accepted, and /status names which sender is still on the old key.
//                 3. The old key is retired: restarted with KEY_B only. The open page's view key is now refused (the browser closes a refused stream for
//                    good), so the page must fetch its new view key by itself and go live again WITHOUT reloading (a reload would lose the browser's
//                    permission to play the siren). The old sender key and the old view key are refused.
// Inputs:       argv[2] = output folder (work files and screenshots).
// Outputs:      One JSON result on stdout (every check true/false); exit code 1 if any check fails.
// Dependencies: playwright-core (installed beside this file), local Chrome, python + uvicorn + the listener source.
import { chromium } from 'playwright-core';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const out = process.argv[2];
const PORT = Number(process.env.TEST_PORT || 8437);
const BASE = `http://127.0.0.1:${PORT}`;
const LISTENER = process.env.LISTENER_DIR || 'C:/ops/broadcast-watchdog/core-listener';
const DB = path.join(out, 'listener.db');
const KEY_A = 'rotation-test-key-A';
const KEY_B = 'rotation-test-key-B';
const checks = {};
fs.mkdirSync(out, { recursive: true });
// a database left in the output folder by an earlier run would carry its alarms into this one (counters, 'since last critical') and fail checks that are about THIS run
fs.rmSync(DB, { force: true });

function startServer(shared, previous = '') {
  return spawn('python', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(PORT)], {
    cwd: LISTENER,
    env: { ...process.env, WATCHDOG_SHARED_TOKEN: shared, WATCHDOG_PREVIOUS_SHARED_TOKEN: previous, WATCHDOG_DB_PATH: DB,
      WATCHDOG_EXPECTED_INSTANCES: '{"MAIN": ["usa", "caribbean"], "BACKUP-SERVER": ["usa", "caribbean"]}' },
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
async function killServer(child) {      // by process id only: never by image name (that would also close the user's own browser)
  await new Promise(resolve => { const k = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' }); k.on('exit', resolve); });
  await new Promise(r => setTimeout(r, 800));
}
const isoAgo = s => new Date(Date.now() - s * 1000).toISOString();
const post = (route, body, key) => fetch(BASE + route, { method: 'POST', headers: { 'X-Watchdog-Token': key, 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).catch(() => null);
const status = async (key) => { const r = await fetch(BASE + '/status', { headers: { 'X-Watchdog-Token': key } }).catch(() => null); return r ? { code: r.status, json: r.ok ? await r.json() : null } : { code: 0, json: null }; };
const viewKeyOf = async () => {      // exactly what a browser is handed when it loads the page
  const html = await (await fetch(BASE + '/')).text();
  const found = /var token = "([0-9a-f]{64})";/.exec(html);
  return found ? found[1] : null;
};
const heartbeatFor = (name, key, item = 'Likuvume Zat Luzemoliv 119.mp4') => {
  if (name === 'SITE-B') {
    return post('/heartbeat', { hostname: 'SITE-B', expected_interval_seconds: 30, current_item: item, timestamp: isoAgo(0), player_running: true,
      current_item_started_utc: isoAgo(300), current_item_duration_s: 7192, next_item: 'Station.mp4' }, key);
  }
  return post('/heartbeat', { hostname: name, expected_interval_seconds: 10, instance_count: 2, activity_monitor: { version: 1 }, timestamp: isoAgo(0),
    instances: ['usa', 'caribbean'].map(feed => ({ feed, launcher_present: true, engine_present: true, advance_alarm_confirmed: true, advancing: true, advance_age_seconds: 0,
      advance_confirmed_seconds: 300, engine_uptime_seconds: 9000, engine_cpu_cores: 1.2, now_playing: { filename: 'FILE ' + feed + '.mp4', result: 'Playing' } })) }, key);
};
async function waitFor(fn, ms = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (await fn()) return true; await new Promise(r => setTimeout(r, 300)); }
  return false;
}

// ---------- 1. before: KEY_A is the only key ----------
let server = startServer(KEY_A);
checks.listener_up_on_key_a = await waitUp();
const results = await Promise.all(['MAIN', 'BACKUP-SERVER', 'SITE-B'].map(n => heartbeatFor(n, KEY_A)));
checks.senders_accepted_on_key_a = results.every(r => r && r.status === 200);
const viewKeyA = await viewKeyOf();
checks.page_is_given_a_view_key = /^[0-9a-f]{64}$/.test(viewKeyA || '');
checks.view_key_is_not_the_sender_key = viewKeyA !== KEY_A;
let before = await status(viewKeyA);
checks.before_status_reads_with_view_key = before.code === 200;
checks.before_no_window_open = before.json && before.json.key_window_open === false;
checks.before_senders_are_on_the_current_key = !!before.json && ['MAIN', 'BACKUP-SERVER', 'SITE-B'].every(n => before.json.sender_keys.current.includes(n)) && before.json.sender_keys.previous.length === 0;

const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await page.evaluate(() => { window.__survived = 7; });                       // a reload would wipe this
const live = () => page.evaluate(() => /SSE connected/.test(document.body.innerText));
checks.page_is_live_before = await waitFor(live, 15000);

// Keep the senders beating the whole time, like the real machines do (each with the key it currently holds).
let senderKeys = { MAIN: KEY_A, 'BACKUP-SERVER': KEY_A, SITE_B: KEY_A };
let site_bItem = 'Likuvume Zat Luzemoliv 119.mp4';
const beater = setInterval(() => { for (const [name, key] of Object.entries(senderKeys)) { heartbeatFor(name, key, name === 'SITE-B' ? site_bItem : undefined); } }, 2000);

// ---------- 2. the window: KEY_B current, KEY_A previous ----------
await killServer(server);
server = startServer(KEY_B, KEY_A);
checks.listener_up_in_the_window = await waitUp();
const old = await status(viewKeyA);
checks.window_old_view_key_still_reads = old.code === 200;
const oldWrite = await post('/heartbeat', { hostname: 'MAIN' }, viewKeyA);
checks.window_view_key_cannot_write = !!oldWrite && oldWrite.status === 401;
const stranger = await post('/heartbeat', { hostname: 'MAIN' }, 'not-a-key');
checks.window_unknown_key_refused = !!stranger && stranger.status === 401;
checks.window_old_sender_key_still_posts = !!(await heartbeatFor('MAIN', KEY_A)) && (await heartbeatFor('MAIN', KEY_A)).status === 200;
senderKeys['BACKUP-SERVER'] = KEY_B;                                         // BACKUP-SERVER is moved first
await heartbeatFor('BACKUP-SERVER', KEY_B);
const mid = await status(viewKeyA);
checks.window_status_names_who_is_on_the_old_key = !!mid.json && mid.json.key_window_open === true && mid.json.sender_keys.previous.includes('MAIN') && mid.json.sender_keys.current.includes('BACKUP-SERVER');
checks.window_page_is_given_a_different_view_key = (await viewKeyOf()) !== viewKeyA;
checks.window_open_page_stays_live = await waitFor(live, 30000);
checks.window_page_was_not_reloaded = (await page.evaluate(() => window.__survived)) === 7;
senderKeys.MAIN = KEY_B; senderKeys.SITE_B = KEY_B;                        // the rest are moved over; the next beats (every 2 s) carry the new key
await new Promise(r => setTimeout(r, 2600));
const moved = await status(viewKeyA);
checks.window_everyone_seen_on_the_new_key = !!moved.json && moved.json.sender_keys.previous.length === 0 && ['MAIN', 'BACKUP-SERVER', 'SITE-B'].every(n => moved.json.sender_keys.current.includes(n));

// ---------- 3. the old key is retired ----------
await killServer(server);
server = startServer(KEY_B);
checks.listener_up_after_retiring = await waitUp();
const refusedWrite = await post('/heartbeat', { hostname: 'MAIN' }, KEY_A);
checks.retired_old_sender_key_refused = !!refusedWrite && refusedWrite.status === 401;
const refusedRead = await status(viewKeyA);
checks.retired_old_view_key_refused = refusedRead.code === 401;
const newRead = await status(KEY_B);
checks.retired_new_sender_key_works = newRead.code === 200 && newRead.json.key_window_open === false;
const viewKeyB = await viewKeyOf();
checks.retired_new_view_key_works = (await status(viewKeyB)).code === 200;
// The proof that matters: the page that has been open all along holds the OLD view key, is refused, and must get back on its own.
site_bItem = 'ROTATION CHECK AFTER RETIRING.mp4';
const backOnTheFeed = await waitFor(() => page.evaluate(() => document.body.innerText.includes('ROTATION CHECK AFTER RETIRING')), 60000);
checks.retired_open_page_recovers_by_itself = backOnTheFeed;
checks.retired_open_page_shows_live = await waitFor(live, 10000);
checks.retired_page_was_not_reloaded = (await page.evaluate(() => window.__survived)) === 7;
await page.screenshot({ path: path.join(out, 'after-retiring.png') });

clearInterval(beater);
await browser.close();
await killServer(server);
const failed = Object.entries(checks).filter(([, ok]) => !ok).map(([name]) => name);
console.log(JSON.stringify({ passed: Object.keys(checks).length - failed.length, total: Object.keys(checks).length, failed, checks }, null, 2));
process.exit(failed.length ? 1 : 0);
