# File:         test_matrix_tiles.py
# Purpose:      Tests of the three MATRIX SOURCE TILES on the main dashboard (core-listener/app/matrix_pages.py as put into dashboard.py): the three counters that were taken out are gone, the
#               tiles sit directly above the Site C ZIXI Feed card, everyone SEES them, and they only WORK for the super user on the staff door of the standby. Anyone else can click
#               and nothing happens: no request, no message, no warning. The tiles' real script is run in Node against a fake page and a fake server.
# Inputs:       None (a fake document, a fake fetch, status views made by the real matrix.Switcher over a fake matrix). Nothing leaves this machine.
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/dashboard.py, matrix_pages.py, matrix.py, ops/matrix_fixtures.py, ops/test_matrix_panel.py (its fake page and helpers), and Node (a missing Node is a failure).
"""Run: python ops/test_matrix_tiles.py

What is proven here, in plain words (the unpublished design notes section 10): the dashboard shows Playouts healthy, Since last critical alarm and Critical alarms 24 h, and no longer Active alarms, Events
24 h or Heartbeats per minute; three matrix tiles (MAIN PC, BACKUP PC, BRIGHTSIGN PLAYER) sit right above the Site C ZIXI Feed card; a visitor who is not the signed-in super user on the
staff door sees the tiles but a click does nothing at all and nothing is said; the super user on the door sees the live state, can press, and the emergency tile asks first.
"""
import importlib
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import matrix_fixtures as fx  # noqa: E402
import test_matrix_panel as panel  # noqa: E402

dashboard = importlib.import_module("app.dashboard")
pages = importlib.import_module("app.matrix_pages")
PAGE = dashboard.render_dashboard_html("0" * 64)
TOPOLOGY = dashboard.render_topology_html("0" * 64)
def status_for(routing, **changes):
    """A view as the server answers the page: the matrix status with the keys the real endpoint adds (enabled, core_reached, last_success)."""
    view = panel.status_for(routing, **changes)
    view["enabled"] = True
    return view


ENDPOINT = "/control/panel/matrix"

PRELUDE = r"""
var ids = {}, timers = [], confirms = [], calls = [], confirmAnswer = true, responses = [], deferreds = [];
function Fake(tag) { this.tag = tag; this.children = []; this._t = ""; this.className = ""; this.hidden = false; this.disabled = false; this.attrs = {}; this.listeners = {}; this.style = {}; }
Fake.prototype.appendChild = function (child) { this.children.push(child); return child; };
Fake.prototype.setAttribute = function (name, value) { this.attrs[name] = String(value); };
Fake.prototype.getAttribute = function (name) { return this.attrs[name] === undefined ? null : this.attrs[name]; };
Fake.prototype.addEventListener = function (name, fn) { this.listeners[name] = fn; };
Object.defineProperty(Fake.prototype, "textContent", {get: function () { return this._t; }, set: function (v) { this._t = String(v); this.children = []; }});
var document = {getElementById: function (id) { if (!ids[id]) { ids[id] = new Fake("#" + id); } return ids[id]; }, createElement: function (tag) { return new Fake(tag); }};
function Sync(value, failed) { this.v = value; this.f = !!failed; }
Sync.prototype.then = function (ok, bad) {
  try {
    if (this.f) { return bad ? wrap(bad(this.v)) : this; }
    return ok ? wrap(ok(this.v)) : this;
  } catch (error) { return new Sync(error, true); }
};
Sync.prototype.catch = function (bad) { return this.then(undefined, bad); };
function wrap(result) { return result && typeof result.then === "function" ? result : new Sync(result); }
function Deferred() { this.cbs = []; this.done = false; this.v = undefined; this.f = false; }
Deferred.prototype.settle = function (value, failed) { this.v = value; this.f = !!failed; this.done = true; var list = this.cbs; this.cbs = []; list.forEach(function (fire) { fire(); }); };
Deferred.prototype.then = function (ok, bad) {
  var child = new Deferred(), self = this;
  function fire() {
    try {
      var out = self.f ? (bad ? bad(self.v) : null) : (ok ? ok(self.v) : self.v);
      var failed = self.f && !bad;
      if (!failed && out && typeof out.then === "function") { out.then(function (v) { child.settle(v, false); }, function (e) { child.settle(e, true); }); } else { child.settle(failed ? self.v : out, failed); }
    } catch (error) { child.settle(error, true); }
  }
  if (this.done) { fire(); } else { this.cbs.push(fire); }
  return child;
};
Deferred.prototype.catch = function (bad) { return this.then(undefined, bad); };
function reply(next) { return {status: next.status, json: function () { return next.badjson ? new Sync(new Error("not json"), true) : new Sync(next.data); }}; }
function fetch(path, options) {
  calls.push({path: path, options: options || {}});
  var next = responses.shift();
  if (next === "network") { return new Sync(new Error("network"), true); }
  if (next === "defer") { var late = new Deferred(); deferreds.push(late); return late; }
  return new Sync(reply(next || {status: 404, data: {error: "not found"}}));
}
function confirm(text) { confirms.push(text); return confirmAnswer; }
function setTimeout(fn, ms) { timers.push({fn: fn, ms: ms}); return timers.length; }
function clearTimeout() {}
var KEYS = ["main", "backup", "brightsign"];
function notes() { return KEYS.map(function (k) { return ids["mx-note-" + k].textContent; }); }
function look() { return KEYS.map(function (k) { var t = ids["mx-tile-" + k]; return {cls: t.className, disabled: t.getAttribute("aria-disabled"), note: ids["mx-note-" + k].textContent}; }); }
function press(key) { ids["mx-tile-" + key].listeners.click(); }
"""
# What the real page ships with: three inert buttons whose class, notes and aria-disabled are in the markup. The script must leave all of it alone for anyone who is not allowed.
INIT = r"""
KEYS.forEach(function (k) { var t = document.getElementById("mx-tile-" + k); t.className = "counter mx-tile"; t.setAttribute("aria-disabled", "true"); });
document.getElementById("mx-note-main").textContent = "Normal operation";
document.getElementById("mx-note-backup").textContent = "Backup operation";
document.getElementById("mx-note-brightsign").textContent = "Emergency operation";
"""
RUNNER = panel.RUNNER


def node(calls, setup=""):
    binary = shutil.which("node")
    if not binary:
        raise AssertionError("Node is not installed: the tiles cannot be run, which is a failure and not a skip")
    program = PRELUDE + "\n" + pages.TILES_SCRIPT + "\n" + INIT + "\n" + setup
    done = subprocess.run([binary, "-e", RUNNER], input=json.dumps({"code": program, "calls": calls}), capture_output=True, text=True, timeout=60, encoding="utf-8")
    if done.returncode != 0:
        raise AssertionError("node failed: " + done.stderr[-1200:])
    return json.loads(done.stdout)


def ok(view):
    return {"status": 200, "data": view}


INITIAL_NOTES = ["Normal operation", "Backup operation", "Emergency operation"]


def css_rules(css):
    """Plain CSS text as a list of (selector, declarations), comments dropped, whitespace squeezed. A rule inside an @media block is listed on its own."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(" ".join(selector.split()), " ".join(body.split())) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


class PageTests(unittest.TestCase):
    def test_the_three_counters_the_owner_removed_are_gone_from_markup_and_script(self):
        for text in ('id="counter-alarms"', 'id="counter-events24"', 'id="counter-beats"', "Active alarms", "Events, 24 h", "Heartbeats per minute", "counter-alarms", "counter-events24", "counter-beats"):
            self.assertNotIn(text, PAGE, text)

    def test_the_three_that_stay_are_still_there_and_still_filled_in(self):
        for element_id in ("counter-healthy", "counter-since", "counter-red24"):
            self.assertIn(f'id="{element_id}"', PAGE)
        for call in ('setCounter("counter-healthy"', 'setCounter("counter-red24"'):
            self.assertIn(call, PAGE)

    def test_the_tiles_sit_directly_above_the_site_c_card_and_below_the_overview(self):
        tiles, site_c = PAGE.index('id="matrix-tiles"'), PAGE.index('id="vz-panel"')
        aside_end = PAGE.index("</aside>", PAGE.index('id="vitals"')) + len("</aside>")
        self.assertEqual(" ".join(PAGE[aside_end:tiles].split()), "</section> <section", "the tiles come straight after the overview section closes")
        self.assertEqual(" ".join(PAGE[PAGE.index("</section>", tiles) + len("</section>"):site_c].split()), "<details", "nothing sits between the tiles and the Site C card")

    def test_there_are_exactly_three_tiles_for_the_three_presets_each_a_real_button_inert_until_proven(self):
        section = PAGE[PAGE.index('id="matrix-tiles"'):PAGE.index('id="vz-panel"')]
        self.assertEqual(section.count("<button"), 3)
        for key, title in (("main", "MAIN PC"), ("backup", "BACKUP PC"), ("brightsign", "BRIGHTSIGN PLAYER")):
            self.assertIn(f'id="mx-tile-{key}"', section)
            self.assertIn(f'data-preset="{key}"', section)
            self.assertIn(title, section)
        self.assertEqual(section.count('aria-disabled="true"'), 3)

    def test_the_tiles_are_hidden_on_the_topology_view_like_the_counters(self):
        body = PAGE[PAGE.index("function setPageMode()"):]
        body = body[:body.index("\n  }\n") + 4]
        self.assertIn('document.getElementById("matrix-tiles").hidden = isTopology;', body)

    def test_the_page_starts_the_tiles_and_carries_their_exact_script(self):
        self.assertIn(pages.TILES_SCRIPT, PAGE)
        self.assertIn("mxTilesStart();", PAGE)
        self.assertIn(pages.TILES_STYLE, PAGE)
        self.assertIn(pages.TILES_HTML, PAGE)
        self.assertNotIn("__MATRIX", PAGE)

    def test_the_public_page_holds_no_address_no_wiring_and_no_input_name(self):
        for needle in ("192.168", "WATCHDOG_MATRIX", "Main Server", "Backup Server", "BrightSign Player", "outputs 1", "1v1"):
            self.assertNotIn(needle, PAGE, needle)

    def test_the_tiles_are_built_with_text_only_and_ask_the_one_endpoint(self):
        for token in panel.FORBIDDEN_SCRIPT:
            self.assertNotIn(token, pages.TILES_SCRIPT, token)
        self.assertIn(ENDPOINT, pages.TILES_SCRIPT)
        self.assertNotIn("console.", pages.TILES_SCRIPT)                          # an unauthorised click must not even log a warning
        self.assertNotIn("alert(", pages.TILES_SCRIPT)

    def test_the_whole_dashboard_script_still_compiles(self):
        script = PAGE[PAGE.index("<script>") + len("<script>"):PAGE.rindex("</script>")]
        done = subprocess.run([shutil.which("node"), "-e", "new (require('vm').Script)(require('fs').readFileSync(0, 'utf8'))"], input=script, capture_output=True, text=True, timeout=30, encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stderr[-600:])

    def test_the_page_is_still_ascii_and_free_of_the_forbidden_characters(self):
        self.assertTrue(PAGE.isascii())
        self.assertNotIn(chr(0x2014), PAGE)
        self.assertNotIn(chr(0xA7), PAGE)

    def test_the_topology_page_gets_the_same_template_so_it_has_the_tiles_hidden_by_the_mode(self):
        self.assertIn('id="matrix-tiles"', TOPOLOGY)


class ActiveIsRedTests(unittest.TestCase):
    """His request of 2026-11-10: whichever source is active shows in RED, so everyone at the desk can see which one is on air. Red must then mean that and nothing else on the tiles."""

    RULES = css_rules(pages.TILES_STYLE)

    def body(self, selector):
        found = [body for rule, body in self.RULES if rule == selector]
        self.assertEqual(len(found), 1, f"{selector!r} must be styled by exactly one rule")
        return found[0]

    def test_the_active_tile_is_red_in_its_edge_its_tint_its_name_and_its_word(self):
        self.assertIn("border-color:var(--red)", self.body("button.mx-tile.mx-on"))
        self.assertRegex(self.body("button.mx-tile.mx-on"), r"background:[^;]*var\(--red-rgb\)")     # a red tint over the whole tile, not only a thin edge
        self.assertIn("color:var(--red)", self.body("button.mx-tile.mx-on .counter-value b"))
        self.assertIn("color:var(--red)", self.body("button.mx-tile.mx-on .counter-note"))

    def test_nothing_on_the_tiles_is_green_any_more(self):
        self.assertNotIn("--green", pages.TILES_STYLE)

    def test_red_belongs_to_the_active_tile_alone(self):
        for selector, body in self.RULES:
            if "mx-on" not in selector:
                self.assertNotIn("--red", body, selector)

    def test_a_failed_switch_is_orange_so_it_cannot_be_taken_for_the_source_on_air(self):
        for selector in ("button.mx-tile.mx-error", "button.mx-tile.mx-error .counter-note"):
            self.assertIn("var(--orange)", self.body(selector))

    def test_the_red_is_never_the_only_signal_the_word_ACTIVE_is_printed_as_well(self):
        for routing, index in ((fx.MAIN_ROUTING, 0), (fx.BACKUP_ROUTING, 1), ((1, 1, 1, 1, 1, 1, 7, 7), 2)):
            with self.subTest(index=index):
                shown = node([f"responses.push({json.dumps(ok(status_for(routing)))})", "mxTilesStart()", "look()"])[-1]
                self.assertEqual([("mx-on" in item["cls"]) for item in shown], [position == index for position in range(3)])     # red on the active tile and on no other
                self.assertEqual(shown[index]["note"], "ACTIVE")

    def test_the_control_panel_card_agrees_with_the_tiles_red_for_active_and_orange_for_a_failed_switch(self):
        rules = dict(css_rules(pages.CARD_STYLE))
        self.assertIn("#ff4d5e", rules["button.mx-btn.mx-active"])
        self.assertIn("#ff4d5e", rules["button.mx-btn.mx-active .mx-word"])
        self.assertNotIn("--good", rules["button.mx-btn.mx-active"] + rules["button.mx-btn.mx-active .mx-word"])
        for selector in ("button.mx-btn.mx-error", "button.mx-btn.mx-error .mx-word"):
            self.assertIn("#ff9a2e", rules[selector])
            self.assertNotIn("--bad", rules[selector])


class InertForEveryoneElseTests(unittest.TestCase):
    """A visitor who is not the super user on the staff door: the tiles show, a click does nothing, and nothing is said."""

    def start(self, first_answer, extra=()):
        push = "responses.push('network')" if first_answer == "network" else "responses.push(" + json.dumps(first_answer) + ")"
        return node(["look()", push, "mxTilesStart()", *extra])

    def test_the_tiles_look_the_same_before_and_after_a_refused_poll(self):
        for answer in ({"status": 404, "data": {"error": "not found"}}, {"status": 401, "data": {"error": "sign in first"}}, {"status": 403, "data": {}},
                       {"status": 503, "data": {"error": "service temporarily unavailable"}}, {"status": 429, "data": {}}, {"status": 200, "data": {"unrelated": True}}, "network"):
            with self.subTest(answer=answer):
                out = self.start(answer, ["look()", "calls.length", "timers.map(function (t) { return t.ms; })"])
                shipped, shown = out[0], out[-3]
                self.assertEqual([item["note"] for item in shipped], INITIAL_NOTES)
                self.assertEqual(shown, shipped)                                            # not one class, note or attribute differs from what the page shipped with
                self.assertTrue(all(item["disabled"] == "true" for item in shown))
                self.assertEqual(out[-2], 1)
                self.assertEqual(out[-1], [60000])                                          # a stranger's page asks again only once a minute

    def test_a_click_by_anyone_else_sends_nothing_says_nothing_and_changes_nothing(self):
        out = node(["look()", "responses.push({status: 404, data: {}})", "mxTilesStart()", "press('main'); press('backup'); press('brightsign'); look()", "calls.length", "confirms.length"])
        self.assertEqual(out[-3], out[0])                                                   # not one class, note or attribute differs after three clicks
        self.assertEqual((out[-2], out[-1]), (1, 0))                                         # only the poll went out; no confirmation was even asked

    def test_nothing_that_sounds_like_a_warning_appears_anywhere_after_clicks_by_anyone_else(self):
        out = node(["responses.push({status: 401, data: {error: 'sign in first'}})", "mxTilesStart()", "press('brightsign'); press('main'); Object.keys(ids).map(function (k) { return ids[k].textContent; }).join(' | ').toLowerCase()"])
        for word in ("denied", "unauthor", "not authoris", "forbidden", "refused", "sign in", "error", "warning", "not allowed", "permission", "failed"):
            self.assertNotIn(word, out[-1], word)

    def test_a_press_that_is_refused_after_the_page_thought_it_was_allowed_goes_quiet_without_a_word(self):
        live = ok(status_for(fx.LIVE_ROUTING))
        refused = {"status": 401, "data": {"error": "sign in first"}}
        out = node(["look()", f"responses.push({json.dumps(live)}, {json.dumps(refused)})", "mxTilesStart()", "press('main')", "look()", "timers.map(function (t) { return t.ms; })",
                    "[mxT.authorized, mxT.view, calls.length]"])
        self.assertEqual(out[-3], out[0])                                                   # exactly what the page shipped with: inert again, every note restored, not a word said
        self.assertTrue(all("mx-error" not in item["cls"] for item in out[-3]))
        self.assertEqual(out[-2][-1], 60000)                                                # the latest timer: back to asking once a minute, like any stranger
        self.assertEqual(out[-1], [False, None, 2])                                         # the page itself knows it is not allowed, and sent only the poll and the press: no fresh read behind the refusal
        for status in (403, 404):
            gone = {"status": status, "data": {}}
            again = node(["look()", f"responses.push({json.dumps(live)}, {json.dumps(gone)})", "mxTilesStart()", "press('backup')", "look()"])
            self.assertEqual(again[-1], again[0], status)

    def test_the_click_guard_holds_on_its_own_even_if_a_view_were_somehow_still_held(self):
        # Defence in depth: the page never keeps a view for someone who is not allowed, but the click guard must not depend on that. A held view with the allowed flag off must send nothing.
        live = ok(status_for(fx.LIVE_ROUTING))
        out = node([f"responses.push({json.dumps(live)})", "mxTilesStart()", "calls.length", "mxT.authorized = false; press('main'); press('backup'); press('brightsign'); [calls.length, confirms.length]"])
        self.assertEqual(out[-2], 1)
        self.assertEqual(out[-1], [1, 0])                                                   # no request and no question

    def test_a_network_failure_on_the_poll_also_leaves_the_tiles_inert_and_quiet(self):
        out = node(["look()", "responses.push('network')", "mxTilesStart()", "look()", "press('main'); calls.length"])
        self.assertEqual(out[-2], out[0])
        self.assertEqual(out[-1], 1)


class AuthorisedTests(unittest.TestCase):
    """The super user on the staff door: the poll answers 200 with the matrix's view."""

    def start(self, view, extra=(), more_responses=()):
        pushes = ", ".join(json.dumps(item) if item != "network" and item != "defer" else repr(item) for item in (ok(view), *more_responses))
        return node([f"responses.push({pushes})", "mxTilesStart()", *extra])

    def test_the_live_state_shows_on_the_tiles_and_they_can_be_pressed(self):
        out = self.start(status_for(fx.LIVE_ROUTING), ["look()", "timers.map(function (t) { return t.ms; })"])
        shown = out[-2]
        self.assertEqual([item["note"] for item in shown], ["Press to switch"] * 3)
        self.assertTrue(all(item["disabled"] == "false" and "mx-ready" in item["cls"] for item in shown))
        self.assertEqual(out[-1], [5000])                                                    # an authorised page keeps the state fresh every five seconds

    def test_the_active_source_says_ACTIVE_is_marked_and_cannot_be_pressed_again(self):
        shown = self.start(status_for(fx.MAIN_ROUTING), ["look()"])[-1]
        self.assertEqual((shown[0]["note"], shown[0]["disabled"], "mx-on" in shown[0]["cls"]), ("ACTIVE", "true", True))
        self.assertEqual([item["note"] for item in shown[1:]], ["Press to switch"] * 2)

    def test_an_unreachable_matrix_or_core_is_said_in_words_and_no_tile_is_active(self):
        fake = fx.FakeMatrix(fx.MAIN_ROUTING)
        fake.offline = True
        matrix_view = fx.matrix.Switcher(fake, sleep=lambda s: None).status()
        matrix_view.update(core_reached=True, enabled=True)
        shown = self.start(matrix_view, ["look()"])[-1]
        self.assertEqual([item["note"] for item in shown], ["not reached"] * 3)
        self.assertTrue(all(item["disabled"] == "true" and "mx-on" not in item["cls"] for item in shown))
        shown = self.start({"enabled": True, "core_reached": False, "error": "Core was not reached."}, ["look()"])[-1]
        self.assertEqual([item["note"] for item in shown], ["not reached"] * 3)

    def test_pressing_main_sends_the_preset_and_confirm_false_to_the_one_endpoint(self):
        done = ok({"ok": True, "state": "done", "message": "MAIN PC is active.", "status": status_for(fx.MAIN_ROUTING)})
        out = self.start(status_for(fx.LIVE_ROUTING), ["press('main')", "calls.map(function (c) { return [c.path, c.options.method, c.options.body ? JSON.parse(c.options.body) : null]; })", "confirms.length"], [done])
        self.assertEqual(out[-2][-1], [ENDPOINT, "POST", {"preset": "main", "confirm": False}])
        self.assertEqual(out[-1], 0)

    def test_the_emergency_tile_asks_first_and_a_cancel_sends_nothing(self):
        out = self.start(status_for(fx.MAIN_ROUTING), ["confirmAnswer = false; press('brightsign'); calls.length", "confirms.length", "confirms[0]"])
        self.assertEqual((out[-3], out[-2]), (1, 1))                                         # only the poll; one question
        self.assertIn("Put BRIGHTSIGN PLAYER on air?", out[-1])

    def test_the_emergency_tile_after_ok_sends_confirm_true(self):
        done = ok({"ok": True, "state": "done", "message": "ok", "status": status_for((1, 1, 1, 1, 1, 1, 7, 7))})
        out = self.start(status_for(fx.MAIN_ROUTING), ["press('brightsign')", "JSON.parse(calls[calls.length - 1].options.body)", "look()"], [done])
        self.assertEqual(out[-2], {"preset": "brightsign", "confirm": True})
        self.assertEqual(out[-1][2]["note"], "ACTIVE")

    def test_the_tile_shows_the_read_back_after_a_press_and_locks_for_three_seconds(self):
        done = ok({"ok": True, "state": "done", "message": "ok", "status": status_for(fx.MAIN_ROUTING)})
        out = self.start(status_for(fx.LIVE_ROUTING), ["press('main')", "look()", "timers.filter(function (t) { return t.ms === 3000; }).length", "timers.filter(function (t) { return t.ms === 3000; })[0].fn(); look()"], [done])
        during, locks, after = out[-3], out[-2], out[-1]
        self.assertEqual(during[0]["note"], "ACTIVE")
        self.assertTrue(all(item["disabled"] == "true" for item in during))                  # active, and the others are locked by the cool-down
        self.assertEqual(locks, 1)
        self.assertEqual([item["disabled"] for item in after], ["true", "false", "false"])

    def test_pressing_the_tile_that_is_already_active_sends_nothing(self):
        out = self.start(status_for(fx.MAIN_ROUTING), ["press('main'); calls.length", "confirms.length"])
        self.assertEqual((out[-2], out[-1]), (1, 0))                                         # only the poll

    def test_a_press_during_the_three_second_lock_after_a_switch_sends_nothing(self):
        done = ok({"ok": True, "state": "done", "message": "ok", "status": status_for(fx.MAIN_ROUTING)})
        out = self.start(status_for(fx.LIVE_ROUTING), ["press('main'); calls.length", "press('backup'); press('brightsign'); calls.length", "confirms.length"], [done])
        self.assertEqual(out[-3], 2)                                                         # the poll and the one press
        self.assertEqual(out[-2], 2)                                                         # nothing more while locked
        self.assertEqual(out[-1], 0)

    def test_a_failed_switch_marks_its_tile_with_the_outputs_and_never_active(self):
        failed = ok({"ok": False, "state": "failed", "message": "x", "failed_outputs": [{"output": 5, "wanted": 2, "got": 7}], "status": status_for(fx.LIVE_ROUTING)})
        shown = self.start(status_for(fx.LIVE_ROUTING), ["press('main')", "look()"], [failed])[-1]
        self.assertEqual(shown[0]["note"], "ERROR: output 5 shows input 7")
        self.assertIn("mx-error", shown[0]["cls"])
        self.assertTrue(all("mx-on" not in item["cls"] for item in shown))

    def test_a_second_press_while_one_runs_sends_nothing(self):
        out = self.start(status_for(fx.LIVE_ROUTING), ["responses.push('defer'); press('main'); press('backup'); press('main'); calls.length", "deferreds.length"])
        self.assertEqual(out[-2], 2)                                                         # the poll and ONE press
        self.assertEqual(out[-1], 1)

    def test_a_poll_already_in_flight_cannot_repaint_the_state_from_before_a_press(self):
        old, new = status_for(fx.LIVE_ROUTING), status_for(fx.MAIN_ROUTING)
        done = ok({"ok": True, "state": "done", "message": "ok", "status": new})
        out = self.start(old, ["responses.push('defer'); timers[0].fn(); deferreds.length", f"responses.push({json.dumps(done)}); press('main'); look()[0].note",
                               f"deferreds[0].settle(reply({json.dumps(ok(old))}), false); look()[0].note"])
        self.assertEqual(out[-3], 1)
        self.assertEqual(out[-2], "ACTIVE")
        self.assertEqual(out[-1], "ACTIVE")                                                  # the late, older answer was dropped

    def test_an_answer_with_no_read_back_clears_the_state_and_asks_for_a_fresh_reading(self):
        unreachable = {"status": 200, "data": {"ok": False, "state": "core_unreachable", "message": "may or may not have switched"}}
        out = self.start(status_for(fx.MAIN_ROUTING), ["press('backup'); calls[calls.length - 1].path", "look().map(function (i) { return i.note; })"], [unreachable, "defer"])
        self.assertEqual(out[-2], ENDPOINT + "?fresh=1")
        self.assertNotIn("ACTIVE", out[-1])                                                  # no green MAIN PC while nobody knows what the matrix shows

    def test_a_poll_never_piles_up_behind_one_that_is_still_running(self):
        out = node(["responses.push('defer'); mxTilesStart(); calls.length", "mxTilesLoad(false); mxTilesLoad(false); calls.length"])
        self.assertEqual(out, [1, 1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
