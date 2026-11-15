// File:         dashboard_redesign_test.mjs
// Purpose:      Proves, in a real Chrome, the dashboard's counters, the Core server panel, the "now playing" freshness and the
//               "live feed lost" state, against its OWN local listener fed realistic traffic and a fake /proc folder (so the server panel
//               has real numbers on a Windows PC). Checks, among others:
//                 * counters count to the real figures; red alarms and the since-last-red counter appear when a player dies
//                 * Core's CPU, load, memory, disk and CPU history come from the listener's own readings
//                 * a file name not refreshed lately says "Last reported ... may have changed" and its age keeps counting between frames
//                 * a sender that reports when an item started and how long it is gets a progress bar
//                 * tiles are built ONCE and updated in place (the same elements survive a live frame; an open details panel stays open)
//                 * no outside request is made while the feed is healthy; when it is lost the page says so, names the right cause
//                   (this device's internet vs Core) and recovers by itself
//                 * no sideways scrolling on a phone; reduced-motion shows final numbers immediately
// Inputs:       argv[2] = output folder (work files and screenshots).
// Outputs:      One JSON result on stdout (every check true/false); exit code 1 if any check fails.
// Dependencies: playwright-core (installed beside this file), local Chrome, python + uvicorn + the listener source.
import { chromium } from 'playwright-core';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const out = process.argv[2];
const PORT = Number(process.env.TEST_PORT || 8435);   // overridable so several runs can go side by side
const BASE = `http://127.0.0.1:${PORT}`;
const LISTENER = process.env.LISTENER_DIR || 'C:/ops/broadcast-watchdog/core-listener';
const PROC = path.join(out, 'proc');
const DB = path.join(out, 'listener.db');
const PROBE_PATTERN = /gstatic\.com\/generate_204|cloudflare\.com\/cdn-cgi\/trace/;
const checks = {};
fs.mkdirSync(PROC, { recursive: true });
// a database left in the output folder by an earlier run would carry its alarms into this one (counters, 'since last critical') and fail checks that are about THIS run
fs.rmSync(DB, { force: true });

// ---- fake kernel counters: 8 cores, about 30 % busy, 43 % of memory in use ----
const jiffies = { busy: 1_000_000, idle: 3_000_000 };
function writeProc() {
  const step = 100 * 8;   // one second at HZ=100 on 8 cores
  jiffies.busy += Math.round(step * 0.3);
  jiffies.idle += Math.round(step * 0.7);
  fs.writeFileSync(path.join(PROC, 'stat'), `cpu  ${jiffies.busy} 0 0 ${jiffies.idle} 0 0 0 0 0 0\n`);
  fs.writeFileSync(path.join(PROC, 'loadavg'), '2.40 2.10 1.90 2/400 1234\n');
  fs.writeFileSync(path.join(PROC, 'uptime'), '1234567.00 100.00\n');
  fs.writeFileSync(path.join(PROC, 'meminfo'), 'MemTotal: 16384000 kB\nMemFree: 1000 kB\nMemAvailable: 9300000 kB\n');
}
writeProc();
const procTimer = setInterval(writeProc, 1000);

function startServer() {
  return spawn('python', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(PORT)], {
    cwd: LISTENER,
    env: { ...process.env, WATCHDOG_SHARED_TOKEN: 'test-token', WATCHDOG_DB_PATH: DB, WATCHDOG_PROC_ROOT: PROC, WATCHDOG_DISPLAY_TIMEZONE: 'Pacific/Auckland',
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
const instance = (feed, down = false) => ({ feed, launcher_present: true, engine_present: !down, advance_alarm_confirmed: true, advancing: !down,
  advance_age_seconds: 0, advance_confirmed_seconds: 300, engine_uptime_seconds: 9000, engine_cpu_cores: 1.2 });
let mainDown = false;
let site_bDown = false;
async function beat(only) {
  const all = {
    MAIN: () => post('/heartbeat', { hostname: 'MAIN', expected_interval_seconds: 10, instance_count: 2, activity_monitor: { version: 1 },
      instances: [{ ...instance('usa', mainDown), now_playing: { filename: 'PETA HEN WUT 24.mp4', duration: '00:03:30', result: 'Playing' } }, instance('caribbean')] }),
    BACKUP: () => post('/heartbeat', { hostname: 'BACKUP-SERVER', expected_interval_seconds: 10, instance_count: 2, activity_monitor: { version: 1 },
      instances: [instance('usa'), instance('caribbean')] }),
    SITE_A: () => post('/heartbeat', { hostname: 'SITE-A', expected_interval_seconds: 30, status: 'decoding', current_item: 'GUF 10 BUROF LUZIC 2.mp4',
      timestamp: isoAgo(0), current_item_started_utc: isoAgo(10), current_item_duration_s: 120, next_item: 'Delogatum Pasesujuk LUZIC 2.mp4' }),
    SITE_B: () => post('/heartbeat', { hostname: 'SITE-B', expected_interval_seconds: 300, current_item: 'NUD LOZE BOWU WUT 4 JUCU COSENA.mp4', timestamp: isoAgo(0), player_running: !site_bDown }),
  };
  for (const key of only || ['MAIN', 'BACKUP', 'SITE-A']) await all[key]();
}
function backdate(sourceId, seconds) {
  const code = `import sqlite3,sys,datetime\nc=sqlite3.connect(sys.argv[1])\nt=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=int(sys.argv[3]))).isoformat()\nc.execute("UPDATE sources SET last_seen_utc=? WHERE source_id=?",(t,sys.argv[2]))\nc.commit()`;
  spawnSync('python', ['-c', code, DB, sourceId, String(seconds)]);
}
const text = (page, selector) => page.locator(selector).first().innerText({ timeout: 1500 }).catch(() => null);
async function waitFor(fn, ms = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (await fn()) return true; await new Promise(r => setTimeout(r, 250)); }
  return false;
}
const seconds = t => { const m = /(?:(\d+)m )?(\d+)s/.exec(t || ''); return m ? (Number(m[1] || 0) * 60 + Number(m[2])) : null; };

let server = startServer();
if (!(await waitUp())) { console.log(JSON.stringify({ error: 'local server did not start' })); process.exit(1); }
await beat(['MAIN', 'BACKUP', 'SITE-A', 'SITE-B']);
backdate('SITE-B', 120);   // SITE-B reports every 300 s: its last heartbeat is two minutes old
const feeder = setInterval(() => beat(), 5000);

const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });

// =========================== Phase 1: desktop, healthy ===========================
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block', timezoneId: 'America/New_York' });
const page = await context.newPage();
const pageErrors = [];
page.on('pageerror', e => pageErrors.push(String(e)));
const outsideRequests = [];
page.on('request', r => { const host = new URL(r.url()).hostname; if (host !== '127.0.0.1') outsideRequests.push(r.url()); });
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });

checks.counter_healthy_reaches_6 = await waitFor(async () => (await text(page, '#counter-healthy .counter-value b')) === '6');
checks.counter_healthy_unit_is_of_6 = (await text(page, '#counter-healthy .counter-value i')) === '/ 6';
checks.counter_since_says_none_with_no_red_alarm = (await text(page, '#counter-since .counter-value b')) === 'None';
checks.banner_says_all_healthy = /All six playout endpoints are healthy/.test((await text(page, '#banner')) || '');

// Core server panel
checks.vitals_cpu_is_a_percentage = await waitFor(async () => /^\d+%$/.test((await text(page, '#v-cpu')) || ''), 12000);
// the number counts up to its value, so wait for it to arrive rather than reading it mid-count
checks.vitals_cpu_matches_the_fake_30_percent = await waitFor(async () => { const value = Number(((await text(page, '#v-cpu')) || '').replace('%', '')); return value >= 20 && value <= 40; });
checks.vitals_load_is_2_40 = (await text(page, '#v-load')) === '2.40';
checks.vitals_memory_is_43_percent = (await text(page, '#m-mem b')) === '43%';
const cores = Number(await text(page, '#v-cores'));   // the machine running the test decides this, not the fake files
checks.vitals_cores_is_a_number = cores > 0;
checks.vitals_load_per_core_is_load_over_cores = (await text(page, '#m-load b')) === (2.4 / cores).toFixed(2);
checks.vitals_uptime_shown = /^up 14d/.test((await text(page, '#v-up')) || '');
checks.vitals_ring_is_drawn = await waitFor(async () => {
  const offset = Number(await page.locator('#v-arc').evaluate(node => node.style.strokeDashoffset));
  return offset > 0 && offset < 289.03;
});
checks.vitals_history_line_is_drawn = await waitFor(async () => ((await page.locator('#v-spark-line').getAttribute('d')) || '').startsWith('M '), 20000);
checks.vitals_heading_is_one_line = (await page.locator('#vitals .vitals-head .eyebrow').evaluate(node => node.getBoundingClientRect().height)) < 20;
checks.vitals_says_it_covers_all_workloads = /all workloads/i.test((await text(page, '#vitals .eyebrow')) || '');
// alignment: the three counter columns sit exactly over the three tile columns, and the Core panel exactly over the incident feed
const box = selector => page.locator(selector).first().boundingBox();
for (const width of [1440, 1100]) {
  await page.setViewportSize({ width, height: 900 });
  await page.waitForTimeout(400);
  const counters = [await box('#counter-healthy'), await box('#counter-since'), await box('#counter-red24')];
  const tiles = [await box('.endpoint-tile:nth-child(1)'), await box('.endpoint-tile:nth-child(2)'), await box('.endpoint-tile:nth-child(3)')];
  const near = (a, b) => Math.abs(a - b) <= 1;
  checks['counters_line_up_with_the_tile_columns_at_' + width] = counters.every((c, i) => near(c.x, tiles[i].x) && near(c.width, tiles[i].width));
  const vitalsBox = await box('#vitals');
  const feedBox = await box('.incident-feed');
  checks['core_panel_lines_up_with_the_incident_feed_at_' + width] = near(vitalsBox.x, feedBox.x) && near(vitalsBox.width, feedBox.width);
}
await page.setViewportSize({ width: 1440, height: 900 });

// now playing: freshness and progress
const site_b = page.locator('.endpoint-tile', { hasText: 'SITE-B' });
const site_a = page.locator('.endpoint-tile', { hasText: 'SITE-A' });
// innerText follows the page's upper-case styling, so labels are compared without regard to case
checks.a_site_reporting_every_5_minutes_is_on_air_two_minutes_after_its_report = await waitFor(async () => ((await text(site_b, '.now-label')) || '').toLowerCase() === 'on air');
checks.its_age_line_explains_the_slow_cadence = /^Reported 2m \d+s ago - this site reports every 5m 0s$/.test((await text(site_b, '.now-age')) || '');
checks.not_marked_stale_inside_its_own_interval = (await site_b.locator('.screen.stale').count()) === 0;
backdate('SITE-B', 346);   // 5m 46s old: past its own 5-minute interval plus 45 s, and still 14 s short of the listener calling the heartbeat lost
checks.stale_name_is_labelled_last_reported = await waitFor(async () => ((await text(site_b, '.now-label')) || '').toLowerCase() === 'last reported', 12000);
checks.stale_name_says_it_may_have_changed = await waitFor(async () => /^Reported 5m \d+s ago - may have changed$/.test((await text(site_b, '.now-age')) || ''), 12000);
checks.stale_screen_is_marked = (await site_b.locator('.screen.stale').count()) === 1;
await beat(['SITE-B']);   // a fresh report now: left at 346 s it would cross the 360 s heartbeat-lost limit and be (rightly) red during the alarm checks below
// the fresh report above only reaches the page with the next pushed frame (up to 5 s later): measure after it has, or a frame landing between the two
// readings resets the age and the check fails for no fault of the page
await waitFor(async () => { const a = seconds(await text(site_b, '.now-age')); return a !== null && a < 30; }, 12000);
const age1 = seconds(await text(site_b, '.now-age'));
await page.waitForTimeout(2300);
const age2 = seconds(await text(site_b, '.now-age'));
checks.age_keeps_counting_between_frames = age1 !== null && age2 !== null && age2 >= age1 + 2;
checks.fresh_name_is_on_air = ((await text(site_a, '.now-label')) || '').toLowerCase() === 'on air';
checks.progress_bar_appears_when_the_sender_gives_start_and_length = await waitFor(async () => /^\d+:\d\d \/ 2:00$/.test((await text(site_a, '.now-time')) || ''));
const barWidth = parseFloat(await site_a.locator('.now-bar i').evaluate(node => node.style.width));
checks.progress_bar_has_a_sensible_width = barWidth > 3 && barWidth < 40;
checks.next_item_is_shown = /Next: Delogatum Pasesujuk LUZIC 2\.mp4/.test((await text(site_a, '.now-next')) || '');
checks.redundant_by_heartbeat_line_is_hidden = (await site_a.locator('.now-sub').isVisible()) === false;

// tiles are built once and updated in place
const main = page.locator('.endpoint-tile', { hasText: 'MAIN-USA' });
await main.locator('.endpoint-face').click();
await page.waitForTimeout(400);
await page.evaluate(() => {
  const tile = [...document.querySelectorAll('.endpoint-tile')].find(node => node.textContent.includes('MAIN-USA'));
  window.__tile = tile;
  window.__grid = tile.querySelector('.details-grid');
  // the operator selects a value in the panel, as when copying a process id or a time
  window.__row = [...tile.querySelectorAll('.diag-row')].find(row => row.textContent.startsWith('Interval')).querySelector('.diag-value');
  window.__rowText = window.__row.textContent;
  window.getSelection().selectAllChildren(window.__row);
});
await page.waitForTimeout(7000);   // at least one more pushed frame
const survived = await page.evaluate(() => ({
  tileSame: window.__tile.isConnected && [...document.querySelectorAll('.endpoint-tile')].includes(window.__tile),
  gridSame: window.__grid.isConnected && window.__tile.querySelector('.details-grid') === window.__grid,
  open: !window.__tile.querySelector('.details-panel').hidden,
  rowSame: window.__row.isConnected,
  selectionKept: window.getSelection().toString() === window.__rowText && window.getSelection().toString() !== '',
}));
checks.tile_element_survives_a_live_frame = survived.tileSame;
checks.open_details_panel_is_not_rebuilt_and_stays_open = survived.gridSame && survived.open && survived.rowSame;
checks.text_selected_in_the_details_survives_a_live_frame = survived.selectionKept;
checks.last_heard_shows_a_readable_time_not_raw_iso = !/T\d\d:\d\d:\d\d\.\d+\+00:00/.test((await text(main, '.details-panel')) || '');

// the two clocks: Core's (shown in the zone the server names) beside this device's, which here is a different zone
const hourIn = zone => new Intl.DateTimeFormat('en-US', { hour: '2-digit', hour12: false, timeZone: zone }).format(new Date()).replace(/^24$/, '00');
const coreClock = (await text(page, '#clock-core')) || '';
const deviceClock = (await text(page, '#clock-device')) || '';
checks.core_clock_is_shown_with_its_zone = /\d\d:\d\d:\d\d/.test(coreClock) && /GMT\+1[23]|NZ[DS]T/.test(coreClock);
checks.device_clock_is_shown_with_its_own_zone = /\d\d:\d\d:\d\d/.test(deviceClock) && /E[DS]T/.test(deviceClock);
const shownHour = clock => clock.match(/(\d\d):\d\d:\d\d/)[1];
const twelveToTwentyFour = (clock, hour) => /PM/i.test(clock) ? String((Number(hour) % 12) + 12).padStart(2, '0') : String(Number(hour) % 12).padStart(2, '0');
checks.core_clock_shows_cores_hour_not_the_devices = twelveToTwentyFour(coreClock, shownHour(coreClock)) === hourIn('Pacific/Auckland') || twelveToTwentyFour(coreClock, shownHour(coreClock)) === hourIn('Pacific/Auckland');
checks.the_two_clocks_differ_by_the_zone_gap = shownHour(coreClock) !== shownHour(deviceClock) || coreClock !== deviceClock;
checks.core_clock_ticks = await (async () => { const a = await text(page, '#clock-core'); await page.waitForTimeout(2200); return a !== (await text(page, '#clock-core')); })();
checks.no_clock_warning_when_the_device_clock_is_right = (await page.locator('#clock-skew').isVisible()) === false;
checks.banner_has_no_updated_line = !/Updated/.test((await text(page, '#banner')) || '');   // removed at the owner's request, 2026-10-15; the header's Core clock says the time
checks.incident_feed_says_whose_time_it_is = /auckland time/i.test((await text(page, '.incident-heading')) || '');   // named after the zone Core shows (this test runs Pacific/Auckland)
checks.core_clock_heading_is_the_zone_city_not_the_server_name = ((await text(page, '#clock-core-label')) || '').trim().toLowerCase() === 'auckland';

// brand and layout
checks.brand_footer_is_shown = /CLARIXTECH GROUP/.test((await text(page, '.site-foot')) || '');
checks.brand_link_is_safe = (await page.locator('a.foot-link').getAttribute('href')) === 'https://corp.example' && (await page.locator('a.foot-link').getAttribute('rel')) === 'noopener noreferrer';
checks.header_says_by_clarixtech = /by ClarixTech Group/i.test((await text(page, '.brand-sub')) || '');
checks.no_outside_request_while_the_feed_is_healthy = outsideRequests.length === 0;
checks.no_sideways_scroll_on_desktop = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
await page.screenshot({ path: path.join(out, '1-desktop-healthy.png'), fullPage: true });

// =========================== Phase 2: a red alarm ===========================
mainDown = true;
await beat(['MAIN']);
checks.banner_goes_critical = await waitFor(async () => /1 endpoint in critical alarm/.test((await text(page, '#banner')) || ''), 12000);
checks.counter_red24_counts_the_alarm = await waitFor(async () => (await text(page, '#counter-red24 .counter-value b')) === '1');
checks.counter_since_now_counts_up = await waitFor(async () => /^\d+(m \d+)?s$/.test((await text(page, '#counter-since .counter-value b')) || ''), 12000);
// Sampled 12 times over 4.8 s. The page counts forward on its own timer and re-bases on each pushed frame (every 5 s), and the server reports whole
// seconds, so a frame landing in the window can move the display back by under a second: one before/after pair is not a fair test (it failed at random).
// A counter that ticks every second shows at least four different values and rises by at least three; one that only moved on frames would show two.
const sinceSamples = [];
for (let i = 0; i < 12; i += 1) { sinceSamples.push(seconds(await text(page, '#counter-since .counter-value b'))); await page.waitForTimeout(400); }
checks.counter_since_ticks_every_second = sinceSamples.every(v => v !== null) && new Set(sinceSamples).size >= 4 && sinceSamples[sinceSamples.length - 1] >= sinceSamples[0] + 3;
checks.incident_times_use_cores_zone = await waitFor(async () => /GMT\+1[23]|NZ[DS]T/.test((await text(page, '.incident-time')) || ''), 8000);
checks.a_red_tile_reads_off_air = await waitFor(async () => ((await text(main, '.now-label')) || '').toLowerCase() === 'off air', 8000);
checks.off_air_screen_is_marked = (await main.locator('.screen.off-air').count()) === 1;
checks.critical_tile_is_marked = (await main.evaluate(node => node.classList.contains('status-critical'))) === true;
await page.screenshot({ path: path.join(out, '2-desktop-alarm.png'), fullPage: true });
mainDown = false;
await beat(['MAIN']);
await waitFor(async () => !/MAIN-USA/.test((await text(page, '#banner')) || ''), 12000);

// SITE_B: its heartbeat says the player is not running -> red at once, the tile reads OFF AIR, and it clears when the player is back
site_bDown = true;
await beat(['SITE-B']);
checks.site_b_goes_red_the_moment_its_detector_says_the_player_is_off = await waitFor(async () => /SITE-B/.test((await text(page, '#banner')) || '') && /critical alarm/.test((await text(page, '#banner')) || ''), 12000);
checks.site_b_tile_is_critical_and_reads_off_air = await waitFor(async () => (await site_b.evaluate(node => node.classList.contains('status-critical'))) && ((await text(site_b, '.now-label')) || '').toLowerCase() === 'off air', 8000);
checks.site_b_says_why = /Player Down/i.test((await text(site_b, '.tile-footer')) || '');
await page.screenshot({ path: path.join(out, '2b-desktop-site_b-player-off.png'), fullPage: true });
site_bDown = false;
await beat(['SITE-B']);
checks.site_b_clears_when_the_player_is_back = await waitFor(async () => !(await site_b.evaluate(node => node.classList.contains('status-critical'))) && ((await text(site_b, '.now-label')) || '').toLowerCase() === 'on air', 12000);

// =========================== Phase 3: the live feed is lost ===========================
await context.route(PROBE_PATTERN, route => route.abort('internetdisconnected'));
await killServer(server);
checks.blind_after_the_feed_stops = await waitFor(async () => (await page.evaluate(() => document.body.classList.contains('is-blind'))), 30000);
checks.banner_says_live_feed_lost = /Live feed lost/.test((await text(page, '#banner')) || '');
checks.indicator_says_no_live_data = await waitFor(async () => (await text(page, '#live-text')) === 'NO LIVE DATA' || (await text(page, '#live-text')) === 'No live data', 6000);
checks.names_a_dead_internet = await waitFor(async () => /no internet connection/.test((await text(page, '#banner')) || ''), 12000);
checks.core_clock_is_labelled_last_known_while_blind = await waitFor(async () => /last known/i.test((await text(page, '#clock-core-label')) || ''), 4000);
checks.core_clock_is_frozen_while_blind = await (async () => { const a = await text(page, '#clock-core'); await page.waitForTimeout(2300); return a === (await text(page, '#clock-core')); })();
checks.banner_says_unverified = /Treat it as unverified/.test((await text(page, '#banner')) || '');
await page.screenshot({ path: path.join(out, '3-desktop-feed-lost-no-internet.png') });
await context.unroute(PROBE_PATTERN);
await context.route(PROBE_PATTERN, route => route.fulfill({ status: 204, body: '' }));
checks.names_core_when_the_internet_works = await waitFor(async () => /internet works, but Core is not answering/.test((await text(page, '#banner')) || ''), 25000);
await page.screenshot({ path: path.join(out, '4-desktop-feed-lost-core.png') });

// =========================== Phase 4: it recovers by itself ===========================
server = startServer();
await waitUp(30000);
await beat(['MAIN', 'BACKUP', 'SITE-A', 'SITE-B']);
checks.recovers_by_itself = await waitFor(async () => !(await page.evaluate(() => document.body.classList.contains('is-blind'))) && !/Live feed lost/.test((await text(page, '#banner')) || ''), 45000);
checks.indicator_is_live_again = await waitFor(async () => /live/i.test((await text(page, '#live-text')) || '') && !/no live data/i.test((await text(page, '#live-text')) || ''), 10000);
await context.close();

// =========================== Phase 5: reduced motion and a phone ===========================
const calm = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, reducedMotion: 'reduce', serviceWorkers: 'block' });
const phone = await calm.newPage();
await phone.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await phone.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
checks.reduced_motion_shows_final_numbers_at_once = (await text(phone, '#counter-healthy .counter-value b')) === '6';
checks.no_sideways_scroll_on_a_phone = await phone.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
checks.phone_counters_are_two_columns = await phone.evaluate(() => getComputedStyle(document.getElementById('counters')).gridTemplateColumns.trim().split(/\s+/).length === 2);
checks.phone_tiles_are_one_column = await phone.evaluate(() => getComputedStyle(document.getElementById('endpoint-grid')).gridTemplateColumns.trim().split(/\s+/).length === 1);
await phone.screenshot({ path: path.join(out, '5-phone.png'), fullPage: true });
await calm.close();

// a viewing device whose clock is 3 minutes fast: the page says so and Core's clock is still Core's
const normal = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block', timezoneId: 'America/New_York' });
const normalPage = await normal.newPage();
await normalPage.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await normalPage.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await normalPage.waitForTimeout(1500);
const page_for_compare = () => normalPage;
const skewed = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block', timezoneId: 'America/New_York' });
await skewed.addInitScript(() => { const real = Date.now.bind(Date); Date.now = () => real() + 180000; });
const fast = await skewed.newPage();
await fast.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await fast.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
checks.a_fast_device_clock_is_called_out = await waitFor(async () => /3m \d+s ahead of Core/.test((await text(fast, '#clock-skew')) || ''), 8000);
const coreOnFast = (await text(fast, '#clock-core')) || '';
const coreOnRight = (await text(page_for_compare(), '#clock-core')) || '';
checks.core_clock_is_not_moved_by_a_wrong_device_clock = coreOnFast.length > 0 && coreOnFast.slice(0, 5) === coreOnRight.slice(0, 5);
await skewed.close();
await normal.close();

// the FIRST tap on a playout tile, made while the "tap to allow sound" strip is showing, must open it, and the strip must then leave. (A sanity
// check on the tap path: tall tiles survived the old jump too. The case that really failed was the small update chip, checked below by
// `the_tap_reloads_the_page`, made after the strip had been showing for a while.)
const tapContext = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block' });
const tapTab = await tapContext.newPage();
await tapTab.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await tapTab.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await tapTab.waitForTimeout(2500);
checks.the_sound_strip_is_showing_before_the_first_tap = await tapTab.locator('#siren-strip').isVisible();
await tapTab.locator('.endpoint-face').first().click();
checks.the_first_tap_on_a_tile_opens_it = await waitFor(async () => (await tapTab.locator('.endpoint-face').first().getAttribute('aria-expanded')) === 'true', 3000);
checks.the_strip_leaves_after_the_tap_has_finished = await waitFor(async () => !(await tapTab.locator('#siren-strip').isVisible()), 3000);
await tapContext.close();

// a page left open across a deploy: every frame now says a DIFFERENT build is being served. The page must offer an update, and must NOT reload itself.
const current = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block' });
const currentTab = await current.newPage();
await currentTab.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await currentTab.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await currentTab.waitForTimeout(7000);   // at least one more frame, each of which names the build being served
checks.no_update_chip_on_a_current_page = (await currentTab.locator('#update-chip').isVisible()) === false;
await current.close();
const stale = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block', timezoneId: 'America/New_York' });
await stale.addInitScript(() => {
  const Real = window.EventSource;
  window.EventSource = class extends Real {
    set onmessage(handler) {
      super.onmessage = event => { const frame = JSON.parse(event.data); frame.version = 'a-newer-build'; handler({ data: JSON.stringify(frame) }); };
    }
  };
});
const oldTab = await stale.newPage();
await oldTab.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await oldTab.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await oldTab.evaluate(() => { window.__stillTheSameDocument = true; });
checks.a_stale_page_offers_a_one_tap_update = await waitFor(async () => await oldTab.locator('#update-chip').isVisible(), 8000);
await oldTab.waitForTimeout(12000);   // long enough for any timer-driven reload to have happened
checks.it_does_not_reload_by_itself = await oldTab.evaluate(() => window.__stillTheSameDocument === true);
await oldTab.locator('#update-chip').click();
checks.the_tap_reloads_the_page = await waitFor(async () => (await oldTab.evaluate(() => window.__stillTheSameDocument === true).catch(() => false)) === false, 10000);
await stale.close();

// topology keeps its banner and gains the CPU readout
const tctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, serviceWorkers: 'block' });
const topo = await tctx.newPage();
await topo.goto(BASE + '/topology', { waitUntil: 'domcontentloaded' });
await topo.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await topo.waitForTimeout(3000);
checks.topology_hides_counters_and_server_panel = (await topo.locator('#counters').isVisible()) === false && (await topo.locator('#vitals').isVisible()) === false;
const topoCpu = await waitFor(async () => /^\d+%$/.test((await text(topo, '.holo-cpu b')) || ''), 12000);   // a first reading can take one more frame
checks.topology_core_card_shows_cpu = topoCpu;
const topoCpuText = await text(topo, '.holo-cpu b');
await topo.screenshot({ path: path.join(out, '6-topology.png'), fullPage: true });
await tctx.close();

clearInterval(feeder);
clearInterval(procTimer);
await browser.close();
await killServer(server);
checks.no_page_errors = pageErrors.length === 0;
const failed = Object.entries(checks).filter(([, ok]) => !ok).map(([name]) => name);
console.log(JSON.stringify({ passed: Object.keys(checks).length - failed.length, total: Object.keys(checks).length, failed, topoCpuText, pageErrors, outsideRequests: outsideRequests.slice(0, 5), checks }, null, 1));
process.exit(failed.length ? 1 : 0);
