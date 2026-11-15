# File:         test_site_c_feed_logic.py
# Purpose:      Tests of core-listener/app/site_c_feed.py: the report parser, the judgement of each Site C stream from what the playout PCs hear, the alarm
#               raise / clear steps, and the small database layer (latest reading per PC, the below-the-floor clock, the 24-hour history).
# Inputs:       None (a fake clock, an in-memory database and made-up PC reports).
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/site_c_feed.py (no network, no clock of its own).
"""Run: python ops/test_site_c_feed_logic.py

What is proven here, in plain words (the unpublished design notes): a stream is critical only when EVERY PC that can listen says it is lost (no packets for 20 s) or has been
under 1 Mbit/s for 120 s; one PC disagreeing is a yellow warning; a PC that cannot listen or has gone quiet is not a witness; an alarm is raised once per episode, clears
only after 30 s of the stream being heard again, and never repeats after a restart.
"""
import importlib
import json
import math
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

LISTENER_DIR = Path(__file__).resolve().parent.parent / "core-listener"
sys.path.insert(0, str(LISTENER_DIR))
vf = importlib.import_module("app.site_c_feed")

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
S = vf.Settings()


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def report(mbit=5.2, idle=0.0, listening=True, mbit_5005=None, idle_5005=None, error=""):
    """What parse_report returns for a normal heartbeat field: both streams, the second one defaulting to the same numbers as the first."""
    return {"listening": listening, "window_seconds": 10.0, "error": error, "streams": {
        "5004": {"mbit_per_s": mbit, "packets": int(mbit * 50), "idle_seconds": idle},
        "5005": {"mbit_per_s": mbit if mbit_5005 is None else mbit_5005, "packets": 500, "idle_seconds": idle if idle_5005 is None else idle_5005}}}


def witness(source="MAIN", mbit=5.2, idle=0.0, below_for=None, age=3.0):
    return vf.Witness(source, mbit, 100, idle, age, below_for)


def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    vf.init_tables(conn)
    return conn


class ParseReportTests(unittest.TestCase):
    def good(self):
        return {"listening": True, "window_seconds": 10.0, "error": "", "streams": {"5004": {"mbit_per_s": 5.2, "packets": 5131, "idle_seconds": 0}, "5005": {"mbit_per_s": 5.19, "packets": 5104, "idle_seconds": 0}}}

    def test_a_normal_field_is_kept_as_it_is(self):
        parsed = vf.parse_report(self.good())
        self.assertTrue(parsed["listening"])
        self.assertEqual(parsed["streams"]["5004"], {"mbit_per_s": 5.2, "packets": 5131, "idle_seconds": 0.0})
        self.assertEqual(set(parsed["streams"]), {"5004", "5005"})

    def test_a_missing_or_wrong_kind_of_field_is_ignored(self):
        for bad in (None, "x", 5, [], True, {}, {"listening": "yes", "streams": {}}, {"listening": True, "streams": []}, {"listening": True}):
            with self.subTest(bad=bad):
                self.assertIsNone(vf.parse_report(bad))

    def test_listening_must_be_a_real_true_or_false_even_when_everything_else_is_fine(self):
        for wrong in ("yes", "true", 1, 0, None):
            field = self.good()
            field["listening"] = wrong
            with self.subTest(listening=wrong):
                self.assertIsNone(vf.parse_report(field))

    def test_unknown_streams_are_dropped_and_bad_numbers_drop_only_that_stream(self):
        field = self.good()
        field["streams"]["6000"] = {"mbit_per_s": 1, "packets": 1, "idle_seconds": 0}
        field["streams"]["5005"] = {"mbit_per_s": -1, "packets": 1, "idle_seconds": 0}
        parsed = vf.parse_report(field)
        self.assertEqual(set(parsed["streams"]), {"5004"})

    def test_nan_infinity_booleans_strings_and_huge_numbers_are_refused(self):
        for key, value in (("mbit_per_s", float("nan")), ("mbit_per_s", math.inf), ("mbit_per_s", True), ("mbit_per_s", "5"), ("mbit_per_s", 1e9),
                           ("packets", -3), ("packets", 1.5), ("packets", 10 ** 12), ("idle_seconds", -1), ("idle_seconds", math.inf), ("idle_seconds", 10 ** 9)):
            with self.subTest(key=key, value=value):
                field = self.good()
                field["streams"]["5004"][key] = value
                self.assertNotIn("5004", vf.parse_report(field)["streams"])

    def test_a_pc_that_cannot_listen_keeps_its_reason_clipped(self):
        parsed = vf.parse_report({"listening": False, "error": "x" * 500, "window_seconds": 10, "streams": {}})
        self.assertFalse(parsed["listening"])
        self.assertLessEqual(len(parsed["error"]), 80)

    def test_a_bad_window_makes_the_whole_field_unusable(self):
        for window in (0, -5, 100000, "10", None):
            field = self.good()
            field["window_seconds"] = window
            with self.subTest(window=window):
                self.assertIsNone(vf.parse_report(field))


class JudgeTests(unittest.TestCase):
    def test_every_pc_hearing_it_normally_is_ok(self):
        verdict = vf.judge([witness("MAIN"), witness("BACKUP-SERVER", mbit=4.9)], S)
        self.assertEqual((verdict.level, verdict.word, verdict.kind), ("ok", "OK", None))
        self.assertEqual(set(verdict.heard_by), {"MAIN", "BACKUP-SERVER"})

    def test_nobody_listening_is_no_data_and_never_an_alarm(self):
        verdict = vf.judge([], S)
        self.assertEqual((verdict.level, verdict.word), ("no_data", "NO DATA"))

    def test_lost_needs_twenty_seconds_without_a_packet_on_every_pc(self):
        self.assertEqual(vf.judge([witness(mbit=0, idle=19.9), witness("BACKUP-SERVER", mbit=0, idle=19.9)], S).level, "warning")
        verdict = vf.judge([witness(mbit=0, idle=20.0), witness("BACKUP-SERVER", mbit=0, idle=25.0)], S)
        self.assertEqual((verdict.level, verdict.kind, verdict.word), ("critical", "lost", "LOST"))

    def test_the_floor_is_one_megabit_for_a_hundred_and_twenty_seconds(self):
        self.assertEqual(vf.judge([witness(mbit=0.99, below_for=119.9)], S).level, "warning")
        verdict = vf.judge([witness(mbit=0.99, below_for=120.0)], S)
        self.assertEqual((verdict.level, verdict.kind, verdict.word), ("critical", "floor", "BELOW 1 MBIT/S"))

    def test_exactly_one_megabit_is_not_below_the_floor(self):
        self.assertEqual(vf.classify(witness(mbit=1.0), S), "fine")
        self.assertEqual(vf.classify(witness(mbit=0.999, below_for=0.0), S), "low")

    def test_one_pc_disagreeing_is_a_yellow_warning_that_names_it(self):
        verdict = vf.judge([witness("MAIN"), witness("BACKUP-SERVER", mbit=0, idle=300)], S)
        self.assertEqual((verdict.level, verdict.word), ("warning", "WARNING"))
        self.assertIn("BACKUP-SERVER", verdict.cause)
        self.assertTrue(verdict.any_fine)

    def test_a_lost_pc_and_a_long_low_pc_together_are_critical(self):
        verdict = vf.judge([witness("MAIN", mbit=0, idle=40, below_for=200), witness("BACKUP-SERVER", mbit=0.4, below_for=150)], S)
        self.assertEqual((verdict.level, verdict.kind), ("critical", "floor"))

    def test_a_lost_pc_and_a_briefly_low_pc_are_only_a_warning(self):
        verdict = vf.judge([witness("MAIN", mbit=0, idle=40, below_for=200), witness("BACKUP-SERVER", mbit=0.4, below_for=30)], S)
        self.assertEqual(verdict.level, "warning")
        self.assertFalse(verdict.any_fine)

    def test_idle_time_wins_when_the_numbers_contradict_each_other(self):
        self.assertEqual(vf.classify(witness(mbit=5.0, idle=60), S), "lost")

    def test_the_cause_is_short_enough_for_the_staff_failure_and_says_how_long(self):
        lost = vf.judge([witness(mbit=0, idle=47)], S)
        self.assertIn("47", lost.cause)
        floor = vf.judge([witness(mbit=0.5, below_for=130)], S)
        self.assertIn("1 Mbit/s", floor.cause)
        for verdict in (lost, floor):
            self.assertLessEqual(len(verdict.cause), 80)


class AlarmStepTests(unittest.TestCase):
    def critical(self, kind="lost", cause="Stream lost: no packets for 25 s"):
        return vf.Verdict("critical", kind, "LOST", cause, ("MAIN",), False)

    def fine(self):
        return vf.Verdict("ok", None, "OK", "", ("MAIN",), True)

    def test_the_alarm_is_raised_once_and_not_again_while_it_lasts(self):
        row, action = vf.next_step(vf.empty_alert(), self.critical(), at(0), S)
        self.assertEqual((row["level"], action), ("critical", "raise"))
        for seconds in (10, 20, 30, 300):
            row, action = vf.next_step(row, self.critical(), at(seconds), S)
            self.assertIsNone(action)
            self.assertEqual(row["level"], "critical")

    def test_it_clears_only_after_thirty_seconds_of_the_stream_being_heard(self):
        row, _ = vf.next_step(vf.empty_alert(), self.critical(), at(0), S)
        row, action = vf.next_step(row, self.fine(), at(100), S)
        self.assertIsNone(action)
        row, action = vf.next_step(row, self.fine(), at(129.9), S)
        self.assertIsNone(action)
        row, action = vf.next_step(row, self.fine(), at(130), S)
        self.assertEqual((row["level"], action), ("ok", "restore"))

    def test_a_relapse_in_the_clearing_time_starts_the_thirty_seconds_again(self):
        row, _ = vf.next_step(vf.empty_alert(), self.critical(), at(0), S)
        row, _ = vf.next_step(row, self.fine(), at(100), S)
        row, action = vf.next_step(row, self.critical(), at(115), S)
        self.assertIsNone(action)
        row, _ = vf.next_step(row, self.fine(), at(120), S)
        row, action = vf.next_step(row, self.fine(), at(149), S)
        self.assertIsNone(action)
        row, action = vf.next_step(row, self.fine(), at(150), S)
        self.assertEqual(action, "restore")

    def test_a_warning_that_still_has_a_working_pc_counts_towards_clearing(self):
        row, _ = vf.next_step(vf.empty_alert(), self.critical(), at(0), S)
        mixed = vf.Verdict("warning", None, "WARNING", "BACKUP-SERVER hears nothing", ("MAIN",), True)
        row, _ = vf.next_step(row, mixed, at(50), S)
        row, action = vf.next_step(row, mixed, at(80), S)
        self.assertEqual(action, "restore")

    def test_no_data_keeps_a_critical_alarm_and_stops_the_clearing_clock(self):
        row, _ = vf.next_step(vf.empty_alert(), self.critical(), at(0), S)
        row, _ = vf.next_step(row, self.fine(), at(50), S)
        row, action = vf.next_step(row, vf.Verdict("no_data", None, "NO DATA", "", (), False), at(60), S)
        self.assertIsNone(action)
        self.assertEqual(row["level"], "critical")
        row, action = vf.next_step(row, self.fine(), at(85), S)
        self.assertIsNone(action)            # the clock restarted at 85. Had it kept running from 50 it would be 35 s old now and the alarm would clear.
        row, action = vf.next_step(row, self.fine(), at(114.9), S)
        self.assertIsNone(action)
        row, action = vf.next_step(row, self.fine(), at(115), S)
        self.assertEqual(action, "restore")

    def test_lost_turning_into_low_is_the_same_episode(self):
        row, _ = vf.next_step(vf.empty_alert(), self.critical("lost"), at(0), S)
        row, action = vf.next_step(row, self.critical("floor", "Under 1 Mbit/s for 2 min"), at(40), S)
        self.assertIsNone(action)
        self.assertEqual(row["kind"], "floor")

    def test_a_new_episode_after_clearing_raises_again(self):
        row, _ = vf.next_step(vf.empty_alert(), self.critical(), at(0), S)
        row, _ = vf.next_step(row, self.fine(), at(10), S)
        row, action = vf.next_step(row, self.fine(), at(40), S)
        self.assertEqual(action, "restore")
        row, action = vf.next_step(row, self.critical(), at(500), S)
        self.assertEqual(action, "raise")

    def test_ok_warning_and_no_data_never_raise_anything_by_themselves(self):
        for verdict in (self.fine(), vf.Verdict("warning", None, "WARNING", "x", (), False), vf.Verdict("no_data", None, "NO DATA", "", (), False)):
            row, action = vf.next_step(vf.empty_alert(), verdict, at(0), S)
            self.assertIsNone(action)
            self.assertEqual(row["level"], "ok")


class SettingsTests(unittest.TestCase):
    def test_the_defaults_are_the_owners_numbers(self):
        self.assertEqual((S.lost_after_seconds, S.floor_mbit, S.floor_seconds, S.clear_seconds), (20.0, 1.0, 120.0, 30.0))

    def test_the_group_and_the_usual_rate_default_to_the_encoders_numbers(self):
        self.assertEqual((S.group, S.expected_mbit), ("224.1.1.1", 5.0))

    def test_the_group_must_be_an_ipv4_multicast_address_or_the_default_is_kept(self):
        for text, expected in (("239.10.10.10", "239.10.10.10"), (" 224.1.1.1 ", "224.1.1.1"), ("192.168.7.166", "224.1.1.1"), ("banana", "224.1.1.1"), ("", "224.1.1.1"),
                               ("ff02::1", "224.1.1.1"), ("224.1.1.1:5004", "224.1.1.1"), ("0.0.0.0", "224.1.1.1")):
            with self.subTest(text=text):
                self.assertEqual(vf.settings_from_environ({"WATCHDOG_SITE_C_GROUP": text}).group, expected)

    def test_the_usual_rate_comes_from_the_environment_and_bad_values_fall_back(self):
        self.assertEqual(vf.settings_from_environ({"WATCHDOG_SITE_C_EXPECTED_MBIT": "4.2"}).expected_mbit, 4.2)
        for bad in ("0", "-1", "nan", "inf", "fast"):
            with self.subTest(bad=bad):
                self.assertEqual(vf.settings_from_environ({"WATCHDOG_SITE_C_EXPECTED_MBIT": bad}).expected_mbit, 5.0)

    def test_settings_come_from_the_environment_and_bad_values_fall_back(self):
        env = {"WATCHDOG_SITE_C_LOST_AFTER_SECONDS": "30", "WATCHDOG_SITE_C_FLOOR_MBIT": "2.5", "WATCHDOG_SITE_C_FLOOR_SECONDS": "banana", "WATCHDOG_SITE_C_CLEAR_SECONDS": "-4"}
        settings = vf.settings_from_environ(env)
        self.assertEqual((settings.lost_after_seconds, settings.floor_mbit, settings.floor_seconds, settings.clear_seconds), (30.0, 2.5, 120.0, 30.0))


class StorageTests(unittest.TestCase):
    def feed(self, conn, source, seconds, **kwargs):
        vf.ingest_locked(conn, source, report(**kwargs), at(seconds), S)

    def test_the_below_floor_clock_starts_at_the_first_low_reading_and_stops_at_a_good_one(self):
        conn = database()
        self.feed(conn, "MAIN", 0, mbit=5.0)
        self.feed(conn, "MAIN", 10, mbit=0.5)
        self.feed(conn, "MAIN", 20, mbit=0.5)
        row = conn.execute("SELECT below_since_utc FROM site_c_feed_latest WHERE source_id='MAIN' AND stream='5004'").fetchone()
        self.assertEqual(row["below_since_utc"], at(10).isoformat())
        self.feed(conn, "MAIN", 30, mbit=2.0)
        row = conn.execute("SELECT below_since_utc FROM site_c_feed_latest WHERE source_id='MAIN' AND stream='5004'").fetchone()
        self.assertIsNone(row["below_since_utc"])

    def test_a_gap_in_the_reports_restarts_the_clock_because_nobody_saw_what_happened(self):
        conn = database()
        self.feed(conn, "MAIN", 0, mbit=0.5)
        self.feed(conn, "MAIN", 200, mbit=0.5)          # no report for 200 s: longer than the 60 s a witness stays fresh
        row = conn.execute("SELECT below_since_utc FROM site_c_feed_latest WHERE source_id='MAIN' AND stream='5004'").fetchone()
        self.assertEqual(row["below_since_utc"], at(200).isoformat())

    def test_floor_alarm_is_raised_by_the_reading_at_one_hundred_and_twenty_seconds(self):
        conn = database()
        raised = []
        for seconds in range(0, 131, 10):
            self.feed(conn, "MAIN", seconds, mbit=0.5, idle=0.0)
            self.feed(conn, "BACKUP-SERVER", seconds, mbit=0.5, idle=0.0)
            for action in vf.evaluate_locked(conn, at(seconds), S):
                raised.append((seconds, action["action"], action["stream"], action["kind"]))
        self.assertEqual(raised, [(120, "raise", "5004", "floor"), (120, "raise", "5005", "floor")])

    def test_lost_alarm_is_raised_once_and_cleared_once_and_a_restart_repeats_nothing(self):
        conn = database()
        log = []
        timeline = [(0, 5.2, 0), (10, 5.2, 0), (20, 0, 10), (30, 0, 20), (40, 0, 30), (50, 0, 40), (60, 5.2, 0), (70, 5.2, 0), (80, 5.2, 0), (90, 5.2, 0), (100, 5.2, 0)]
        for seconds, mbit, idle in timeline:
            for source in ("MAIN", "BACKUP-SERVER"):
                self.feed(conn, source, seconds, mbit=mbit, idle=idle)
            log += [(seconds, a["action"], a["stream"]) for a in vf.evaluate_locked(conn, at(seconds), S)]
        self.assertEqual(log, [(30, "raise", "5004"), (30, "raise", "5005"), (90, "restore", "5004"), (90, "restore", "5005")])
        self.assertEqual(vf.evaluate_locked(conn, at(105), S), [])        # a restart evaluates the same stored state again: nothing is repeated

    def test_the_two_streams_are_judged_independently(self):
        conn = database()
        for seconds in (0, 10, 20, 30):
            for source in ("MAIN", "BACKUP-SERVER"):
                self.feed(conn, source, seconds, mbit=5.2, idle=0, mbit_5005=0, idle_5005=seconds + 10)
        snapshot = vf.snapshot(conn, at(30), S)
        levels = {entry["stream"]: entry["level"] for entry in snapshot["streams"]}
        self.assertEqual(levels, {"5004": "ok", "5005": "critical"})

    def test_a_stale_pc_and_a_pc_that_cannot_listen_are_not_witnesses(self):
        conn = database()
        self.feed(conn, "MAIN", 0, mbit=5.2)                                  # fine, but its last report is 100 s old at the time of judging
        vf.ingest_locked(conn, "BACKUP-SERVER", report(mbit=0, idle=300), at(100), S)
        verdicts = {e["stream"]: e["level"] for e in vf.snapshot(conn, at(100), S)["streams"]}
        self.assertEqual(verdicts["5004"], "critical")                        # judged on BACKUP-SERVER alone
        vf.ingest_locked(conn, "BACKUP-SERVER", report(listening=False, error="join failed"), at(110), S)
        verdicts = {e["stream"]: e["level"] for e in vf.snapshot(conn, at(110), S)["streams"]}
        self.assertEqual(verdicts["5004"], "no_data")

    def test_the_snapshot_has_the_panel_data_and_names_no_machine(self):
        conn = database()
        for seconds in range(0, 200, 10):
            self.feed(conn, "MAIN", seconds, mbit=5.2)
        snapshot = vf.snapshot(conn, at(190), S)
        entry = snapshot["streams"][0]
        for key in ("stream", "level", "word", "cause", "mbit_per_s", "heard_by", "witnesses", "history", "alarm", "role", "label", "address", "live"):
            self.assertIn(key, entry)
        self.assertEqual(entry["heard_by"], ["MAIN"])
        self.assertGreater(len(entry["history"]), 0)
        self.assertEqual(snapshot["floor_mbit"], 1.0)
        # 2026-11-08, at the owner's request: the multicast address of each stream IS shown (udp://@224.1.1.1:5005). Nothing else that names a machine may appear: not
        # the encoder, not a playout PC (the page can be opened from outside the studio).
        text = json.dumps(snapshot)
        for forbidden in ("192.168", "10.0.", "172.16"):
            self.assertNotIn(forbidden, text)

    def test_before_any_report_the_snapshot_says_it_is_not_reporting_yet(self):
        snapshot = vf.snapshot(database(), at(0), S)
        self.assertFalse(snapshot["reporting"])
        self.assertEqual({e["level"] for e in snapshot["streams"]}, {"no_data"})

    def test_history_older_than_eight_days_is_pruned_and_newer_is_kept(self):
        conn = database()
        self.feed(conn, "MAIN", 0, mbit=5.2)
        vf.ingest_locked(conn, "MAIN", report(mbit=5.2), at(0) + timedelta(days=9), S)
        removed = vf.prune_locked(conn, at(0) + timedelta(days=9, seconds=60))
        self.assertGreater(removed, 0)
        remaining = conn.execute("SELECT COUNT(*) FROM site_c_feed_minutes").fetchone()[0]
        self.assertGreater(remaining, 0)


class MainAndBackupTests(unittest.TestCase):
    """2026-11-08, the owner's words: "main stream :5005 while backup stream is :5004". The panel groups the two streams under those names, main first."""

    def test_5005_is_the_main_stream_and_5004_the_backup_stream_and_main_comes_first(self):
        streams = vf.snapshot(database(), at(0), S)["streams"]
        self.assertEqual([(e["stream"], e["role"], e["label"]) for e in streams], [("5005", "main", "Main stream"), ("5004", "backup", "Backup stream")])

    def test_each_stream_carries_its_udp_address(self):
        streams = vf.snapshot(database(), at(0), S)["streams"]
        self.assertEqual({e["stream"]: e["address"] for e in streams}, {"5005": "udp://@224.1.1.1:5005", "5004": "udp://@224.1.1.1:5004"})

    def test_the_address_follows_the_configured_group(self):
        settings = vf.settings_from_environ({"WATCHDOG_SITE_C_GROUP": "239.1.2.3"})
        streams = vf.snapshot(database(), at(0), settings)["streams"]
        self.assertEqual({e["stream"]: e["address"] for e in streams}, {"5005": "udp://@239.1.2.3:5005", "5004": "udp://@239.1.2.3:5004"})

    def test_the_alarm_titles_say_which_stream_and_which_port(self):
        self.assertEqual(vf.stream_title("5005"), "Site C ZIXI Feed main stream (UDP 5005)")
        self.assertEqual(vf.stream_title("5004"), "Site C ZIXI Feed backup stream (UDP 5004)")

    def test_the_evaluation_order_and_the_judgement_do_not_change_with_the_display_order(self):
        conn = database()
        for seconds in (0, 10, 20, 30):
            for source in ("MAIN", "BACKUP-SERVER"):
                vf.ingest_locked(conn, source, report(mbit=5.2, idle=0, mbit_5005=0, idle_5005=seconds + 10), at(seconds), S)
        self.assertEqual({e["stream"]: e["level"] for e in vf.snapshot(conn, at(30), S)["streams"]}, {"5004": "ok", "5005": "critical"})


class LiveDataTests(unittest.TestCase):
    """What the opened panel draws in real time: the last ten minutes at ten-second steps, and the packet details of each PC's latest reading."""

    def feed(self, conn, source, seconds, mbit=5.0, window=10.0, **kwargs):
        value = report(mbit=mbit, **kwargs)
        value["window_seconds"] = window
        vf.ingest_locked(conn, source, value, at(seconds), S)

    def live(self, conn, seconds, stream="5004"):
        return {e["stream"]: e for e in vf.snapshot(conn, at(seconds), S)["streams"]}[stream]["live"]

    def test_the_snapshot_says_how_long_and_how_fine_the_live_chart_is(self):
        snapshot = vf.snapshot(database(), at(0), S)
        self.assertEqual((snapshot["live_seconds"], snapshot["live_step_seconds"], snapshot["expected_mbit"], snapshot["group"]), (600, 10, 5.0, "224.1.1.1"))

    def test_one_point_per_ten_seconds_oldest_first_each_with_its_time(self):
        conn = database()
        for seconds in range(0, 61, 10):
            self.feed(conn, "MAIN", seconds, mbit=5.0)
        points = self.live(conn, 60)
        self.assertEqual([p["t"] for p in points], [at(s).isoformat() for s in range(0, 61, 10)])
        self.assertEqual({p["mbit"] for p in points}, {5.0})

    def test_when_two_pcs_report_in_one_step_the_better_one_is_drawn_not_the_average(self):
        conn = database()
        self.feed(conn, "MAIN", 0, mbit=3.0)
        self.feed(conn, "BACKUP-SERVER", 5, mbit=5.0)
        self.assertEqual([p["mbit"] for p in self.live(conn, 9)], [5.0])

    def test_a_step_with_one_pc_is_that_pcs_reading_and_a_silent_stream_draws_zero(self):
        conn = database()
        self.feed(conn, "MAIN", 0, mbit=4.5)
        self.feed(conn, "MAIN", 10, mbit=0.0, idle=12)
        self.assertEqual([p["mbit"] for p in self.live(conn, 15)], [4.5, 0.0])

    def test_only_the_last_ten_minutes_are_returned(self):
        conn = database()
        self.feed(conn, "MAIN", 0)
        self.feed(conn, "MAIN", 300)
        self.feed(conn, "MAIN", 700)
        self.assertEqual([p["t"] for p in self.live(conn, 700)], [at(300).isoformat(), at(700).isoformat()])

    def test_old_readings_are_removed_from_the_table_as_new_ones_arrive(self):
        conn = database()
        self.feed(conn, "MAIN", 0)
        self.feed(conn, "MAIN", 700)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM site_c_feed_recent WHERE taken_utc < ?", (at(40).isoformat(),)).fetchone()[0], 0)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM site_c_feed_recent").fetchone()[0], 0)

    def test_a_pc_that_cannot_listen_adds_nothing_to_the_live_line(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", report(listening=False, error="join failed"), at(0), S)
        self.assertEqual(self.live(conn, 5), [])

    def test_each_witness_shows_its_packets_in_the_window_and_the_packets_per_second(self):
        conn = database()
        self.feed(conn, "MAIN", 0, mbit=5.2, window=10.0)
        stream = {e["stream"]: e for e in vf.snapshot(conn, at(3), S)["streams"]}["5005"]
        entry = stream["witnesses"][0]
        self.assertEqual((entry["source"], entry["packets"], entry["window_seconds"], entry["packets_per_s"]), ("MAIN", 500, 10.0, 50))

    def test_a_reading_that_has_no_window_yet_still_shows_without_the_extra_figures(self):
        """A listener upgraded while a PC's reading is stored from before the new table existed: the panel must not break on the missing figures."""
        conn = database()
        self.feed(conn, "MAIN", 0, mbit=5.2)
        conn.execute("DELETE FROM site_c_feed_recent")
        entry = {e["stream"]: e for e in vf.snapshot(conn, at(3), S)["streams"]}["5004"]["witnesses"][0]
        self.assertEqual(entry["source"], "MAIN")
        self.assertNotIn("packets_per_s", entry)

    def test_the_live_and_detail_figures_name_no_machine(self):
        conn = database()
        self.feed(conn, "MAIN", 0)
        self.assertNotIn("192.168", json.dumps(vf.snapshot(conn, at(3), S)))


def raw_field(seconds=None, **kwargs):
    """The `site_c_feed` field as a PC sends it (JSON numbers), with the per-second samples added when given. A sample is [age, span, mbit, packets]."""
    field = {"listening": True, "window_seconds": 10.0, "error": "", "streams": {
        "5004": {"mbit_per_s": 5.0, "packets": 500, "idle_seconds": 0}, "5005": {"mbit_per_s": 5.0, "packets": 500, "idle_seconds": 0}}}
    field.update(kwargs)
    if seconds is not None:
        field["seconds"] = seconds
    return field


def sample(age=0.0, span=1.0, mbit=5.0, packets=480):
    return [age, span, mbit, packets]


def fine_report(samples_5004, samples_5005=None, **kwargs):
    """What parse_report returns for a heartbeat field that carries per-second samples (both streams)."""
    return vf.parse_report(raw_field({"5004": samples_5004, "5005": samples_5004 if samples_5005 is None else samples_5005}, **kwargs))


class ParseSecondsTests(unittest.TestCase):
    """the unpublished design notes, "The per-second readings": the PCs add the last seconds' readings to the heartbeat; a bad sample never costs the heartbeat or the ten-second figures."""

    def test_good_samples_are_kept_oldest_first_with_named_parts(self):
        parsed = vf.parse_report(raw_field({"5004": [sample(0.0, 1.0, 5.012, 481), sample(1.0, 1.0, 4.998, 480), sample(2.5, 1.5, 5.1, 700)]}))
        self.assertEqual(parsed["seconds"]["5004"], [
            {"age": 2.5, "span": 1.5, "mbit_per_s": 5.1, "packets": 700},
            {"age": 1.0, "span": 1.0, "mbit_per_s": 4.998, "packets": 480},
            {"age": 0.0, "span": 1.0, "mbit_per_s": 5.012, "packets": 481}])
        self.assertEqual(parsed["seconds"].get("5005", []), [])

    def test_the_ten_second_figures_are_exactly_what_they_were(self):
        with_seconds, without = vf.parse_report(raw_field({"5004": [sample()]})), vf.parse_report(raw_field())
        self.assertEqual({k: with_seconds[k] for k in ("listening", "window_seconds", "error", "streams")}, {k: without[k] for k in ("listening", "window_seconds", "error", "streams")})
        self.assertEqual(without["seconds"], {})

    def test_a_missing_or_wrongly_shaped_seconds_field_costs_only_the_seconds(self):
        for bad in (None, "x", 7, [], True, {"5004": "x"}, {"5004": 5}, {"5004": {"a": 1}}, {"9999": [sample()]}, {"5004": [[]]}):
            with self.subTest(bad=bad):
                parsed = vf.parse_report(raw_field(bad))
                self.assertIsNotNone(parsed, "the heartbeat's own figures must survive")
                self.assertEqual(parsed["seconds"], {})
                self.assertEqual(parsed["streams"]["5004"]["mbit_per_s"], 5.0)

    def test_a_bad_sample_is_dropped_on_its_own_and_the_good_ones_are_kept(self):
        nan = float("nan")
        bad = [[1, 1, "5", 10], [2, 1, True, 10], [3, 1, nan, 10], [4, 1, -1, 10], [5, 0, 5, 10], [6, 61, 5, 10], [3601, 1, 5, 10], [-1, 1, 5, 10],
               [7, 1, 10001, 10], [8, 1, 5, 1.5], [9, 1, 5], [10, 1, 5, 10, 99], "junk", None, {"age": 1}, [11, 1, 5, -3], [12, 1, 5, 2_000_000_000], [13, 1, float("inf"), 10]]
        kept = vf.parse_report(raw_field({"5004": bad + [sample(0.0, 1.0, 5.0, 480)]}))["seconds"]["5004"]
        self.assertEqual(kept, [{"age": 0.0, "span": 1.0, "mbit_per_s": 5.0, "packets": 480}])

    def test_the_bounds_are_inclusive_where_they_should_be(self):
        edges = [sample(0, 0.05, 0, 0), sample(3600, 60, 10000, 1_000_000_000)]
        self.assertEqual(len(vf.parse_report(raw_field({"5004": edges}))["seconds"]["5004"]), 2)

    def test_at_most_thirty_samples_a_stream_and_they_are_the_newest(self):
        parsed = vf.parse_report(raw_field({"5004": [sample(float(age)) for age in range(45)]}))["seconds"]["5004"]
        self.assertEqual(len(parsed), 30)
        self.assertEqual((parsed[0]["age"], parsed[-1]["age"]), (29.0, 0.0))

    def test_a_pc_that_cannot_listen_keeps_no_samples_even_if_it_sent_some(self):
        self.assertEqual(vf.parse_report(raw_field({"5004": [sample()]}, listening=False))["seconds"], {})


class StoreSecondsTests(unittest.TestCase):
    """The new small table: one row per PC, stream and second, the second worked out from when Core received the heartbeat (a PC's own clock is never trusted)."""

    def rows(self, conn, stream="5004"):
        return [dict(r) for r in conn.execute("SELECT second_utc, source_id, mbit_per_s, packets, span_seconds FROM site_c_feed_seconds WHERE stream = ? ORDER BY second_utc, source_id", (stream,))]

    def test_a_sample_is_filed_under_the_second_it_ended_in_by_the_listeners_clock(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", fine_report([sample(3.0, 1.0, 5.1, 480), sample(0.4, 1.0, 4.9, 470)]), at(100.7), S)
        self.assertEqual([(r["second_utc"], r["mbit_per_s"]) for r in self.rows(conn)], [(at(97).isoformat(), 5.1), (at(100).isoformat(), 4.9)])

    def test_the_same_second_sent_again_replaces_it_and_never_duplicates(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", fine_report([sample(0.0, 1.0, 5.0, 480)]), at(100), S)
        vf.ingest_locked(conn, "MAIN", fine_report([sample(0.0, 1.0, 4.0, 380)]), at(100), S)
        self.assertEqual([r["mbit_per_s"] for r in self.rows(conn)], [4.0])

    def test_two_pcs_in_the_same_second_are_two_rows(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", fine_report([sample(0.0, 1.0, 5.0)]), at(100), S)
        vf.ingest_locked(conn, "BACKUP-SERVER", fine_report([sample(0.0, 1.0, 4.0)]), at(100), S)
        self.assertEqual([(r["source_id"], r["mbit_per_s"]) for r in self.rows(conn)], [("BACKUP-SERVER", 4.0), ("MAIN", 5.0)])

    def test_both_streams_are_stored_and_a_pc_that_cannot_listen_stores_nothing(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", fine_report([sample(0.0, 1.0, 5.0)], [sample(0.0, 1.0, 5.2)]), at(100), S)
        vf.ingest_locked(conn, "BACKUP-SERVER", vf.parse_report(raw_field({"5004": [sample()]}, listening=False)), at(100), S)
        self.assertEqual((len(self.rows(conn, "5004")), len(self.rows(conn, "5005"))), (1, 1))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM site_c_feed_seconds WHERE source_id = 'BACKUP-SERVER'").fetchone()[0], 0)

    def test_a_report_without_samples_stores_none_and_changes_nothing_else(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", report(), at(100), S)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM site_c_feed_seconds").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM site_c_feed_latest").fetchone()[0], 2)

    def test_rows_older_than_the_chart_keeps_are_removed_as_new_ones_arrive(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", fine_report([sample(0.0)]), at(0), S)
        vf.ingest_locked(conn, "MAIN", fine_report([sample(0.0)]), at(700), S)
        self.assertEqual([r["second_utc"] for r in self.rows(conn)], [at(700).isoformat()])

    def test_the_table_is_ceilinged_by_the_retention_and_the_per_report_cap(self):
        conn = database()
        for start in range(0, 1200, 10):
            for source in ("MAIN", "BACKUP-SERVER"):
                vf.ingest_locked(conn, source, fine_report([sample(float(age)) for age in range(10)]), at(start + 9), S)
        self.assertLessEqual(conn.execute("SELECT COUNT(*) FROM site_c_feed_seconds").fetchone()[0], 2 * 2 * (vf.RECENT_SECONDS + 1))

    def test_an_older_database_gets_the_table_and_a_repeat_changes_nothing(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE site_c_feed_recent (taken_utc TEXT)")
        vf.init_tables(conn)
        vf.init_tables(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM site_c_feed_seconds").fetchone()[0], 0)


class FineSnapshotTests(unittest.TestCase):
    """What the card's chart draws: one number a second for the last ten minutes, in a compact list."""

    def fine(self, conn, now_seconds, stream="5004"):
        return {e["stream"]: e for e in vf.snapshot(conn, at(now_seconds), S)["streams"]}[stream]["fine"]

    def test_with_no_samples_the_block_is_empty_and_says_how_fine_it_is(self):
        self.assertEqual(self.fine(database(), 0), {"step_seconds": 1, "start": None, "mbit": []})

    def test_one_number_a_second_from_the_first_sample_with_a_null_for_a_second_nobody_reported(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", fine_report([sample(4.0, 1.0, 5.01), sample(3.0, 1.0, 5.02), sample(1.0, 1.0, 4.98)]), at(100.2), S)
        block = self.fine(conn, 101)
        self.assertEqual(block["step_seconds"], 1)
        self.assertEqual(block["start"], at(96).isoformat())
        self.assertEqual(block["mbit"], [5.01, 5.02, None, 4.98])

    def test_when_two_pcs_report_the_same_second_the_better_reading_is_drawn(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", fine_report([sample(0.0, 1.0, 3.0)]), at(100), S)
        vf.ingest_locked(conn, "BACKUP-SERVER", fine_report([sample(0.0, 1.0, 5.0)]), at(100), S)
        self.assertEqual(self.fine(conn, 101)["mbit"], [5.0])

    def test_only_the_last_ten_minutes_are_returned_and_the_list_never_outgrows_the_window(self):
        conn = database()
        for seconds in range(0, 1300, 10):
            vf.ingest_locked(conn, "MAIN", fine_report([sample(float(age), 1.0, 5.0 + age / 1000) for age in range(10)]), at(seconds + 9), S)
        block = self.fine(conn, 1299)
        self.assertLessEqual(len(block["mbit"]), vf.LIVE_SECONDS + 1)
        self.assertGreaterEqual(datetime.fromisoformat(block["start"]), at(1299 - vf.LIVE_SECONDS))

    def test_values_are_rounded_to_two_decimals_to_keep_the_list_small(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", fine_report([sample(0.0, 1.0, 5.012345)]), at(100), S)
        self.assertEqual(self.fine(conn, 101)["mbit"], [5.01])

    def test_the_two_streams_have_their_own_lists(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", fine_report([sample(0.0, 1.0, 5.0)], [sample(0.0, 1.0, 6.0)]), at(100), S)
        self.assertEqual((self.fine(conn, 101, "5004")["mbit"], self.fine(conn, 101, "5005")["mbit"]), ([5.0], [6.0]))

    def test_the_older_ten_second_line_is_still_there_unchanged_for_pages_that_do_not_know_the_new_block(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", fine_report([sample(0.0)]), at(100), S)
        entry = {e["stream"]: e for e in vf.snapshot(conn, at(101), S)["streams"]}["5004"]
        self.assertEqual([p["mbit"] for p in entry["live"]], [5.0])

    def test_a_full_window_from_two_pcs_stays_inside_the_size_budget(self):
        conn = database()
        # the worst case for the card's whole block: a full 24 hours of per-minute history from both PCs on both streams, on top of the live lines
        conn.executemany("INSERT OR REPLACE INTO site_c_feed_minutes VALUES (?, ?, ?, ?, ?)",
                         [((T0 - timedelta(minutes=minute)).replace(second=0, microsecond=0).isoformat(), stream, source, 15.0, 3)
                          for minute in range(1, 1441) for stream in ("5004", "5005") for source in ("MAIN", "BACKUP-SERVER")])
        for start in range(0, 700, 10):
            for source in ("MAIN", "BACKUP-SERVER"):
                vf.ingest_locked(conn, source, fine_report([sample(float(age), 1.0, 4.8 + (age % 7) / 10) for age in range(10)]), at(start + 9), S)
        snapshot = vf.snapshot(conn, at(700), S)
        per_stream = max(len(json.dumps(e["fine"], separators=(",", ":"))) for e in snapshot["streams"])
        self.assertLess(per_stream, 4500, "one stream's per-second list must stay small: it is sent to every open page every five seconds")
        self.assertLess(len(json.dumps(snapshot, separators=(",", ":"))), 32_000, "the whole card block is sent to every open page every five seconds")

    def test_the_per_second_block_names_no_machine(self):
        conn = database()
        vf.ingest_locked(conn, "MAIN", fine_report([sample(0.0)]), at(100), S)
        text = json.dumps(vf.snapshot(conn, at(101), S)["streams"][0]["fine"])
        self.assertNotIn("MAIN", text)
        self.assertNotIn("192.168", text)


class PerSecondNeverDecidesTests(unittest.TestCase):
    """The owner's rules (no packet for 20 s; under 1 Mbit/s for 120 s; clear after 30 s of being heard) are judged from the ten-second figures, exactly as before. The
    per-second samples are for the chart only: they may look perfect, dead or be missing, and the alarms and their timing must not move by a second."""
    SAMPLE_SETS = {"perfect": [sample(float(age), 1.0, 5.0) for age in range(10)], "dead": [sample(float(age), 1.0, 0.0, 0) for age in range(10)], "none": []}

    def streams(self, mbit, idle):
        return {p: {"mbit_per_s": mbit, "packets": int(mbit * 50), "idle_seconds": idle} for p in ("5004", "5005")}

    def timeline(self, points, samples):
        conn = database()
        log = []
        for seconds, mbit, idle in points:
            for source in ("MAIN", "BACKUP-SERVER"):
                vf.ingest_locked(conn, source, fine_report(samples, streams=self.streams(mbit, idle)), at(seconds), S)
            log += [(seconds, a["action"], a["stream"]) for a in vf.evaluate_locked(conn, at(seconds), S)]
        return log, conn

    def test_a_stream_under_the_floor_is_raised_at_one_hundred_and_twenty_seconds_whatever_the_samples_say(self):
        for name, samples in self.SAMPLE_SETS.items():
            with self.subTest(samples=name):
                log, _ = self.timeline([(s, 0.5, 0.0) for s in range(0, 131, 10)], samples)
                self.assertEqual(log, [(120, "raise", "5004"), (120, "raise", "5005")])

    def test_a_lost_stream_is_raised_at_twenty_seconds_and_cleared_after_thirty_whatever_the_samples_say(self):
        points = [(0, 5.2, 0), (10, 5.2, 0), (20, 0, 10), (30, 0, 20), (40, 0, 30), (50, 0, 40), (60, 5.2, 0), (70, 5.2, 0), (80, 5.2, 0), (90, 5.2, 0), (100, 5.2, 0)]
        for name, samples in self.SAMPLE_SETS.items():
            with self.subTest(samples=name):
                log, _ = self.timeline(points, samples)
                self.assertEqual(log, [(30, "raise", "5004"), (30, "raise", "5005"), (90, "restore", "5004"), (90, "restore", "5005")])

    def test_a_healthy_ten_second_stream_raises_nothing_even_when_the_samples_are_dead(self):
        log, conn = self.timeline([(s, 5.0, 0.0) for s in range(0, 181, 10)], self.SAMPLE_SETS["dead"])
        self.assertEqual(log, [])
        self.assertEqual({e["level"] for e in vf.snapshot(conn, at(180), S)["streams"]}, {"ok"})

    def test_the_judgement_never_reads_the_samples_table(self):
        conn = database()
        for source in ("MAIN", "BACKUP-SERVER"):
            vf.ingest_locked(conn, source, fine_report(self.SAMPLE_SETS["dead"], streams=self.streams(5.0, 0.0)), at(10), S)
        conn.execute("DELETE FROM site_c_feed_seconds")
        self.assertEqual({e["level"] for e in vf.snapshot(conn, at(11), S)["streams"]}, {"ok"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
