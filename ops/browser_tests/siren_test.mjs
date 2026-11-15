// File:         siren_test.mjs
// Purpose:      Proves the siren behaves in real Chrome against a LOCAL copy of the listener (never production):
//               fresh page + active alarm + no tap => a loud red strip says the siren is silent; one tap anywhere starts it
//               (exactly one oscillator); sound-on is remembered across reloads; silencing is NOT remembered and re-arms by
//               itself when the board clears; sound-off is remembered and the strip can turn it back on (there is no Test siren
//               button any more: the owner's rule is that the dashboard has none). The speakers are a fake AudioContext that obeys Chrome's autoplay rule
//               (resume() only works after the person has touched the page).
// Inputs:       argv[2] = output folder (database and screenshots).
// Outputs:      PASS/FAIL line per check and a JSON summary; exit code 1 if any check fails.
// Dependencies: playwright-core (beside this file), local Chrome, python + uvicorn + the listener source.
import { chromium } from 'playwright-core';
import { spawn } from 'node:child_process';
import path from 'node:path';

const out = process.argv[2];
const PORT = 8434;
const BASE = `http://127.0.0.1:${PORT}`;
const checks = [];
const check = (name, ok, detail = '') => { checks.push({ name, ok: !!ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '  -> ' + detail}`); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

const server = spawn('python', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(PORT)], {
  cwd: process.env.LISTENER_DIR || 'C:/ops/broadcast-watchdog/core-listener',
  env: { ...process.env, WATCHDOG_SHARED_TOKEN: 'test-token', WATCHDOG_DB_PATH: path.join(out, 'siren.db'),
    WATCHDOG_EXPECTED_INSTANCES: '{"MAIN":["usa","caribbean"],"BACKUP-SERVER":["usa","caribbean"]}' },
  stdio: 'ignore',
});
for (let i = 0; i < 60; i += 1) { try { if ((await fetch(BASE + '/offline.html')).ok) break; } catch { /* starting */ } await sleep(300); }

let frozen = true;
const headers = { 'X-Watchdog-Token': 'test-token', 'Content-Type': 'application/json' };
const inst = (f, age = 0, advancing = true, cpu = 1.2) => ({ feed: f, launcher_present: true, engine_present: true, advance_alarm_confirmed: true, advancing,
  advance_age_seconds: age, advance_confirmed_seconds: advancing ? 300 : 0, engine_uptime_seconds: 9000, engine_cpu_cores: cpu, engine_pid: f === 'usa' ? 100 : 200 });
async function beat() {
  for (const body of [
    { hostname: 'SITE-A', expected_interval_seconds: 30, status: 'decoding' },
    { hostname: 'SITE-B', expected_interval_seconds: 300, current_item: 'x.mp4' },
    { hostname: 'MAIN', expected_interval_seconds: 10, instance_count: 2, instances: [frozen ? inst('usa', 75, false, 0.01) : inst('usa'), inst('caribbean')], activity_monitor: { version: 1 } },
    { hostname: 'BACKUP-SERVER', expected_interval_seconds: 10, instance_count: 2, instances: [inst('usa'), inst('caribbean')], activity_monitor: { version: 1 } },
  ]) await fetch(BASE + '/heartbeat', { method: 'POST', headers, body: JSON.stringify(body) });
}
await beat();
const beating = setInterval(beat, 3000);

const FAKE_AUDIO = () => {
  const fake = { ctxs: 0, oscillators: 0, state: 'none', gainTarget: 0 };
  fake.audible = () => fake.state === 'running' && fake.gainTarget > 0.5;
  window.__fake = fake;
  class Param { constructor(v = 0) { this.value = v; } cancelScheduledValues() {} setValueAtTime(v) { this.value = v; return this; } linearRampToValueAtTime() { return this; } setTargetAtTime(v) { this.value = v; return this; } }
  class GainParam extends Param { setTargetAtTime(v) { this.value = v; fake.gainTarget = v; return this; } }
  class FakeCtx {
    constructor() { fake.ctxs += 1; this._state = 'suspended'; fake.state = 'suspended'; this.destination = {}; this.onstatechange = null; this._t0 = performance.now(); }
    get state() { return this._state; }
    get currentTime() { return (performance.now() - this._t0) / 1000; }
    _set(s) { if (this._state !== s) { this._state = s; fake.state = s; if (this.onstatechange) this.onstatechange(); } }
    // Chrome's rule: sound is refused until the person has touched the page
    resume() { if (navigator.userActivation && navigator.userActivation.hasBeenActive) { this._set('running'); return Promise.resolve(); } return new Promise(() => {}); }
    suspend() { this._set('suspended'); return Promise.resolve(); }
    createOscillator() { fake.oscillators += 1; return { type: '', frequency: new Param(440), connect() {}, start() {}, stop() {} }; }
    createGain() { return { gain: new GainParam(1), connect() {} }; }
    createDynamicsCompressor() { return { threshold: new Param(), knee: new Param(), ratio: new Param(), attack: new Param(), release: new Param(), connect() {} }; }
  }
  window.AudioContext = FakeCtx; window.webkitAudioContext = FakeCtx;
};

const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
const context = await browser.newContext({ viewport: { width: 1000, height: 800 }, serviceWorkers: 'block' });
const page = await context.newPage();
await page.addInitScript(FAKE_AUDIO);

const audio = () => page.evaluate(() => ({ state: window.__fake.state, oscillators: window.__fake.oscillators, audible: window.__fake.audible() }));
const strip = () => page.evaluate(() => { const e = document.getElementById('siren-strip'); return { hidden: e.hidden, text: e.textContent, loud: e.classList.contains('alarm') }; });
const muteLabel = () => page.locator('#mute-toggle').textContent();
async function load() { await page.goto(BASE + '/'); await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 }); await sleep(900); }
async function waitCritical() { for (let i = 0; i < 30; i += 1) { if ((await page.title()).startsWith('ALARM')) return true; await sleep(500); } return false; }
const tap = () => page.mouse.click(40, 400);

// A. fresh page, alarm active, nobody has touched the page yet
await load();
check('alarm is active on the board', await waitCritical(), await page.title());
let a = await audio(); let s = await strip();
check('A1 no tap yet: siren is NOT audible (browser rule)', !a.audible, JSON.stringify(a));
check('A2 a loud red strip says the siren is silent', !s.hidden && s.loud && /ALARM ACTIVE, SIREN SILENT/.test(s.text), JSON.stringify(s));
check('A3 the strip tells the person what to do', /Tap anywhere/i.test(s.text), s.text);
await page.screenshot({ path: path.join(out, 'siren-A-silent.png') });

// B. one tap anywhere
await tap(); await sleep(700);
a = await audio(); s = await strip();
check('B1 one tap anywhere starts the siren', a.audible, JSON.stringify(a));
check('B2 exactly one oscillator was ever created', a.oscillators === 1, JSON.stringify(a));
check('B3 the strip disappears once it sounds', s.hidden, JSON.stringify(s));

// C. reload: sound-on is remembered, the page needs one tap again
await page.reload(); await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 }); await sleep(900);
await waitCritical();
check('C1 after reload the sound toggle is still ON (remembered)', /on/i.test(await page.locator('#sound-toggle').textContent()), await page.locator('#sound-toggle').textContent());
a = await audio(); s = await strip();
check('C2 after reload and before any tap the strip asks for a tap', !s.hidden && s.loud && !a.audible, JSON.stringify({ s, a }));
await tap(); await sleep(700);
check('C3 one tap and it sounds again', (await audio()).audible);

// D. silence the siren: allowed, but never remembered
await page.locator('#mute-toggle').click(); await sleep(500);
a = await audio(); s = await strip();
check('D1 silencing stops the sound', !a.audible, JSON.stringify(a));
check('D2 while silenced a loud strip still says so', !s.hidden && s.loud && /silenced/i.test(s.text), JSON.stringify(s));
await page.reload(); await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 }); await sleep(900); await waitCritical();
check('D3 after reload silence is NOT remembered', (await muteLabel()) === 'Silence siren', await muteLabel());
await tap(); await sleep(700);
check('D4 siren sounds after reload without un-silencing', (await audio()).audible);

// E. silence, then the board clears: silence re-arms by itself, the next alarm sounds with no new tap
await page.locator('#mute-toggle').click(); await sleep(400);
frozen = false;
for (let i = 0; i < 30; i += 1) { if (!(await page.title()).startsWith('ALARM')) break; await sleep(500); }
await sleep(800);
check('E1 board is clear', !(await page.title()).startsWith('ALARM'), await page.title());
check('E2 silence re-armed itself once the board cleared', (await muteLabel()) === 'Silence siren', await muteLabel());
frozen = true;
await waitCritical(); await sleep(1200);
a = await audio();
check('E3 the next alarm sounds with no new tap and still one oscillator', a.audible && a.oscillators === 1, JSON.stringify(a));

// F. sound off is remembered; the strip can turn it back on
await page.locator('#sound-toggle').click(); await sleep(500);
check('F1 turning sound off silences it', !(await audio()).audible);
await page.reload(); await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 }); await sleep(900); await waitCritical();
s = await strip();
check('F2 after reload sound is still OFF (remembered) and the strip says so', !s.hidden && /OFF/.test(s.text) && s.loud, JSON.stringify(s));
await page.locator('#siren-strip').click(); await sleep(800);
a = await audio();
check('F3 tapping the strip turns sound back on and it sounds', a.audible, JSON.stringify(a));

// G. the dashboard has no Test siren and no Reconnect stream button
check('G1 no Test siren button on the page', (await page.locator('#siren-test').count()) === 0);
check('G2 no Reconnect stream button on the page', (await page.locator('#refresh').count()) === 0);

clearInterval(beating);
await browser.close();
await new Promise(resolve => { const k = spawn('taskkill', ['/PID', String(server.pid), '/T', '/F'], { stdio: 'ignore' }); k.on('exit', resolve); });
const failed = checks.filter(c => !c.ok);
console.log(JSON.stringify({ total: checks.length, failed: failed.length }));
process.exit(failed.length ? 1 : 0);
