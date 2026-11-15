// File:         offline_hang_test.mjs
// Purpose:      Proves what happens when the connection HANGS instead of failing (a weak or ship link, Wi-Fi with no internet): the port accepts
//               the connection and never answers.
//               A. With the app's shell saved (every normal visitor after the first visit) the app OPENS from it within a few seconds and says
//                  there is no live data, instead of a "server not accessible" screen for a server that may be perfectly fine.
//               B. On a first-ever visit with no saved shell the offline placeholder appears after a longer wait (8 s), because nothing else exists.
//               Also checks the manifest's icon URLs. Starts its own local listener; never touches production.
// Inputs:       argv[2] = output folder.
// Outputs:      One JSON result (every check true/false) and screenshots; exit code 1 if any check fails.
// Dependencies: playwright-core (beside this file), local Chrome, python + uvicorn + the listener source.
import { chromium } from 'playwright-core';
import { spawn } from 'node:child_process';
import net from 'node:net';
import path from 'node:path';

const out = process.argv[2];
const PORT = Number(process.env.TEST_PORT || 8433);
const BASE = `http://127.0.0.1:${PORT}`;
const LISTENER = process.env.LISTENER_DIR || 'C:/ops/broadcast-watchdog/core-listener';
const checks = {};
const detail = {};

const server = spawn('python', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(PORT)], {
  cwd: LISTENER,
  env: { ...process.env, WATCHDOG_SHARED_TOKEN: 'test-token', WATCHDOG_DB_PATH: path.join(out, 'hang.db'),
    WATCHDOG_EXPECTED_INSTANCES: '{"MAIN":["usa","caribbean"],"BACKUP-SERVER":["usa","caribbean"]}' },
  stdio: 'ignore',
});
for (let i = 0; i < 60; i += 1) { try { if ((await fetch(BASE + '/offline.html')).ok) break; } catch { /* starting */ } await new Promise(r => setTimeout(r, 300)); }

const manifest = await (await fetch(BASE + '/manifest.json')).json();
const iconStatuses = [];
for (const icon of manifest.icons) { const r = await fetch(BASE + icon.src); iconStatuses.push(r.status === 200 && /image\/png/.test(r.headers.get('content-type') || '')); }
checks.manifest_icons_all_load = iconStatuses.length === 3 && iconStatuses.every(Boolean);

const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, serviceWorkers: 'allow' });
const page = await context.newPage();
await page.goto(BASE + '/');
await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
checks.worker_is_active = await page.evaluate(async () => !!(await navigator.serviceWorker.ready).active);
await page.waitForTimeout(1500);

// the connection now HANGS: the port accepts and never answers
await new Promise(resolve => { const k = spawn('taskkill', ['/PID', String(server.pid), '/T', '/F'], { stdio: 'ignore' }); k.on('exit', resolve); });
await new Promise(r => setTimeout(r, 800));
const tarpit = net.createServer(socket => { socket.on('error', () => {}); /* accept, never reply */ });
await new Promise(resolve => tarpit.listen(PORT, '127.0.0.1', resolve));

// A. the shell is saved: the app opens from it after the short wait
let t0 = Date.now();
let openedA = true;
try { await page.goto(BASE + '/', { timeout: 40000 }); } catch (e) { openedA = false; }
detail.secondsToAppFromSavedShell = Math.round((Date.now() - t0) / 100) / 10;
checks.A_app_opens_from_the_saved_shell = openedA && (await page.locator('#banner').count()) === 1;
checks.A_within_about_the_short_timeout = detail.secondsToAppFromSavedShell >= 3 && detail.secondsToAppFromSavedShell <= 9;
checks.A_not_chromes_error_page_and_not_the_blocking_screen = (await page.locator('#main-frame-error').count()) === 0 && (await page.locator('#node-server').count()) === 0;
await page.waitForFunction(() => /Live feed lost/.test(document.getElementById('banner').innerText), null, { timeout: 25000 }).then(() => { checks.A_then_says_it_has_no_live_data = true; }, () => { checks.A_then_says_it_has_no_live_data = false; });
await page.screenshot({ path: path.join(out, 'A-app-from-saved-shell-hanging-connection.png') });

// B. a first visit has no saved shell: remove the saved shells, and the placeholder is what is left
await page.evaluate(async () => { const c = await caches.open((await caches.keys())[0]); await c.delete('/'); await c.delete('/topology'); });
t0 = Date.now();
try { await page.goto(BASE + '/', { timeout: 40000 }); } catch (e) { /* the placeholder is served by the worker, so this normally succeeds */ }
detail.secondsToPlaceholderOnAFirstVisit = Math.round((Date.now() - t0) / 100) / 10;
checks.B_placeholder_appears_when_nothing_is_saved = (await page.locator('#node-server').count()) === 1;
checks.B_after_the_longer_first_visit_wait = detail.secondsToPlaceholderOnAFirstVisit >= 7 && detail.secondsToPlaceholderOnAFirstVisit <= 14;
checks.B_not_chromes_error_page = (await page.locator('#main-frame-error').count()) === 0;
await page.screenshot({ path: path.join(out, 'B-first-visit-placeholder.png') });

await browser.close();
tarpit.close();
const failed = Object.entries(checks).filter(([, ok]) => !ok).map(([name]) => name);
console.log(JSON.stringify({ passed: Object.keys(checks).length - failed.length, total: Object.keys(checks).length, failed, detail, checks }, null, 1));
process.exit(failed.length ? 1 : 0);
