# File:         test_control_logic.py
# Purpose:      Tests of core-listener/app/control.py: the switch-off state, who alerts (preferred node and automatic fallback), who is told, and the two mails.
# Inputs:       None (made-up states, a fixed clock).
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/control.py (pure logic: no network, no clock of its own, no database).
"""Run: python ops/test_control_logic.py

What is proven here, in plain words: Core is the preferred node and alerts, the VPS alerts only when Core is down; choosing the VPS swaps that, and the node that is
not preferred ALWAYS takes over when the preferred one is down (a choice is never a mute). When the two nodes disagree about who is preferred, both alert (loud, never
silent). Switching access OFF routes alerts to the super user only and queues exactly one "offline" mail; switching it ON queues exactly one "back" mail; repeating a
command changes nothing. A state that cannot be read falls back to ON with Core preferred and says so.
"""
import importlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

LISTENER_DIR = Path(__file__).resolve().parent.parent / "core-listener"
sys.path.insert(0, str(LISTENER_DIR))
control = importlib.import_module("app.control")

T0 = datetime(2026, 10, 25, 14, 30, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


H, V = control.CORE, control.VPS
UP = control.PeerView(down=False)
DOWN = control.PeerView(down=True)


class DefaultsTests(unittest.TestCase):
    def test_a_new_node_is_on_with_core_preferred_and_nothing_pending(self):
        state = control.ControlState()
        self.assertTrue(state.access_on)
        self.assertEqual(state.preferred, H)
        self.assertIsNone(state.pending_notice)
        self.assertIsNone(state.off_at)

    def test_states_are_frozen(self):
        with self.assertRaises(Exception):
            control.ControlState().access_on = False


class ParsingTests(unittest.TestCase):
    def test_preferred_accepts_either_case_and_spaces_and_nothing_else(self):
        self.assertEqual(control.parse_preferred(" core "), H)
        self.assertEqual(control.parse_preferred("Vps"), V)
        for bad in ("", "both", "core,vps", "auto", None, 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                control.parse_preferred(bad)

    def test_access_accepts_on_and_off_and_nothing_else(self):
        self.assertTrue(control.parse_access("ON"))
        self.assertFalse(control.parse_access(" off "))
        for bad in ("", "yes", "true", "1", None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                control.parse_access(bad)


class AlertRuleTests(unittest.TestCase):
    def test_the_preferred_node_alerts_whatever_the_other_one_is_doing(self):
        for peer in (UP, DOWN, control.PeerView(down=False, preferred=V)):
            self.assertTrue(control.may_alert(H, control.ControlState(preferred=H), peer))
            self.assertTrue(control.may_alert(V, control.ControlState(preferred=V), peer))

    def test_a_node_that_is_not_preferred_stays_quiet_while_the_preferred_one_is_up(self):
        self.assertFalse(control.may_alert(V, control.ControlState(preferred=H), UP))
        self.assertFalse(control.may_alert(H, control.ControlState(preferred=V), UP))

    def test_a_node_that_is_not_preferred_takes_over_when_the_preferred_one_is_down(self):
        self.assertTrue(control.may_alert(V, control.ControlState(preferred=H), DOWN))
        self.assertTrue(control.may_alert(H, control.ControlState(preferred=V), DOWN))

    def test_when_the_nodes_disagree_about_who_is_preferred_both_alert(self):
        # A command that reached only one node must be loud (a double alert), never silent.
        self.assertTrue(control.may_alert(V, control.ControlState(preferred=H), control.PeerView(down=False, preferred=V)))
        self.assertTrue(control.may_alert(H, control.ControlState(preferred=V), control.PeerView(down=False, preferred=H)))

    def test_when_the_nodes_agree_the_non_preferred_node_stays_quiet(self):
        self.assertFalse(control.may_alert(V, control.ControlState(preferred=H), control.PeerView(down=False, preferred=H)))

    def test_an_unknown_peer_preference_is_not_treated_as_a_disagreement(self):
        # An older peer (before this feature) does not report one; it always alerts if it is Core, so staying quiet is right.
        self.assertFalse(control.may_alert(V, control.ControlState(preferred=H), control.PeerView(down=False, preferred=None)))

    def test_no_state_leaves_both_nodes_quiet_while_both_are_up(self):
        for preferred in (H, V):
            for core_view, vps_view in ((H, H), (H, V), (V, H), (V, V)):
                core_state, vps_state = control.ControlState(preferred=core_view), control.ControlState(preferred=vps_view)
                alerting = [control.may_alert(H, core_state, control.PeerView(False, vps_view)),
                            control.may_alert(V, vps_state, control.PeerView(False, core_view))]
                self.assertTrue(any(alerting), f"core thinks {core_view}, vps thinks {vps_view}: nobody alerts")

    def test_an_unknown_node_name_stops_the_call(self):
        with self.assertRaises(ValueError):
            control.may_alert("MAIN", control.ControlState(), UP)


class SetAccessTests(unittest.TestCase):
    def test_switching_off_queues_one_offline_mail_and_remembers_when(self):
        change = control.set_access(control.ControlState(), False, at(0))
        self.assertFalse(change.state.access_on)
        self.assertEqual(change.state.pending_notice, "offline")
        self.assertEqual(change.state.off_at, at(0))
        self.assertEqual(change.state.access_changed_at, at(0))
        self.assertEqual(change.effects, ("access_off",))

    def test_switching_off_again_changes_nothing_and_queues_no_second_mail(self):
        first = control.set_access(control.ControlState(), False, at(0)).state
        again = control.set_access(first, False, at(5))
        self.assertEqual(again.state, first)
        self.assertEqual(again.effects, ())

    def test_switching_on_after_the_offline_mail_went_queues_one_back_mail(self):
        off = control.notice_sent(control.set_access(control.ControlState(), False, at(0)).state, "offline")
        change = control.set_access(off, True, at(130))
        self.assertTrue(change.state.access_on)
        self.assertEqual(change.state.pending_notice, "restored")
        self.assertEqual(change.state.off_at, at(0))                 # kept, so the mail can say how long it was off
        self.assertEqual(change.state.access_changed_at, at(130))
        self.assertEqual(change.effects, ("access_on",))

    def test_switching_on_when_already_on_changes_nothing(self):
        change = control.set_access(control.ControlState(), True, at(0))
        self.assertEqual((change.state, change.effects), (control.ControlState(), ()))

    def test_switching_on_before_the_offline_mail_went_cancels_both_mails(self):
        # Users were never told it was off, so telling them it is back would be noise.
        off = control.set_access(control.ControlState(), False, at(0)).state
        back = control.set_access(off, True, at(1)).state
        self.assertIsNone(back.pending_notice)

    def test_a_second_outage_replaces_the_remembered_off_time(self):
        first = control.notice_sent(control.set_access(control.ControlState(), False, at(0)).state, "offline")
        first = control.notice_sent(control.set_access(first, True, at(10)).state, "restored")
        second = control.set_access(first, False, at(60)).state
        self.assertEqual(second.off_at, at(60))
        self.assertEqual(second.pending_notice, "offline")

    def test_a_node_told_not_to_notify_records_the_change_but_queues_no_mail(self):
        # The command goes to both nodes; only one of them may send the mail, or support gets two.
        off = control.set_access(control.ControlState(), False, at(0), notify=False)
        self.assertEqual((off.state.access_on, off.state.pending_notice, off.effects), (False, None, ("access_off",)))
        back = control.set_access(off.state, True, at(5), notify=False)
        self.assertEqual((back.state.access_on, back.state.pending_notice), (True, None))

    def test_a_node_told_not_to_notify_drops_a_stale_pending_mail(self):
        stale = control.set_access(control.ControlState(), False, at(0)).state          # "offline" still queued here
        self.assertIsNone(control.set_access(stale, True, at(1), notify=False).state.pending_notice)

    def test_a_naive_time_is_refused(self):
        with self.assertRaises(ValueError):
            control.set_access(control.ControlState(), False, datetime(2026, 10, 25, 14, 30))


class NothingSwitchesAccessByItselfTests(unittest.TestCase):
    """The super user decides when access goes OFF and when it comes back ON. No clock, peer, mail or restart changes it: only set_access does, and only when called."""

    def test_the_module_has_no_clock_or_timer_of_its_own(self):
        source = (LISTENER_DIR / "app" / "control.py").read_text(encoding="utf-8")
        for word in ("datetime.now", "utcnow", "time.time", "monotonic", "sleep", "Timer", "timedelta", "expires", "auto_off", "auto_on"):
            self.assertNotIn(word, source, word)

    def test_off_stays_off_whatever_the_peer_the_mail_or_the_calendar_does(self):
        state = control.set_access(control.ControlState(), False, at(0)).state
        for step, peer in enumerate((UP, DOWN, UP, control.PeerView(down=False, preferred=V), DOWN)):
            for node in (H, V):
                control.may_alert(node, state, peer)
            state = control.set_preferred(state, V if step % 2 == 0 else H, at(60 * 24 * 40 + step)).state          # forty days later and still choosing
            self.assertFalse(state.access_on, f"step {step}")
            self.assertEqual(control.route_delivery(state), control.Route(users=False, super_user=True))
        for kind in ("offline", "restored", "offline"):
            state = control.notice_sent(state, kind)
            self.assertFalse(state.access_on)
        self.assertFalse(control.from_store(control.to_store(state)).state.access_on)
        self.assertEqual(state.off_at, at(0))

    def test_on_stays_on_in_the_same_way(self):
        state = control.ControlState()
        for step, peer in enumerate((UP, DOWN, UP, DOWN)):
            for node in (H, V):
                control.may_alert(node, state, peer)
            state = control.set_preferred(state, V if step % 2 == 0 else H, at(60 * 24 * 40 + step)).state
            self.assertTrue(state.access_on, f"step {step}")
            self.assertEqual(control.route_delivery(state), control.Route(users=True, super_user=False))
        self.assertTrue(control.from_store(control.to_store(state)).state.access_on)

    def test_only_the_command_changes_it_and_a_command_always_says_so(self):
        off = control.set_access(control.ControlState(), False, at(0))
        self.assertEqual(off.effects, ("access_off",))
        on = control.set_access(off.state, True, at(1))
        self.assertEqual(on.effects, ("access_on",))


class NoticeSentTests(unittest.TestCase):
    def test_it_clears_only_the_notice_that_was_sent(self):
        queued = control.set_access(control.ControlState(), False, at(0)).state
        self.assertEqual(control.notice_sent(queued, "restored").pending_notice, "offline")
        self.assertIsNone(control.notice_sent(queued, "offline").pending_notice)

    def test_an_unknown_kind_changes_nothing(self):
        queued = control.set_access(control.ControlState(), False, at(0)).state
        self.assertEqual(control.notice_sent(queued, "other"), queued)


class SetPreferredTests(unittest.TestCase):
    def test_choosing_the_vps_records_it_with_one_effect(self):
        change = control.set_preferred(control.ControlState(), V, at(0))
        self.assertEqual(change.state.preferred, V)
        self.assertEqual(change.state.preferred_changed_at, at(0))
        self.assertEqual(change.effects, ("preferred_changed",))

    def test_choosing_the_same_node_again_changes_nothing(self):
        change = control.set_preferred(control.ControlState(preferred=V), V, at(5))
        self.assertEqual((change.state.preferred_changed_at, change.effects), (None, ()))

    def test_a_bad_node_name_is_refused_and_the_state_is_untouched(self):
        with self.assertRaises(ValueError):
            control.set_preferred(control.ControlState(), "MAIN", at(0))

    def test_choosing_a_node_never_touches_access_or_the_pending_mail(self):
        off = control.set_access(control.ControlState(), False, at(0)).state
        changed = control.set_preferred(off, V, at(1)).state
        self.assertEqual((changed.access_on, changed.pending_notice), (False, "offline"))


class RoutingTests(unittest.TestCase):
    def test_while_access_is_on_alerts_go_to_the_normal_recipients_only(self):
        self.assertEqual(control.route_delivery(control.ControlState()), control.Route(users=True, super_user=False))

    def test_while_access_is_off_alerts_go_to_the_super_user_only(self):
        off = control.set_access(control.ControlState(), False, at(0)).state
        self.assertEqual(control.route_delivery(off), control.Route(users=False, super_user=True))


class StoreTests(unittest.TestCase):
    def test_every_state_survives_a_round_trip(self):
        states = [control.ControlState(),
                  control.set_access(control.ControlState(), False, at(0)).state,
                  control.set_preferred(control.set_access(control.ControlState(), False, at(0)).state, V, at(3)).state,
                  control.notice_sent(control.set_access(control.notice_sent(control.set_access(control.ControlState(), False, at(0)).state, "offline"), True, at(9)).state, "restored")]
        for state in states:
            loaded = control.from_store(control.to_store(state))
            self.assertEqual(loaded.state, state)
            self.assertEqual(loaded.problems, ())

    def test_nothing_stored_is_a_fresh_node_and_not_a_problem(self):
        loaded = control.from_store({})
        self.assertEqual((loaded.state, loaded.problems), (control.ControlState(), ()))

    def test_a_garbled_access_value_falls_back_to_on_and_says_so(self):
        loaded = control.from_store({"control_access": "maybe"})
        self.assertTrue(loaded.state.access_on)
        self.assertEqual(len(loaded.problems), 1)
        self.assertIn("control_access", loaded.problems[0])

    def test_a_garbled_preference_falls_back_to_core_and_says_so(self):
        loaded = control.from_store({"control_access": "off", "control_preferred": "both"})
        self.assertEqual((loaded.state.access_on, loaded.state.preferred), (False, H))        # the readable field is kept
        self.assertEqual(len(loaded.problems), 1)

    def test_a_garbled_or_naive_time_is_dropped_and_reported(self):
        loaded = control.from_store({"control_access_at": "yesterday", "control_off_at": "2026-10-25T14:30:00"})
        self.assertIsNone(loaded.state.access_changed_at)
        self.assertIsNone(loaded.state.off_at)
        self.assertEqual(len(loaded.problems), 2)

    def test_a_garbled_pending_notice_is_dropped_and_reported(self):
        loaded = control.from_store({"control_pending": "later"})
        self.assertIsNone(loaded.state.pending_notice)
        self.assertEqual(len(loaded.problems), 1)

    def test_the_stored_values_are_plain_short_text(self):
        stored = control.to_store(control.set_access(control.ControlState(), False, at(0)).state)
        self.assertEqual(stored["control_access"], "off")
        self.assertEqual(stored["control_preferred"], "CORE")
        self.assertEqual(stored["control_pending"], "offline")
        self.assertTrue(all(isinstance(v, str) for v in stored.values()))


class MessageTests(unittest.TestCase):
    def test_the_offline_mail_says_when_and_what_stops(self):
        message = control.offline_message(at(0))
        self.assertEqual(message.subject, "[Watchdog] Service is OFFLINE")
        self.assertIn("switched off at 14:30 UTC", message.body)
        self.assertIn("no alert e-mails or phone push messages will be sent", message.body)
        self.assertIn("Contact ClarixTech to restore it.", message.body)

    def test_the_back_mail_says_when_how_long_and_that_alerts_were_not_sent(self):
        message = control.restored_message(at(0), at(130))
        self.assertEqual(message.subject, "[Watchdog] Service is back")
        self.assertIn("switched back on at 16:40 UTC after 2 h 10 min", message.body)
        self.assertIn("Alerts raised while it was off were not sent.", message.body)

    def test_the_back_mail_copes_with_a_missing_off_time(self):
        message = control.restored_message(None, at(130))
        self.assertIn("switched back on at 16:40 UTC", message.body)
        self.assertNotIn("after", message.body)

    def test_short_and_long_durations(self):
        self.assertIn("after 45 s", control.restored_message(at(0), at(0) + timedelta(seconds=45)).body)
        self.assertIn("after 20 min", control.restored_message(at(0), at(20)).body)
        self.assertIn("after 26 h 0 min", control.restored_message(at(0), at(26 * 60)).body)

    def test_the_preference_mail_names_the_node_and_who_alerts(self):
        message = control.preference_message(V, at(0))
        self.assertEqual(message.subject, "[Watchdog] Preferred node is now VPS")
        self.assertIn("The VPS", message.body)
        self.assertIn("Core takes over by itself", message.body)

    def test_messages_are_plain_ascii_without_dashes_or_secrets(self):
        for message in (control.offline_message(at(0)), control.restored_message(at(0), at(5)), control.preference_message(H, at(0))):
            text = message.subject + message.body
            self.assertTrue(text.isascii())
            self.assertNotIn(chr(0x2014), text)


class FallbackTextTests(unittest.TestCase):
    def test_the_takeover_text_names_who_is_down_and_who_took_over(self):
        text = control.peer_down_text(V, H, "HTTP 502")
        self.assertEqual(text.title, "VPS watchdog is DOWN")
        self.assertIn("VPS has stopped answering. CORE has taken over alerting.", text.summary)
        self.assertIn("Last error: HTTP 502.", text.summary)

    def test_the_stand_down_text_says_how_long(self):
        text = control.peer_up_text(H, V, 600)
        self.assertEqual(text.title, "CORE watchdog is back")
        self.assertIn("after 10 min", text.summary)
        self.assertIn("VPS has stood down", text.summary)

    def test_the_error_is_cut_and_kept_to_one_line(self):
        text = control.peer_down_text(V, H, "a\nb " + "x" * 500)
        self.assertNotIn("\n", text.summary)
        self.assertLess(len(text.summary), 260)

    def test_an_unknown_node_stops_the_call(self):
        with self.assertRaises(ValueError):
            control.peer_down_text("MAIN", H)


class SnapshotTests(unittest.TestCase):
    def test_the_status_block_has_state_and_no_secret(self):
        off = control.set_access(control.ControlState(), False, at(0)).state
        block = control.snapshot(off, ("something",))
        self.assertEqual(block["access"], "off")
        self.assertEqual(block["preferred"], H)
        self.assertEqual(block["off_since"], at(0).isoformat())
        self.assertEqual(block["problems"], ["something"])
        self.assertEqual(set(block), {"access", "preferred", "off_since", "access_changed_at", "preferred_changed_at", "pending_notice", "problems"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
