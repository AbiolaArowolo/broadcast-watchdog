# File:         test_matrix_logic.py
# Purpose:      Tests of core-listener/app/matrix.py: the protocol bytes against the manufacturer's guide and the real answer from the owner's matrix, the presets, which preset is
#               active, the switch algorithm against a fake matrix (lost datagram, ignored datagram, silence, roll back, someone else acting, two presses at once), the status
#               cache, the settings and the small log table.
# Inputs:       None (a fake matrix, a fake clock and an in-memory database). Nothing leaves this machine.
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/matrix.py and ops/matrix_fixtures.py.
"""Run: python ops/test_matrix_logic.py

What is proven here, in plain words (the unpublished design notes): the packets are the ones the guide prints; a switch sends only the outputs that are wrong; success is reported only
from a read-back that matches; a lost datagram is resent; a route that never applies is rolled back and reported with the outputs and what the matrix says; a matrix that
stops answering is never described as switched; a second press while one runs is refused without sending anything; and no preset is ever shown as active unless the matrix says so.
"""
import sqlite3
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import matrix_fixtures as fx  # noqa: E402

matrix = fx.matrix
T0 = datetime(2026, 11, 9, 11, 45, 0, tzinfo=timezone.utc)


def make_switcher(fake, **options):
    """A Switcher with a clock that only moves when asked and a sleep that does not wait."""
    clock = {"now": 0.0}
    switcher = matrix.Switcher(fake, clock=lambda: T0 + timedelta(seconds=clock["now"]), monotonic=lambda: clock["now"], sleep=lambda seconds: clock.update(now=clock["now"] + seconds), **options)
    switcher.test_clock = clock
    return switcher


class ProtocolTests(unittest.TestCase):
    def test_the_read_packet_is_the_one_the_guide_prints(self):
        self.assertEqual(matrix.read_packet().hex(" "), "a5 6c 14 00 82 01 01 00 00 00 00 00 00 00 00 00 53 fc 01 ae")

    def test_the_frame_builder_reproduces_a_second_example_from_the_guide(self):
        # "Read Status of LCD": device type 03, command 50, checksum 7a 01. Proves the checksum rule on bytes this code was not written from.
        self.assertEqual(matrix.frame(0x03, 0x50).hex(" "), "a5 6c 14 00 03 01 01 00 00 00 00 00 00 00 00 00 50 7a 01 ae")

    def test_route_commands_are_the_guides_ascii_form(self):
        self.assertEqual(matrix.route_command(2, (1, 2, 5)), b"2v1,2,5.")
        self.assertEqual(matrix.route_command(3, (6,)), b"3v6.")
        self.assertEqual(matrix.route_command(7, (5,)), b"7v5.")

    def test_a_route_command_refuses_anything_outside_one_input_and_real_outputs(self):
        for bad_input in (0, 9, -1, "2", None, True, 2.0):
            with self.subTest(input=bad_input), self.assertRaises(ValueError):
                matrix.route_command(bad_input, (1,))
        for bad_outputs in ((), (0,), (9,), (1, 1), ("1",), (True,), "All", None, (1.5,)):
            with self.subTest(outputs=bad_outputs), self.assertRaises(ValueError):
                matrix.route_command(2, bad_outputs)

    def test_the_all_forms_of_the_guide_are_never_built(self):
        for preset in matrix.PRESETS.values():
            for command in matrix.commands_for(preset, fx.EVERYTHING_ON_BRIGHTSIGN):
                self.assertNotIn(b"All", command)
                self.assertNotIn(b"#", command)

    def test_the_real_answer_from_the_matrix_parses_to_the_routing_it_showed(self):
        self.assertEqual(matrix.parse_status(fx.REAL_REPLY), fx.LIVE_ROUTING)

    def test_the_independent_builder_agrees_with_the_real_answer(self):
        self.assertEqual(fx.status_reply(fx.LIVE_ROUTING), fx.REAL_REPLY)

    def test_a_reply_that_is_not_a_clean_status_answer_is_refused_with_a_reason(self):
        good = fx.status_reply(fx.LIVE_ROUTING)
        cases = {
            "empty": b"",
            "short": good[:30],
            "wrong header": b"\x00" + good[1:],
            "wrong end": good[:-1] + b"\x00",
            "bad checksum": fx.status_reply(fx.LIVE_ROUTING, checksum_delta=1),
            "length field disagrees": fx.status_reply(fx.LIVE_ROUTING, length_delta=1),
            "another command": fx.status_reply(fx.LIVE_ROUTING, command=0x50),
            "error status": fx.status_reply(fx.LIVE_ROUTING, status=0x01),
            "not bytes": None,
        }
        for name, reply in cases.items():
            with self.subTest(name), self.assertRaises(matrix.MatrixError):
                matrix.parse_status(reply)

    def test_an_output_number_outside_one_to_eight_is_kept_as_unknown_not_dropped(self):
        routing = matrix.parse_status(fx.status_reply((2, 255, 0, 3, 7, 3, 7, 7)))
        self.assertEqual(routing, (2, 255, 0, 3, 7, 3, 7, 7))
        self.assertEqual([entry["input"] for entry in matrix.output_rows(routing)][:3], [2, None, None])


class PresetTests(unittest.TestCase):
    def test_main_and_backup_are_exactly_what_the_owner_wrote(self):
        main, backup = matrix.PRESETS["main"], matrix.PRESETS["backup"]
        self.assertEqual(main.wanted(), {1: 2, 2: 2, 5: 2, 6: 3})
        self.assertEqual(backup.wanted(), {1: 7, 2: 7, 5: 7, 6: 5})
        self.assertEqual(main.outputs, (1, 2, 5, 6))
        self.assertEqual(backup.outputs, (1, 2, 5, 6))

    def test_brightsign_is_input_1_to_outputs_1_to_6_and_asks_for_confirmation(self):
        # The owner's answer of 2026-11-10 ("1,2,3,4,5,6", replacing "12345" of 2026-11-09): the emergency outputs are 1 to 6.
        brightsign = matrix.PRESETS["brightsign"]
        self.assertTrue(brightsign.configured)
        self.assertEqual(brightsign.wanted(), {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1})
        self.assertEqual(brightsign.outputs, (1, 2, 3, 4, 5, 6))
        self.assertTrue(brightsign.needs_confirm)
        self.assertFalse(matrix.PRESETS["main"].needs_confirm)
        self.assertFalse(matrix.PRESETS["backup"].needs_confirm)
        self.assertEqual(matrix.PRESET_ORDER, ("main", "backup", "brightsign"))

    def test_main_and_backup_stay_strictly_as_written_and_never_set_outputs_3_and_4(self):
        # The owner's answer of 2026-11-09: "follow this route ... STRICTLY". So an emergency leaves outputs 3 and 4 on BrightSign until they are changed by hand.
        for key in ("main", "backup"):
            self.assertEqual(matrix.PRESETS[key].outputs, (1, 2, 5, 6))
        for command in matrix.commands_for(matrix.PRESETS["main"], fx.EVERYTHING_ON_BRIGHTSIGN) + matrix.commands_for(matrix.PRESETS["backup"], fx.EVERYTHING_ON_BRIGHTSIGN):
            outputs = {int(number) for number in command.decode().split("v")[1].rstrip(".").split(",")}      # the part after the "v" names OUTPUTS (input 3 is a real source)
            self.assertFalse(outputs & {3, 4}, command)

    def test_input_names_are_the_owners(self):
        self.assertEqual(matrix.INPUT_NAMES, {1: "BrightSign Player", 2: "Main Server USA", 3: "Main Server Caribbean", 5: "Backup Server Caribbean", 7: "Backup Server USA"})

    def test_from_a_full_change_main_sends_one_datagram_per_input_in_the_preset_order(self):
        sent = matrix.commands_for(matrix.PRESETS["main"], fx.EVERYTHING_ON_BRIGHTSIGN)
        self.assertEqual(sent, [b"2v1,2,5.", b"3v6."])
        sent = matrix.commands_for(matrix.PRESETS["backup"], fx.EVERYTHING_ON_BRIGHTSIGN)
        self.assertEqual(sent, [b"7v1,2,5.", b"5v6."])

    def test_only_outputs_that_are_wrong_are_sent(self):
        self.assertEqual(matrix.commands_for(matrix.PRESETS["main"], fx.LIVE_ROUTING), [b"2v5."])
        self.assertEqual(matrix.commands_for(matrix.PRESETS["backup"], fx.LIVE_ROUTING), [b"7v1,2.", b"5v6."])
        self.assertEqual(matrix.commands_for(matrix.PRESETS["main"], fx.MAIN_ROUTING), [])

    def test_a_preset_never_touches_an_output_it_does_not_own(self):
        for key in ("main", "backup"):
            preset = matrix.PRESETS[key]
            for command in matrix.commands_for(preset, fx.EVERYTHING_ON_BRIGHTSIGN):
                outputs = {int(n) for n in command.decode().split("v")[1].rstrip(".").split(",")}
                self.assertLessEqual(outputs, set(preset.outputs), key)

    def test_the_live_routing_matches_no_preset_and_the_page_can_say_which_outputs_differ(self):
        self.assertIsNone(matrix.active_preset(fx.LIVE_ROUTING))
        self.assertEqual(matrix.differences(fx.LIVE_ROUTING, matrix.PRESETS["main"]), [5])
        self.assertEqual(matrix.differences(fx.LIVE_ROUTING, matrix.PRESETS["backup"]), [1, 2, 6])

    def test_a_preset_is_active_only_when_every_one_of_its_outputs_matches(self):
        self.assertEqual(matrix.active_preset(fx.MAIN_ROUTING), "main")
        self.assertEqual(matrix.active_preset(fx.BACKUP_ROUTING), "backup")
        for index in range(8):
            if index + 1 in (1, 2, 5, 6):
                broken = list(fx.MAIN_ROUTING)
                broken[index] = 8
                self.assertIsNone(matrix.active_preset(tuple(broken)), f"output {index + 1} wrong")

    def test_outputs_outside_the_preset_do_not_decide_which_preset_is_active(self):
        other = list(fx.MAIN_ROUTING)
        other[2], other[3], other[6], other[7] = 8, 8, 8, 8
        self.assertEqual(matrix.active_preset(tuple(other)), "main")

    def test_nothing_is_active_without_a_reading(self):
        self.assertIsNone(matrix.active_preset(None))

    def test_brightsign_is_active_when_outputs_1_to_6_carry_input_1_whatever_7_and_8_carry(self):
        self.assertEqual(matrix.active_preset(fx.EVERYTHING_ON_BRIGHTSIGN), "brightsign")
        self.assertEqual(matrix.active_preset((1, 1, 1, 1, 1, 1, 7, 7)), "brightsign")
        self.assertIsNone(matrix.active_preset((1, 1, 1, 1, 1, 3, 7, 7)))        # output 6 is part of the emergency now
        self.assertIsNone(matrix.active_preset((1, 1, 1, 1, 2, 1, 7, 7)))

    def test_a_preset_with_no_outputs_set_is_never_active_and_has_no_differences(self):
        empty = matrix.Preset("brightsign", "BRIGHTSIGN PLAYER", "EMERGENCY OPERATION", (), needs_confirm=True)
        with mock.patch.dict(matrix.PRESETS, {"brightsign": empty}):
            self.assertIsNone(matrix.active_preset(fx.EVERYTHING_ON_BRIGHTSIGN))
            self.assertEqual(matrix.differences(fx.LIVE_ROUTING, empty), [])
            self.assertEqual(matrix.commands_for(empty, fx.LIVE_ROUTING), [])

    def test_brightsign_is_one_datagram_so_it_is_applied_in_one_step(self):
        self.assertEqual(matrix.commands_for(matrix.PRESETS["brightsign"], fx.LIVE_ROUTING), [b"1v1,2,3,4,5,6."])
        self.assertEqual(matrix.commands_for(matrix.PRESETS["brightsign"], (1, 1, 2, 1, 1, 3, 7, 7)), [b"1v3,6."])
        self.assertEqual(matrix.commands_for(matrix.PRESETS["brightsign"], fx.EVERYTHING_ON_BRIGHTSIGN), [])


class SwitchTests(unittest.TestCase):
    def test_main_from_the_live_routing_sends_only_output_5_and_is_confirmed(self):
        fake = fx.FakeMatrix(fx.LIVE_ROUTING)
        outcome = make_switcher(fake).switch("main")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.state, "done")
        self.assertEqual(fake.sent, ["2v5."])
        self.assertEqual(outcome.after, fx.MAIN_ROUTING)
        self.assertEqual(outcome.before, fx.LIVE_ROUTING)
        self.assertEqual(outcome.changed_elsewhere, [])
        self.assertEqual(outcome.attempts, 1)

    def test_backup_from_the_live_routing_sends_two_datagrams_and_leaves_other_outputs_alone(self):
        fake = fx.FakeMatrix(fx.LIVE_ROUTING)
        outcome = make_switcher(fake).switch("backup")
        self.assertEqual(outcome.state, "done")
        self.assertEqual(fake.sent, ["7v1,2.", "5v6."])
        self.assertEqual(tuple(fake.routing), (7, 7, 2, 3, 7, 5, 7, 7))        # outputs 3, 4, 7, 8 are exactly as they were

    def test_a_preset_that_is_already_in_place_sends_nothing(self):
        fake = fx.FakeMatrix(fx.MAIN_ROUTING)
        outcome = make_switcher(fake).switch("main")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.state, "already")
        self.assertEqual(fake.sent, [])

    def test_a_silent_matrix_is_offline_nothing_is_sent_and_nothing_is_assumed(self):
        fake = fx.FakeMatrix()
        fake.offline = True
        switcher = make_switcher(fake)
        outcome = switcher.switch("main")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.state, "offline")
        self.assertEqual(fake.sent, [])
        self.assertIsNone(outcome.after)
        self.assertIn("did not answer", outcome.message)
        self.assertIsNone(switcher.status()["active"])

    def test_a_lost_datagram_is_noticed_by_the_read_back_and_only_that_one_is_resent(self):
        fake = fx.FakeMatrix(fx.EVERYTHING_ON_BRIGHTSIGN)
        fake.drop_sends = 1
        outcome = make_switcher(fake).switch("main")
        self.assertEqual(outcome.state, "done")
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(fake.sent, ["2v1,2,5.", "3v6.", "2v1,2,5."])

    def test_a_route_that_never_applies_fails_after_three_tries_and_names_the_output_and_what_the_matrix_says(self):
        fake = fx.FakeMatrix(fx.LIVE_ROUTING)
        fake.ignore = lambda text: True
        outcome = make_switcher(fake).switch("main")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.state, "failed")
        self.assertEqual(outcome.attempts, 3)
        self.assertEqual(fake.sent, ["2v5.", "2v5.", "2v5."])
        self.assertEqual(outcome.failed_outputs, [{"output": 5, "wanted": 2, "got": 7}])
        self.assertTrue(outcome.restored)                                   # nothing had changed, so the earlier routing is in place
        self.assertEqual(outcome.after, fx.LIVE_ROUTING)
        self.assertIn("output 5", outcome.message)

    def test_a_half_applied_preset_is_rolled_back_to_the_routing_from_before_the_press(self):
        fake = fx.FakeMatrix(fx.EVERYTHING_ON_BRIGHTSIGN)
        fake.ignore = lambda text: text == "3v6."
        outcome = make_switcher(fake).switch("main")
        self.assertEqual(outcome.state, "failed")
        self.assertEqual(fake.sent, ["2v1,2,5.", "3v6.", "3v6.", "3v6.", "1v1,2,5."])
        self.assertEqual(outcome.failed_outputs, [{"output": 6, "wanted": 3, "got": 1}])
        self.assertTrue(outcome.restored)
        self.assertEqual(outcome.after, fx.EVERYTHING_ON_BRIGHTSIGN)
        self.assertIn("put back", outcome.message)

    def test_a_rollback_that_does_not_take_is_reported_as_not_restored_with_the_outputs(self):
        fake = fx.FakeMatrix(fx.EVERYTHING_ON_BRIGHTSIGN)
        fake.ignore = lambda text: text == "3v6." or text.startswith("1v")
        outcome = make_switcher(fake).switch("main")
        self.assertEqual(outcome.state, "failed")
        self.assertFalse(outcome.restored)
        self.assertEqual(outcome.after, (2, 2, 1, 1, 2, 1, 1, 1))
        self.assertIn("could NOT be put back", outcome.message)

    def test_an_output_whose_earlier_input_is_unknown_cannot_be_restored_and_is_listed(self):
        fake = fx.FakeMatrix((255, 2, 2, 3, 7, 1, 7, 7))             # output 1 reads as an unknown input and output 6 is wrong, and the matrix ignores the datagram for output 6
        fake.ignore = lambda text: text == "3v6."
        outcome = make_switcher(fake).switch("main")
        self.assertEqual(outcome.state, "failed")
        self.assertFalse(outcome.restored)
        self.assertEqual(outcome.unrestorable, [1])

    def test_a_send_the_system_refuses_is_retried_like_a_lost_datagram_and_never_escapes(self):
        fake = fx.FakeMatrix(fx.EVERYTHING_ON_BRIGHTSIGN)
        fake.fail_sends = 1                                                  # the first datagram cannot be sent (EPERM from a firewall rule, ENETUNREACH ...)
        outcome = make_switcher(fake).switch("main")
        self.assertEqual((outcome.state, outcome.attempts), ("done", 2))
        self.assertEqual(fake.sent, ["2v1,2,5.", "3v6.", "2v1,2,5."])

    def test_when_no_send_ever_works_it_fails_cleanly_with_the_earlier_routing_in_place(self):
        fake = fx.FakeMatrix(fx.LIVE_ROUTING)
        fake.fail_sends = 99
        outcome = make_switcher(fake).switch("main")
        self.assertEqual((outcome.state, outcome.attempts, outcome.restored), ("failed", 3, True))
        self.assertEqual(outcome.after, fx.LIVE_ROUTING)

    def test_a_rollback_whose_send_fails_is_reported_as_not_restored_not_raised(self):
        fake = fx.FakeMatrix(fx.EVERYTHING_ON_BRIGHTSIGN)
        fake.ignore = lambda text: text == "3v6."
        fake.send_error = lambda text: text.startswith("1v")                # the rollback datagram cannot be sent
        outcome = make_switcher(fake).switch("main")
        self.assertEqual(outcome.state, "failed")
        self.assertFalse(outcome.restored)
        self.assertIn("could NOT be put back", outcome.message)

    def test_an_unexpected_error_after_the_first_send_ends_as_unverified_with_what_was_sent(self):
        fake = fx.FakeMatrix(fx.EVERYTHING_ON_BRIGHTSIGN)
        fake.after_send = lambda text: (_ for _ in ()).throw(RuntimeError("boom")) if text == "3v6." else None
        outcome = make_switcher(fake).switch("main")
        self.assertEqual((outcome.ok, outcome.state), (False, "unverified"))
        self.assertEqual(outcome.sent, ["2v1,2,5.", "3v6."])
        self.assertIn("not confirmed", outcome.message)

    def test_a_matrix_that_stops_answering_after_the_send_is_unverified_and_nothing_is_claimed(self):
        fake = fx.FakeMatrix(fx.LIVE_ROUTING)
        fake.fail_reads_after_send = True
        outcome = make_switcher(fake).switch("main")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.state, "unverified")
        self.assertIsNone(outcome.after)
        self.assertEqual(fake.sent, ["2v5."])                                # no blind resend and no blind rollback
        self.assertIn("not confirmed", outcome.message)

    def test_an_untouched_output_changed_by_someone_else_is_reported_but_is_not_a_failure(self):
        fake = fx.FakeMatrix(fx.LIVE_ROUTING)
        fake.after_send = lambda text: fake.routing.__setitem__(7, 1)         # someone uses the manufacturer's Control Panel on output 8 meanwhile
        outcome = make_switcher(fake).switch("main")
        self.assertEqual(outcome.state, "done")
        self.assertEqual(outcome.changed_elsewhere, [{"output": 8, "was": 7, "now": 1}])
        self.assertIn("output 8", outcome.message)                           # the operator is told, not only the data
        self.assertIn("was input 7, now input 1", outcome.message)

    def test_a_preset_whose_outputs_are_not_set_cannot_be_pressed_and_sends_nothing(self):
        fake = fx.FakeMatrix()
        empty = matrix.Preset("brightsign", "BRIGHTSIGN PLAYER", "EMERGENCY OPERATION", (), needs_confirm=True)
        with mock.patch.dict(matrix.PRESETS, {"brightsign": empty}):
            outcome = make_switcher(fake).switch("brightsign")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.state, "not_configured")
        self.assertEqual((fake.sent, fake.reads), ([], 0))

    def test_brightsign_from_the_live_routing_is_one_datagram_and_leaves_outputs_7_and_8_alone(self):
        fake = fx.FakeMatrix(fx.LIVE_ROUTING)
        outcome = make_switcher(fake).switch("brightsign")
        self.assertEqual(outcome.state, "done")
        self.assertEqual(fake.sent, ["1v1,2,3,4,5,6."])
        self.assertEqual(tuple(fake.routing), (1, 1, 1, 1, 1, 1, 7, 7))
        self.assertEqual(make_switcher(fake).status()["active"], "brightsign")

    def test_a_brightsign_that_does_not_take_is_rolled_back_on_outputs_1_to_6_only(self):
        fake = fx.FakeMatrix(fx.LIVE_ROUTING)
        fake.ignore = lambda text: text.startswith("1v1")                      # the matrix ignores the emergency datagram
        outcome = make_switcher(fake).switch("brightsign")
        self.assertEqual(outcome.state, "failed")
        self.assertTrue(outcome.restored)
        self.assertEqual(sorted(item["output"] for item in outcome.failed_outputs), [1, 2, 3, 4, 5, 6])
        self.assertEqual(tuple(fake.routing), fx.LIVE_ROUTING)

    def test_main_after_an_emergency_does_exactly_what_was_written_and_leaves_outputs_3_and_4_on_brightsign(self):
        fake = fx.FakeMatrix(fx.EVERYTHING_ON_BRIGHTSIGN)
        outcome = make_switcher(fake).switch("main")
        self.assertEqual(outcome.state, "done")
        self.assertEqual(tuple(fake.routing), (2, 2, 1, 1, 2, 3, 1, 1))

    def test_an_unknown_preset_is_refused(self):
        for bad in ("nope", "", "MAIN", None, 3, "all"):
            with self.subTest(preset=bad), self.assertRaises(ValueError):
                make_switcher(fx.FakeMatrix()).switch(bad)

    def test_the_outcome_is_json_ready_and_carries_the_times(self):
        import json
        outcome = make_switcher(fx.FakeMatrix()).switch("main")
        data = json.loads(json.dumps(outcome.as_dict()))
        self.assertEqual({"ok", "state", "message", "preset", "before", "after", "failed_outputs", "restored", "unrestorable", "changed_elsewhere", "attempts", "sent", "started_at", "finished_at"}, set(data))
        self.assertTrue(data["started_at"].startswith("2026-11-09T11:45:00"))


class ConcurrencyTests(unittest.TestCase):
    def hold_first_switch(self, fake):
        entered, release = threading.Event(), threading.Event()
        fake.on_send = lambda text: (entered.set(), release.wait(5))
        return entered, release

    def test_a_second_press_while_one_runs_is_refused_at_once_and_sends_nothing(self):
        fake = fx.FakeMatrix(fx.EVERYTHING_ON_BRIGHTSIGN)
        entered, release = self.hold_first_switch(fake)
        switcher = make_switcher(fake)
        first = {}
        worker = threading.Thread(target=lambda: first.update(outcome=switcher.switch("main")))
        worker.start()
        self.assertTrue(entered.wait(5))
        sent_so_far = list(fake.sent)
        second = switcher.switch("backup")
        self.assertEqual(second.state, "busy")
        self.assertFalse(second.ok)
        self.assertEqual(fake.sent, sent_so_far)
        release.set()
        worker.join(5)
        self.assertEqual(first["outcome"].state, "done")

    def test_the_lock_is_released_after_a_failure_and_after_an_exception(self):
        fake = fx.FakeMatrix()
        fake.offline = True
        switcher = make_switcher(fake)
        self.assertEqual(switcher.switch("main").state, "offline")
        fake.offline = False
        self.assertEqual(switcher.switch("main").state, "done")
        fake.on_send = lambda text: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertEqual(switcher.switch("backup").state, "unverified")        # an unexpected error is an outcome, never an escape
        fake.on_send = None
        self.assertNotEqual(switcher.switch("backup").state, "busy")

    def test_status_during_a_switch_does_not_read_the_matrix_and_says_it_is_switching(self):
        fake = fx.FakeMatrix(fx.MAIN_ROUTING)                     # MAIN is in place and cached, so "active" would be main if nothing guarded it
        switcher = make_switcher(fake)
        self.assertEqual(switcher.status()["active"], "main")
        reads = fake.reads
        entered, release = self.hold_first_switch(fake)
        worker = threading.Thread(target=lambda: switcher.switch("backup"))
        worker.start()
        self.assertTrue(entered.wait(5))
        reads_before = fake.reads
        view = switcher.status()
        self.assertTrue(view["switching"])
        self.assertEqual(fake.reads, reads_before)
        self.assertGreaterEqual(reads_before, reads)
        self.assertIsNone(view["active"])                          # while a switch runs the matrix is half way: no preset is claimed
        self.assertTrue(all(item["active"] is False for item in view["presets"]))
        release.set()
        worker.join(5)


class StatusTests(unittest.TestCase):
    def test_the_status_lists_the_eight_outputs_with_names_and_no_active_preset_for_the_live_routing(self):
        view = make_switcher(fx.FakeMatrix(fx.LIVE_ROUTING)).status()
        self.assertTrue(view["reachable"])
        self.assertEqual(view["routing"], list(fx.LIVE_ROUTING))
        self.assertIsNone(view["active"])
        self.assertEqual(len(view["outputs"]), 8)
        self.assertEqual(view["outputs"][0], {"output": 1, "input": 2, "input_name": "Main Server USA"})
        self.assertEqual(view["outputs"][3], {"output": 4, "input": 3, "input_name": "Main Server Caribbean"})
        self.assertEqual(view["outputs"][7]["input_name"], "Backup Server USA")
        main = next(item for item in view["presets"] if item["key"] == "main")
        self.assertEqual((main["active"], main["differs"], main["configured"]), (False, [5], True))
        brightsign = next(item for item in view["presets"] if item["key"] == "brightsign")
        self.assertEqual((brightsign["active"], brightsign["configured"], brightsign["needs_confirm"], brightsign["differs"]), (False, True, True, [1, 2, 3, 4, 5, 6]))
        self.assertEqual(brightsign["routes"], [{"input": 1, "input_name": "BrightSign Player", "outputs": [1, 2, 3, 4, 5, 6]}])

    def test_an_input_nobody_named_is_shown_by_its_number_and_an_unreadable_value_as_unknown(self):
        rows = matrix.output_rows((4, 255, 0, 3, 7, 3, 7, 7))
        self.assertEqual(rows[0]["input_name"], "Input 4")
        self.assertEqual(rows[1]["input_name"], "unknown")

    def test_the_active_preset_comes_only_from_a_reading(self):
        self.assertEqual(make_switcher(fx.FakeMatrix(fx.MAIN_ROUTING)).status()["active"], "main")
        self.assertEqual(make_switcher(fx.FakeMatrix(fx.BACKUP_ROUTING)).status()["active"], "backup")

    def test_an_unreachable_matrix_shows_offline_with_no_routing_and_no_active_preset_even_after_a_good_reading(self):
        fake = fx.FakeMatrix(fx.MAIN_ROUTING)
        switcher = make_switcher(fake)
        self.assertEqual(switcher.status()["active"], "main")
        fake.offline = True
        switcher.test_clock["now"] += 10
        view = switcher.status()
        self.assertIs(view["reachable"], False)
        self.assertIsNone(view["routing"])
        self.assertIsNone(view["active"])
        self.assertTrue(all(item["active"] is False for item in view["presets"]))
        self.assertTrue(view["error"])

    def test_two_looks_within_two_seconds_read_the_matrix_once_and_a_later_one_reads_again(self):
        fake = fx.FakeMatrix()
        switcher = make_switcher(fake)
        switcher.status()
        switcher.test_clock["now"] += 1.9
        switcher.status()
        self.assertEqual(fake.reads, 1)
        switcher.test_clock["now"] += 0.2
        switcher.status()
        self.assertEqual(fake.reads, 2)

    def test_another_thread_clearing_the_cache_between_the_read_and_the_answer_cannot_break_status(self):
        class Racy(matrix.Switcher):
            def _remember(self, routing, *rest):
                checked = super()._remember(routing, *rest)
                with self._cache_lock:
                    self._cache = None                                       # what a failed read in another thread does at the worst moment
                return checked
        view = Racy(fx.FakeMatrix(fx.LIVE_ROUTING), sleep=lambda seconds: None).status()
        self.assertTrue(view["reachable"])
        self.assertTrue(view["checked_at"])

    def test_an_older_reading_never_overwrites_a_newer_one_in_the_cache(self):
        switcher = make_switcher(fx.FakeMatrix(fx.LIVE_ROUTING))
        switcher._remember(fx.MAIN_ROUTING, 5.0)                          # the switch's read-back, started at t=5
        switcher._remember(fx.LIVE_ROUTING, 3.0)                          # a status read that started at t=3 and finished later
        self.assertEqual(switcher._cache[0], fx.MAIN_ROUTING)
        switcher._remember(fx.BACKUP_ROUTING, 6.0)
        self.assertEqual(switcher._cache[0], fx.BACKUP_ROUTING)

    def test_a_forced_refresh_always_reads(self):
        fake = fx.FakeMatrix()
        switcher = make_switcher(fake)
        switcher.status()
        switcher.status(fresh=True)
        self.assertEqual(fake.reads, 2)

    def test_after_a_switch_the_next_look_shows_the_read_back_without_asking_again_within_two_seconds(self):
        fake = fx.FakeMatrix(fx.LIVE_ROUTING)
        switcher = make_switcher(fake)
        switcher.switch("main")
        reads = fake.reads
        self.assertEqual(switcher.status()["active"], "main")
        self.assertEqual(fake.reads, reads)


class UdpLinkTests(unittest.TestCase):
    """The real transport on real loopback sockets, against a stand-in matrix."""

    def setUp(self):
        self.standin = fx.StandInMatrix(fx.LIVE_ROUTING)
        self.addCleanup(self.standin.close)
        self.link = matrix.UdpLink("127.0.0.1", self.standin.port, timeout=0.25, attempts=2)

    def received_after(self, pause=0.3):
        import time
        time.sleep(pause)
        return list(self.standin.received)

    def test_a_datagram_arrives_exactly_as_built_and_nothing_comes_back_to_wait_for(self):
        self.link.send(matrix.route_command(2, (5,)))
        self.assertEqual(self.received_after(), [b"2v5."])
        self.assertEqual(self.standin.routing[4], 2)

    def test_the_question_on_the_wire_is_the_guides_packet_and_the_answer_is_read(self):
        self.assertEqual(self.link.read_status(), fx.LIVE_ROUTING)
        self.assertEqual(self.standin.received[0].hex(" "), "a5 6c 14 00 82 01 01 00 00 00 00 00 00 00 00 00 53 fc 01 ae")

    def test_silence_is_a_matrix_error_after_every_try_and_not_a_hang(self):
        import time
        self.standin.replies[:] = [None, None]
        began = time.monotonic()
        with self.assertRaises(matrix.MatrixError) as caught:
            self.link.read_status()
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertIn("no answer", str(caught.exception))
        self.assertEqual(sum(1 for data in self.standin.received if len(data) == 20), 2)        # it asked twice

    def test_a_lost_first_answer_is_covered_by_the_second_try(self):
        self.standin.replies[:] = [None]
        self.assertEqual(self.link.read_status(), fx.LIVE_ROUTING)

    def test_a_stray_datagram_before_the_real_answer_is_ignored(self):
        self.standin.replies[:] = [[b"junk", fx.status_reply(fx.MAIN_ROUTING)]]
        self.assertEqual(self.link.read_status(), fx.MAIN_ROUTING)

    def test_only_unusable_answers_end_as_a_matrix_error_that_says_why(self):
        self.standin.replies[:] = [fx.status_reply(fx.LIVE_ROUTING, checksum_delta=1), fx.status_reply(fx.LIVE_ROUTING, checksum_delta=1)]
        with self.assertRaises(matrix.MatrixError) as caught:
            self.link.read_status()
        self.assertIn("unusable answer", str(caught.exception))
        self.assertIn("checksum", str(caught.exception))

    def test_an_answer_that_comes_from_another_address_is_ignored(self):
        import socket
        import time
        foreign = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(foreign.close)
        foreign.bind(("127.0.0.2", 0))                                   # not the matrix's address (127.0.0.1)
        self.standin.replies[:] = [None, None]                           # the real matrix says nothing

        def answer_from_elsewhere():
            for _ in range(200):
                if self.standin.sources:
                    foreign.sendto(fx.status_reply(fx.MAIN_ROUTING), self.standin.sources[-1])
                    return
                time.sleep(0.005)
        delivered = threading.Event()

        def answer_and_note():
            answer_from_elsewhere()
            delivered.set()
        helper = threading.Thread(target=answer_and_note)
        helper.start()
        with self.assertRaises(matrix.MatrixError):
            self.link.read_status()
        helper.join(5)
        self.assertTrue(delivered.is_set(), "the foreign answer was never sent, so this test proved nothing")

    def test_a_socket_the_system_will_not_give_is_a_matrix_error_for_both_send_and_read(self):
        with mock.patch("socket.socket", side_effect=OSError(24, "too many open files")):
            with self.assertRaises(matrix.MatrixError):
                self.link.send(b"2v5.")
            with self.assertRaises(matrix.MatrixError):
                self.link.read_status()

    def test_a_full_switch_over_real_sockets_is_confirmed_by_the_read_back(self):
        switcher = matrix.Switcher(self.link, sleep=lambda seconds: None)
        outcome = switcher.switch("backup")
        self.assertEqual(outcome.state, "done")
        self.assertEqual(self.standin.routing, [7, 7, 2, 3, 7, 5, 7, 7])


class SettingsTests(unittest.TestCase):
    def test_a_private_ipv4_address_turns_the_feature_on_with_the_default_port(self):
        settings, problems = matrix.settings_from_environ({"WATCHDOG_MATRIX_HOST": "192.168.7.50"})
        self.assertEqual((settings.host, settings.port, problems), ("192.168.7.50", 7000, []))

    def test_no_host_means_off_and_is_not_a_problem(self):
        self.assertEqual(matrix.settings_from_environ({}), (None, []))
        self.assertEqual(matrix.settings_from_environ({"WATCHDOG_MATRIX_HOST": "  "}), (None, []))

    def test_a_name_a_public_address_or_nonsense_is_refused_and_the_feature_stays_off(self):
        for bad in ("matrix.local", "8.8.8.8", "192.168.7.256", "192.168.0", "::1", "192.168.7.50:7000", "http://192.168.7.50", "169.254.1.1", "0.0.0.0", "224.0.0.1", "255.255.255.255"):
            with self.subTest(host=bad):
                settings, problems = matrix.settings_from_environ({"WATCHDOG_MATRIX_HOST": bad})
                self.assertIsNone(settings)
                self.assertEqual(len(problems), 1)

    def test_loopback_is_allowed_so_the_live_tests_can_run(self):
        settings, _ = matrix.settings_from_environ({"WATCHDOG_MATRIX_HOST": "127.0.0.1", "WATCHDOG_MATRIX_PORT": "40000"})
        self.assertEqual((settings.host, settings.port), ("127.0.0.1", 40000))

    def test_a_bad_port_is_refused(self):
        for bad in ("0", "80", "70000", "x", "-1", "7000.5"):
            with self.subTest(port=bad):
                settings, problems = matrix.settings_from_environ({"WATCHDOG_MATRIX_HOST": "192.168.7.50", "WATCHDOG_MATRIX_PORT": bad})
                self.assertIsNone(settings)
                self.assertEqual(len(problems), 1)


class LogTableTests(unittest.TestCase):
    def conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        matrix.init_table(conn)
        return conn

    def row(self, conn, at, result="done", operator="owner@mail.example", preset="main", via="staff-door"):
        matrix.insert_log(conn, at.isoformat(), operator, preset, result, via, {"before": [1] * 8, "after": [2] * 8})

    def test_creating_the_table_twice_is_harmless(self):
        conn = self.conn()
        matrix.init_table(conn)
        self.assertEqual(matrix.recent(conn, 10), [])

    def test_the_last_success_is_the_newest_done_row_and_ignores_failures_and_no_ops(self):
        conn = self.conn()
        self.assertIsNone(matrix.last_success(conn))
        self.row(conn, T0, "done", preset="backup")
        self.row(conn, T0 + timedelta(minutes=1), "failed")
        self.row(conn, T0 + timedelta(minutes=2), "already")
        self.row(conn, T0 + timedelta(minutes=3), "offline")
        found = matrix.last_success(conn)
        self.assertEqual((found["preset"], found["at"], found["operator"]), ("backup", T0.isoformat(), "owner@mail.example"))

    def test_recent_is_newest_first_and_limited(self):
        conn = self.conn()
        for minute in range(5):
            self.row(conn, T0 + timedelta(minutes=minute))
        rows = matrix.recent(conn, 3)
        self.assertEqual(len(rows), 3)
        self.assertGreater(rows[0]["at"], rows[1]["at"])

    def test_the_table_keeps_only_the_newest_rows(self):
        conn = self.conn()
        for minute in range(7):
            self.row(conn, T0 + timedelta(minutes=minute))
        matrix.prune(conn, keep=4)
        rows = matrix.recent(conn, 100)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[-1]["at"], (T0 + timedelta(minutes=3)).isoformat())

    def test_an_operator_name_with_control_characters_is_stored_without_them(self):
        conn = self.conn()
        self.row(conn, T0, operator="bad\r\nname\x00" + "x" * 300)
        stored = matrix.recent(conn, 1)[0]["operator"]
        self.assertNotRegex(stored, r"[\x00-\x1f\x7f]")
        self.assertLessEqual(len(stored), 80)


if __name__ == "__main__":
    unittest.main(verbosity=2)
