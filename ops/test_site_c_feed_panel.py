# File:         test_site_c_feed_panel.py
# Purpose:      Tests of the "Site C feed" card on the dashboard page (core-listener/app/dashboard.py): it is a collapsible card that stays hidden until the listener sends data for
#               it, shows the state in words (never by colour alone), opens by itself when a stream goes critical but never closes what the viewer opened, is built from text only,
#               groups the streams as Main (:5005) and Backup (:5004) with their UDP addresses, draws the last ten minutes live, is violet so it stands apart, makes the page siren
#               sound on a critical stream, and writes no machine address into the page itself (the addresses arrive as data).
# Inputs:       None (the page is rendered with a fake token).
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/dashboard.py (pure string building: no server, no browser). A real-browser look is in the unpublished design notes, section 7.
"""Run: python ops/test_site_c_feed_panel.py"""
import importlib
import json
import re
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

LISTENER_DIR = Path(__file__).resolve().parent.parent / "core-listener"
sys.path.insert(0, str(LISTENER_DIR))
dashboard = importlib.import_module("app.dashboard")
PAGE = dashboard.render_dashboard_html("test-token-123")


def function_body(name):
    start = PAGE.index("function " + name + "(")
    depth, i = 0, PAGE.index("{", start)
    first = i
    while True:
        depth += PAGE[i] == "{"
        depth -= PAGE[i] == "}"
        i += 1
        if depth == 0:
            return PAGE[first:i]


class CardMarkupTests(unittest.TestCase):
    def tag(self):
        match = re.search(r'<details[^>]*id="vz-panel"[^>]*>', PAGE)
        self.assertIsNotNone(match, "the card must be a native details element with id vz-panel")
        return match.group(0)

    def test_it_is_a_collapsed_native_details_card_hidden_until_data_arrives(self):
        tag = self.tag()
        self.assertNotRegex(tag, r"\sopen[\s>=]", "collapsed by default: long content is opened on purpose, never by default")
        self.assertIn("hidden", tag)

    def test_the_summary_line_carries_the_headline_so_a_closed_card_still_tells_the_state(self):
        block = PAGE[PAGE.index('id="vz-panel"'):]
        block = block[:block.index("</details>")]
        self.assertIn("<summary", block)
        self.assertIn('id="vz-headline"', block)
        self.assertIn('id="vz-body"', block)

    def test_the_card_is_named_site_c_zixi_feed(self):
        """The owner's name for it (2026-11-08): "Site C ZIXI Feed"."""
        block = PAGE[PAGE.index('id="vz-panel"'):]
        block = block[:block.index("</details>")]
        self.assertIn('<span class="eyebrow">Site C ZIXI Feed</span>', block)
        self.assertNotIn(">Site C feed<", PAGE)

    def test_the_card_sits_between_the_overview_and_the_playout_tiles(self):
        self.assertLess(PAGE.index('id="overview"'), PAGE.index('id="vz-panel"'))
        self.assertLess(PAGE.index('id="vz-panel"'), PAGE.index('id="dashboard-view"'))


class ScriptTests(unittest.TestCase):
    def test_every_frame_redraws_the_card(self):
        self.assertRegex(function_body("renderAll"), r"renderSiteC\(uiState\.lastData\.site_c_feed\)")

    def test_an_older_listener_without_the_block_hides_the_card(self):
        body = function_body("renderSiteC")
        self.assertRegex(body, r"if \(!block \|\| !block\.streams\)")
        self.assertIn("card.hidden = true", body)

    def test_it_opens_by_itself_when_a_stream_turns_critical_and_only_then(self):
        body = function_body("renderSiteC")
        self.assertRegex(body, r'level === "critical" && vzLevelSeen !== "critical"')
        self.assertIn("card.open = true", body)
        self.assertEqual(body.count("card.open = false"), 0, "it must never close a card the viewer opened")

    def test_the_inner_list_keeps_its_open_state_across_the_redraws(self):
        body = function_body("renderSiteC")
        self.assertIn("vzDayOpen", body)
        self.assertIn('addEventListener("toggle"', body)

    def test_no_playout_pc_is_named_on_the_card(self):
        """The owner (2026-11-08): take out "Heard by MAIN" and "What each PC hears"."""
        body = function_body("renderSiteC")
        for gone in ("What each PC hears", "Heard by", "vz-pcs", "vzPcOpen", "w.source", "heard_by.join"):
            self.assertNotIn(gone, body)
        css = PAGE[PAGE.index("/* site_c-feed */"):PAGE.index("/* end site_c-feed */")]
        self.assertNotIn("vz-pcs", css)

    def test_the_state_is_printed_in_words_not_only_by_colour(self):
        body = function_body("renderSiteC") + function_body("vzHeadline")
        for word in ("Not reporting yet", "word"):
            self.assertIn(word, body)

    def test_the_card_is_built_with_text_only(self):
        self.assertNotIn("innerHTML", function_body("renderSiteC"))
        self.assertNotIn("innerHTML", function_body("vzSpark"))
        self.assertIn("textContent", function_body("vzEl"))

    def test_the_history_line_is_drawn_with_the_floor_marked(self):
        body = function_body("vzSpark")
        self.assertIn("createElementNS", body)
        self.assertIn("vz-floor", body)
        self.assertIn("aria-label", body)


RUNNER = r"""
const vm = require('vm');
let input = '';
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  const job = JSON.parse(input);
  const context = vm.createContext({});
  vm.runInContext(job.code, context);
  const results = job.calls.map(call => {
    const value = vm.runInContext(call, context, {timeout: 2000});
    return value === undefined ? null : JSON.parse(JSON.stringify(value));
  });
  process.stdout.write(JSON.stringify(results));
});
"""
PURE_NAMES = ("vzCritical", "vzLabel", "vzWhere", "vzOrder", "vzHeadline", "vzWindowLabel", "vzWindow", "vzRange", "vzPlace", "vzAgeText", "vzAlarmLines", "vzBannerTitle", "vzFineWindow", "vzChartPoints", "vzSplit", "vzDots")


def pure_functions():
    """The card's own rule functions (no page, no DOM), cut out of the page text so a missing one fails only the test that needs it."""
    return "\n".join(function_source(name) for name in PURE_NAMES)


def function_source(name):
    """The whole function: its first line (name and parameters) and its body."""
    start = PAGE.index("function " + name + "(")
    return PAGE[start:PAGE.index("{", start)] + function_body(name)


def node_eval(calls):
    """Runs the card's rule functions in Node and returns the value of each expression in `calls`. A missing Node is a FAILURE, never a skip."""
    node = shutil.which("node")
    if not node:
        raise AssertionError("Node is not installed: the card's rules cannot be run, which is a failure and not a skip")
    done = subprocess.run([node, "-e", RUNNER], input=json.dumps({"code": pure_functions(), "calls": calls}), capture_output=True, text=True, timeout=30, encoding="utf-8")
    if done.returncode != 0:
        raise AssertionError("node failed: " + done.stderr[-800:])
    return json.loads(done.stdout)


def stream(level, role=None, port="5005", **extra):
    entry = {"stream": port, "level": level, "word": {"ok": "OK", "critical": "LOST", "warning": "WARNING", "no_data": "NO DATA"}[level], "mbit_per_s": 5.0}
    if role:
        entry.update(role=role, label={"main": "Main stream", "backup": "Backup stream"}[role], address="udp://@224.1.1.1:" + port)
    entry.update(extra)
    return entry


class SirenTests(unittest.TestCase):
    """2026-11-08, the owner's words: "site_c siren should sound [on a] site_c critical alert". Until now the page's siren listened only to the six playouts."""

    def test_a_critical_stream_counts_as_a_critical_alarm_before_the_siren_is_set(self):
        body = function_body("renderAll")
        self.assertRegex(body, r"uiState\.criticalActive = states\.some\(function \(state\) \{ return state\.status\.level === \"critical\"; \}\) \|\| vzCritical\(uiState\.lastData\.site_c_feed\);")
        self.assertLess(body.index("vzCritical("), body.index("setAlarm(uiState.criticalActive)"))

    def test_only_a_critical_stream_makes_the_card_critical(self):
        cases = [
            ({"streams": [stream("ok", "main"), stream("critical", "backup", "5004")]}, True),
            ({"streams": [stream("critical", "main")]}, True),
            ({"streams": [stream("ok", "main"), stream("ok", "backup", "5004")]}, False),
            ({"streams": [stream("warning", "main"), stream("no_data", "backup", "5004")]}, False),
            ({"streams": []}, False),
            ({}, False),
            ({"streams": "critical"}, False),
            (None, False),
        ]
        results = node_eval([f"vzCritical({json.dumps(block)})" for block, _ in cases] + ["vzCritical(undefined)"])
        self.assertEqual(results, [expected for _, expected in cases] + [False])

    def test_it_is_strictly_true_or_false_so_a_missing_block_can_never_leave_the_siren_unset(self):
        self.assertIs(node_eval(["vzCritical(null)"])[0], False)

    def test_the_old_all_clear_path_is_unchanged_for_the_playouts(self):
        body = function_body("renderAll")
        self.assertIn("setAlarm(uiState.criticalActive);", body)
        self.assertIn("if (!uiState.criticalActive && uiState.muted)", body)


class GroupingTests(unittest.TestCase):
    """2026-11-08: "main stream :5005 while backup stream is :5004". Main comes first, each stream carries its name and its UDP address."""

    def test_main_is_listed_before_backup_whatever_order_the_data_arrives_in(self):
        streams = [stream("ok", "backup", "5004"), stream("ok", "main", "5005")]
        order = node_eval([f"vzOrder({json.dumps(streams)}).map(function (s) {{ return s.stream; }})"])[0]
        self.assertEqual(order, ["5005", "5004"])

    def test_data_without_roles_from_an_older_listener_keeps_its_order(self):
        streams = [stream("ok", None, "5004"), stream("ok", None, "5005")]
        self.assertEqual(node_eval([f"vzOrder({json.dumps(streams)}).map(function (s) {{ return s.stream; }})"])[0], ["5004", "5005"])

    def test_ordering_does_not_change_the_list_it_was_given(self):
        streams = [stream("ok", "backup", "5004"), stream("ok", "main", "5005")]
        result = node_eval([f"(function () {{ var input = {json.dumps(streams)}; vzOrder(input); return input.map(function (s) {{ return s.stream; }}); }})()"])[0]
        self.assertEqual(result, ["5004", "5005"])

    def test_the_label_comes_from_the_data_and_falls_back_to_the_port(self):
        self.assertEqual(node_eval([f"vzLabel({json.dumps(stream('ok', 'main'))})", f"vzLabel({json.dumps(stream('ok', None, '5004'))})"]), ["Main stream", "Stream 5004"])

    def test_the_closed_card_headline_names_both_streams_with_their_full_multicast_address(self):
        """The owner (2026-11-08): "Stream 5004" is supposed to be the full multicast address, on both sides."""
        block = {"reporting": True, "streams": [stream("ok", "backup", "5004"), stream("critical", "main", "5005")]}
        self.assertEqual(node_eval([f"vzHeadline({json.dumps(block)})"])[0],
                         "Main stream udp://@224.1.1.1:5005 LOST   |   Backup stream udp://@224.1.1.1:5004 OK 5.0 Mbit/s")

    def test_data_without_an_address_from_an_older_listener_still_reads_sensibly(self):
        block = {"reporting": True, "streams": [stream("ok", None, "5004")]}
        self.assertEqual(node_eval([f"vzHeadline({json.dumps(block)})", f"vzWhere({json.dumps(stream('ok', None, '5004'))})"]), ["Stream 5004 OK 5.0 Mbit/s", ""])

    def test_an_unreporting_card_says_so(self):
        self.assertEqual(node_eval(['vzHeadline({"reporting": false, "streams": []})', "vzHeadline(null)"]), ["Not reporting yet", "Not reporting yet"])

    def test_each_tile_prints_its_name_and_its_udp_address_as_text(self):
        body = function_body("renderSiteC")
        self.assertIn("vzOrder(block.streams)", body)
        self.assertIn("vzLabel(s)", body)
        self.assertIn('vzEl("div", "vz-addr", s.address)', body)
        self.assertNotIn("innerHTML", body)


class LiveChartTests(unittest.TestCase):
    """The owner (2026-11-08): the live graph "is just a straight line". A steady encoder IS a flat line when the chart starts at zero, so the chart now zooms to the readings
    in a window the owner can choose (2, 5 or 10 minutes), draws a dot at every real reading, and says what scale it is showing."""
    NOW = "Date.parse('2026-11-08T12:10:00Z')"
    LIVE = [{"t": "2026-11-08T12:09:50+00:00", "mbit": 5.1}, {"t": "2026-11-08T11:50:00+00:00", "mbit": 5.0}, {"t": "banana", "mbit": 5.0},
            {"t": "2026-11-08T12:09:00+00:00", "mbit": "fast"}, {"t": "2026-11-08T12:08:30+00:00", "mbit": 4.9}]

    def window(self, live, span):
        return node_eval([f"vzWindow({json.dumps(live)}, {self.NOW}, {span})"])[0]

    def test_the_window_keeps_readings_inside_it_oldest_first_and_only_real_numbers(self):
        self.assertEqual([p["mbit"] for p in self.window(self.LIVE, 600)], [4.9, 5.1])

    def test_a_shorter_window_keeps_fewer_readings(self):
        self.assertEqual([len(self.window(self.LIVE, span)) for span in (600, 120, 60)], [2, 2, 1])

    def test_the_window_labels_read_in_minutes(self):
        self.assertEqual(node_eval(["vzWindowLabel(120)", "vzWindowLabel(300)", "vzWindowLabel(600)", "vzWindowLabel(60)"]), ["2 min", "5 min", "10 min", "60 s"])

    def test_with_no_readings_the_scale_has_room_for_the_usual_rate(self):
        self.assertEqual(node_eval(["vzRange([], 5, 1)"])[0], {"low": 0, "high": 6.25})

    def test_a_steady_stream_is_zoomed_so_its_small_movement_shows(self):
        points = [{"mbit": 4.95}, {"mbit": 5.05}]
        self.assertEqual(node_eval([f"vzRange({json.dumps(points)}, 5, 1)"])[0], {"low": 4.85, "high": 5.15})

    def test_one_reading_gets_the_same_zoomed_scale(self):
        self.assertEqual(node_eval(["vzRange([{'mbit': 5}], 5, 1)"])[0], {"low": 4.85, "high": 5.15})

    def test_a_dip_widens_the_scale_down_to_zero_and_the_alarm_line_is_inside_it(self):
        points = [{"mbit": 5.0}, {"mbit": 0.6}]
        self.assertEqual(node_eval([f"vzRange({json.dumps(points)}, 5, 1)"])[0], {"low": 0, "high": 6.1})

    def test_a_stream_running_near_the_alarm_line_keeps_the_line_on_the_chart(self):
        self.assertEqual(node_eval(["vzRange([{'mbit': 1.5}, {'mbit': 1.5}], 5, 1)"])[0], {"low": 0.5, "high": 1.65})

    def test_a_dead_stream_still_shows_the_alarm_line_above_the_zeros(self):
        self.assertEqual(node_eval(["vzRange([{'mbit': 0}, {'mbit': 0}], 5, 1)"])[0], {"low": 0, "high": 1.5})

    def test_a_point_is_placed_by_its_age_inside_the_window_and_by_its_rate_inside_the_scale(self):
        live = [{"t": "2026-11-08T12:00:00+00:00", "mbit": 0.0}, {"t": "2026-11-08T12:05:00+00:00", "mbit": 10.0}, {"t": "2026-11-08T12:10:00+00:00", "mbit": 5.0}]
        places = node_eval([f"vzPlace(vzWindow({json.dumps(live)}, {self.NOW}, 600), {self.NOW}, 600, {{low: 0, high: 10}}, 240, 60)"])[0]
        self.assertEqual([round(p["x"], 1) for p in places], [0.0, 120.0, 240.0])
        self.assertEqual([round(p["y"], 1) for p in places], [58.0, 2.0, 30.0])

    def test_a_zoomed_scale_stretches_the_readings_across_the_chart_height(self):
        live = [{"t": "2026-11-08T12:09:00+00:00", "mbit": 4.0}, {"t": "2026-11-08T12:09:30+00:00", "mbit": 5.0}, {"t": "2026-11-08T12:10:00+00:00", "mbit": 6.0}]
        places = node_eval([f"vzPlace(vzWindow({json.dumps(live)}, {self.NOW}, 120), {self.NOW}, 120, {{low: 4, high: 6}}, 240, 60)"])[0]
        self.assertEqual([round(p["y"], 1) for p in places], [58.0, 30.0, 2.0])
        self.assertEqual([round(p["x"], 1) for p in places], [120.0, 180.0, 240.0])

    def test_a_reading_outside_the_scale_is_held_at_the_edge(self):
        live = [{"t": "2026-11-08T12:10:00+00:00", "mbit": 99.0}, {"t": "2026-11-08T12:09:50+00:00", "mbit": -3.0}]
        places = node_eval([f"vzPlace(vzWindow({json.dumps(live)}, {self.NOW}, 600), {self.NOW}, 600, {{low: 0, high: 10}}, 240, 60)"])[0]
        self.assertEqual([round(p["y"], 1) for p in places], [58.0, 2.0])

    def test_the_age_text_counts_on_between_reports(self):
        self.assertEqual(node_eval(["vzAgeText(4, 0, 3000)", "vzAgeText(4, 0, 0)", "vzAgeText(100, 0, 25000)", "vzAgeText(null, 0, 0)"]),
                         ["last report 7 s ago", "last report 4 s ago", "last report 2 min 5 s ago", "no report yet"])

    def test_an_age_never_runs_backwards_if_the_clock_does(self):
        self.assertEqual(node_eval(["vzAgeText(2, 10000, 0)"])[0], "last report 0 s ago")

    def test_the_chart_is_an_svg_with_a_spoken_summary_the_alarm_line_the_usual_line_and_a_dot_per_reading(self):
        body = function_body("vzLive")
        for part in ("createElementNS", "vz-floor", "vz-usual", "vz-line", "vz-dot", "aria-label", "role", "vzChartPoints(", "vzRange(", "vzPlace(", "vzWindowSeconds"):
            self.assertIn(part, body)
        self.assertNotIn("innerHTML", body)

    def test_the_chart_really_uses_the_chosen_window_for_both_the_readings_and_their_places(self):
        body = function_body("vzLive")
        self.assertIn("vzChartPoints(s, now, vzWindowSeconds)", body)
        self.assertIn("vzWindow(s.live, nowMs, spanSeconds)", function_body("vzChartPoints"))      # the ten-second readings are still the fallback
        self.assertIn("vzPlace(points, now, vzWindowSeconds, range, width, height)", body)
        self.assertIn("vzRange(points, expected, floor)", body)

    def test_the_chart_says_what_scale_it_shows_so_the_zoom_cannot_mislead(self):
        body = function_body("vzLive")
        self.assertIn('"Scale "', body)
        self.assertIn("Mbit/s", body)

    def test_the_alarm_and_usual_lines_are_drawn_only_when_they_are_inside_the_scale(self):
        body = function_body("vzLive")
        self.assertRegex(body, r"floor >= range\.low && floor <= range\.high")
        self.assertRegex(body, r"expected >= range\.low && expected <= range\.high")

    def test_a_chart_with_no_readings_says_it_is_still_collecting(self):
        self.assertIn("Collecting readings", function_body("vzLive"))

    def test_the_window_can_be_chosen_two_five_or_ten_minutes_and_is_remembered(self):
        self.assertRegex(PAGE, r"var vzWindowChoices = \[120, 300, 600\];")
        self.assertRegex(PAGE, r"var vzWindowSeconds = 300;")
        body = function_body("vzSetWindow")
        self.assertIn("vzWindowSeconds = ", body)
        self.assertIn("renderSiteC(uiState.lastData.site_c_feed)", body)
        self.assertIn("localStorage", body)
        self.assertIn("catch", body)
        tools = function_body("vzBuildTools")
        self.assertIn("vzWindowChoices.forEach", tools)
        self.assertIn("aria-pressed", tools)
        self.assertIn("vzSetWindow(", tools)
        self.assertIn("vzBuildTools()", function_body("renderSiteC"))
        self.assertRegex(PAGE, r'<div id="vz-tools" class="vz-tools"></div>')

    def test_the_window_buttons_are_built_once_so_a_click_is_never_lost_to_a_redraw(self):
        self.assertNotIn("vz-win", function_body("renderSiteC"))
        self.assertRegex(function_body("vzBuildTools"), r"if \(tools\.firstChild\)")

    def test_the_window_buttons_are_real_buttons_big_enough_to_press(self):
        css = PAGE[PAGE.index("/* site_c-feed */"):PAGE.index("/* end site_c-feed */")]
        self.assertRegex(css, r"\.vz-win \{[^}]*min-height:44px")
        self.assertIn('vzEl("button", "vz-win"', function_body("vzBuildTools"))

    def test_the_remembered_window_is_only_accepted_when_it_is_one_of_the_choices(self):
        self.assertIn("vzWindowChoices.indexOf(", PAGE[PAGE.index("var vzWindowSeconds"):PAGE.index("function vzEl(")])

    def test_the_ages_tick_every_second_with_the_rest_of_the_page(self):
        self.assertIn("vzTickAges()", function_body("tickLive"))
        self.assertIn("textContent", function_body("vzTickAges"))

    def test_the_day_chart_is_collapsible_and_the_stream_details_are_listed(self):
        body = function_body("renderSiteC")
        for part in ("vz-facts", "vzDayOpen", "Last 24 hours"):
            self.assertIn(part, body)
        self.assertEqual(body.count('addEventListener("toggle"'), 1)

    def test_the_details_list_the_packets_the_gap_and_the_window(self):
        body = function_body("renderSiteC")
        for part in ("Packets per second", "Packets in the window", "Counting window", "Seconds since the last packet"):
            self.assertIn(part, body)

    def test_the_stream_address_is_the_tiles_main_line(self):
        self.assertRegex(PAGE, r"\.vz-addr \{[^}]*font-size:1[4-9]px")


class AlarmIndicationTests(unittest.TestCase):
    """2026-11-08, the owner's words: when the encoder stops, or the bit rate gets very low, "we get a red alert and it blows [the] siren to the dashboard like all the other
    6 playouts and it indicates the Site C ZIXI Feed". The siren is covered above; here the top banner and the incident feed say it too, in the Site C ZIXI Feed's name."""

    def lines(self, block):
        return node_eval([f"vzAlarmLines({json.dumps(block)})"])[0]

    def test_a_critical_stream_becomes_a_line_that_names_the_feed_the_stream_and_the_cause(self):
        block = {"streams": [stream("critical", "backup", "5004", cause="Stream lost: no packets for 40 s", alarm={"since": "2026-11-08T12:00:00+00:00"})]}
        self.assertEqual(self.lines(block), [{
            "short": "Site C ZIXI Feed: Backup stream udp://@224.1.1.1:5004 LOST",
            "text": "Site C ZIXI Feed: Backup stream udp://@224.1.1.1:5004 LOST - Stream lost: no packets for 40 s",
            "since": "2026-11-08T12:00:00+00:00"}])

    def test_a_low_bit_rate_stream_reads_the_same_way_with_its_own_word(self):
        block = {"streams": [dict(stream("critical", "main", "5005", cause="Under 1 Mbit/s for 2 min 5 s"), word="BELOW 1 MBIT/S")]}
        self.assertEqual(self.lines(block)[0]["text"], "Site C ZIXI Feed: Main stream udp://@224.1.1.1:5005 BELOW 1 MBIT/S - Under 1 Mbit/s for 2 min 5 s")

    def test_only_critical_streams_make_lines_and_main_comes_first(self):
        block = {"streams": [stream("critical", "backup", "5004"), stream("ok", None, "1"), stream("warning", None, "2"), stream("critical", "main", "5005")]}
        self.assertEqual([line["short"] for line in self.lines(block)],
                         ["Site C ZIXI Feed: Main stream udp://@224.1.1.1:5005 LOST", "Site C ZIXI Feed: Backup stream udp://@224.1.1.1:5004 LOST"])

    def test_no_block_or_no_critical_stream_gives_no_lines(self):
        for block in (None, {}, {"streams": "x"}, {"streams": [stream("ok", "main")]}, {"streams": [stream("warning", "main")]}, {"streams": [stream("no_data", "main")]}):
            with self.subTest(block=block):
                self.assertEqual(self.lines(block), [])

    def test_a_line_without_a_cause_or_an_alarm_time_is_still_complete(self):
        line = self.lines({"streams": [stream("critical", "main", "5005")]})[0]
        self.assertEqual((line["text"], line["since"]), ("Site C ZIXI Feed: Main stream udp://@224.1.1.1:5005 LOST", None))

    def test_the_banner_title_counts_playouts_and_names_the_site_c_zixi_feed(self):
        cases = [((2, 0), "2 endpoints in critical alarm"), ((1, 0), "1 endpoint in critical alarm"), ((0, 1), "Site C ZIXI Feed in critical alarm"),
                 ((0, 2), "Site C ZIXI Feed in critical alarm"), ((1, 1), "1 endpoint and the Site C ZIXI Feed in critical alarm"),
                 ((3, 2), "3 endpoints and the Site C ZIXI Feed in critical alarm")]
        titles = node_eval([f"vzBannerTitle({playouts}, {streams})" for (playouts, streams), _ in cases])
        self.assertEqual(titles, [expected for _, expected in cases])

    def test_the_banner_turns_critical_for_a_site_c_stream_even_when_all_six_playouts_are_healthy(self):
        body = function_body("renderBanner")
        self.assertIn("vzAlarmLines(", body)
        self.assertIn("vzBannerTitle(critical.length, vzLines.length)", body)
        self.assertIn("critical.length || vzLines.length", body)
        self.assertIn("All six playout endpoints are healthy", body)
        self.assertLess(body.index("critical.length || vzLines.length"), body.index("All six playout endpoints are healthy"))
        self.assertLess(body.index("uiState.blind"), body.index("critical.length || vzLines.length"), "a lost live feed still comes first")

    def test_the_banner_reads_the_site_c_block_itself_so_every_place_that_redraws_it_agrees(self):
        self.assertIn("uiState.lastData.site_c_feed", function_body("renderBanner"))

    def test_the_incident_feed_lists_the_live_site_c_alarms_first_and_not_the_old_event_rows_twice(self):
        body = function_body("renderIncidentFeed")
        self.assertIn("vzAlarmLines(data.site_c_feed)", body)
        self.assertIn('event.source_id !== "SITE-C"', body)
        self.assertIn("vzIncidents.concat(incidents)", body)
        self.assertLess(body.index("states.filter"), body.index("vzIncidents.concat(incidents)"), "the playout fallback must still run on the playouts alone")

    def test_the_site_c_incident_is_critical_and_carries_the_time_the_alarm_began(self):
        body = function_body("renderIncidentFeed")
        self.assertRegex(body, r'level:\s*"critical"')
        self.assertIn("line.since", body)


class ColourTests(unittest.TestCase):
    """2026-11-08: "make the colour of that section different so it can be distinct". Violet: no other part of the page uses it, and the status colours keep their meaning."""

    @staticmethod
    def hue(hex_colour):
        red, green, blue = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
        high, low = max(red, green, blue), min(red, green, blue)
        if high == low:
            return 0.0
        delta = high - low
        if high == red:
            value = ((green - blue) / delta) % 6
        elif high == green:
            value = (blue - red) / delta + 2
        else:
            value = (red - green) / delta + 4
        return value * 60

    @staticmethod
    def luminance(hex_colour):
        channels = []
        for i in (1, 3, 5):
            c = int(hex_colour[i:i + 2], 16) / 255
            channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    def token(self, name):
        match = re.search(r"--" + name + r":(#[0-9a-fA-F]{6});", PAGE)
        self.assertIsNotNone(match, "the page tokens must define --" + name)
        return match.group(1)

    def test_violet_is_a_page_token_with_its_rgb_twin_for_tints(self):
        self.assertRegex(PAGE, r"--violet-rgb:\d{1,3},\d{1,3},\d{1,3};")
        self.token("violet")

    def test_the_violet_is_far_in_hue_from_every_other_accent_on_the_page(self):
        violet = self.hue(self.token("violet"))
        for other in ("blue", "green", "yellow", "orange", "red"):
            distance = abs(violet - self.hue(self.token(other)))
            distance = min(distance, 360 - distance)
            self.assertGreaterEqual(distance, 40, "violet is too close to " + other)

    def test_violet_text_is_readable_on_the_card_surface(self):
        light, dark = sorted((self.luminance(self.token("violet")), self.luminance(self.token("surface"))), reverse=True)
        self.assertGreaterEqual((light + 0.05) / (dark + 0.05), 4.5)

    def test_the_card_border_header_and_tiles_use_violet_and_nothing_hard_coded(self):
        css = PAGE[PAGE.index("/* site_c-feed */"):PAGE.index("/* end site_c-feed */")]
        self.assertIn("var(--violet)", css)
        self.assertIn("var(--violet-rgb)", css)
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,6}\b")

    def test_nothing_outside_the_card_uses_violet(self):
        outside = PAGE.replace(PAGE[PAGE.index("/* site_c-feed */"):PAGE.index("/* end site_c-feed */")], "")
        self.assertEqual(len(re.findall(r"var\(--violet(?:-rgb)?\)", outside)), 0)

    def test_the_status_words_keep_green_yellow_and_red_so_an_alarm_still_looks_like_one(self):
        css = PAGE[PAGE.index("/* site_c-feed */"):PAGE.index("/* end site_c-feed */")]
        self.assertRegex(css, r"\.vz-tile\.vz-ok \.vz-word \{ color:var\(--green\); \}")
        self.assertRegex(css, r"\.vz-tile\.vz-warning \.vz-word \{ color:var\(--yellow\); \}")
        self.assertRegex(css, r"\.vz-tile\.vz-critical \.vz-word \{ color:var\(--red\); \}")
        self.assertIn(".vz.vz-critical { border-color:rgba(var(--red-rgb)", css)


class PrivacyTests(unittest.TestCase):
    def test_the_public_page_names_no_address_of_the_encoder_the_streams_or_the_pcs(self):
        for forbidden in ("224.1.1.1", "192.168.7.166", "192.168.7.188", "192.168.7.184", "udp://"):
            self.assertNotIn(forbidden, PAGE)

    def test_the_colours_come_from_the_page_tokens_so_dark_mode_and_contrast_stay_consistent(self):
        css = PAGE[PAGE.index("/* site_c-feed */"):PAGE.index("/* end site_c-feed */")]
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,6}\b", "no hard-coded colours in the card's styles")
        self.assertIn("var(--red)", css)
        self.assertIn("min-height:44px", css.replace(" ", ""), "touch target of at least 44 px for the summary")


class FineChartTests(unittest.TestCase):
    """The per-second chart (the unpublished design notes, "The per-second readings"): the listener sends one number a second for the last ten minutes; the chart draws it as a
    line of its own (no dot per second, it would be clutter), breaks the line where nobody reported, and falls back to the ten-second readings when there is nothing finer."""
    NOW_MS = 1_794_000_000_000
    NOW = str(NOW_MS)

    @staticmethod
    def iso(ms):
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()

    def block(self, seconds_before_now, values, step=1):
        return {"step_seconds": step, "start": self.iso(self.NOW_MS - seconds_before_now * 1000), "mbit": values}

    def fine_window(self, block, span=600):
        return node_eval([f"vzFineWindow({json.dumps(block)}, {self.NOW}, {span})"])[0]

    def test_each_number_becomes_a_point_at_its_own_second_and_a_null_is_a_gap(self):
        points = self.fine_window(self.block(4, [5.0, 5.1, None, 4.9]))
        self.assertEqual([(p["ms"] - self.NOW_MS, p["mbit"]) for p in points], [(-4000, 5.0), (-3000, 5.1), (-1000, 4.9)])

    def test_the_window_keeps_only_the_seconds_asked_for(self):
        self.assertEqual([p["mbit"] for p in self.fine_window(self.block(4, [5.0, 5.1, None, 4.9]), 2)], [4.9])

    def test_a_point_a_little_ahead_of_the_page_clock_is_kept_and_one_far_ahead_is_dropped(self):
        self.assertEqual(len(self.fine_window(self.block(-0.5, [5.0]))), 1)
        self.assertEqual(self.fine_window(self.block(-5, [5.0])), [])

    def test_rubbish_gives_no_points_and_never_an_error(self):
        good_start = self.iso(self.NOW_MS - 3000)
        for bad in (None, {}, 7, "x", [], {"start": "banana", "step_seconds": 1, "mbit": [5.0]}, {"start": good_start, "step_seconds": 0, "mbit": [5.0]},
                    {"start": good_start, "step_seconds": -1, "mbit": [5.0, 5.0]}, {"start": good_start, "step_seconds": 1, "mbit": "5.0"}, {"start": good_start, "step_seconds": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(self.fine_window(bad), [])

    def test_only_real_numbers_are_drawn(self):
        points = self.fine_window(self.block(3, ["5.0", True, 5.0, None, {"a": 1}]))
        self.assertEqual([p["mbit"] for p in points], [5.0])

    def test_a_huge_list_is_cut_so_a_bad_listener_cannot_stall_the_page(self):
        points = self.fine_window(self.block(5000, [5.0] * 20000), 100000)
        self.assertEqual(len(points), 1200)

    def test_the_chart_prefers_the_per_second_readings(self):
        stream = {"fine": self.block(4, [5.0, 5.1, 5.2]), "live": [{"t": self.iso(self.NOW_MS - 10000), "mbit": 4.0}, {"t": self.iso(self.NOW_MS - 20000), "mbit": 4.1}]}
        chart = node_eval([f"vzChartPoints({json.dumps(stream)}, {self.NOW}, 600)"])[0]
        self.assertEqual((chart["fine"], [p["mbit"] for p in chart["points"]]), (True, [5.0, 5.1, 5.2]))

    def test_without_enough_per_second_readings_it_falls_back_to_the_ten_second_ones(self):
        live = [{"t": self.iso(self.NOW_MS - 10000), "mbit": 4.0}, {"t": self.iso(self.NOW_MS - 20000), "mbit": 4.1}]
        for name, stream in (("no fine block (an older listener)", {"live": live}), ("an empty block", {"fine": {"step_seconds": 1, "start": None, "mbit": []}, "live": live}),
                             ("a single second", {"fine": self.block(1, [5.0]), "live": live}), ("rubbish", {"fine": "x", "live": live})):
            with self.subTest(name):
                chart = node_eval([f"vzChartPoints({json.dumps(stream)}, {self.NOW}, 600)"])[0]
                self.assertEqual((chart["fine"], [p["mbit"] for p in chart["points"]]), (False, [4.1, 4.0]))

    def test_with_nothing_at_all_the_chart_has_no_points(self):
        chart = node_eval([f"vzChartPoints({{}}, {self.NOW}, 600)"])[0]
        self.assertEqual((chart["fine"], chart["points"]), (False, []))

    def test_the_line_breaks_where_nobody_reported_for_more_than_three_seconds(self):
        points = [{"ms": ms} for ms in (0, 1000, 2000, 10000, 11000)]
        segments = node_eval([f"vzSplit({json.dumps(points)}, 3000)"])[0]
        self.assertEqual([[p["ms"] for p in seg] for seg in segments], [[0, 1000, 2000], [10000, 11000]])
        self.assertEqual(len(node_eval([f"vzSplit({json.dumps(points[:3])}, 3000)"])[0]), 1)
        self.assertEqual(node_eval(["vzSplit([], 3000)"])[0], [])

    def dots(self, count, fine, lone_at=None):
        places = [{"x": i, "y": 1, "ms": i * 1000} for i in range(count)]
        pieces = [[p] for p in places] if lone_at == "all" else [places]
        return [p["ms"] for p in node_eval([f"vzDots({json.dumps(places)}, {json.dumps(pieces)}, {json.dumps(fine)})"])[0]]

    def test_per_second_readings_get_one_dot_on_the_newest_not_a_dot_each(self):
        self.assertEqual(self.dots(300, True), [299000])

    def test_a_lone_per_second_reading_between_two_gaps_gets_a_dot_because_a_line_cannot_show_it(self):
        places = [{"x": 0, "y": 1, "ms": 0}, {"x": 1, "y": 1, "ms": 1000}, {"x": 9, "y": 1, "ms": 9000}, {"x": 20, "y": 1, "ms": 20000}, {"x": 21, "y": 1, "ms": 21000}]
        pieces = [places[:2], places[2:3], places[3:]]
        self.assertEqual([p["ms"] for p in node_eval([f"vzDots({json.dumps(places)}, {json.dumps(pieces)}, true)"])[0]], [9000, 21000])

    def test_ten_second_readings_keep_a_dot_each_up_to_forty_and_none_beyond(self):
        self.assertEqual(len(self.dots(40, False)), 40)
        self.assertEqual(self.dots(41, False), [])

    def test_no_readings_no_dots(self):
        self.assertEqual((self.dots(0, True), self.dots(0, False)), ([], []))

    def test_a_place_carries_the_time_of_its_reading_so_the_line_can_be_broken(self):
        points = [{"ms": self.NOW_MS - 1000, "mbit": 5.0}]
        places = node_eval([f"vzPlace({json.dumps(points)}, {self.NOW}, 600, {{low: 0, high: 10}}, 240, 60)"])[0]
        self.assertEqual(places[0]["ms"], self.NOW_MS - 1000)

    def test_the_chart_draws_the_line_in_pieces_one_dot_on_the_newest_second_and_says_a_reading_every_second(self):
        body = function_body("vzLive")
        self.assertIn("vzChartPoints(s, now, vzWindowSeconds)", body)
        self.assertIn("vzSplit(places, 6000)", body)
        self.assertIn("vzDots(places, pieces, chart.fine)", body)
        self.assertIn("a reading every second", body)
        self.assertIn("a dot every ", body)                      # the ten-second fallback still says what its dots are
        self.assertNotIn("innerHTML", body)

    def test_the_fine_fallback_functions_build_nothing_in_the_page(self):
        for name in ("vzFineWindow", "vzChartPoints", "vzSplit", "vzDots"):
            self.assertNotIn("document.", function_body(name))
            self.assertNotIn("innerHTML", function_body(name))

    def test_the_page_names_no_pc_for_the_per_second_readings(self):
        for name in ("vzFineWindow", "vzChartPoints", "vzSplit", "vzLive"):
            for word in ("MAIN", "BACKUP-SERVER", "192.168"):
                self.assertNotIn(word, function_body(name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
