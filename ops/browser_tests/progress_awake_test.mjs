// File:         progress_awake_test.mjs
// Purpose:      Proves, in a real Chrome, two promises the owner asked for (2026-10-15), against the listener's OWN local copy fed realistic traffic:
//                 1. Every one of the six players ALWAYS shows a progress bar, on the dashboard and on the topology map. A player that reports
//                    when its file started and how long it is gets a live bar ("m:ss / h:mm:ss") that keeps moving between frames. A player that does
//                    not (yet) report them still shows an empty bar that says so; a red player shows "Off air"; a lost feed dims the bar.
//                 2. The installed app / open tab stays alive: it asks the browser to keep the screen on, asks again whenever the browser takes
//                    that away or the page returns from the background, can be switched off deliberately (and remembers that), says plainly when a
//                    browser cannot do it, and re-opens its own live stream when the connection died for good (a non-200 answer is not retried by
//                    the browser itself).
// Inputs:       argv[2] = output folder (work files and screenshots).
// Outputs:      One JSON result on stdout (every check true/false); exit code 1 if any check fails.
// Dependencies: playwright-core (installed beside this file), local Chrome, python + uvicorn + the listener source.
import { chromium } from 'playwright-core';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const out = process.argv[2];
const PORT = Number(process.env.TEST_PORT || 8436);
const BASE = `http://127.0.0.1:${PORT}`;
const LISTENER = process.env.LISTENER_DIR || 'C:/ops/broadcast-watchdog/core-listener';
const DB = path.join(out, 'listener.db');
const checks = {};
fs.mkdirSync(out, { recursive: true });
// a database left in the output folder by an earlier run would carry its alarms into this one (counters, 'since last critical') and fail checks that are about THIS run
fs.rmSync(DB, { force: true });

function startServer() {
  return spawn('python', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(PORT)], {
    cwd: LISTENER,
    env: { ...process.env, WATCHDOG_SHARED_TOKEN: 'test-token', WATCHDOG_DB_PATH: DB,
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
async function killServer(child) {
  await new Promise(resolve => { const k = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' }); k.on('exit', resolve); });
  await new Promise(r => setTimeout(r, 800));
}
const headers = { 'X-Watchdog-Token': 'test-token', 'Content-Type': 'application/json' };
const post = (route, body) => fetch(BASE + route, { method: 'POST', headers, body: JSON.stringify(body) }).catch(() => null);
const isoAgo = s => new Date(Date.now() - s * 1000).toISOString();
const instance = (feed, down = false, nowPlaying, quiet = 0) => ({ feed, launcher_present: true, engine_present: !down, advance_alarm_confirmed: true, advancing: !down && !quiet,
  advance_age_seconds: quiet, ...(quiet ? { pause_quiet: true } : {}), advance_confirmed_seconds: 300, engine_uptime_seconds: 9000, engine_cpu_cores: 1.2, now_playing: nowPlaying });
let mainUsaDown = false;
let caribbeanEstimated = false;     // MAIN's Caribbean channel in FileBlock hours: its sender works the item out from Insta's day schedule and marks it estimated
let site_aLong = false;            // SITE-A playing a 75 minute documentary with its start labelled in local clock digits (4 h early): the 2026-10-17 case
let usaPauseStart = null;           // set while MAIN-USA is "paused": its quiet age then grows by itself, like the real sender's
const quietAge = () => (usaPauseStart ? 15 + (Date.now() - usaPauseStart) / 1000 : 0);
// every item has ONE start for its whole life, as on the real machines (a start that moved with each beat would look like a new item each time)
const START = { mainUsa: isoAgo(300), backupUsa: isoAgo(900), site_b: isoAgo(1600), caribbean: isoAgo(600) };
async function beat() {
  // The shapes the real senders now send (the unpublished design notes): MAIN and BACKUP USA report start + length inside now_playing, the Caribbean
  // players and SITE-A report a file name only (their senders are not updated yet), SITE-B reports everything at the top of the heartbeat.
  await post('/heartbeat', { hostname: 'MAIN', expected_interval_seconds: 10, instance_count: 2, activity_monitor: { version: 1 }, timestamp: isoAgo(0),
    instances: [instance('usa', mainUsaDown, { filename: 'DUWEW NIV WUT. 4.mp4', result: 'Started 20:06:27', duration: '00:26:42', duration_s: 1602, started_utc: START.mainUsa, next: 'WABINI KEMUMASID PABID.mp4' }, quietAge()),
      instance('caribbean', false, caribbeanEstimated ? { filename: "DARIMA'G DEKUWAL CEDU 2.mp4", result: 'Block schedule (estimated)', duration_s: 4235.2, started_utc: START.caribbean, estimated: true } : { filename: 'SOCAV RICUP.mp4', result: 'Playing' })] });
  await post('/heartbeat', { hostname: 'BACKUP-SERVER', expected_interval_seconds: 10, instance_count: 2, activity_monitor: { version: 1 }, timestamp: isoAgo(0),
    instances: [instance('usa', false, { filename: 'Likuvume Zat Luzemoliv 119.mp4', result: 'Started 09:00:06', duration_s: 7192.6, started_utc: START.backupUsa }),
      instance('caribbean', false, { filename: 'CEPE LEM (HUC HAMIBAMAV).mp4', result: 'Started 09:00:01' })] });
  await post('/heartbeat', { hostname: 'SITE-A', expected_interval_seconds: 30, status: 'decoding', current_item: 'CEPE LEM (HUC HAMIBAMAV).mp4', timestamp: isoAgo(0),
    ...(site_aLong ? { current_item: 'Documentry .mp4', current_item_started_utc: isoAgo(4 * 3600 + 940), current_item_duration_s: 4502.4 } : {}) });
  await post('/heartbeat', { hostname: 'SITE-B', expected_interval_seconds: 30, current_item: 'Likuvume Zat Luzemoliv 119.mp4', timestamp: isoAgo(0), player_running: true,
    current_item_started_utc: START.site_b, current_item_duration_s: 7192, next_item: 'Station.mp4' });
}
const text = (scope, selector) => scope.locator(selector).first().innerText({ timeout: 1500 }).catch(() => null);
async function waitFor(fn, ms = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (await fn()) return true; await new Promise(r => setTimeout(r, 250)); }
  return false;
}

// A fake Screen Wake Lock: Chrome's real one refuses in a headless, unattended run. It records every request, hands out a sentinel the test can
// "release" the way a browser does when the battery saver kicks in, and can be removed to test a browser that has none.
const FAKE_WAKE_LOCK = `
  (function () {
    var log = { requests: 0, releases: 0, active: [] };
    window.__wake = log;
    Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: {
      request: function (type) {
        log.requests += 1;
        var listeners = [];
        var sentinel = { type: type, released: false,
          addEventListener: function (name, fn) { if (name === 'release') { listeners.push(fn); } },
          release: function () { if (!sentinel.released) { sentinel.released = true; log.releases += 1; listeners.forEach(function (f) { f(); }); } return Promise.resolve(); } };
        log.active.push(sentinel);
        return Promise.resolve(sentinel);
      } } });
    // let the test pretend the page is in the background and comes back
    window.__hidden = false;
    Object.defineProperty(document, 'hidden', { configurable: true, get: function () { return window.__hidden; } });
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: function () { return window.__hidden ? 'hidden' : 'visible'; } });
  })();`;
const NO_WAKE_LOCK = `Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: undefined });`;

let server = startServer();
if (!(await waitUp())) { console.log(JSON.stringify({ error: 'local server did not start' })); process.exit(1); }
await beat();
const feeder = setInterval(() => beat(), 5000);

// =========================== Phase 0: plain http becomes https, https carries HSTS ===========================
const viaCloudflare = scheme => ({ 'CF-Visitor': JSON.stringify({ scheme }) });
const plainVisit = await fetch(BASE + '/topology?x=1', { headers: viaCloudflare('http'), redirect: 'manual' });
checks.http_page_visit_is_redirected = plainVisit.status === 301 && plainVisit.headers.get('location') === 'https://watchdog.corp.example/topology?x=1';
const secureVisit = await fetch(BASE + '/offline.html', { headers: viaCloudflare('https') });
checks.https_visit_is_served_with_hsts = secureVisit.status === 200 && secureVisit.headers.get('strict-transport-security') === 'max-age=15552000';
const lanVisit = await fetch(BASE + '/offline.html');
checks.a_request_not_through_cloudflare_is_untouched = lanVisit.status === 200 && lanVisit.headers.get('strict-transport-security') === null;
const postedOverHttp = await fetch(BASE + '/heartbeat', { method: 'POST', headers: { ...headers, ...viaCloudflare('http') }, redirect: 'manual',
  body: JSON.stringify({ hostname: 'PROBE', expected_interval_seconds: 30, current_item: 'probe.mp4' }) });
checks.a_heartbeat_posted_over_http_is_not_redirected = postedOverHttp.status === 200;
const statusAfter = await (await fetch(BASE + '/status', { headers })).json();
checks.status_names_the_source_that_posted_over_plain_http = !!statusAfter.plain_http_senders && Object.keys(statusAfter.plain_http_senders).includes('PROBE');
checks.a_direct_lan_style_http_sender_is_listed_too = Object.keys(statusAfter.plain_http_senders || {}).includes('MAIN');   // the test feeds MAIN straight to the listener over http, like the real LAN senders
await fetch(BASE + '/heartbeat', { method: 'POST', headers: { ...headers, ...viaCloudflare('https') }, body: JSON.stringify({ hostname: 'SECURE', expected_interval_seconds: 30, current_item: 's.mp4' }) });
checks.a_sender_that_came_through_cloudflare_over_https_is_not_listed = !Object.keys((await (await fetch(BASE + '/status', { headers })).json()).plain_http_senders || {}).includes('SECURE');
// The key a visitor can read out of the public page must only READ. If it could post, anyone opening the dashboard could forge a heartbeat.
const pageHtml = await (await fetch(BASE + '/', { headers: viaCloudflare('https') })).text();
const embedded = (/var token = "([^"]+)"/.exec(pageHtml) || [])[1] || '';
const viewerHeaders = { 'X-Watchdog-Token': embedded, 'Content-Type': 'application/json' };
checks.the_page_does_not_carry_the_sender_key = embedded.length === 64 && embedded !== 'test-token' && !pageHtml.includes('test-token');
checks.the_page_key_cannot_post_a_heartbeat = (await fetch(BASE + '/heartbeat', { method: 'POST', headers: viewerHeaders, body: JSON.stringify({ hostname: 'FORGED', current_item: 'x.mp4' }) })).status === 401;
checks.the_page_key_cannot_post_an_event = (await fetch(BASE + '/event', { method: 'POST', headers: viewerHeaders, body: JSON.stringify({ hostname: 'FORGED', event_type: 'off_air', severity: 'critical' }) })).status === 401;
checks.the_page_key_cannot_reset_alarms = (await fetch(BASE + '/alerts/reset', { method: 'POST', headers: viewerHeaders, body: '{}' })).status === 401;
checks.the_page_key_can_read_status = (await fetch(BASE + '/status', { headers: { 'X-Watchdog-Token': embedded } })).status === 200;
checks.the_sender_key_can_still_read_status = (await fetch(BASE + '/status', { headers })).status === 200;
checks.nothing_forged_got_in = !(await (await fetch(BASE + '/status', { headers })).json()).sources.some(row => row.source_id === 'FORGED');
const viewerStreamController = new AbortController();
const viewerStream = await fetch(BASE + '/stream?token=' + embedded, { signal: viewerStreamController.signal });
checks.the_page_key_opens_the_live_stream = viewerStream.status === 200;
viewerStreamController.abort();
checks.a_wrong_key_is_refused_everywhere = (await fetch(BASE + '/stream?token=wrong')).status === 401 && (await fetch(BASE + '/status', { headers: { 'X-Watchdog-Token': 'wrong' } })).status === 401;
const streamController = new AbortController();
const stream = await fetch(BASE + '/stream?token=test-token', { headers: viaCloudflare('https'), signal: streamController.signal });
const firstChunk = await Promise.race([stream.body.getReader().read().then(r => r.value && r.value.length > 0), new Promise(r => setTimeout(() => r(false), 9000))]);
streamController.abort();
checks.the_live_stream_still_flows_through_the_policy = stream.status === 200 && firstChunk === true;
const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });

// =========================== Phase 1: the bar on all six players (dashboard) ===========================
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block' });
await context.addInitScript(FAKE_WAKE_LOCK);
const page = await context.newPage();
const pageErrors = [];
page.on('pageerror', e => pageErrors.push(String(e)));
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
const labels = ['SITE-A', 'SITE-B', 'MAIN-USA', 'MAIN-CARIBBEAN', 'BACKUP-USA', 'BACKUP-CARIBBEAN'];
const tile = label => page.locator('.endpoint-tile', { hasText: label }).first();
const visible = async scope => { const b = await scope.boundingBox().catch(() => null); return !!b && b.width > 20 && b.height > 2; };

checks.dashboard_has_six_tiles = (await page.locator('.endpoint-tile').count()) === 6;
// Lagos time (WAT) beside Core's: read off Core's clock, written "WAT", ticking
const lagosHm = ms => new Intl.DateTimeFormat('en-US', { hour: '2-digit', minute: '2-digit', timeZone: 'Africa/Lagos' }).format(new Date(ms));
checks.lagos_chip_shows_a_time_with_wat = await waitFor(async () => /^\d\d:\d\d:\d\d (AM|PM) WAT$/.test((await text(page, '#clock-lagos')) || ''));
const lagosShown = ((await text(page, '#clock-lagos')) || '').replace(/:\d\d (AM|PM) WAT$/, ' $1').replace(/^(\d\d:\d\d):\d\d/, '$1');
const lagosPm = ((await text(page, '#clock-lagos')) || '').match(/(AM|PM)/);
const lagosHhmm = ((await text(page, '#clock-lagos')) || '').slice(0, 5) + ' ' + (lagosPm ? lagosPm[1] : '');
checks.lagos_time_is_lagos_time = [Date.now(), Date.now() - 60000].some(ms => lagosHm(ms) === lagosHhmm);
checks.core_clock_heading_says_new_york = ((await text(page, '#clock-core-label')) || '').trim().toLowerCase() === 'new york';
checks.incident_feed_heading_says_new_york_time = /new york time/i.test((await text(page, '.incident-heading')) || '');
checks.lagos_label_is_lagos = ((await text(page, '#clock-lagos-label')) || '').trim().toLowerCase() === 'lagos';
const lagosBefore = await text(page, '#clock-lagos');
await page.waitForTimeout(2300);
checks.lagos_clock_ticks = lagosBefore !== (await text(page, '#clock-lagos'));
for (const label of labels) {
  checks['bar_is_drawn_on_' + label] = await waitFor(async () => visible(tile(label).locator('.now-progress')));
}
// the players that report start + length: a live bar with a position
for (const label of ['SITE-B', 'MAIN-USA', 'BACKUP-USA']) {
  checks['live_bar_on_' + label] = await waitFor(async () => (await tile(label).locator('.now-progress.is-live').count()) === 1);
  checks['position_text_on_' + label] = /\d+:\d\d \/ \d+:\d\d/.test((await text(tile(label), '.now-time')) || '');
}
checks.site_b_position_is_about_26_minutes_of_2_hours = /^2[67]:\d\d \/ 1:59:52$/.test((await text(tile('SITE-B'), '.now-time')) || '');
checks.main_usa_position_is_about_5_minutes_of_26_42 = /^[45]:\d\d \/ 26:42$/.test((await text(tile('MAIN-USA'), '.now-time')) || '');
checks.backup_usa_position_is_about_15_minutes = /^1[45]:\d\d \/ 1:59:53$/.test((await text(tile('BACKUP-USA'), '.now-time')) || '') || /^1[45]:\d\d \/ 1:59:52$/.test((await text(tile('BACKUP-USA'), '.now-time')) || '');
const fillOf = label => tile(label).locator('.now-bar i').evaluate(node => parseFloat(node.style.width));
checks.live_bar_is_orange_to_yellow = await tile('SITE-B').locator('.now-bar i').evaluate(node => { const bg = getComputedStyle(node).backgroundImage; return bg.includes('255, 138, 0') && bg.includes('255, 196, 0'); });
checks.live_bar_is_thick_enough_to_see = (await tile('SITE-B').locator('.now-bar').evaluate(node => node.getBoundingClientRect().height)) >= 6;
checks.site_b_bar_fill_is_about_22_percent = Math.abs((await fillOf('SITE-B')) - 22.2) < 2;
checks.main_usa_bar_fill_is_about_19_percent = Math.abs((await fillOf('MAIN-USA')) - 18.7) < 3;
// the live bar keeps moving between the 5-second frames, on this device's own timer
const before = await text(tile('SITE-B'), '.now-time');
await page.waitForTimeout(2300);
const after = await text(tile('SITE-B'), '.now-time');
const toSeconds = t => { const m = /^(\d+):(\d\d) \//.exec(t || ''); return m ? Number(m[1]) * 60 + Number(m[2]) : null; };
checks.live_bar_keeps_moving_between_frames = toSeconds(after) !== null && toSeconds(before) !== null && toSeconds(after) >= toSeconds(before) + 2;
// the players that report a name only: an empty bar that says why, never nothing
for (const label of ['SITE-A', 'MAIN-CARIBBEAN', 'BACKUP-CARIBBEAN']) {
  checks['unknown_bar_on_' + label] = await waitFor(async () => (await tile(label).locator('.now-progress.is-unknown').count()) === 1);
  checks['unknown_bar_says_length_not_reported_on_' + label] = /length not reported/i.test((await text(tile(label), '.now-time')) || '');
  checks['unknown_bar_is_empty_on_' + label] = (await fillOf(label)) === 0;
}
// MAIN's Caribbean channel in FileBlock hours: the item is worked out from the day schedule and shown as an estimate, never as a measured position
caribbeanEstimated = true;
await beat();
checks.an_estimated_item_is_marked_on_its_bar = await waitFor(async () => (await tile('MAIN-CARIBBEAN').locator('.now-progress.is-estimated').count()) === 1, 12000);
checks.an_estimated_position_starts_with_a_tilde = /^~\d+:\d\d \/ 1:10:3[45] est\.$/.test((await text(tile('MAIN-CARIBBEAN'), '.now-time')) || '');
checks.an_estimated_bar_explains_itself_on_hover = /block schedule/i.test((await tile('MAIN-CARIBBEAN').locator('.now-time').getAttribute('title')) || '');
checks.the_estimated_item_name_is_shown = /DARIMA'G DEKUWAL CEDU 2/.test((await text(tile('MAIN-CARIBBEAN'), '.now-title')) || '');
caribbeanEstimated = false;
await beat();
checks.the_estimate_goes_away_when_the_sender_stops_sending_it = await waitFor(async () => (await tile('MAIN-CARIBBEAN').locator('.now-progress.is-estimated').count()) === 0 && (await tile('MAIN-CARIBBEAN').locator('.now-progress.is-unknown').count()) === 1, 12000);
checks.bar_is_not_exposed_as_a_broken_progressbar = (await page.locator('.endpoint-tile .now-bar[role="progressbar"]').count()) === 0;   // decorative; the text beside it carries the meaning

// SITE-A on a 75 minute item whose start is four hours early (local clock digits labelled Z): longer than the 55 minutes the plain whole-hour fix
// can decide alone, so the listener uses the shift it assumes for SITE-A until a short item shows the real one. The bar must be live, not "Start time looks wrong".
site_aLong = true;
await beat();
checks.site_a_long_item_with_a_mislabelled_start_gets_a_live_bar = await waitFor(async () => (await tile('SITE-A').locator('.now-progress.is-live').count()) === 1, 12000);
checks.site_a_long_item_position_is_about_15_minutes_of_1_15_02 = /^1[56]:\d\d \/ 1:15:0[23]$/.test((await text(tile('SITE-A'), '.now-time')) || '');
checks.site_a_long_item_does_not_say_the_start_looks_wrong = !/start time looks wrong/i.test((await text(tile('SITE-A'), '.now-time')) || '');
site_aLong = false;
await beat();
checks.site_a_back_to_a_name_only_item_shows_the_empty_bar_again = await waitFor(async () => (await tile('SITE-A').locator('.now-progress.is-unknown').count()) === 1, 12000);

// a pause: the bar stands still while it lasts and says so, and after the resume the time spent paused stays off the position
const pos = label => text(tile(label), '.now-time').then(t => { const m = /(\d+):(\d\d) \//.exec(t || ''); return m ? Number(m[1]) * 60 + Number(m[2]) : null; });
const beforePause = await pos('MAIN-USA');
const beforeAt = Date.now();
usaPauseStart = Date.now();
await beat();
checks.a_paused_player_shows_a_paused_bar = await waitFor(async () => (await tile('MAIN-USA').locator('.now-progress.is-paused').count()) === 1, 12000);
checks.the_paused_bar_says_paused = /^Paused \d+:\d\d \/ 26:42$/.test((await text(tile('MAIN-USA'), '.now-time')) || '');
const frozenA = await pos('MAIN-USA');
await page.waitForTimeout(7000);          // across at least one more heartbeat, with the quiet age growing
const frozenB = await pos('MAIN-USA');
checks.the_paused_bar_stands_still = frozenA !== null && frozenB !== null && Math.abs(frozenB - frozenA) <= 1;
usaPauseStart = null;
await beat();
checks.bar_goes_live_again_after_the_resume = await waitFor(async () => (await tile('MAIN-USA').locator('.now-progress.is-live').count()) === 1, 12000);
await page.waitForTimeout(1200);
const afterResume = await pos('MAIN-USA');
const wall = (Date.now() - beforeAt) / 1000;
checks.the_pause_stays_off_the_position = afterResume !== null && beforePause !== null && afterResume <= beforePause + wall - 10 && afterResume >= beforePause - 25;

// a red player is off air: the bar stays, empty, and says so
mainUsaDown = true;
await beat();
checks.red_player_bar_says_off_air = await waitFor(async () => (await tile('MAIN-USA').locator('.now-progress.is-off').count()) === 1 && /off air/i.test((await text(tile('MAIN-USA'), '.now-time')) || ''), 12000);
checks.red_player_bar_is_empty = (await fillOf('MAIN-USA')) === 0;
mainUsaDown = false;
await beat();
checks.bar_comes_back_when_the_player_does = await waitFor(async () => (await tile('MAIN-USA').locator('.now-progress.is-live').count()) === 1, 14000);
await page.screenshot({ path: path.join(out, 'dashboard_bars.png') });

// =========================== Phase 2: the bar on the topology map ===========================
await page.goto(BASE + '/topology', { waitUntil: 'domcontentloaded' });
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
checks.topology_has_six_nodes = await waitFor(async () => (await page.locator('.holo-node').count()) === 6);
checks.topology_bar_is_drawn_on_every_node = await waitFor(async () => {
  const bars = page.locator('.holo-node .holo-progress');
  if ((await bars.count()) !== 6) { return false; }
  for (let i = 0; i < 6; i += 1) { if (!(await visible(bars.nth(i)))) { return false; } }
  return true;
});
checks.topology_live_bars = await waitFor(async () => (await page.locator('.holo-node .holo-progress.is-live').count()) === 3);
checks.topology_unknown_bars = (await page.locator('.holo-node .holo-progress.is-unknown').count()) === 3;
const topoBefore = await text(page.locator('.holo-node .holo-progress.is-live'), '.now-time');
await page.waitForTimeout(2300);
const topoAfter = await text(page.locator('.holo-node .holo-progress.is-live'), '.now-time');
checks.topology_bar_keeps_moving = topoBefore !== topoAfter && !!topoAfter;
await page.screenshot({ path: path.join(out, 'topology_bars.png') });

// =========================== Phase 2b: the map says On air / Off air exactly as the dashboard does ===========================
const airOnMap = async () => {
  const result = {};
  const cards = page.locator('.holo-node');
  for (let i = 0; i < await cards.count(); i += 1) {
    const name = ((await cards.nth(i).locator('.holo-name').innerText()) || '').trim();
    result[name.toUpperCase()] = ((await cards.nth(i).locator('.holo-air').innerText()) || '').trim().toLowerCase();
  }
  return result;
};
checks.every_map_card_has_an_air_line = await waitFor(async () => (await page.locator('.holo-node .holo-air').count()) === 6);
checks.healthy_map_cards_all_say_on_air = await waitFor(async () => Object.values(await airOnMap()).filter(v => v === 'on air').length === 6);
mainUsaDown = true;
await beat();
checks.red_map_card_says_off_air = await waitFor(async () => (await airOnMap())['MAIN-USA'] === 'off air', 12000);
checks.red_map_card_air_line_is_red_class = (await page.locator('.holo-node .holo-air.is-off').count()) === 1;
checks.the_other_map_cards_still_say_on_air = Object.entries(await airOnMap()).filter(([n, v]) => n !== 'MAIN-USA' && v === 'on air').length === 5;
const mapLabels = await airOnMap();
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await waitFor(async () => /off air/i.test((await text(tile('MAIN-USA'), '.now-label')) || ''), 12000);
const tileLabels = {};
for (const label of labels) { tileLabels[label] = ((await text(tile(label), '.now-label')) || '').trim().toLowerCase(); }
checks.map_mirrors_the_dashboard_for_all_six = labels.every(label => mapLabels[label] === tileLabels[label]);
// the phone layout (narrow screens) carries the same line and bar
await page.goto(BASE + '/topology', { waitUntil: 'domcontentloaded' });
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await page.setViewportSize({ width: 390, height: 844 });
await page.waitForTimeout(800);
checks.phone_layout_has_an_air_line_on_all_six = await waitFor(async () => (await page.locator('.stack-node .holo-air').count()) === 6);
checks.phone_layout_has_a_bar_on_all_six = (await page.locator('.stack-node .holo-progress').count()) === 6;
checks.phone_layout_red_player_says_off_air = (await page.locator('.stack-node.status-critical .holo-air.is-off').innerText().catch(() => '')).trim().toLowerCase() === 'off air';
checks.phone_layout_does_not_scroll_sideways = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
await page.screenshot({ path: path.join(out, 'topology_phone.png'), fullPage: true });
await page.setViewportSize({ width: 1440, height: 900 });
mainUsaDown = false;
await beat();
checks.map_goes_back_to_on_air_when_the_player_returns = await waitFor(async () => (await airOnMap())['MAIN-USA'] === 'on air', 14000);

// the removed wording must not be on screen (the owner, 2026-10-15)
const gone = ['Red remains reserved', 'sends the alarms', 'Updated '];
const dashText = await (async () => { await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' }); await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 }); await page.waitForTimeout(600); return page.evaluate(() => document.body.innerText); })();
checks.removed_statements_are_not_on_the_dashboard = gone.every(g => !dashText.includes(g));
checks.the_healthy_headline_is_still_there = /All six playout endpoints are healthy/.test(dashText);
const mapText = await (async () => { await page.goto(BASE + '/topology', { waitUntil: 'domcontentloaded' }); await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 }); await page.waitForTimeout(600); return page.evaluate(() => document.body.innerText); })();
checks.removed_statements_are_not_on_the_topology = gone.every(g => !mapText.includes(g));
checks.map_nodes_hosted_at_core_say_new_york = (mapText.match(/NODE 0\d \/\/ NEW YORK/gi) || []).length === 4 && !/NODE 0\d \/\/ CORE/i.test(mapText);
checks.dashboard_tiles_hosted_at_core_say_new_york = (dashText.match(/NEW YORK \/ (USA|CARIBBEAN)/gi) || []).length === 4 && !/CORE \/ (USA|CARIBBEAN)/i.test(dashText);
checks.core_card_is_tagged_core_new_york = /core \/\/ new york/i.test(mapText) && !/receives and alarms/i.test(mapText);

// =========================== Phase 3: keeping the screen on ===========================
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
const wake = () => page.evaluate(() => ({ requests: window.__wake.requests, releases: window.__wake.releases, active: window.__wake.active.filter(s => !s.released).length }));
const awakeButton = page.locator('#awake-toggle');
checks.keep_awake_button_exists = (await awakeButton.count()) === 1;
checks.screen_wake_lock_is_requested_by_itself = await waitFor(async () => (await wake()).active === 1);
checks.button_says_on = await waitFor(async () => /keep screen on: on$/i.test((await awakeButton.innerText()) || ''));
checks.button_is_pressed_when_on = (await awakeButton.getAttribute('aria-pressed')) === 'true';
// the browser takes it away (battery saver, another window): the app asks again by itself
await page.evaluate(() => window.__wake.active.forEach(s => s.release()));
checks.wake_lock_is_requested_again_after_the_browser_releases_it = await waitFor(async () => (await wake()).active === 1, 6000);
// the page goes to the background and comes back
await page.evaluate(() => { window.__hidden = true; document.dispatchEvent(new Event('visibilitychange')); });
await page.evaluate(() => window.__wake.active.forEach(s => s.release()));   // browsers drop the lock when a page is hidden
await page.waitForTimeout(1500);
const whileHidden = await wake();
await page.evaluate(() => { window.__hidden = false; document.dispatchEvent(new Event('visibilitychange')); });
checks.no_request_is_made_while_hidden = whileHidden.active === 0;
checks.wake_lock_returns_when_the_page_returns = await waitFor(async () => (await wake()).active === 1, 6000);
// switched off on purpose: stays off, and is remembered
await awakeButton.click();
checks.switching_off_releases_the_lock = await waitFor(async () => (await wake()).active === 0);
checks.button_says_off = /keep screen on: off$/i.test((await awakeButton.innerText()) || '');
checks.off_is_remembered = (await page.evaluate(() => window.localStorage.getItem('watchdog-keep-awake'))) === 'off';
await page.waitForTimeout(2500);
checks.nothing_requests_it_again_while_off = (await wake()).active === 0;
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await page.waitForTimeout(2000);
checks.stays_off_after_a_reload = (await wake()).requests === 0 && /keep screen on: off$/i.test((await awakeButton.innerText()) || '');
await awakeButton.click();
checks.switching_back_on_asks_again = await waitFor(async () => (await wake()).active === 1);
checks.on_is_remembered = (await page.evaluate(() => window.localStorage.getItem('watchdog-keep-awake'))) === 'on';
await page.screenshot({ path: path.join(out, 'toolbar_awake.png') });

// a browser with no Screen Wake Lock: the button says so and does nothing else
const bare = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block' });
await bare.addInitScript(NO_WAKE_LOCK);
const barePage = await bare.newPage();
await barePage.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await barePage.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
checks.unsupported_browser_says_so = /not supported/i.test((await barePage.locator('#awake-toggle').innerText()) || '');
checks.unsupported_button_is_disabled = await barePage.locator('#awake-toggle').isDisabled();
await bare.close();

// =========================== Phase 4: the stream comes back by itself ===========================
// A non-200 answer to the live stream (a proxy error during a restart) makes the browser give up for good. The app must open it again itself.
await page.evaluate(() => { window.localStorage.setItem('watchdog-keep-awake', 'on'); });
await page.route('**/stream*', route => route.fulfill({ status: 502, body: 'bad gateway' }));
await page.evaluate(() => document.getElementById('refresh').click());   // reconnect now, into the 502
checks.page_notices_the_lost_feed = await waitFor(async () => /live feed lost/i.test((await text(page, '#banner')) || '') || (await page.locator('body.is-blind').count()) === 1, 30000);
checks.bar_dims_when_the_feed_is_lost = await waitFor(async () => (await page.locator('.endpoint-tile .now-progress.is-stale').count()) >= 3, 8000);
checks.lagos_chip_says_last_known_when_the_feed_is_lost = /last known/i.test((await text(page, '#clock-lagos-label')) || '');
checks.lost_feed_bar_keeps_its_place_and_says_so = /feed lost/i.test((await text(tile('SITE-B'), '.now-time')) || '');
await page.unroute('**/stream*');
checks.stream_reopens_by_itself_after_a_dead_connection = await waitFor(async () => (await page.locator('body.is-blind').count()) === 0 && (await tile('SITE-B').locator('.now-progress.is-live').count()) === 1, 20000);   // well inside the 45 s start-over fallback: only the dead-stream check can pass this

checks.no_page_errors = pageErrors.length === 0;
clearInterval(feeder);
await browser.close();
await killServer(server);
const failed = Object.entries(checks).filter(([, ok]) => !ok).map(([name]) => name);
console.log(JSON.stringify({ passed: Object.keys(checks).length - failed.length, total: Object.keys(checks).length, failed, pageErrors, checks }, null, 1));
process.exit(failed.length ? 1 : 0);
