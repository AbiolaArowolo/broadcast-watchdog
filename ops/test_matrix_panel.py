# File:         test_matrix_panel.py
# Purpose:      Tests of the AV MATRIX card on the control panel (core-listener/app/matrix_pages.py as put into control_access.panel_page()): the page is built with text only and holds
#               no address or preset name, the whole panel script compiles, and the card's real script is run in Node against a tiny fake page and a fake server: the button states,
#               the headline, the confirmation before the emergency preset, the busy and cool-down locks, and "never show a preset as active unless the matrix said so".
# Inputs:       None (a fake document, a fake fetch, status views made by the real matrix.Switcher over a fake matrix). Nothing leaves this machine.
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/matrix_pages.py, control_access.py, matrix.py, ops/matrix_fixtures.py, and Node (a missing Node is a failure, not a skip).
"""Run: python ops/test_matrix_panel.py"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import matrix_fixtures as fx  # noqa: E402

matrix = fx.matrix
import importlib  # noqa: E402

access = importlib.import_module("app.control_access")
pages = importlib.import_module("app.matrix_pages")
PAGE = access.panel_page()
SCRIPT = PAGE[PAGE.index("<script>") + len("<script>"):PAGE.index("</script>")]
FORBIDDEN_SCRIPT = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "javascript:")


def function_body(name, text=SCRIPT):
    """The text of function `name`, braces matched, so one test reads one function."""
    start = text.index("function " + name + "(")
    depth, index = 0, text.index("{", start)
    first = index
    while True:
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
        index += 1
        if index > len(text) or index - first > 20000:
            raise AssertionError("unbalanced function " + name)


def status_for(routing, **changes):
    """A status view exactly as the server makes it (the real Switcher over a fake matrix), plus whatever a test overrides."""
    view = matrix.Switcher(fx.FakeMatrix(routing), sleep=lambda seconds: None).status()
    view.update({"core_reached": True, "last_success": None}, **{})
    view.update(changes)
    return view


FAKE_DOM = r"""
var ids = {}, timers = [], confirms = [], calls = [], confirmAnswer = true, responses = [], deferreds = [];
function Fake(tag) { this.tag = tag; this.children = []; this._t = ""; this.className = ""; this.hidden = false; this.disabled = false; this.attrs = {}; this.listeners = {}; this.style = {}; this.type = ""; this.scope = ""; }
Fake.prototype.appendChild = function (child) { this.children.push(child); this.lastChild = child; return child; };
Fake.prototype.setAttribute = function (name, value) { this.attrs[name] = value; };
Fake.prototype.addEventListener = function (name, fn) { this.listeners[name] = fn; };
Object.defineProperty(Fake.prototype, "textContent", {get: function () { return this._t; }, set: function (v) { this._t = String(v); this.children = []; }});
var document = {getElementById: function (id) { if (!ids[id]) { ids[id] = new Fake("#" + id); } return ids[id]; }, createElement: function (tag) { return new Fake(tag); }};
function Sync(value, failed) { this.v = value; this.f = !!failed; }
Sync.prototype.then = function (ok, bad) { try { if (this.f) { return bad ? new Sync(bad(this.v)) : this; } return ok ? new Sync(ok(this.v)) : this; } catch (error) { return new Sync(error, true); } };
Sync.prototype.catch = function (bad) { return this.then(undefined, bad); };
function Deferred() { this.cbs = []; this.done = false; this.v = undefined; this.f = false; }
Deferred.prototype.settle = function (value, failed) { this.v = value; this.f = !!failed; this.done = true; var list = this.cbs; this.cbs = []; list.forEach(function (fire) { fire(); }); };
Deferred.prototype.then = function (ok, bad) {
  var child = new Deferred(), self = this;
  function fire() { try { if (self.f) { if (bad) { child.settle(bad(self.v), false); } else { child.settle(self.v, true); } } else { child.settle(ok ? ok(self.v) : self.v, false); } } catch (error) { child.settle(error, true); } }
  if (this.done) { fire(); } else { this.cbs.push(fire); }
  return child;
};
Deferred.prototype.catch = function (bad) { return this.then(undefined, bad); };
function api(path, options) {
  calls.push({path: path, options: options || {}});
  var next = responses.shift();
  if (next === "network") { return new Sync(new Error("network"), true); }
  if (next === "defer") { var late = new Deferred(); deferreds.push(late); return late; }
  return new Sync(next || {status: 200, data: {}});
}
function confirm(text) { confirms.push(text); return confirmAnswer; }
function setTimeout(fn, ms) { timers.push({fn: fn, ms: ms, once: true}); }
function setInterval(fn, ms) { timers.push({fn: fn, ms: ms, once: false}); }
function look() {
  var groups = ids["mx-groups"].children;
  return groups.map(function (box) { var button = box.children[1]; return {label: box.children[0].textContent, title: button.children[0].textContent, word: button.children[1].textContent, cls: button.className, disabled: button.disabled, pressed: button.attrs["aria-pressed"], routes: box.children[2].textContent}; });
}
"""
RUNNER = r"""
const vm = require('vm');
let input = '';
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  const job = JSON.parse(input);
  const context = vm.createContext({JSON: JSON, Object: Object, String: String, Array: Array, Error: Error});
  vm.runInContext(job.code, context);
  process.stdout.write(JSON.stringify(job.calls.map(call => { const value = vm.runInContext(call, context, {timeout: 2000}); return value === undefined ? null : JSON.parse(JSON.stringify(value)); })));
});
"""


def node(calls, code=None):
    """Runs the card's REAL script after a tiny fake page and returns the value of each expression in `calls`. A missing Node is a failure."""
    binary = shutil.which("node")
    if not binary:
        raise AssertionError("Node is not installed: the card cannot be run, which is a failure and not a skip")
    program = FAKE_DOM + "\n" + function_body("el", PAGE[PAGE.index("<script>"):]) + "\n" + (code or pages.CARD_SCRIPT)
    done = subprocess.run([binary, "-e", RUNNER], input=json.dumps({"code": program, "calls": calls}), capture_output=True, text=True, timeout=60, encoding="utf-8")
    if done.returncode != 0:
        raise AssertionError("node failed: " + done.stderr[-1200:])
    return json.loads(done.stdout)


def load(view, extra=()):
    return [f"mxView = {json.dumps(view)}", "mxPaint()", *extra]


class PageStructureTests(unittest.TestCase):
    def test_the_card_is_in_the_panel_and_hidden_until_the_panel_says_a_matrix_is_available(self):
        self.assertIn('<div class="card" id="matrix" hidden>', PAGE)
        self.assertIn("mxShow(!!(state.matrix && state.matrix.available));", function_body("render"))
        self.assertIn("card.hidden = !open;", function_body("mxShow", pages.CARD_SCRIPT))

    def test_the_whole_assembled_panel_script_compiles(self):
        binary = shutil.which("node")
        self.assertTrue(binary, "Node is not installed")
        done = subprocess.run([binary, "-e", "new (require('vm').Script)(require('fs').readFileSync(0, 'utf8'))"], input=SCRIPT, capture_output=True, text=True, timeout=30, encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stderr[-600:])

    def test_no_placeholder_is_left_in_the_page(self):
        self.assertNotIn("__MATRIX", PAGE)
        self.assertNotIn("__STYLE__", PAGE)

    def test_the_card_is_built_with_text_only(self):
        for token in FORBIDDEN_SCRIPT:
            self.assertNotIn(token, pages.CARD_SCRIPT, token)
        for name in ("mxPaint", "mxPress", "mxLoad"):
            self.assertNotIn("innerHTML", function_body(name, pages.CARD_SCRIPT))

    def test_the_page_holds_no_address_no_setting_name_and_no_preset_name(self):
        for text in ("192.168", "WATCHDOG_MATRIX", "7000", "MAIN PC", "BACKUP PC", "BRIGHTSIGN", "Main Server", "BrightSign"):
            self.assertNotIn(text, PAGE, text)         # presets, names and routes all come from the server after sign-in, so the page needs no edit when they change

    def test_it_is_ascii_without_the_forbidden_characters(self):
        self.assertTrue(PAGE.isascii())
        self.assertNotIn(chr(0x2014), PAGE)
        self.assertNotIn(chr(0xA7), PAGE)

    def test_the_press_sends_only_the_preset_and_the_confirmation(self):
        body = function_body("mxPress", pages.CARD_SCRIPT)
        self.assertIn("JSON.stringify({preset: key, confirm: !!preset.needs_confirm})", body)
        self.assertNotIn("operator", body)
        self.assertNotIn("via", body)

    def test_the_emergency_question_comes_before_anything_is_sent(self):
        body = function_body("mxPress", pages.CARD_SCRIPT)
        self.assertLess(body.index("confirm(mxConfirmText(preset))"), body.index('api("/control/panel/matrix"'))

    def test_it_asks_for_a_new_reading_every_five_seconds_but_not_while_a_press_is_running(self):
        body = function_body("mxShow", pages.CARD_SCRIPT)
        self.assertIn("5000", body)
        self.assertIn("if (!mxBusy)", body)
        self.assertIn("3000", function_body("mxPress", pages.CARD_SCRIPT))              # the buttons stay locked three seconds after a switch

    def test_the_progress_bar_and_the_reduced_motion_rule_exist(self):
        self.assertIn('role="progressbar"', PAGE)
        self.assertIn("@media (prefers-reduced-motion: reduce) { * { animation:none !important", PAGE)


class ButtonStateTests(unittest.TestCase):
    """The card's own script, run for real, fed with status views made by the real server code."""

    def paint(self, view, extra=()):
        return node(load(view, extra) + ["look()", "ids['mx-line'].textContent", "ids['mx-progress'].hidden"])

    def test_the_three_buttons_come_from_the_server_in_the_owners_order_with_their_group_labels_and_routes(self):
        *_, shown, headline, progress = self.paint(status_for(fx.LIVE_ROUTING))
        self.assertEqual([item["title"] for item in shown], ["MAIN PC", "BACKUP PC", "BRIGHTSIGN PLAYER"])
        self.assertEqual([item["label"] for item in shown], ["NORMAL OPERATION", "BACKUP OPERATION", "EMERGENCY OPERATION"])
        self.assertEqual(shown[0]["routes"], "Main Server USA to outputs 1, 2, 5; Main Server Caribbean to output 6")
        self.assertEqual(shown[1]["routes"], "Backup Server USA to outputs 1, 2, 5; Backup Server Caribbean to output 6")
        self.assertEqual(shown[2]["routes"], "BrightSign Player to outputs 1, 2, 3, 4, 5, 6")

    def test_when_nothing_matches_no_button_is_active_and_the_headline_says_so(self):
        *_, shown, headline, _ = self.paint(status_for(fx.LIVE_ROUTING))
        self.assertEqual([item["word"] for item in shown], ["not active"] * 3)
        self.assertTrue(all(not item["disabled"] for item in shown))
        self.assertTrue(all(item["pressed"] == "false" for item in shown))
        self.assertIn("No preset matches the matrix right now", headline)

    def test_the_active_preset_says_ACTIVE_is_pressed_and_cannot_be_pressed_again(self):
        *_, shown, headline, _ = self.paint(status_for(fx.MAIN_ROUTING))
        self.assertEqual((shown[0]["word"], shown[0]["disabled"], shown[0]["pressed"], "mx-active" in shown[0]["cls"]), ("ACTIVE", True, "true", True))
        self.assertEqual([item["word"] for item in shown[1:]], ["not active", "not active"])
        self.assertEqual(headline, "Active now: MAIN PC")

    def test_brightsign_is_active_when_outputs_1_to_6_carry_input_1(self):
        *_, shown, headline, _ = self.paint(status_for((1, 1, 1, 1, 1, 1, 7, 7)))
        self.assertEqual((shown[2]["word"], headline), ("ACTIVE", "Active now: BRIGHTSIGN PLAYER"))

    def test_an_unreachable_matrix_shows_no_active_preset_and_locks_every_button(self):
        fake = fx.FakeMatrix(fx.MAIN_ROUTING)
        fake.offline = True
        view = matrix.Switcher(fake, sleep=lambda s: None).status()
        view["core_reached"] = True
        *_, shown, headline, _ = self.paint(view)
        self.assertEqual([item["word"] for item in shown], ["not reached"] * 3)
        self.assertTrue(all(item["disabled"] for item in shown))
        self.assertFalse(any("ACTIVE" in item["word"] for item in shown))
        self.assertIn("did not answer", headline)

    def test_an_unreachable_matrix_wins_over_a_press_in_flight_the_buttons_say_not_reached(self):
        fake = fx.FakeMatrix(fx.MAIN_ROUTING)
        fake.offline = True
        view = matrix.Switcher(fake, sleep=lambda s: None).status()
        view["core_reached"] = True
        out = node(load(view, ["mxBusy = true; mxPressed = 'main'; mxPaint(); look()"]))
        self.assertEqual([item["word"] for item in out[-1]], ["not reached"] * 3)

    def test_when_core_is_not_reached_nothing_is_assumed(self):
        *_, shown, headline, _ = self.paint({"enabled": True, "core_reached": False, "error": "Core was not reached."})
        self.assertEqual(shown, [])                                                         # no routing and no presets came back, so no button is drawn
        self.assertIn("Core was not reached", headline)
        view = status_for(fx.MAIN_ROUTING, core_reached=False)
        *_, shown, headline, _ = self.paint(view)
        self.assertTrue(all(item["word"] == "not reached" and item["disabled"] for item in shown))
        self.assertFalse(any(item["pressed"] == "true" for item in shown))

    def test_a_switch_running_somewhere_locks_the_buttons_and_shows_the_progress_bar(self):
        view = status_for(fx.LIVE_ROUTING, switching=True, active=None)
        *_, shown, headline, hidden = self.paint(view)
        self.assertEqual([item["word"] for item in shown], ["a switch is running"] * 3)
        self.assertTrue(all(item["disabled"] for item in shown))
        self.assertIs(hidden, False)
        self.assertIn("switch is running", headline)

    def test_a_preset_with_no_outputs_set_is_shown_disabled_with_the_reason(self):
        view = status_for(fx.LIVE_ROUTING)
        view["presets"][2].update(configured=False, routes=[])
        *_, shown, _, _ = self.paint(view)
        self.assertEqual((shown[2]["word"], shown[2]["disabled"], shown[2]["routes"]), ("outputs not set yet", True, "outputs not set yet"))

    def test_the_eight_outputs_are_listed_with_their_input_and_name_and_an_unreadable_one_is_unknown(self):
        view = status_for((2, 255, 2, 3, 7, 3, 7, 7))
        rows = node(load(view, ["ids['mx-outputs'].children.map(function (r) { return r.children.map(function (c) { return c.textContent; }); })"]))[-1]
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0], ["1", "input 2", "Main Server USA"])
        self.assertEqual(rows[1], ["2", "unknown", "unknown"])
        self.assertEqual(rows[3], ["4", "input 3", "Main Server Caribbean"])

    def test_the_last_successful_switch_is_shown_with_who_and_when_and_survives_as_text(self):
        last = {"preset": "backup", "operator": "owner@mail.example", "at": "2026-11-09T11:45:00.123456+00:00"}
        out = node(load(status_for(fx.BACKUP_ROUTING, last_success=last), ["ids['mx-last'].textContent"]) + ["mxLastText(null)", "mxWhen('nonsense')", "mxWhen('2026-11-09T11:45:00Z')"])
        self.assertEqual(out[2], "Last successful switch: BACKUP, by owner@mail.example, at 2026-11-09 11:45:00 UTC.")
        self.assertEqual(out[3], "No successful switch is recorded yet.")
        self.assertEqual((out[4], out[5]), ("an unknown time", "2026-11-09 11:45:00 UTC"))


class PressFlowTests(unittest.TestCase):
    def test_the_emergency_press_asks_first_and_a_cancel_sends_nothing(self):
        view = status_for(fx.MAIN_ROUTING)
        out = node(load(view, ["confirmAnswer = false", "mxPress('brightsign')", "[confirms.length, calls.length, mxBusy]", "confirms[0]"]))
        self.assertEqual(out[-2], [1, 0, False])
        self.assertIn("Put BRIGHTSIGN PLAYER on air?", out[-1])
        self.assertIn("BrightSign Player to outputs 1, 2, 3, 4, 5, 6", out[-1])

    def test_the_emergency_press_after_ok_sends_the_preset_with_confirm_true_and_nothing_else(self):
        view = status_for(fx.MAIN_ROUTING)
        done = {"status": 200, "data": {"ok": True, "state": "done", "message": "BRIGHTSIGN PLAYER is active.", "status": status_for((1, 1, 1, 1, 1, 1, 7, 7))}}
        out = node(load(view, [f"responses.push({json.dumps(done)})", "mxPress('brightsign')", "calls.map(function (c) { return [c.path, c.options.method, JSON.parse(c.options.body)]; })", "ids['mx-msg'].textContent"]))
        self.assertEqual(out[-2], [["/control/panel/matrix", "POST", {"preset": "brightsign", "confirm": True}]])
        self.assertEqual(out[-1], "BRIGHTSIGN PLAYER is active.")

    def test_main_and_backup_do_not_ask_and_send_confirm_false(self):
        view = status_for(fx.LIVE_ROUTING)
        done = {"status": 200, "data": {"ok": True, "state": "done", "message": "ok", "status": status_for(fx.MAIN_ROUTING)}}
        out = node(load(view, [f"responses.push({json.dumps(done)})", "mxPress('main')", "[confirms.length, calls.map(function (c) { return JSON.parse(c.options.body); })]"]))
        self.assertEqual(out[-1], [0, [{"preset": "main", "confirm": False}]])

    def test_while_a_press_is_running_every_button_is_locked_and_a_second_press_sends_nothing(self):
        view = status_for(fx.LIVE_ROUTING)
        out = node(load(view, ["mxBusy = true; mxPressed = 'main'; mxPaint(); look()", "mxPress('backup'); calls.length"]))
        shown = out[-2]
        self.assertEqual([(item["word"], item["disabled"]) for item in shown], [("SWITCHING ...", True), ("wait", True), ("wait", True)])
        self.assertEqual(out[-1], 0)

    def test_after_a_switch_the_buttons_stay_locked_for_three_seconds_then_free_themselves(self):
        view = status_for(fx.LIVE_ROUTING)
        done = {"status": 200, "data": {"ok": True, "state": "done", "message": "ok", "status": status_for(fx.MAIN_ROUTING)}}
        out = node(load(view, [f"responses.push({json.dumps(done)})", "mxPress('main')", "[mxBusy, mxCooling, look().map(function (i) { return i.disabled; })]",
                               "timers.filter(function (t) { return t.once; }).map(function (t) { return t.ms; })", "timers.filter(function (t) { return t.once; })[0].fn(); [mxCooling, look().map(function (i) { return i.disabled; })]"]))
        self.assertEqual(out[-3], [False, True, [True, True, True]])                 # main is active (cannot be pressed again) and the other two are locked by the cool-down
        self.assertEqual(out[-2], [3000])
        self.assertEqual(out[-1], [False, [True, False, False]])

    def test_a_failed_switch_marks_that_button_ERROR_with_the_outputs_and_never_active(self):
        failed = {"status": 200, "data": {"ok": False, "state": "failed", "message": "The matrix did not confirm MAIN PC: output 5 shows input 7, wanted 2.", "failed_outputs": [{"output": 5, "wanted": 2, "got": 7}],
                                           "status": status_for(fx.LIVE_ROUTING)}}
        out = node(load(status_for(fx.LIVE_ROUTING), [f"responses.push({json.dumps(failed)})", "mxPress('main')", "look()", "ids['mx-msg'].textContent", "ids['mx-msg'].style.color"]))
        shown = out[-3]
        self.assertEqual((shown[0]["word"], "mx-error" in shown[0]["cls"]), ("ERROR: output 5 shows input 7", True))
        self.assertEqual(out[-2], "The matrix did not confirm MAIN PC: output 5 shows input 7, wanted 2.")
        self.assertEqual(out[-1], "var(--bad)")
        self.assertFalse(any(item["word"] == "ACTIVE" for item in shown))

    def test_an_unverified_switch_says_not_confirmed_and_a_network_failure_says_it_may_or_may_not_have_switched(self):
        unverified = {"status": 200, "data": {"ok": False, "state": "unverified", "message": "not confirmed", "status": status_for(fx.LIVE_ROUTING)}}
        out = node(load(status_for(fx.LIVE_ROUTING), [f"responses.push({json.dumps(unverified)})", "mxPress('main')", "look()[0].word", "mxBusy"]))
        self.assertEqual((out[-2], out[-1]), ("ERROR: not confirmed", False))
        out = node(load(status_for(fx.LIVE_ROUTING), ["responses.push('network')", "mxPress('main')", "ids['mx-msg'].textContent", "mxBusy"]))
        self.assertIn("may or may not have switched", out[-2])
        self.assertIs(out[-1], False)

    def test_a_refusal_from_core_is_shown_as_an_error_message_and_not_as_a_failed_button(self):
        busy = {"status": 409, "data": {"ok": False, "state": "busy", "message": "A switch is already running. Wait for it to finish."}}
        reread = {"status": 200, "data": status_for(fx.LIVE_ROUTING)}                    # the card asks for a fresh reading after a press that carried no read-back
        out = node(load(status_for(fx.LIVE_ROUTING), [f"responses.push({json.dumps(busy)}, {json.dumps(reread)})", "mxPress('main')", "ids['mx-msg'].textContent", "look()[0].word", "mxFailed"]))
        self.assertEqual(out[-3], "A switch is already running. Wait for it to finish.")
        self.assertEqual((out[-2], out[-1]), ("not active", None))

    def test_showing_the_card_loads_it_once_and_a_404_hides_it_again(self):
        view = status_for(fx.MAIN_ROUTING)
        out = node(["responses.push({status: 200, data: %s})" % json.dumps(view), "mxShow(true); mxShow(true); [ids['matrix'].hidden, calls.length, timers.filter(function (t) { return !t.once; }).length]",
                    "calls[0].path", "responses.push({status: 404, data: {error: 'not found'}}); mxLoad(false); ids['matrix'].hidden", "mxShow(false); ids['matrix'].hidden"])
        self.assertEqual(out[1], [False, 1, 1])
        self.assertEqual(out[2], "/control/panel/matrix")
        self.assertTrue(out[3])
        self.assertTrue(out[4])

    def test_a_poll_that_was_already_in_flight_cannot_repaint_the_old_state_after_a_press(self):
        old, new = status_for(fx.LIVE_ROUTING), status_for(fx.MAIN_ROUTING)
        done = {"status": 200, "data": {"ok": True, "state": "done", "message": "ok", "status": new}}
        out = node(load(old, ["responses.push('defer'); mxLoad(false); deferreds.length", f"responses.push({json.dumps(done)}); mxPress('main'); ids['mx-line'].textContent",
                              f"deferreds[0].settle({json.dumps({'status': 200, 'data': old})}, false); ids['mx-line'].textContent"]))
        self.assertEqual(out[-3], 1)
        self.assertEqual(out[-2], "Active now: MAIN PC")
        self.assertEqual(out[-1], "Active now: MAIN PC")                                   # the late, older answer was dropped

    def test_a_poll_never_piles_up_behind_a_slow_one_and_a_refresh_waits_while_a_press_runs(self):
        out = node(["responses.push('defer'); mxLoad(false); mxLoad(false); calls.length", "mxLoad(true); calls.length", "mxBusy = true; mxLoad(true); calls.length"])
        self.assertEqual(out, [1, 2, 2])

    def test_a_press_with_no_status_in_the_answer_clears_the_view_and_asks_for_a_fresh_reading(self):
        view = status_for(fx.MAIN_ROUTING)
        unreachable = {"status": 200, "data": {"ok": False, "state": "core_unreachable", "message": "Core did not answer in time. The matrix may or may not have switched: press Refresh to see what it shows."}}
        out = node(load(view, [f"responses.push({json.dumps(unreachable)})", "mxPress('backup'); calls[calls.length - 1].path", "ids['mx-msg'].textContent"]))
        self.assertEqual(out[-2], "/control/panel/matrix?fresh=1")
        self.assertIn("may or may not have switched", out[-1])
        out = node(load(view, ["responses.push('network')", "mxPress('backup'); calls[calls.length - 1].path"]))
        self.assertEqual(out[-1], "/control/panel/matrix?fresh=1")

    def test_while_the_fresh_reading_is_awaited_the_old_state_is_not_shown_as_current(self):
        view = status_for(fx.MAIN_ROUTING)
        unreachable = {"status": 200, "data": {"ok": False, "state": "core_unreachable", "message": "Core did not answer in time. The matrix may or may not have switched."}}
        out = node(load(view, [f"responses.push({json.dumps(unreachable)}, 'defer')", "mxPress('backup'); [look(), ids['mx-line'].textContent]"]))
        self.assertEqual(out[-1], [[], "Reading the matrix ..."])                          # no green MAIN PC while nobody knows what the matrix shows

    def test_the_last_successful_switch_stays_on_the_card_right_after_a_press(self):
        last = {"preset": "main", "operator": "owner@mail.example", "at": "2026-11-09T11:45:00+00:00"}
        done = {"status": 200, "data": {"ok": True, "state": "done", "message": "ok", "status": status_for(fx.MAIN_ROUTING, last_success=last)}}
        out = node(load(status_for(fx.LIVE_ROUTING), [f"responses.push({json.dumps(done)})", "mxPress('main'); ids['mx-last'].textContent"]))
        self.assertEqual(out[-1], "Last successful switch: MAIN, by owner@mail.example, at 2026-11-09 11:45:00 UTC.")

    def test_refresh_asks_the_server_to_read_the_matrix_again(self):
        out = node(["ids['mx-refresh'].listeners.click()", "calls[0].path"])
        self.assertEqual(out[-1], "/control/panel/matrix?fresh=1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
