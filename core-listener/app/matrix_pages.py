# File:         matrix_pages.py
# Purpose:      The AV MATRIX pieces of two pages, as plain text: (1) the CARD of the control panel (CARD_STYLE, CARD_HTML, CARD_SCRIPT: three preset buttons, the eight outputs, a progress bar,
#               the last successful switch) handed to control_access.panel_page(); (2) the three MATRIX SOURCE TILES of the main dashboard (TILES_STYLE, TILES_HTML, TILES_SCRIPT: MAIN PC,
#               BACKUP PC, BRIGHTSIGN PLAYER, right above the Site C ZIXI Feed card) handed to dashboard.py. SHARED_SCRIPT holds the display rules both use, so they can never disagree.
# Inputs:       Nothing at run time here: these are text constants. Both pages ask /control/panel/matrix themselves. The server answers it ONLY for the signed-in super user (from any
#               address) and for an office PC (a request on the standby's staff door, no sign-in); for everyone else it answers 404, 401, 429 or 503, and the dashboard tiles then stay
#               exactly as shipped (a click does nothing and nothing is said).
# Outputs:      The constants named above (no secret, no address, no input name, no outside resource).
# Dependencies: None. The card's script uses the panel script's own helpers (api, el); the tiles' script uses only fetch, confirm and setTimeout.
CARD_STYLE = """
  .mx-groups { display:grid; grid-template-columns:repeat(auto-fit, minmax(14rem, 1fr)); gap:14px; margin:0 0 14px; }
  .mx-group { border:1px solid var(--line); border-radius:12px; padding:12px; }
  .mx-label { color:var(--muted); font-size:.8rem; letter-spacing:.06em; margin:0 0 8px; font-weight:600; }
  button.mx-btn { width:100%; min-height:96px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px; font-size:1.15rem; background:#17233a; color:var(--text); border:2px solid #3a4b6b; }
  button.mx-btn .mx-word { font-size:.85rem; font-weight:600; color:var(--muted); }
  button.mx-btn.mx-active { background:#3a1218; border-color:#ff4d5e; }
  button.mx-btn.mx-active .mx-word { color:#ff4d5e; }
  button.mx-btn.mx-switching { border-color:#ffd27a; }
  button.mx-btn.mx-switching .mx-word { color:#ffd27a; }
  button.mx-btn.mx-error { border-color:#ff9a2e; }
  button.mx-btn.mx-error .mx-word { color:#ff9a2e; }
  button.mx-btn.mx-unavailable { opacity:.55; }
  button.mx-btn:disabled.mx-active, button.mx-btn:disabled.mx-switching, button.mx-btn:disabled.mx-error { opacity:1; cursor:default; }
  .mx-routes { color:var(--muted); font-size:.85rem; margin:8px 0 0; }
  .mx-progress { height:6px; border-radius:3px; background:#22304a; overflow:hidden; margin:0 0 12px; }
  .mx-progress > span { display:block; height:100%; width:35%; background:#ffd27a; animation:mx-slide 1.1s linear infinite; }
  @keyframes mx-slide { from { transform:translateX(-100%); } to { transform:translateX(300%); } }
  #mx-line { font-weight:600; color:var(--text); }
"""

CARD_HTML = """
  <div class="card" id="matrix" hidden>
    <h2>AV Matrix Control</h2>
    <p id="mx-line" role="status" aria-live="polite">Reading the matrix ...</p>
    <div id="mx-progress" class="mx-progress" role="progressbar" aria-label="Switching in progress" hidden><span></span></div>
    <div id="mx-groups" class="mx-groups"></div>
    <h3>What the matrix shows now (output and the input it carries)</h3>
    <table><thead><tr><th scope="col">Output</th><th scope="col">Carries input</th><th scope="col">Source</th></tr></thead><tbody id="mx-outputs"></tbody></table>
    <p id="mx-last" class="hint"></p>
    <button id="mx-refresh" type="button" class="small quiet">Refresh</button>
    <p id="mx-msg" role="status" aria-live="polite"></p>
  </div>
"""

# The display rules both pages use (pure functions, no page, no network). One button's state: not set or not reached (never shown as active) > a switch running > the last press failed >
# active > inactive. The word is printed, so colour is never the only signal.
SHARED_SCRIPT = r"""  function mxList(numbers) { return (numbers.length === 1 ? "output " : "outputs ") + numbers.join(", "); }
  function mxRoutesText(preset) {
    if (!preset.routes || !preset.routes.length) { return "outputs not set yet"; }
    return preset.routes.map(function (route) { return route.input_name + " to " + mxList(route.outputs); }).join("; ");
  }
  function mxWhen(iso) {
    var found = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/.exec(String(iso || ""));
    return found ? found[1] + " " + found[2] + " UTC" : "an unknown time";
  }
  function mxLastText(last) {
    if (!last) { return "No successful switch is recorded yet."; }
    return "Last successful switch: " + String(last.preset || "?").toUpperCase() + ", by " + (last.operator || "?") + ", at " + mxWhen(last.at) + ".";
  }
  function mxConfirmText(preset) {
    return "Put " + preset.title + " on air? This sends " + mxRoutesText(preset) + ". Press OK only if this is an emergency.";
  }
  function mxOutputText(row) { return row.input === null ? "unknown" : "input " + row.input; }
  function mxHeadline(view) {
    if (!view) { return "Reading the matrix ..."; }
    if (view.core_reached === false) { return "Core was not reached. The matrix state is unknown, so no preset is shown as active."; }
    if (view.configured === false) { return view.error || "Core has no matrix connected."; }
    if (view.reachable === false) { return "The matrix did not answer. Its state is unknown, so no preset is shown as active."; }
    if (view.switching) { return "A switch is running ..."; }
    if (view.reachable !== true) { return view.error || "The matrix state is unknown."; }
    var active = (view.presets || []).filter(function (item) { return item.active; })[0];
    return active ? "Active now: " + active.title : "No preset matches the matrix right now. The outputs below are what it really shows.";
  }
  function mxButtonState(preset, view, ctx) {
    if (!preset.configured) { return {state: "unavailable", word: "outputs not set yet", disabled: true}; }
    if (!view || view.core_reached === false || view.configured === false || view.reachable === false) { return {state: "unavailable", word: "not reached", disabled: true}; }
    if (ctx.busy) { return ctx.pressed === preset.key ? {state: "switching", word: "SWITCHING ...", disabled: true} : {state: "inactive", word: "wait", disabled: true}; }
    if (view.switching) { return {state: "switching", word: "a switch is running", disabled: true}; }
    if (view.reachable !== true) { return {state: "unavailable", word: "not reached", disabled: true}; }
    if (ctx.failed && ctx.failed.key === preset.key && !preset.active) { return {state: "error", word: "ERROR: " + ctx.failed.text, disabled: !!ctx.cooling}; }
    if (preset.active) { return {state: "active", word: "ACTIVE", disabled: true}; }
    return {state: "inactive", word: "not active", disabled: !!ctx.cooling};
  }
  function mxFailureText(data) {
    var items = data.failed_outputs || [];
    if (items.length) { return items.map(function (item) { return "output " + item.output + " shows input " + item.got; }).join(", "); }
    return data.state === "unverified" ? "not confirmed" : "did not apply";
  }
"""

_CARD_HEAD = r"""
  // ---- AV matrix card (the unpublished design notes). It appears only when the panel's state says a matrix is available on this node; the server decides who may use it. ----
  var mxView = null, mxStarted = false, mxBusy = false, mxPressed = "", mxFailed = null, mxCooling = false, mxGen = 0, mxLoading = false;
  // Every press (start and end) and every load bumps mxGen; an answer that arrives for an older request is dropped, so a poll already in flight can never repaint the state from before a press.
  function mxSupersede() { mxGen += 1; mxLoading = false; }
"""

_CARD_REST = r"""  function mxSay(text, bad) { var box = document.getElementById("mx-msg"); box.textContent = text; box.style.color = bad ? "var(--bad)" : "var(--good)"; }
  function mxPaint() {
    var view = mxView, ctx = {busy: mxBusy, pressed: mxPressed, failed: mxFailed, cooling: mxCooling};
    document.getElementById("mx-line").textContent = mxHeadline(view);
    document.getElementById("mx-progress").hidden = !(mxBusy || (view && view.switching));
    var groups = document.getElementById("mx-groups");
    groups.textContent = "";
    ((view && view.presets) || []).forEach(function (preset) {
      var look = mxButtonState(preset, view, ctx), box = el("div", undefined, "mx-group"), button = el("button", undefined, "mx-btn mx-" + look.state);
      box.appendChild(el("div", preset.group, "mx-label"));
      button.type = "button";
      button.disabled = look.disabled;
      button.setAttribute("aria-pressed", look.state === "active" ? "true" : "false");
      button.appendChild(el("span", preset.title));
      button.appendChild(el("span", look.word, "mx-word"));
      button.addEventListener("click", function () { mxPress(preset.key); });
      box.appendChild(button);
      box.appendChild(el("div", mxRoutesText(preset), "mx-routes"));
      groups.appendChild(box);
    });
    var body = document.getElementById("mx-outputs");
    body.textContent = "";
    ((view && view.outputs) || []).forEach(function (row) {
      var line = el("tr");
      line.appendChild(el("th", String(row.output))); line.lastChild.scope = "row";
      line.appendChild(el("td", mxOutputText(row)));
      line.appendChild(el("td", row.input_name));
      body.appendChild(line);
    });
    document.getElementById("mx-last").textContent = view && view.core_reached !== false ? mxLastText(view.last_success) : "";
  }
  function mxLoad(fresh) {
    if (mxBusy || (mxLoading && !fresh)) { return; }       // nothing to read while a press runs; a poll never piles up behind a slow one
    var mine = ++mxGen;
    mxLoading = true;
    api("/control/panel/matrix" + (fresh ? "?fresh=1" : ""), {method: "GET"}).then(function (result) {
      if (mine !== mxGen || mxBusy) { return; }
      if (result.status === 404) { document.getElementById("matrix").hidden = true; return; }
      mxView = result.data;
      mxPaint();
    }).catch(function () { if (mine === mxGen && !mxBusy) { mxView = {core_reached: false}; mxPaint(); } })
      .then(function () { if (mine === mxGen) { mxLoading = false; } });
  }
  function mxPress(key) {
    var preset = ((mxView && mxView.presets) || []).filter(function (item) { return item.key === key; })[0];
    if (!preset || mxBusy) { return; }
    if (preset.needs_confirm && !confirm(mxConfirmText(preset))) { return; }
    var needFresh = true;                                    // unless the answer carries the matrix's own read-back, the old view is not trusted
    mxBusy = true; mxPressed = key; mxFailed = null; mxSupersede();
    mxSay("Switching to " + preset.title + " ... this can take up to about 12 seconds.", false);
    mxPaint();
    api("/control/panel/matrix", {method: "POST", body: JSON.stringify({preset: key, confirm: !!preset.needs_confirm})}).then(function (result) {
      var data = result.data || {};
      mxSay(data.message || data.error || "No answer.", !data.ok);
      if (data.status) { mxView = Object.assign({core_reached: true}, data.status); needFresh = false; }
      if (!data.ok && (data.state === "failed" || data.state === "unverified")) { mxFailed = {key: key, text: mxFailureText(data)}; }
    }).catch(function () { mxSay("The server could not be reached. The matrix may or may not have switched: press Refresh.", true); })
      .then(function () {
        mxBusy = false; mxCooling = true; mxSupersede();
        if (needFresh) { mxView = null; }
        mxPaint();
        if (needFresh) { mxLoad(true); }
        setTimeout(function () { mxCooling = false; mxPaint(); }, 3000);     // the buttons stay locked for three seconds after a switch, against repeated clicks
      });
  }
  function mxShow(open) {
    var card = document.getElementById("matrix");
    card.hidden = !open;
    if (open && !mxStarted) {
      mxStarted = true;
      mxLoad(false);
      setInterval(function () { if (!mxBusy) { mxLoad(false); } }, 5000);
    }
  }
  document.getElementById("mx-refresh").addEventListener("click", function () { mxLoad(true); });
"""

CARD_SCRIPT = _CARD_HEAD + SHARED_SCRIPT + _CARD_REST

# ---- The three matrix source tiles of the main dashboard (the unpublished design notes section 10) -------------------------------------------------------------------
# They look like the counters beside them (they reuse the counter card's classes) and sit right above the Site C ZIXI Feed card. EVERYONE sees them. They WORK only for the signed-in super user
# (from any address) and on an office PC (the standby's staff door, no sign-in): for anyone else a click does nothing at all (no request, no message, no warning, no log line) and the
# tiles stay exactly as shipped.
TILES_STYLE = """
.mx-tiles { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:var(--gap); margin:0 0 var(--gap); }
.mx-tiles[hidden] { display:none; }
button.mx-tile { font:inherit; color:inherit; text-align:left; width:100%; cursor:default; }
button.mx-tile .counter-value b { font-size:clamp(20px, 2.2vw, 28px); font-weight:500; letter-spacing:0; }
button.mx-tile:focus-visible { outline:2px solid var(--text); outline-offset:2px; }
button.mx-tile.mx-ready, button.mx-tile.mx-on, button.mx-tile.mx-error { cursor:pointer; }
/* RED means "this source is on air now" and nothing else on these tiles (his request, 2026-11-10); a failed switch is orange so it can never be taken for the source in use. */
button.mx-tile.mx-on { border-color:var(--red); background:linear-gradient(180deg, rgba(var(--red-rgb),.26), rgba(var(--red-rgb),.12)), rgba(9,14,23,.88); box-shadow:0 0 0 1px rgba(var(--red-rgb),.5), 0 0 24px rgba(var(--red-rgb),.28); }
button.mx-tile.mx-on .counter-value b { color:var(--red); }
button.mx-tile.mx-on .counter-note { color:var(--red); font-weight:700; }
button.mx-tile.mx-switching { border-color:var(--yellow); }
button.mx-tile.mx-switching .counter-note { color:var(--yellow); font-weight:700; }
button.mx-tile.mx-error { border-color:var(--orange); }
button.mx-tile.mx-error .counter-note { color:var(--orange); font-weight:700; }
button.mx-tile.mx-off { opacity:.6; }
@media (max-width:700px) { .mx-tiles { grid-template-columns:1fr; } }
"""

TILES_HTML = """
  <section id="matrix-tiles" class="mx-tiles" aria-label="Matrix sources" aria-live="polite">
    <button type="button" id="mx-tile-main" class="counter mx-tile" data-preset="main" aria-disabled="true"><span class="eyebrow">Matrix source</span><div class="counter-value"><b>MAIN PC</b></div><div class="counter-note" id="mx-note-main">Normal operation</div></button>
    <button type="button" id="mx-tile-backup" class="counter mx-tile" data-preset="backup" aria-disabled="true"><span class="eyebrow">Matrix source</span><div class="counter-value"><b>BACKUP PC</b></div><div class="counter-note" id="mx-note-backup">Backup operation</div></button>
    <button type="button" id="mx-tile-brightsign" class="counter mx-tile" data-preset="brightsign" aria-disabled="true"><span class="eyebrow">Matrix source</span><div class="counter-value"><b>BRIGHTSIGN PLAYER</b></div><div class="counter-note" id="mx-note-brightsign">Emergency operation</div></button>
  </section>
"""

_TILES_HEAD = r"""
  // ---- Matrix source tiles (the unpublished design notes section 10). EVERYONE sees them; they WORK only for the signed-in super user (any address) and on an office PC (the staff door, no sign-in). /control/panel/matrix answers those
  // callers with 200; for anyone else it answers 404, 401, 429 or 503, and the tiles stay exactly as the page shipped them: a click sends nothing and says nothing. ----
  var MX_KEYS = ["main", "backup", "brightsign"];
  var mxT = {view: null, authorized: false, busy: false, pressed: "", failed: null, cooling: false, gen: 0, loading: false, started: false, timer: 0, base: {}};
  // Every press and every load bumps gen; an answer that arrives for an older request is dropped, so a poll already in flight can never repaint the state from before a press.
  function mxTilesSupersede() { mxT.gen += 1; mxT.loading = false; }
"""

_TILES_REST = r"""  function mxTilesFetch(path, options) {
    return fetch(path, Object.assign({credentials: "same-origin", headers: {"Content-Type": "application/json"}}, options)).then(function (reply) {
      return reply.json().then(function (data) { return {status: reply.status, data: data}; }, function () { return {status: reply.status, data: {}}; });
    });
  }
  function mxTilesPaint() {
    var view = mxT.view, ctx = {busy: mxT.busy, pressed: mxT.pressed, failed: mxT.failed, cooling: mxT.cooling};
    MX_KEYS.forEach(function (key) {
      var tile = document.getElementById("mx-tile-" + key), note = document.getElementById("mx-note-" + key);
      if (!mxT.authorized) {                                  // anyone else: exactly what the page shipped with, nothing added
        tile.className = mxT.base[key].cls; note.textContent = mxT.base[key].note; tile.setAttribute("aria-disabled", "true");
        return;
      }
      var preset = ((view && view.presets) || []).filter(function (item) { return item.key === key; })[0];
      var look = !view ? {state: "unavailable", word: "checking ...", disabled: true} : (preset ? mxButtonState(preset, view, ctx) : {state: "unavailable", word: "not reached", disabled: true});
      var mark = look.state === "active" ? "mx-on" : (look.state === "switching" ? "mx-switching" : (look.state === "error" ? "mx-error" : (look.state === "inactive" ? "mx-ready" : "mx-off")));
      tile.className = mxT.base[key].cls + " " + mark;
      tile.setAttribute("aria-disabled", look.disabled ? "true" : "false");
      note.textContent = look.word === "not active" ? "Press to switch" : look.word;
    });
  }
  function mxTilesSchedule() {
    if (mxT.timer) { clearTimeout(mxT.timer); }
    mxT.timer = setTimeout(function () { mxTilesLoad(false); }, mxT.authorized ? 5000 : 60000);     // a page that is not allowed asks only once a minute
  }
  function mxTilesLoad(fresh) {
    if (mxT.busy || (mxT.loading && !fresh)) { return; }
    var mine = ++mxT.gen;
    mxT.loading = true;
    mxTilesFetch("/control/panel/matrix" + (fresh ? "?fresh=1" : ""), {method: "GET"}).then(function (result) {
      if (mine !== mxT.gen || mxT.busy) { return; }
      var allowed = result.status === 200 && !!result.data && result.data.enabled === true;
      mxT.authorized = allowed;
      mxT.view = allowed ? result.data : null;
      mxTilesPaint();
    }).catch(function () {
      if (mine === mxT.gen && !mxT.busy) { mxT.authorized = false; mxT.view = null; mxTilesPaint(); }
    }).then(function () {
      if (mine === mxT.gen) { mxT.loading = false; mxTilesSchedule(); }
    });
  }
  function mxTilesPress(key) {
    if (!mxT.authorized || mxT.busy) { return; }              // anyone else, or a press already running: nothing at all
    var preset = ((mxT.view && mxT.view.presets) || []).filter(function (item) { return item.key === key; })[0];
    if (!preset || mxButtonState(preset, mxT.view, {busy: false, pressed: "", failed: mxT.failed, cooling: mxT.cooling}).disabled) { return; }     // active, locked after a switch, not reached: nothing
    if (preset.needs_confirm && !confirm(mxConfirmText(preset))) { return; }
    var needFresh = true;                                      // unless the answer carries the matrix's own read-back, the old view is not trusted
    mxT.busy = true; mxT.pressed = key; mxT.failed = null; mxTilesSupersede(); mxTilesPaint();
    mxTilesFetch("/control/panel/matrix", {method: "POST", body: JSON.stringify({preset: key, confirm: !!preset.needs_confirm})}).then(function (result) {
      if (result.status === 401 || result.status === 403 || result.status === 404) { mxT.authorized = false; mxT.view = null; needFresh = false; return; }     // no longer allowed: inert again, silently
      var data = result.data || {};
      if (data.status) { mxT.view = Object.assign({core_reached: true}, data.status); needFresh = false; }
      if (!data.ok && (data.state === "failed" || data.state === "unverified")) { mxT.failed = {key: key, text: mxFailureText(data)}; }
    }).catch(function () {}).then(function () {
      mxT.busy = false; mxT.cooling = mxT.authorized; mxTilesSupersede();
      if (needFresh && mxT.authorized) { mxT.view = null; }
      mxTilesPaint();
      if (needFresh && mxT.authorized) { mxTilesLoad(true); } else { mxTilesSchedule(); }
      if (mxT.cooling) { setTimeout(function () { mxT.cooling = false; mxTilesPaint(); }, 3000); }       // locked for three seconds after a switch, against repeated clicks
    });
  }
  function mxTilesStart() {
    if (mxT.started) { return; }
    mxT.started = true;
    MX_KEYS.forEach(function (key) {
      var tile = document.getElementById("mx-tile-" + key);
      mxT.base[key] = {cls: tile.className, note: document.getElementById("mx-note-" + key).textContent};
      tile.addEventListener("click", function () { mxTilesPress(key); });
    });
    mxTilesLoad(false);
  }
"""

TILES_SCRIPT = _TILES_HEAD + SHARED_SCRIPT + _TILES_REST
