"""Unit tests for the listener's pure logic. Runs locally with no server and no FastAPI install:
a tiny stub stands in for the fastapi package, since the functions under test never touch it.

Run: python test_listener_units.py
"""
import asyncio
import copy
import importlib
import os
import re
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

LISTENER_DIR = Path(__file__).resolve().parent.parent / "core-listener"
TEST_TOKEN = "test-token-123"


def install_fastapi_stub() -> None:
    fastapi = types.ModuleType("fastapi")
    responses = types.ModuleType("fastapi.responses")

    class HTTPException(Exception):
        def __init__(self, status_code, detail=""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class FastAPI:
        def __init__(self, **kwargs):
            pass

        def _decorator(self, *args, **kwargs):
            return lambda func: func

        post = get = on_event = _decorator

        def add_middleware(self, *args, **kwargs):
            pass

    class Request:
        pass

    class HTMLResponse:
        def __init__(self, content, status_code=200):
            self.content = content
            self.status_code = status_code

    class JSONResponse:
        def __init__(self, content, status_code=200, headers=None):
            self.content = content
            self.status_code = status_code
            self.headers = headers

    class StreamingResponse:
        def __init__(self, content, media_type=None, status_code=200, headers=None, background=None):
            self.content = content
            self.body_iterator = content          # the real class names its iterator this
            self.media_type = media_type
            self.status_code = status_code
            self.headers = headers

        async def stream_response(self, send):
            # What the real class does: start the response, hand each chunk to `send`, finish.
            await send({"type": "http.response.start", "status": self.status_code, "headers": []})
            async for chunk in self.body_iterator:
                await send({"type": "http.response.body", "body": chunk if isinstance(chunk, bytes) else str(chunk).encode(), "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    class Response:
        def __init__(self, content=None, media_type=None, status_code=200, headers=None):
            self.content = content
            self.media_type = media_type
            self.status_code = status_code
            self.headers = headers

    fastapi.Depends = lambda dependency=None: dependency
    fastapi.FastAPI = FastAPI
    fastapi.Header = lambda default=None, **kwargs: default
    fastapi.HTTPException = HTTPException
    fastapi.Query = lambda default=None, **kwargs: default
    fastapi.Request = Request
    responses.JSONResponse = JSONResponse
    responses.HTMLResponse = HTMLResponse
    responses.StreamingResponse = StreamingResponse
    responses.Response = Response
    fastapi.responses = responses
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses


install_fastapi_stub()
os.environ["WATCHDOG_SHARED_TOKEN"] = TEST_TOKEN
os.environ["WATCHDOG_EXPECTED_INSTANCES"] = '{"MAIN": ["usa", "caribbean"], "BACKUP-SERVER": ["usa", "caribbean"]}'
sys.path.insert(0, str(LISTENER_DIR))
listener = importlib.import_module("app.main")
guard = importlib.import_module("app.guard")


class TokenCheckTests(unittest.TestCase):
    def test_correct_token_is_accepted(self):
        listener.require_token(TEST_TOKEN)  # no exception means accepted

    def test_previous_token_is_accepted_only_during_a_rotation_window(self):
        saved = listener.WRITE_TOKENS
        listener.WRITE_TOKENS = (TEST_TOKEN, "previous-token")
        try:
            listener.require_token("previous-token")
            self.assertTrue(listener._view_token_matches("previous-token"))
        finally:
            listener.WRITE_TOKENS = saved

    def test_missing_or_wrong_tokens_get_401(self):
        for presented in [None, "", "wrong", TEST_TOKEN[:-1], TEST_TOKEN + "x", "tést-token-123"]:
            with self.subTest(presented=presented):
                with self.assertRaises(listener.HTTPException) as caught:
                    listener.require_token(presented)
                self.assertEqual(caught.exception.status_code, 401)

    def test_listener_refuses_to_start_without_token(self):
        saved = os.environ.pop("WATCHDOG_SHARED_TOKEN")
        try:
            del sys.modules["app.main"]
            with self.assertRaises(RuntimeError):
                importlib.import_module("app.main")
        finally:
            os.environ["WATCHDOG_SHARED_TOKEN"] = saved
            sys.modules.pop("app.main", None)
            importlib.import_module("app.main")


class ExpectedInstancesConfigTests(unittest.TestCase):
    def test_counts_and_feed_lists_are_parsed_from_env(self):
        self.assertEqual(
            listener.EXPECTED_INSTANCES,
            {"MAIN": ["usa", "caribbean"], "BACKUP-SERVER": ["usa", "caribbean"]},
        )

    def test_bad_specs_are_rejected_at_startup(self):
        for bad in ["2", True, [], [1, 2], None]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    listener._parse_expected(bad)


class EvaluateInstancesTests(unittest.TestCase):
    def test_count_expectation(self):
        self.assertEqual(listener.evaluate_instances({"instance_count": 2}, 2), (True, []))
        self.assertEqual(
            listener.evaluate_instances({"instance_count": 1}, 2), (False, ["1 of 2 instances running"])
        )

    def test_count_not_judged_when_absent(self):
        self.assertEqual(listener.evaluate_instances({"instance_count": None}, 2), (None, []))
        self.assertEqual(listener.evaluate_instances({}, 2), (None, []))

    def test_named_feed_expectation(self):
        both = {"instances": [{"feed": "usa"}, {"feed": "caribbean"}]}
        self.assertEqual(listener.evaluate_instances(both, ["usa", "caribbean"]), (True, []))

    def test_missing_named_feed_is_named(self):
        caribbean_only = {"instances": [{"feed": "caribbean"}]}
        self.assertEqual(
            listener.evaluate_instances(caribbean_only, ["usa", "caribbean"]),
            (False, ["USA feed not running"]),
        )

    def test_wrong_program_in_place_of_a_feed_is_still_caught(self):
        # GLOBAL_2 (installed in admax2) started instead of NETWORK: the count reads 2, the feed check does not.
        wrong_program = {"instances": [{"feed": "unknown"}, {"feed": "caribbean"}]}
        self.assertEqual(
            listener.evaluate_instances(wrong_program, ["usa", "caribbean"]),
            (False, ["USA feed not running"]),
        )

    def test_machine_without_expectation_is_not_judged(self):
        self.assertEqual(listener.evaluate_instances({"instance_count": 0}, None), (None, []))


class NextAlertActionTests(unittest.TestCase):
    def test_alert_and_restore_happen_once_each(self):
        cases = [
            (None, False, "missing"),       # first short reading, including a machine already short at startup
            ("ok", False, "missing"),       # full -> short: one alert
            ("missing", False, None),       # still short: no repeat, before or after a restart
            ("missing", True, "restored"),  # short -> full: one note
            ("ok", True, None),
            (None, True, None),
            (None, None, None),             # not judged: nothing changes
            ("missing", None, None),
        ]
        for current, verdict, expected in cases:
            with self.subTest(current=current, verdict=verdict):
                self.assertEqual(listener.next_alert_action(current, verdict), expected)


class EndpointStateTests(unittest.TestCase):
    def endpoint(self, endpoint_id):
        return next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == endpoint_id)

    def source_row(self, source_id, payload, last_seen=None, interval=10):
        seen = last_seen or datetime.now(timezone.utc)
        return {
            "source_id": source_id,
            "last_seen_utc": seen.isoformat(),
            "last_payload_json": listener.json.dumps(payload),
            "state": "healthy",
            "state_changed_utc": seen.isoformat(),
            "expected_interval_seconds": interval,
            "instance_alert": None,
            "expected_instances": None,
            "latest_event": None,
        }

    def test_missing_named_feed_stays_visible_and_critical(self):
        now = datetime.now(timezone.utc)
        source = self.source_row(
            "BACKUP-SERVER",
            {"instances": [{"feed": "caribbean", "process_name": "STATION_BOX_OFFICE_BACKUP.exe", "pid_value": 802640}],
             "_absent": {"usa": (now - timedelta(seconds=60)).isoformat()}},          # Core's clock: gone for 60 s, past the 45 s grace
            now,
        )
        endpoint = listener._endpoint_snapshot(self.endpoint("BACKUP-USA"), source, [], now)
        self.assertEqual(endpoint["endpoint_id"], "BACKUP-USA")
        self.assertEqual(endpoint["status_level"], "critical")
        self.assertEqual(endpoint["root_cause"], "Process Missing")

    def test_missing_feed_event_does_not_contaminate_sibling_tile(self):
        now = datetime.now(timezone.utc)
        source = self.source_row(
            "BACKUP-SERVER",
            {"instances": [{"feed": "caribbean", "process_name": "STATION_BOX_OFFICE_BACKUP.exe", "pid_value": 802640}],
             "_absent": {"usa": (now - timedelta(seconds=60)).isoformat()}},
            now,
        )
        event = {
            "source_id": "BACKUP-SERVER",
            "timestamp_utc": now.isoformat(),
            "event_type": "instance_missing",
            "severity": "critical",
            "detail": "USA feed not running",
        }
        usa = listener._endpoint_snapshot(self.endpoint("BACKUP-USA"), source, [event], now)
        caribbean = listener._endpoint_snapshot(self.endpoint("BACKUP-Caribbean"), source, [event], now)
        self.assertEqual(usa["status_level"], "critical")
        self.assertEqual(usa["root_cause"], "Process Missing")
        self.assertEqual(caribbean["status_level"], "healthy")

    def test_endpoint_critical_event_is_not_status_feedback_loop(self):
        now = datetime.now(timezone.utc)
        source = self.source_row(
            "BACKUP-SERVER",
            {
                "instances": [
                    {"feed": "usa", "process_name": "STATION_NETWORK_BACKUP.exe", "pid_value": 101},
                    {"feed": "caribbean", "process_name": "STATION_BOX_OFFICE_BACKUP.exe", "pid_value": 202},
                ]
            },
            now,
        )
        event = {
            "source_id": "BACKUP-SERVER",
            "timestamp_utc": now.isoformat(),
            "event_type": "endpoint_critical",
            "severity": "critical",
            "detail": listener.json.dumps({"endpoint": "BACKUP - USA", "root_cause": "Process Missing"}),
        }
        usa = listener._endpoint_snapshot(self.endpoint("BACKUP-USA"), source, [event], now)
        caribbean = listener._endpoint_snapshot(self.endpoint("BACKUP-Caribbean"), source, [event], now)
        self.assertEqual(usa["status_level"], "healthy")
        self.assertEqual(caribbean["status_level"], "healthy")

    def test_internal_event_does_not_hide_active_detector_event(self):
        now = datetime.now(timezone.utc)
        source = self.source_row("SITE-A", {"current_item": "Luzemoliv Gefi TO.25.mp4"}, now)
        internal_event = {
            "source_id": "SITE-A",
            "timestamp_utc": now.isoformat(),
            "event_type": "endpoint_critical",
            "severity": "critical",
            "detail": listener.json.dumps({"endpoint": "SITE-A", "root_cause": "Freeze > 60s"}),
        }
        freeze_event = {
            "source_id": "SITE-A",
            "timestamp_utc": (now - timedelta(seconds=1)).isoformat(),
            "event_type": "freeze",
            "severity": "warning",
            "detail": listener.json.dumps({"phase": "ongoing", "duration_s": 61}),
        }
        snapshot = listener._endpoint_snapshot(self.endpoint("SITE-A"), source, [internal_event, freeze_event], now)
        self.assertEqual(snapshot["status_level"], "critical")
        self.assertEqual(snapshot["root_cause"], "Freeze > 60s")

    def test_feed_scoped_detector_event_applies_only_to_matching_tile(self):
        now = datetime.now(timezone.utc)
        source = self.source_row(
            "BACKUP-SERVER",
            {
                "instances": [
                    {"feed": "usa", "process_name": "STATION_NETWORK_BACKUP.exe", "pid_value": 101},
                    {"feed": "caribbean", "process_name": "STATION_BOX_OFFICE_BACKUP.exe", "pid_value": 202},
                ]
            },
            now,
        )
        event = {
            "source_id": "BACKUP-SERVER",
            "timestamp_utc": now.isoformat(),
            "event_type": "off_air",
            "severity": "critical",
            "detail": listener.json.dumps({"feed": "usa", "phase": "ongoing"}),
        }
        usa = listener._endpoint_snapshot(self.endpoint("BACKUP-USA"), source, [event], now)
        caribbean = listener._endpoint_snapshot(self.endpoint("BACKUP-Caribbean"), source, [event], now)
        self.assertEqual(usa["status_level"], "critical")
        self.assertEqual(usa["root_cause"], "Off-Air Status")
        self.assertEqual(caribbean["status_level"], "healthy")

    def test_freeze_thresholds_escalate_warning_then_critical(self):
        now = datetime.now(timezone.utc)
        source = self.source_row("SITE-A", {"current_item": "Luzemoliv Gefi TO.25.mp4"}, now)
        def event_with(duration):
            return {
                "source_id": "SITE-A",
                "timestamp_utc": now.isoformat(),
                "event_type": "freeze",
                "severity": "warning",
                "detail": listener.json.dumps({"phase": "ongoing", "duration_s": duration}),
            }

        site_a = self.endpoint("SITE-A")
        # Under 45 s a fault is recorded in the feed but does not colour the tile.
        self.assertEqual(listener._endpoint_snapshot(site_a, source, [event_with(31)], now)["status_level"], "healthy")
        warning = listener._endpoint_snapshot(site_a, source, [event_with(46)], now)
        self.assertEqual(warning["status_level"], "warning")
        self.assertEqual(warning["root_cause"], "Freeze > 45s")
        critical = listener._endpoint_snapshot(site_a, source, [event_with(61)], now)
        self.assertEqual(critical["status_level"], "critical")
        self.assertEqual(critical["root_cause"], "Freeze > 60s")

    def test_phase_end_event_clears_older_same_type_fault(self):
        now = datetime.now(timezone.utc)
        source = self.source_row("SITE-A", {"current_item": "Luzemoliv Gefi TO.25.mp4"}, now)
        end_event = {
            "source_id": "SITE-A",
            "timestamp_utc": now.isoformat(),
            "event_type": "freeze",
            "severity": "warning",
            "detail": listener.json.dumps({"phase": "end", "duration_s": 2}),
        }
        older_ongoing = {
            "source_id": "SITE-A",
            "timestamp_utc": (now - timedelta(minutes=7)).isoformat(),
            "event_type": "freeze",
            "severity": "warning",
            "detail": listener.json.dumps({"phase": "ongoing", "duration_s": 420}),
        }
        snapshot = listener._endpoint_snapshot(self.endpoint("SITE-A"), source, [end_event, older_ongoing], now)
        self.assertEqual(snapshot["status_level"], "healthy")

    def test_advancement_thresholds_escalate_warning_then_critical(self):
        now = datetime.now(timezone.utc)
        source = self.source_row("SITE-B", {
            "now_playing": {"filename": "PETA HEN WUT 24.mp4"},
            "advance_age_seconds": 46,
            "advance_alarm_eligible": True,
            "advance_alarm_confirmed": True,
            "advance_source": "Mainplaylist.xml",
        }, now)
        warning = listener._endpoint_snapshot(self.endpoint("SITE-B"), source, [], now)
        self.assertEqual(warning["status_level"], "warning")
        self.assertEqual(warning["root_cause"], "Paused/Frozen > 45s")
        payload = listener.json.loads(source["last_payload_json"])
        payload["advance_age_seconds"] = 61
        source["last_payload_json"] = listener.json.dumps(payload)
        critical = listener._endpoint_snapshot(self.endpoint("SITE-B"), source, [], now)
        self.assertEqual(critical["status_level"], "critical")
        self.assertEqual(critical["root_cause"], "Paused/Frozen > 60s")

    def test_advisory_file_age_does_not_trigger_false_alarm(self):
        now = datetime.now(timezone.utc)
        source = self.source_row("SITE-B", {
            "now_playing": {"filename": "PETA HEN WUT 24.mp4"},
            "advance_age_seconds": 3600,
            "advance_alarm_eligible": False,
            "advance_monitoring_model": "playlist_metadata_only",
        }, now)
        snapshot = listener._endpoint_snapshot(self.endpoint("SITE-B"), source, [], now)
        self.assertEqual(snapshot["status_level"], "healthy")

    def test_feed_advancement_fault_does_not_contaminate_sibling_tile(self):
        now = datetime.now(timezone.utc)
        source = self.source_row(
            "BACKUP-SERVER",
            {
                "instances": [
                    {"feed": "usa", "process_name": "STATION_NETWORK_BACKUP.exe", "pid_value": 101, "advance_age_seconds": 61, "advance_alarm_eligible": True, "advance_alarm_confirmed": True},
                    {"feed": "caribbean", "process_name": "STATION_BOX_OFFICE_BACKUP.exe", "pid_value": 202, "advance_age_seconds": 5, "advance_alarm_eligible": True, "advance_alarm_confirmed": True},
                ]
            },
            now,
        )
        usa = listener._endpoint_snapshot(self.endpoint("BACKUP-USA"), source, [], now)
        caribbean = listener._endpoint_snapshot(self.endpoint("BACKUP-Caribbean"), source, [], now)
        self.assertEqual(usa["status_level"], "critical")
        self.assertEqual(usa["root_cause"], "Paused/Frozen > 60s")
        self.assertEqual(caribbean["status_level"], "healthy")

    def test_heartbeat_overdue_respects_expected_cadence(self):
        now = datetime.now(timezone.utc)
        source = self.source_row("SITE-B", {"current_item": "PETA HEN WUT 24.mp4"}, now - timedelta(seconds=350), interval=300)
        warning = listener._endpoint_snapshot(self.endpoint("SITE-B"), source, [], now)
        self.assertEqual(warning["status_level"], "warning")
        self.assertEqual(warning["root_cause"], "Heartbeat Delay > 45s")
        source["last_seen_utc"] = (now - timedelta(seconds=370)).isoformat()
        critical = listener._endpoint_snapshot(self.endpoint("SITE-B"), source, [], now)
        self.assertEqual(critical["status_level"], "critical")
        self.assertEqual(critical["root_cause"], "Heartbeat Timeout > 60s")


class EmailRowTests(unittest.TestCase):
    ADMAX_PAYLOAD = {
        "hostname": "BACKUP-SERVER",
        "timestamp": "2026-10-05T08:55:53Z",
        "expected_interval_seconds": 10,
        "instance_count": 1,
        "instances": [
            {"slot": "admax3", "feed": "caribbean", "process_name": "STATION_BOX_OFFICE_BACKUP.exe",
             "pid_value": 802640, "executable_path": "C:\\x"},
        ],
    }

    def test_payload_is_not_mutated(self):
        before = copy.deepcopy(self.ADMAX_PAYLOAD)
        listener._humanize_json(self.ADMAX_PAYLOAD)
        self.assertEqual(self.ADMAX_PAYLOAD, before)

    def test_instances_render_as_one_readable_line(self):
        rows = listener._humanize_json(self.ADMAX_PAYLOAD)
        self.assertIn(("Instance", "CARIBBEAN: STATION_BOX_OFFICE_BACKUP.exe, PID 802640"), rows)

    def test_internal_and_identity_fields_are_hidden(self):
        labels = [label for label, _ in listener._humanize_json(self.ADMAX_PAYLOAD)]
        self.assertNotIn("Hostname", labels)
        self.assertNotIn("Timestamp", labels)
        self.assertNotIn("Expected interval seconds", labels)

    def test_non_object_json_falls_back_to_plain_detail(self):
        self.assertEqual(listener._humanize_json("[1, 2]"), [("Detail", "[1, 2]")])

    def test_insta_instance_line(self):
        line = listener._humanize_instance({"feed": "usa", "launcher_present": True, "engine_present": False})
        self.assertEqual(line, "USA: launcher up, engine DOWN")

    def test_expectation_wording(self):
        self.assertEqual(listener._describe_expectation(2), "2 playout instances")
        self.assertEqual(listener._describe_expectation(["usa", "caribbean"]), "USA and Caribbean feeds")

    def test_no_em_dash_in_rendered_output(self):
        rendered = " ".join(
            f"{label} {value}" for label, value in listener._humanize_json(self.ADMAX_PAYLOAD)
        ) + listener._render_alert_html("MAIN", "instance missing", "Only 1 of 2", [("a", "b")])
        self.assertNotIn("\u2014", rendered)

    def test_alert_html_escapes_sender_values(self):
        rendered = listener._render_alert_html(
            '<img src=x onerror="bad">',
            "warning <script>alert(1)</script>",
            "Summary & <b>unsafe</b>",
            [("Label", 'Value <script>alert("bad")</script> & more')],
        )
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&amp;", rendered)


class DashboardPageTests(unittest.TestCase):
    def test_page_embeds_the_real_token_and_writes_values_as_text(self):
        # The page is meant to load straight into live status with nothing to type -- the
        # server's own token is embedded at render time, not withheld and asked for.
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertIn("<title>Clarix Broadcast Watchdog</title>", page)
        self.assertIn('var token = "test-token-123";', page)
        self.assertNotIn("innerHTML", page)

    def test_token_is_json_escaped_not_string_concatenated(self):
        # A token containing a quote must not be able to break out of the JS string literal.
        page = listener.render_dashboard_html('a"b\\c')
        self.assertIn('var token = "a\\"b\\\\c";', page)

    def test_token_never_duplicated_into_persistent_storage(self):
        # localStorage IS used on this page now (a sound on/off preference), so a blunt "no
        # localStorage substring" check would no longer mean anything. What has to stay true
        # is that the token value itself appears exactly once in the page -- its own
        # declaration -- never copied into a storage call or anywhere else.
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertEqual(page.count(TEST_TOKEN), 1)
        self.assertNotIn("sessionStorage", page)
        self.assertIn("watchdog-sound", page)  # the one legitimate localStorage key

    def test_dashboard_renders_six_independent_endpoints(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        for label in ["SITE-A", "SITE-B", "MAIN-USA", "MAIN-Caribbean", "BACKUP-USA", "BACKUP-Caribbean"]:
            with self.subTest(label=label):
                self.assertIn(f'label:"{label}"', page)
        self.assertIn("Now playing", page)
        self.assertIn("status-critical", page)
        self.assertIn("badge-warn", page)

    def test_dashboard_header_and_alarm_assets_are_current(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertIn("<h1 id=\"page-title\">Clarix Watchdog</h1>", page)
        self.assertIn("class=\"logo-svg\"", page)
        self.assertIn("cyber-eye", page)
        self.assertIn("620", page)
        self.assertIn("1240", page)
        self.assertNotIn("MCR PRIMARY", page)
        self.assertNotIn("STATION LIVE OPERATIONS", page.upper())
        self.assertNotIn("FASTAPI", page.upper())
        self.assertNotIn("SQLITE", page.upper())

    def test_alarm_is_one_oscillator_for_the_life_of_the_page(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        # The only place an oscillator is made is buildAlarmTone, guarded so it runs once.
        self.assertEqual(page.count("createOscillator"), 1)
        self.assertIn("if (alarm.osc || !audioCtx) { return; }", page)
        # No per-cycle timer that could stack tones, and no per-cycle node creation.
        self.assertNotIn("setInterval(policeSirenOnce", page)
        self.assertNotIn("policeSirenOnce", page)
        self.assertEqual(page.count("new Ctor()"), 1)  # one shared AudioContext
        self.assertIn("createDynamicsCompressor", page)  # limiter ahead of the speakers

    def test_preloader_follows_the_connection_and_cannot_trap_the_operator(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertIn('id="preloader"', page)
        self.assertIn("loader-dog", page)
        self.assertIn("hidePreloader();", page)
        self.assertIn("Show dashboard anyway", page)  # escape hatch when the stream is slow
        self.assertIn("prefers-reduced-motion", page)

    def test_diagnostics_text_wraps_by_word_not_by_letter(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertIn("white-space:normal; word-wrap:break-word; overflow-wrap:break-word; word-break:normal;", page)
        self.assertNotIn("word-break:break-word", page)
        self.assertIn(".details-grid { display:grid; grid-template-columns:minmax(0, 1fr);", page)

    def test_topology_is_a_grid_so_cards_cannot_overlap_and_lines_follow_the_measured_cards(self):
        page = listener.render_topology_html(TEST_TOKEN)
        # a real grid with the Core card in the middle column, not absolutely positioned cards on a fixed canvas
        self.assertIn("grid-template-columns:minmax(0, 1fr) minmax(230px, 310px) minmax(0, 1fr);", page)
        self.assertIn('el("div", undefined, "holo-stage")', page)
        self.assertNotIn("topology-plane", page)
        self.assertNotIn("topologyEdgePoint", page)
        self.assertNotIn("scale(", page.split("function layoutHoloLines")[1].split("function fitTopologyPlane")[0])
        # lines come from where the cards ACTUALLY are, and are redrawn when anything changes size
        self.assertIn("function layoutHoloLines()", page)
        self.assertIn("getBoundingClientRect()", page.split("function layoutHoloLines")[1])
        self.assertIn("new ResizeObserver", page)
        # a long file name is clamped to three lines (full text on hover) instead of growing the card without limit
        self.assertIn("-webkit-line-clamp:3", page)
        self.assertIn("part.now.title = state.nowPlaying.title;", page)
        # the DOM is built once and updated in place, so the animations keep running between live frames
        self.assertIn("function updateHolo(states)", page)
        self.assertIn("if (!holo.stage || holo.signature !== signature", page)
        self.assertNotIn("Core Engine", page)

    def test_topology_motion_respects_reduced_motion(self):
        page = listener.render_topology_html(TEST_TOKEN)
        self.assertIn(".holo-stage::before { animation:holoFloor", page)
        self.assertIn("@media (prefers-reduced-motion:no-preference) {", page)
        self.assertIn("@media (prefers-reduced-motion:reduce) { .holo-packet { display:none; } }", page)

    def test_topology_works_on_a_phone_held_upright(self):
        page = listener.render_topology_html(TEST_TOKEN)
        # No more "rotate your phone": the map is swapped for a stacked layout by width, not orientation.
        self.assertNotIn("Rotate device", page)
        self.assertNotIn("rotate-overlay", page)
        self.assertNotIn("orientation:portrait", page)
        self.assertIn("@media (max-width:980px)", page)
        self.assertIn(".holo-stage { display:none; }", page)
        self.assertIn(".topology-stack { display:block; }", page)
        # the stack and its tappable cards exist, and the details are no longer hidden on phones
        self.assertIn("function renderTopologyStack(states)", page)
        self.assertIn("topologyRoot.appendChild(renderTopologyStack(states));", page)
        self.assertIn("min-height:44px", page)  # thumb-sized tap targets
        # names wrap at the hyphen, never mid-word, and the badge sits above the name instead of squeezing it
        self.assertIn(".stack-title { display:flex; flex-direction:column-reverse;", page)
        self.assertNotIn(".stack-name { font-size:13px; font-weight:900; letter-spacing:.04em; text-transform:uppercase; overflow-wrap:anywhere; }", page)
        # until the operator taps, the details panel follows the most urgent playout
        self.assertIn("function mostUrgentEndpointId(states)", page)
        self.assertIn("if (!uiState.userSelected)", page)
        self.assertNotIn(".topology-wrap, .topology-detail { display:none; }", page)
        self.assertNotIn("innerHTML", page)

    def test_header_bulldog_is_the_shared_mark(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertEqual(page.count('class="logo-svg"'), 1)
        self.assertEqual(page.count('class="loader-dog"'), 1)
        self.assertIn("M25,40 L95,40 L105,65", page)  # the head outline, from the single BULLDOG_BODY

    def test_topology_page_uses_stream_not_status_polling(self):
        page = listener.render_topology_html(TEST_TOKEN)
        self.assertIn('var pageMode = "topology";', page)
        self.assertIn('new EventSource("/stream?token="', page)
        self.assertNotIn('fetch("/status', page)


def edge_instance(feed, age=0, steady=300, uptime=3600, advancing=True, cpu=1.2):
    """One player as the edge sender reports it once it measures engine activity."""
    return {
        "feed": feed,
        "launcher_present": True,
        "engine_present": True,
        "engine_pid": 100 if feed == "usa" else 200,
        "advance_age_seconds": age,
        "advance_alarm_confirmed": True,
        "advancing": advancing,
        "advance_confirmed_seconds": steady,
        "engine_uptime_seconds": uptime,
        "engine_cpu_cores": cpu,
        "advance_source": "engine CPU",
    }


def edge_payload(*instances, monitored=True):
    payload = {"hostname": "MAIN", "instance_count": len(instances), "instances": list(instances)}
    if monitored:
        payload["activity_monitor"] = {"version": 1, "floor_cores": 0.3}
    return payload


class FrozenPlayoutTests(unittest.TestCase):
    """Yellow after 45 s without engine activity, red after 60 s, per player, independently."""

    def snapshot(self, endpoint_id, payload, stored=None, source="MAIN"):
        now = datetime.now(timezone.utc)
        row = {
            "source_id": source, "last_seen_utc": now.isoformat(), "last_payload_json": listener.json.dumps(payload),
            "state": "healthy", "state_changed_utc": now.isoformat(), "expected_interval_seconds": 10,
            "instance_alert": None, "expected_instances": None, "latest_event": None,
        }
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == endpoint_id)
        return listener._endpoint_snapshot(endpoint, row, [], now, stored)

    def test_thresholds_are_45_and_60(self):
        self.assertEqual((listener.WARNING_OVERDUE_SECONDS, listener.CRITICAL_OVERDUE_SECONDS), (45, 60))
        for age, level in [(0, "healthy"), (44, "healthy"), (45, "warning"), (59, "warning"), (60, "critical"), (500, "critical")]:
            with self.subTest(age=age):
                got = self.snapshot("MAIN-USA", edge_payload(edge_instance("usa", age=age), edge_instance("caribbean")))
                self.assertEqual(got["status_level"], level)

    def test_each_player_is_judged_on_its_own_motion(self):
        payload = edge_payload(edge_instance("usa", age=75, cpu=0.01), edge_instance("caribbean", age=0))
        usa = self.snapshot("MAIN-USA", payload)
        caribbean = self.snapshot("MAIN-Caribbean", payload)
        self.assertEqual(usa["status_level"], "critical")
        self.assertEqual(usa["root_cause"], "Paused/Frozen > 60s")
        self.assertIn("0.01 cores", usa["status_detail"])
        self.assertTrue(usa["email_eligible"])
        self.assertEqual(caribbean["status_level"], "healthy")

    def test_surviving_player_is_never_mistaken_for_the_missing_one(self):
        # Only the Caribbean player is left. The USA tile must be the red one.
        payload = edge_payload(edge_instance("caribbean"))
        payload["_absent"] = {"usa": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()}      # gone for 60 s, past the 45 s grace
        usa = self.snapshot("MAIN-USA", payload)
        caribbean = self.snapshot("MAIN-Caribbean", payload)
        self.assertEqual((usa["status_level"], usa["root_cause"]), ("critical", "Process Missing"))
        self.assertEqual(usa["status_label"], "Player Down")
        self.assertEqual(caribbean["status_level"], "healthy")

    def test_engine_is_picked_over_a_client_window_of_the_same_feed(self):
        client = {"feed": "usa", "process_name": "STATION_NETWORK_BACKUP_CLIENT.exe", "pid_value": 5}
        engine = dict(edge_instance("usa", age=70), process_name="STATION_NETWORK_BACKUP.exe", pid_value=6)
        payload = edge_payload(client, engine, edge_instance("caribbean"))
        got = self.snapshot("BACKUP-USA", payload, source="BACKUP-SERVER")
        self.assertEqual(got["status_level"], "critical")

    def test_sender_that_does_not_confirm_never_raises_a_motion_alarm(self):
        instance = edge_instance("usa", age=900)
        instance["advance_alarm_confirmed"] = False
        got = self.snapshot("MAIN-USA", edge_payload(instance, edge_instance("caribbean")))
        self.assertEqual(got["status_level"], "healthy")
        self.assertIn("not measured", self.snapshot("MAIN-USA", edge_payload(instance, monitored=False))["motion_text"])


class HeldAlarmTests(unittest.TestCase):
    """A red alarm on MAIN / BACKUP-SERVER clears only when the edge confirms the player is advancing."""

    def snapshot(self, payload, stored):
        now = datetime.now(timezone.utc)
        row = {
            "source_id": "MAIN", "last_seen_utc": now.isoformat(), "last_payload_json": listener.json.dumps(payload),
            "state": "healthy", "state_changed_utc": now.isoformat(), "expected_interval_seconds": 10,
            "instance_alert": None, "expected_instances": None, "latest_event": None,
        }
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "MAIN-USA")
        return listener._endpoint_snapshot(endpoint, row, [], now, stored)

    STORED = {"endpoint_id": "MAIN-USA", "alert_level": "critical", "root_cause": "Paused/Frozen > 60s"}

    def test_a_restart_alone_does_not_clear_the_alarm(self):
        # New engine process: it is busy loading, so activity looks fine, but it is seconds old.
        fresh = edge_payload(edge_instance("usa", age=0, steady=10, uptime=20), edge_instance("caribbean"))
        got = self.snapshot(fresh, self.STORED)
        self.assertEqual(got["status_level"], "critical")
        self.assertTrue(got["held"])
        self.assertEqual(got["root_cause"], "Paused/Frozen > 60s")  # the original cause is kept
        self.assertFalse(got["email_eligible"])  # a continuation, never a second email
        self.assertIn("Alarm held", got["motion_text"])

    def test_activity_without_enough_steady_time_still_holds(self):
        confirm, grace = listener.CLEAR_CONFIRM_SECONDS, listener.ENGINE_STARTUP_GRACE_SECONDS   # the settings, not copies of their values
        for steady, uptime in [(confirm - 1, 600), (confirm, grace - 1), (0, 600), (None, 600), (600, None)]:
            with self.subTest(steady=steady, uptime=uptime):
                instance = edge_instance("usa", steady=steady, uptime=uptime)
                for key in ("advance_confirmed_seconds", "engine_uptime_seconds"):
                    if instance[key] is None:
                        del instance[key]
                got = self.snapshot(edge_payload(instance, edge_instance("caribbean")), self.STORED)
                self.assertEqual(got["status_level"], "critical")

    def test_the_defaults_clear_quickly_but_never_on_a_restart_alone(self):
        # 2026-10-11: clearing took up to 3 min after a relaunch; media reads are strong evidence, so the confirmation is short, but not zero
        self.assertEqual(listener.CLEAR_CONFIRM_SECONDS, 15)
        self.assertEqual(listener.ENGINE_STARTUP_GRACE_SECONDS, 30)
        restart_only = edge_payload(edge_instance("usa", steady=0, uptime=5), edge_instance("caribbean"))
        self.assertEqual(self.snapshot(restart_only, self.STORED)["status_level"], "critical")

    def test_confirmed_advancing_releases_the_alarm(self):
        steady = edge_payload(edge_instance("usa", steady=listener.CLEAR_CONFIRM_SECONDS, uptime=listener.ENGINE_STARTUP_GRACE_SECONDS), edge_instance("caribbean"))
        got = self.snapshot(steady, self.STORED)
        self.assertEqual(got["status_level"], "healthy")
        self.assertFalse(got["held"])

    def test_sender_that_is_not_confirming_activity_never_holds_forever(self):
        # An older sender cannot give the confirmation, so holding would mean an alarm that never clears.
        got = self.snapshot(edge_payload(edge_instance("usa"), edge_instance("caribbean"), monitored=False), self.STORED)
        self.assertEqual(got["status_level"], "healthy")

    def test_a_missing_process_is_held_after_it_comes_back(self):
        stored = {"endpoint_id": "MAIN-USA", "alert_level": "critical", "root_cause": "Process Missing"}
        back_just_now = edge_payload(edge_instance("usa", steady=5, uptime=8), edge_instance("caribbean"))
        self.assertEqual(self.snapshot(back_just_now, stored)["status_level"], "critical")
        settled = edge_payload(edge_instance("usa", steady=40, uptime=140), edge_instance("caribbean"))
        self.assertEqual(self.snapshot(settled, stored)["status_level"], "healthy")

    def test_site_a_and_site_b_are_not_held(self):
        now = datetime.now(timezone.utc)
        row = {
            "source_id": "SITE-A", "last_seen_utc": now.isoformat(), "last_payload_json": "{}", "state": "healthy",
            "state_changed_utc": now.isoformat(), "expected_interval_seconds": 30, "instance_alert": None,
            "expected_instances": None, "latest_event": None,
        }
        site_a = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "SITE-A")
        stored = {"endpoint_id": "SITE-A", "alert_level": "critical", "root_cause": "Black > 60s"}
        self.assertEqual(listener._endpoint_snapshot(site_a, row, [], now, stored)["status_level"], "healthy")


class DetectorMarkerTests(unittest.TestCase):
    """SITE_A: phase end means healthy, and a marker whose end event was lost cannot hold a tile on alarm."""

    def site_a(self, events):
        now = datetime.now(timezone.utc)
        row = {
            "source_id": "SITE-A", "last_seen_utc": now.isoformat(), "last_payload_json": "{}", "state": "healthy",
            "state_changed_utc": now.isoformat(), "expected_interval_seconds": 30, "instance_alert": None,
            "expected_instances": None, "latest_event": None,
        }
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "SITE-A")
        return listener._endpoint_snapshot(endpoint, row, events, now)

    def marker(self, age_s, ongoing_s=15.3, phase="ongoing", event_type="black"):
        stamp = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        return {
            "source_id": "SITE-A", "timestamp_utc": stamp.isoformat(), "event_type": event_type, "severity": "warning",
            "detail": listener.json.dumps({"phase": phase, "ongoing_s": ongoing_s}),
        }

    def test_phase_end_is_healthy(self):
        self.assertEqual(self.site_a([self.marker(2, phase="end"), self.marker(100)])["status_level"], "healthy")

    def test_fresh_marker_climbs_the_ladder_as_time_passes(self):
        for age, level in [(5, "healthy"), (30, "warning"), (46, "critical")]:
            with self.subTest(age=age):
                self.assertEqual(self.site_a([self.marker(age)])["status_level"], level)

    def test_unclosed_marker_goes_unconfirmed_then_is_ignored(self):
        recent = self.site_a([self.marker(600)])
        self.assertEqual((recent["status_level"], recent["status_label"]), ("warning", "Unconfirmed"))
        self.assertFalse(recent["email_eligible"])
        self.assertIn("never reported it ended", recent["status_detail"])
        # The 14-hour-old marker that was holding SITE-A on a warning in production.
        self.assertEqual(self.site_a([self.marker(14 * 3600)])["status_level"], "healthy")


class RedundancyChannelTests(unittest.TestCase):
    """The channels that do not depend on the email path: a phone push for RED alarms and a dead man's switch."""

    def fake_urlopen(self, calls, fail=False, status=200):
        class Reply:
            def __init__(self):
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def opener(request, timeout=0):
            if fail:
                raise OSError("network is down")
            calls.append(request)
            return Reply()
        return opener

    def with_push(self, url, calls, fail=False):
        saved = (listener.PUSH_URL, listener.urllib.request.urlopen)
        listener.PUSH_URL = url
        listener.urllib.request.urlopen = self.fake_urlopen(calls, fail=fail)
        return saved

    def restore_push(self, saved):
        listener.PUSH_URL, listener.urllib.request.urlopen = saved

    def test_push_is_off_unless_configured(self):
        calls = []
        saved = self.with_push("", calls)
        try:
            listener.send_push_notice("Watchdog: MAIN - USA CRITICAL", "Paused/Frozen > 60s")
        finally:
            self.restore_push(saved)
        self.assertEqual(calls, [])

    def test_push_posts_an_urgent_notice(self):
        calls = []
        saved = self.with_push("https://push.example/topic", calls)
        try:
            listener.send_push_notice("Watchdog: MAIN - USA CRITICAL", "Paused/Frozen > 60s. Now playing: x.mp4")
        finally:
            self.restore_push(saved)
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(request.full_url, "https://push.example/topic")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Priority"), "urgent")
        self.assertIn(b"Paused/Frozen > 60s", request.data)

    def test_a_failed_push_never_raises_and_never_stops_the_email(self):
        calls = []
        saved = self.with_push("https://push.example/topic", calls, fail=True)
        try:
            listener.send_push_notice("t", "m")   # must not raise
        finally:
            self.restore_push(saved)

    def test_a_red_alarm_sends_the_email_and_then_the_push(self):
        order = []
        saved = (listener.send_alert_email, listener.send_push_notice)
        listener.send_alert_email = lambda *args, **kwargs: order.append("email")
        listener.send_push_notice = lambda title, message: order.append("push: " + title)
        try:
            listener._send_endpoint_critical_email({
                "display_name": "MAIN - USA", "root_cause": "Paused/Frozen > 60s", "status_label": "Critical Alarm", "status_detail": "d",
                "source_id": "MAIN", "now_playing": {"title": "x.mp4"}, "pid_text": "engine 1",
            })
        finally:
            listener.send_alert_email, listener.send_push_notice = saved
        self.assertEqual(order, ["email", "push: Watchdog: MAIN - USA CRITICAL"])

    def smtp_check(self, configured, login_works):
        class FakeSMTP:
            def __init__(self, *_a, **_k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def starttls(self, context=None):
                pass

            def login(self, *_a):
                if not login_works:
                    raise Exception("535 credentials refused")
        saved = (listener.smtplib.SMTP, listener.SMTP_HOST, listener.SMTP_USER, listener.SMTP_PASS)
        listener.smtplib.SMTP = FakeSMTP
        listener.SMTP_HOST, listener.SMTP_USER, listener.SMTP_PASS = (("smtp.test", "u", "p") if configured else (None, None, None))
        try:
            return listener.check_smtp_login()
        finally:
            listener.smtplib.SMTP, listener.SMTP_HOST, listener.SMTP_USER, listener.SMTP_PASS = saved

    def test_the_email_login_check(self):
        self.assertTrue(self.smtp_check(True, True))
        self.assertFalse(self.smtp_check(True, False))
        self.assertIsNone(self.smtp_check(False, True))

    def test_the_dead_man_pings_only_when_healthy(self):
        self.assertTrue(listener.deadman_should_ping(True, True))
        self.assertTrue(listener.deadman_should_ping(None, True))       # email not configured: nothing to check
        self.assertFalse(listener.deadman_should_ping(False, True))     # cannot log in to email: stop on purpose
        self.assertFalse(listener.deadman_should_ping(True, False))     # database does not answer
        self.assertFalse(listener.deadman_should_ping(False, False))

    def test_ping_reports_success_and_failure(self):
        calls = []
        saved = listener.urllib.request.urlopen
        try:
            listener.urllib.request.urlopen = self.fake_urlopen(calls)
            self.assertTrue(listener.ping_deadman("https://hc.example/abc"))
            listener.urllib.request.urlopen = self.fake_urlopen(calls, fail=True)
            self.assertFalse(listener.ping_deadman("https://hc.example/abc"))
        finally:
            listener.urllib.request.urlopen = saved

    def run_loop_for(self, smtp_result):
        import asyncio
        import tempfile
        pings = []
        tmp = tempfile.TemporaryDirectory()
        saved = (listener.DB_PATH, listener.DEADMAN_URL, listener.DEADMAN_INTERVAL_SECONDS, listener.check_smtp_login, listener.ping_deadman)
        listener.DB_PATH = Path(tmp.name) / "watchdog.db"
        listener.init_db()
        listener.DEADMAN_URL, listener.DEADMAN_INTERVAL_SECONDS = "https://hc.example/abc", 0.05
        listener.check_smtp_login = lambda: smtp_result
        listener.ping_deadman = lambda url: pings.append(url) or True
        try:
            try:
                asyncio.run(asyncio.wait_for(listener.deadman_loop(), timeout=0.4))
            except asyncio.TimeoutError:
                pass
        finally:
            listener.DB_PATH, listener.DEADMAN_URL, listener.DEADMAN_INTERVAL_SECONDS, listener.check_smtp_login, listener.ping_deadman = saved
            tmp.cleanup()
        return pings

    def test_the_real_loop_pings_while_healthy_and_goes_quiet_when_the_email_login_breaks(self):
        self.assertGreaterEqual(len(self.run_loop_for(True)), 3)
        self.assertEqual(self.run_loop_for(False), [])


class RedOnlyEmailPolicyTests(unittest.TestCase):
    """Email only when something is RED. The green recovered/restored notices are recorded but not mailed unless switched on."""

    def run_notice(self, enabled):
        sent = []
        saved = (listener.EMAIL_RECOVERY_NOTICES, listener.send_alert_email)
        listener.EMAIL_RECOVERY_NOTICES = enabled
        listener.send_alert_email = lambda *args, **kwargs: sent.append(args)
        try:
            listener._send_recovery_notice("MAIN", "heartbeat recovered", "MAIN is reporting again.", [("Recovered at", "now")])
        finally:
            listener.EMAIL_RECOVERY_NOTICES, listener.send_alert_email = saved
        return sent

    def test_recovery_notices_are_not_mailed_by_default(self):
        self.assertEqual(self.run_notice(False), [])

    def test_they_can_be_switched_back_on(self):
        self.assertEqual(len(self.run_notice(True)), 1)

    def test_the_default_is_off(self):
        self.assertFalse(listener.EMAIL_RECOVERY_NOTICES)

    def test_amber_never_makes_an_email_only_red_does(self):
        # a warning-level pause (48 s) is not mailed; the same pause at 62 s is
        now = datetime.now(timezone.utc)
        row = {"source_id": "MAIN", "last_seen_utc": now.isoformat(), "state": "healthy", "state_changed_utc": now.isoformat(),
               "expected_interval_seconds": 10, "instance_alert": None, "expected_instances": None, "latest_event": None}
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "MAIN-USA")
        for age, mailed in ((48, False), (62, True)):
            with self.subTest(age=age):
                instance = edge_instance("usa", age=age, advancing=False, steady=0, cpu=0.37)
                row["last_payload_json"] = listener.json.dumps(edge_payload(instance, edge_instance("caribbean")))
                shot = listener._endpoint_snapshot(endpoint, row, [], now)
                self.assertEqual(shot["status_level"] == "critical" and shot.get("email_eligible", True), mailed)


class EventPruningTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_path = listener.DB_PATH
        listener.DB_PATH = Path(self.tmp.name) / "watchdog.db"
        listener.init_db()

    def tearDown(self):
        listener.DB_PATH = self.saved_path
        self.tmp.cleanup()

    def test_only_events_older_than_the_retention_are_removed(self):
        now = datetime.now(timezone.utc)
        with listener.db() as conn:
            for days_old in (400, 181, 179, 1, 0):
                listener._insert_event_locked(conn, (now - timedelta(days=days_old)).isoformat(), "MAIN", "test", f"{days_old} days old", "info")
            removed = listener.prune_old_events(conn, now, days=180)
            left = sorted(row[0] for row in conn.execute("SELECT detail FROM events"))
        self.assertEqual(removed, 2)
        self.assertEqual(left, ["0 days old", "1 days old", "179 days old"])


class MediaPauseWordingTests(unittest.TestCase):
    """A pause seen by the senders' media-read test (engine CPU still busy) is named as a pause, with the number that proves it."""

    def snapshot(self, **instance_fields):
        now = datetime.now(timezone.utc)
        instance = edge_instance("usa", advancing=False, steady=0, cpu=0.37, **{k: v for k, v in instance_fields.items() if k in ("age",)})
        instance.update({k: v for k, v in instance_fields.items() if k != "age"})
        row = {"source_id": "MAIN", "last_seen_utc": now.isoformat(), "last_payload_json": listener.json.dumps(edge_payload(instance, edge_instance("caribbean"))),
               "state": "healthy", "state_changed_utc": now.isoformat(), "expected_interval_seconds": 10, "instance_alert": None,
               "expected_instances": None, "latest_event": None}
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "MAIN-USA")
        return listener._endpoint_snapshot(endpoint, row, [], now)

    def test_a_paused_player_is_critical_at_60s_and_the_detail_shows_the_reads(self):
        shot = self.snapshot(age=62, pause_quiet=True, media_reads_mb_per_s=0.0)
        self.assertEqual(shot["status_level"], "critical")
        self.assertIn("Paused/Frozen > 60s", shot["root_cause"])
        self.assertIn("media reads 0.00 MB/s", shot["status_detail"])
        self.assertIn("engine CPU 0.37 cores", shot["status_detail"])
        self.assertEqual(shot["motion_text"], "Paused or stopped: no media read for 62s")

    def test_amber_at_45s(self):
        shot = self.snapshot(age=48, pause_quiet=True, media_reads_mb_per_s=0.16)
        self.assertEqual(shot["status_level"], "warning")
        self.assertEqual(shot["motion_text"], "Paused or stopped: no media read for 48s")

    def test_a_player_without_the_media_test_keeps_the_old_wording(self):
        shot = self.snapshot(age=62)
        self.assertEqual(shot["motion_text"], "No engine activity for 62s")
        self.assertNotIn("media reads", shot["status_detail"])


class ClockOffsetTests(unittest.TestCase):
    """Each tile says how far that site's clock is from Core's, so a drifting PC is seen at a glance."""

    def text(self, offset_s, zone="Z"):
        received = datetime.now(timezone.utc)
        sent = received + timedelta(seconds=offset_s)
        stamp = sent.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + zone
        return listener._clock_text({"last_seen_utc": received.isoformat()}, {"timestamp": stamp})

    def test_a_site_in_step_is_reported_as_such(self):
        for offset in (0, -1.04, 0.3, -4.0):
            with self.subTest(offset=offset):
                self.assertEqual(self.text(offset), "In step with Core")

    def test_the_slow_main_clock_found_in_production_is_named(self):
        self.assertEqual(self.text(-222), "3 min 42 s behind Core: fix this PC's time sync")

    def test_a_fast_clock_and_small_drift_read_sensibly(self):
        self.assertEqual(self.text(75), "1 min 15 s ahead of Core: fix this PC's time sync")
        self.assertEqual(self.text(-12), "12 s behind Core: fix this PC's time sync")

    def test_a_timestamp_without_a_zone_is_taken_as_utc(self):
        # MAIN and BACKUP-SERVER send "2026-10-11T10:51:46.131" with no zone letter
        self.assertEqual(self.text(-222, zone=""), "3 min 42 s behind Core: fix this PC's time sync")

    def test_missing_or_garbled_timestamps_never_raise(self):
        row = {"last_seen_utc": datetime.now(timezone.utc).isoformat()}
        for payload in ({}, {"timestamp": None}, {"timestamp": "yesterday-ish"}, {"timestamp": 12345}):
            with self.subTest(payload=payload):
                self.assertEqual(listener._clock_text(row, payload), "Not reported")
        self.assertEqual(listener._clock_text(None, {"timestamp": "2026-10-11T10:00:00Z"}), "Not reported")

    def test_the_tile_diagnostics_carry_the_clock_row(self):
        now = datetime.now(timezone.utc)
        row = {"source_id": "MAIN", "last_seen_utc": now.isoformat(), "last_payload_json": listener.json.dumps({"timestamp": (now - timedelta(seconds=222)).isoformat()}),
               "state": "healthy", "state_changed_utc": now.isoformat(), "expected_interval_seconds": 10, "instance_alert": None,
               "expected_instances": None, "latest_event": None}
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "MAIN-USA")
        snapshot = listener._endpoint_snapshot(endpoint, row, [], now)
        clock = next(d for d in snapshot["diagnostics"] if d["label"] == "Clock")
        self.assertIn("3 min 42 s behind Core", clock["value"])


class AlertEmailLogoTests(unittest.TestCase):
    """Every alert email carries the Clarix Watchdog (Bulldog) logo, attached inline so it shows even
    where remote images are blocked. (The small picture beside an email in an inbox is the sending
    account's own profile photo; no email content can change that.)"""

    def sent_messages(self, kinds):
        import email
        import smtplib as real_smtplib
        captured = []

        class FakeSMTP:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def starttls(self, context=None):
                pass

            def login(self, *_args):
                pass

            def sendmail(self, _from, _to, text):
                captured.append(email.message_from_string(text))

        saved = (listener.smtplib.SMTP, listener.SMTP_HOST, listener.SMTP_USER, listener.SMTP_PASS, listener.ALERT_EMAIL_TO)
        listener.smtplib.SMTP = FakeSMTP
        listener.SMTP_HOST, listener.SMTP_USER, listener.SMTP_PASS, listener.ALERT_EMAIL_TO = "smtp.test", "u", "p", "ops@example.test"
        try:
            for kind in kinds:
                listener.send_alert_email("MAIN - USA", kind, "A summary sentence.", [("Root Cause", "Paused/Frozen > 60s")], subject_extra="x")
        finally:
            listener.smtplib.SMTP, listener.SMTP_HOST, listener.SMTP_USER, listener.SMTP_PASS, listener.ALERT_EMAIL_TO = saved
            assert listener.smtplib is real_smtplib
        return captured

    def test_every_kind_of_alert_email_carries_the_logo_inline(self):
        kinds = ("critical", "warning", "heartbeat lost", "heartbeat recovered", "instances restored", "info")
        messages = self.sent_messages(kinds)
        self.assertEqual(len(messages), len(kinds))
        logo_bytes = (Path(listener.__file__).parent / "static_icons" / "icon-192.png").read_bytes()
        for kind, message in zip(kinds, messages):
            with self.subTest(kind=kind):
                self.assertEqual(message.get_content_type(), "multipart/related")
                parts = list(message.walk())
                images = [p for p in parts if p.get_content_type() == "image/png"]
                self.assertEqual(len(images), 1)
                self.assertEqual(images[0]["Content-ID"], "<clarix-watchdog-logo>")
                self.assertTrue(images[0]["Content-Disposition"].startswith("inline"))
                self.assertEqual(images[0].get_payload(decode=True), logo_bytes)
                html = next(p for p in parts if p.get_content_type() == "text/html").get_payload(decode=True).decode("utf-8")
                self.assertIn("cid:clarix-watchdog-logo", html)
                self.assertIn("Clarix Watchdog", html)
                plain = next(p for p in parts if p.get_content_type() == "text/plain").get_payload(decode=True).decode("utf-8")
                self.assertIn("A summary sentence.", plain)
                self.assertIn("MAIN - USA", message["Subject"])

class SiteBPauseContractTests(unittest.TestCase):
    """SITE_B: pressing pause must reach the dashboard through the SAME event contract SITE-A uses.
    Found 2026-10-11: SITE-B's detector saw each pause but posted it as a `suppressed` info event, which
    never alarms, and its heartbeat carries no motion fields. These tests pin what it must send instead
    (an event whose type contains "paused", with ongoing_s, repeated while paused, then phase end), and
    prove the listener needs no change for it."""

    def site_b(self, events):
        now = datetime.now(timezone.utc)
        row = {
            "source_id": "SITE-B", "last_seen_utc": now.isoformat(), "last_payload_json": '{"current_item": "x.mp4"}',
            "state": "healthy", "state_changed_utc": now.isoformat(), "expected_interval_seconds": 300,
            "instance_alert": None, "expected_instances": None, "latest_event": None,
        }
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "SITE-B")
        return listener._endpoint_snapshot(endpoint, row, events, now)

    def pause_event(self, age_s, ongoing_s, phase="ongoing", event_type="playout_paused", severity="warning"):
        stamp = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        return {
            "source_id": "SITE-B", "timestamp_utc": stamp.isoformat(), "event_type": event_type, "severity": severity,
            "detail": listener.json.dumps({"phase": phase, "ongoing_s": ongoing_s}),
        }

    def test_what_the_detector_sends_today_never_alarms(self):
        # production, 2026-10-11 01:36-03:06 UTC: six of these while the owner paused the player
        for kind in ("suppressed", "paused_or_skipped_row"):
            with self.subTest(event_type=kind):
                event = self.pause_event(3, 0, event_type=kind, severity="info")
                event["detail"] = "paused_or_skipped_row"
                self.assertEqual(self.site_b([event])["status_level"], "healthy")

    def test_a_pause_event_climbs_to_yellow_at_45s_and_red_at_60s(self):
        # first post when the pause is 15 s old, then the listener adds the post's age
        for age, level in [(0, "healthy"), (30, "warning"), (45, "critical")]:
            with self.subTest(age=age):
                self.assertEqual(self.site_b([self.pause_event(age, 15)])["status_level"], level)

    def test_a_long_pause_stays_red_while_it_is_refreshed(self):
        # keepalive every 30 s with the growing ongoing_s: 10 minutes into the pause it is still red
        snapshot = self.site_b([self.pause_event(5, 600), self.pause_event(35, 570)])
        self.assertEqual(snapshot["status_level"], "critical")
        self.assertIsNot(snapshot.get("email_eligible"), False)

    def test_without_a_keepalive_a_long_pause_falls_back_to_unconfirmed(self):
        # the gap the SITE-A detector also had: one post, then silence, for more than 120 s
        snapshot = self.site_b([self.pause_event(300, 15)])
        self.assertEqual((snapshot["status_level"], snapshot["status_label"]), ("warning", "Unconfirmed"))

    def test_resume_clears_it(self):
        self.assertEqual(self.site_b([self.pause_event(2, 90, phase="end"), self.pause_event(32, 60)])["status_level"], "healthy")


class EndToEndAlarmFlowTests(unittest.TestCase):
    """The stored alarm, the email decision and the hold, run through a real (temporary) database."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_path = listener.DB_PATH
        listener.DB_PATH = Path(self.tmp.name) / "watchdog.db"
        listener.init_db()

    def tearDown(self):
        listener.DB_PATH = self.saved_path
        self.tmp.cleanup()

    def beat(self, payload):
        now = datetime.now(timezone.utc)
        with listener.db() as conn:
            conn.execute(
                "INSERT INTO sources (source_id, last_seen_utc, last_payload_json, state, state_changed_utc, expected_interval_seconds)"
                " VALUES ('MAIN', ?, ?, 'healthy', ?, 10) ON CONFLICT(source_id) DO UPDATE SET "
                "last_seen_utc = excluded.last_seen_utc, last_payload_json = excluded.last_payload_json, state = 'healthy'",
                (now.isoformat(), listener.json.dumps(payload), now.isoformat()),
            )
            _s, _e, endpoints, emails = listener._evaluate_and_sync_locked(conn, now, now.isoformat())
        return {e["endpoint_id"]: e for e in endpoints}, emails

    def test_freeze_email_once_then_hold_through_restart_then_clear_and_rearm(self):
        healthy = edge_payload(edge_instance("usa"), edge_instance("caribbean"))
        _, emails = self.beat(healthy)
        self.assertEqual([e["endpoint_id"] for e in emails if e["endpoint_id"].startswith("MAIN")], [])

        frozen = edge_payload(edge_instance("usa", age=64, cpu=0.0), edge_instance("caribbean"))
        endpoints, emails = self.beat(frozen)
        self.assertEqual(endpoints["MAIN-USA"]["status_level"], "critical")
        self.assertEqual([e["endpoint_id"] for e in emails if e["endpoint_id"].startswith("MAIN")], ["MAIN-USA"])
        self.assertEqual(endpoints["MAIN-Caribbean"]["status_level"], "healthy")  # strict separation

        endpoints, emails = self.beat(frozen)  # still frozen: no repeat mail
        self.assertEqual([e for e in emails if e["endpoint_id"].startswith("MAIN")], [])

        restarted = edge_payload(edge_instance("usa", age=0, steady=12, uptime=25), edge_instance("caribbean"))
        endpoints, emails = self.beat(restarted)
        self.assertEqual(endpoints["MAIN-USA"]["status_level"], "critical")  # restart did not clear it
        self.assertTrue(endpoints["MAIN-USA"]["held"])
        self.assertEqual([e for e in emails if e["endpoint_id"].startswith("MAIN")], [])

        endpoints, _ = self.beat(edge_payload(edge_instance("usa", steady=45, uptime=120), edge_instance("caribbean")))
        self.assertEqual(endpoints["MAIN-USA"]["status_level"], "healthy")

        endpoints, emails = self.beat(frozen)  # a later freeze alarms again
        self.assertEqual([e["endpoint_id"] for e in emails if e["endpoint_id"].startswith("MAIN")], ["MAIN-USA"])

    def test_manual_reset_unholds(self):
        self.beat(edge_payload(edge_instance("usa", age=70), edge_instance("caribbean")))
        restarted = edge_payload(edge_instance("usa", age=0, steady=5, uptime=10), edge_instance("caribbean"))
        endpoints, _ = self.beat(restarted)
        self.assertEqual(endpoints["MAIN-USA"]["status_level"], "critical")

        def body_of(payload):
            class FakeRequest:
                async def stream(self):
                    yield listener.json.dumps(payload).encode()
            return FakeRequest()

        import asyncio
        reply = asyncio.run(listener.reset_alert(body_of({"endpoint_id": "MAIN-USA"})))
        self.assertEqual(reply.status_code, 200)
        endpoints, _ = self.beat(restarted)
        self.assertEqual(endpoints["MAIN-USA"]["status_level"], "healthy")

        self.assertEqual(asyncio.run(listener.reset_alert(body_of({"endpoint_id": "NOPE"}))).status_code, 400)


class IdlePlayerAlarmTests(unittest.TestCase):
    """SITE-B is judged on its playlist timing alone, so a player that is running but has nothing playing (a stalled or paused playout, silent and off air) stayed 'Healthy'
    for hours. A player that says it is running and names no item for 10 minutes is now a critical alarm. The clock is a mark stored with the heartbeat (`_idle_since`)."""
    NOW = datetime(2026, 10, 26, 0, 40, 0, tzinfo=timezone.utc)

    def endpoint(self, endpoint_id="SITE-B"):
        return next(e for e in listener.PLAYOUT_ENDPOINTS if e["endpoint_id"] == endpoint_id)

    def idle_payload(self, minutes_idle, **extra):
        since = self.NOW - timedelta(seconds=minutes_idle * 60)
        return {"hostname": "SITE-B", "current_item": None, "player_running": True, "_idle_since": since.isoformat(), **extra}

    # ---- what counts as "something is playing" ----
    def test_a_heartbeat_names_an_item_in_any_of_the_fields_the_dashboard_reads(self):
        for payload in ({"current_item": "SHOW.mp4"}, {"current_program": "SHOW"}, {"program": "SHOW"}, {"title": "SHOW"}, {"filename": "SHOW.mp4"},
                        {"now_playing": {"filename": "SHOW.mp4"}}):
            with self.subTest(payload=payload):
                self.assertTrue(listener._payload_has_item(payload))

    def test_empty_blank_missing_or_odd_values_are_not_an_item(self):
        for payload in ({}, {"current_item": None}, {"current_item": ""}, {"current_item": "   "}, {"current_item": 5}, {"current_item": ["SHOW.mp4"]},
                        {"now_playing": {}}, {"now_playing": {"filename": ""}}, {"now_playing": "SHOW"}):
            with self.subTest(payload=payload):
                self.assertFalse(listener._payload_has_item(payload))

    # ---- the mark that carries the clock from one heartbeat to the next ----
    def mark(self, payload, previous=None, source="SITE-B", now=None):
        return listener._with_idle_mark(payload, previous, source, now or self.NOW)

    def test_the_first_idle_heartbeat_starts_the_clock_now(self):
        marked = self.mark({"current_item": None, "player_running": True})
        self.assertEqual(marked["_idle_since"], self.NOW.isoformat())

    def test_a_following_idle_heartbeat_keeps_the_original_start(self):
        started = (self.NOW - timedelta(minutes=7)).isoformat()
        marked = self.mark({"current_item": None, "player_running": True}, {"current_item": None, "player_running": True, "_idle_since": started})
        self.assertEqual(marked["_idle_since"], started)

    def test_an_item_ends_the_idle_spell_and_the_next_idle_spell_starts_from_zero(self):
        started = (self.NOW - timedelta(minutes=7)).isoformat()
        playing = self.mark({"current_item": "SHOW.mp4", "player_running": True}, {"current_item": None, "_idle_since": started})
        self.assertNotIn("_idle_since", playing)
        again = self.mark({"current_item": None, "player_running": True}, playing)
        self.assertEqual(again["_idle_since"], self.NOW.isoformat())

    def test_only_a_player_that_says_it_is_running_counts(self):
        for running in (False, None, "true", 1, "yes"):
            with self.subTest(player_running=running):
                self.assertNotIn("_idle_since", self.mark({"current_item": None, "player_running": running}))
        self.assertNotIn("_idle_since", self.mark({"current_item": None}))

    def test_a_sender_cannot_forge_or_carry_over_the_mark(self):
        forged = self.mark({"current_item": "SHOW.mp4", "player_running": True, "_idle_since": "2000-01-01T00:00:00+00:00"})
        self.assertNotIn("_idle_since", forged)
        also = self.mark({"current_item": None, "player_running": True, "_idle_since": "2000-01-01T00:00:00+00:00"})
        self.assertEqual(also["_idle_since"], self.NOW.isoformat())               # its own claim is ignored; the clock starts when Core first saw it idle

    def test_a_damaged_or_future_previous_mark_restarts_the_clock(self):
        for bad in ("yesterday", "", None, 12, (self.NOW + timedelta(hours=1)).isoformat()):
            with self.subTest(previous=bad):
                marked = self.mark({"current_item": None, "player_running": True}, {"_idle_since": bad})
                self.assertEqual(marked["_idle_since"], self.NOW.isoformat())

    def test_only_the_listed_source_is_marked(self):
        for source in ("SITE-A", "MAIN", "BACKUP-SERVER", "site_b"):
            with self.subTest(source=source):
                self.assertNotIn("_idle_since", self.mark({"current_item": None, "player_running": True}, source=source))

    def test_a_marking_problem_never_fails_a_heartbeat(self):
        for previous in ("not a dict", 5, [], {"_idle_since": {"a": 1}}):
            with self.subTest(previous=previous):
                self.assertIn("_idle_since", self.mark({"current_item": None, "player_running": True}, previous))

    # ---- the alarm itself ----
    def test_ten_minutes_idle_is_a_critical_alarm_that_may_be_emailed(self):
        status = listener._source_idle_status(self.endpoint(), self.idle_payload(10), self.NOW)
        self.assertEqual((status["level"], status["label"], status["root_cause"]), ("critical", "Critical Alarm", "No Item Playing > 600s"))
        self.assertIs(status["email_eligible"], True)
        self.assertIn("nothing", status["detail"].lower())
        self.assertIn("600", status["detail"])                                      # it says how long: the idle seconds

    def test_nine_minutes_fifty_nine_seconds_is_not_yet(self):
        payload = self.idle_payload(0)
        payload["_idle_since"] = (self.NOW - timedelta(seconds=599)).isoformat()
        self.assertIsNone(listener._source_idle_status(self.endpoint(), payload, self.NOW))
        payload["_idle_since"] = (self.NOW - timedelta(seconds=600)).isoformat()
        self.assertIsNotNone(listener._source_idle_status(self.endpoint(), payload, self.NOW))

    def test_the_threshold_is_ten_minutes_and_only_site_b_has_it(self):
        self.assertEqual(listener.IDLE_ALARM_SECONDS, 600)
        self.assertEqual(listener.IDLE_ALARM_SOURCES, ("SITE-B",))
        for endpoint_id in ("SITE-A", "MAIN-USA", "MAIN-Caribbean", "BACKUP-USA", "BACKUP-Caribbean"):
            with self.subTest(endpoint=endpoint_id):
                self.assertIsNone(listener._source_idle_status(self.endpoint(endpoint_id), self.idle_payload(60), self.NOW))

    def test_no_alarm_unless_the_player_says_it_is_running_and_names_nothing(self):
        endpoint = self.endpoint()
        for change in ({"player_running": False}, {"player_running": None}, {"player_running": "true"}, {"current_item": "SHOW.mp4"}, {"now_playing": {"filename": "SHOW.mp4"}}):
            with self.subTest(change=change):
                self.assertIsNone(listener._source_idle_status(endpoint, self.idle_payload(60, **change), self.NOW))

    def test_no_mark_or_a_damaged_mark_or_one_in_the_future_gives_no_alarm(self):
        endpoint = self.endpoint()
        payload = self.idle_payload(60)
        del payload["_idle_since"]
        self.assertIsNone(listener._source_idle_status(endpoint, payload, self.NOW))
        for bad in ("yesterday", "", 7, (self.NOW + timedelta(hours=1)).isoformat()):
            with self.subTest(mark=bad):
                self.assertIsNone(listener._source_idle_status(endpoint, {**self.idle_payload(60), "_idle_since": bad}, self.NOW))

    def test_a_player_that_is_not_running_is_still_the_existing_player_not_running_alarm(self):
        payload = self.idle_payload(60, player_running=False)
        snapshot = listener._endpoint_snapshot(self.endpoint(), {"source_id": "SITE-B", "last_seen_utc": self.NOW.isoformat(), "state": "healthy",
                                                                 "expected_interval_seconds": 30, "last_payload_json": listener.json.dumps(payload)}, [], self.NOW)
        self.assertEqual(snapshot["root_cause"], "Player Not Running")


class IdleAlarmFlowTests(unittest.TestCase):
    """The alarm through a real (temporary) database: mailed once, kept while idle, cleared when something plays, mailed again the next time."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_path = listener.DB_PATH
        listener.DB_PATH = Path(self.tmp.name) / "watchdog.db"
        listener.init_db()

    def tearDown(self):
        listener.DB_PATH = self.saved_path
        self.tmp.cleanup()

    def beat(self, payload):
        now = datetime.now(timezone.utc)
        with listener.db() as conn:
            conn.execute(
                "INSERT INTO sources (source_id, last_seen_utc, last_payload_json, state, state_changed_utc, expected_interval_seconds)"
                " VALUES ('SITE-B', ?, ?, 'healthy', ?, 30) ON CONFLICT(source_id) DO UPDATE SET "
                "last_seen_utc = excluded.last_seen_utc, last_payload_json = excluded.last_payload_json, state = 'healthy'",
                (now.isoformat(), listener.json.dumps(payload), now.isoformat()),
            )
            _s, _e, endpoints, emails = listener._evaluate_and_sync_locked(conn, now, now.isoformat())
            events = [row["event_type"] for row in conn.execute("SELECT event_type FROM events WHERE source_id = 'SITE-B' ORDER BY id")]
        return {e["endpoint_id"]: e for e in endpoints}, [e["endpoint_id"] for e in emails if e["endpoint_id"] == "SITE-B"], events

    def idle(self, minutes):
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        return {"hostname": "SITE-B", "current_item": None, "player_running": True, "expected_interval_seconds": 30, "_idle_since": since.isoformat()}

    def test_the_alarm_is_mailed_once_held_while_idle_and_cleared_when_something_plays(self):
        endpoints, mails, _ = self.beat(self.idle(5))
        self.assertEqual((endpoints["SITE-B"]["status_level"], mails), ("healthy", []))
        endpoints, mails, events = self.beat(self.idle(11))
        self.assertEqual((endpoints["SITE-B"]["status_level"], endpoints["SITE-B"]["root_cause"]), ("critical", "No Item Playing > 600s"))
        self.assertEqual(mails, ["SITE-B"])
        self.assertIn("endpoint_critical", events)
        endpoints, mails, _ = self.beat(self.idle(14))
        self.assertEqual((endpoints["SITE-B"]["status_level"], mails), ("critical", []))             # still idle: no second mail
        endpoints, mails, _ = self.beat({"hostname": "SITE-B", "current_item": "SHOW.mp4", "player_running": True, "expected_interval_seconds": 30})
        self.assertEqual((endpoints["SITE-B"]["status_level"], mails), ("healthy", []))
        endpoints, mails, _ = self.beat(self.idle(12))
        self.assertEqual((endpoints["SITE-B"]["status_level"], mails), ("critical", ["SITE-B"]))    # a later idle spell raises it again

    def test_an_idle_site_b_does_not_turn_any_other_playout_red_for_that_reason(self):
        endpoints, _, _ = self.beat(self.idle(30))
        for endpoint_id, endpoint in endpoints.items():
            if endpoint_id != "SITE-B":
                self.assertNotEqual(endpoint["root_cause"], "No Item Playing > 600s", endpoint_id)


class MediaActiveIdleTests(unittest.TestCase):
    """SITE-B's detector (v0.7.0) can say whether a player has decoded media in the last 15 seconds: `media_active`. At midnight a FileBlock hour can play a clip for
    many minutes while Insta's playlist names nothing, and the 10 minute idle rule went red on a channel that was on air (2026-11-09 00:00). While `media_active` is the
    JSON true, a clip is playing: there is no idle spell and no idle alarm, and the clock starts from zero when the activity stops. Nothing else changes: a player that
    says it is not running, a lost heartbeat, a stalled transition and every other alarm are judged exactly as before, and a heartbeat without the field behaves as it did."""

    NOW = datetime(2026, 11, 15, 0, 20, 0, tzinfo=timezone.utc)

    def endpoint(self):
        return next(e for e in listener.PLAYOUT_ENDPOINTS if e["endpoint_id"] == "SITE-B")

    def idle(self, minutes, **extra):
        since = self.NOW - timedelta(minutes=minutes)
        return {"hostname": "SITE-B", "current_item": None, "player_running": True, "_idle_since": since.isoformat(), **extra}

    def test_only_the_json_true_counts_as_active(self):
        self.assertTrue(listener._media_is_active({"media_active": True}))
        for value in (False, None, "true", "yes", 1, 0, [], {}, [True]):
            with self.subTest(value=value):
                self.assertFalse(listener._media_is_active({"media_active": value}))
        self.assertFalse(listener._media_is_active({}))

    def test_an_active_player_with_no_name_does_not_start_an_idle_spell(self):
        marked = listener._with_idle_mark({"current_item": None, "player_running": True, "media_active": True}, None, "SITE-B", self.NOW)
        self.assertNotIn("_idle_since", marked)

    def test_activity_ends_a_spell_in_progress_and_the_next_spell_starts_from_zero(self):
        previous = {"current_item": None, "player_running": True, "_idle_since": (self.NOW - timedelta(minutes=9)).isoformat()}
        active = listener._with_idle_mark({"current_item": None, "player_running": True, "media_active": True}, previous, "SITE-B", self.NOW)
        self.assertNotIn("_idle_since", active)
        later = self.NOW + timedelta(seconds=30)
        again = listener._with_idle_mark({"current_item": None, "player_running": True, "media_active": False}, active, "SITE-B", later)
        self.assertEqual(again["_idle_since"], later.isoformat())            # not the old 9 minutes: a new 10 minute wait

    def test_the_status_is_not_critical_while_media_is_active_even_with_an_old_mark(self):
        self.assertIsNone(listener._source_idle_status(self.endpoint(), self.idle(30, media_active=True), self.NOW))

    def test_without_the_field_or_with_anything_but_true_the_idle_alarm_is_unchanged(self):
        for extra in ({}, {"media_active": False}, {"media_active": None}, {"media_active": "true"}, {"media_active": 1}):
            with self.subTest(extra=extra):
                status = listener._source_idle_status(self.endpoint(), self.idle(11, **extra), self.NOW)
                self.assertEqual(status["root_cause"], "No Item Playing > 600s")
                self.assertEqual(status["level"], "critical")

    def test_an_active_flag_never_hides_a_player_that_says_it_is_not_running(self):
        down = self.idle(0, media_active=True)
        down["player_running"] = False
        status = listener._source_player_status(self.endpoint(), down)
        self.assertEqual(status["root_cause"], "Player Not Running")

    def test_a_sender_cannot_forge_the_idle_clock_with_the_flag(self):
        forged = listener._with_idle_mark({"current_item": None, "player_running": True, "media_active": False, "_idle_since": "2000-01-01T00:00:00+00:00"}, None, "SITE-B", self.NOW)
        self.assertEqual(forged["_idle_since"], self.NOW.isoformat())


class MediaActiveFlowTests(unittest.TestCase):
    """Through a real (temporary) database: a clip playing for 25 minutes with no name never raises the alarm; once the activity stops the 10 minute wait starts."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_path = listener.DB_PATH
        listener.DB_PATH = Path(self.tmp.name) / "watchdog.db"
        listener.init_db()

    def tearDown(self):
        listener.DB_PATH = self.saved_path
        self.tmp.cleanup()

    def beat(self, payload):
        now = datetime.now(timezone.utc)
        with listener.db() as conn:
            conn.execute(
                "INSERT INTO sources (source_id, last_seen_utc, last_payload_json, state, state_changed_utc, expected_interval_seconds)"
                " VALUES ('SITE-B', ?, ?, 'healthy', ?, 30) ON CONFLICT(source_id) DO UPDATE SET "
                "last_seen_utc = excluded.last_seen_utc, last_payload_json = excluded.last_payload_json, state = 'healthy'",
                (now.isoformat(), listener.json.dumps(payload), now.isoformat()),
            )
            _s, _e, endpoints, emails = listener._evaluate_and_sync_locked(conn, now, now.isoformat())
        return {e["endpoint_id"]: e for e in endpoints}["SITE-B"], [e["endpoint_id"] for e in emails if e["endpoint_id"] == "SITE-B"]

    def idle(self, minutes, **extra):
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        return {"hostname": "SITE-B", "current_item": None, "player_running": True, "expected_interval_seconds": 30, "_idle_since": since.isoformat(), **extra}

    def test_a_long_unnamed_clip_raises_nothing_and_a_silent_player_still_does(self):
        endpoint, mails = self.beat(self.idle(25, media_active=True))
        self.assertEqual((endpoint["status_level"], mails), ("healthy", []))
        endpoint, mails = self.beat(self.idle(25, media_active=False))
        self.assertEqual((endpoint["status_level"], endpoint["root_cause"], mails), ("critical", "No Item Playing > 600s", ["SITE-B"]))
        endpoint, mails = self.beat(self.idle(25, media_active=True))
        self.assertEqual((endpoint["status_level"], mails), ("healthy", []))                      # playing again: cleared


class CriticalEmailTests(unittest.TestCase):
    def test_email_names_the_playout_and_the_root_cause(self):
        sent = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=0):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self, context=None):
                pass

            def login(self, user, password):
                pass

            def sendmail(self, sender, recipients, message):
                sent["recipients"] = recipients
                sent["message"] = message

        saved = (listener.SMTP_HOST, listener.SMTP_USER, listener.SMTP_PASS, listener.ALERT_EMAIL_TO, listener.smtplib.SMTP)
        listener.SMTP_HOST, listener.SMTP_USER, listener.SMTP_PASS = "smtp.example", "user", "secret"
        listener.ALERT_EMAIL_TO = "support@client.example"
        listener.smtplib.SMTP = FakeSMTP
        try:
            now = datetime.now(timezone.utc)
            endpoint = {
                "endpoint_id": "MAIN-USA", "source_id": "MAIN", "display_name": "MAIN - USA", "root_cause": "Paused/Frozen > 60s",
                "status_label": "Critical Alarm", "status_detail": "No engine activity for 64s.", "motion_text": "No engine activity for 64s",
                "now_playing": {"title": "Some Show.mp4"}, "pid_text": "engine 200",
            }
            listener._send_endpoint_critical_email(endpoint)
        finally:
            listener.SMTP_HOST, listener.SMTP_USER, listener.SMTP_PASS, listener.ALERT_EMAIL_TO, listener.smtplib.SMTP = saved
        self.assertEqual(sent["recipients"], ["support@client.example"])
        message = sent["message"]
        self.assertIn("Subject: [Watchdog] MAIN - USA: CRITICAL - Paused/Frozen > 60s", message)
        decoded = " ".join(part.get_payload(decode=True).decode("utf-8") for part in __import__("email").message_from_string(message).walk() if part.get_content_maintype() == "text")  # text parts only: the logo is an image part
        self.assertIn("Playout Name", decoded)
        self.assertIn("MAIN - USA", decoded)
        self.assertIn("Paused/Frozen > 60s", decoded)


class OfflineAssetTests(unittest.TestCase):
    def test_service_worker_keeps_the_placeholder_and_the_page_shells_and_nothing_else(self):
        # 2026-10-13: the worker used to keep ONLY the offline placeholder, and abandoned any page load slower than 6 s, so a weak or ship
        # connection met "Cannot reach the Watchdog server" for a server that was fine. It now also keeps the SHELL of the app (the page
        # itself, which holds no data: every number arrives over the live stream) so the app opens at once and explains a lost feed itself.
        sw = listener.SERVICE_WORKER_JS
        self.assertIn("/offline.html", sw)
        self.assertIn("event.request.mode !== 'navigate'", sw)  # streams, API calls: straight to the network
        self.assertIn("response.status >= 500", sw)  # a tunnel error page is also "cannot reach"
        self.assertIn("var SHELL_PATHS = ['/', '/topology'];", sw)
        self.assertEqual(sw.count("cache.add("), 1)    # the placeholder, which must be present for the install to succeed
        self.assertEqual(sw.count("cache.put("), 2)    # the install (shells, best effort) and the refresh of a shell after a page load
        for never in ("/stream", "/status", "/heartbeat", "token", "recent_events"):
            with self.subTest(never=never):
                self.assertNotIn(never, sw)             # status data and the stream are never stored or even named
        # a hung connection must not keep the user on the launch splash: short with a saved shell, longer for a first visit that has none
        self.assertIn("var SHELL_TIMEOUT_MS = 4000;", sw)
        self.assertIn("var FIRST_VISIT_TIMEOUT_MS = 8000;", sw)
        self.assertIn("saved ? SHELL_TIMEOUT_MS : FIRST_VISIT_TIMEOUT_MS", sw)
        self.assertIn("self.navigator.onLine === false", sw)  # a phone that says it is offline: no waiting at all

    def test_an_installed_phone_gets_the_new_worker_and_drops_the_old_caches(self):
        # A phone keeps what the worker cached until the worker itself changes, so the cache name and the registration version move with it.
        sw = listener.SERVICE_WORKER_JS
        self.assertIn("var CACHE = 'watchdog-shell-v3';", sw)
        self.assertNotIn("watchdog-offline-v", sw)
        self.assertIn('register("/sw.js?v=20261013-shell")', listener.render_dashboard_html(TEST_TOKEN))
        self.assertIn("caches.delete(name)", sw)    # the old cache is removed when the new worker takes over

    def test_a_page_that_opens_with_no_connection_explains_itself(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertIn("(Date.now() - (uiState.lastFrameAt || pageOpenedAt)) > BLIND_AFTER_MS", page)     # blind even if no frame EVER came
        self.assertIn("No live data has arrived yet, so there is nothing to show.", page)
        self.assertIn("if (blind) { hidePreloader(); }", page)

    def test_icons_are_versioned_so_installed_apps_refresh_their_copy(self):
        icon_version = importlib.import_module("app.dashboard").ICON_VERSION
        manifest = listener.json.loads(listener.MANIFEST_JSON)
        for icon in manifest["icons"]:
            with self.subTest(src=icon["src"]):
                self.assertIn("?v=" + icon_version, icon["src"])
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertIn("/icon-192.png?v=" + icon_version, page)
        self.assertIn("/apple-touch-icon.png?v=" + icon_version, page)
        self.assertNotIn("__ICON_VERSION__", page)

    OFFLINE_ALLOWED_ADDRESSES = {
        "https://www.gstatic.com/generate_204",        # connectivity check 1
        "https://www.cloudflare.com/cdn-cgi/trace",    # connectivity check 2 (a different provider)
        "https://corp.example",                      # the brand link
    }

    def test_offline_page_carries_no_status_and_never_looks_healthy(self):
        page = listener.OFFLINE_HTML
        self.assertIn("Clarix Watchdog", page)
        self.assertIn("window.location.replace(\"/\")", page)  # returns to the dashboard by itself
        self.assertIn("addEventListener(\"online\"", page)
        for forbidden in ("healthy", "token", "/stream"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, page.lower())
        self.assertNotIn("__BULLDOG", page)

    def test_the_old_alarm_email_note_is_gone_and_clarixtech_is_promoted_instead(self):
        page = listener.OFFLINE_HTML
        for removed in ("Status unknown while this screen is showing", "Alarm emails are sent by", "support@client.example"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, page)
        self.assertIn("CLARIXTECH GROUP", page)
        self.assertIn("Resilient systems for broadcast, automation, and field infrastructure.", page)   # the line corp.example itself uses
        self.assertIn('href="https://corp.example" target="_blank" rel="noopener noreferrer"', page)

    def test_the_only_outside_addresses_are_the_two_connectivity_checks_and_the_brand_link(self):
        import re
        page = listener.OFFLINE_HTML.replace('xmlns="http://www.w3.org/2000/svg"', "")
        found = {match.rstrip('"\'.,;)') for match in re.findall(r"https?://[^\s\"'<>)]+", page)}
        self.assertEqual(found, self.OFFLINE_ALLOWED_ADDRESSES)
        self.assertNotIn("http://", page)

    def test_the_connectivity_check_sends_nothing_and_reads_nothing(self):
        page = listener.OFFLINE_HTML
        self.assertIn('mode: "no-cors"', page)              # the answer is opaque: it can only say "reached" or "did not"
        self.assertIn('credentials: "omit"', page)          # no cookies
        self.assertIn('referrerPolicy: "no-referrer"', page)
        self.assertIn("PROBE_TIMEOUT_MS = 4000", page)      # a hung network cannot keep the message on "checking"
        self.assertIn("SERVER_TIMEOUT_MS = 8000", page)

    def test_the_message_tells_a_dead_internet_from_a_dead_server(self):
        page = listener.OFFLINE_HTML
        # the three outcomes are worded separately, and "your internet works" is only said once the internet check has passed
        self.assertIn("This device is offline", page)
        self.assertIn("No internet connection", page)
        self.assertIn("Cannot reach the Watchdog server", page)
        self.assertIn("Your internet works, but the Watchdog server did not answer.", page)
        self.assertNotIn("Your connection is up", page)      # the old claim, made from navigator.onLine alone
        internet_branch = page.split("} else if (internet === true) {")[1].split("} else {")[0]
        self.assertIn("Your internet works", internet_branch)
        no_internet_branch = page.split("} else if (internet === false) {")[1].split("} else if (internet === true)")[0]
        self.assertIn("No internet connection", no_internet_branch)
        self.assertIn('setNode("server", "unknown", "Unknown");', no_internet_branch)   # with no internet the server's state is NOT known, and it says so
        self.assertIn("navigator.onLine !== false", page)

    def test_manifest_offers_a_maskable_icon_for_the_install_splash(self):
        manifest = listener.json.loads(listener.MANIFEST_JSON)
        purposes = {icon["purpose"] for icon in manifest["icons"]}
        self.assertEqual(purposes, {"any", "maskable"})
        self.assertIn("icon-512-maskable.png", " ".join(icon["src"] for icon in manifest["icons"]))


telemetry = importlib.import_module("app.telemetry")


class DashboardRedesignTests(unittest.TestCase):
    """Page-level pins for the redesigned dashboard. Behaviour is proven in a real browser (ops/browser_tests/dashboard_redesign_test.mjs);
    these keep the structural promises from being quietly lost."""

    def page(self):
        return listener.render_dashboard_html(TEST_TOKEN)

    def test_counters_and_the_core_panel_exist(self):
        page = self.page()
        for element_id in ("counter-healthy", "counter-since", "counter-red24",       # Active alarms, Events 24 h and Heartbeats per minute were taken out by the owner (2026-11-10)
                           "vitals", "v-gauge", "v-cpu", "v-load", "v-cores", "m-mem", "m-disk", "m-load", "v-spark-line"):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', page)
        self.assertIn("function renderCounters(", page)
        self.assertIn("function renderVitals(", page)
        self.assertIn("Core server, all workloads", page)
        self.assertIn(".vitals-head .eyebrow { white-space:nowrap;", page)   # the heading stays on one line in the 340 px panel
        self.assertIn("--side:340px;", page)           # the Core panel and the incident feed share one column width
        self.assertIn("grid-template-columns:minmax(0, 1fr) var(--side);", page)
        self.assertEqual(page.count("gap:var(--gap)"), 7)   # counters and tiles use the same gap, so their columns line up (7 = the five before + the Site C feed card's grid + the matrix source tiles)

    def test_numbers_count_up_but_not_for_people_who_asked_for_less_motion(self):
        page = self.page()
        self.assertIn("function animateNumber(", page)
        self.assertIn("REDUCED_MOTION.matches", page.split("function animateNumber")[1].split("var counterParts")[0])
        self.assertIn("(prefers-reduced-motion: reduce)", page)
        self.assertIn("@media (prefers-reduced-motion:no-preference) {\n  .banner.status-critical .banner-orb", page)

    def test_a_stale_file_name_is_labelled_and_ages_count_on_the_devices_own_clock(self):
        page = self.page()
        self.assertIn("var STALE_NOW_PLAYING_S = 45;", page)
        self.assertIn('"Last reported"', page)
        self.assertIn(" - may have changed", page)
        timing = page.split("function nowPlayingTiming")[1].split("function refreshNowPlaying")[0]
        # counted forward from when the frame ARRIVED here, never compared with a server time, so a wrong phone clock cannot distort it
        self.assertIn("(Date.now() - uiState.frameAt) / 1000", timing)
        self.assertNotIn("Date.parse", timing)
        self.assertIn("uiState.frameAt = uiState.lastFrameAt;", page)

    def test_tiles_and_details_are_built_once_and_updated_in_place(self):
        page = self.page()
        for name in ("function createTile(", "function updateTile(", "function syncRows(", "function syncEvents(", "function createDetailsGrid("):
            with self.subTest(name=name):
                self.assertIn(name, page)
        render = page.split("function renderDashboard(states)")[1].split("function incidentTextFromEvent")[0]
        self.assertIn("if (uiState.tileSignature !== signature)", render)   # rebuilt only when the SET of endpoints changes
        # a diagnostics value is only written when it changed
        self.assertIn("if (kept[index].value !== value)", page)
        for gone in ("renderEndpointTile", "renderDetailsPanel", "detailsSignature"):
            self.assertNotIn(gone, page)
        self.assertNotIn("innerHTML", page)

    def test_the_page_says_when_it_is_blind_and_names_the_cause(self):
        page = self.page()
        self.assertIn("var BLIND_AFTER_MS = 15000;", page)
        self.assertIn('"Live feed lost"', page)
        self.assertIn("Treat it as unverified.", page)
        self.assertIn("body.is-blind .endpoint-tile", page)
        for message in ("This device is offline.", "This device has no internet connection.", "This device's internet works, but Core is not answering."):
            with self.subTest(message=message):
                self.assertIn(message, page)

    def test_the_only_outside_addresses_are_the_two_connectivity_checks_and_the_brand_link(self):
        import re
        page = self.page().replace('xmlns="http://www.w3.org/2000/svg"', "").replace("http://www.w3.org/2000/svg", "")
        found = {match.rstrip("\"'.,;)") for match in re.findall(r"https?://[^\s\"'<>)]+", page)}
        self.assertEqual(found, OfflineAssetTests.OFFLINE_ALLOWED_ADDRESSES)
        probe = page.split("function internetProbe")[1].split("function probeInternet")[0]
        for needle in ('mode:"no-cors"', 'credentials:"omit"', 'referrerPolicy:"no-referrer"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, probe)

    def test_clarixtech_is_promoted_in_the_header_the_loader_and_the_footer(self):
        page = self.page()
        self.assertIn("by ClarixTech Group", page)
        self.assertEqual(page.count("CLARIXTECH GROUP"), 1)
        self.assertIn('href="https://corp.example" target="_blank" rel="noopener noreferrer"', page)
        self.assertIn("Resilient systems for broadcast, automation, and field infrastructure.", page)

    def test_topology_gets_the_cpu_readout_and_drops_the_counters(self):
        page = listener.render_topology_html(TEST_TOKEN)
        self.assertIn('el("div", undefined, "holo-cpu")', page)
        self.assertIn(".overview.compact .counters, .overview.compact .vitals { display:none; }", page)
        self.assertIn('overview.className = isTopology ? "overview compact" : "overview";', page)


class CoreVitalsTests(unittest.TestCase):
    """The Linux server's own load, read from /proc. Every reader must survive a machine with no /proc at all."""

    def test_cpu_percent_is_the_busy_share_between_two_readings(self):
        first = telemetry.parse_cpu_times("cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 1 1 1 0 0 0 0 0 0\n")
        second = telemetry.parse_cpu_times("cpu  150 0 150 900 0 0 0 0 0 0\n")
        self.assertEqual(first, (800, 1000))
        self.assertEqual(telemetry.cpu_percent(first, second), 50.0)

    def test_waiting_on_the_disk_counts_as_idle_and_guest_time_is_not_added_twice(self):
        # idle = idle + iowait; only the first eight fields add up (guest and guest_nice are inside user already)
        reading = telemetry.parse_cpu_times("cpu  10 0 10 70 20 0 0 0 999 999\n")
        self.assertEqual(reading, (90, 110))

    def test_unreadable_or_missing_input_gives_none_not_an_error(self):
        for bad in (None, "", "garbage", "cpu  a b c d e\n", "cpu  1 2\n"):
            with self.subTest(bad=bad):
                self.assertIsNone(telemetry.parse_cpu_times(bad))
        self.assertIsNone(telemetry.cpu_percent(None, (1, 2)))
        self.assertIsNone(telemetry.cpu_percent((5, 10), (5, 10)))   # no time passed: nothing to divide by
        self.assertEqual(telemetry.cpu_percent((0, 100), (100, 200)), 0.0)    # all idle
        self.assertEqual(telemetry.cpu_percent((0, 100), (0, 200)), 100.0)    # none idle

    def test_memory_in_use_is_total_minus_available_not_total_minus_free(self):
        text = "MemTotal:       8388608 kB\nMemFree:          100000 kB\nMemAvailable:   2097152 kB\nBuffers: 5 kB\n"
        self.assertEqual(telemetry.parse_meminfo(text), {"mem_total_mb": 8192, "mem_used_percent": 75.0})
        self.assertIsNone(telemetry.parse_meminfo("MemTotal: 0 kB\nMemAvailable: 0 kB\n"))
        self.assertIsNone(telemetry.parse_meminfo("nothing useful"))

    def test_load_and_uptime_parse(self):
        self.assertEqual(telemetry.parse_loadavg("0.52 0.41 0.30 1/234 5678\n"), (0.52, 0.41, 0.30))
        self.assertIsNone(telemetry.parse_loadavg("x y"))
        self.assertEqual(telemetry.parse_uptime("12345.67 5555.55\n"), 12345)
        self.assertIsNone(telemetry.parse_uptime(""))

    def test_sampler_reports_cpu_from_the_second_reading_and_caps_its_history(self):
        # every reading adds 50 user + 50 system + 100 idle jiffies, so every interval is exactly 50 % busy
        stats = iter(["cpu  %d 0 %d %d 0 0 0 0\n" % (100 + 50 * i, 100 + 50 * i, 800 + 100 * i) for i in range(telemetry.HISTORY_POINTS + 20)])
        files = {"/proc/loadavg": "0.5 0.4 0.3 1/2 3\n", "/proc/uptime": "100.0 1.0\n",
                 "/proc/meminfo": "MemTotal: 1048576 kB\nMemAvailable: 524288 kB\n"}
        saved = telemetry._read_text
        telemetry._read_text = lambda path: next(stats) if path == "/proc/stat" else files.get(path)
        try:
            sampler = telemetry.SystemSampler()
            first = sampler.sample()
            self.assertIsNone(first["cpu_percent"])         # nothing to compare against yet
            second = sampler.sample()
            self.assertEqual(second["cpu_percent"], 50.0)
            self.assertEqual(second["mem_used_percent"], 50.0)
            self.assertEqual((second["load1"], second["uptime_s"]), (0.5, 100))
            for _ in range(telemetry.HISTORY_POINTS + 5):
                sampler.sample()
            self.assertEqual(len(sampler.snapshot()["cpu_history"]), telemetry.HISTORY_POINTS)
        finally:
            telemetry._read_text = saved

    def test_one_failed_read_keeps_the_last_good_reading_instead_of_blanking_the_panel(self):
        lines = iter(["cpu  100 0 100 800 0 0 0 0\n", "cpu  150 0 150 900 0 0 0 0\n", None, "cpu  200 0 200 1000 0 0 0 0\n"])
        saved = telemetry._read_text
        telemetry._read_text = lambda path: next(lines) if path.endswith("/stat") else None
        try:
            sampler = telemetry.SystemSampler()
            sampler.sample()
            good = sampler.sample()
            self.assertTrue(good["available"])
            during_glitch = sampler.sample()                  # the third read fails
            self.assertTrue(during_glitch["available"])
            self.assertEqual(during_glitch["cpu_percent"], good["cpu_percent"])
            after = sampler.sample()                          # and the fourth recovers, measured against the last good reading
            self.assertEqual(after["cpu_percent"], 50.0)
            self.assertEqual(len(sampler.snapshot()["cpu_history"]), 2)   # the glitch added no point to the history
        finally:
            telemetry._read_text = saved

    def test_a_machine_without_proc_gives_empty_readings_and_never_raises(self):
        saved = telemetry._read_text
        telemetry._read_text = lambda path: None
        try:
            sampler = telemetry.SystemSampler()
            reading = sampler.sample()
            sampler.sample()
        finally:
            telemetry._read_text = saved
        self.assertFalse(reading["available"])
        self.assertIsNone(reading["cpu_percent"])
        self.assertIsNone(reading["load1"])
        self.assertEqual(sampler.snapshot()["cpu_history"], [])

    def test_stream_and_status_carry_the_vitals_and_counters(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        for needle in ('"system": _system.snapshot()', '"counters": counters', "_heartbeats.record()", "system_sampler_loop()"):
            with self.subTest(needle=needle):
                self.assertIn(needle, source)
        self.assertEqual(source.count('"system": _system.snapshot()'), 2)   # /status and /stream


class HeartbeatCounterTests(unittest.TestCase):
    def test_total_counts_everything_and_the_rate_counts_only_the_last_minute(self):
        counter = telemetry.HeartbeatCounter()
        for stamp in (0.0, 10.0, 20.0, 70.0):
            counter.record(now=stamp)
        self.assertEqual(counter.total, 4)
        self.assertEqual(counter.per_minute(now=75.0), 2)    # the beats at 20 s and 70 s are within 60 s of 75 s; those at 0 s and 10 s are not
        self.assertEqual(counter.per_minute(now=200.0), 0)
        self.assertEqual(counter.total, 4)


class FleetCounterTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_path = listener.DB_PATH
        listener.DB_PATH = Path(self.tmp.name) / "watchdog.db"
        listener.init_db()

    def tearDown(self):
        listener.DB_PATH = self.saved_path
        self.tmp.cleanup()

    def test_counts_last_24_hours_leaves_out_suppressed_noise_and_finds_the_last_red_alarm(self):
        now = datetime.now(timezone.utc)
        rows = [
            (2, "endpoint_critical"), (30, "endpoint_critical"),   # 30 h ago: outside 24 h, but still the older red alarm
            (1, "suppressed"), (3, "freeze"), (5, "heartbeat_lost"), (26, "freeze"),
        ]
        with listener.db() as conn:
            for hours_ago, kind in rows:
                listener._insert_event_locked(conn, (now - timedelta(hours=hours_ago)).isoformat(), "MAIN", kind, "", "info")
            counter = telemetry.HeartbeatCounter()
            counter.record()
            counters = telemetry.fleet_counters(conn, now, now - timedelta(hours=1), counter)
        self.assertEqual(counters["red_alarms_24h"], 1)
        self.assertEqual(counters["events_24h"], 3)              # the red alarm, the freeze and the heartbeat loss; not the suppressed one
        self.assertEqual(counters["last_red_alarm_utc"], (now - timedelta(hours=2)).isoformat())
        self.assertEqual(counters["heartbeats_total"], 1)
        self.assertEqual(counters["heartbeats_per_minute"], 1)
        self.assertIn("listener_started_utc", counters)
        self.assertEqual(counters["since_last_red_s"], 7200.0)     # the red alarm was 2 h ago; the browser counts forward from here
        self.assertEqual(counters["listener_uptime_s"], 3600)      # started an hour ago

    def test_an_empty_log_gives_zeros_and_no_last_alarm(self):
        now = datetime.now(timezone.utc)
        with listener.db() as conn:
            counters = telemetry.fleet_counters(conn, now, now, telemetry.HeartbeatCounter())
        self.assertEqual((counters["events_24h"], counters["red_alarms_24h"]), (0, 0))
        self.assertIsNone(counters["last_red_alarm_utc"])
        self.assertIsNone(counters["since_last_red_s"])            # never alarmed: nothing to count from


class OffAirSignatureTests(unittest.TestCase):
    """When SITE-A's output dies (the player switched off) its detector reports black, frozen and silent TOGETHER. The listener waited for any
    ONE marker to reach 60 s and then raised a separate red for each (Freeze, Black, Silence: three emails per episode). Black plus freeze alone
    is NOT enough: SITE-A's history has two real black-and-frozen episodes of 16 s and 28 s with no silence, which must stay quiet. The third
    marker (silence, posted at 45 s) makes it a red under ONE cause. Built from the recorded episode of 2026-10-13 11:22:49 UTC."""

    START = datetime(2026, 10, 13, 11, 22, 49, tzinfo=timezone.utc)

    def event(self, kind, at_s, ongoing, phase="ongoing", severity="warning"):
        moment = self.START + timedelta(seconds=at_s)
        return {"timestamp_utc": moment.isoformat(), "source_id": "SITE-A", "event_type": kind, "severity": severity,
                "detail": listener.json.dumps({"phase": phase, "ongoing_s": ongoing, "limit_s": 45 if kind == "silence" else 15})}

    def episode(self, seconds_in):
        """The markers the detector had posted by `seconds_in`, newest first (the order the listener reads them)."""
        posted = [("black", 15.1, 15.1), ("freeze", 15.3, 15.3), ("silence", 45.4, 45.4), ("black", 45.3, 45.3), ("freeze", 45.5, 45.5),
                  ("silence", 75.5, 75.5), ("black", 75.4, 75.4), ("freeze", 75.6, 75.6)]
        events = [self.event(kind, at, ongoing) for kind, at, ongoing in posted if at <= seconds_in]
        return list(reversed(events))

    def snapshot(self, events, seconds_in):
        now = self.START + timedelta(seconds=seconds_in)
        row = {"source_id": "SITE-A", "last_seen_utc": (now - timedelta(seconds=3)).isoformat(), "state": "healthy", "state_changed_utc": now.isoformat(),
               "expected_interval_seconds": 30, "instance_alert": None, "expected_instances": None, "latest_event": None,
               "last_payload_json": listener.json.dumps({"hostname": "SITE-A", "expected_interval_seconds": 30, "current_item": "x.mp4", "status": "decoding"})}
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "SITE-A")
        return listener._endpoint_snapshot(endpoint, row, events, now)

    def test_nothing_is_red_before_the_third_marker_and_red_the_moment_it_arrives(self):
        self.assertNotEqual(self.snapshot(self.episode(44), 44)["status_level"], "critical")      # black and frozen only
        shot = self.snapshot(self.episode(46), 46)                                                   # silence has just been posted
        self.assertEqual((shot["status_level"], shot["root_cause"]), ("critical", "Off Air"))
        self.assertTrue(shot["email_eligible"])

    def test_it_is_one_alarm_under_one_cause_for_the_whole_episode(self):
        causes = {seconds: self.snapshot(self.episode(seconds), seconds)["root_cause"] for seconds in (46, 55, 62, 70, 80, 86)}
        self.assertEqual(set(causes.values()), {"Off Air"}, causes)       # before: Freeze > 60s, then Black > 60s, then Silence > 60s, one email each

    def test_black_and_frozen_without_silence_never_raises_it(self):
        short = [self.event("freeze", 15.3, 15.3), self.event("black", 15.1, 15.1)]
        for seconds in (16, 20, 28):
            with self.subTest(seconds=seconds):
                self.assertNotEqual(self.snapshot(short, seconds)["root_cause"], "Off Air")
        longer = self.snapshot(self.episode(44), 44)
        self.assertNotEqual(longer["root_cause"], "Off Air")

    def test_a_marker_the_detector_has_closed_or_let_go_stale_does_not_count(self):
        events = self.episode(46)
        closed = [self.event("silence", 47, 46.0, phase="end")] + events
        self.assertNotEqual(self.snapshot(closed, 47)["root_cause"], "Off Air")
        self.assertNotEqual(self.snapshot(events, 46 + listener.ONGOING_FRESH_SECONDS + 5)["root_cause"], "Off Air")      # nobody refreshed them
        informational = [dict(e, severity="informational") for e in events]
        self.assertNotEqual(self.snapshot(informational, 46)["root_cause"], "Off Air")

    def test_it_applies_to_the_decode_sites_only_not_to_main_or_backup(self):
        now = self.START + timedelta(seconds=46)
        feed_events = [dict(e, source_id="MAIN", detail=listener.json.dumps({"phase": "ongoing", "feed": "usa", "ongoing_s": 46})) for e in self.episode(46)]
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "MAIN-USA")
        self.assertIsNone(listener._off_air_signature(endpoint, feed_events, now))

    def test_an_explicit_off_air_event_is_still_an_immediate_red(self):
        explicit = [self.event("off_air", 1, 1, severity="critical")]
        self.assertEqual(self.snapshot(explicit, 2)["status_level"], "critical")


class PageVersionTests(unittest.TestCase):
    """A dashboard left open for days keeps the build it loaded with, so a deployed fix never reached it (the owner still saw UTC on an open tab after
    New York time was live). Every frame now names the build the listener serves; a page that differs offers a one-tap update. It must NEVER
    reload by itself: a reload loses the browser's permission to play sound, and an unattended screen would be left with a silent siren."""

    def test_the_version_is_a_short_stable_fingerprint_of_the_page_and_the_worker_and_not_of_the_key(self):
        import hashlib
        dash = importlib.import_module("app.dashboard")
        self.assertRegex(dash.PAGE_VERSION, r"^[0-9a-f]{12}$")
        self.assertEqual(dash.PAGE_VERSION, hashlib.sha256((dash._TEMPLATE + dash.SERVICE_WORKER_JS).encode("utf-8")).hexdigest()[:12])
        self.assertEqual(listener.render_dashboard_html("one-key").count(dash.PAGE_VERSION), 1)
        # the key is not part of it: every viewer of the same build has the same version
        self.assertEqual(listener.render_dashboard_html("one-key").replace("one-key", "").count("var pageVersion"), 1)
        self.assertIn(f'var pageVersion = "{dash.PAGE_VERSION}";', listener.render_dashboard_html("one-key"))
        self.assertIn(f'var pageVersion = "{dash.PAGE_VERSION}";', listener.render_dashboard_html("a-different-key"))

    def test_every_frame_and_status_carries_it(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count('"version": PAGE_VERSION'), 2)       # /status and /stream

    def test_a_stale_page_offers_an_update_and_never_reloads_itself(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertIn('id="update-chip"', page)
        self.assertIn("function checkVersion(frame)", page)
        self.assertIn("frame.version === pageVersion", page)
        self.assertEqual(page.count("window.location.reload()"), 1)       # exactly one: the tap on the chip, never a timer or a frame handler
        handler = page.split('updateChip.addEventListener("click", function () {')[1].split("});")[0]
        self.assertLess(handler.index("unlockAudio();"), handler.index("window.location.reload();"))   # the tap that updates also restores the siren's permission
        self.assertNotIn("setTimeout(function () { window.location.reload", page)


class NoManualStreamOrSirenButtonsTests(unittest.TestCase):
    """The owner's rule (2026-10-26): the stream must always be connected, so there is no button to reconnect it, and no Test siren button on the dashboard."""

    def page(self):
        return listener.render_dashboard_html(TEST_TOKEN)

    def test_the_two_buttons_and_their_code_are_gone(self):
        page = self.page()
        for gone in ('id="refresh"', 'id="siren-test"', "Reconnect stream", "Test siren", "refreshButton", "sirenTest"):
            self.assertNotIn(gone, page, gone)

    def test_the_other_siren_and_stream_controls_are_still_there(self):
        page = self.page()
        for element_id in ("sound-toggle", "mute-toggle", "awake-toggle", "siren-strip", "stream-pill"):
            self.assertIn(f'id="{element_id}"', page, element_id)

    def test_no_dead_test_siren_state_is_left_behind(self):
        page = self.page()
        self.assertNotIn("uiState.testing", page)
        self.assertNotIn("testing: false", page)

    def test_the_stream_opens_at_load_and_heals_itself_when_closed_or_silent(self):
        page = self.page()
        keep = page.split("function keepAlive() {")[1].split("\n  }\n")[0]
        self.assertIn("eventSource.readyState === 2", keep)                 # closed for good by an error (a proxy 502, Cloudflare 524): open it again
        self.assertIn("STALL_RECONNECT_MS", keep)                           # open but silent: open it again
        self.assertIn("var STALL_RECONNECT_MS = 15000;", page)              # three missed frames (the listener pushes one every 5 s)
        self.assertIn("var RECONNECT_SPACING_MS = 20000;", page)            # but never faster than this, so a slow link can finish connecting
        self.assertNotIn("sinceConnect > 45000", page)                      # the old, slower rule is gone
        self.assertIn("\n  connectStream();\n  setInterval(tickLive, 1000);", page)
        tick = page.split("function tickLive() {")[1].split("\n  }\n")[0]
        self.assertIn("keepAlive();", tick)                                 # checked every second

    def test_it_also_reconnects_the_moment_the_page_or_the_network_comes_back(self):
        page = self.page()
        self.assertIn('["pageshow", "online", "focus"].forEach(function (name) { window.addEventListener(name, resumeNow); });', page)
        self.assertIn('document.addEventListener("visibilitychange", function () { if (!document.hidden) { resumeNow(); } });', page)
        resume = page.split("function resumeNow() {")[1].split("\n  }\n")[0]
        self.assertIn("connectStream();", resume)


class DashboardFollowsAccessTests(unittest.TestCase):
    """When the super user switches access OFF or ON from the control panel, a dashboard that is already open follows by itself: OFF puts the unavailable screen over it
    (no refresh, the page and its siren permission stay alive), ON takes it away and the live stream opens again."""

    def page(self):
        return listener.render_dashboard_html(TEST_TOKEN)

    def body(self, page, name):
        return page.split(name + " {")[1].split("\n  }\n")[0]

    def test_each_page_has_exactly_one_place_for_the_off_screen(self):
        for render in (listener.render_dashboard_html, listener.render_topology_html):
            self.assertEqual(render(TEST_TOKEN).count("<!--off-screen-->"), 1)

    def test_the_page_knows_how_to_show_hide_and_follow_the_off_screen(self):
        page = self.page()
        for name in ("function readAccess()", "function showOff()", "function hideOff()", "function followAccess()", "function renderControlBanner("):
            self.assertIn(name, page)
        self.assertIn("var ACCESS_POLL_MS = 5000;", page)

    def test_it_reads_the_access_state_from_the_open_health_answer_without_caching(self):
        read = self.body(self.page(), "function readAccess()")
        self.assertIn('fetch("/health"', read)
        self.assertIn('cache: "no-store"', read)
        self.assertIn('credentials: "omit"', read)
        self.assertIn(".catch(function () { return null; })", read)         # an unreachable server is "unknown", never "off"

    def test_off_closes_the_stream_silences_the_siren_and_shows_the_screen(self):
        show = self.body(self.page(), "function showOff()")
        for part in ("eventSource.close();", "setAlarm(false);", "uiState.criticalActive = false;", "offScreen.hidden = false;", "uiState.offShown = true;",
                     'document.documentElement.style.overflow = "hidden";'):
            self.assertIn(part, show)

    def test_on_hides_the_screen_and_opens_the_stream_again(self):
        hide = self.body(self.page(), "function hideOff()")
        self.assertIn("offScreen.hidden = true;", hide)
        self.assertIn('document.documentElement.style.overflow = "";', hide)          # the dashboard scrolls again
        self.assertIn("uiState.offShown = false;", hide)
        self.assertIn("connectStream();", hide)

    def test_the_server_tells_the_page_at_once_and_an_error_makes_it_look(self):
        page = self.page()
        self.assertIn('eventSource.addEventListener("access", function', page)
        error = page.split("eventSource.onerror = function () {")[1].split("\n    };\n")[0]
        self.assertIn("followAccess();", error)

    def test_while_the_off_screen_is_up_the_page_only_asks_how_access_is_and_never_reconnects(self):
        page = self.page()
        keep = self.body(page, "function keepAlive()")
        self.assertIn("uiState.offShown", keep)
        self.assertLess(keep.index("uiState.offShown"), keep.index("connectStream();"))
        off_block = keep.split("var sinceConnect")[0]
        self.assertIn("return;", off_block)                                  # while the screen is up the function stops before any reconnect
        self.assertIn("ACCESS_POLL_MS", keep)
        resume = self.body(page, "function resumeNow()")
        self.assertIn("uiState.offShown", resume)
        self.assertLess(resume.index("uiState.offShown"), resume.index("connectStream();"))

    def test_the_super_users_banner_follows_the_frames_with_no_refresh(self):
        page = self.page()
        self.assertIn("renderControlBanner(frame.control);", page)
        banner = self.body(page, "function renderControlBanner(control)")
        self.assertIn('control.access === "off"', banner)
        self.assertIn("control.off_since", banner)
        self.assertIn("node.remove();", banner)                               # ON takes it away again

    def test_none_of_the_new_code_writes_html(self):
        page = self.page()
        for name in ("function readAccess()", "function showOff()", "function hideOff()", "function followAccess()", "function renderControlBanner(control)"):
            self.assertNotIn("innerHTML", self.body(page, name), name)


class InstanceGraceTests(unittest.TestCase):
    """A player that an operator closes and opens again, or that restarts itself, is back within 10 to 25 seconds. Each of those used to raise a critical alarm and an e-mail
    (seven on BACKUP-SERVER in one day). A playout process that is missing from its heartbeat, or whose launcher or engine is reported down, is now a WARNING for the first
    45 seconds and only then critical. Core keeps the clock (`_absent`, stored with the heartbeat); a sender's own copy of it is thrown away."""
    NOW = datetime(2026, 11, 1, 3, 0, 0, tzinfo=timezone.utc)
    FEEDS = ["usa", "caribbean"]

    def ago(self, seconds):
        return (self.NOW - timedelta(seconds=seconds)).isoformat()

    def marks(self, payload, previous=None, expectation=None):
        return listener._with_absent_marks(payload, previous, self.FEEDS if expectation is None else expectation, self.NOW)

    def snapshot(self, endpoint_id, payload, source="MAIN"):
        row = {"source_id": source, "last_seen_utc": self.NOW.isoformat(), "last_payload_json": listener.json.dumps(payload), "state": "healthy",
               "state_changed_utc": self.NOW.isoformat(), "expected_interval_seconds": 10, "instance_alert": None, "expected_instances": None, "latest_event": None}
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == endpoint_id)
        return listener._endpoint_snapshot(endpoint, row, [], self.NOW, None)

    def only(self, *feeds, **down):
        instances = [edge_instance(feed) for feed in feeds]
        for instance in instances:
            instance.update(down.get(instance["feed"], {}))
        return edge_payload(*instances, monitored=False)

    # ---- the clock ----
    def test_the_grace_is_forty_five_seconds(self):
        self.assertEqual(listener.INSTANCE_DOWN_GRACE_SECONDS, 45)

    def test_a_feed_missing_from_the_heartbeat_gets_a_clock_that_starts_now(self):
        self.assertEqual(self.marks(self.only("caribbean"))["_absent"], {"usa": self.NOW.isoformat()})

    def test_the_clock_is_kept_from_one_heartbeat_to_the_next(self):
        previous = {"_absent": {"usa": self.ago(20)}}
        self.assertEqual(self.marks(self.only("caribbean"), previous)["_absent"], {"usa": self.ago(20)})

    def test_a_feed_that_is_back_loses_its_clock_and_a_second_absence_starts_a_new_one(self):
        previous = {"_absent": {"usa": self.ago(20)}}
        back = self.marks(self.only("usa", "caribbean"), previous)
        self.assertNotIn("_absent", back)
        again = self.marks(self.only("caribbean"), back)
        self.assertEqual(again["_absent"], {"usa": self.NOW.isoformat()})

    def test_a_launcher_or_an_engine_reported_down_counts_as_down(self):
        for field in ("engine_present", "launcher_present"):
            with self.subTest(field=field):
                payload = self.marks(self.only("usa", "caribbean", usa={field: False}))
                self.assertEqual(payload["_absent"], {"usa": self.NOW.isoformat()})
        self.assertNotIn("_absent", self.marks(self.only("usa", "caribbean")))

    def test_each_feed_has_its_own_clock(self):
        previous = {"_absent": {"usa": self.ago(30)}}
        both = self.marks(edge_payload(monitored=False), previous)
        self.assertEqual(both["_absent"], {"usa": self.ago(30), "caribbean": self.NOW.isoformat()})

    def test_a_sender_cannot_forge_or_carry_over_a_clock(self):
        forged = self.only("usa", "caribbean")
        forged["_absent"] = {"usa": self.ago(500)}
        self.assertNotIn("_absent", self.marks(forged))
        claimed = self.only("caribbean")
        claimed["_absent"] = {"usa": self.ago(500)}
        self.assertEqual(self.marks(claimed)["_absent"], {"usa": self.NOW.isoformat()})

    def test_only_a_named_list_of_feeds_gets_clocks(self):
        for expectation in (None, 2, 0, "usa"):
            with self.subTest(expectation=expectation):
                self.assertNotIn("_absent", listener._with_absent_marks(self.only("caribbean"), None, expectation, self.NOW))
        self.assertEqual(self.marks(self.only("caribbean"), None, ["caribbean"]).get("_absent"), None)

    def test_a_damaged_or_future_old_clock_restarts_the_clock_and_never_fails_a_heartbeat(self):
        for bad in ("yesterday", "", None, 12, [], {"a": 1}, (self.NOW + timedelta(hours=1)).isoformat()):
            with self.subTest(old=bad):
                self.assertEqual(self.marks(self.only("caribbean"), {"_absent": {"usa": bad}})["_absent"], {"usa": self.NOW.isoformat()})
        for previous in ("not a dict", 5, [], {"_absent": "x"}, {"_absent": [1]}):
            with self.subTest(previous=previous):
                self.assertEqual(self.marks(self.only("caribbean"), previous)["_absent"], {"usa": self.NOW.isoformat()})

    # ---- the judgement of the heartbeat ----
    def test_a_short_absence_is_not_yet_a_missing_feed(self):
        payload = self.marks(self.only("caribbean"), {"_absent": {"usa": self.ago(12)}})
        self.assertEqual(listener.evaluate_instances(payload, self.FEEDS, payload["_absent"], self.NOW), (None, []))

    def test_a_long_absence_is_named(self):
        payload = self.marks(self.only("caribbean"), {"_absent": {"usa": self.ago(45)}})
        self.assertEqual(listener.evaluate_instances(payload, self.FEEDS, payload["_absent"], self.NOW), (False, ["USA feed not running"]))

    def test_with_one_feed_confirmed_and_one_pending_only_the_confirmed_one_is_named(self):
        payload = self.marks(edge_payload(monitored=False), {"_absent": {"usa": self.ago(60), "caribbean": self.ago(5)}})
        self.assertEqual(listener.evaluate_instances(payload, self.FEEDS, payload["_absent"], self.NOW), (False, ["USA feed not running"]))

    def test_everything_running_is_still_fine(self):
        payload = self.marks(self.only("usa", "caribbean"))
        self.assertEqual(listener.evaluate_instances(payload, self.FEEDS, payload.get("_absent"), self.NOW), (True, []))

    def test_a_blip_that_ends_inside_the_grace_raises_and_restores_nothing(self):
        self.assertIsNone(listener.next_alert_action("ok", None))                      # the short absence is "not judged": the stored state is left as it was
        self.assertIsNone(listener.next_alert_action("ok", True))                      # and coming back is not a restoration, because nothing was ever raised

    # ---- what the tile shows ----
    def test_for_forty_four_seconds_a_missing_process_is_a_warning_and_never_mailed(self):
        got = self.snapshot("MAIN-USA", self.marks(self.only("caribbean"), {"_absent": {"usa": self.ago(44)}}))
        self.assertEqual((got["status_level"], got["status_label"], got["root_cause"]), ("warning", "Warning", "Process Missing < 45s"))
        self.assertIs(got["email_eligible"], False)
        self.assertIn("44", got["status_detail"])

    def test_at_forty_five_seconds_it_is_the_critical_it_always_was(self):
        got = self.snapshot("MAIN-USA", self.marks(self.only("caribbean"), {"_absent": {"usa": self.ago(45)}}))
        self.assertEqual((got["status_level"], got["status_label"], got["root_cause"]), ("critical", "Player Down", "Process Missing"))
        self.assertIsNot(got["email_eligible"], False)

    def test_a_missing_process_with_no_clock_yet_starts_as_a_warning(self):
        got = self.snapshot("MAIN-USA", self.only("caribbean"))
        self.assertEqual((got["status_level"], got["root_cause"]), ("warning", "Process Missing < 45s"))

    def test_a_halted_engine_and_a_missing_launcher_get_the_same_grace_with_their_own_names(self):
        for field, short, long_ in (("engine_present", "Engine Halted < 45s", "Engine Halted"), ("launcher_present", "Launcher Missing < 45s", "Launcher Missing")):
            for seconds, level, cause in ((10, "warning", short), (50, "critical", long_)):
                with self.subTest(field=field, seconds=seconds):
                    payload = self.marks(self.only("usa", "caribbean", usa={field: False}), {"_absent": {"usa": self.ago(seconds)}})
                    got = self.snapshot("MAIN-USA", payload)
                    self.assertEqual((got["status_level"], got["root_cause"]), (level, cause))

    def test_the_other_feed_is_never_touched(self):
        payload = self.marks(self.only("caribbean"), {"_absent": {"usa": self.ago(10)}})
        self.assertEqual(self.snapshot("MAIN-Caribbean", payload)["status_level"], "healthy")

    def test_backup_server_gets_the_same_grace(self):
        payload = self.marks(self.only("usa"), {"_absent": {"caribbean": self.ago(12)}})
        got = self.snapshot("BACKUP-Caribbean", payload, source="BACKUP-SERVER")
        self.assertEqual((got["status_level"], got["root_cause"]), ("warning", "Process Missing < 45s"))

    def test_the_single_source_studios_have_no_grace_because_they_have_no_players_to_lose(self):
        site_a = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "SITE-A")
        self.assertIsNone(listener._instance_status(site_a, None, 0.0))


class InstanceGraceFlowTests(unittest.TestCase):
    """Through a real (temporary) database: a blip raises nothing; a real outage is mailed once after 45 s; the return is not mailed twice."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_path = listener.DB_PATH
        listener.DB_PATH = Path(self.tmp.name) / "watchdog.db"
        listener.init_db()

    def tearDown(self):
        listener.DB_PATH = self.saved_path
        self.tmp.cleanup()

    def beat(self, payload):
        now = datetime.now(timezone.utc)
        with listener.db() as conn:
            conn.execute(
                "INSERT INTO sources (source_id, last_seen_utc, last_payload_json, state, state_changed_utc, expected_interval_seconds)"
                " VALUES ('BACKUP-SERVER', ?, ?, 'healthy', ?, 10) ON CONFLICT(source_id) DO UPDATE SET "
                "last_seen_utc = excluded.last_seen_utc, last_payload_json = excluded.last_payload_json, state = 'healthy'",
                (now.isoformat(), listener.json.dumps(payload), now.isoformat()),
            )
            _s, _e, endpoints, emails = listener._evaluate_and_sync_locked(conn, now, now.isoformat())
        return {e["endpoint_id"]: e for e in endpoints}, [e["endpoint_id"] for e in emails if e["endpoint_id"].startswith("BACKUP")]

    def payload(self, seconds_missing):
        base = edge_payload(edge_instance("usa"), monitored=False)
        base["hostname"] = "BACKUP-SERVER"
        base["_absent"] = {"caribbean": (datetime.now(timezone.utc) - timedelta(seconds=seconds_missing)).isoformat()}
        return base

    def test_a_blip_is_a_warning_with_no_mail_and_a_real_outage_is_mailed_once(self):
        endpoints, mails = self.beat(self.payload(12))
        self.assertEqual((endpoints["BACKUP-Caribbean"]["status_level"], mails), ("warning", []))
        endpoints, mails = self.beat(self.payload(50))
        self.assertEqual((endpoints["BACKUP-Caribbean"]["status_level"], endpoints["BACKUP-Caribbean"]["root_cause"], mails), ("critical", "Process Missing", ["BACKUP-Caribbean"]))
        endpoints, mails = self.beat(self.payload(80))
        self.assertEqual((endpoints["BACKUP-Caribbean"]["status_level"], mails), ("critical", []))             # still down: no second mail
        both = edge_payload(edge_instance("usa"), edge_instance("caribbean"), monitored=False)
        both["hostname"] = "BACKUP-SERVER"
        endpoints, mails = self.beat(both)
        self.assertEqual((endpoints["BACKUP-Caribbean"]["status_level"], mails), ("healthy", []))


class TapStaysPutTests(unittest.TestCase):
    """The 'tap to allow sound' strip used to vanish the instant the first tap unlocked the audio, moving the page up under the finger in the
    middle of that tap, so a tap on a small target right below it (the 44 px update chip) silently missed. (Tall targets such as a playout tile
    survive the shift: measured on the previous version, whose first tap on a tile did open it.)"""

    def test_the_strip_leaves_after_the_tap_not_during_it(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        render = page.split("function renderSirenStrip()")[1].split("sirenStrip.addEventListener")[0]
        self.assertIn("var STRIP_LEAVES_AFTER_MS = 350;", page)
        self.assertIn("setTimeout(function () { stripLeaveTimer = null; if (!sirenProblem()) { sirenStrip.hidden = true; } }, STRIP_LEAVES_AFTER_MS)", render)
        before_problem_check = render.split("if (!problem) {")[1].split("return;")[0]
        self.assertNotIn("sirenStrip.hidden = true;", before_problem_check.split("setTimeout")[0])     # never hidden synchronously when the problem ends
        self.assertIn("clearTimeout(stripLeaveTimer)", render)                                           # a new problem cancels a pending removal


class HealthEndpointTests(unittest.TestCase):
    """`/health` is for an EXTERNAL uptime monitor and Docker's healthcheck: no token, no status data, a few milliseconds. It exists so the
    public address can be watched 24 hours a day by something other than the page itself (which cannot report its own absence)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_path = listener.DB_PATH
        listener.DB_PATH = Path(self.tmp.name) / "watchdog.db"
        listener.init_db()

    def tearDown(self):
        listener.DB_PATH = self.saved_path
        self.tmp.cleanup()

    def test_a_working_listener_answers_200_with_no_status_data_in_it(self):
        import asyncio
        reply = asyncio.run(listener.health())
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.content["status"], "ok")
        self.assertEqual(reply.content["database"], "ok")
        self.assertGreaterEqual(reply.content["uptime_s"], 0)
        self.assertEqual(set(reply.content), {"status", "database", "uptime_s"})      # nothing about playouts, files, tokens or machines

    def test_a_broken_database_is_a_503_not_a_crash(self):
        import asyncio
        listener.DB_PATH = Path(self.tmp.name)        # a FOLDER where the database file should be: it cannot be opened (db() creates missing folders)
        reply = asyncio.run(listener.health())
        self.assertEqual(reply.status_code, 503)
        self.assertEqual((reply.content["status"], reply.content["database"]), ("degraded", "error"))

    def test_a_listener_stuck_holding_its_lock_is_a_503_within_seconds_not_a_hang(self):
        import asyncio
        saved_lock, saved_wait = listener._db_lock, listener.HEALTH_LOCK_WAIT_SECONDS
        listener.HEALTH_LOCK_WAIT_SECONDS = 0.2
        async def scenario():
            listener._db_lock = asyncio.Lock()
            await listener._db_lock.acquire()          # something has held the lock for good
            started = asyncio.get_running_loop().time()
            reply = await listener.health()
            return reply, asyncio.get_running_loop().time() - started
        try:
            reply, seconds = asyncio.run(scenario())
        finally:
            listener._db_lock, listener.HEALTH_LOCK_WAIT_SECONDS = saved_lock, saved_wait
        self.assertEqual(reply.status_code, 503)
        self.assertEqual(reply.content["status"], "busy")
        self.assertLess(seconds, 2)

    def test_it_is_public_like_the_offline_page_and_docker_watches_it(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        route = source.split('@app.get("/health")')[1].split("async def health")[0]
        self.assertNotIn("require_token", route)
        compose = (LISTENER_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:8420/health", compose)
        self.assertIn("healthcheck:", compose)
        self.assertIn("--timeout-graceful-shutdown", compose)      # a deploy must not wait on open live streams: that is the gap viewers see


class SourcePlayerDownTests(unittest.TestCase):
    """SITE-B (and SITE-A) are judged from what their own detector reports. SITE-B's detector had no report for 'the Insta player is
    shut', so closing the player never went red: its heartbeat kept arriving and nothing said the player was gone. Two ways to say it now
    go red immediately and send the one email: `player_running: false` in the heartbeat, or a `player_down` event."""

    NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)

    def site_b_row(self, **payload):
        return {"source_id": "SITE-B", "last_seen_utc": (self.NOW - timedelta(seconds=20)).isoformat(), "state": "healthy",
                "state_changed_utc": self.NOW.isoformat(), "expected_interval_seconds": 300, "instance_alert": None,
                "expected_instances": None, "latest_event": None,
                "last_payload_json": listener.json.dumps({"hostname": "SITE-B", "expected_interval_seconds": 300, "current_item": "x.mp4", **payload})}

    def snapshot(self, events=(), **payload):
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "SITE-B")
        return listener._endpoint_snapshot(endpoint, self.site_b_row(**payload), list(events), self.NOW)

    def event(self, kind="player_down", seconds_ago=10, **detail):
        return {"timestamp_utc": (self.NOW - timedelta(seconds=seconds_ago)).isoformat(), "source_id": "SITE-B", "event_type": kind,
                "severity": "critical", "detail": listener.json.dumps(detail) if detail else "Insta is not running"}

    def test_the_heartbeat_flag_is_an_immediate_red_with_an_email(self):
        shot = self.snapshot(player_running=False)
        self.assertEqual((shot["status_level"], shot["status_label"], shot["root_cause"]), ("critical", "Player Down", "Player Not Running"))
        self.assertTrue(shot["email_eligible"])

    def test_the_flag_clears_the_moment_the_detector_says_the_player_is_back(self):
        self.assertEqual(self.snapshot(player_running=True)["status_level"], "healthy")
        self.assertEqual(self.snapshot()["status_level"], "healthy")                  # a detector that never sends the flag is judged as before

    def test_the_flag_is_ignored_where_it_does_not_belong_and_when_it_is_not_a_real_false(self):
        for odd in ("false", 0, None, "no", [], {}):
            with self.subTest(odd=odd):
                self.assertEqual(self.snapshot(player_running=odd)["status_level"], "healthy")     # only a real JSON false counts
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "MAIN-USA")
        row = {"source_id": "MAIN", "last_seen_utc": self.NOW.isoformat(), "state": "healthy", "state_changed_utc": self.NOW.isoformat(),
               "expected_interval_seconds": 10, "instance_alert": None, "expected_instances": None, "latest_event": None,
               "last_payload_json": listener.json.dumps({**edge_payload(edge_instance("usa"), edge_instance("caribbean")), "player_running": False})}
        self.assertEqual(listener._endpoint_snapshot(endpoint, row, [], self.NOW)["status_level"], "healthy")   # MAIN/BACKUP use their own instance checks

    def test_a_player_down_event_is_an_immediate_red(self):
        for kind in ("player_down", "insta_not_running", "insta_closed", "insta_stopped", "player_not_running"):
            with self.subTest(kind=kind):
                shot = self.snapshot(events=[self.event(kind)])
                self.assertEqual((shot["status_level"], shot["root_cause"]), ("critical", "Player Not Running"))
                self.assertTrue(shot["email_eligible"])

    def test_the_event_clears_when_the_detector_closes_it_and_fades_if_nobody_does(self):
        closed = [self.event(seconds_ago=1, phase="end"), self.event(seconds_ago=40, phase="ongoing")]
        self.assertEqual(self.snapshot(events=closed)["status_level"], "healthy")
        stale = self.snapshot(events=[self.event(seconds_ago=listener.ONGOING_FRESH_SECONDS + 30, phase="ongoing")])
        self.assertEqual((stale["status_level"], stale["status_label"]), ("warning", "Unconfirmed"))      # a detector that died: not red for ever
        self.assertEqual(self.snapshot(events=[self.event(seconds_ago=listener.ONGOING_UNCONFIRMED_SECONDS + 60)])["status_level"], "healthy")

    def test_the_restart_note_is_not_mistaken_for_the_player_being_down(self):
        note = {"timestamp_utc": self.NOW.isoformat(), "source_id": "SITE-B", "event_type": "suppressed", "severity": "info", "detail": "insta_restarted"}
        self.assertEqual(self.snapshot(events=[note])["status_level"], "healthy")

    def test_the_whole_alarm_path_mails_once_and_only_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            saved = listener.DB_PATH
            listener.DB_PATH = Path(folder) / "watchdog.db"
            listener.init_db()
            try:
                def beat(**payload):
                    now = datetime.now(timezone.utc)
                    with listener.db() as conn:
                        conn.execute(
                            "INSERT INTO sources (source_id, last_seen_utc, last_payload_json, state, state_changed_utc, expected_interval_seconds)"
                            " VALUES ('SITE-B', ?, ?, 'healthy', ?, 300) ON CONFLICT(source_id) DO UPDATE SET "
                            "last_seen_utc = excluded.last_seen_utc, last_payload_json = excluded.last_payload_json, state = 'healthy'",
                            (now.isoformat(), listener.json.dumps({"hostname": "SITE-B", "expected_interval_seconds": 300, **payload}), now.isoformat()))
                        _s, _e, endpoints, emails = listener._evaluate_and_sync_locked(conn, now, now.isoformat())
                    return [e["endpoint_id"] for e in emails if e["endpoint_id"] == "SITE-B"]
                self.assertEqual(beat(current_item="a.mp4", player_running=True), [])
                self.assertEqual(beat(current_item="a.mp4", player_running=False), ["SITE-B"])      # red at once, one email
                self.assertEqual(beat(current_item="a.mp4", player_running=False), [])              # still down: no second email
                self.assertEqual(beat(current_item="a.mp4", player_running=True), [])
            finally:
                listener.DB_PATH = saved


class TwoClocksTests(unittest.TestCase):
    """The dashboard shows CORE's time beside the viewing device's time, because they differ and an alarm time read against the wrong
    one is a mistake. Core's operating system keeps UTC (checked 2026-10-12) but the server is in New York, so Core time means New
    York time and that is the default zone."""

    def test_the_server_sends_its_own_clock_in_utc_and_the_zone_to_show_it_in(self):
        moment = datetime(2026, 10, 12, 10, 32, 8, 123456, tzinfo=timezone(timedelta(hours=2)))   # a non-UTC aware time is still sent as UTC
        clock = listener._clock_snapshot(moment)
        self.assertEqual(clock["server_time_utc"], "2026-10-12T08:32:08.123456+00:00")
        self.assertEqual(clock["timezone"], listener.DISPLAY_TIMEZONE)
        self.assertEqual(listener.DISPLAY_TIMEZONE, "America/New_York")

    def test_the_alert_email_footer_gives_new_york_time_first_and_utc_in_brackets(self):
        moment = datetime(2026, 10, 13, 0, 21, 36, tzinfo=timezone.utc)
        saved = listener._display_zone
        listener._display_zone = lambda: timezone(timedelta(hours=-4), "EDT")      # fixed offset: no zone database needed on a Windows dev PC
        try:
            self.assertEqual(listener._stamp_text(moment), "2026-10-12 08:21:36 PM EDT (00:21:36 UTC)")
            listener._display_zone = lambda: None                                   # no zone data (or a bad name): UTC alone, never an error
            self.assertEqual(listener._stamp_text(moment), "2026-10-13 00:21:36 UTC")
        finally:
            listener._display_zone = saved
        self.assertIn("{_stamp_text(datetime.now(timezone.utc))}", Path(listener.__file__).read_text(encoding="utf-8"))

    def test_a_bad_zone_name_never_breaks_a_stamp(self):
        saved = listener.DISPLAY_TIMEZONE
        listener.DISPLAY_TIMEZONE = "Not/AZone"
        try:
            self.assertEqual(listener._stamp_text(datetime(2026, 10, 13, 0, 0, 0, tzinfo=timezone.utc)), "2026-10-13 00:00:00 UTC")
        finally:
            listener.DISPLAY_TIMEZONE = saved

    def test_status_and_every_stream_frame_carry_it(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count('"clock": _clock_snapshot(now_dt)'), 2)        # /status and /stream
        compose = (LISTENER_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("WATCHDOG_DISPLAY_TIMEZONE: ${WATCHDOG_DISPLAY_TIMEZONE:-America/New_York}", compose)

    def test_the_page_shows_two_labelled_clocks_and_calls_out_a_wrong_device_clock(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        for needle in ('id="clock-core"', 'id="clock-device"', ">New York<", ">This device<", 'id="clock-skew"',
                       "function formatInZone(", "function coreNowMs()", "var SKEW_NOTICE_MS = 5000;", "function zoneCity(", '", last known"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, page)
        # Core's clock is the SERVER's time carried forward on this device's own timer, never the device's clock
        self.assertIn("clock.ms + (until - clock.at)", page)
        self.assertIn("uiState.serverClock = { ms: parseIso(frame.clock.server_time_utc)", page)

    def test_times_that_used_to_follow_the_device_now_follow_core_and_say_so(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertIn("function coreTimestamp(", page)
        self.assertIn('coreTimestamp(incident.time)', page)                          # the incident feed
        self.assertIn('value:coreTimestamp(row.value)', page)                         # "Last heard" in the details
        self.assertIn('id="incident-zone">New York time<', page)                          # and the feed says whose time it is
        # The banner no longer carries an "Updated ... (Core)" line (removed at the owner's request, 2026-10-15): the header's Core clock says it.
        self.assertNotIn('"Updated "', page)


class StaleWarningTests(unittest.TestCase):
    """SITE-B sat on 'warning - Transition Stuck Suspected' for about 10 hours: its detector posted that one warning event at 00:44 UTC,
    never posted anything to close it, and the listener kept an ordinary warning event 'active' forever. Freeze, black, silence and pause
    events already fade (active, then 'unconfirmed', then ignored); ordinary warning events now fade the same way."""

    NOW = datetime(2026, 10, 12, 10, 22, 49, tzinfo=timezone.utc)

    def warning(self, seconds_ago, event_type="transition_stuck_suspected", severity="warning"):
        return {"timestamp_utc": (self.NOW - timedelta(seconds=seconds_ago)).isoformat(), "source_id": "SITE-B", "event_type": event_type,
                "severity": severity, "detail": "Zebife'g Dumewekev.mp4: ended 141.475 s after its expected end (tier warning, declared 556.0 s)"}

    def test_a_fresh_warning_is_a_warning(self):
        status = listener._event_status(self.warning(30), self.NOW)
        self.assertEqual((status["level"], status["label"], status["root_cause"]), ("warning", "Warning", "Transition Stuck Suspected"))

    def test_a_warning_nobody_closed_becomes_unconfirmed_and_never_emails(self):
        status = listener._event_status(self.warning(listener.ONGOING_FRESH_SECONDS + 60), self.NOW)
        self.assertEqual((status["level"], status["label"]), ("warning", "Unconfirmed"))
        self.assertEqual(status["root_cause"], "Transition Stuck Suspected - no end event")
        self.assertIn("never reported it ended", status["detail"])
        self.assertFalse(status["email_eligible"])

    def test_the_real_ten_hour_old_event_no_longer_holds_the_tile(self):
        self.assertIsNone(listener._event_status(self.warning(9 * 3600 + 38 * 60), self.NOW))
        self.assertIsNone(listener._event_status(self.warning(listener.ONGOING_UNCONFIRMED_SECONDS + 1), self.NOW))

    def test_the_real_site_b_tile_is_healthy_again(self):
        row = {"source_id": "SITE-B", "last_seen_utc": (self.NOW - timedelta(seconds=33)).isoformat(), "state": "healthy",
               "state_changed_utc": self.NOW.isoformat(), "expected_interval_seconds": 300, "instance_alert": None, "expected_instances": None,
               "latest_event": None,
               "last_payload_json": listener.json.dumps({"hostname": "SITE-B", "expected_interval_seconds": 300, "current_item": "Rugomevocirik Tok Hetur Hed 1 (3jag).mp4"})}
        events = [self.warning(9 * 3600 + 38 * 60),
                  {**self.warning(9 * 3600 + 39 * 60, "late_pending"), "detail": "Zebife'g Dumewekev.mp4: 90.267 s past its expected end (warning threshold)"},
                  {**self.warning(6 * 3600, "suppressed", "info"), "detail": "paused_or_skipped_row"}]
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "SITE-B")
        shot = listener._endpoint_snapshot(endpoint, row, events, self.NOW)
        self.assertEqual((shot["status_level"], shot["root_cause"]), ("healthy", "Normal"))

    def test_the_same_event_posted_a_minute_ago_would_still_show(self):
        row = {"source_id": "SITE-B", "last_seen_utc": self.NOW.isoformat(), "state": "healthy", "state_changed_utc": self.NOW.isoformat(),
               "expected_interval_seconds": 300, "instance_alert": None, "expected_instances": None, "latest_event": None,
               "last_payload_json": listener.json.dumps({"hostname": "SITE-B", "current_item": "x.mp4"})}
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "SITE-B")
        shot = listener._endpoint_snapshot(endpoint, row, [self.warning(60)], self.NOW)
        self.assertEqual((shot["status_level"], shot["root_cause"]), ("warning", "Transition Stuck Suspected"))

    def test_informational_events_never_colour_a_tile_and_critical_ones_are_not_softened(self):
        self.assertIsNone(listener._event_status(self.warning(30, "late_pending", "informational"), self.NOW))
        old_critical = listener._event_status(self.warning(6 * 3600, "something_serious", "critical"), self.NOW)
        self.assertEqual(old_critical["level"], "critical")          # deliberately unchanged: a red alarm is never quietly downgraded here


class HostileHeartbeatTests(unittest.TestCase):
    """A heartbeat comes from outside. One carrying Infinity, NaN, absurd lengths or a megabyte of text must not be able to
    break the dashboard stream for every viewer, return a 500 from /status, or crash the heartbeat handler. (Python's json
    happily writes Infinity and NaN, which are not JSON: a browser's JSON.parse rejects the whole frame.)"""

    NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    SOURCE = {"source_id": "SITE-A", "last_seen_utc": NOW.isoformat()}

    def shown(self, **fields):
        return listener._now_playing({"current_item": "A.mp4", **fields}, None, self.SOURCE, self.NOW)

    def test_non_finite_or_absurd_item_lengths_are_dropped_and_sane_ones_kept(self):
        for bad in (float("inf"), float("-inf"), float("nan"), 1e308, 10 ** 12, "Infinity", "NaN", 700000, -5, 0):
            with self.subTest(bad=bad):
                self.assertIsNone(self.shown(current_item_duration_s=bad)["duration_s"])
        self.assertEqual(self.shown(current_item_duration_s=3600)["duration_s"], 3600.0)

    def test_no_snapshot_field_can_make_the_json_invalid(self):
        row = {"source_id": "MAIN", "last_seen_utc": self.NOW.isoformat(), "state": "healthy", "state_changed_utc": self.NOW.isoformat(),
               "expected_interval_seconds": 10, "instance_alert": None, "expected_instances": None, "latest_event": None,
               "last_payload_json": listener.json.dumps(edge_payload(
                   {**edge_instance("usa"), "advance_age_seconds": float("nan"), "engine_cpu_cores": float("inf"),
                    "now_playing": {"filename": "A.mp4", "started_utc": self.NOW.isoformat(), "duration_s": float("inf")}},
                   edge_instance("caribbean")))}
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "MAIN-USA")
        snapshot = listener._endpoint_snapshot(endpoint, row, [], self.NOW)
        listener.json.dumps(snapshot, allow_nan=False)        # raises ValueError if any NaN or Infinity got through

    def test_long_text_is_clipped_and_non_text_is_not_passed_through(self):
        long = "x" * 100000
        shown = listener._now_playing({"current_item": long, "next_item": long}, None, self.SOURCE, self.NOW)
        self.assertLessEqual(len(shown["title"]), listener.MAX_TEXT_CHARS)
        self.assertLessEqual(len(shown["next"]), listener.MAX_TEXT_CHARS)
        self.assertTrue(shown["title"].endswith("..."))
        nested = listener._now_playing({"now_playing": {"filename": {"a": [1] * 500}, "result": "r" * 5000}}, None, self.SOURCE, self.NOW)
        self.assertIsInstance(nested["title"], str)
        self.assertLessEqual(len(nested["detail"]), listener.MAX_TEXT_CHARS)
        self.assertEqual(self.shown()["title"], "A.mp4")        # ordinary names are untouched

    def test_the_stream_and_status_are_always_valid_json(self):
        text = listener._strict_json({"a": float("nan"), "b": float("inf"), "c": 1, "d": "Infinity War.mp4", "e": [float("-inf"), 2]})
        def refuse(constant):
            raise AssertionError("not JSON: " + constant)
        parsed = listener.json.loads(text, parse_constant=refuse)       # a browser's JSON.parse would reject NaN and Infinity
        self.assertEqual(parsed, {"a": None, "b": None, "c": 1, "d": "Infinity War.mp4", "e": [None, 2]})
        self.assertEqual(listener._strict_json({"plain": [1, 2.5, "x"]}), listener.json.dumps({"plain": [1, 2.5, "x"]}))   # the common case is untouched
        source = Path(listener.__file__).read_text(encoding="utf-8")
        stream_source = source.split("async def stream(")[1].split('@app.get("/", response_class=HTMLResponse)')[0]
        status_source = source.split("async def status(")[1].split("STREAM_PUSH_SECONDS")[0]
        self.assertIn("_strict_json(frame)", stream_source)      # what every viewer's browser parses
        self.assertIn("_strict_json(", status_source)            # and /status, whose JSONResponse refuses NaN with a 500

    def test_a_heartbeat_interval_that_cannot_be_a_number_falls_back_instead_of_crashing(self):
        default = listener.HEARTBEAT_INTERVAL_SECONDS
        for bad in (float("inf"), float("nan"), 1e400, "abc", None, [], {}, ""):
            with self.subTest(bad=bad):
                self.assertEqual(listener._interval_seconds(bad), default)
        for too_small in (-3, 0):
            with self.subTest(too_small=too_small):
                self.assertEqual(listener._interval_seconds(too_small), 1)
        self.assertEqual(listener._interval_seconds(300), 300)
        self.assertEqual(listener._interval_seconds("30"), 30)
        self.assertEqual(listener._interval_seconds(10 ** 9), listener.MAX_INTERVAL_SECONDS)

    def test_ingest_identifiers_reject_non_text_control_characters_and_overlong_values(self):
        cases = (
            (None, "hostname"),
            ({"nested": True}, "hostname"),
            ("MAIN\nInjected", "hostname"),
            ("x" * (listener.MAX_EVENT_TYPE_CHARS + 1), "event_type"),
        )
        for value, field in cases:
            with self.subTest(value=value, field=field):
                validated, error = listener._validate_text(value, field, listener.MAX_SOURCE_ID_CHARS if field == "hostname" else listener.MAX_EVENT_TYPE_CHARS)
                self.assertIsNone(validated)
                self.assertTrue(error)

    def test_event_detail_is_bounded_and_strictly_serializable(self):
        detail, error = listener._event_detail({"detail": {"phase": "ongoing"}})
        self.assertEqual((detail, error), ('{"phase": "ongoing"}', None))
        detail, error = listener._event_detail({"detail": "x" * (listener.MAX_DETAIL_CHARS + 1)})
        self.assertIsNone(detail)
        self.assertIn("character limit", error)
        detail, error = listener._event_detail({"detail": float("nan")})
        self.assertIsNone(detail)
        self.assertIn("JSON-serializable", error)

    @staticmethod
    def fake_request(*chunks):
        class FakeRequest:
            async def stream(self):
                for chunk in chunks:
                    yield chunk
        return FakeRequest()

    def read_body(self, *chunks):
        import asyncio
        return asyncio.run(listener._json_body(self.fake_request(*chunks)))

    def test_a_good_body_is_read_even_when_it_arrives_in_pieces(self):
        payload, refused = self.read_body(b'{"hostname": "MA', b'IN", "n": 1}')
        self.assertIsNone(refused)
        self.assertEqual(payload, {"hostname": "MAIN", "n": 1})

    def test_oversized_malformed_or_non_object_bodies_are_refused_with_a_4xx_and_never_stored(self):
        big = b'{"hostname": "X", "current_item": "' + b"x" * (listener.MAX_BODY_BYTES + 10) + b'"}'
        cases = (
            ("one huge chunk", (big,), 413),
            ("many small chunks that add up past the limit", tuple(b"x" * 1024 for _ in range(listener.MAX_BODY_BYTES // 1024 + 2)), 413),
            ("not JSON at all", (b"hostname=MAIN",), 400),
            ("empty", (b"",), 400),
            ("a list", (b"[1, 2, 3]",), 400),
            ("a bare number", (b"42",), 400),
            ("nesting deep enough to overflow the parser, but under the size limit", (b"[" * 60000,), 400),
            ("the same nesting once it is also over the size limit", (b"[" * 100000,), 413),
        )
        for label, chunks, status in cases:
            with self.subTest(label=label):
                payload, refused = self.read_body(*chunks)
                self.assertIsNone(payload)
                self.assertEqual(refused.status_code, status)

    def test_every_handler_that_takes_a_body_reads_it_through_the_bounded_reader(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("await _json_body(request)"), 11)     # /heartbeat, /event, /alerts/reset, the three control doors (POST /control, POST /control/session, POST /control/panel/act), the three staff doors (POST /staff/act, POST /control/panel/staff, POST /control/staff/rows), and the two matrix doors (POST /control/matrix/switch on Core, POST /control/panel/matrix on the standby)
        self.assertNotIn("await request.json()", source)

    def test_the_heartbeat_counter_cannot_grow_without_limit(self):
        counter = telemetry.HeartbeatCounter()
        for i in range(telemetry.MAX_TRACKED_HEARTBEATS + 500):
            counter.record(now=float(i) / 1000)      # a flood inside one second
        self.assertLessEqual(counter.per_minute(now=1.0), telemetry.MAX_TRACKED_HEARTBEATS)
        self.assertEqual(counter.total, telemetry.MAX_TRACKED_HEARTBEATS + 500)

    def test_only_valid_heartbeats_are_counted(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        handler = source.split("async def heartbeat(request: Request)")[1].split("async def event(")[0]
        self.assertLess(handler.index("if not source_id"), handler.index("_heartbeats.record()"))

    def test_the_event_log_has_the_indexes_the_counters_need(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            saved = listener.DB_PATH
            listener.DB_PATH = Path(folder) / "watchdog.db"
            try:
                listener.init_db()
                with listener.db() as conn:
                    names = {row[1] for row in conn.execute("PRAGMA index_list(events)")}
            finally:
                listener.DB_PATH = saved
        self.assertTrue({"idx_events_timestamp", "idx_events_type_timestamp"} <= names, names)


class NowPlayingFreshnessTests(unittest.TestCase):
    """Why SITE-B and SITE-A looked 'stuck': the name was the last heartbeat's, with no sign of how old that was."""

    NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)

    def source(self, seconds_ago):
        return {"source_id": "SITE-B", "last_seen_utc": (self.NOW - timedelta(seconds=seconds_ago)).isoformat()}

    def test_the_name_still_comes_through_and_now_carries_its_age(self):
        shown = listener._now_playing({"current_item": "NUD LOZE BOWU WUT 4.mp4"}, None, self.source(166), self.NOW)
        self.assertEqual(shown["title"], "NUD LOZE BOWU WUT 4.mp4")
        self.assertEqual(shown["detail"], "Reported by heartbeat")
        self.assertEqual(shown["age_s"], 166.0)
        self.assertEqual(shown["reported_utc"], (self.NOW - timedelta(seconds=166)).isoformat())
        self.assertIsNone(shown["started_utc"])
        self.assertIsNone(shown["elapsed_s"])

    def test_old_callers_without_a_source_still_work(self):
        shown = listener._now_playing({"current_item": "x.mp4"}, None)
        self.assertEqual(shown["title"], "x.mp4")
        self.assertIsNone(shown["age_s"])

    def test_item_position_comes_from_the_senders_own_two_timestamps(self):
        # Sender clock is an HOUR slow. Item started 30 s before the heartbeat, which Core heard 5 s ago.
        slow = self.NOW - timedelta(hours=1)
        payload = {
            "current_item": "A.mp4", "timestamp": slow.isoformat(),
            "current_item_started_utc": (slow - timedelta(seconds=30)).isoformat(),
            "current_item_duration_s": 120, "next_item": "B.mp4",
        }
        shown = listener._now_playing(payload, None, self.source(5), self.NOW)
        self.assertEqual(shown["elapsed_s"], 35.0)       # 30 s at the heartbeat + 5 s since: the slow clock cancels out
        self.assertEqual(shown["duration_s"], 120.0)
        self.assertEqual(shown["next"], "B.mp4")

    def test_without_a_sender_timestamp_the_servers_clock_is_used(self):
        payload = {"current_item": "A.mp4", "current_item_started_utc": (self.NOW - timedelta(seconds=40)).isoformat(), "current_item_duration_s": 90}
        self.assertEqual(listener._now_playing(payload, None, self.source(5), self.NOW)["elapsed_s"], 40.0)

    def test_a_time_with_no_zone_is_read_as_utc_and_a_bad_one_is_ignored(self):
        payload = {"current_item": "A.mp4", "current_item_started_utc": "2026-10-12T11:59:00", "current_item_duration_s": 90}
        self.assertEqual(listener._now_playing(payload, None, self.source(0), self.NOW)["elapsed_s"], 60.0)
        payload["current_item_started_utc"] = "not a time"
        self.assertIsNone(listener._now_playing(payload, None, self.source(0), self.NOW)["elapsed_s"])

    def test_a_zero_or_negative_length_is_ignored_and_the_position_never_goes_negative(self):
        payload = {"current_item": "A.mp4", "current_item_duration_s": 0, "current_item_started_utc": (self.NOW + timedelta(seconds=30)).isoformat()}
        shown = listener._now_playing(payload, None, self.source(0), self.NOW)
        self.assertIsNone(shown["duration_s"])
        self.assertEqual(shown["elapsed_s"], 0.0)

    def test_similar_looking_fields_at_the_top_of_a_heartbeat_are_not_mistaken_for_item_timing(self):
        payload = {"current_item": "A.mp4", "started_at": "2026-10-12T11:00:00+00:00", "duration_s": 5, "next": "nope"}
        shown = listener._now_playing(payload, None, self.source(0), self.NOW)
        self.assertEqual((shown["started_utc"], shown["duration_s"], shown["next"]), (None, None, None))

    def test_a_now_playing_object_can_carry_the_timing_too(self):
        payload = {"now_playing": {"filename": "PETA HEN WUT 24.mp4", "started_utc": (self.NOW - timedelta(seconds=20)).isoformat(), "duration_s": 60, "next": "NEXT.mp4"}}
        shown = listener._now_playing(payload, None, self.source(0), self.NOW)
        self.assertEqual((shown["title"], shown["elapsed_s"], shown["duration_s"], shown["next"]), ("PETA HEN WUT 24.mp4", 20.0, 60.0, "NEXT.mp4"))

    def test_endpoint_snapshot_includes_the_freshness_fields(self):
        row = {"source_id": "SITE-A", "last_seen_utc": (self.NOW - timedelta(seconds=12)).isoformat(),
               "last_payload_json": listener.json.dumps({"hostname": "SITE-A", "current_item": "GUF 10 BUROF LUZIC 2.mp4", "status": "ok"}),
               "state": "healthy", "state_changed_utc": self.NOW.isoformat(), "expected_interval_seconds": 30,
               "instance_alert": None, "expected_instances": None, "latest_event": None}
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "SITE-A")
        shot = listener._endpoint_snapshot(endpoint, row, [], self.NOW)
        self.assertEqual(shot["now_playing"]["title"], "GUF 10 BUROF LUZIC 2.mp4")
        self.assertEqual(shot["now_playing"]["age_s"], 12.0)


class WordingRemovedTests(unittest.TestCase):
    """the owner asked for three statements to be taken out of the app (2026-10-15). They must not come back by accident."""

    def test_the_removed_statements_are_gone_from_the_dashboard_and_the_topology_page(self):
        for render in (listener.render_dashboard_html, listener.render_topology_html):
            page = render(TEST_TOKEN)
            for gone in ("Red remains reserved for active critical events", "and sends the alarms", '"Updated "'):
                with self.subTest(render=render.__name__, gone=gone):
                    self.assertNotIn(gone, page)

    def test_the_core_card_on_the_map_is_tagged_core_new_york(self):
        # the owner, 2026-10-15: the tag read "Core // Receives and alarms"; it must read "Core // New York" (where Core is).
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertIn('"Core // New York"', page)
        self.assertNotIn("Receives and alarms", page)

    def test_the_four_players_hosted_at_core_are_located_in_new_york(self):
        # the owner, 2026-10-15: every node that said "Core" as its place must say "New York" (tile subtitle, map tag, phone layout).
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertEqual(page.count('location:"New York"'), 4)             # MAIN-USA, MAIN-Caribbean, BACKUP-USA, BACKUP-Caribbean
        self.assertNotIn('location:"Core"', page)
        self.assertIn('location:"Region A"', page)                          # the other two sites keep their own places
        self.assertIn('location:"Region B"', page)

    def test_the_healthy_banner_keeps_its_headline(self):
        self.assertIn("All six playout endpoints are healthy", listener.render_dashboard_html(TEST_TOKEN))


class TopologyMirrorsDashboardTests(unittest.TestCase):
    """The topology map's playout cards say On air / Off air / Last reported exactly as the dashboard tiles do: one function decides, both draw it."""

    def test_one_function_decides_the_label_and_both_views_use_it(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertEqual(page.count("function airStatus("), 1)
        self.assertGreaterEqual(page.count("airStatus("), 3)               # defined once, used by the tile and by the map card
        self.assertIn("holo-air", page)                                   # the map card has its own label line
        self.assertNotIn(".holo-now.is-off-air::before", page)            # the old CSS-only OFF AIR prefix is replaced by a real label

    def test_the_label_words_are_the_dashboards(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        for words in ('"Off air"', '"Last reported"', '"On air"'):
            with self.subTest(words=words):
                self.assertIn(words, page)


class AlwaysOnProgressTests(unittest.TestCase):
    """Every player always shows a progress bar (the owner, 2026-10-15): the bar is never hidden for lack of data, and it is on the map as well."""

    def test_the_bar_is_painted_for_every_state_and_never_hidden(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertEqual(page.count("function paintProgress("), 1)
        self.assertNotIn("part.progress.hidden", page)
        for mode in ("is-live", "is-unknown", "is-off", "is-stale", "is-none"):
            with self.subTest(mode=mode):
                self.assertIn(mode, page)
        self.assertIn("Length not reported", page)
        self.assertIn("holo-progress", page)

    def test_a_player_that_reports_only_a_clock_length_still_gets_its_length(self):
        # MAIN's Insta playlist sends "00:26:42"; BACKUP's AdMax log gives the play range. Both senders now send duration_s and started_utc inside
        # now_playing (edge/); the listener turns those into elapsed_s and duration_s for the bar.
        now = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
        started = now - timedelta(minutes=5)
        payload = {"timestamp": now.isoformat(), "instances": [{"feed": "usa", "now_playing": {
            "filename": "DUWEW NIV WUT. 4.mp4", "result": "Started 20:06:27", "duration": "00:26:42", "duration_s": 1602,
            "started_utc": started.isoformat(), "next": "WABINI KEMUMASID PABID.mp4"}}]}
        source = {"last_seen_utc": now.isoformat()}
        playing = listener._now_playing(payload, payload["instances"][0], source, now)
        self.assertEqual(playing["duration_s"], 1602)
        self.assertAlmostEqual(playing["elapsed_s"], 300, delta=1)
        self.assertEqual(playing["next"], "WABINI KEMUMASID PABID.mp4")


class KeepAliveTests(unittest.TestCase):
    """The installed app / open tab stays alive unless switched off on purpose (the owner, 2026-10-15)."""

    def test_the_screen_wake_lock_is_wired_with_a_switch_that_is_remembered(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        for needle in ('id="awake-toggle"', "navigator.wakeLock", "watchdog-keep-awake", "function requestAwake(", "function releaseAwake("):
            with self.subTest(needle=needle):
                self.assertIn(needle, page)

    def test_the_app_resumes_when_it_returns_and_reopens_a_dead_stream(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        for needle in ("function resumeNow(", '"pageshow"', '"online"', "eventSource.readyState === 2", "lastConnectAt"):
            with self.subTest(needle=needle):
                self.assertIn(needle, page)


class HttpsPolicyTests(unittest.TestCase):
    """Plain http page visits are sent to https, and https answers carry HSTS, for this hostname only (the owner asked for the most secure and fastest
    way, 2026-10-15). The stored Cloudflare tokens cannot create redirect rules and a zone-wide switch would touch every other site on the zone, so
    the app does it. It trusts only what Cloudflare reports about the visitor (CF-Visitor / X-Forwarded-Proto): LAN senders, the container health
    check and tests carry neither and are left exactly as before."""

    def test_visitor_scheme_reads_what_cloudflare_reports(self):
        self.assertEqual(listener.visitor_scheme({"cf-visitor": '{"scheme":"http"}'}), "http")
        self.assertEqual(listener.visitor_scheme({"cf-visitor": '{"scheme":"https"}'}), "https")
        self.assertEqual(listener.visitor_scheme({"x-forwarded-proto": "https, http"}), "https")
        self.assertEqual(listener.visitor_scheme({"x-forwarded-proto": "http"}), "http")
        # CF-Visitor wins when both are present
        self.assertEqual(listener.visitor_scheme({"cf-visitor": '{"scheme":"https"}', "x-forwarded-proto": "http"}), "https")

    def test_no_report_or_garbage_means_not_through_cloudflare(self):
        for headers in ({}, {"cf-visitor": "not json"}, {"cf-visitor": '{"scheme":"gopher"}'}, {"cf-visitor": "[1]"}, {"x-forwarded-proto": "ftp"}):
            with self.subTest(headers=headers):
                self.assertEqual(listener.visitor_scheme(headers), "")

    def test_only_plain_http_page_visits_are_redirected(self):
        self.assertEqual(listener.https_redirect_target("GET", "http", "/", ""), "https://watchdog.corp.example/")
        self.assertEqual(listener.https_redirect_target("HEAD", "http", "/topology", ""), "https://watchdog.corp.example/topology")
        self.assertEqual(listener.https_redirect_target("GET", "http", "/stream", "token=abc"), "https://watchdog.corp.example/stream?token=abc")
        # a POST is never redirected: a sender posting to an http:// address would otherwise break (a redirected POST is retried as a GET)
        self.assertIsNone(listener.https_redirect_target("POST", "http", "/heartbeat", ""))
        self.assertIsNone(listener.https_redirect_target("GET", "https", "/", ""))
        self.assertIsNone(listener.https_redirect_target("GET", "", "/", ""))      # LAN sender, health check, tests

    def test_the_redirect_always_goes_to_our_own_host(self):
        # the target is built from a constant, never from the request's Host header, so it cannot be turned into an open redirect
        self.assertEqual(listener.PUBLIC_HOST, "watchdog.corp.example")
        target = listener.https_redirect_target("GET", "http", "//evil.example/x", "")
        self.assertTrue(target.startswith("https://watchdog.corp.example/"), target)

    def test_hsts_is_modest_and_scoped_to_this_host(self):
        self.assertEqual(listener.HSTS_VALUE, "max-age=15552000")                       # 180 days
        self.assertNotIn("includeSubDomains", listener.HSTS_VALUE)                      # the zone's other hostnames are not ours to commit
        self.assertNotIn("preload", listener.HSTS_VALUE)

    def _run(self, headers, method="GET", path="/", query=b""):
        import asyncio
        sent = []
        calls = []

        async def inner(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": method, "path": path, "query_string": query,
                 "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]}
        asyncio.run(listener.HttpsPolicy(inner)(scope, receive, send))
        return sent, calls

    def test_middleware_redirects_a_plain_http_visit_without_reaching_the_app(self):
        sent, calls = self._run({"cf-visitor": '{"scheme":"http"}'}, path="/topology", query=b"a=1")
        self.assertEqual(calls, [])
        self.assertEqual(sent[0]["status"], 301)
        self.assertIn((b"location", b"https://watchdog.corp.example/topology?a=1"), sent[0]["headers"])

    def test_middleware_adds_hsts_to_https_answers_once(self):
        sent, calls = self._run({"cf-visitor": '{"scheme":"https"}'})
        self.assertEqual(calls, ["/"])
        headers = sent[0]["headers"]
        self.assertEqual([v for k, v in headers if k == b"strict-transport-security"], [b"max-age=15552000"])
        self.assertIn((b"content-type", b"text/plain"), headers)                         # the app's own headers survive

    def test_middleware_adds_browser_security_headers_to_http_and_https_answers(self):
        sent, _calls = self._run({})
        names = {name for name, _value in sent[0]["headers"]}
        for expected in (b"x-content-type-options", b"x-frame-options", b"referrer-policy", b"permissions-policy", b"content-security-policy"):
            with self.subTest(header=expected):
                self.assertIn(expected, names)

    def test_middleware_leaves_lan_and_internal_requests_alone(self):
        sent, calls = self._run({})
        self.assertEqual(calls, ["/"])
        self.assertNotIn(b"strict-transport-security", [k for k, _ in sent[0]["headers"]])
        sent, calls = self._run({}, method="POST", path="/heartbeat")
        self.assertEqual(calls, ["/heartbeat"])

    def test_middleware_does_not_redirect_a_posted_heartbeat_even_over_http(self):
        sent, calls = self._run({"cf-visitor": '{"scheme":"http"}'}, method="POST", path="/heartbeat")
        self.assertEqual(calls, ["/heartbeat"])
        self.assertEqual(sent[0]["status"], 200)

    def test_middleware_passes_other_protocol_scopes_straight_through(self):
        import asyncio
        seen = []

        async def inner(scope, receive, send):
            seen.append(scope["type"])

        asyncio.run(listener.HttpsPolicy(inner)({"type": "lifespan"}, None, None))
        self.assertEqual(seen, ["lifespan"])


class PauseShiftTests(unittest.TestCase):
    """SITE-B's detector learned (2026-10-14) that Insta resumes a paused item where it stopped, so a bar drawn from the true start runs ahead
    by the pause. MAIN and BACKUP-SERVER are the same: Core counts the time each player spent quiet (a pause: no media reads) per item, takes it
    off the position, and freezes the bar while the pause lasts. The state travels inside the stored heartbeat (`_pause`), so it survives a restart."""

    T0 = datetime(2026, 10, 14, 18, 0, 0, tzinfo=timezone.utc)      # the item started here

    def instance(self, quiet_age=0.0, name="DUWEW NIV WUT. 4.mp4", feed="usa", started=None, flagged=True):
        item = {"feed": feed, "advance_alarm_confirmed": True, "advancing": not quiet_age, "advance_age_seconds": quiet_age,
                "now_playing": {"filename": name, "duration_s": 1800, "started_utc": (started or self.T0).isoformat()}}
        if quiet_age and flagged:
            item["pause_quiet"] = True
        return item

    def beat(self, previous, seconds_into_item, quiet_age=0.0, **kw):
        at = self.T0 + timedelta(seconds=seconds_into_item)
        payload = {"hostname": "MAIN", "timestamp": at.isoformat(), "instances": [self.instance(quiet_age, **kw)]}
        return listener._with_pause_marks(payload, previous), at

    def shown(self, payload, at, heard_ago=0.0):
        # "now" is the moment the listener builds the frame: heard_ago after the heartbeat was received
        source = {"last_seen_utc": at.isoformat()}
        return listener._now_playing(payload, payload["instances"][0], source, at + timedelta(seconds=heard_ago))

    def test_without_a_pause_nothing_is_taken_off(self):
        payload, at = self.beat(None, 600)
        self.assertEqual(payload["_pause"]["usa"]["closed_s"], 0)
        shown = self.shown(payload, at)
        self.assertAlmostEqual(shown["elapsed_s"], 600, delta=0.5)
        self.assertFalse(shown["paused"])

    def test_an_open_pause_freezes_the_position(self):
        payload, at = self.beat(None, 600, quiet_age=30)
        shown = self.shown(payload, at)
        self.assertTrue(shown["paused"])
        self.assertAlmostEqual(shown["elapsed_s"], 570, delta=0.5)         # 600 s in, 30 s of it paused
        later = self.shown(payload, at, heard_ago=8)                        # eight seconds later, no new heartbeat yet
        self.assertAlmostEqual(later["elapsed_s"], shown["elapsed_s"], delta=0.5)   # it did not creep forward

    def test_after_the_resume_the_pause_stays_off_the_position(self):
        paused, _ = self.beat(None, 600, quiet_age=35)
        resumed, at = self.beat(paused, 610, quiet_age=0)
        self.assertEqual(resumed["_pause"]["usa"]["closed_s"], 35)
        self.assertEqual(resumed["_pause"]["usa"]["open_s"], 0)
        shown = self.shown(resumed, at)
        self.assertFalse(shown["paused"])
        self.assertAlmostEqual(shown["elapsed_s"], 575, delta=0.5)         # 610 s of wall time minus the 35 s it stood still
        again = self.shown(resumed, at, heard_ago=10)
        self.assertAlmostEqual(again["elapsed_s"], 585, delta=0.5)         # and it runs on from there

    def test_two_pauses_in_one_item_add_up(self):
        a, _ = self.beat(None, 300, quiet_age=40)
        b, _ = self.beat(a, 320)
        c, _ = self.beat(b, 500, quiet_age=20)
        d, at = self.beat(c, 520)
        self.assertEqual(d["_pause"]["usa"]["closed_s"], 60)
        self.assertAlmostEqual(self.shown(d, at)["elapsed_s"], 460, delta=0.5)

    def test_a_short_quiet_spell_is_not_a_pause(self):
        payload, at = self.beat(None, 600, quiet_age=10)
        self.assertEqual(payload["_pause"]["usa"]["open_s"], 0)
        self.assertFalse(self.shown(payload, at)["paused"])

    def test_quiet_that_is_not_a_pause_or_stall_flag_is_not_counted(self):
        # an age with advancing true and no pause_quiet flag is not evidence of a pause
        item = self.instance(quiet_age=40, flagged=False)
        item["advancing"] = True
        payload = {"hostname": "MAIN", "timestamp": self.T0.isoformat(), "instances": [item]}
        self.assertEqual(listener._with_pause_marks(payload, None)["_pause"]["usa"]["open_s"], 0)

    def test_a_stalled_engine_also_stands_still(self):
        item = self.instance(quiet_age=45, flagged=False)             # advancing False (set by quiet_age), no pause_quiet flag
        payload = {"hostname": "MAIN", "timestamp": (self.T0 + timedelta(seconds=600)).isoformat(), "instances": [item]}
        marked = listener._with_pause_marks(payload, None)
        self.assertEqual(marked["_pause"]["usa"]["open_s"], 45)

    def test_a_new_item_starts_from_zero(self):
        a, _ = self.beat(None, 600, quiet_age=35)
        b, at = self.beat(a, 5, name="NEXT.mp4", started=self.T0 + timedelta(seconds=605))
        self.assertEqual((b["_pause"]["usa"]["closed_s"], b["_pause"]["usa"]["open_s"]), (0, 0))
        self.assertNotEqual(a["_pause"]["usa"]["item"], b["_pause"]["usa"]["item"])

    def test_each_feed_keeps_its_own_count(self):
        usa = self.instance(quiet_age=30, feed="usa")
        carib = self.instance(quiet_age=0, feed="caribbean", name="CARIB.mp4")
        payload = {"hostname": "MAIN", "timestamp": (self.T0 + timedelta(seconds=600)).isoformat(), "instances": [usa, carib]}
        marked = listener._with_pause_marks(payload, None)
        self.assertEqual(marked["_pause"]["usa"]["open_s"], 30)
        self.assertEqual(marked["_pause"]["caribbean"]["open_s"], 0)

    def test_bad_input_never_breaks_the_heartbeat(self):
        for payload in ({"hostname": "MAIN"}, {"hostname": "MAIN", "instances": "x"}, {"hostname": "MAIN", "instances": [None, 5, {}]},
                        {"hostname": "MAIN", "instances": [{"feed": "usa", "advance_age_seconds": float("inf"), "pause_quiet": True,
                                                              "now_playing": {"filename": "A", "started_utc": self.T0.isoformat()}}]}):
            with self.subTest(payload=payload):
                listener._with_pause_marks(dict(payload), {"_pause": "garbage"})

    def test_a_player_without_a_start_gets_no_marks(self):
        item = {"feed": "usa", "advance_age_seconds": 40, "pause_quiet": True, "now_playing": {"filename": "A.mp4"}}
        marked = listener._with_pause_marks({"hostname": "MAIN", "instances": [item]}, None)
        self.assertNotIn("usa", marked.get("_pause", {}))

    def test_the_marks_are_not_listed_in_mail_rows(self):
        rows = listener._humanize_json({"hostname": "MAIN", "_pause": {"usa": {"item": "x", "closed_s": 1, "open_s": 0}}, "instance_count": 2})
        self.assertNotIn("Pause", [label for label, _ in rows])
        self.assertIn(("Instance count", "2"), rows)


class SourceLevelPauseTests(unittest.TestCase):
    """SITE-B and SITE-A have no per-feed state here: SITE-B's detector moves the start forward itself, and Core only has to freeze the bar while
    a `playout_paused` event is open (a pause of 15 s or more is reported long before the 45 s amber ladder shows anything on the tile)."""

    NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=timezone.utc)

    def snapshot(self, events):
        endpoint = next(item for item in listener.PLAYOUT_ENDPOINTS if item["endpoint_id"] == "SITE-B")
        payload = {"hostname": "SITE-B", "expected_interval_seconds": 30, "timestamp": self.NOW.isoformat(), "player_running": True,
                   "current_item": "MUTAFANENE 7.mp4", "current_item_started_utc": (self.NOW - timedelta(seconds=1200)).isoformat(),
                   "current_item_duration_s": 7193.0}
        source = {"source_id": "SITE-B", "last_seen_utc": (self.NOW - timedelta(seconds=20)).isoformat(), "last_payload_json": listener.json.dumps(payload),
                  "state": "healthy", "state_changed_utc": self.NOW.isoformat(), "expected_interval_seconds": 30, "instance_alert": None,
                  "expected_instances": None, "latest_event": None}
        return listener._endpoint_snapshot(endpoint, source, events, self.NOW)["now_playing"]

    def pause_event(self, seconds_ago, phase="ongoing"):
        return {"source_id": "SITE-B", "event_type": "playout_paused", "severity": "warning",
                "timestamp_utc": (self.NOW - timedelta(seconds=seconds_ago)).isoformat(),
                "detail": listener.json.dumps({"phase": phase, "ongoing_s": 18})}

    def test_without_a_pause_the_position_includes_the_time_since_the_heartbeat(self):
        playing = self.snapshot([])
        self.assertFalse(playing["paused"])
        self.assertAlmostEqual(playing["elapsed_s"], 1220, delta=1)

    def test_an_open_pause_event_freezes_the_bar_at_the_heartbeat_position(self):
        playing = self.snapshot([self.pause_event(25)])
        self.assertTrue(playing["paused"])
        self.assertAlmostEqual(playing["elapsed_s"], 1200, delta=1)         # no +20 s for the time since the heartbeat

    def test_a_closed_pause_does_not_freeze(self):
        self.assertFalse(self.snapshot([self.pause_event(25, "end")])["paused"])

    def test_an_old_unrefreshed_pause_does_not_freeze(self):
        self.assertFalse(self.snapshot([self.pause_event(400)])["paused"])


class TimingSanityTests(unittest.TestCase):
    """SITE-A's detector labelled its local clock digits with a Z (EDT, so four hours early): the bar would have read 'finished' for ever. A start
    that cannot be true for a FRESH heartbeat is ignored (the bar then says it has no length) instead of drawing nonsense."""

    NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=timezone.utc)

    def shown(self, started_offset_s, duration=1800, heard_ago=5):
        payload = {"timestamp": self.NOW.isoformat(), "current_item": "A.mp4", "current_item_duration_s": duration,
                   "current_item_started_utc": (self.NOW + timedelta(seconds=started_offset_s)).isoformat()}
        source = {"last_seen_utc": (self.NOW - timedelta(seconds=heard_ago)).isoformat()}
        return listener._now_playing(payload, None, source, self.NOW)

    def test_a_sensible_start_is_kept(self):
        self.assertAlmostEqual(self.shown(-600)["elapsed_s"], 605, delta=1)

    def test_a_start_that_no_whole_hour_explains_is_ignored(self):
        # three hours and 45 minutes early: no whole-hour shift puts it inside a 30-minute item (a start out by WHOLE hours is corrected, see MislabelledZoneTests)
        shown = self.shown(-3.75 * 3600)
        self.assertIsNone(shown["elapsed_s"])
        self.assertTrue(shown["timing_suspect"])

    def test_a_start_in_the_future_is_ignored(self):
        shown = self.shown(+1200)                              # twenty minutes ahead of the heartbeat: not a whole hour, not believable
        self.assertIsNone(shown["elapsed_s"])
        self.assertTrue(shown["timing_suspect"])

    def test_a_small_overrun_is_still_believed(self):
        self.assertAlmostEqual(self.shown(-1900)["elapsed_s"], 1905, delta=1)       # 100 s over: playlists do overrun

    def test_an_old_heartbeat_keeps_its_finished_logic(self):
        shown = self.shown(-4 * 3600, heard_ago=400)       # not fresh: this is the "item has finished" case, not a wrong clock
        self.assertGreater(shown["elapsed_s"], 4 * 3600)
        self.assertFalse(shown["timing_suspect"])


class PausedBarTests(unittest.TestCase):
    """The page side of the pause work: a paused item freezes its bar and says so; a start that cannot be true is said, not drawn."""

    def test_the_page_freezes_a_paused_bar_and_says_so(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        for needle in ('paused: nowPlaying.paused === true', 'timing_suspect: nowPlaying.timing_suspect === true', 'var paused = !!nowPlaying.paused;', '(paused ? 0 : sinceFrame)', '"Paused " + text', "is-paused", "Start time looks wrong"):
            with self.subTest(needle=needle):
                self.assertIn(needle, page)


class LagosClockTests(unittest.TestCase):
    """the owner asked for Lagos time (WAT) on the dashboard, 2026-10-16, for the operations team."""

    def test_the_header_has_a_lagos_chip_read_off_cores_clock(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        for needle in ('id="chip-lagos"', 'id="clock-lagos"', ">Lagos<", 'var LAGOS_ZONE = "Africa/Lagos";', "function lagosText(", '" WAT"',
                       "clockLagos.textContent = lagosText(core);", '"Lagos, last known"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, page)

    def test_it_is_on_the_topology_page_too(self):
        self.assertIn('id="clock-lagos"', listener.render_topology_html(TEST_TOKEN))


class PlainHttpSenderTests(unittest.TestCase):
    """The weakest remaining point: a site that posts its heartbeats to an http:// address sends the access key in the clear. The listener cannot
    know a sender's configuration, but Cloudflare tells it how each request arrived, so it now records which sources posted over plain http (shown in
    /status and logged) before anyone decides to refuse those posts."""

    class FakeRequest:
        def __init__(self, headers):
            self.headers = headers

    def setUp(self):
        listener.PLAIN_HTTP_POSTS.clear()

    def test_a_post_that_arrived_over_plain_http_is_recorded_with_its_source(self):
        listener._note_plain_http(self.FakeRequest({"cf-visitor": '{"scheme":"http"}'}), "SITE-A")
        self.assertIn("SITE-A", listener._plain_http_senders(datetime.now(timezone.utc)))

    def test_https_lan_and_unknown_posts_are_not_recorded(self):
        for headers in ({"cf-visitor": '{"scheme":"https"}'}, {}, {"cf-visitor": "garbage"}):
            listener._note_plain_http(self.FakeRequest(headers), "MAIN")
        self.assertEqual(listener._plain_http_senders(datetime.now(timezone.utc)), {})

    def test_an_old_record_ages_out(self):
        listener._note_plain_http(self.FakeRequest({"x-forwarded-proto": "http"}), "SITE-B")
        later = datetime.now(timezone.utc) + timedelta(seconds=listener.PLAIN_HTTP_WINDOW_SECONDS + 5)
        self.assertEqual(listener._plain_http_senders(later), {})

    def test_the_optional_rejection_switch_blocks_direct_http_posts(self):
        class DirectHttpRequest:
            headers = {}
            scope = {"scheme": "http"}

        saved = listener.REJECT_PLAIN_HTTP_POSTS
        listener.REJECT_PLAIN_HTTP_POSTS = True
        try:
            with self.assertRaises(listener.HTTPException) as caught:
                listener._note_plain_http(DirectHttpRequest(), "MAIN")
            self.assertEqual(caught.exception.status_code, 426)
        finally:
            listener.REJECT_PLAIN_HTTP_POSTS = saved

    def test_a_missing_source_or_odd_headers_never_break_a_heartbeat(self):
        listener._note_plain_http(self.FakeRequest({"cf-visitor": '{"scheme":"http"}'}), None)
        listener._note_plain_http(object(), "X")
        self.assertEqual(listener._plain_http_senders(datetime.now(timezone.utc)), {})

    def test_the_record_is_bounded(self):
        for index in range(listener.PLAIN_HTTP_MAX_SOURCES + 50):
            listener._note_plain_http(self.FakeRequest({"cf-visitor": '{"scheme":"http"}'}), f"S{index}")
        self.assertLessEqual(len(listener.PLAIN_HTTP_POSTS), listener.PLAIN_HTTP_MAX_SOURCES)

    def test_status_reports_it(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn('"plain_http_senders": _plain_http_senders(now_dt)', source)
        self.assertEqual(len(re.findall(r"^    _note_plain_http\(request, source_id\)$", source, re.M)), 2)      # the calls in /heartbeat and /event (not the def line)


class DeadAirFloorTests(unittest.TestCase):
    """SITE-A's detector v1.3.0 was installed with its default 5 s dead-air limit and, at 06:46 New York time on 2026-10-15, raised a critical `off_air`
    for a 7.4 s black-and-silent moment (a programme change looks exactly like that). Each one would be a RED and an e-mail. The listener therefore
    does not turn a source red for dead air (cause `black_freeze_silence`) until it has lasted 20 s, whatever limit the detector uses; an input
    that is gone (any other cause) is red at once."""

    NOW = datetime(2026, 10, 15, 10, 46, 30, tzinfo=timezone.utc)

    def status(self, detail, seconds_ago=2):
        event = {"source_id": "SITE-A", "event_type": "off_air", "severity": "critical",
                 "timestamp_utc": (self.NOW - timedelta(seconds=seconds_ago)).isoformat(), "detail": listener.json.dumps(detail)}
        return listener._event_status(event, self.NOW)

    DEAD_AIR = {"phase": "ongoing", "start_t": 2817.02, "ongoing_s": 5.2, "limit_s": 5, "cause": "black_freeze_silence"}   # the real 06:46 event

    def test_the_real_short_dead_air_event_is_amber_and_sends_no_email(self):
        status = self.status(self.DEAD_AIR)
        self.assertEqual(status["level"], "warning")
        self.assertFalse(status["email_eligible"])
        self.assertIn("Dead Air", status["root_cause"])

    def test_dead_air_that_goes_on_turns_red_once_it_has_lasted_twenty_seconds(self):
        self.assertEqual(self.status(self.DEAD_AIR, seconds_ago=10)["level"], "warning")          # 5.2 + 10 = 15 s
        status = self.status(self.DEAD_AIR, seconds_ago=16)                                         # 5.2 + 16 = 21 s
        self.assertEqual(status["level"], "critical")
        self.assertEqual(status["root_cause"], "Off-Air Status")

    def test_a_detector_set_to_twenty_seconds_is_red_at_once(self):
        detail = dict(self.DEAD_AIR, ongoing_s=20.4, limit_s=20)
        self.assertEqual(self.status(detail)["level"], "critical")

    def test_a_lost_input_is_red_at_once_whatever_its_length(self):
        for cause in ("input_lost", "no_input", "decoder_stopped", ""):
            with self.subTest(cause=cause):
                detail = {"phase": "ongoing", "ongoing_s": 5.0, "limit_s": 5, "cause": cause}
                self.assertEqual(self.status(detail)["level"], "critical")

    def test_a_closed_dead_air_event_is_nothing(self):
        self.assertIsNone(self.status(dict(self.DEAD_AIR, phase="end", duration_s=7.4)))

    def test_the_threshold_is_twenty_seconds(self):
        self.assertEqual(listener.DEAD_AIR_RED_SECONDS, 20)



class ViewTokenTests(unittest.TestCase):
    """The public dashboard page used to embed the SAME key the playout senders use to post heartbeats, so anyone who could open the page could read
    it and forge heartbeats or events (hiding a dead player, or raising false alarms). The page now carries a VIEW key that can only read; posting
    needs the sender key, which is never put in a page. The view key is derived from the sender key (HMAC, one way), so there is nothing new to store and
    it cannot be turned back into the sender key."""

    def test_the_view_key_is_derived_stable_and_different(self):
        self.assertNotEqual(listener.VIEW_TOKEN, listener.SHARED_TOKEN)
        self.assertRegex(listener.VIEW_TOKEN, r"^[0-9a-f]{64}$")
        import hashlib
        import hmac as hmac_module
        expected = hmac_module.new(listener.SHARED_TOKEN.encode("utf-8"), b"clarix-watchdog-view-v1", hashlib.sha256).hexdigest()
        self.assertEqual(listener.VIEW_TOKEN, expected)
        self.assertNotIn(listener.SHARED_TOKEN, listener.VIEW_TOKEN)

    def test_reading_accepts_either_key_and_nothing_else(self):
        self.assertTrue(listener._view_token_matches(listener.VIEW_TOKEN))
        self.assertTrue(listener._view_token_matches(listener.SHARED_TOKEN))      # senders, the deploy check and the weekly report read /status with theirs
        for wrong in (None, "", "nope", listener.VIEW_TOKEN[:-1], listener.VIEW_TOKEN + "0"):
            with self.subTest(wrong=wrong):
                self.assertFalse(listener._view_token_matches(wrong))

    def test_writing_does_not_accept_the_view_key(self):
        self.assertTrue(listener._token_matches(listener.SHARED_TOKEN))
        self.assertFalse(listener._token_matches(listener.VIEW_TOKEN))
        with self.assertRaises(listener.HTTPException):
            listener.require_token(listener.VIEW_TOKEN)
        listener.require_token(listener.SHARED_TOKEN)
        with self.assertRaises(listener.HTTPException):
            listener.require_view_token("nope")
        listener.require_view_token(listener.VIEW_TOKEN)

    def test_the_routes_use_the_right_key(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        for route in ("/heartbeat", "/event"):
            with self.subTest(route=route):
                self.assertIn(f'@app.post("{route}", dependencies=[Depends(require_token)])', source)
        # Resetting a held alarm is an operator action: since the per-sender keys it takes the admin (shared) key, never a sender's key.
        self.assertIn('@app.post("/alerts/reset", dependencies=[Depends(require_admin_token)])', source)
        self.assertIn('@app.get("/status", dependencies=[Depends(require_view_token)])', source)
        self.assertIn("if not _view_token_matches(token):", source)                  # /stream, which takes the key in the query string

    def test_no_page_is_given_the_sender_key(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn("render_dashboard_html(VIEW_TOKEN)", source)
        self.assertIn("render_topology_html(VIEW_TOKEN)", source)
        self.assertNotIn("render_dashboard_html(SHARED_TOKEN)", source)
        self.assertNotIn("render_topology_html(SHARED_TOKEN)", source)
        page = listener.render_dashboard_html(listener.VIEW_TOKEN)
        self.assertIn(listener.VIEW_TOKEN, page)
        self.assertNotIn(listener.SHARED_TOKEN, page)


class MislabelledZoneTests(unittest.TestCase):
    """SITE-A's detector v1.3.0 sends the log's LOCAL clock digits with a Z, so on EDT its starts are four hours early (five in winter) and the bar read
    "Start time looks wrong" until the detector is restarted with `-ItemStartedClock TrueUtc` on a PC that needs someone on site. For short items the
    listener can tell, and undo, a start that is out by whole hours: of all whole-hour shifts exactly one puts the position inside the item (an item
    under 55 minutes cannot straddle two candidates). The correction is reported (`timing_corrected_hours`) and never touches a start that is already
    believable; longer items are not guessed at."""

    NOW = datetime(2026, 10, 15, 19, 43, 40, tzinfo=timezone.utc)

    def shown(self, true_position_s, label_error_hours, duration=70.751, heard_ago=1.0):
        true_start = self.NOW - timedelta(seconds=true_position_s)
        labelled = true_start - timedelta(hours=label_error_hours)
        payload = {"timestamp": self.NOW.isoformat(), "current_item": "MuvuCafi M Weceh DA.mp4", "current_item_duration_s": duration,
                   "current_item_started_utc": labelled.isoformat()}
        source = {"last_seen_utc": (self.NOW - timedelta(seconds=heard_ago)).isoformat()}
        return listener._now_playing(payload, None, source, self.NOW)

    def test_a_start_four_hours_early_is_corrected(self):
        shown = self.shown(25.0, 4)                            # the real SITE-A case on EDT
        self.assertEqual(shown["timing_corrected_hours"], 4)
        self.assertFalse(shown["timing_suspect"])
        self.assertAlmostEqual(shown["elapsed_s"], 26.0, delta=1.0)          # 25 s in, heard 1 s ago
        self.assertEqual(shown["started_utc"], (self.NOW - timedelta(seconds=25)).isoformat())

    def test_winter_five_hours_early_is_corrected_too(self):
        shown = self.shown(40.0, 5)
        self.assertEqual(shown["timing_corrected_hours"], 5)
        self.assertAlmostEqual(shown["elapsed_s"], 41.0, delta=1.0)

    def test_a_sender_east_of_utc_is_corrected_the_other_way(self):
        shown = self.shown(10.0, -1)                           # Lagos clock digits labelled Z: the start looks an hour in the future
        self.assertEqual(shown["timing_corrected_hours"], -1)
        self.assertAlmostEqual(shown["elapsed_s"], 11.0, delta=1.0)

    def test_an_item_near_its_end_and_a_short_overrun_are_still_corrected(self):
        self.assertEqual(self.shown(70.0, 4)["timing_corrected_hours"], 4)
        self.assertEqual(self.shown(80.0, 4)["timing_corrected_hours"], 4)    # 9 s over its length: a late next item

    def test_a_believable_start_is_never_touched(self):
        shown = self.shown(25.0, 0)
        self.assertEqual(shown["timing_corrected_hours"], 0)
        self.assertAlmostEqual(shown["elapsed_s"], 26.0, delta=1.0)

    def test_long_items_are_not_guessed_at(self):
        shown = self.shown(1500.0, 4, duration=4000.0)         # more than 55 minutes: two shifts could fit, so none is applied
        self.assertEqual(shown["timing_corrected_hours"], 0)
        self.assertTrue(shown["timing_suspect"])
        self.assertIsNone(shown["elapsed_s"])

    def test_a_start_that_no_whole_hour_can_explain_is_still_suspect(self):
        payload = {"timestamp": self.NOW.isoformat(), "current_item": "A.mp4", "current_item_duration_s": 60.0,
                   "current_item_started_utc": (self.NOW - timedelta(hours=3, minutes=30)).isoformat()}
        shown = listener._now_playing(payload, None, {"last_seen_utc": (self.NOW - timedelta(seconds=1)).isoformat()}, self.NOW)
        self.assertEqual(shown["timing_corrected_hours"], 0)
        self.assertTrue(shown["timing_suspect"])

    def test_an_old_heartbeat_is_not_corrected(self):
        shown = self.shown(25.0, 4, heard_ago=400)             # not fresh: the "item finished" logic applies, no guessing
        self.assertEqual(shown["timing_corrected_hours"], 0)


class ProgressBarColourTests(unittest.TestCase):
    """the owner, 2026-10-16: the progress bar must be orange / yellow so it stands out (it was the tile's status colour, green when healthy)."""

    def test_the_bar_is_orange_to_yellow_not_the_status_colour(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertIn("background:linear-gradient(90deg, #ff8a00, #ffc400)", page)
        self.assertNotIn("rgba(var(--tone),.5), rgb(var(--tone))); transition:width 1s linear", page)
        self.assertIn(".now-bar { position:relative; flex:1; height:7px;", page)            # thicker than the old 4 px hairline

    def test_a_paused_bar_is_still_told_apart(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        self.assertIn(".now-progress.is-paused .now-bar i { background:repeating-linear-gradient(135deg, #ffd23f", page)


class EstimatedItemTests(unittest.TestCase):
    """MAIN's Caribbean channel plays Insta's FileBlock schedule from midnight; its sender works the item out from the clock and marks it `estimated`.
    The listener passes that on, and never applies pause arithmetic to it (a schedule is wall-clock, not a player position)."""

    NOW = datetime(2026, 10, 15, 6, 0, 0, tzinfo=timezone.utc)

    def payload(self, estimated):
        started = (self.NOW - timedelta(seconds=600)).isoformat()
        np = {"filename": "DARIMA'G DEKUWAL CEDU 2.mp4", "result": "Block schedule (estimated)", "duration_s": 4235.2, "started_utc": started}
        if estimated:
            np["estimated"] = True
        return {"hostname": "MAIN", "timestamp": self.NOW.isoformat(), "instances": [{"feed": "caribbean", "advance_age_seconds": 40, "pause_quiet": True,
                                                                                         "advance_alarm_confirmed": True, "now_playing": np}]}

    def shown(self, payload):
        payload = listener._with_pause_marks(payload, None)
        return listener._now_playing(payload, payload["instances"][0], {"last_seen_utc": self.NOW.isoformat()}, self.NOW), payload

    def test_the_estimated_flag_reaches_the_dashboard_data(self):
        shown, _ = self.shown(self.payload(True))
        self.assertTrue(shown["estimated"])
        self.assertEqual(shown["title"], "DARIMA'G DEKUWAL CEDU 2.mp4")
        self.assertAlmostEqual(shown["elapsed_s"], 600, delta=1)

    def test_an_ordinary_item_is_not_estimated(self):
        shown, _ = self.shown(self.payload(False))
        self.assertFalse(shown["estimated"])

    def test_no_pause_arithmetic_is_applied_to_an_estimate(self):
        shown, payload = self.shown(self.payload(True))
        self.assertNotIn("caribbean", payload.get("_pause", {}))
        self.assertFalse(shown["paused"])
        self.assertAlmostEqual(shown["elapsed_s"], 600, delta=1)      # not 600 minus a 40 s "pause", and not frozen

    def test_the_page_marks_an_estimate_with_a_tilde(self):
        page = listener.render_dashboard_html(TEST_TOKEN)
        for needle in ("estimated: nowPlaying.estimated === true", 'text = "~" + text', "is-estimated"):
            with self.subTest(needle=needle):
                self.assertIn(needle, page)



class CspCoversWhatThePageDoesTests(unittest.TestCase):
    """The hardening commit added a Content-Security-Policy with `connect-src 'self'`. The dashboard and the offline screen ask two public addresses whether
    THIS DEVICE has internet (that is how they tell "your internet is down" from "Core is down"); a policy that blocks those requests makes every
    outage read "no internet connection", the exact misdiagnosis fixed on 2026-10-12. Caught by the real-Chrome suites, not by the unit tests, so it is
    pinned here: every address the pages probe must be allowed by the policy."""

    @staticmethod
    def policy():
        for name, value in listener.SECURITY_HEADERS:
            if name == b"content-security-policy":
                return value.decode("latin-1")
        raise AssertionError("no content-security-policy header")

    @staticmethod
    def directive(policy, name):
        for part in policy.split(";"):
            tokens = part.split()
            if tokens and tokens[0] == name:
                return tokens[1:]
        return []

    def test_every_probe_address_is_allowed_to_connect(self):
        from urllib.parse import urlparse
        allowed = self.directive(self.policy(), "connect-src")
        pages = [listener.render_dashboard_html(TEST_TOKEN), listener.render_topology_html(TEST_TOKEN)]
        import app.dashboard as dashboard_module
        pages.append(dashboard_module.OFFLINE_HTML)
        probed = set()
        for page in pages:
            for block in re.findall(r"var PROBES = \[([^\]]*)\]", page):
                probed.update(re.findall(r'"(https://[^"]+)"', block))
        self.assertGreaterEqual(len(probed), 2)
        for url in probed:
            origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
            with self.subTest(url=url):
                self.assertIn(origin, allowed)

    def test_the_policy_is_not_wide_open(self):
        policy = self.policy()
        for directive in ("default-src", "connect-src", "script-src", "frame-ancestors", "object-src", "base-uri"):
            with self.subTest(directive=directive):
                tokens = self.directive(policy, directive)
                self.assertTrue(tokens, directive)
                self.assertNotIn("*", tokens)
        self.assertEqual(self.directive(policy, "frame-ancestors"), ["'none'"])
        self.assertEqual(self.directive(policy, "connect-src")[0], "'self'")


class RejectPlainHttpModeTests(unittest.TestCase):
    """Refusing plain-http posts has two strengths. `internet` refuses only posts that came in through Cloudflare over http (the sender key would cross the
    internet unencrypted); no sender does that, so it is safe to switch on. `true` also refuses the LAN path (MAIN and BACKUP-SERVER post to
    http://192.168.7.183:8420): do not use it until those two have an encrypted path, or both go heartbeat-lost."""

    class Req:
        def __init__(self, headers, scheme="http"):
            self.headers = headers
            self.scope = {"scheme": scheme}

    def run_with(self, internet, everything, request):
        saved = (listener.REJECT_PLAIN_HTTP_INTERNET, listener.REJECT_PLAIN_HTTP_POSTS)
        listener.REJECT_PLAIN_HTTP_INTERNET, listener.REJECT_PLAIN_HTTP_POSTS = internet, everything
        try:
            listener._note_plain_http(request, "X")
            return "accepted"
        except listener.HTTPException as refusal:
            return refusal.status_code
        finally:
            listener.REJECT_PLAIN_HTTP_INTERNET, listener.REJECT_PLAIN_HTTP_POSTS = saved

    def test_off_accepts_everything(self):
        self.assertEqual(self.run_with(False, False, self.Req({"cf-visitor": '{"scheme":"http"}'})), "accepted")
        self.assertEqual(self.run_with(False, False, self.Req({})), "accepted")

    def test_internet_mode_refuses_plain_http_through_cloudflare_only(self):
        self.assertEqual(self.run_with(True, False, self.Req({"cf-visitor": '{"scheme":"http"}'})), 426)
        self.assertEqual(self.run_with(True, False, self.Req({})), "accepted")                       # the LAN path: untouched
        self.assertEqual(self.run_with(True, False, self.Req({"cf-visitor": '{"scheme":"https"}'}, "http")), "accepted")

    def test_all_mode_refuses_the_lan_path_too(self):
        self.assertEqual(self.run_with(True, True, self.Req({})), 426)
        self.assertEqual(self.run_with(True, True, self.Req({"cf-visitor": '{"scheme":"https"}'}, "http")), "accepted")

    def test_the_setting_values(self):
        self.assertEqual(listener._reject_modes("internet"), (True, False))
        self.assertEqual(listener._reject_modes("true"), (True, True))
        self.assertEqual(listener._reject_modes("ALL"), (True, True))
        for off in ("", "false", "0", "no", "off", "nonsense"):
            with self.subTest(off=off):
                self.assertEqual(listener._reject_modes(off), (False, False))


class KeyRotationTests(unittest.TestCase):
    """Changing the sender key must not blind a single open dashboard or drop a single sender. The page's view key is derived from the sender key, so a
    new sender key would otherwise change every open page's key at once. During the window (old key kept in WATCHDOG_PREVIOUS_SHARED_TOKEN) the listener
    accepts the view key of BOTH sender keys and says which sender is still on the old one; after the window an open page asks for its new key by itself."""

    OLD = "old-sender-key-0123456789"
    NEW = "new-sender-key-9876543210"

    class FakeRequest:
        def __init__(self, token):
            self.headers = {} if token is None else {"x-watchdog-token": token}

    def setUp(self):
        self.saved = (listener.SHARED_TOKEN, listener.PREVIOUS_SHARED_TOKEN, listener.WRITE_TOKENS, listener.VIEW_TOKENS, listener.VIEW_TOKEN)
        listener.KEY_GENERATION.clear()

    def tearDown(self):
        (listener.SHARED_TOKEN, listener.PREVIOUS_SHARED_TOKEN, listener.WRITE_TOKENS, listener.VIEW_TOKENS, listener.VIEW_TOKEN) = self.saved
        listener.KEY_GENERATION.clear()

    def rotate(self, current, previous=""):
        listener.SHARED_TOKEN = current
        listener.PREVIOUS_SHARED_TOKEN = previous
        listener.WRITE_TOKENS = tuple(dict.fromkeys(token for token in (current, previous) if token))
        listener.VIEW_TOKENS = listener._view_tokens(listener.WRITE_TOKENS)
        listener.VIEW_TOKEN = listener.VIEW_TOKENS[0]

    def old_view_key(self):
        return listener._view_tokens((self.OLD,))[0]

    def new_view_key(self):
        return listener._view_tokens((self.NEW,))[0]

    def test_there_is_one_view_key_per_sender_key_and_the_current_one_comes_first(self):
        keys = listener._view_tokens((self.NEW, self.OLD))
        self.assertEqual(keys, (self.new_view_key(), self.old_view_key()))
        self.assertEqual(len(set(keys)), 2)
        for key in keys:
            self.assertRegex(key, r"^[0-9a-f]{64}$")
            self.assertNotIn(self.NEW, key)
            self.assertNotIn(self.OLD, key)
        self.assertEqual(listener._view_tokens((self.NEW,)), (self.new_view_key(),))

    def test_during_the_window_a_page_with_either_view_key_keeps_reading(self):
        self.rotate(self.NEW, self.OLD)
        self.assertTrue(listener._view_token_matches(self.new_view_key()))
        self.assertTrue(listener._view_token_matches(self.old_view_key()))
        self.assertEqual(listener.VIEW_TOKEN, self.new_view_key())              # a page loaded now is handed the NEW one

    def test_after_the_window_the_old_page_key_is_refused_and_the_new_one_works(self):
        self.rotate(self.NEW)
        self.assertFalse(listener._view_token_matches(self.old_view_key()))
        self.assertTrue(listener._view_token_matches(self.new_view_key()))
        self.assertFalse(listener._view_token_matches(self.OLD))                 # the old SENDER key reads nothing either

    def test_no_view_key_ever_writes(self):
        self.rotate(self.NEW, self.OLD)
        for key in (self.new_view_key(), self.old_view_key()):
            with self.subTest(key=key[:8]):
                self.assertFalse(listener._token_matches(key))
                with self.assertRaises(listener.HTTPException):
                    listener.require_token(key)

    def test_the_old_sender_key_writes_only_during_the_window(self):
        self.rotate(self.NEW, self.OLD)
        self.assertTrue(listener._token_matches(self.OLD))
        self.assertTrue(listener._token_matches(self.NEW))
        self.rotate(self.NEW)
        self.assertFalse(listener._token_matches(self.OLD))
        self.assertTrue(listener._token_matches(self.NEW))

    def test_the_key_a_sender_used_is_named(self):
        self.rotate(self.NEW, self.OLD)
        self.assertEqual(listener._key_generation(self.NEW), "current")
        self.assertEqual(listener._key_generation(self.OLD), "previous")
        for other in (None, "", "nope", self.new_view_key()):
            with self.subTest(other=other):
                self.assertIsNone(listener._key_generation(other))

    def test_each_sender_is_listed_under_the_key_it_last_used(self):
        self.rotate(self.NEW, self.OLD)
        listener._note_key_generation(self.FakeRequest(self.OLD), "MAIN")
        listener._note_key_generation(self.FakeRequest(self.NEW), "BACKUP-SERVER")
        now = datetime.now(timezone.utc)
        self.assertEqual(listener._sender_keys(now), {"previous": ["MAIN"], "current": ["BACKUP-SERVER"]})
        listener._note_key_generation(self.FakeRequest(self.NEW), "MAIN")         # MAIN has been moved over
        self.assertEqual(listener._sender_keys(now), {"previous": [], "current": ["BACKUP-SERVER", "MAIN"]})

    def test_an_old_note_ages_out_and_odd_requests_never_break_a_post(self):
        self.rotate(self.NEW, self.OLD)
        listener._note_key_generation(self.FakeRequest(self.OLD), "SITE-B")
        later = datetime.now(timezone.utc) + timedelta(seconds=listener.KEY_GENERATION_WINDOW_SECONDS + 5)
        self.assertEqual(listener._sender_keys(later), {"previous": [], "current": []})
        for request, source in ((object(), "X"), (self.FakeRequest(None), "X"), (self.FakeRequest(self.NEW), None), (self.FakeRequest("nope"), "X")):
            listener._note_key_generation(request, source)                         # must not raise

    def test_status_reports_it_and_the_write_routes_record_it(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn('"sender_keys": _sender_keys(now_dt)', source)
        self.assertIn('"key_window_open": bool(PREVIOUS_SHARED_TOKEN)', source)
        self.assertGreaterEqual(source.count("_note_key_generation(request, source_id)"), 2)   # heartbeat and event

    def test_an_open_page_fetches_its_new_key_without_reloading(self):
        # A reload would lose the browser's permission to play the siren on an unattended screen, so the page asks for the new key quietly.
        source = Path(listener.__file__).with_name("dashboard.py").read_text(encoding="utf-8")
        body = re.search(r"function renewViewKey\(\) \{.*?\n  \}\n", source, re.S)
        self.assertIsNotNone(body, "the page needs renewViewKey()")
        self.assertNotIn("reload", body.group(0))
        self.assertIn("renewViewKey()", source.split("eventSource.onerror")[1][:900])    # a stream the listener closed for good triggers it
        pattern = re.search(r'var token = "\(\[0-9a-f\]\{64\}\)";', source)
        self.assertIsNotNone(pattern, "the page must read the key from the exact line the server writes")
        page = listener.render_dashboard_html(listener.VIEW_TOKEN)
        self.assertRegex(page, r'var token = "[0-9a-f]{64}";')


class LongItemHourShiftTests(unittest.TestCase):
    """SITE-A showed no progress bar on a 75 minute documentary (2026-10-17): its start is the local clock digits labelled Z, four hours early. The
    correction in MislabelledZoneTests only handles items under 55 minutes, because for a longer one two whole-hour shifts can both fit (here 3 and 4
    hours) and it cannot tell which is right. The listener now REMEMBERS the shift each sender was last found to use, from the items where it is
    certain (stored with the heartbeat, so it survives a restart), and applies it to the long ones if - and only if - the result is believable.
    Until a short item has been seen, SITE-A is assumed to be four hours out. A sender that has been put right is left alone."""

    NOW = datetime(2026, 10, 17, 14, 37, 28, tzinfo=timezone.utc)

    def shown(self, true_position_s, label_error_hours, duration=4502.4, source_id="SITE-A", hint=None):
        labelled = self.NOW - timedelta(seconds=true_position_s) - timedelta(hours=label_error_hours)
        payload = {"timestamp": self.NOW.isoformat(), "current_item": "Documentry .mp4", "current_item_duration_s": duration,
                   "current_item_started_utc": labelled.isoformat()}
        if hint is not None:
            payload["_hour_hint"] = hint
        source = {"source_id": source_id, "last_seen_utc": (self.NOW - timedelta(seconds=1)).isoformat()}
        return listener._now_playing(payload, None, source, self.NOW)

    def test_the_real_documentary_case_is_corrected_with_the_learned_shift(self):
        shown = self.shown(940.0, 4, hint=4)
        self.assertEqual(shown["timing_corrected_hours"], 4)
        self.assertFalse(shown["timing_suspect"])
        self.assertAlmostEqual(shown["elapsed_s"], 941.0, delta=1.0)

    def test_site_a_is_assumed_four_hours_out_until_a_short_item_shows_otherwise(self):
        shown = self.shown(940.0, 4)
        self.assertEqual(shown["timing_corrected_hours"], 4)
        self.assertAlmostEqual(shown["elapsed_s"], 941.0, delta=1.0)

    def test_another_sender_gets_no_guess(self):
        shown = self.shown(940.0, 4, source_id="SITE-B")
        self.assertEqual(shown["timing_corrected_hours"], 0)
        self.assertTrue(shown["timing_suspect"])
        self.assertIsNone(shown["elapsed_s"])

    def test_a_learned_zero_means_the_sender_is_right_and_beats_the_assumption(self):
        shown = self.shown(940.0, 4, hint=0)
        self.assertEqual(shown["timing_corrected_hours"], 0)
        self.assertTrue(shown["timing_suspect"])

    def test_a_shift_that_does_not_make_the_start_believable_is_not_applied(self):
        shown = self.shown(940.0, 4, hint=2)               # 2 h would leave it 2 h 15 min past the end
        self.assertEqual(shown["timing_corrected_hours"], 0)
        self.assertTrue(shown["timing_suspect"])

    def test_a_believable_long_item_is_never_shifted(self):
        shown = self.shown(1500.0, 0, hint=4)
        self.assertEqual(shown["timing_corrected_hours"], 0)
        self.assertFalse(shown["timing_suspect"])
        self.assertAlmostEqual(shown["elapsed_s"], 1501.0, delta=1.0)

    def payload_for(self, true_position_s, label_error_hours, duration):
        labelled = self.NOW - timedelta(seconds=true_position_s) - timedelta(hours=label_error_hours)
        return {"hostname": "SITE-A", "timestamp": self.NOW.isoformat(), "current_item": "X.mp4", "current_item_duration_s": duration,
                "current_item_started_utc": labelled.isoformat()}

    def test_a_short_item_teaches_the_shift(self):
        stored = listener._with_hour_hint(self.payload_for(25.0, 4, 70.0), None)
        self.assertEqual(stored["_hour_hint"], 4)
        winter = listener._with_hour_hint(self.payload_for(25.0, 5, 70.0), stored)
        self.assertEqual(winter["_hour_hint"], 5)                    # the clocks went back: the next short item corrects the memory

    def test_a_short_item_that_fits_as_reported_says_the_sender_has_been_put_right(self):
        stored = listener._with_hour_hint(self.payload_for(25.0, 0, 70.0), {"_hour_hint": 4})
        self.assertEqual(stored["_hour_hint"], 0)

    def test_a_long_item_carries_the_memory_forward_and_changes_nothing(self):
        stored = listener._with_hour_hint(self.payload_for(940.0, 4, 4502.4), {"_hour_hint": 4})
        self.assertEqual(stored["_hour_hint"], 4)
        self.assertNotIn("_hour_hint", listener._with_hour_hint(self.payload_for(940.0, 4, 4502.4), None))

    def test_a_short_item_no_whole_hour_can_explain_leaves_the_memory_alone(self):
        odd = self.payload_for(25.0, 0, 60.0)
        odd["current_item_started_utc"] = (self.NOW - timedelta(hours=3, minutes=30)).isoformat()
        self.assertEqual(listener._with_hour_hint(odd, {"_hour_hint": 4})["_hour_hint"], 4)

    def test_odd_input_never_breaks_a_heartbeat(self):
        for payload, previous in (({}, None), ({"timestamp": "garbage"}, {"_hour_hint": "x"}), ({"current_item_duration_s": "long"}, [])):
            with self.subTest(payload=payload):
                listener._with_hour_hint(dict(payload), previous)    # must not raise

    def test_the_heartbeat_route_stores_it(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn("payload = _with_hour_hint(payload,", source)


class CriticalWordingTests(unittest.TestCase):
    """the owner asked (2026-10-18) for every RED alarm to be called CRITICAL. The status codes already say "critical"; the words people read were the
    counters on the Monitor page. The data keys (`red_alarms_24h`, `since_last_red_s`) are part of the API and stay as they are."""

    NEW_WORDING = ("Since last critical alarm", "Critical alarms, 24 h", "Emails are sent on critical only", "No critical alarm on record")
    OLD_WORDING = ("Since last red alarm", "Red alarms, 24 h", "Emails are sent on red only", "No red alarm on record")

    def pages(self):
        return {"dashboard": listener.render_dashboard_html(TEST_TOKEN), "topology": listener.render_topology_html(TEST_TOKEN)}

    def test_the_counters_say_critical(self):
        page = self.pages()["dashboard"]
        for phrase in self.NEW_WORDING:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, page)

    def test_the_old_red_wording_is_gone(self):
        for name, page in self.pages().items():
            for phrase in self.OLD_WORDING:
                with self.subTest(page=name, phrase=phrase):
                    self.assertNotIn(phrase, page)

    def test_no_visible_word_red_is_left_in_either_page(self):
        # Colour names inside the style sheet (--red, var(--red-rgb)) and in comments are fine; a person must not read "red" anywhere else.
        for name, page in self.pages().items():
            text = re.sub(r"<style.*?</style>", "", page, flags=re.S)
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            text = re.sub(r"(?m)^\s*//.*$", "", text)
            text = re.sub(r"(?m)\s//\s.*$", "", text)
            hits = re.findall(r"(?i)[^\n]{0,40}(?<![-\w])red(?![-\w])[^\n]{0,40}", text)
            with self.subTest(page=name):
                self.assertEqual(hits, [])

    def test_the_api_keys_are_unchanged(self):
        counters = listener.fleet_counters.__module__          # the module that owns them is imported with the listener
        self.assertTrue(counters)
        page = self.pages()["dashboard"]
        self.assertIn("counters.red_alarms_24h", page)         # the page still reads the same data key
        self.assertIn('id="counter-red24"', page)


class StartupGraceTests(unittest.TestCase):
    """When the listener itself has been down (a reboot of Core), every source's last heartbeat is minutes old the moment it comes back, and the checker
    would raise a "heartbeat lost" critical alarm for each source (an e-mail and a phone push apiece) before any sender has had a chance to report in.
    For the first seconds after the listener starts, the checker waits."""

    START = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)

    def test_the_checker_waits_inside_the_grace_period_and_not_after(self):
        self.assertTrue(listener._in_startup_grace(self.START + timedelta(seconds=1), self.START, 90))
        self.assertTrue(listener._in_startup_grace(self.START + timedelta(seconds=89), self.START, 90))
        self.assertFalse(listener._in_startup_grace(self.START + timedelta(seconds=90), self.START, 90))
        self.assertFalse(listener._in_startup_grace(self.START + timedelta(seconds=600), self.START, 90))

    def test_a_grace_of_zero_switches_it_off(self):
        self.assertFalse(listener._in_startup_grace(self.START + timedelta(seconds=1), self.START, 0))

    def test_the_setting_has_a_sensible_default_and_limits(self):
        self.assertEqual(listener.LISTENER_STARTUP_GRACE_SECONDS, 90)          # nothing in the test environment sets it
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn('_env_int("WATCHDOG_LISTENER_STARTUP_GRACE_SECONDS", 90, 0, 600)', source)

    def test_the_checker_loop_and_the_compose_file_use_it(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        loop = source[source.index("async def staleness_checker_loop"):source.index("async def system_sampler_loop")]
        self.assertIn("_in_startup_grace(now, STARTED_AT, LISTENER_STARTUP_GRACE_SECONDS)", loop)
        compose = Path(listener.__file__).resolve().parent.parent.joinpath("docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("WATCHDOG_LISTENER_STARTUP_GRACE_SECONDS", compose)


class RouteKindTests(unittest.TestCase):
    """an earlier review item asked "did a sender's key cross the open internet in cleartext?". `plain_http_senders` cannot answer that (it records the
    application scheme, so a LAN post and a Tailscale post count too). The route can: it is read from the address the SENDER DIALLED (the Host header)
    and from the headers only Cloudflare adds. The peer address is not used, because Docker's port proxy rewrites it to the bridge gateway."""

    def kind(self, host=None, **cloudflare):
        headers = {} if host is None else {"host": host}
        headers.update({name.replace("_", "-"): value for name, value in cloudflare.items()})
        return guard.route_kind(headers, "watchdog.corp.example")

    def test_private_addresses_are_the_lan(self):
        for host in ("192.168.7.183:8420", "10.1.2.3", "172.16.0.9:8420", "172.31.255.1:8420", "169.254.10.10:8420", "[fd12:3456::1]:8420"):
            with self.subTest(host=host):
                self.assertEqual(self.kind(host), "lan")

    def test_tailscale_addresses_and_names_are_tailscale(self):
        for host in ("100.64.0.10:8420", "100.64.0.1:8420", "100.127.255.254", "[fd7a:115c:a1e0::1]:8420", "core.tail1a2b3.ts.net:8420"):
            with self.subTest(host=host):
                self.assertEqual(self.kind(host), "tailscale")

    def test_the_edges_of_the_tailscale_range_are_exact(self):
        self.assertEqual(self.kind("100.63.255.255:8420"), "public-ip")      # just below 100.64.0.0/10
        self.assertEqual(self.kind("100.128.0.1:8420"), "public-ip")         # just above 100.127.255.255
        self.assertEqual(self.kind("172.32.0.1:8420"), "public-ip")          # just above 172.16.0.0/12

    def test_a_public_address_is_the_open_internet(self):
        for host in ("107.173.135.41:8420", "8.8.8.8", "[2606:4700:4700::1111]:8420"):
            with self.subTest(host=host):
                self.assertEqual(self.kind(host), "public-ip")

    def test_cloudflare_headers_mean_cloudflare_whatever_the_host_says(self):
        self.assertEqual(self.kind("watchdog.corp.example", cf_ray="8a1b2c-JFK"), "cloudflare")
        self.assertEqual(self.kind("clarix-watchdog-listener:8420", cf_connecting_ip="203.0.113.7"), "cloudflare")   # the tunnel may rewrite Host
        self.assertEqual(self.kind(None, cf_visitor='{"scheme":"https"}'), "cloudflare")

    def test_the_public_name_without_cloudflare_headers_is_not_trusted_as_cloudflare(self):
        # Someone talking to the origin directly with the public name in the Host header (or a hosts-file override): not the tunnel.
        self.assertEqual(self.kind("watchdog.corp.example"), "other")

    def test_anything_unrecognised_is_other(self):
        for host in (None, "", "   ", "core", "localhost:8420", "127.0.0.1:8420", "[::1]:8420", "0.0.0.0", "evil.example.com", "[broken", "999.1.1.1"):
            with self.subTest(host=host):
                self.assertEqual(self.kind(host), "other")

    def test_a_forged_cloudflare_header_with_no_value_does_not_count(self):
        self.assertEqual(self.kind("192.168.7.183:8420", cf_ray=""), "lan")

    def test_only_the_five_documented_kinds_are_ever_returned(self):
        for host in ("192.168.7.1", "100.100.100.100", "8.8.8.8", "", "x" * 5000, "éè.example", "a:b:c:d:e:f:g:h:i"):
            self.assertIn(self.kind(host), guard.ROUTE_KINDS)
        self.assertEqual(guard.ROUTE_KINDS, ("lan", "tailscale", "cloudflare", "public-ip", "other"))

    def test_protection_follows_the_route_and_the_scheme(self):
        self.assertEqual(guard.route_protection("tailscale", "http"), "encrypted")             # WireGuard encrypts it whatever the application says
        self.assertEqual(guard.route_protection("tailscale", ""), "encrypted")
        self.assertEqual(guard.route_protection("lan", "http"), "cleartext")
        self.assertEqual(guard.route_protection("lan", "https"), "encrypted")
        self.assertEqual(guard.route_protection("cloudflare", "https"), "encrypted")
        self.assertEqual(guard.route_protection("cloudflare", "http"), "cleartext")
        self.assertEqual(guard.route_protection("public-ip", "http"), "cleartext")
        self.assertEqual(guard.route_protection("public-ip", "https"), "encrypted")
        self.assertEqual(guard.route_protection("other", "http"), "unknown")
        self.assertEqual(guard.route_protection("nonsense", "https"), "unknown")


class RouteLogTests(unittest.TestCase):
    NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)

    def test_a_source_shows_its_latest_route_and_when_each_route_was_last_used(self):
        log = guard.RouteLog()
        log.note("SITE-A", "tailscale", "encrypted", self.NOW)
        log.note("SITE-A", "cloudflare", "encrypted", self.NOW + timedelta(seconds=30))
        shown = log.snapshot(self.NOW + timedelta(seconds=40))["SITE-A"]
        self.assertEqual(shown["kind"], "cloudflare")
        self.assertEqual(shown["protection"], "encrypted")
        self.assertEqual(shown["at"], (self.NOW + timedelta(seconds=30)).isoformat())
        self.assertEqual(set(shown["last_by_kind"]), {"tailscale", "cloudflare"})                  # the fallback test reads exactly this
        self.assertEqual(shown["last_by_kind"]["tailscale"], self.NOW.isoformat())

    def test_a_route_not_used_for_a_day_is_forgotten_but_the_source_stays_listed(self):
        log = guard.RouteLog()
        log.note("SITE-B", "tailscale", "encrypted", self.NOW)
        log.note("SITE-B", "cloudflare", "encrypted", self.NOW + timedelta(hours=25))
        shown = log.snapshot(self.NOW + timedelta(hours=25, seconds=1))["SITE-B"]
        self.assertEqual(set(shown["last_by_kind"]), {"cloudflare"})

    def test_a_source_silent_for_more_than_a_day_drops_off(self):
        log = guard.RouteLog()
        log.note("OLD", "lan", "cleartext", self.NOW)
        self.assertEqual(log.snapshot(self.NOW + timedelta(hours=24, seconds=1)), {})

    def test_the_snapshot_holds_no_addresses(self):
        log = guard.RouteLog()
        log.note("MAIN", "lan", "cleartext", self.NOW)
        text = repr(log.snapshot(self.NOW))
        self.assertNotRegex(text, r"\d+\.\d+\.\d+\.\d+")
        self.assertEqual(set(log.snapshot(self.NOW)["MAIN"]), {"kind", "protection", "at", "last_by_kind"})

    def test_an_unknown_kind_is_stored_as_other_and_the_record_is_bounded(self):
        log = guard.RouteLog(max_sources=5)
        log.note("X", "made-up", "encrypted", self.NOW)
        self.assertEqual(log.snapshot(self.NOW)["X"]["kind"], "other")
        for index in range(50):
            log.note(f"S{index}", "lan", "cleartext", self.NOW)
        self.assertLessEqual(len(log.snapshot(self.NOW)), 5)

    def test_exposed_senders_are_untrusted_routes_in_cleartext_seen_recently(self):
        log = guard.RouteLog()
        log.note("MAIN", "lan", "cleartext", self.NOW)                        # a private LAN: cleartext but not exposed
        log.note("SITE-A", "tailscale", "encrypted", self.NOW)
        log.note("SITE-B", "cloudflare", "cleartext", self.NOW)             # key on the open internet in cleartext
        log.note("BACKUP-SERVER", "public-ip", "cleartext", self.NOW)
        log.note("VIA-TUNNEL", "cloudflare", "encrypted", self.NOW)          # the tunnel done properly
        self.assertEqual(log.exposed(self.NOW + timedelta(seconds=5)), ["BACKUP-SERVER", "SITE-B"])

    def test_an_exposure_is_cleared_by_the_next_good_post_and_by_time(self):
        log = guard.RouteLog()
        log.note("SITE-B", "cloudflare", "cleartext", self.NOW)
        self.assertEqual(log.exposed(self.NOW), ["SITE-B"])
        log.note("SITE-B", "tailscale", "encrypted", self.NOW + timedelta(seconds=10))
        self.assertEqual(log.exposed(self.NOW + timedelta(seconds=11)), [])
        log.note("SITE-B", "cloudflare", "cleartext", self.NOW + timedelta(seconds=20))
        self.assertEqual(log.exposed(self.NOW + timedelta(seconds=20 + guard.ROUTE_FRESH_SECONDS + 1)), [])


class RouteWiringTests(unittest.TestCase):
    class Req:
        def __init__(self, headers, scheme="http"):
            self.headers = headers
            self.scope = {"scheme": scheme}

    def setUp(self):
        listener.ROUTES.clear()

    def test_a_post_is_recorded_under_its_route_with_no_address_kept(self):
        listener._note_route(self.Req({"host": "192.168.7.183:8420"}), "MAIN")
        listener._note_route(self.Req({"host": "100.64.0.10:8420"}), "SITE-A")
        listener._note_route(self.Req({"host": "watchdog.corp.example", "cf-ray": "abc", "cf-visitor": '{"scheme":"https"}'}, "http"), "SITE-B")
        shown = listener.ROUTES.snapshot(datetime.now(timezone.utc))
        self.assertEqual((shown["MAIN"]["kind"], shown["MAIN"]["protection"]), ("lan", "cleartext"))
        self.assertEqual((shown["SITE-A"]["kind"], shown["SITE-A"]["protection"]), ("tailscale", "encrypted"))
        self.assertEqual((shown["SITE-B"]["kind"], shown["SITE-B"]["protection"]), ("cloudflare", "encrypted"))
        self.assertNotRegex(repr(shown), r"\d+\.\d+\.\d+\.\d+")

    def test_odd_requests_never_break_a_heartbeat(self):
        listener._note_route(object(), "X")
        listener._note_route(self.Req({"host": "192.168.7.183"}), None)
        listener._note_route(self.Req({"host": "192.168.7.183"}), "")
        self.assertEqual(listener.ROUTES.snapshot(datetime.now(timezone.utc)), {})

    def test_status_and_both_write_routes_use_it(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn('"routes": ROUTES.snapshot(now_dt)', source)
        self.assertIn('"exposed_senders": ROUTES.exposed(now_dt)', source)
        self.assertEqual(len(re.findall(r"^    _note_route\(request, source_id\)$", source, re.M)), 2)       # /heartbeat and /event

SENDER_KEYS = {
    "MAIN": "main-key-0123456789abcdefghijkl",
    "BACKUP-SERVER": "backup-key-0123456789abcdefghij",
    "SITE-A": "site_a-key-0123456789abcdefghi",
    "SITE-B": "site_b-key-0123456789abcdefghi",
}
SHARED = ("shared-key-current-0123456789ab", "shared-key-previous-0123456789a")


class SenderKeyConfigTests(unittest.TestCase):
    """an earlier review item: all four senders shared one key and `hostname` in the body is whatever the caller says, so a key taken from ONE machine
    could post as ANY of them (hide a dead player, forge a heartbeat). Each sender now has its own key, bound to its own name."""

    SOURCES = ["SITE-A", "SITE-B", "MAIN", "BACKUP-SERVER"]

    def test_the_variable_names_follow_the_source_names(self):
        self.assertEqual(guard.sender_key_var("MAIN"), "WATCHDOG_SENDER_KEY_MAIN")
        self.assertEqual(guard.sender_key_var("BACKUP-SERVER"), "WATCHDOG_SENDER_KEY_BACKUP_SERVER")
        self.assertEqual(guard.sender_key_var("BACKUP-SERVER", previous=True), "WATCHDOG_SENDER_KEY_BACKUP_SERVER_PREVIOUS")

    def test_keys_are_read_for_the_known_sources_only(self):
        environ = {"WATCHDOG_SENDER_KEY_MAIN": SENDER_KEYS["MAIN"], "WATCHDOG_SENDER_KEY_SITE_A_PREVIOUS": SENDER_KEYS["SITE-A"],
                   "WATCHDOG_SENDER_KEY_STRANGER": "stranger-key-0123456789abcdefghi", "WATCHDOG_SENDER_KEY_SITE_B": ""}
        loaded = guard.load_sender_keys(environ, self.SOURCES)
        self.assertEqual(loaded, {"MAIN": (SENDER_KEYS["MAIN"], ""), "SITE-A": ("", SENDER_KEYS["SITE-A"])})

    def test_a_key_too_short_to_be_one_stops_the_start_and_the_message_hides_the_value(self):
        with self.assertRaises(guard.KeyConfigError) as caught:
            guard.load_sender_keys({"WATCHDOG_SENDER_KEY_MAIN": "short-secret"}, self.SOURCES)
        self.assertIn("WATCHDOG_SENDER_KEY_MAIN", str(caught.exception))
        self.assertNotIn("short-secret", str(caught.exception))

    def test_a_key_shared_by_two_sources_stops_the_start(self):
        # Binding means nothing if two machines hold the same key.
        environ = {"WATCHDOG_SENDER_KEY_MAIN": SENDER_KEYS["MAIN"], "WATCHDOG_SENDER_KEY_SITE_A": SENDER_KEYS["MAIN"]}
        with self.assertRaises(guard.KeyConfigError) as caught:
            guard.load_sender_keys(environ, self.SOURCES)
        self.assertNotIn(SENDER_KEYS["MAIN"], str(caught.exception))

    def test_a_sources_previous_key_may_not_be_another_sources_current_key(self):
        environ = {"WATCHDOG_SENDER_KEY_MAIN": SENDER_KEYS["MAIN"], "WATCHDOG_SENDER_KEY_SITE_A_PREVIOUS": SENDER_KEYS["MAIN"]}
        with self.assertRaises(guard.KeyConfigError):
            guard.load_sender_keys(environ, self.SOURCES)

    def test_a_previous_key_equal_to_the_current_one_of_the_same_source_is_just_ignored(self):
        environ = {"WATCHDOG_SENDER_KEY_MAIN": SENDER_KEYS["MAIN"], "WATCHDOG_SENDER_KEY_MAIN_PREVIOUS": SENDER_KEYS["MAIN"]}
        self.assertEqual(guard.load_sender_keys(environ, self.SOURCES), {"MAIN": (SENDER_KEYS["MAIN"], "")})

    def test_the_mode_setting(self):
        self.assertEqual(guard.parse_auth_mode(""), "both")
        self.assertEqual(guard.parse_auth_mode(None), "both")
        self.assertEqual(guard.parse_auth_mode(" Both "), "both")
        self.assertEqual(guard.parse_auth_mode("per-sender"), "per-sender")
        self.assertEqual(guard.parse_auth_mode("PER-SENDER"), "per-sender")

    def test_a_mistyped_mode_stops_the_start_instead_of_quietly_staying_loose(self):
        for typo in ("per_sender", "persender", "strict", "true", "on"):
            with self.subTest(typo=typo):
                with self.assertRaises(guard.KeyConfigError):
                    guard.parse_auth_mode(typo)


class KeyringTests(unittest.TestCase):
    def ring(self, mode="both", with_previous=True):
        keys = {name: (key, "") for name, key in SENDER_KEYS.items()}
        if with_previous:
            keys["MAIN"] = (SENDER_KEYS["MAIN"], "main-OLD-key-0123456789abcdefgh")
        return guard.Keyring(keys, mode)

    def test_a_senders_own_key_names_that_sender(self):
        ring = self.ring()
        for name, key in SENDER_KEYS.items():
            with self.subTest(name=name):
                self.assertEqual(ring.identify(key, SHARED), guard.Identity("own", name))
        self.assertEqual(ring.identify("main-OLD-key-0123456789abcdefgh", SHARED), guard.Identity("own-previous", "MAIN"))

    def test_the_shared_key_and_its_previous_are_shared_and_belong_to_no_source(self):
        ring = self.ring()
        for key in SHARED:
            self.assertEqual(ring.identify(key, SHARED), guard.Identity("shared", None))

    def test_anything_else_is_nobody(self):
        ring = self.ring()
        for junk in (None, "", "nope", SENDER_KEYS["MAIN"][:-1], SENDER_KEYS["MAIN"] + "x", 42, b"bytes"):
            with self.subTest(junk=junk):
                self.assertEqual(ring.identify(junk, SHARED), guard.NOBODY)

    def test_in_both_mode_the_shared_key_may_still_post_and_in_per_sender_mode_it_may_not(self):
        both, strict = self.ring("both"), self.ring("per-sender")
        shared = both.identify(SHARED[0], SHARED)
        self.assertTrue(both.may_post(shared))
        self.assertFalse(strict.may_post(strict.identify(SHARED[0], SHARED)))
        self.assertFalse(strict.may_post(strict.identify(SHARED[1], SHARED)))
        for ring in (both, strict):
            self.assertTrue(ring.may_post(ring.identify(SENDER_KEYS["SITE-A"], SHARED)))
            self.assertTrue(ring.may_post(ring.identify("main-OLD-key-0123456789abcdefgh", SHARED)))
            self.assertFalse(ring.may_post(guard.NOBODY))

    def test_only_the_shared_key_is_an_admin_key_in_every_mode(self):
        for mode in ("both", "per-sender"):
            ring = self.ring(mode)
            self.assertTrue(ring.is_admin(ring.identify(SHARED[0], SHARED)))
            self.assertFalse(ring.is_admin(ring.identify(SENDER_KEYS["MAIN"], SHARED)))
            self.assertFalse(ring.is_admin(guard.NOBODY))

    def test_a_key_may_only_act_as_its_own_source(self):
        ring = self.ring()
        main = ring.identify(SENDER_KEYS["MAIN"], SHARED)
        self.assertTrue(ring.may_act_as(main, "MAIN"))
        self.assertFalse(ring.may_act_as(main, "SITE-A"))
        self.assertFalse(ring.may_act_as(main, "main"))                     # exact name, not a look-alike
        self.assertFalse(ring.may_act_as(main, "MAIN "))
        self.assertTrue(ring.may_act_as(ring.identify(SHARED[0], SHARED), "SITE-A"))        # the shared key has no binding (both mode only)

    def test_keyed_sources_and_open_windows_are_listed_by_name_only(self):
        ring = self.ring()
        self.assertEqual(ring.keyed_sources(), ["BACKUP-SERVER", "MAIN", "SITE-A", "SITE-B"])
        self.assertEqual(ring.windows_open(), ["MAIN"])
        self.assertEqual(self.ring(with_previous=False).windows_open(), [])
        self.assertEqual(guard.Keyring({}, "both").keyed_sources(), [])

    def test_identify_does_the_same_amount_of_work_whether_or_not_the_key_matches(self):
        # A comparison that stops at the first match would let a caller learn which key (and so which sender) it came close to by timing.
        ring, calls = self.ring(), []
        real = guard.hmac.compare_digest
        guard.hmac.compare_digest = lambda a, b: calls.append(1) or real(a, b)
        try:
            ring.identify(SENDER_KEYS["BACKUP-SERVER"], SHARED)
            hit = len(calls)
            calls.clear()
            ring.identify("completely-wrong-key-0123456789", SHARED)
            miss = len(calls)
        finally:
            guard.hmac.compare_digest = real
        self.assertEqual(hit, miss)
        self.assertGreaterEqual(hit, len(SENDER_KEYS) + 1 + len(SHARED))


class AuthLogTests(unittest.TestCase):
    NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)

    def test_each_source_shows_which_kind_of_key_it_last_used(self):
        log = guard.AuthLog()
        log.note("MAIN", "shared", self.NOW)
        log.note("MAIN", "own", self.NOW + timedelta(seconds=10))
        log.note("SITE-A", "own-previous", self.NOW)
        shown = log.used(self.NOW + timedelta(seconds=20))
        self.assertEqual(shown["MAIN"], {"credential": "own", "at": (self.NOW + timedelta(seconds=10)).isoformat()})
        self.assertEqual(shown["SITE-A"]["credential"], "own-previous")

    def test_a_source_not_heard_for_fifteen_minutes_is_not_shown(self):
        log = guard.AuthLog()
        log.note("SITE-B", "own", self.NOW)
        self.assertEqual(log.used(self.NOW + timedelta(seconds=guard.AUTH_FRESH_SECONDS + 1)), {})

    def test_a_refused_key_use_is_recorded_by_name_and_never_keeps_a_key(self):
        log = guard.AuthLog()
        log.mismatch("MAIN", "SITE-A", self.NOW)
        log.mismatch("MAIN", "SITE-A", self.NOW + timedelta(seconds=5))
        log.mismatch("SITE-B", "MAIN", self.NOW)
        shown = log.mismatches(self.NOW + timedelta(seconds=6))
        self.assertEqual(len(shown), 2)                                         # one line per pair, newest time kept
        self.assertEqual(shown[0], {"key_of": "MAIN", "claimed": "SITE-A", "at": (self.NOW + timedelta(seconds=5)).isoformat(), "count": 2})
        self.assertNotIn("key-0123", repr(shown))

    def test_the_records_are_bounded_and_old_mismatches_age_out(self):
        log = guard.AuthLog()
        for index in range(100):
            log.note(f"S{index}", "own", self.NOW)
            log.mismatch(f"K{index}", f"C{index}", self.NOW)
        self.assertLessEqual(len(log.used(self.NOW)), guard.AUTH_MAX_ENTRIES)
        self.assertLessEqual(len(log.mismatches(self.NOW)), guard.AUTH_MAX_ENTRIES)
        self.assertEqual(log.mismatches(self.NOW + timedelta(seconds=guard.AUTH_MISMATCH_SECONDS + 1)), [])


class SenderBindingTests(unittest.TestCase):
    """The listener wiring: /heartbeat and /event refuse a key that is not valid for the name the caller claims, before anything is stored."""

    class Req:
        def __init__(self, token, payload=None, scheme="http"):
            self.headers = {} if token is None else {"x-watchdog-token": token}
            self.scope = {"scheme": scheme}
            self._payload = payload

        async def stream(self):
            yield listener.json.dumps(self._payload or {}).encode()

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (listener.DB_PATH, listener.KEYRING, listener.WRITE_TOKENS)
        listener.DB_PATH = Path(self.tmp.name) / "watchdog.db"
        listener.init_db()
        listener.KEYRING = guard.Keyring({name: (key, "") for name, key in SENDER_KEYS.items()}, "both")
        listener.AUTH.clear()

    def tearDown(self):
        listener.DB_PATH, listener.KEYRING, listener.WRITE_TOKENS = self.saved
        listener.AUTH.clear()
        self.tmp.cleanup()

    def stored_sources(self):
        with listener.db() as conn:
            return sorted(row[0] for row in conn.execute("SELECT source_id FROM sources"))

    def post(self, handler, token, payload):
        import asyncio
        return asyncio.run(handler(self.Req(token, payload)))

    def test_a_key_posting_as_another_sender_is_refused_with_403_and_nothing_is_stored(self):
        for handler, payload in ((listener.heartbeat, {"hostname": "SITE-A"}), (listener.event, {"hostname": "SITE-A", "event_type": "x"}),
                                 (listener.event, {"source_id": "SITE-A", "event_type": "x"})):
            with self.subTest(handler=handler.__name__, payload=payload):
                with self.assertRaises(listener.HTTPException) as caught:
                    self.post(handler, SENDER_KEYS["MAIN"], payload)
                self.assertEqual(caught.exception.status_code, 403)
                self.assertNotIn("MAIN", caught.exception.detail)              # the answer does not reveal whose key it was
        self.assertEqual(self.stored_sources(), [])
        with listener.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(listener.AUTH.mismatches(datetime.now(timezone.utc))[0]["key_of"], "MAIN")

    def test_a_key_posting_as_itself_is_accepted_and_noted(self):
        reply = self.post(listener.heartbeat, SENDER_KEYS["MAIN"], {"hostname": "MAIN"})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(self.stored_sources(), ["MAIN"])
        self.assertEqual(listener.AUTH.used(datetime.now(timezone.utc))["MAIN"]["credential"], "own")

    def test_the_shared_key_still_posts_as_anyone_in_both_mode_and_is_noted_as_shared(self):
        reply = self.post(listener.heartbeat, TEST_TOKEN, {"hostname": "SITE-A"})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(listener.AUTH.used(datetime.now(timezone.utc))["SITE-A"]["credential"], "shared")

    def test_the_dependencies_follow_the_mode(self):
        listener.require_token(SENDER_KEYS["SITE-B"])                      # a sender's own key may post
        listener.require_token(TEST_TOKEN)                                  # the shared key may, in `both`
        listener.KEYRING = guard.Keyring({name: (key, "") for name, key in SENDER_KEYS.items()}, "per-sender")
        listener.require_token(SENDER_KEYS["SITE-B"])
        for refused in (TEST_TOKEN, "nope", None, ""):
            with self.subTest(refused=refused):
                with self.assertRaises(listener.HTTPException) as caught:
                    listener.require_token(refused)
                self.assertEqual(caught.exception.status_code, 401)

    def test_resetting_an_alarm_takes_the_admin_key_not_a_senders_key(self):
        listener.require_admin_token(TEST_TOKEN)
        for refused in (SENDER_KEYS["MAIN"], "nope", None):
            with self.subTest(refused=refused):
                with self.assertRaises(listener.HTTPException) as caught:
                    listener.require_admin_token(refused)
                self.assertEqual(caught.exception.status_code, 401)
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn('@app.post("/alerts/reset", dependencies=[Depends(require_admin_token)])', source)
        self.assertIn('@app.post("/heartbeat", dependencies=[Depends(require_token)])', source)
        self.assertIn('@app.post("/event", dependencies=[Depends(require_token)])', source)

    def test_a_senders_key_cannot_read_status_or_the_stream(self):
        # Reading stays with the shared (admin) key and the page's view key; a stolen sender key reaches nothing but its own heartbeat.
        self.assertFalse(listener._view_token_matches(SENDER_KEYS["MAIN"]))
        self.assertTrue(listener._view_token_matches(TEST_TOKEN))

    def test_the_shared_key_still_reads_in_per_sender_mode(self):
        listener.KEYRING = guard.Keyring({name: (key, "") for name, key in SENDER_KEYS.items()}, "per-sender")
        self.assertTrue(listener._view_token_matches(TEST_TOKEN))           # the deploy check and the weekly report keep working

    def test_status_reports_the_auth_picture_without_any_key(self):
        listener.KEYRING = guard.Keyring({"MAIN": (SENDER_KEYS["MAIN"], "main-OLD-key-0123456789abcdefgh")}, "both")
        self.post(listener.heartbeat, SENDER_KEYS["MAIN"], {"hostname": "MAIN"})
        shown = listener._sender_auth_status(datetime.now(timezone.utc))
        self.assertEqual(shown["mode"], "both")
        self.assertEqual(shown["keyed_sources"], ["MAIN"])
        self.assertEqual(shown["windows_open"], ["MAIN"])
        self.assertEqual(shown["used"]["MAIN"]["credential"], "own")
        text = repr(shown)
        for secret in list(SENDER_KEYS.values()) + ["main-OLD-key-0123456789abcdefgh", TEST_TOKEN]:
            self.assertNotIn(secret, text)
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn('"sender_auth": _sender_auth_status(now_dt, admin)', source)       # the key-set digest only for a caller holding the admin key

    def test_the_compose_file_hands_every_sender_key_and_the_mode_to_the_container(self):
        compose = Path(listener.__file__).resolve().parent.parent.joinpath("docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("WATCHDOG_SENDER_AUTH", compose)
        for name in listener.KNOWN_SOURCE_IDS:
            for previous in (False, True):
                variable = guard.sender_key_var(name, previous)
                with self.subTest(variable=variable):
                    self.assertIn(f"{variable}: ${{{variable}:-}}", compose)


class VisitorKeyTests(unittest.TestCase):
    """Per-visitor limits need to know who is asking. Behind the Cloudflare tunnel the TCP peer is always the tunnel, so the visitor is the address Cloudflare
    reports. That header is believed ONLY when the request came from the tunnel's own network (a client that reaches the port directly could write it
    himself), and then only CF-Connecting-IP together with CF-Ray; every other forwarding header is ignored. This is for fairness, never for authentication."""

    TUNNEL = "172.21.0.3"

    def key(self, headers, peer=TUNNEL, trusted=None):
        return guard.visitor_key({name.lower(): value for name, value in headers.items()}, peer, trusted if trusted is not None else guard.DEFAULT_TRUSTED_PROXIES)

    def test_behind_cloudflare_the_visitor_is_the_address_cloudflare_reports(self):
        self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "8a1b2c-JFK"}), "cf:198.51.100.23")

    def test_cloudflare_headers_from_a_peer_outside_the_tunnels_network_are_not_believed(self):
        # The audit's concern: someone who reaches port 8420 directly could write CF-Connecting-IP himself and look like any number of visitors.
        for peer in ("192.168.7.50", "100.100.60.12", "8.8.8.8", "203.0.113.9", None, "", "not-an-address"):
            with self.subTest(peer=peer):
                self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, peer), "peer:" + (peer or "unknown"))

    def test_the_default_trusted_network_is_docker_s_private_range_and_loopback(self):
        for peer in ("172.21.0.3", "172.17.0.2", "172.31.255.254", "127.0.0.1"):
            with self.subTest(peer=peer):
                self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, peer), "cf:198.51.100.23")
        self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, "172.32.0.1"), "peer:172.32.0.1")        # just outside 172.16.0.0/12

    def test_a_docker_bridge_gateway_is_never_believed_because_host_relayed_connections_appear_as_it(self):
        # Connections that reach the published port from the LAN, Tailscale or the port proxy arrive at the container as the bridge's gateway (x.x.0.1).
        # Those are exactly the clients that could write Cloudflare headers themselves, so the gateway is excluded from the trusted range.
        for gateway in ("172.21.0.1", "172.17.0.1", "172.18.0.1", "172.31.0.1"):
            with self.subTest(gateway=gateway):
                self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, gateway), "peer:" + gateway)
        self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, "172.21.0.2"), "cf:198.51.100.23")      # the next address is a container
        self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, "172.21.1.1"), "cf:198.51.100.23")      # only the x.x.0.1 convention

    # The gateway is read from the container's own routing table (/proc/net/route), not guessed from an address pattern; the pattern is only the fallback.
    ROUTE_TABLE = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
                   "eth0\t00000000\t010015AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
                   "eth0\t000015AC\t00000000\t0001\t0\t0\t0\tFFFF0000\t0\t0\t0\n")

    def test_the_real_gateway_is_read_from_the_routing_table(self):
        self.assertEqual(guard.parse_default_gateways(self.ROUTE_TABLE), frozenset({"172.21.0.1"}))
        self.assertEqual(guard.parse_default_gateways(""), frozenset())
        self.assertEqual(guard.parse_default_gateways("garbage\nmore garbage"), frozenset())
        two = self.ROUTE_TABLE + "eth1\t00000000\t0107A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        self.assertEqual(guard.parse_default_gateways(two), frozenset({"172.21.0.1", "192.168.7.1"}))

    def test_an_unreadable_routing_table_means_unknown_not_empty(self):
        self.assertIsNone(guard.read_default_gateways("/nonexistent/route"))

    def test_a_known_gateway_set_is_excluded_exactly_and_the_pattern_is_not_applied(self):
        known = frozenset({"172.21.0.1"})
        headers = {"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}
        lower = {name.lower(): value for name, value in headers.items()}
        self.assertEqual(guard.visitor_key(lower, "172.21.0.1", guard.DEFAULT_TRUSTED_PROXIES, known), "peer:172.21.0.1")
        self.assertEqual(guard.visitor_key(lower, "172.21.0.3", guard.DEFAULT_TRUSTED_PROXIES, known), "cf:198.51.100.23")
        # A container that merely happens to hold an address ending in .0.1 on some OTHER network is not the gateway of this one.
        self.assertEqual(guard.visitor_key(lower, "172.30.0.1", guard.DEFAULT_TRUSTED_PROXIES, known), "cf:198.51.100.23")

    def test_when_the_gateway_cannot_be_read_the_pattern_is_the_safe_fallback(self):
        lower = {"cf-connecting-ip": "198.51.100.23", "cf-ray": "x"}
        self.assertEqual(guard.visitor_key(lower, "172.21.0.1", guard.DEFAULT_TRUSTED_PROXIES, None), "peer:172.21.0.1")
        self.assertEqual(guard.visitor_key(lower, "172.21.0.3", guard.DEFAULT_TRUSTED_PROXIES, None), "cf:198.51.100.23")

    def test_a_narrow_list_also_excludes_the_gateway_but_an_explicit_single_address_is_believed(self):
        narrow = guard.parse_trusted_proxies("172.21.0.0/16")
        self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, "172.21.0.1", narrow), "peer:172.21.0.1")
        exact = guard.parse_trusted_proxies("172.21.0.3/32")                                  # the strictest form: only the tunnel container itself
        self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, "172.21.0.3", exact), "cf:198.51.100.23")
        self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, "172.21.0.4", exact), "peer:172.21.0.4")
        self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, "172.21.0.1", guard.parse_trusted_proxies("172.21.0.1/32")), "cf:198.51.100.23")

    def test_the_trusted_networks_can_be_narrowed_to_the_tunnels_own_subnet(self):
        narrow = guard.parse_trusted_proxies("172.21.0.0/16")
        self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, "172.21.0.3", narrow), "cf:198.51.100.23")
        self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, "172.17.0.2", narrow), "peer:172.17.0.2")

    def test_the_trusted_network_setting(self):
        self.assertEqual(guard.parse_trusted_proxies(""), guard.DEFAULT_TRUSTED_PROXIES)
        self.assertEqual(guard.parse_trusted_proxies(None), guard.DEFAULT_TRUSTED_PROXIES)
        self.assertEqual(len(guard.parse_trusted_proxies(" 172.21.0.0/16 , 10.0.0.0/8 ")), 2)
        for bad in ("nonsense", "172.21.0.0/99", "172.21.0.0/16,oops"):
            with self.subTest(bad=bad):
                with self.assertRaises(guard.KeyConfigError):
                    guard.parse_trusted_proxies(bad)

    def test_other_forwarding_headers_are_never_believed(self):
        for header in ("X-Forwarded-For", "X-Real-IP", "True-Client-IP", "Forwarded", "X-Client-IP"):
            with self.subTest(header=header):
                self.assertEqual(self.key({header: "198.51.100.99"}, "192.168.7.50"), "peer:192.168.7.50")
                self.assertEqual(self.key({header: "198.51.100.99", "CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}), "cf:198.51.100.23")

    def test_the_cloudflare_address_is_used_only_when_the_request_carries_cloudflares_other_marker(self):
        self.assertEqual(self.key({"CF-Connecting-IP": "198.51.100.23"}), "peer:" + self.TUNNEL)        # a lone header is just a header

    def test_a_cloudflare_address_that_is_not_an_address_falls_back_to_the_peer(self):
        for bad in ("not-an-ip", "", "198.51.100.23, 203.0.113.7", "999.1.1.1", "<script>", "a" * 500):
            with self.subTest(bad=bad[:20]):
                self.assertEqual(self.key({"CF-Connecting-IP": bad, "CF-Ray": "x"}), "peer:" + self.TUNNEL)

    def test_an_ipv6_visitor_is_one_visitor_per_slash_64(self):
        # Anyone with an IPv6 connection owns a whole /64, so keying by the single address would let one visitor become billions.
        first = self.key({"CF-Connecting-IP": "2001:db8:abcd:12::1", "CF-Ray": "x"})
        same_64 = self.key({"CF-Connecting-IP": "2001:db8:abcd:12:ffff:ffff:ffff:ffff", "CF-Ray": "x"})
        other_64 = self.key({"CF-Connecting-IP": "2001:db8:abcd:13::1", "CF-Ray": "x"})
        self.assertEqual(first, same_64)
        self.assertNotEqual(first, other_64)
        self.assertTrue(first.startswith("cf:"))

    def test_the_same_address_written_differently_is_the_same_visitor(self):
        self.assertEqual(self.key({"CF-Connecting-IP": " 198.51.100.23 ", "CF-Ray": "x"}), self.key({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "y"}))
        self.assertEqual(self.key({"CF-Connecting-IP": "2001:DB8::1", "CF-Ray": "x"}), self.key({"CF-Connecting-IP": "2001:db8:0:0:0:0:0:1", "CF-Ray": "x"}))

    def test_without_cloudflare_the_visitor_is_the_peer_and_odd_peers_are_safe(self):
        self.assertEqual(self.key({}, "192.168.7.50"), "peer:192.168.7.50")
        self.assertEqual(self.key({}, None), "peer:unknown")
        self.assertEqual(self.key({}, ""), "peer:unknown")
        self.assertLessEqual(len(self.key({}, "x" * 5000)), 80)


class StreamPoolTests(unittest.TestCase):
    """The audit's remaining worry: many different internet addresses at once could use up every live stream. The origin cannot beat a large botnet (that is
    Cloudflare's job) but it can make a handful of addresses useless: one pool owns the capacity, each visitor is capped, and when the pool is full a newcomer
    takes a stream from whoever holds the MOST (only if they hold at least two more than the newcomer, so evictions settle at a fair share and never ping-pong)."""

    def fill(self, pool, visitor, count):
        return [pool.admit(visitor)[0] for _ in range(count)]

    def test_a_visitor_is_held_to_its_cap_and_told_why(self):
        pool = guard.StreamPool(capacity=10, per_visitor=3)
        self.assertTrue(all(h is not None for h in self.fill(pool, "a", 3)))
        self.assertEqual(pool.admit("a"), (None, "visitor-cap"))
        handle, reason = pool.admit("b")
        self.assertIsNotNone(handle)
        self.assertEqual(reason, "ok")

    def test_a_pool_full_of_different_visitors_refuses_a_newcomer_and_takes_nothing(self):
        # The honest limit: as many different addresses as there are slots cannot be told apart from real viewers at this level.
        pool = guard.StreamPool(capacity=4, per_visitor=2)
        for name in "abcd":
            self.assertIsNotNone(pool.admit(name)[0])
        self.assertEqual(pool.admit("e"), (None, "full"))
        self.assertEqual(pool.total(), 4)
        self.assertEqual(pool.count("e"), 0)

    def test_a_newcomer_takes_a_stream_from_whoever_holds_the_most(self):
        pool = guard.StreamPool(capacity=4, per_visitor=2)
        first = self.fill(pool, "a", 2)
        second = self.fill(pool, "b", 2)
        handle, reason = pool.admit("c")
        self.assertEqual(reason, "ok")
        self.assertIsNotNone(handle)
        self.assertEqual(pool.total(), 4)                                       # capacity is never exceeded
        evicted = [h for h in first + second if h.evicted]
        self.assertEqual(len(evicted), 1)
        self.assertEqual(evicted[0], first[0])                                  # the heaviest holder that came first, and its OLDEST stream
        self.assertEqual(pool.count("a"), 1)
        self.assertEqual(pool.evictions, 1)

    def test_nobody_is_evicted_unless_the_difference_is_at_least_two(self):
        pool = guard.StreamPool(capacity=3, per_visitor=3)
        self.fill(pool, "a", 2)
        self.fill(pool, "b", 1)
        self.assertEqual(pool.admit("b")[1], "full")                            # b has 1, a has 2: a lead of only 1, so a swap would achieve nothing
        self.assertEqual(pool.evictions, 0)

    def test_an_evicted_visitor_who_reconnects_does_not_push_others_out_again(self):
        # No ping-pong: four addresses fill 32 slots at 8 each; eight real viewers arrive; the squatters settle at an equal share and stay there.
        pool = guard.StreamPool(capacity=32, per_visitor=8)
        squatters = {name: self.fill(pool, name, 8) for name in ("s1", "s2", "s3", "s4")}
        self.assertEqual(pool.total(), 32)
        for index in range(8):
            self.assertEqual(pool.admit(f"real{index}")[1], "ok")
        counts = sorted(pool.count(name) for name in squatters)
        self.assertEqual(sum(counts), 24)
        self.assertLessEqual(counts[-1] - counts[0], 1)                         # the squatters are level
        evicted_before = pool.evictions
        for name in squatters:                                                  # every squatter tries to take its streams back
            for _ in range(8):
                pool.admit(name)
        self.assertEqual(pool.evictions, evicted_before)                        # and displaces nobody
        self.assertTrue(all(pool.count(f"real{index}") == 1 for index in range(8)))
        self.assertEqual(pool.total(), 32)

    def test_a_flood_of_different_addresses_is_the_limit_of_what_the_origin_can_do(self):
        pool = guard.StreamPool(capacity=8, per_visitor=8)
        for index in range(8):
            pool.admit(f"bot{index}")
        self.assertEqual(pool.admit("real")[1], "full")                         # documented: this needs Cloudflare in front, not a smarter origin

    def test_releasing_is_safe_to_repeat_and_an_evicted_stream_is_not_released_twice(self):
        pool = guard.StreamPool(capacity=2, per_visitor=2)
        a1, a2 = self.fill(pool, "a", 2)
        pool.admit("b")                                                         # evicts a1
        self.assertTrue(a1.evicted)
        pool.release(a1)
        pool.release(a1)
        self.assertEqual(pool.total(), 2)                                       # a1 had already given its slot up
        pool.release(a2)
        pool.release(a2)
        self.assertEqual(pool.total(), 1)
        self.assertEqual(pool.count("a"), 0)

    def test_the_invariants_hold_under_a_long_random_run(self):
        import random
        rng = random.Random(20261020)
        pool = guard.StreamPool(capacity=12, per_visitor=4)
        live = []
        for _ in range(4000):
            if live and rng.random() < 0.4:
                pool.release(live.pop(rng.randrange(len(live))))
            else:
                handle, _reason = pool.admit(f"v{rng.randrange(9)}")
                if handle is not None:
                    live.append(handle)
            live = [h for h in live if not h.evicted]
            self.assertLessEqual(pool.total(), 12)
            self.assertEqual(pool.total(), len(live))
            for visitor in {h.visitor for h in live}:
                self.assertLessEqual(pool.count(visitor), 4)

    def test_a_pool_of_zero_refuses_everyone_and_the_snapshot_names_no_visitor(self):
        self.assertEqual(guard.StreamPool(capacity=0, per_visitor=2).admit("a"), (None, "full"))
        pool = guard.StreamPool(capacity=5, per_visitor=2)
        pool.admit("198.51.100.23")
        self.assertEqual(pool.snapshot(), {"open": 1, "capacity": 5})
        self.assertNotIn("198", repr(pool.snapshot()))

    def test_stream_numbers_are_unique(self):
        pool = guard.StreamPool(capacity=20, per_visitor=20)
        numbers = [h.number for h in self.fill(pool, "a", 20)]
        self.assertEqual(len(set(numbers)), 20)

    def test_the_default_per_visitor_cap_leaves_room_for_several_visitors(self):
        self.assertLess(guard.STREAMS_PER_VISITOR, 32)
        self.assertGreaterEqual(guard.STREAMS_PER_VISITOR, 2)


class VisitorRateTests(unittest.TestCase):
    def test_a_visitor_gets_its_allowance_per_window_and_then_waits(self):
        rate = guard.VisitorRate(limit=3, window_seconds=60)
        self.assertEqual([rate.allow("v", t) for t in (0.0, 1.0, 2.0)], [True, True, True])
        self.assertFalse(rate.allow("v", 3.0))
        self.assertGreater(rate.retry_after("v", 3.0), 0)
        self.assertLessEqual(rate.retry_after("v", 3.0), 60)

    def test_the_allowance_comes_back_as_old_requests_leave_the_window(self):
        rate = guard.VisitorRate(limit=2, window_seconds=60)
        rate.allow("v", 0.0)
        rate.allow("v", 30.0)
        self.assertFalse(rate.allow("v", 59.0))
        self.assertTrue(rate.allow("v", 61.0))                          # the request at 0.0 has left the window

    def test_visitors_do_not_share_an_allowance(self):
        rate = guard.VisitorRate(limit=1, window_seconds=60)
        self.assertTrue(rate.allow("a", 0.0))
        self.assertFalse(rate.allow("a", 1.0))
        self.assertTrue(rate.allow("b", 1.0))

    def test_a_refused_request_does_not_extend_the_wait(self):
        rate = guard.VisitorRate(limit=1, window_seconds=60)
        rate.allow("v", 0.0)
        for moment in range(1, 50):
            rate.allow("v", float(moment))                              # hammering while refused
        self.assertTrue(rate.allow("v", 61.0))

    def test_the_number_of_remembered_visitors_and_their_history_are_bounded(self):
        rate = guard.VisitorRate(limit=5, window_seconds=60, max_visitors=40)
        for index in range(400):
            rate.allow(f"v{index}", float(index) / 100)
        self.assertLessEqual(rate.visitors(), 40)
        for _ in range(1000):
            rate.allow("one", 1.0)
        self.assertLessEqual(rate.recent("one"), 5)

    def test_when_the_table_is_full_expired_visitors_go_first_and_a_busy_one_is_kept(self):
        rate = guard.VisitorRate(limit=2, window_seconds=60, max_visitors=3)
        rate.allow("busy", 0.0)                                         # "busy" is the OLDEST-INSERTED visitor ...
        rate.allow("busy", 50.0)                                        # ... and is still inside its window at t=70
        rate.allow("expired-1", 1.0)
        rate.allow("expired-2", 2.0)
        self.assertEqual(rate.visitors(), 3)
        self.assertTrue(rate.allow("new", 70.0))                        # the table is full: the two EXPIRED visitors make room, not the busy one
        self.assertEqual(rate.recent("busy"), 1)                        # busy still has its history (a forgotten visitor would have a fresh allowance)
        self.assertEqual(rate.visitors(), 2)


class ReadSideWiringTests(unittest.TestCase):
    """The listener applies those limits to /status and /stream, and /status asks for fewer rows at most."""

    TUNNEL = "172.21.0.3"

    class Req:
        def __init__(self, headers=None, host="172.21.0.3"):
            self.headers = {k.lower(): v for k, v in (headers or {}).items()}
            self.client = type("Client", (), {"host": host})()
            self.scope = {"scheme": "http"}

        async def is_disconnected(self):
            return True

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (listener.DB_PATH, listener._public_streams, listener._private_streams, listener._status_rate, listener.STREAM_SEND_TIMEOUT_SECONDS)
        listener.DB_PATH = Path(self.tmp.name) / "watchdog.db"
        listener.init_db()
        listener._public_streams = guard.StreamPool(capacity=4, per_visitor=2)
        listener._private_streams = guard.StreamPool(capacity=3, per_visitor=2)
        listener._status_rate = guard.VisitorRate(limit=3, window_seconds=60)

    def tearDown(self):
        (listener.DB_PATH, listener._public_streams, listener._private_streams, listener._status_rate, listener.STREAM_SEND_TIMEOUT_SECONDS) = self.saved
        self.tmp.cleanup()

    def run_async(self, coroutine):
        return listener.asyncio.run(coroutine)

    def tunnel_request(self, n):
        return self.Req({"CF-Connecting-IP": f"198.51.100.{n}", "CF-Ray": "x"}, self.TUNNEL)

    def test_status_refuses_a_visitor_over_its_allowance_with_429_and_retry_after(self):
        request = self.tunnel_request(23)
        for _ in range(3):
            self.assertEqual(self.run_async(listener.status(request, limit=5, source=None)).status_code, 200)
        refused = self.run_async(listener.status(request, limit=5, source=None))
        self.assertEqual(refused.status_code, 429)
        self.assertGreater(int(refused.headers["Retry-After"]), 0)
        self.assertEqual(self.run_async(listener.status(self.tunnel_request(24), limit=5, source=None)).status_code, 200)

    def test_a_visitor_cannot_pick_its_own_key_by_writing_headers(self):
        for index in range(3):
            self.run_async(listener.status(self.Req({"X-Forwarded-For": f"10.9.9.{index}", "CF-Connecting-IP": f"198.51.100.{index}", "CF-Ray": "x"}, "192.168.7.50"),
                                           limit=5, source=None))
        refused = self.run_async(listener.status(self.Req({"X-Forwarded-For": "10.9.9.99", "CF-Connecting-IP": "198.51.100.99", "CF-Ray": "x"}, "192.168.7.50"),
                                                 limit=5, source=None))
        self.assertEqual(refused.status_code, 429)                      # a direct client's forged Cloudflare headers bought it nothing

    def test_cloudflare_headers_from_a_peer_that_is_not_trusted_are_ignored_and_logged_once(self):
        # A spoofing attempt (or a trust list set wrong, which would make every public viewer look like one visitor) must be visible, not silent.
        messages = []
        saved = (listener.logger.warning, dict(listener._untrusted_cf_logged))
        listener.logger.warning = lambda message, *args: messages.append(message % args if args else message)
        listener._untrusted_cf_logged.clear()
        try:
            request = self.Req({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, "192.168.7.50")
            self.assertEqual(listener._visitor_of(request), "peer:192.168.7.50")
            listener._visitor_of(request)
            listener._visitor_of(request)
            self.assertEqual(len(messages), 1)                                   # once, not once per request
            self.assertIn("192.168.7.50", messages[0])
            self.assertNotIn("198.51.100.23", messages[0])                        # the claimed visitor address is not echoed into the log
            listener._visitor_of(self.Req({"CF-Connecting-IP": "198.51.100.23", "CF-Ray": "x"}, "192.168.7.51"))
            self.assertEqual(len(messages), 2)                                   # a different peer is a different note
            listener._visitor_of(self.tunnel_request(23))
            listener._visitor_of(self.Req({}, "192.168.7.50"))
            self.assertEqual(len(messages), 2)                                   # a trusted peer, or no Cloudflare headers at all, is not worth a note
        finally:
            listener.logger.warning = saved[0]
            listener._untrusted_cf_logged.clear()
            listener._untrusted_cf_logged.update(saved[1])

    def test_status_asks_for_at_most_200_rows(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn("MAX_STATUS_EVENTS = 200", source)
        self.assertIn("limit: int = Query(default=50, ge=1, le=MAX_STATUS_EVENTS)", source)

    def drain(self, response):
        async def go():
            return [chunk async for chunk in response.content]
        return self.run_async(go())

    def pools_empty(self):
        return listener._public_streams.total() == 0 and listener._private_streams.total() == 0

    def test_a_visitor_over_its_stream_cap_gets_429_before_it_can_take_a_pool_slot(self):
        view = listener.VIEW_TOKEN
        request = self.tunnel_request(23)
        first = self.run_async(listener.stream(request, token=view))
        second = self.run_async(listener.stream(request, token=view))
        with self.assertRaises(listener.HTTPException) as caught:
            self.run_async(listener.stream(request, token=view))
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(listener._public_streams.total(), 2)             # the refused one took no slot
        other = self.run_async(listener.stream(self.tunnel_request(24), token=view))
        self.assertEqual(listener._public_streams.total(), 3)
        for response in (first, second, other):                           # run each to its end (the fake client is already gone) so it cleans up
            self.drain(response)
        self.assertTrue(self.pools_empty())

    def test_a_pool_full_of_different_visitors_answers_503_and_leaves_nothing_behind(self):
        view = listener.VIEW_TOKEN
        for n in range(4):                                                # capacity 4, one stream each: nobody holds two more than a newcomer
            self.run_async(listener.stream(self.tunnel_request(n + 1), token=view))
        with self.assertRaises(listener.HTTPException) as caught:
            self.run_async(listener.stream(self.tunnel_request(77), token=view))
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(listener._public_streams.total(), 4)
        self.assertEqual(listener._public_streams.count("cf:198.51.100.77"), 0)

    def test_a_newcomer_displaces_a_stream_of_whoever_holds_the_most_and_that_stream_ends(self):
        view = listener.VIEW_TOKEN
        saved_push = listener.STREAM_PUSH_SECONDS
        listener.STREAM_PUSH_SECONDS = 0.01

        class LiveReq(self.Req):
            async def is_disconnected(self):
                return False                                              # a viewer that is still there: only eviction can end its stream

        def live(n):
            return LiveReq({"CF-Connecting-IP": f"198.51.100.{n}", "CF-Ray": "x"}, self.TUNNEL)

        async def scenario():
            responses = [await listener.stream(live(1), token=view) for _ in range(2)] + [await listener.stream(live(2), token=view) for _ in range(2)]
            newcomer = await listener.stream(live(3), token=view)            # pool of 4 is full; visitors 1 and 2 hold 2 each
            ended, alive = 0, []
            for response in responses:
                iterator = response.content.__aiter__()
                try:
                    await listener.asyncio.wait_for(iterator.__anext__(), 5)
                    alive.append(iterator)
                except StopAsyncIteration:
                    ended += 1                                                # this one was evicted: its generator stops before sending a frame
            newcomer_iterator = newcomer.content.__aiter__()
            await listener.asyncio.wait_for(newcomer_iterator.__anext__(), 5)        # the newcomer really gets frames (it was admitted)
            for iterator in alive + [newcomer_iterator]:
                await iterator.aclose()                                              # a generator that has started runs its `finally` when closed
            return ended

        try:
            ended = self.run_async(scenario())
        finally:
            listener.STREAM_PUSH_SECONDS = saved_push
        self.assertEqual(ended, 1)
        self.assertEqual(listener._public_streams.evictions, 1)
        self.assertTrue(self.pools_empty())

    def test_lan_and_tailscale_viewers_have_their_own_pool_that_the_internet_cannot_fill(self):
        view = listener.VIEW_TOKEN
        for n in range(4):
            self.run_async(listener.stream(self.tunnel_request(n + 1), token=view))     # the internet side is now full
        with self.assertRaises(listener.HTTPException) as caught:
            self.run_async(listener.stream(self.tunnel_request(90), token=view))
        self.assertEqual(caught.exception.status_code, 503)
        lan = self.Req({"Host": "192.168.7.183:8420"}, "192.168.7.50")
        tailscale = self.Req({"Host": "100.64.0.10:8420"}, "172.21.0.1")
        self.run_async(listener.stream(lan, token=view))
        self.run_async(listener.stream(tailscale, token=view))
        self.assertEqual(listener._private_streams.total(), 2)
        self.assertEqual(listener._public_streams.total(), 4)

    def test_a_request_through_cloudflare_cannot_claim_the_private_pool_by_writing_a_host_header(self):
        view = listener.VIEW_TOKEN
        spoof = self.Req({"Host": "192.168.7.183:8420", "CF-Connecting-IP": "198.51.100.5", "CF-Ray": "x"}, self.TUNNEL)
        self.run_async(listener.stream(spoof, token=view))
        self.assertEqual(listener._public_streams.total(), 1)
        self.assertEqual(listener._private_streams.total(), 0)

    def test_an_unrecognised_route_counts_as_the_public_pool(self):
        self.run_async(listener.stream(self.Req({"Host": "evil.example.com"}, "203.0.113.9"), token=listener.VIEW_TOKEN))
        self.assertEqual((listener._public_streams.total(), listener._private_streams.total()), (1, 0))

    def test_the_private_pool_has_its_own_per_visitor_cap_and_size(self):
        view = listener.VIEW_TOKEN
        lan = self.Req({"Host": "192.168.7.183:8420"}, "192.168.7.50")
        self.run_async(listener.stream(lan, token=view))
        self.run_async(listener.stream(lan, token=view))
        with self.assertRaises(listener.HTTPException) as caught:
            self.run_async(listener.stream(lan, token=view))
        self.assertEqual(caught.exception.status_code, 429)

    def test_status_reports_how_full_each_pool_is_without_naming_anyone(self):
        self.run_async(listener.stream(self.tunnel_request(23), token=listener.VIEW_TOKEN))
        shown = listener._stream_pools_status()
        self.assertEqual(shown, {"public": {"open": 1, "capacity": 4}, "private": {"open": 0, "capacity": 3}})
        self.assertNotIn("198", repr(shown))
        self.assertIn('"stream_pools": _stream_pools_status()', Path(listener.__file__).read_text(encoding="utf-8"))

    def test_the_bad_token_is_refused_before_any_slot_is_counted(self):
        with self.assertRaises(listener.HTTPException) as caught:
            self.run_async(listener.stream(self.tunnel_request(23), token="nope"))
        self.assertEqual(caught.exception.status_code, 401)
        self.assertTrue(self.pools_empty())

    # --- a stream gives its slot back however it ends, and a client that stops reading is cut off ------------------------------------------------
    def test_a_stream_gives_its_slot_back_when_its_response_ends_normally(self):
        response = self.run_async(listener.stream(self.tunnel_request(23), token=listener.VIEW_TOKEN))
        self.assertFalse(self.pools_empty())
        sent = []

        async def send(message):
            sent.append(message)

        self.run_async(response.stream_response(send))
        self.assertTrue(self.pools_empty())

    def test_a_response_that_dies_before_its_generator_ever_started_still_gives_its_slot_back(self):
        # The client vanishes while the headers are going out, so the generator (and its `finally`) never runs.
        response = self.run_async(listener.stream(self.tunnel_request(23), token=listener.VIEW_TOKEN))

        async def send(message):
            raise ConnectionResetError("client gone")

        with self.assertRaises(ConnectionResetError):
            self.run_async(response.stream_response(send))
        self.assertTrue(self.pools_empty())

    def test_a_client_that_stops_reading_is_cut_off_and_its_slot_comes_back(self):
        listener.STREAM_SEND_TIMEOUT_SECONDS = 0.05
        response = self.run_async(listener.stream(self.tunnel_request(23), token=listener.VIEW_TOKEN))

        async def send(message):
            await listener.asyncio.sleep(5)                              # a socket whose client never reads: the send never completes

        import time
        started = time.monotonic()
        with self.assertRaises(listener.asyncio.TimeoutError):
            self.run_async(response.stream_response(send))
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(self.pools_empty())

    def test_a_stalled_client_holds_at_most_its_own_cap_of_slots(self):
        # A stalled stream must not leave its visitor free to open more: the slot stays held until the response is over.
        view = listener.VIEW_TOKEN
        request = self.tunnel_request(23)
        for _ in range(2):
            self.run_async(listener.stream(request, token=view))          # opened and never run: their clients are "stalled"
        with self.assertRaises(listener.HTTPException) as caught:
            self.run_async(listener.stream(request, token=view))
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(listener._public_streams.total(), 2)

    def test_the_response_class_is_used_and_nothing_ages_a_slot_out(self):
        source = Path(listener.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
        stream_source = source.split("async def stream(")[1].split("HEALTH_LOCK_WAIT_SECONDS")[0]
        self.assertIn("return SseResponse(", stream_source)
        self.assertIn("pool.release(handle)", stream_source)
        self.assertIn("handle.evicted", stream_source)
        self.assertNotIn(".touch(", source)
        guard_source = Path(listener.__file__).with_name("guard.py").read_text(encoding="utf-8")
        self.assertNotIn("stale", guard_source.split("class StreamPool")[1].split("class VisitorRate")[0].lower())

    def test_uvicorn_is_told_not_to_rewrite_the_peer_address_from_forwarding_headers(self):
        # By default uvicorn replaces the peer address with X-Forwarded-For for loopback peers; the listener reads Cloudflare's headers itself.
        folder = Path(listener.__file__).resolve().parent.parent
        self.assertIn('"--no-proxy-headers"', folder.joinpath("docker-compose.yml").read_text(encoding="utf-8"))
        self.assertIn('"--no-proxy-headers"', folder.joinpath("Dockerfile").read_text(encoding="utf-8"))

    def test_the_settings_are_in_the_compose_file_with_their_defaults(self):
        compose = Path(listener.__file__).resolve().parent.parent.joinpath("docker-compose.yml").read_text(encoding="utf-8")
        for variable, default in (("WATCHDOG_MAX_STREAMS", "32"), ("WATCHDOG_MAX_PRIVATE_STREAMS", "16"), ("WATCHDOG_MAX_STREAMS_PER_VISITOR", "8"), ("WATCHDOG_MAX_STATUS_PER_MINUTE", "60"),
                                  ("WATCHDOG_STREAM_SEND_TIMEOUT_SECONDS", "30"), ("WATCHDOG_TRUSTED_PROXY_CIDRS", "172.16.0.0/12,127.0.0.1/32")):
            self.assertIn(f"{variable}: ${{{variable}:-{default}}}", compose)
        self.assertEqual(listener.MAX_STREAMS_PER_VISITOR, 8)
        self.assertEqual(listener.MAX_STATUS_PER_MINUTE, 60)
        self.assertEqual(listener.STREAM_SEND_TIMEOUT_SECONDS, 30)


class DoNotIndexTests(unittest.TestCase):
    """The dashboard is public on purpose (the owner's decision on a review item), but it should not be LISTED: nothing told search engines to leave it alone."""

    def test_every_answer_carries_the_do_not_index_header(self):
        headers = dict(listener.SECURITY_HEADERS)
        self.assertEqual(headers[b"x-robots-tag"], b"noindex, nofollow, noarchive, nosnippet")
        added = dict(listener._with_security_headers([]))
        self.assertEqual(added[b"x-robots-tag"], b"noindex, nofollow, noarchive, nosnippet")

    def test_an_answer_that_already_sets_it_keeps_its_own_value(self):
        kept = dict(listener._with_security_headers([(b"x-robots-tag", b"all")]))
        self.assertEqual(kept[b"x-robots-tag"], b"all")

    def test_robots_txt_asks_every_crawler_to_stay_out_and_needs_no_key(self):
        import asyncio
        reply = asyncio.run(listener.robots())
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.media_type, "text/plain")
        self.assertEqual(reply.content, "User-agent: *\nDisallow: /\n")
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn('@app.get("/robots.txt")\nasync def robots()', source.replace("\r\n", "\n"))        # no dependencies=[...]: it carries no data

    def test_the_service_worker_does_not_cache_robots_txt_as_part_of_the_app(self):
        # robots.txt is for crawlers, not for the installed app's offline shell.
        self.assertNotIn("robots", listener.SERVICE_WORKER_JS)


class ConfiguredUrlTests(unittest.TestCase):
    """The phone-push and dead-man's-switch addresses come from the settings file and are opened with urllib, which also opens file: and ftp: addresses.
    A mistyped or hostile value must never be opened (the static scan, bandit B310, flagged both places)."""

    def test_only_http_and_https_addresses_with_a_host_are_accepted(self):
        for good in ("https://ntfy.sh/clarix-wd-abc", "http://192.168.7.5:8080/topic", "  https://hc-ping.com/uuid  "):
            with self.subTest(good=good):
                self.assertEqual(listener._checked_web_url("X", good), good.strip())

    def test_everything_else_is_dropped_and_the_value_is_not_logged(self):
        messages = []
        saved = listener.logger.error
        listener.logger.error = lambda message, *args: messages.append(message % args if args else message)
        try:
            for bad in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "data:text/plain,hi", "https://", "http:///path", "ntfy.sh/topic",
                        "//ntfy.sh/topic", "https:ntfy.sh", "gopher://x", "https://a b/c", "https://host/\nInjected: header"):
                with self.subTest(bad=bad):
                    self.assertEqual(listener._checked_web_url("WATCHDOG_PUSH_URL", bad), "")
        finally:
            listener.logger.error = saved
        self.assertTrue(messages and all("WATCHDOG_PUSH_URL" in m for m in messages))
        self.assertFalse(any("passwd" in m or "Injected" in m for m in messages), "the rejected value must not be echoed into the log")

    def test_an_empty_setting_stays_off_quietly(self):
        self.assertEqual(listener._checked_web_url("X", ""), "")
        self.assertEqual(listener._checked_web_url("X", "   "), "")

    def test_the_functions_that_open_them_refuse_a_bad_address_even_if_called_directly(self):
        self.assertFalse(listener.ping_deadman("file:///etc/passwd"))
        saved = listener.PUSH_URL
        listener.PUSH_URL = "file:///etc/passwd"
        try:
            listener.send_push_notice("t", "m")                     # must return quietly without opening anything
        finally:
            listener.PUSH_URL = saved

    def test_the_settings_are_read_through_the_check_and_the_opens_are_annotated_for_the_scanner(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn('PUSH_URL = _checked_web_url("WATCHDOG_PUSH_URL", os.environ.get("WATCHDOG_PUSH_URL", ""))', source)
        self.assertIn('DEADMAN_URL = _checked_web_url("WATCHDOG_DEADMAN_URL", os.environ.get("WATCHDOG_DEADMAN_URL", ""))', source)
        self.assertEqual(source.count("# nosec B310"), 5)      # the push, the dead man's switch, the standby's probe of Core, the "Standby alive" ping and the panel's https-only call to the other node


class ForwardedHeaderTrustTests(unittest.TestCase):
    """Standby spec 3.4. Behind Cloudflare the listener learns who is asking and whether the post was encrypted from Cloudflare's headers. The standby sits
    behind the VPS's own Caddy instead, which writes X-Forwarded-For and X-Forwarded-Proto. Those are believed ONLY from the one peer named in
    WATCHDOG_TRUST_FORWARDED_FROM (default empty: off, so Core is unchanged), and while it is set Cloudflare's headers are never believed at all."""

    TRUST = guard.parse_forwarded_trust("127.0.0.1")
    HOST = "standby.corp.example"

    class FakeRequest:
        def __init__(self, headers, peer="127.0.0.1", scope_scheme="http"):
            self.headers = {k.lower(): v for k, v in headers.items()}
            self.client = types.SimpleNamespace(host=peer)
            self.scope = {"scheme": scope_scheme}

    def setUp(self):
        self._saved = (listener.FORWARDED_TRUST, listener.PUBLIC_HOST)
        listener.ROUTES.clear()

    def tearDown(self):
        listener.FORWARDED_TRUST, listener.PUBLIC_HOST = self._saved
        listener.ROUTES.clear()

    # ---- the setting ----
    def test_it_is_off_by_default_and_parsed_when_given(self):
        self.assertEqual(guard.parse_forwarded_trust(""), ())
        self.assertEqual(guard.parse_forwarded_trust(None), ())
        self.assertEqual(len(guard.parse_forwarded_trust("127.0.0.1, 10.0.0.0/8")), 2)

    def test_a_bad_entry_refuses_to_start(self):
        for bad in ("not-an-address", "127.0.0.1, banana", "300.1.1.1"):
            with self.subTest(bad=bad):
                with self.assertRaises(guard.KeyConfigError):
                    guard.parse_forwarded_trust(bad)
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn('FORWARDED_TRUST = parse_forwarded_trust(os.environ.get("WATCHDOG_TRUST_FORWARDED_FROM"))', source)
        self.assertIn("invalid forwarded-header trust setting", source)

    def test_the_listener_really_refuses_to_start_with_a_bad_setting(self):
        saved_module = sys.modules.pop("app.main")
        os.environ["WATCHDOG_TRUST_FORWARDED_FROM"] = "banana"
        try:
            with self.assertRaises(RuntimeError) as caught:
                importlib.import_module("app.main")
            self.assertIn("forwarded-header trust", str(caught.exception))
        finally:
            os.environ.pop("WATCHDOG_TRUST_FORWARDED_FROM", None)
            sys.modules.pop("app.main", None)
            sys.modules["app.main"] = saved_module

    def test_only_the_named_peer_is_trusted(self):
        self.assertTrue(guard.forwarded_peer_trusted("127.0.0.1", self.TRUST))
        for peer in ("10.0.0.5", "192.168.7.183", "::1", "", None, "garbage"):
            with self.subTest(peer=peer):
                self.assertFalse(guard.forwarded_peer_trusted(peer, self.TRUST))
        self.assertFalse(guard.forwarded_peer_trusted("127.0.0.1", ()), "empty trust list = off")

    # ---- who is asking ----
    def test_the_visitor_is_the_forwarded_address_but_only_from_the_trusted_peer(self):
        headers = {"x-forwarded-for": "203.0.113.9"}
        self.assertEqual(guard.visitor_key(headers, "127.0.0.1", forwarded_trust=self.TRUST), "xff:203.0.113.9")
        self.assertEqual(guard.visitor_key(headers, "10.1.2.3", forwarded_trust=self.TRUST), "peer:10.1.2.3")
        self.assertEqual(guard.visitor_key(headers, "127.0.0.1"), "peer:127.0.0.1", "setting off: unchanged")

    def test_the_address_the_proxy_added_is_the_one_believed(self):
        # Caddy overwrites the header, but if it ever appended, the RIGHT-most entry is the proxy's own observation; the left ones are the client's claim
        self.assertEqual(guard.visitor_key({"x-forwarded-for": "6.6.6.6, 203.0.113.9"}, "127.0.0.1", forwarded_trust=self.TRUST), "xff:203.0.113.9")

    def test_odd_forwarded_values_fall_back_to_the_peer(self):
        for value in ("", "banana", "999.1.1.1", "  ", "203.0.113.9:8080"):
            with self.subTest(value=value):
                self.assertEqual(guard.visitor_key({"x-forwarded-for": value}, "127.0.0.1", forwarded_trust=self.TRUST), "peer:127.0.0.1")

    def test_an_ipv6_visitor_is_one_visitor_per_64(self):
        a = guard.visitor_key({"x-forwarded-for": "2001:db8:1:2::1"}, "127.0.0.1", forwarded_trust=self.TRUST)
        b = guard.visitor_key({"x-forwarded-for": "2001:db8:1:2:ffff::9"}, "127.0.0.1", forwarded_trust=self.TRUST)
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("xff:"))

    def test_while_the_setting_is_on_cloudflare_headers_are_never_believed(self):
        headers = {"cf-connecting-ip": "198.51.100.7", "cf-ray": "abc", "x-forwarded-for": "203.0.113.9"}
        self.assertEqual(guard.visitor_key(headers, "127.0.0.1", forwarded_trust=self.TRUST), "xff:203.0.113.9")
        only_cf = {"cf-connecting-ip": "198.51.100.7", "cf-ray": "abc"}
        self.assertEqual(guard.visitor_key(only_cf, "127.0.0.1", forwarded_trust=self.TRUST), "peer:127.0.0.1")

    def test_with_the_setting_off_cloudflare_still_works_as_before(self):
        headers = {"cf-connecting-ip": "198.51.100.7", "cf-ray": "abc"}
        self.assertEqual(guard.visitor_key(headers, "172.21.0.3", gateways=frozenset({"172.21.0.1"})), "cf:198.51.100.7")

    # ---- which road, and was it encrypted ----
    def test_the_public_name_through_a_trusted_proxy_is_a_public_route_not_a_stranger(self):
        headers = {"host": self.HOST}
        self.assertEqual(guard.route_kind(headers, self.HOST, behind_trusted_proxy=True), "public-ip")
        self.assertEqual(guard.route_kind(headers, self.HOST), "other", "without a trusted proxy the public name alone is still suspicious")
        self.assertEqual(guard.route_kind({"host": "192.168.7.183:8420"}, self.HOST, behind_trusted_proxy=True), "lan")

    def test_cloudflare_headers_written_by_a_client_do_not_change_the_route_behind_the_proxy(self):
        headers = {"host": self.HOST, "cf-ray": "abc", "cf-connecting-ip": "198.51.100.7"}
        self.assertEqual(guard.route_kind(headers, self.HOST, behind_trusted_proxy=True), "public-ip")
        self.assertEqual(guard.route_kind(headers, self.HOST), "cloudflare", "on Core (behind Cloudflare) the headers still decide, as before")

    def test_a_forged_cloudflare_scheme_cannot_make_a_cleartext_post_look_encrypted_behind_the_proxy(self):
        listener.FORWARDED_TRUST = self.TRUST
        request = self.FakeRequest({"cf-visitor": '{"scheme":"https"}'}, peer="127.0.0.1", scope_scheme="http")
        self.assertEqual(listener.request_scheme(request), "http")

    def test_the_scheme_comes_from_the_proxy_only_when_the_proxy_is_trusted(self):
        listener.FORWARDED_TRUST = self.TRUST
        trusted = self.FakeRequest({"x-forwarded-proto": "https"}, peer="127.0.0.1")
        forged = self.FakeRequest({"x-forwarded-proto": "https"}, peer="10.9.9.9")
        self.assertEqual(listener.request_scheme(trusted), "https")
        self.assertEqual(listener.request_scheme(forged), "http", "an untrusted peer cannot claim its post was encrypted")

    def test_with_the_setting_off_the_scheme_logic_is_unchanged(self):
        listener.FORWARDED_TRUST = ()
        self.assertEqual(listener.request_scheme(self.FakeRequest({"x-forwarded-proto": "https"}, peer="10.9.9.9")), "https")
        self.assertEqual(listener.request_scheme(self.FakeRequest({"cf-visitor": '{"scheme":"http"}'}, peer="172.21.0.3")), "http")

    def test_a_post_through_the_proxy_is_recorded_as_an_encrypted_public_route(self):
        listener.FORWARDED_TRUST, listener.PUBLIC_HOST = self.TRUST, self.HOST
        listener._note_route(self.FakeRequest({"host": self.HOST, "x-forwarded-proto": "https", "x-forwarded-for": "203.0.113.9"}), "MAIN")
        route = listener.ROUTES.snapshot(datetime.now(timezone.utc))["MAIN"]
        self.assertEqual((route["kind"], route["protection"]), ("public-ip", "encrypted"))
        self.assertEqual(listener.ROUTES.exposed(datetime.now(timezone.utc)), [])

    def test_a_forged_claim_of_https_is_not_hidden_by_the_proxy_trust(self):
        listener.FORWARDED_TRUST, listener.PUBLIC_HOST = self.TRUST, self.HOST
        listener._note_route(self.FakeRequest({"host": self.HOST, "x-forwarded-proto": "https"}, peer="10.9.9.9"), "SITE-A")
        route = listener.ROUTES.snapshot(datetime.now(timezone.utc))["SITE-A"]
        self.assertEqual(route["kind"], "other")
        self.assertNotEqual(route["protection"], "encrypted")

    def test_the_visitor_of_a_request_follows_the_setting(self):
        listener.FORWARDED_TRUST = self.TRUST
        self.assertEqual(listener._visitor_of(self.FakeRequest({"x-forwarded-for": "203.0.113.9"})), "xff:203.0.113.9")
        listener.FORWARDED_TRUST = ()
        self.assertEqual(listener._visitor_of(self.FakeRequest({"x-forwarded-for": "203.0.113.9"})), "peer:127.0.0.1")


class StandbyRoleTests(unittest.TestCase):
    """Standby spec 3.1 to 3.3. The same listener code with WATCHDOG_ROLE=standby: it tracks everything but delivers nothing while Core is believed up,
    announces the takeover and the stand-down exactly once, and tags its mail. With no role set (Core) nothing changes."""

    T0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)

    class FakeSMTP:
        sent = []

        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def starttls(self, context=None):
            pass

        def login(self, *_a):
            pass

        def sendmail(self, _from, _to, message):
            StandbyRoleTests.FakeSMTP.sent.append(message)

    def setUp(self):
        self._saved = {name: getattr(listener, name) for name in
                       ("IS_STANDBY", "ROLE", "WATCH", "SUPPRESSED", "SMTP_HOST", "SMTP_USER", "SMTP_PASS", "ALERT_EMAIL_TO", "PUSH_URL", "DEADMAN_URL")}
        self._smtp, self._urlopen = listener.smtplib.SMTP, listener.urllib.request.urlopen
        self.FakeSMTP.sent = []
        self.pushes = []
        listener.smtplib.SMTP = self.FakeSMTP
        listener.SMTP_HOST, listener.SMTP_USER, listener.SMTP_PASS, listener.ALERT_EMAIL_TO = "smtp.test", "u", "p", "me@example.test"
        listener.PUSH_URL = "https://push.example/topic"
        test = self

        class Reply:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def opener(request, timeout=0):
            test.pushes.append(request)
            return Reply()
        listener.urllib.request.urlopen = opener
        listener.SUPPRESSED = {"email": 0, "push": 0}
        self.become(standby=True)

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(listener, name, value)
        listener.smtplib.SMTP, listener.urllib.request.urlopen = self._smtp, self._urlopen

    def become(self, standby, down=False):
        listener.ROLE = "standby" if standby else "primary"
        listener.IS_STANDBY = standby
        listener.WATCH = importlib.import_module("app.standby").CoreWatch(importlib.import_module("app.standby").WatchConfig(), self.T0, proven_at=self.T0)
        if down:
            for i in range(3):
                listener.WATCH.record_probe(False, self.T0 + timedelta(seconds=30 * i), "refused")

    def subjects(self):
        import email
        return [email.message_from_string(raw)["Subject"] for raw in self.FakeSMTP.sent]

    def deliver_one_alert(self, **kwargs):
        listener.send_alert_email("MAIN - USA", "critical", "Paused", [("Reason", "frozen")], **kwargs)
        listener.send_push_notice("Watchdog: MAIN - USA CRITICAL", "Paused/Frozen", **kwargs)

    # ---- the gate ----
    def test_a_primary_listener_delivers_exactly_as_before(self):
        self.become(standby=False)
        self.deliver_one_alert()
        self.assertEqual(self.subjects(), ["[Watchdog] MAIN - USA: critical"])
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(listener.SUPPRESSED, {"email": 0, "push": 0})

    def test_a_standby_that_believes_core_is_up_delivers_nothing_and_counts_it(self):
        self.deliver_one_alert()
        self.assertEqual((self.subjects(), self.pushes), ([], []))
        self.assertEqual(listener.SUPPRESSED, {"email": 1, "push": 1})

    def test_after_the_takeover_mail_and_push_go_out_and_say_standby(self):
        self.become(standby=True, down=True)
        self.deliver_one_alert()
        self.assertEqual(self.subjects(), ["[Watchdog] [STANDBY] MAIN - USA: critical"])
        self.assertEqual(len(self.pushes), 1)
        self.assertTrue(self.pushes[0].get_header("Title").startswith("[STANDBY]"))
        self.assertEqual(listener.SUPPRESSED, {"email": 0, "push": 0})

    def test_the_stand_down_notice_is_sent_even_though_the_gate_is_closing(self):
        self.deliver_one_alert(force=True)
        self.assertEqual(len(self.subjects()), 1)
        self.assertEqual(len(self.pushes), 1)

    # ---- the two announcements ----
    def test_the_takeover_sends_one_mail_and_one_push_listing_the_studios(self):
        self.become(standby=True)
        change = None
        for i in range(3):
            change = listener.WATCH.record_probe(False, self.T0 + timedelta(seconds=30 * i), "HTTP 502") or change
        import asyncio
        asyncio.run(listener._apply_transition(change, [("MAIN", "healthy"), ("SITE-B", "heartbeat_lost")]))
        self.assertEqual(len(self.subjects()), 1)
        self.assertIn("[STANDBY]", self.subjects()[0])
        self.assertIn("CORE", self.subjects()[0])
        self.assertEqual(len(self.pushes), 1)
        self.assertIn(b"SITE-B", self.pushes[0].data)

    def test_the_stand_down_sends_one_mail_and_one_push_and_the_gate_closes(self):
        self.become(standby=True, down=True)
        change = None
        for i in range(4):
            change = listener.WATCH.record_probe(True, self.T0 + timedelta(seconds=200 + 30 * i)) or change
        self.assertEqual(change.kind, "up")
        import asyncio
        asyncio.run(listener._apply_transition(change, [("MAIN", "healthy")]))
        self.assertEqual(len(self.subjects()), 1)
        self.assertIn("back", self.subjects()[0].lower())
        self.assertTrue(any("3 min" in raw for raw in self.FakeSMTP.sent), "the message must say how long Core was down (down at +60 s, back at +230 s)")
        self.assertEqual(len(self.pushes), 1)
        self.assertFalse(listener._delivery_allowed())

    def test_no_transition_means_no_message(self):
        import asyncio
        asyncio.run(listener._apply_transition(None, []))
        self.assertEqual((self.subjects(), self.pushes), ([], []))

    # ---- what the studios and the probe feed it ----
    def test_a_studios_primary_flag_is_recorded_on_a_standby_only(self):
        import asyncio
        asyncio.run(listener._note_primary_report("MAIN", {"hostname": "MAIN", "primary_ok": True}))
        self.assertIn("MAIN", listener.WATCH.snapshot(self.T0)["studios"])
        self.become(standby=False)
        asyncio.run(listener._note_primary_report("MAIN", {"hostname": "MAIN", "primary_ok": False}))
        self.assertEqual(listener.WATCH.snapshot(self.T0)["studios"], {}, "Core (primary) ignores the flag")

    def test_a_bad_primary_flag_is_ignored_without_breaking_the_post(self):
        import asyncio
        for value in ("yes", 1, None, [], {"a": 1}):
            with self.subTest(value=value):
                asyncio.run(listener._note_primary_report("MAIN", {"hostname": "MAIN", "primary_ok": value}))
        asyncio.run(listener._note_primary_report("MAIN", {"hostname": "MAIN"}))
        self.assertEqual(listener.WATCH.snapshot(self.T0)["studios"], {})

    def test_failed_probes_flow_through_to_one_takeover(self):
        import asyncio
        saved = listener.probe_primary
        listener.probe_primary = lambda url, timeout: (False, "HTTP 502")
        try:
            async def run():
                for i in range(4):
                    await listener.primary_watch_step(self.T0 + timedelta(seconds=30 * i), studios=[("MAIN", "healthy")])
            asyncio.run(run())
        finally:
            listener.probe_primary = saved
        self.assertEqual(len(self.subjects()), 1, "one takeover mail, not one per failed probe")
        self.assertEqual(len(self.pushes), 1)
        self.assertTrue(listener.WATCH.delivery_allowed())

    def test_the_probe_reads_cores_health_page(self):
        import http.server
        import threading

        listener.urllib.request.urlopen = self._urlopen          # setUp installed a fake for the push; this test talks to a real local server
        answers = {"/ok": (200, b'{"status":"ok","database":"ok"}'), "/busy": (200, b'{"status":"busy"}'), "/down": (503, b'{"status":"degraded"}'),
                   "/junk": (200, b"<html>Cloudflare</html>"), "/empty": (200, b"")}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                code, body = answers.get(self.path, (404, b""))
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            self.assertEqual(listener.probe_primary(base + "/ok", 3), (True, ""))
            for path in ("/busy", "/down", "/junk", "/empty", "/missing"):
                with self.subTest(path=path):
                    ok, error = listener.probe_primary(base + path, 3)
                    self.assertFalse(ok)
                    self.assertTrue(error)
        finally:
            server.shutdown()
            server.server_close()
        ok, error = listener.probe_primary("http://127.0.0.1:9/health", 2)      # nothing listens there
        self.assertFalse(ok)
        self.assertTrue(error)
        self.assertEqual(listener.probe_primary("", 2)[0], False)
        self.assertEqual(listener.probe_primary("file:///etc/passwd", 2)[0], False)

    def test_the_probe_identifies_itself_so_cloudflare_does_not_block_it(self):
        """Found on the first real day: Cloudflare answered Python's default web client with 403 'error code: 1010' while curl got 200, so the standby thought Core was down."""
        import http.server
        import threading

        listener.urllib.request.urlopen = self._urlopen
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                agent = self.headers.get("User-Agent", "")
                seen.append(agent)
                if not agent or "python-urllib" in agent.lower():
                    code, body = 403, b"error code: 1010"                       # what Cloudflare's browser-signature check says to the default Python agent
                else:
                    code, body = 200, b'{"status":"ok","database":"ok"}'
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.assertEqual(listener.probe_primary(f"http://127.0.0.1:{server.server_port}/health", 3), (True, ""))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("ClarixWatchdog-Standby/"), seen[0])
        self.assertNotIn("python", seen[0].lower())

    def test_a_blocked_probe_is_reported_with_the_http_code_it_got(self):
        import http.server
        import threading

        listener.urllib.request.urlopen = self._urlopen

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(403)
                self.send_header("Content-Length", "17")
                self.end_headers()
                self.wfile.write(b"error code: 1010")

            def log_message(self, *_a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.assertEqual(listener.probe_primary(f"http://127.0.0.1:{server.server_port}/health", 3), (False, "HTTP 403"))
        finally:
            server.shutdown()
            server.server_close()

    def test_a_fresh_standby_that_has_never_seen_core_does_not_take_over_on_failed_probes(self):
        """The exact real failure: three failed probes, no studio copies to say otherwise, and the probe had never worked."""
        self.become(standby=True)
        listener.WATCH = importlib.import_module("app.standby").CoreWatch(importlib.import_module("app.standby").WatchConfig(), self.T0)
        saved = listener.probe_primary
        listener.probe_primary = lambda url, timeout: (False, "HTTP 403")
        try:
            async def run():
                for i in range(6):
                    await listener.primary_watch_step(self.T0 + timedelta(seconds=30 * i), studios=[])
            asyncio.run(run())
        finally:
            listener.probe_primary = saved
        self.assertEqual(listener.WATCH.state, "UP")
        self.assertEqual(self.subjects(), [], "no takeover mail from a probe that has never worked")
        self.assertEqual(self.pushes, [])
        self.assertFalse(listener.WATCH.delivery_allowed())

    # ---- the proof that the probe works is remembered across restarts ----
    def with_database(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = listener.DB_PATH
        listener.DB_PATH = Path(tmp.name) / "watchdog.db"
        self.addCleanup(setattr, listener, "DB_PATH", saved)
        listener.init_db()

    def good_probe_once(self, when):
        saved = listener.probe_primary
        listener.probe_primary = lambda url, timeout: (True, "")
        try:
            asyncio.run(listener.primary_watch_step(when, studios=[]))
        finally:
            listener.probe_primary = saved

    def test_a_good_probe_is_remembered_in_the_database_and_restored_after_a_restart(self):
        self.with_database()
        std = importlib.import_module("app.standby")
        self.become(standby=True)
        now = datetime.now(timezone.utc)                                 # the proof expires after a week, so it must be dated by the real clock, not by the fixed T0
        listener.WATCH = std.CoreWatch(std.WatchConfig(), now)
        listener.PROBE_STATE["proof_saved_at"] = None
        self.good_probe_once(now)
        self.assertTrue(listener.WATCH.probe_proven(datetime.now(timezone.utc)))
        listener.WATCH = std.CoreWatch(std.WatchConfig(), datetime.now(timezone.utc))          # a restart: a brand new watch
        self.assertFalse(listener.WATCH.probe_proven(datetime.now(timezone.utc)))
        asyncio.run(listener._restore_proof())
        self.assertTrue(listener.WATCH.probe_proven(datetime.now(timezone.utc)), "the proof came back from the database")

    def test_the_proof_is_written_at_most_every_five_minutes_not_on_every_probe(self):
        self.with_database()
        std = importlib.import_module("app.standby")
        self.become(standby=True)
        now = datetime.now(timezone.utc)
        listener.WATCH = std.CoreWatch(std.WatchConfig(), now)
        listener.PROBE_STATE["proof_saved_at"] = None
        writes = []
        original = listener._write_state
        listener._write_state = lambda key, value: writes.append((key, value)) or original(key, value)
        saved = listener.probe_primary
        listener.probe_primary = lambda url, timeout: (True, "")
        try:
            async def run():
                for i in range(10):
                    await listener.primary_watch_step(now + timedelta(seconds=30 * i), studios=[])
            asyncio.run(run())
        finally:
            listener.probe_primary = saved
            listener._write_state = original
        self.assertEqual(len(writes), 1, "ten good probes in five minutes write the proof once")

    def test_a_failed_probe_never_writes_a_proof(self):
        self.with_database()
        std = importlib.import_module("app.standby")
        self.become(standby=True)
        listener.WATCH = std.CoreWatch(std.WatchConfig(), self.T0)
        listener.PROBE_STATE["proof_saved_at"] = None
        saved = listener.probe_primary
        listener.probe_primary = lambda url, timeout: (False, "HTTP 403")
        try:
            asyncio.run(listener.primary_watch_step(self.T0, studios=[]))
        finally:
            listener.probe_primary = saved
        listener.WATCH = std.CoreWatch(std.WatchConfig(), datetime.now(timezone.utc))
        asyncio.run(listener._restore_proof())
        self.assertFalse(listener.WATCH.probe_proven(datetime.now(timezone.utc)))

    def test_a_damaged_proof_in_the_database_is_ignored_not_trusted_and_not_fatal(self):
        self.with_database()
        std = importlib.import_module("app.standby")
        listener._write_state("primary_last_ok", "not a time at all")
        self.become(standby=True)
        listener.WATCH = std.CoreWatch(std.WatchConfig(), datetime.now(timezone.utc))
        asyncio.run(listener._restore_proof())
        self.assertFalse(listener.WATCH.probe_proven(datetime.now(timezone.utc)))

    def test_the_self_check_is_told_when_the_probe_is_unproven(self):
        self.with_database()
        std = importlib.import_module("app.standby")
        self.become(standby=True)
        listener.WATCH = std.CoreWatch(std.WatchConfig(), self.T0)
        listener.WATCH.record_probe(False, self.T0, "HTTP 403")
        inputs = asyncio.run(listener._gather_health_inputs(datetime.now(timezone.utc), None))
        self.assertFalse(inputs.probe_proven)
        self.assertEqual(inputs.probe_error, "HTTP 403")

    # ---- what /status says ----
    def test_status_block_for_a_primary_is_minimal_and_for_a_standby_is_complete_and_secret_free(self):
        self.become(standby=False)
        self.assertEqual(listener._standby_status(self.T0), {"role": "primary", "code": listener.CODE_FINGERPRINT})
        self.assertRegex(listener.CODE_FINGERPRINT, r"^[0-9a-f]{12}$")
        self.become(standby=True, down=True)
        listener.SUPPRESSED = {"email": 4, "push": 2}
        block = listener._standby_status(self.T0 + timedelta(seconds=100))
        self.assertEqual(block["role"], "standby")
        self.assertEqual(block["primary"]["state"], "DOWN")
        self.assertEqual(block["suppressed"], {"email": 4, "push": 2})
        text = listener.json.dumps(block)
        for forbidden in ("push.example", "smtp.test", "me@example.test", "token", "secret"):
            self.assertNotIn(forbidden, text)

    def test_the_public_view_hides_the_self_check_reasons_and_the_key_set_and_the_admin_view_shows_them(self):
        """The dashboard carries a public view key; "the email login does not work" and the key-set digest are for the admin key only."""
        self.become(standby=True)
        saved = dict(listener.HEALTH_STATE)
        listener.HEALTH_STATE.update(ok=False, problems=["the email login does not work, so alert emails could not be sent"], checked_at="2026-10-20T12:00:00+00:00", refused_recently=7)
        try:
            public = listener._standby_status(self.T0)
            admin = listener._standby_status(self.T0, detail=True)
            self.assertEqual(public["health"], {"ok": False, "checked_at": "2026-10-20T12:00:00+00:00"})
            self.assertNotIn("email login", listener.json.dumps(public))
            self.assertEqual(set(public["keys"]), {"file", "errors"})
            self.assertEqual(admin["health"]["problems"], ["the email login does not work, so alert emails could not be sent"])
            self.assertEqual(admin["health"]["refused_recently"], 7)
            self.assertIn("last_error", admin["keys"])
            self.assertNotIn("keyset", listener._sender_auth_status(self.T0))
            self.assertRegex(listener._sender_auth_status(self.T0, detail=True)["keyset"], r"^[0-9a-f]{12}$|^$")
            self.assertIn("keyset", listener._sender_auth_status(self.T0, detail=True))
        finally:
            listener.HEALTH_STATE.clear()
            listener.HEALTH_STATE.update(saved)

    def test_the_stream_never_carries_the_admin_detail(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn('"standby": _standby_status(now_dt), "control": _control_status(now_dt), "staff": _staff_public()}', source)       # the stream frame: the default (public) view of both blocks
        self.assertIn('_standby_status(now_dt, admin)', source)                  # only /status passes the admin flag, computed from the caller's key
        self.assertIn("admin = _is_admin_request(request)", source)

    def test_the_status_and_the_stream_carry_the_block_and_both_posts_feed_the_flag(self):
        source = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertIn('"standby": _standby_status(now_dt)', source)
        self.assertEqual(source.count("await _note_primary_report(source_id, payload)"), 2)      # /heartbeat and /event

    # ---- the dead man's switch belongs to Core ----
    def test_a_standby_never_pings_the_core_alive_check(self):
        listener.DEADMAN_URL = "https://hc.example/abc"
        self.assertFalse(listener.deadman_enabled())
        self.become(standby=False)
        self.assertTrue(listener.deadman_enabled())
        listener.DEADMAN_URL = ""
        self.assertFalse(listener.deadman_enabled())

    # ---- the settings ----
    def import_fresh(self, **env):
        saved_module = sys.modules.pop("app.main")
        old = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        try:
            return importlib.import_module("app.main")
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            sys.modules.pop("app.main", None)
            sys.modules["app.main"] = saved_module

    PER_SENDER = {"WATCHDOG_SENDER_AUTH": "per-sender", "WATCHDOG_SENDER_KEY_MAIN": "unit-test-main-key-0123456789abcdef"}

    def test_a_standby_refuses_to_start_unless_senders_have_keys_of_their_own(self):
        # security review: in `both` mode the shared key could post `primary_ok` in any studio's name and keep the standby quiet
        for env in ({"WATCHDOG_ROLE": "standby"}, {"WATCHDOG_ROLE": "standby", "WATCHDOG_SENDER_AUTH": "both"}):
            with self.subTest(env=env):
                with self.assertRaises(RuntimeError) as caught:
                    self.import_fresh(**env)
                self.assertIn("per-sender", str(caught.exception))
        self.assertTrue(self.import_fresh(WATCHDOG_ROLE="standby", **self.PER_SENDER).IS_STANDBY)
        self.assertEqual(self.import_fresh(WATCHDOG_ROLE="primary").ROLE, "primary", "Core (primary) in `both` mode is untouched")

    def test_the_role_defaults_to_primary_and_a_standby_gets_cores_health_page_to_watch(self):
        fresh = self.import_fresh(WATCHDOG_ROLE="standby", **self.PER_SENDER)
        self.assertEqual((fresh.ROLE, fresh.IS_STANDBY), ("standby", True))
        self.assertEqual(fresh.PRIMARY_URL, "https://watchdog.corp.example/health")
        plain = self.import_fresh(WATCHDOG_ROLE="")
        self.assertEqual((plain.ROLE, plain.IS_STANDBY, plain.PRIMARY_URL), ("primary", False, ""))

    def test_timings_come_from_settings(self):
        fresh = self.import_fresh(WATCHDOG_ROLE="standby", WATCHDOG_STANDBY_FAILURES_TO_DOWN="5", WATCHDOG_STANDBY_MIN_DOWN_SECONDS="300",
                                  WATCHDOG_STANDBY_VETO_MAX_SECONDS="900", WATCHDOG_PRIMARY_URL="https://core.example/health", **self.PER_SENDER)
        self.assertEqual((fresh.WATCH_CONFIG.down_after_failures, fresh.WATCH_CONFIG.min_down_seconds, fresh.WATCH_CONFIG.veto_max_seconds, fresh.PRIMARY_URL),
                         (5, 300, 900, "https://core.example/health"))

    def test_nonsense_settings_stop_the_start(self):
        for env in ({"WATCHDOG_ROLE": "secondary"}, {"WATCHDOG_ROLE": "standby", "WATCHDOG_PRIMARY_URL": "ftp://x/health", **self.PER_SENDER},
                    {"WATCHDOG_ROLE": "standby", "WATCHDOG_PRIMARY_URL": "https://u:p@core.example/health", **self.PER_SENDER}):
            with self.subTest(env=env):
                with self.assertRaises(RuntimeError):
                    self.import_fresh(**env)


if __name__ == "__main__":
    unittest.main(verbosity=2)
