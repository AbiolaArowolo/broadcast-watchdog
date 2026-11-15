// File:         topology_deck_test.mjs
// Purpose:      Proves the command-deck topology in real Chrome against a LOCAL listener (never production): with very long file names and an
//               alarm active, at several screen sizes, no card touches another card or the Core card, every card stays inside the stage,
//               every connector line starts and ends on the real edge of its cards, long names are clamped to 3 lines with the full text on
//               hover, nothing scrolls sideways, the DOM and animations survive live updates, and phones still get the stacked layout.
// Inputs:       argv[2] = output folder (database and screenshots).
// Outputs:      PASS/FAIL line per check, a JSON summary, screenshots; exit code 1 if any check fails.
// Dependencies: playwright-core (beside this file), local Chrome, python + uvicorn + the listener source.
import { chromium } from 'playwright-core';
import { spawn } from 'node:child_process';
import path from 'node:path';

const out = process.argv[2];
const PORT = 8435;
const BASE = `http://127.0.0.1:${PORT}`;
const checks = [];
const check = (name, ok, detail = '') => { checks.push({ name, ok: !!ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '  -> ' + detail}`); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

const server = spawn('python', ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(PORT)], {
  cwd: process.env.LISTENER_DIR || 'C:/ops/broadcast-watchdog/core-listener',
  env: { ...process.env, WATCHDOG_SHARED_TOKEN: 'test-token', WATCHDOG_DB_PATH: path.join(out, 'deck.db'),
    WATCHDOG_EXPECTED_INSTANCES: '{"MAIN":["usa","caribbean"],"BACKUP-SERVER":["usa","caribbean"]}' },
  stdio: 'ignore',
});
for (let i = 0; i < 60; i += 1) { try { if ((await fetch(BASE + '/offline.html')).ok) break; } catch { /* starting */ } await sleep(300); }

const LONG = 'MuvuCafi M Weceh DA These Entrepreneurs Are Giving Back Through Their Beverage Tech Company With A Really Really Long Title Indeed - Part 2 (Extended Cut).mp4';
const headers = { 'X-Watchdog-Token': 'test-token', 'Content-Type': 'application/json' };
const inst = (f, name, age = 0, advancing = true, cpu = 1.2) => ({ feed: f, launcher_present: true, engine_present: true, advance_alarm_confirmed: true, advancing,
  advance_age_seconds: age, advance_confirmed_seconds: advancing ? 300 : 0, engine_uptime_seconds: 9000, engine_cpu_cores: cpu, engine_pid: f === 'usa' ? 100 : 200,
  now_playing: { filename: name, result: 'Started 10:00:00' } });
async function beat() {
  for (const body of [
    { hostname: 'SITE-A', expected_interval_seconds: 30, status: 'decoding', current_item: LONG },
    { hostname: 'SITE-B', expected_interval_seconds: 300, current_item: 'ZEBIFE JOJOZ 2.mp4' },
    { hostname: 'MAIN', expected_interval_seconds: 10, instance_count: 2, instances: [inst('usa', 'ZEPO NAVUBOCO WUT 6.mp4', 75, false, 0.01), inst('caribbean', LONG)], activity_monitor: { version: 1 } },
    { hostname: 'BACKUP-SERVER', expected_interval_seconds: 10, instance_count: 2, instances: [inst('usa', LONG), inst('caribbean', 'Diliwito Nugu Pobelumepof zitu g cadi Fosagen Kasis he Lific Lecehob.mp4')], activity_monitor: { version: 1 } },
  ]) await fetch(BASE + '/heartbeat', { method: 'POST', headers, body: JSON.stringify(body) });
}
await beat();
const beating = setInterval(beat, 3000);

const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });

const measure = () => {
  const rect = e => { const r = e.getBoundingClientRect(); return { l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height }; };
  const stage = document.querySelector('.holo-stage');
  const s = rect(stage);
  const nodes = Array.from(document.querySelectorAll('.holo-node')).map(n => ({ id: n.querySelector('.holo-name').textContent, side: n.closest('.holo-col').classList.contains('holo-left') ? 'left' : 'right', rect: rect(n), cls: n.className }));
  const core = rect(document.querySelector('.holo-core-card'));
  const boxes = nodes.map(n => n.rect).concat([core]);
  let overlaps = 0;
  for (let i = 0; i < boxes.length; i += 1) for (let j = i + 1; j < boxes.length; j += 1) {
    const a = boxes[i], b = boxes[j];
    if (a.l < b.r - 0.5 && b.l < a.r - 0.5 && a.t < b.b - 0.5 && b.t < a.b - 0.5) overlaps += 1;
  }
  const outside = boxes.filter(b => b.l < s.l - 0.5 || b.r > s.r + 0.5 || b.t < s.t - 0.5 || b.b > s.b + 0.5).length;
  const lines = Array.from(document.querySelectorAll('.holo-flow')).map(f => {
    const m = (f.getAttribute('d') || '').match(/M ([\d.]+) ([\d.]+) C .* ([\d.]+) ([\d.]+)$/);
    return m ? { x1: +m[1] + s.l, y1: +m[2] + s.t, x2: +m[3] + s.l, y2: +m[4] + s.t } : null;
  });
  let lineErrors = 0;
  nodes.forEach((n, i) => {
    const l = lines[i];
    if (!l) { lineErrors += 1; return; }
    // lines are in DOM order of the SVG (sorted by y); match by nearest start instead of index
  });
  const matched = nodes.map(n => {
    const edgeX = n.side === 'left' ? n.rect.r : n.rect.l;
    const midY = n.rect.t + n.rect.h / 2;
    const l = lines.find(x => x && Math.abs(x.x1 - edgeX) <= 2 && Math.abs(x.y1 - midY) <= 2);
    const hubX = n.side === 'left' ? core.l : core.r;
    return !!l && Math.abs(l.x2 - hubX) <= 2 && l.y2 >= core.t - 1 && l.y2 <= core.b + 1;
  });
  const clamp = Array.from(document.querySelectorAll('.holo-now')).map(e => {
    const cs = getComputedStyle(e);
    const lh = parseFloat(cs.lineHeight);
    const padding = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);   // the dashed rule's padding is not a text line
    return { lines: Math.round((e.clientHeight - padding) / lh), title: e.title, text: e.textContent, clipped: e.scrollHeight > e.clientHeight + 1 };
  });
  return {
    viewport: innerWidth + 'x' + innerHeight, nodeCount: nodes.length, overlaps, outside,
    sideways: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    linesDrawn: lines.filter(Boolean).length, linesOnEdges: matched.filter(Boolean).length,
    clamp, criticalNode: nodes.filter(n => /status-critical/.test(n.cls)).map(n => n.id),
    hubOverlapsRadar: false,
  };
};

const SIZES = [[1375, 856], [1920, 1080], [1100, 800], [1000, 900]];   // the map; anything under 980 px gets the stacked layout
for (const [w, h] of SIZES) {
  const context = await browser.newContext({ viewport: { width: w, height: h }, serviceWorkers: 'block' });
  const page = await context.newPage();
  await page.goto(BASE + '/topology');
  await page.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
  await page.waitForSelector('.holo-node', { timeout: 20000 });
  // the map exists before the first live frame does (on a busy machine the frame can be a second or more late): measure only once the alarm has been drawn
  await page.waitForFunction(() => document.querySelectorAll('.holo-node.status-critical').length === 1, null, { timeout: 20000 });
  await sleep(1500);
  const m = await page.evaluate(measure);
  const tag = `${w}x${h}`;
  check(`${tag}: all six playout cards and the Core card exist`, m.nodeCount === 6, JSON.stringify(m.nodeCount));
  check(`${tag}: no card touches another card or the Core card`, m.overlaps === 0, `${m.overlaps} overlapping pairs`);
  check(`${tag}: every card stays inside the stage`, m.outside === 0, `${m.outside} outside`);
  check(`${tag}: no sideways scrolling`, !m.sideways);
  check(`${tag}: six connector lines drawn, each on the real edge of its card and of the Core card`, m.linesDrawn === 6 && m.linesOnEdges === 6, `${m.linesDrawn} drawn, ${m.linesOnEdges} on the edges`);
  check(`${tag}: long names are clamped to 3 lines and carry the full text on hover`, m.clamp.every(c => c.lines <= 3) && m.clamp.some(c => c.title === LONG), JSON.stringify(m.clamp.map(c => c.lines)));
  check(`${tag}: the alarm is shown on the right card`, m.criticalNode.length === 1 && m.criticalNode[0] === 'MAIN-USA', JSON.stringify(m.criticalNode));
  if (w === 1375 || w === 1920 || w === 1000) await page.screenshot({ path: path.join(out, `deck-${tag}.png`) });
  if (w === 1375) {
    // the live update must not rebuild the DOM: the same elements, so the animations never restart
    await page.evaluate(() => { window.__flow = document.querySelector('.holo-flow'); window.__card = document.querySelector('.holo-node'); window.__packet = document.querySelector('.holo-packet animateMotion'); });
    await sleep(7000);
    const same = await page.evaluate(() => ({ flow: window.__flow === document.querySelector('.holo-flow') && document.body.contains(window.__flow), card: window.__card === document.querySelector('.holo-node'), packet: !!document.querySelector('.holo-packet animateMotion') && window.__packet === document.querySelector('.holo-packet animateMotion') }));
    check('1375x856: a live update keeps the same elements, so line and packet animations keep running', same.flow && same.card && same.packet, JSON.stringify(same));
    // clicking a card selects it and the detail panel follows
    await page.locator('.holo-node button').first().click();
    await sleep(500);
    const pressed = await page.evaluate(() => ({ n: document.querySelectorAll('.holo-node button[aria-pressed="true"]').length, detail: !!document.querySelector('#topology-detail .endpoint-card, #topology-detail .tile, #topology-detail > *') }));
    check('1375x856: clicking a card selects it and shows its details', pressed.n === 1 && pressed.detail, JSON.stringify(pressed));
    // text growing later (a longer name arrives) re-routes the lines
    await page.evaluate(() => { const n = document.querySelector('.holo-now'); n.style.webkitLineClamp = 'unset'; n.style.maxHeight = 'none'; });
    await sleep(600);
    const after = await page.evaluate(measure);
    check('1375x856: a card that suddenly grows taller still overlaps nothing and its line follows', after.overlaps === 0 && after.linesOnEdges === 6, JSON.stringify({ o: after.overlaps, l: after.linesOnEdges }));
  }
  await context.close();
}

// tablets and phones, both ways up: the stacked layout, no map
for (const [pw, ph] of [[800, 900], [390, 844], [844, 390], [979, 800]]) {
  const device = await browser.newContext({ viewport: { width: pw, height: ph }, deviceScaleFactor: 2, serviceWorkers: 'block' });
  const devicePage = await device.newPage();
  await devicePage.goto(BASE + '/topology');
  await devicePage.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
  await sleep(1200);
  const q = await devicePage.evaluate(() => ({ map: getComputedStyle(document.querySelector('.holo-stage')).display, stack: getComputedStyle(document.querySelector('.topology-stack')).display, cards: document.querySelectorAll('.stack-node').length, sideways: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1, broken: Array.from(document.querySelectorAll('.stack-name')).filter(n => n.scrollWidth > n.clientWidth + 1).length }));
  check(`${pw}x${ph}: the stacked layout shows all six playouts, no map, no sideways scrolling`, q.map === 'none' && q.stack === 'block' && q.cards === 6 && !q.sideways && q.broken === 0, JSON.stringify(q));
  if (pw === 800) await devicePage.screenshot({ path: path.join(out, 'deck-800-stack.png') });
  await device.close();
}
const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, serviceWorkers: 'block' });
const phonePage = await phone.newPage();
await phonePage.goto(BASE + '/topology');
await phonePage.waitForSelector('#preloader.done', { state: 'attached', timeout: 20000 });
await sleep(1500);
const p = await phonePage.evaluate(() => ({
  map: getComputedStyle(document.querySelector('.holo-stage')).display, stack: getComputedStyle(document.querySelector('.topology-stack')).display,
  cards: document.querySelectorAll('.stack-node').length, sideways: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
}));
check('390x844 phone: the map is hidden and the stacked layout shows all six playouts without sideways scrolling', p.map === 'none' && p.stack === 'block' && p.cards === 6 && !p.sideways, JSON.stringify(p));
await phonePage.screenshot({ path: path.join(out, 'deck-phone.png') });
await phone.close();

clearInterval(beating);
await browser.close();
await new Promise(resolve => { const k = spawn('taskkill', ['/PID', String(server.pid), '/T', '/F'], { stdio: 'ignore' }); k.on('exit', resolve); });
const failed = checks.filter(c => !c.ok);
console.log(JSON.stringify({ total: checks.length, failed: failed.length }));
process.exit(failed.length ? 1 : 0);
