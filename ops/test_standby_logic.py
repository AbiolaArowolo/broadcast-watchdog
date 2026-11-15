# File:         test_standby_logic.py
# Purpose:      Tests of core-listener/app/standby.py: the UP/DOWN decision about Core, the delivery gate and the takeover and stand-down messages.
# Inputs:       None (a fake clock and made-up probe and studio reports).
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/standby.py (pure logic: no network, no clock of its own).
"""Run: python ops/test_standby_logic.py

What is proven here, in plain words: the standby stays quiet while Core is fine, takes over only when BOTH its own probe and the studios' testimony agree that
Core is gone, announces the takeover and the stand-down exactly once, cannot flap, and never puts a secret in a message.
"""
import importlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

LISTENER_DIR = Path(__file__).resolve().parent.parent / "core-listener"
sys.path.insert(0, str(LISTENER_DIR))
standby = importlib.import_module("app.standby")

T0 = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def watch(**overrides):
    """A watch whose probe has already succeeded once (a probe that has never worked cannot take over: see UnprovenProbeTests)."""
    return standby.CoreWatch(standby.WatchConfig(**overrides), at(0), proven_at=at(0))


def fail_probes(w, count, start=0, step=30, error="timed out"):
    """Feeds `count` failed probes `step` seconds apart; returns the last transition seen (or None)."""
    seen = None
    for i in range(count):
        seen = w.record_probe(False, at(start + i * step), error) or seen
    return seen


def pass_probes(w, count, start, step=30):
    seen = None
    for i in range(count):
        seen = w.record_probe(True, at(start + i * step)) or seen
    return seen


class StartsQuietTests(unittest.TestCase):
    def test_a_new_standby_believes_core_is_up_and_delivers_nothing(self):
        w = watch()
        self.assertEqual(w.state, standby.UP)
        self.assertFalse(w.delivery_allowed())
        self.assertEqual(w.since, at(0))

    def test_healthy_probes_change_nothing(self):
        w = watch()
        self.assertIsNone(pass_probes(w, 20, 0))
        self.assertEqual(w.state, standby.UP)
        self.assertFalse(w.delivery_allowed())


class TakeoverTests(unittest.TestCase):
    def test_three_failed_probes_in_a_row_take_over_exactly_once(self):
        w = watch()
        self.assertIsNone(w.record_probe(False, at(0), "refused"))
        self.assertIsNone(w.record_probe(False, at(30), "refused"))
        change = w.record_probe(False, at(60), "refused")
        self.assertIsNotNone(change)
        self.assertEqual((change.kind, change.at), ("down", at(60)))
        self.assertEqual(w.state, standby.DOWN)
        self.assertTrue(w.delivery_allowed())
        self.assertIsNone(w.record_probe(False, at(90), "refused"), "a second failure must not announce a second takeover")
        self.assertIsNone(w.record_probe(False, at(120), "refused"))

    def test_one_good_probe_in_between_restarts_the_count(self):
        w = watch()
        fail_probes(w, 2)
        w.record_probe(True, at(60))
        self.assertIsNone(fail_probes(w, 2, start=90))
        self.assertEqual(w.state, standby.UP)

    def test_a_studio_that_still_reaches_core_stops_a_false_takeover(self):
        # the probe path (Cloudflare) is broken, but MAIN just said its post to Core worked: Core is not down
        w = watch()
        w.record_report("MAIN", True, at(55))
        self.assertIsNone(fail_probes(w, 3))
        self.assertEqual(w.state, standby.UP)
        self.assertFalse(w.delivery_allowed())

    def test_the_veto_expires_when_the_studio_goes_quiet_or_changes_its_mind(self):
        w = watch()
        w.record_report("MAIN", True, at(10))
        self.assertIsNone(fail_probes(w, 3))                      # probes at 0, 30, 60: MAIN vouched 50 s ago at the last one
        change = w.record_probe(False, at(120), "timed out")      # MAIN's report is now 110 s old (> 90 s)
        self.assertEqual(change.kind, "down")

    def test_a_studio_switching_to_false_lifts_the_veto_at_once(self):
        w = watch()
        w.record_report("MAIN", True, at(10))
        fail_probes(w, 3)
        self.assertEqual(w.state, standby.UP)
        change = w.record_report("MAIN", False, at(70))
        self.assertIsNotNone(change)
        self.assertEqual(change.kind, "down")

    def test_one_studio_failing_alone_does_not_take_over(self):
        # the probe is fine; only a studio says it cannot reach Core (its own network problem)
        w = watch()
        pass_probes(w, 5, 0)
        self.assertIsNone(w.record_report("SITE-A", False, at(150)))
        self.assertEqual(w.state, standby.UP)

    def test_studios_that_never_send_the_flag_do_not_matter(self):
        w = watch()
        self.assertEqual(fail_probes(w, 3).kind, "down")

    def test_a_stale_report_is_ignored(self):
        w = watch()
        w.record_report("MAIN", True, at(-200))
        self.assertEqual(fail_probes(w, 3).kind, "down")


class RecoveryTests(unittest.TestCase):
    def down(self):
        w = watch()
        fail_probes(w, 3)                                            # DOWN at t=60
        return w

    def test_recovery_needs_two_good_probes_and_the_minimum_down_time(self):
        w = self.down()
        self.assertIsNone(w.record_probe(True, at(70)))
        self.assertIsNone(w.record_probe(True, at(100)), "only 40 s since going down: too soon")
        change = w.record_probe(True, at(190))
        self.assertIsNotNone(change)
        self.assertEqual((change.kind, w.state), ("up", standby.UP))
        self.assertFalse(w.delivery_allowed())
        self.assertIsNone(w.record_probe(True, at(220)), "stand-down is announced once")

    def test_a_failure_between_good_probes_resets_the_recovery_count(self):
        w = self.down()
        w.record_probe(True, at(200))
        w.record_probe(False, at(230), "refused")
        self.assertIsNone(w.record_probe(True, at(260)))
        self.assertEqual(w.state, standby.DOWN)
        self.assertEqual(w.record_probe(True, at(290)).kind, "up")

    def test_a_studio_that_still_cannot_reach_core_blocks_the_stand_down(self):
        w = self.down()
        w.record_report("MAIN", False, at(190))
        w.record_probe(True, at(170))
        self.assertIsNone(w.record_probe(True, at(200)))
        self.assertEqual(w.state, standby.DOWN)
        self.assertEqual(w.record_probe(True, at(260)).kind, "up", "the report is older than 60 s by now")

    def test_it_can_go_down_again_after_recovering(self):
        w = self.down()
        pass_probes(w, 3, 200)
        self.assertEqual(w.state, standby.UP)
        self.assertEqual(fail_probes(w, 3, start=400).kind, "down")


class LyingOrStuckStudioTests(unittest.TestCase):
    """The automated security review's finding: a studio's "Core took my post" is self-reported, so one lying (or stolen-key, or stuck) studio must not be able to
    keep the standby silent while Core is really dead. A veto needs a strict majority of the studios that reported, and no veto lasts longer than the cap."""

    def test_one_studio_cannot_outvote_the_others(self):
        w = watch()
        w.record_report("MAIN", True, at(40))          # claims Core is fine
        w.record_report("SITE-A", False, at(40))      # says it cannot reach Core
        change = fail_probes(w, 3)
        self.assertIsNotNone(change, "a 1 to 1 split is not a majority for Core")
        self.assertEqual(w.state, standby.DOWN)

    def test_two_against_one_still_vetoes(self):
        w = watch()
        w.record_report("MAIN", True, at(40))
        w.record_report("SITE-B", True, at(40))
        w.record_report("SITE-A", False, at(40))
        self.assertIsNone(fail_probes(w, 3))
        self.assertEqual(w.state, standby.UP)

    def test_a_liar_among_honest_studios_cannot_hide_the_outage(self):
        w = watch()
        w.record_report("MAIN", True, at(40))
        for honest in ("BACKUP-SERVER", "SITE-A", "SITE-B"):
            w.record_report(honest, False, at(40))
        self.assertEqual(fail_probes(w, 3).kind, "down")

    def test_a_studio_vouching_forever_cannot_hold_the_takeover_past_the_cap(self):
        w = watch()
        change = None
        for i in range(0, 25):                                  # probes every 30 s for 12 minutes, MAIN vouches every time
            change = w.record_report("MAIN", True, at(i * 30)) or change
            change = w.record_probe(False, at(i * 30), "refused") or change
            if i * 30 < 600:
                self.assertEqual(w.state, standby.UP, f"still vetoed at {i * 30} s")
        self.assertIsNotNone(change)
        self.assertEqual(change.kind, "down")
        self.assertEqual(change.at, at(600), "the takeover happens when the failures have lasted the cap")

    def test_the_cap_counts_from_the_first_failure_of_the_streak_not_from_old_failures(self):
        w = watch()
        for i in range(5):
            w.record_report("MAIN", True, at(i * 30))
            w.record_probe(False, at(i * 30), "x")
        w.record_probe(True, at(150))                          # a good probe ends the streak
        for i in range(10, 30):
            w.record_report("MAIN", True, at(i * 30))
            w.record_probe(False, at(i * 30), "x")
            self.assertEqual(w.state, standby.UP if i * 30 < 300 + 600 else standby.DOWN)

    def test_a_studio_saying_failed_forever_cannot_keep_the_standby_loud_past_the_cap(self):
        w = watch()
        fail_probes(w, 3)                                       # DOWN at 60
        for i in range(0, 40):
            t = 100 + i * 30
            change = w.record_report("SITE-A", False, at(t))
            change = w.record_probe(True, at(t)) or change
            if change:
                self.assertEqual(change.kind, "up")
                self.assertGreaterEqual(t - 100, 600, "it may stand down early only if nobody doubts Core")
                return
        self.fail("a permanent doubter kept the standby in DOWN forever")

    def test_the_takeover_message_says_when_the_studios_disagree(self):
        w = watch()
        w.record_report("MAIN", True, at(10))
        for i in range(0, 22):
            w.record_report("MAIN", True, at(i * 30))
            w.record_probe(False, at(i * 30), "HTTP 530")
        snap = w.snapshot(at(660))
        notice = standby.takeover_notice(snap, [("MAIN", "healthy")], "standby.corp.example")
        text = " ".join(f"{a} {b}" for a, b in notice.rows).lower()
        self.assertIn("still say", text)
        self.assertIn("disagree", text)

    def test_the_cap_is_a_setting_and_must_be_sane(self):
        self.assertEqual(standby.WatchConfig().veto_max_seconds, 600)
        with self.assertRaises(ValueError):
            standby.WatchConfig(veto_max_seconds=0)


class BoundsAndBadInputTests(unittest.TestCase):
    def test_the_report_table_is_bounded(self):
        w = watch(max_sources=4)
        for i in range(10):
            w.record_report(f"S{i}", True, at(i))
        self.assertLessEqual(len(w.snapshot(at(20))["studios"]), 4)

    def test_bad_reports_are_ignored_not_raised(self):
        w = watch()
        for source, value in (("", True), (None, True), ("MAIN", "yes"), ("MAIN", None), ("x" * 500, True), (42, True)):
            with self.subTest(source=source, value=value):
                self.assertIsNone(w.record_report(source, value, at(1)))
        self.assertEqual(w.snapshot(at(2))["studios"], {})

    def test_a_clock_without_a_zone_is_refused(self):
        with self.assertRaises(ValueError):
            watch().record_probe(True, datetime(2026, 10, 20, 12, 0, 0))

    def test_a_non_bool_probe_result_is_refused(self):
        with self.assertRaises(ValueError):
            watch().record_probe("ok", at(1))

    def test_invalid_settings_stop_the_start(self):
        for bad in ({"down_after_failures": 0}, {"up_after_successes": 0}, {"min_down_seconds": -1}, {"veto_true_seconds": 0}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    standby.WatchConfig(**bad)

    def test_the_snapshot_has_state_counts_and_ages_but_no_secret(self):
        w = watch()
        w.record_report("MAIN", True, at(5))
        fail_probes(w, 2, error="HTTP 502 " + "x" * 400)
        snap = w.snapshot(at(70))
        self.assertEqual(snap["state"], "UP")
        self.assertEqual(snap["failed_probes_in_a_row"], 2)
        self.assertLessEqual(len(snap["last_error"]), 160)
        self.assertIn("MAIN", snap["studios"])
        self.assertEqual(sorted(snap["studios"]["MAIN"]), ["age_seconds", "primary_ok"])


class MessageTests(unittest.TestCase):
    def snapshot_down(self):
        w = watch()
        w.record_report("MAIN", False, at(50))
        fail_probes(w, 3, error="HTTP 502")
        return w.snapshot(at(90))

    def test_the_takeover_names_the_cause_the_studios_and_what_changes(self):
        studios = [("MAIN", "healthy"), ("SITE-B", "heartbeat_lost")]
        notice = standby.takeover_notice(self.snapshot_down(), studios, "standby.corp.example")
        text = " ".join([notice.title, notice.summary, notice.push_body] + [f"{a} {b}" for a, b in notice.rows])
        self.assertIn("DOWN", notice.title.upper())
        self.assertIn("HTTP 502", text)
        self.assertIn("MAIN", text)
        self.assertIn("SITE-B", text)
        self.assertIn("heartbeat_lost", text)
        self.assertIn("standby.corp.example", text)
        self.assertLessEqual(len(notice.push_body), 300)

    def test_the_stand_down_says_core_is_back_and_how_long_it_was_gone(self):
        notice = standby.standdown_notice(at(60), at(660), [("MAIN", "healthy")], "standby.corp.example")
        text = " ".join([notice.title, notice.summary] + [f"{a} {b}" for a, b in notice.rows])
        self.assertIn("back", text.lower())
        self.assertIn("10 min", text)

    def test_messages_cope_with_no_studios_and_hostile_text(self):
        snap = self.snapshot_down()
        snap["last_error"] = "<script>alert(1)</script>\r\nBcc: attacker@example.com"
        notice = standby.takeover_notice(snap, [], "standby.corp.example")
        self.assertNotIn("\r", notice.title + notice.summary + notice.push_body)
        self.assertNotIn("\n", notice.title + notice.push_body)
        self.assertTrue(notice.rows)

    def test_no_message_can_hold_a_key(self):
        secret = "S3cretKeyValue-0123456789abcdefghij"
        snap = self.snapshot_down()
        notice = standby.takeover_notice(snap, [("MAIN", "healthy")], "standby.corp.example")
        down = standby.standdown_notice(at(60), at(300), [("MAIN", "healthy")], "standby.corp.example")
        for item in (notice, down):
            self.assertNotIn(secret, repr(item))


class SettingsParsingTests(unittest.TestCase):
    def test_role_and_url_settings_have_safe_defaults_and_refuse_nonsense(self):
        self.assertEqual(standby.parse_role(""), "primary")
        self.assertEqual(standby.parse_role(None), "primary")
        self.assertEqual(standby.parse_role(" Standby "), "standby")
        self.assertEqual(standby.parse_role("primary"), "primary")
        with self.assertRaises(ValueError):
            standby.parse_role("secondary")

    def test_the_probe_address_must_be_a_web_address(self):
        self.assertEqual(standby.parse_probe_url("https://watchdog.corp.example/health"), "https://watchdog.corp.example/health")
        for bad in ("ftp://x/health", "file:///etc/passwd", "javascript:alert(1)", "https:///health", "not a url", "http://user:pass@host/health"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    standby.parse_probe_url(bad)
        self.assertEqual(standby.parse_probe_url(""), "")


class UnprovenProbeTests(unittest.TestCase):
    """Found on the first real day: Cloudflare answered the standby's probe with HTTP 403 every time (it blocked Python's default web client), three failures in a row
    looked like "Core is down", and with no studio copies to say otherwise the standby took over. A standby must not conclude that Core is gone from a probe that
    has never once been able to see Core well: it says the probe is broken instead, and takes over only after the probe has been proven."""

    def blocked_watch(self, **overrides):
        return standby.CoreWatch(standby.WatchConfig(**overrides), at(0))                 # no proof yet

    def test_a_probe_that_has_never_succeeded_never_takes_over_however_often_it_fails(self):
        w = self.blocked_watch()
        for i in range(20):
            self.assertIsNone(w.record_probe(False, at(30 * i), "HTTP 403"))
        self.assertEqual(w.state, standby.UP)
        self.assertFalse(w.delivery_allowed(), "a standby that cannot see Core stays quiet")
        shown = w.snapshot(at(600))
        self.assertIs(shown["probe_proven"], False)
        self.assertIs(shown["takeover_blocked"], True)
        self.assertIsNone(shown["last_ok_at"])
        self.assertEqual(shown["last_error"], "HTTP 403")

    def test_one_good_probe_proves_it_and_then_real_failures_do_take_over(self):
        w = self.blocked_watch()
        w.record_probe(False, at(0), "HTTP 403")
        w.record_probe(True, at(30))
        self.assertIs(w.snapshot(at(30))["probe_proven"], True)
        changes = [w.record_probe(False, at(60 + 30 * i), "timed out") for i in range(3)]
        self.assertEqual([c.kind if c else None for c in changes], [None, None, "down"])

    def test_the_proof_survives_a_restart_for_a_while_and_then_expires(self):
        cfg = standby.WatchConfig()
        fresh = standby.CoreWatch(cfg, at(10_000), proven_at=at(10_000 - 3600))
        self.assertIs(fresh.snapshot(at(10_000))["probe_proven"], True)
        for i in range(3):
            change = fresh.record_probe(False, at(10_000 + 30 * i), "timed out")
        self.assertEqual(change.kind, "down", "a restart during an outage must not lose the ability to take over")
        stale = standby.CoreWatch(cfg, at(10_000_000), proven_at=at(10_000_000 - cfg.proven_max_age_seconds - 1))
        for i in range(10):
            self.assertIsNone(stale.record_probe(False, at(10_000_000 + 30 * i), "timed out"))
        self.assertIs(stale.snapshot(at(10_000_300))["probe_proven"], False)

    def test_the_veto_cap_cannot_turn_an_unproven_probe_into_a_takeover(self):
        w = self.blocked_watch(veto_max_seconds=60)
        w.record_report("MAIN", True, at(0))
        for i in range(40):
            self.assertIsNone(w.record_probe(False, at(30 * i), "HTTP 403"))
            w.record_report("MAIN", True, at(30 * i))
        self.assertEqual(w.state, standby.UP)

    def test_restoring_a_proof_never_moves_it_backwards_and_ignores_nonsense(self):
        w = standby.CoreWatch(standby.WatchConfig(), at(5000), proven_at=at(4000))
        w.restore_proven(at(1000))
        self.assertEqual(w.last_ok_at, at(4000))
        w.restore_proven(at(4500))
        self.assertEqual(w.last_ok_at, at(4500))
        w.restore_proven(None)
        w.restore_proven("not a time")
        self.assertEqual(w.last_ok_at, at(4500))

    def test_the_blocked_takeover_is_logged_once_not_on_every_probe(self):
        w = self.blocked_watch()
        with self.assertLogs("standby", level="WARNING") as captured:
            for i in range(12):
                w.record_probe(False, at(30 * i), "HTTP 403")
        blocked = [line for line in captured.output if "takeover_blocked" in line]
        self.assertEqual(len(blocked), 1)
        self.assertIn("HTTP 403", blocked[0], "the line says why the probe is failing")

    def test_after_a_blocked_period_a_proven_probe_logs_again_if_it_is_blocked_later(self):
        w = self.blocked_watch()
        for i in range(4):
            w.record_probe(False, at(30 * i), "HTTP 403")
        w.record_probe(True, at(200))
        self.assertIs(w.snapshot(at(200))["takeover_blocked"], False)

    def test_a_standby_already_down_can_still_stand_down_with_an_unproven_state(self):
        """Only the way IN is guarded: leaving DOWN needs good probes, which are themselves the proof."""
        w = self.blocked_watch(min_down_seconds=0)
        w.record_probe(True, at(0))
        for i in range(3):
            w.record_probe(False, at(30 + 30 * i), "timed out")
        self.assertEqual(w.state, standby.DOWN)
        w.record_probe(True, at(200))
        change = w.record_probe(True, at(230))
        self.assertEqual(change.kind, "up")

    def test_the_new_setting_is_validated(self):
        with self.assertRaises(ValueError):
            standby.WatchConfig(proven_max_age_seconds=10)
        standby.WatchConfig(proven_max_age_seconds=3600)


class StandbyHealthTests(unittest.TestCase):
    """The "Standby alive" heartbeat: the standby pings an outside check only while this list of problems is empty, so every way the standby can quietly stop
    being ready (a stale key, a studio that stopped sending, a full disk, a blocked mail account, a stuck probe loop) becomes one email from the outside service."""

    def setUp(self):
        self.config = standby.HealthConfig(expect_copies=("MAIN", "BACKUP-SERVER"), copy_max_age_seconds=300, probe_max_age_seconds=180, min_free_mb=200,
                                           refusals_warn=3, refusals_window_seconds=300, startup_grace_seconds=120)

    def inputs(self, now=600, **overrides):
        values = dict(now=at(now), started_at=at(0), last_probe_at=at(now - 20), db_ok=True, free_mb=5000.0, key_error="", keyed_sources=("MAIN", "BACKUP-SERVER", "SITE-A", "SITE-B"),
                      copy_seen={"MAIN": at(now - 8), "BACKUP-SERVER": at(now - 12)}, refusals_recent=0, smtp_ok=True)
        values.update(overrides)
        return standby.HealthInputs(**values)

    def problems(self, now=600, **overrides):
        return standby.assess_health(self.inputs(now, **overrides), self.config)

    def test_everything_in_order_is_no_problem_at_all(self):
        self.assertEqual(self.problems(), [])

    def test_a_database_that_does_not_answer_is_reported(self):
        self.assertTrue(any("database" in text for text in self.problems(db_ok=False)))

    def test_a_nearly_full_disk_is_reported_with_the_numbers(self):
        found = self.problems(free_mb=120.0)
        self.assertEqual(len(found), 1)
        self.assertIn("120", found[0])
        self.assertIn("200", found[0])
        self.assertEqual(self.problems(free_mb=None), [], "an unknown free space is not invented into a problem")

    def test_a_bad_keys_file_is_reported_even_in_the_first_minutes(self):
        found = self.problems(now=10, key_error="line 3 of the keys file is not NAME=value for a sender key setting", copy_seen={})
        self.assertTrue(any("keys file" in text for text in found))

    def test_a_studio_with_no_key_loaded_is_reported(self):
        found = self.problems(keyed_sources=("MAIN",))
        self.assertTrue(any("BACKUP-SERVER" in text and "no sender key" in text for text in found))

    def test_a_studio_whose_copies_stopped_is_reported_with_how_long(self):
        found = self.problems(copy_seen={"MAIN": at(600 - 8), "BACKUP-SERVER": at(600 - 7 * 60)})
        self.assertEqual(len(found), 1)
        self.assertIn("BACKUP-SERVER", found[0])
        self.assertIn("7 minutes", found[0])

    def test_a_studio_that_never_sent_a_copy_since_start_is_reported_after_the_grace_period(self):
        self.assertEqual(self.problems(now=60, copy_seen={}), [], "the first two minutes are for the studios to find the standby")
        found = self.problems(now=600, copy_seen={"MAIN": at(590)})
        self.assertTrue(any("BACKUP-SERVER" in text and "since the standby started" in text for text in found))

    def test_a_studio_that_is_not_expected_to_send_copies_is_never_judged(self):
        self.assertEqual(self.problems(copy_seen={"MAIN": at(590), "BACKUP-SERVER": at(595)}, keyed_sources=("MAIN", "BACKUP-SERVER")), [])
        config = standby.HealthConfig(expect_copies=())
        self.assertEqual(standby.assess_health(self.inputs(copy_seen={}), config), [], "with nothing expected, silence from the studios is fine")

    def test_refusals_turn_a_stale_copy_into_a_key_mismatch_hint_but_never_into_a_problem_by_themselves(self):
        stale = {"MAIN": at(590), "BACKUP-SERVER": at(100)}
        with_hint = self.problems(copy_seen=stale, refusals_recent=12)
        self.assertTrue(any("12 posts were refused" in text and "standby_sync" in text for text in with_hint))
        self.assertEqual(self.problems(refusals_recent=500), [], "internet noise with a wrong key must not page anyone while every studio is arriving")

    def test_a_probe_loop_that_has_stopped_is_reported(self):
        found = self.problems(now=900, last_probe_at=at(900 - 10 * 60), copy_seen={"MAIN": at(890), "BACKUP-SERVER": at(895)})
        self.assertTrue(any("Core" in text and "10 minutes" in text for text in found))
        never = self.problems(now=900, last_probe_at=None, copy_seen={"MAIN": at(890), "BACKUP-SERVER": at(895)})
        self.assertTrue(any("no probe" in text for text in never))
        self.assertEqual(self.problems(now=60, last_probe_at=None, copy_seen={}), [], "not judged during the start-up grace period")

    def test_a_probe_that_cannot_see_core_is_reported_with_its_error_and_the_consequence(self):
        found = self.problems(now=900, probe_proven=False, probe_error="HTTP 403", copy_seen={"MAIN": at(890), "BACKUP-SERVER": at(895)})
        self.assertEqual(len(found), 1)
        self.assertIn("HTTP 403", found[0])
        self.assertIn("NOT take over", found[0])
        self.assertEqual(self.problems(now=900, probe_proven=True, copy_seen={"MAIN": at(890), "BACKUP-SERVER": at(895)}), [])
        self.assertEqual(self.problems(now=60, probe_proven=False, probe_error="HTTP 403", copy_seen={}), [], "not judged during the start-up grace period")

    def test_a_blocked_mail_account_is_reported_but_an_unconfigured_one_is_not_invented(self):
        self.assertTrue(any("email" in text for text in self.problems(smtp_ok=False)))
        self.assertEqual(self.problems(smtp_ok=None), [])

    def test_several_problems_are_all_listed_and_the_summary_is_short_plain_ascii(self):
        found = self.problems(db_ok=False, free_mb=10.0, smtp_ok=False, copy_seen={})
        self.assertGreaterEqual(len(found), 4)
        summary = standby.summarize_problems(found)
        self.assertTrue(summary.isascii())
        self.assertLessEqual(len(summary), standby.MAX_SUMMARY_CHARS)
        self.assertEqual(standby.summarize_problems([]), "")
        self.assertLessEqual(len(standby.summarize_problems(["x" * 5000])), standby.MAX_SUMMARY_CHARS)
        many = standby.summarize_problems(["y" * 300] * 10)                  # each problem is cut to 300 first, so only the whole is long enough to need the final cap
        self.assertEqual(len(many), standby.MAX_SUMMARY_CHARS)

    def test_text_that_is_not_plain_ascii_is_made_so_not_passed_on(self):
        hostile = "problem" + chr(0xE8) + "me " + chr(0x2014) + " caf" + chr(0xE9) + " " + chr(0x4E2D) + chr(0x6587)      # accents, a long dash and CJK, built without typing them
        summary = standby.summarize_problems([hostile])
        self.assertTrue(summary.isascii())
        self.assertIn("probl", summary)
        self.assertIn("?", summary)

    def test_no_message_ever_contains_a_key_or_a_secret_even_if_a_hostile_error_text_is_handed_in(self):
        hostile = "line 2 failed " + "A" * 5 + "\n\x00\x1b[31m"
        summary = standby.summarize_problems(self.problems(key_error=hostile))
        self.assertTrue(summary.isascii())
        self.assertNotIn("\n", summary)
        self.assertNotIn("\x1b", summary)

    def test_the_expected_studios_setting_is_parsed_strictly(self):
        self.assertEqual(standby.parse_expect_copies("MAIN, BACKUP-SERVER ,MAIN"), ("MAIN", "BACKUP-SERVER"))
        self.assertEqual(standby.parse_expect_copies(""), ())
        self.assertEqual(standby.parse_expect_copies(None), ())
        for bad in ("MAIN;DROP", "MAIN BACKUP", "main", "A" * 80):
            with self.subTest(bad=bad[:12]):
                with self.assertRaises(ValueError):
                    standby.parse_expect_copies(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
