# File:         test_staff_logic.py
# Purpose:      Tests of core-listener/app/staff.py: staff accounts (at most 10), shifts (several people at once, no timer ends one), failures (grouped red alarms),
#               assignment (only the super user, only people on shift, several per failure), Done, the row log both nodes copy, and the staff sign-in cookie.
# Inputs:       None (made-up rows, a fixed clock).
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/staff.py (pure logic: no network, no clock of its own, no database) and control_access.py (only to prove a staff cookie is not a super cookie).
"""Run: python ops/test_staff_logic.py

What is proven here, in plain words: up to ten staff can exist and never more; a shift is ended only by Finish shift (the person's or the super user's) and no amount of
elapsed time changes that; red alarms of one studio less than ten minutes apart are one failure, and a failure someone marked Done is never reopened by a later alarm;
only the super user assigns, only to people who are on shift, and several people can hold one failure; a person marks Done only a failure assigned to them; every action is
a new row that is never changed, so two nodes holding different rows converge by copying what they lack, in any order and any number of times; and a staff cookie can never
pass as the super user's, nor his as theirs.
"""
import importlib
import json
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

LISTENER_DIR = Path(__file__).resolve().parent.parent / "core-listener"
sys.path.insert(0, str(LISTENER_DIR))
staff = importlib.import_module("app.staff")
control_access = importlib.import_module("app.control_access")

T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
HASH = "scrypt:16384:8:1:" + "a" * 22 + ":" + "b" * 43
HASH2 = "scrypt:16384:8:1:" + "c" * 22 + ":" + "d" * 43
SUPER = staff.SUPER


def at(minutes=0.0):
    return T0 + timedelta(minutes=minutes)


def person(name):
    return staff.Actor("staff", name)


class World:
    """A node's log: every action goes through plan() and is appended as one row, then the picture is replayed from all rows."""

    def __init__(self, node="CORE"):
        self.rows, self.count, self.node = [], 0, node

    @property
    def state(self):
        return staff.replay(self.rows)

    def nonce(self):
        self.count += 1
        return f"{self.count:06x}"

    def do(self, actor, action, minutes=0.0, **args):
        row = staff.plan(self.state, actor, action, args, at(minutes), self.node, self.nonce())
        self.rows.append(row)
        return row

    def red(self, studio, minutes, cause="Silence > 60s", display=None, gap=None):
        kwargs = {} if gap is None else {"gap": gap}
        row = staff.failure_row(self.state, studio, cause, display or studio, at(minutes), self.node, self.nonce(), **kwargs)
        self.rows.append(row)
        return row

    def add(self, username, display=None, minutes=0.0):
        return self.do(SUPER, "add_person", minutes, username=username, display=display or username.title(), password_hash=HASH)

    def on_shift(self, *names, minutes=0.0):
        for name in names:
            self.add(name, minutes=minutes) if name not in self.state["people"] else None
            self.do(person(name), "start_shift", minutes)


class NameRuleTests(unittest.TestCase):
    def test_good_usernames(self):
        for name in ("ada", "kemi.o", "zara-2", "a1b", "x" * 24):
            self.assertIsNone(staff.username_problem(name), name)

    def test_bad_usernames(self):
        for name in ("", "ad", "x" * 25, "Ada", "ada kemi", "ada@x.com", ".ada", "ada.", "-ada", "ada-", "adéa", "ada\n", None, 7, ["ada"]):
            self.assertIsNotNone(staff.username_problem(name), repr(name))

    def test_names_that_look_like_authority_are_reserved(self):
        for name in ("admin", "administrator", "super", "superuser", "root", "system", "watchdog"):
            self.assertIsNotNone(staff.username_problem(name), name)

    def test_good_display_names(self):
        for text in ("Ada", "Kemi O.", "Zara Ọ.", "O'Brien", "Mary-Jane", "A" * 30):
            self.assertIsNone(staff.display_problem(text), text)

    def test_bad_display_names(self):
        for text in ("", " Ada", "Ada ", "Ada  Obi", "A" * 31, "<b>Ada</b>", "Ada & Kemi", 'Ada "the"', "Ada\n", "Ada;", None, 5):
            self.assertIsNotNone(staff.display_problem(text), repr(text))


class AccountTests(unittest.TestCase):
    def test_the_super_user_adds_a_person_who_starts_active(self):
        world = World()
        row = world.add("ada", "Ada")
        self.assertEqual((row["kind"], row["person"]), ("person_added", "ada"))
        info = world.state["people"]["ada"]
        self.assertTrue(info["active"])
        self.assertEqual(info["display"], "Ada")

    def test_staff_cannot_add_people(self):
        world = World()
        world.add("ada")
        with self.assertRaises(staff.StaffError):
            world.do(person("ada"), "add_person", username="kemi", display="Kemi", password_hash=HASH)

    def test_a_username_is_unique_and_a_disabled_one_stays_reserved(self):
        world = World()
        world.add("ada")
        with self.assertRaises(staff.StaffError):
            world.add("ada")
        world.do(SUPER, "disable_person", username="ada")
        with self.assertRaises(staff.StaffError):
            world.add("ada")

    def test_ten_active_staff_are_allowed_and_an_eleventh_is_not(self):
        world = World()
        for index in range(10):
            world.add(f"person{index}")
        with self.assertRaises(staff.StaffError):
            world.add("person10")
        self.assertEqual(staff.MAX_ACTIVE_STAFF, 10)

    def test_disabling_someone_frees_a_place_and_keeps_their_record(self):
        world = World()
        for index in range(10):
            world.add(f"person{index}")
        world.do(SUPER, "disable_person", username="person3")
        world.add("newcomer")
        people = world.state["people"]
        self.assertFalse(people["person3"]["active"])
        self.assertTrue(people["newcomer"]["active"])
        self.assertEqual(sum(1 for item in people.values() if item["active"]), 10)

    def test_a_bad_username_display_or_hash_is_refused(self):
        world = World()
        for kwargs in ({"username": "A", "display": "Ada", "password_hash": HASH}, {"username": "ada", "display": "<b>", "password_hash": HASH},
                       {"username": "ada", "display": "Ada", "password_hash": "plaintext-password"}, {"username": "ada", "display": "Ada", "password_hash": ""}):
            with self.assertRaises(staff.StaffError):
                world.do(SUPER, "add_person", **kwargs)
        self.assertEqual(world.state["people"], {})

    def test_the_super_user_resets_a_password_and_the_tag_changes(self):
        world = World()
        world.add("ada")
        before = world.state["people"]["ada"]["tag"]
        world.do(SUPER, "reset_password", username="ada", password_hash=HASH2)
        self.assertNotEqual(world.state["people"]["ada"]["tag"], before)

    def test_a_person_changes_only_their_own_password(self):
        world = World()
        world.add("ada")
        world.add("kemi")
        world.do(person("ada"), "change_password", password_hash=HASH2)
        self.assertEqual(world.state["people"]["ada"]["hash"], HASH2)
        self.assertEqual(world.state["people"]["kemi"]["hash"], HASH)
        with self.assertRaises(staff.StaffError):
            world.do(person("ada"), "reset_password", username="kemi", password_hash=HASH2)

    def test_staff_cannot_disable_anyone_and_the_unknown_is_refused(self):
        world = World()
        world.add("ada")
        with self.assertRaises(staff.StaffError):
            world.do(person("ada"), "disable_person", username="ada")
        with self.assertRaises(staff.StaffError):
            world.do(SUPER, "disable_person", username="ghost")
        with self.assertRaises(staff.StaffError):
            world.do(SUPER, "reset_password", username="ghost", password_hash=HASH)

    def test_a_disabled_person_cannot_act_or_change_a_password(self):
        world = World()
        world.add("ada")
        world.do(SUPER, "disable_person", username="ada")
        for action, args in (("start_shift", {}), ("change_password", {"password_hash": HASH2})):
            with self.assertRaises(staff.StaffError):
                world.do(person("ada"), action, **args)

    def test_an_actor_with_an_unknown_role_can_do_nothing(self):
        world = World()
        world.add("ada")
        for actor in (staff.Actor("visitor", ""), staff.Actor("", ""), staff.Actor("super", "ada")):
            with self.assertRaises(staff.StaffError):
                world.do(actor, "disable_person", username="ada")
        with self.assertRaises(staff.StaffError):
            world.do(SUPER, "no_such_action")


class DeleteTests(unittest.TestCase):
    """His request (2026-11-02): the super user can DELETE an account, not only disable it. A disabled name stayed reserved for ever (ifeanyi had to be re-made as ifeanyi1), so a
    delete frees the name and the slot. The log still only ever gains rows, so a delete is one more row; what it must never do is rewrite what people did while they were here."""

    def test_the_super_user_deletes_a_person_and_the_name_is_free_again(self):
        world = World()
        world.add("ada", "Ada")
        row = world.do(SUPER, "delete_person", 1, username="ada")
        self.assertEqual((row["kind"], row["person"], row["data"]), ("person_deleted", "ada", {}))
        self.assertNotIn("ada", world.state["people"])
        world.add("ada", "Ada Again", minutes=2)
        info = world.state["people"]["ada"]
        self.assertEqual((info["display"], info["active"]), ("Ada Again", True))

    def test_a_disabled_person_can_be_deleted_too(self):
        world = World()
        world.add("ada")
        world.do(SUPER, "disable_person", 1, username="ada")
        world.do(SUPER, "delete_person", 2, username="ada")
        self.assertNotIn("ada", world.state["people"])
        world.add("ada", minutes=3)                              # before this, a disabled name stayed taken for ever
        self.assertTrue(world.state["people"]["ada"]["active"])

    def test_only_the_super_user_deletes_and_only_someone_who_exists(self):
        world = World()
        world.add("ada")
        world.add("kemi")
        for actor in (person("ada"), person("kemi"), staff.Actor("visitor", "")):
            with self.assertRaises(staff.StaffError):
                world.do(actor, "delete_person", username="kemi")
        for bad in ("ghost", "", None, 7, ["ada"], "Ada"):
            with self.assertRaises(staff.StaffError, msg=repr(bad)):
                world.do(SUPER, "delete_person", username=bad)
        self.assertEqual(sorted(world.state["people"]), ["ada", "kemi"])

    def test_a_delete_gives_the_slot_back(self):
        world = World()
        for index in range(staff.MAX_ACTIVE_STAFF):
            world.add(f"person{index}")
        with self.assertRaises(staff.StaffError):
            world.add("one-too-many")
        world.do(SUPER, "delete_person", 1, username="person3")
        world.add("one-too-many", minutes=2)
        self.assertEqual(sum(1 for p in world.state["people"].values() if p["active"]), staff.MAX_ACTIVE_STAFF)

    def test_deleting_ends_the_shift_and_takes_the_person_off_open_failures(self):
        world = World()
        world.on_shift("ada", "kemi")
        world.red("MAIN-USA", 1)
        fid = next(iter(world.state["failures"]))
        world.do(SUPER, "assign", 2, fid=fid, people=["ada", "kemi"])
        world.do(SUPER, "delete_person", 3, username="ada")
        state = world.state
        self.assertNotIn("ada", state["shifts"])
        self.assertEqual(state["failures"][fid]["people"], ["kemi"])
        self.assertEqual([name for name, _ in staff._on_shift(state)], ["kemi"])
        self.assertEqual(staff.public_roster(state), ["Kemi"])

    def test_a_name_made_again_does_not_inherit_the_old_accounts_open_failure(self):
        # The cleanup on delete matters here: the new "ada" exists in the final picture, so an assignment made to the OLD ada would otherwise land on her.
        world = World()
        world.on_shift("ada", "kemi")
        world.red("MAIN-USA", 1)
        fid = next(iter(world.state["failures"]))
        world.do(SUPER, "assign", 2, fid=fid, people=["ada", "kemi"])
        world.do(SUPER, "delete_person", 3, username="ada")
        world.add("ada", "New Ada", minutes=4)
        self.assertEqual(world.state["failures"][fid]["people"], ["kemi"])
        self.assertNotIn("ada", world.state["shifts"])
        world.do(person("ada"), "start_shift", 5)
        world.do(SUPER, "assign", 6, fid=fid, people=["ada"])                                     # the new account can be given the same failure afresh
        self.assertEqual(sorted(world.state["failures"][fid]["people"]), ["ada", "kemi"])

    def test_what_the_person_did_keeps_their_name_in_the_history(self):
        world = World()
        world.add("kemi")
        world.on_shift("ada")
        world.red("MAIN-USA", 1)
        fid = next(iter(world.state["failures"]))
        world.do(SUPER, "assign", 2, fid=fid, people=["ada"])
        world.do(person("ada"), "done", 3, fid=fid, note="restarted the player")
        world.do(SUPER, "delete_person", 4, username="ada")
        seen = staff.staff_view(world.state, "kemi", at(5))
        self.assertEqual([item["done_by"] for item in seen["done"]], ["Ada"])                    # the display name, not the bare username and not a blank
        self.assertEqual(staff.panel_view(world.state, at(5))["done"][0]["done_by"], "Ada")

    def test_a_deleted_persons_sign_in_cookie_is_dead_at_once(self):
        world = World()
        world.add("ada")
        state = world.state
        token = staff.issue_session("k" * 32, "ada", state["people"]["ada"]["tag"], at(0))
        self.assertEqual(staff.verify_session("k" * 32, token, at(1), state), "ada")
        world.do(SUPER, "delete_person", 2, username="ada")
        self.assertIsNone(staff.verify_session("k" * 32, token, at(3), world.state))
        world.do(SUPER, "add_person", 4, username="ada", display="Ada", password_hash=HASH2)     # a NEW account of the same name (a new salt, so a new password tag)
        self.assertIsNone(staff.verify_session("k" * 32, token, at(5), world.state), "the old cookie must not work for the new account")

    def test_a_name_made_again_is_a_new_person_and_the_old_ones_rows_do_not_reach_it(self):
        world = World()
        world.add("ada", "Old Ada")
        world.do(SUPER, "reset_password", 1, username="ada", password_hash=HASH2)
        world.do(SUPER, "start_shift", 2, username="ada")
        world.do(SUPER, "delete_person", 3, username="ada")
        world.add("ada", "New Ada", minutes=4)                                                   # made with HASH
        info = world.state["people"]["ada"]
        self.assertEqual((info["display"], info["hash"]), ("New Ada", HASH))                    # not the old account's reset
        self.assertNotIn("ada", world.state["shifts"])                                           # and not the old account's shift
        world.do(SUPER, "reset_password", 5, username="ada", password_hash=HASH2)
        self.assertEqual(world.state["people"]["ada"]["hash"], HASH2)                           # the new account answers to a later reset as usual

    def test_the_picture_is_the_same_whatever_order_the_rows_arrive_in(self):
        world = World()
        world.add("ada", "Old Ada")
        world.do(SUPER, "reset_password", 1, username="ada", password_hash=HASH2)
        world.do(SUPER, "start_shift", 2, username="ada")
        world.do(SUPER, "delete_person", 3, username="ada")
        world.add("ada", "New Ada", minutes=4)
        world.do(SUPER, "start_shift", 5, username="ada")
        world.add("kemi", minutes=6)
        world.do(SUPER, "disable_person", 7, username="kemi")
        world.do(SUPER, "delete_person", 8, username="kemi")
        expected = staff.replay(world.rows)
        self.assertEqual(sorted(expected["people"]), ["ada"])
        self.assertEqual(expected["people"]["ada"]["display"], "New Ada")
        self.assertIn("ada", expected["shifts"])
        shuffler = random.Random(20261102)
        for _ in range(60):
            rows = list(world.rows)
            shuffler.shuffle(rows)
            self.assertEqual(staff.replay(rows), expected)
            self.assertEqual(staff.replay(rows + rows[:3]), expected)                            # a row that arrives twice changes nothing

    def test_a_node_whose_clock_is_a_little_behind_still_lands_the_first_accounts_rows(self):
        # The rule that makes the old behaviour safe: a row for a person that was NEVER deleted lands even when the other node's clock dated it just before the account's own row.
        world = World()
        added = world.add("ada", minutes=5)
        early = staff.make_row("person_password", "ada", {"hash": HASH2}, at(4.9), "VPS", "0000aa")           # written on the VPS, dated slightly BEFORE the add
        self.assertIsNone(staff.row_problem(early))
        self.assertEqual(staff.replay([added, early])["people"]["ada"]["hash"], HASH2)

    def test_a_delete_row_has_exactly_one_shape(self):
        world = World()
        world.add("ada")
        row = world.do(SUPER, "delete_person", 1, username="ada")
        self.assertIn("person_deleted", staff.KINDS)
        self.assertIsNone(staff.row_problem(row, at(2)))
        for broken in ({**row, "data": {"by": "x"}}, {**row, "person": "Ada"}, {**row, "person": ""}, {**row, "data": {"hash": HASH}}):
            self.assertIsNotNone(staff.row_problem(broken, at(2)), broken)


class ShiftTests(unittest.TestCase):
    def test_a_person_starts_and_finishes_a_shift(self):
        world = World()
        world.add("ada")
        world.do(person("ada"), "start_shift", 1)
        self.assertIn("ada", world.state["shifts"])
        world.do(person("ada"), "finish_shift", 5)
        self.assertNotIn("ada", world.state["shifts"])

    def test_two_or_more_people_can_be_on_shift_together_in_start_order(self):
        world = World()
        world.add("ada")
        world.add("kemi")
        world.add("zara")
        world.do(person("kemi"), "start_shift", 1)
        world.do(person("ada"), "start_shift", 2)
        world.do(person("zara"), "start_shift", 3)
        self.assertEqual(staff.public_roster(world.state), ["Kemi", "Ada", "Zara"])

    def test_starting_twice_or_finishing_when_off_shift_is_refused(self):
        world = World()
        world.add("ada")
        with self.assertRaises(staff.StaffError):
            world.do(person("ada"), "finish_shift")
        world.do(person("ada"), "start_shift")
        with self.assertRaises(staff.StaffError):
            world.do(person("ada"), "start_shift")

    def test_the_super_user_can_start_a_shift_for_an_active_person_who_is_not_on_one(self):
        world = World()
        world.add("ada")
        row = world.do(SUPER, "start_shift", 1, username="ada")
        self.assertEqual((row["kind"], row["person"], row["data"]), ("shift_started", "ada", {}))          # the same row a person's own start makes: no new row shape
        self.assertIsNone(staff.row_problem(row))
        self.assertEqual(list(world.state["shifts"]), ["ada"])

    def test_the_super_user_cannot_start_it_for_a_stranger_a_disabled_person_or_someone_already_on_shift(self):
        world = World()
        world.add("ada")
        world.add("kemi")
        world.do(person("kemi"), "start_shift")
        for username in ("ghost", None, 5, "", ["ada"]):
            with self.assertRaises(staff.StaffError, msg=repr(username)):
                world.do(SUPER, "start_shift", username=username)
        with self.assertRaises(staff.StaffError):
            world.do(SUPER, "start_shift", username="kemi")
        world.do(SUPER, "disable_person", username="ada")
        with self.assertRaises(staff.StaffError):
            world.do(SUPER, "start_shift", username="ada")

    def test_staff_start_only_their_own_shift(self):
        world = World()
        world.add("ada")
        world.add("kemi")
        with self.assertRaises(staff.StaffError):
            world.do(person("ada"), "start_shift", username="kemi")
        self.assertEqual(world.state["shifts"], {})
        world.do(person("ada"), "start_shift", username="ada")                                               # naming themselves is fine
        self.assertEqual(list(world.state["shifts"]), ["ada"])

    def test_staff_cannot_finish_someone_elses_shift_but_the_super_user_can(self):
        world = World()
        world.on_shift("ada", "kemi")
        with self.assertRaises(staff.StaffError):
            world.do(person("kemi"), "finish_shift", username="ada")
        world.do(SUPER, "finish_shift", 2, username="ada")
        self.assertEqual(list(world.state["shifts"]), ["kemi"])

    def test_disabling_a_person_takes_them_off_shift(self):
        world = World()
        world.on_shift("ada", "kemi")
        world.do(SUPER, "disable_person", 3, username="ada")
        self.assertEqual(staff.public_roster(world.state), ["Kemi"])

    def test_the_person_can_start_a_new_shift_after_finishing(self):
        world = World()
        world.on_shift("ada")
        world.do(person("ada"), "finish_shift", 10)
        world.do(person("ada"), "start_shift", 20)
        self.assertEqual(world.state["shifts"]["ada"], at(20).isoformat())


class NoTimerEndsAShiftTests(unittest.TestCase):
    """His rule (2026-11-01): a shift takes up to 48 hours and is only Start and Finish. Time alone must never end one, as with the access switch."""

    def test_a_shift_is_still_running_a_month_later(self):
        world = World()
        world.on_shift("ada")
        view = staff.staff_view(world.state, "ada", at(60 * 24 * 30))
        self.assertTrue(view["me"]["on_shift"])
        self.assertEqual([item["display"] for item in view["roster"]], ["Ada"])

    def test_a_shift_over_48_hours_is_marked_long_but_not_ended(self):
        world = World()
        world.on_shift("ada")
        under = staff.staff_view(world.state, "ada", at(60 * 47))
        over = staff.staff_view(world.state, "ada", at(60 * 49))
        self.assertFalse(under["me"]["long"])
        self.assertTrue(over["me"]["long"])
        self.assertTrue(over["me"]["on_shift"])
        panel = staff.panel_view(world.state, at(60 * 49))
        self.assertTrue(panel["roster"][0]["long"])
        self.assertEqual((panel["people"][0]["on_shift"], panel["people"][0]["long"]), (True, True))
        self.assertEqual(staff.LONG_SHIFT_SECONDS, 48 * 3600)

    def test_replay_has_no_clock_so_it_cannot_end_anything(self):
        import inspect
        self.assertEqual(list(inspect.signature(staff.replay).parameters), ["rows"])

    def test_a_sign_in_that_expired_does_not_touch_the_shift(self):
        world = World()
        world.on_shift("ada")
        token = staff.issue_session("k" * 32, "ada", world.state["people"]["ada"]["tag"], at(0))
        self.assertIsNone(staff.verify_session("k" * 32, token, at(60 * 49), world.state))
        self.assertIn("ada", world.state["shifts"])


class FailureGroupingTests(unittest.TestCase):
    def test_the_first_red_alarm_opens_a_failure(self):
        world = World()
        row = world.red("SITE-A", 0, "Silence > 60s")
        self.assertEqual(row["kind"], "failure_opened")
        failure = list(world.state["failures"].values())[0]
        self.assertEqual((failure["studio"], failure["cause"], failure["alarms"]), ("SITE-A", "Silence > 60s", 1))

    def test_a_red_less_than_ten_minutes_later_is_the_same_failure(self):
        world = World()
        world.red("SITE-A", 0)
        row = world.red("SITE-A", 6, "Off Air")
        self.assertEqual(row["kind"], "failure_extended")
        self.assertEqual(len(world.state["failures"]), 1)
        failure = list(world.state["failures"].values())[0]
        self.assertEqual(failure["alarms"], 2)
        self.assertEqual(failure["last_at"], at(6).isoformat())
        self.assertEqual(failure["cause"], "Silence > 60s")          # the first cause stays; the latest is kept apart
        self.assertEqual(failure["latest_cause"], "Off Air")

    def test_a_red_more_than_ten_minutes_after_the_last_is_a_new_failure(self):
        world = World()
        world.red("SITE-A", 0)
        world.red("SITE-A", 10.5)
        self.assertEqual(len(world.state["failures"]), 2)

    def test_reds_every_eight_minutes_for_an_hour_are_one_failure(self):
        world = World()
        for minute in range(0, 60, 8):
            world.red("SITE-A", minute)
        failures = list(world.state["failures"].values())
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["alarms"], 8)

    def test_another_studio_is_another_failure(self):
        world = World()
        world.red("SITE-A", 0)
        world.red("SITE-B", 1)
        self.assertEqual(len(world.state["failures"]), 2)

    def test_every_playout_is_its_own_failure_even_when_they_share_a_studio_or_a_pc(self):
        # 2026-11-01: MAIN and BACKUP-SERVER have two playouts each and a failure is counted per playout. The key is the playout's own id, so "MAIN" (a studio name) and
        # "MAIN-USA" (one of its playouts) are different keys, and the same feed on MAIN and on BACKUP is two failures.
        world = World()
        for playout in ("MAIN-USA", "MAIN-Caribbean", "MAIN", "BACKUP-USA", "BACKUP-Caribbean"):
            world.red(playout, 1)
        self.assertEqual(sorted(f["studio"] for f in world.state["failures"].values()), ["BACKUP-Caribbean", "BACKUP-USA", "MAIN", "MAIN-Caribbean", "MAIN-USA"])

    def test_a_failure_marked_done_is_not_reopened_by_a_later_red(self):
        world = World()
        world.red("SITE-A", 0)
        fid = next(iter(world.state["failures"]))
        world.do(SUPER, "done", 2, fid=fid, note="restarted")
        row = world.red("SITE-A", 4)
        self.assertEqual(row["kind"], "failure_opened")
        self.assertEqual(len(world.state["failures"]), 2)
        self.assertIsNotNone(world.state["failures"][fid]["done"])
        self.assertEqual(world.state["failures"][fid]["alarms"], 1)

    def test_a_gap_of_zero_makes_every_red_its_own_item(self):
        world = World()
        for minute in (0, 1, 2):
            world.red("SITE-A", minute, gap=0)
        self.assertEqual(len(world.state["failures"]), 3)
        self.assertEqual(staff.FAILURE_GAP_SECONDS, 600)

    def test_the_failure_id_comes_from_the_studio_and_the_first_alarm(self):
        world = World()
        world.red("BACKUP-SERVER", 0)
        self.assertEqual(next(iter(world.state["failures"])), "20261101T120000-BACKUP-SERVER")

    def test_a_late_arriving_older_alarm_joins_instead_of_splitting(self):
        world = World()
        world.red("SITE-A", 5)
        world.red("SITE-A", 3)
        self.assertEqual(len(world.state["failures"]), 1)

    def test_an_alarm_dated_long_before_the_failure_does_not_join_it(self):
        world = World()
        world.red("SITE-A", 600)
        row = world.red("SITE-A", 0)
        self.assertEqual(row["kind"], "failure_opened")
        self.assertEqual(len(world.state["failures"]), 2)

    def test_bad_failure_input_is_refused(self):
        world = World()
        for studio, cause in (("", "x"), ("x" * 41, "x"), ("SITE-A", ""), ("SITE-A", "x" * 81), (None, "x"), ("SITE-A", None)):
            with self.assertRaises(staff.StaffError):
                staff.failure_row(world.state, studio, cause, "d", at(0), "CORE", "000001")


class StaffPasswordRuleTests(unittest.TestCase):
    """His rule (2026-11-02): staff passwords are SIMPLE on purpose. Letters and digits, even a 4-digit number, and no strength rule is enforced. Only what could not work or could
    do harm is refused. (His own password keeps its own rule in control_login.py.)"""

    def test_simple_passwords_are_fine(self):
        for password in ("1234", "0000", "abcd", "ada1", "password", "ada2026", "a1b2", "Kemi123", "a long enough pass 1", "12 34", "x" * 128):
            self.assertIsNone(staff.staff_password_problem(password), password)

    def test_a_password_may_repeat_a_character_or_contain_the_name(self):
        for password in ("1111", "aaaaaaaa", "ada-ada-ada", "kemi1"):
            self.assertIsNone(staff.staff_password_problem(password), password)

    def test_only_what_could_not_work_is_refused_and_the_words_never_contain_the_password(self):
        for password in (None, 5, [], "", "1", "12", "123", "x" * 129, "ab\ncd", "ab\tcd", "abc\x00d", "    ", "     "):
            problem = staff.staff_password_problem(password)
            self.assertTrue(problem, repr(password))
            if isinstance(password, str) and password.strip():
                self.assertNotIn(password, problem)

    def test_the_limits_are_four_and_one_hundred_and_twenty_eight(self):
        self.assertEqual((staff.MIN_STAFF_PASSWORD_CHARS, staff.MAX_STAFF_PASSWORD_CHARS), (4, 128))
        self.assertIsNone(staff.staff_password_problem("1234"))
        self.assertTrue(staff.staff_password_problem("123"))
        self.assertIsNone(staff.staff_password_problem("x" * 128))
        self.assertTrue(staff.staff_password_problem("x" * 129))


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.on_shift("ada", "kemi")
        self.world.red("SITE-A", 1)
        self.fid = next(iter(self.world.state["failures"]))

    def assigned(self):
        return self.world.state["failures"][self.fid]["people"]

    def test_the_super_user_assigns_one_person(self):
        self.world.do(SUPER, "assign", 2, fid=self.fid, people=["ada"])
        self.assertEqual(self.assigned(), ["ada"])

    def test_several_people_can_hold_one_failure(self):
        self.world.do(SUPER, "assign", 2, fid=self.fid, people=["ada", "kemi"])
        self.assertEqual(self.assigned(), ["ada", "kemi"])

    def test_naming_one_person_twice_in_one_assignment_records_them_once(self):
        row = self.world.do(SUPER, "assign", 2, fid=self.fid, people=["ada", "ada", "kemi"])
        self.assertEqual(row["data"]["people"], ["ada", "kemi"])

    def test_assigning_again_adds_people_without_repeating_anyone(self):
        self.world.do(SUPER, "assign", 2, fid=self.fid, people=["ada"])
        self.world.do(SUPER, "assign", 3, fid=self.fid, people=["ada", "kemi"])
        self.assertEqual(self.assigned(), ["ada", "kemi"])

    def test_staff_cannot_assign_anyone_not_even_themselves(self):
        for who in ("ada", "kemi"):
            with self.assertRaises(staff.StaffError):
                self.world.do(person("ada"), "assign", 2, fid=self.fid, people=[who])
        self.assertEqual(self.assigned(), [])

    def test_only_people_on_shift_can_be_assigned(self):
        self.world.add("zara")
        with self.assertRaises(staff.StaffError):
            self.world.do(SUPER, "assign", 2, fid=self.fid, people=["zara"])
        self.world.do(person("ada"), "finish_shift", 3)
        with self.assertRaises(staff.StaffError):
            self.world.do(SUPER, "assign", 4, fid=self.fid, people=["ada"])
        with self.assertRaises(staff.StaffError):
            self.world.do(SUPER, "assign", 4, fid=self.fid, people=["ghost"])

    def test_one_person_off_shift_in_the_list_refuses_the_whole_assignment(self):
        self.world.add("zara")
        with self.assertRaises(staff.StaffError):
            self.world.do(SUPER, "assign", 2, fid=self.fid, people=["ada", "zara"])
        self.assertEqual(self.assigned(), [])

    def test_an_empty_or_malformed_list_is_refused(self):
        for people in ([], "ada", None, [""], [5], ["ada"] * 11):
            with self.assertRaises(staff.StaffError):
                self.world.do(SUPER, "assign", 2, fid=self.fid, people=people)

    def test_an_unknown_or_finished_failure_is_refused(self):
        with self.assertRaises(staff.StaffError):
            self.world.do(SUPER, "assign", 2, fid="nope", people=["ada"])
        self.world.do(SUPER, "done", 3, fid=self.fid, note="")
        with self.assertRaises(staff.StaffError):
            self.world.do(SUPER, "assign", 4, fid=self.fid, people=["ada"])

    def test_unassign_removes_one_person(self):
        self.world.do(SUPER, "assign", 2, fid=self.fid, people=["ada", "kemi"])
        self.world.do(SUPER, "unassign", 3, fid=self.fid, username="ada")
        self.assertEqual(self.assigned(), ["kemi"])
        with self.assertRaises(staff.StaffError):
            self.world.do(person("kemi"), "unassign", 4, fid=self.fid, username="kemi")

    def test_finishing_a_shift_keeps_the_assignment_as_history(self):
        self.world.do(SUPER, "assign", 2, fid=self.fid, people=["ada"])
        self.world.do(person("ada"), "finish_shift", 9)
        self.assertEqual(self.assigned(), ["ada"])


class DoneTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.on_shift("ada", "kemi")
        self.world.red("MAIN", 1)
        self.fid = next(iter(self.world.state["failures"]))
        self.world.do(SUPER, "assign", 2, fid=self.fid, people=["ada"])

    def done(self):
        return self.world.state["failures"][self.fid]["done"]

    def test_an_assignee_marks_it_done_with_a_note(self):
        self.world.do(person("ada"), "done", 5, fid=self.fid, note="  restarted the player  ")
        self.assertEqual(self.done()["by"], "ada")
        self.assertEqual(self.done()["note"], "restarted the player")

    def test_a_person_who_does_not_hold_it_cannot_mark_it_done(self):
        with self.assertRaises(staff.StaffError):
            self.world.do(person("kemi"), "done", 5, fid=self.fid, note="x")
        self.assertIsNone(self.done())

    def test_the_super_user_can_mark_any_failure_done(self):
        self.world.do(SUPER, "done", 5, fid=self.fid, note="seen on the picture")
        self.assertEqual(self.done()["by"], "super")

    def test_any_one_of_several_assignees_closes_it(self):
        self.world.do(SUPER, "assign", 3, fid=self.fid, people=["kemi"])
        self.world.do(person("kemi"), "done", 5, fid=self.fid, note="")
        self.assertEqual(self.done()["by"], "kemi")

    def test_the_note_is_optional_but_capped(self):
        self.world.do(person("ada"), "done", 5, fid=self.fid)
        self.assertEqual(self.done()["note"], "")
        world = World()
        world.on_shift("ada")
        world.red("MAIN", 0)
        fid = next(iter(world.state["failures"]))
        with self.assertRaises(staff.StaffError):
            world.do(SUPER, "done", 1, fid=fid, note="x" * 201)
        self.assertEqual(staff.MAX_NOTE_CHARS, 200)

    def test_done_twice_or_unknown_is_refused(self):
        self.world.do(person("ada"), "done", 5, fid=self.fid, note="")
        with self.assertRaises(staff.StaffError):
            self.world.do(SUPER, "done", 6, fid=self.fid, note="")
        with self.assertRaises(staff.StaffError):
            self.world.do(SUPER, "done", 6, fid="nope", note="")

    def test_a_person_who_finished_their_shift_can_still_close_what_they_hold(self):
        self.world.do(person("ada"), "finish_shift", 6)
        self.world.do(person("ada"), "done", 7, fid=self.fid, note="done before I left")
        self.assertEqual(self.done()["by"], "ada")

    def test_a_disabled_person_cannot_mark_anything_done(self):
        self.world.do(SUPER, "disable_person", 6, username="ada")
        with self.assertRaises(staff.StaffError):
            self.world.do(person("ada"), "done", 7, fid=self.fid, note="")


class AcknowledgeTests(unittest.TestCase):
    """The "I am on it" button (his spec of 2026-11-04). A person on shift says they have seen an open failure. It records WHO responded and WHEN, so it is kept for every person who
    presses (not only the first), once each, and it never assigns the failure and never lets anyone mark it Done."""

    def setUp(self):
        self.world = World()
        self.world.on_shift("ada", "kemi")
        self.world.red("SITE-A", 1)                              # the failure opens at minute 1
        self.fid = next(iter(self.world.state["failures"]))

    def seen(self):
        return self.world.state["failures"][self.fid]["acknowledged"]

    def test_a_person_on_shift_says_they_are_on_an_open_failure(self):
        row = self.world.do(person("ada"), "acknowledge", 5, fid=self.fid)
        self.assertEqual((row["kind"], row["person"], row["data"]), ("failure_acknowledged", "", {"fid": self.fid, "by": "ada"}))
        self.assertEqual(self.seen(), [{"by": "ada", "at": row["at"]}])

    def test_every_person_who_presses_is_kept_in_the_order_they_pressed(self):
        self.world.do(person("kemi"), "acknowledge", 5, fid=self.fid)
        self.world.do(person("ada"), "acknowledge", 9, fid=self.fid)
        self.assertEqual([item["by"] for item in self.seen()], ["kemi", "ada"])

    def test_the_same_person_cannot_press_twice_and_the_second_press_stores_nothing(self):
        self.world.do(person("ada"), "acknowledge", 5, fid=self.fid)
        before = len(self.world.rows)
        with self.assertRaises(staff.StaffError):
            self.world.do(person("ada"), "acknowledge", 6, fid=self.fid)
        self.assertEqual(len(self.world.rows), before)
        self.assertEqual(len(self.seen()), 1)

    def test_a_person_who_is_not_on_shift_cannot_press(self):
        self.world.add("zara")
        with self.assertRaises(staff.StaffError):
            self.world.do(person("zara"), "acknowledge", 5, fid=self.fid)
        self.world.do(person("ada"), "finish_shift", 6)
        with self.assertRaises(staff.StaffError):
            self.world.do(person("ada"), "acknowledge", 7, fid=self.fid)
        self.assertEqual(self.seen(), [])

    def test_the_super_user_and_a_visitor_cannot_press(self):
        with self.assertRaises(staff.StaffError) as refused:
            self.world.do(SUPER, "acknowledge", 5, fid=self.fid)
        self.assertIn("Assign and Done", str(refused.exception))                       # he is told why: he has those buttons
        for actor in (staff.Actor("visitor", ""), staff.Actor("visitor", "ada"), staff.Actor("staff", "ghost")):        # a visitor who borrows the name of someone on shift is still no one
            with self.assertRaises(staff.StaffError):
                self.world.do(actor, "acknowledge", 5, fid=self.fid)
        self.assertEqual(self.seen(), [])

    def test_a_disabled_person_cannot_press(self):
        self.world.do(SUPER, "disable_person", 4, username="ada")
        with self.assertRaises(staff.StaffError):
            self.world.do(person("ada"), "acknowledge", 5, fid=self.fid)

    def test_an_unknown_or_finished_failure_is_refused(self):
        for fid in ("nope", None, 5, ""):
            with self.assertRaises(staff.StaffError):
                self.world.do(person("ada"), "acknowledge", 5, fid=fid)
        self.world.do(SUPER, "done", 6, fid=self.fid, note="")
        with self.assertRaises(staff.StaffError):
            self.world.do(person("ada"), "acknowledge", 7, fid=self.fid)

    def test_pressing_neither_assigns_the_failure_nor_lets_the_person_mark_it_done(self):
        self.world.do(person("ada"), "acknowledge", 5, fid=self.fid)
        self.assertEqual(self.world.state["failures"][self.fid]["people"], [])
        with self.assertRaises(staff.StaffError):
            self.world.do(person("ada"), "done", 6, fid=self.fid, note="")
        self.assertIsNone(self.world.state["failures"][self.fid]["done"])

    def test_a_person_can_press_for_a_failure_someone_else_holds(self):
        self.world.do(SUPER, "assign", 3, fid=self.fid, people=["kemi"])
        self.world.do(person("ada"), "acknowledge", 5, fid=self.fid)
        self.assertEqual([item["by"] for item in self.seen()], ["ada"])

    def test_a_press_stays_in_the_history_after_the_person_leaves(self):
        self.world.do(person("ada"), "acknowledge", 5, fid=self.fid)
        self.world.do(person("ada"), "finish_shift", 6)
        self.world.do(SUPER, "delete_person", 7, username="ada")
        self.assertEqual([item["by"] for item in self.seen()], ["ada"])
        item = staff.staff_view(self.world.state, "kemi", at(8))["failures"][0]
        self.assertEqual([seen["display"] for seen in item["seen"]], ["Ada"])

    def test_the_press_is_the_same_whatever_order_the_rows_arrive_in_and_however_often(self):
        self.world.do(person("kemi"), "acknowledge", 5, fid=self.fid)
        self.world.do(person("ada"), "acknowledge", 9, fid=self.fid)
        expected = staff.replay(self.world.rows)["failures"]
        rows = self.world.rows + self.world.rows[-2:]
        for seed in range(8):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(staff.replay(shuffled)["failures"], expected)

    def test_the_same_person_pressing_on_both_nodes_counts_once_with_the_earlier_time(self):
        first = self.world.do(person("ada"), "acknowledge", 5, fid=self.fid)
        twin = staff.make_row("failure_acknowledged", "", {"fid": self.fid, "by": "ada"}, at(5.5), "VPS", "0000ff")
        for rows in (self.world.rows + [twin], [twin] + self.world.rows):
            self.assertEqual(staff.replay(rows)["failures"][self.fid]["acknowledged"], [{"by": "ada", "at": first["at"]}])

    def test_a_press_made_on_the_other_node_just_before_it_heard_of_done_is_still_kept(self):
        self.world.do(SUPER, "done", 4, fid=self.fid, note="")
        late = staff.make_row("failure_acknowledged", "", {"fid": self.fid, "by": "ada"}, at(4.5), "VPS", "0000ff")
        self.assertEqual([item["by"] for item in staff.replay(self.world.rows + [late])["failures"][self.fid]["acknowledged"]], ["ada"])

    def test_a_press_for_a_failure_nobody_knows_is_skipped_and_never_raises(self):
        stray = staff.make_row("failure_acknowledged", "", {"fid": "20260101T000000-NOPE", "by": "ada"}, at(5), "VPS", "0000ff")
        self.assertEqual(set(staff.replay(self.world.rows + [stray])["failures"]), {self.fid})


class ReplayTests(unittest.TestCase):
    def busy_world(self):
        world = World()
        world.on_shift("ada", "kemi", minutes=0)
        world.red("SITE-A", 1)
        world.red("SITE-A", 4)
        fid = next(iter(world.state["failures"]))
        world.do(SUPER, "assign", 5, fid=fid, people=["ada", "kemi"])
        world.do(person("kemi"), "finish_shift", 6)
        world.do(person("ada"), "done", 8, fid=fid, note="back on air")
        world.red("SITE-B", 20)
        world.do(SUPER, "reset_password", 21, username="ada", password_hash=HASH2)
        return world

    def test_the_order_rows_arrive_in_does_not_matter(self):
        rows = self.busy_world().rows
        expected = staff.replay(rows)
        for seed in range(25):
            shuffled = rows[:]
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(staff.replay(shuffled), expected, seed)

    def test_a_row_seen_twice_counts_once(self):
        rows = self.busy_world().rows
        self.assertEqual(staff.replay(rows + rows + rows[:3]), staff.replay(rows))

    def test_two_nodes_with_different_rows_converge_by_copying_what_they_lack(self):
        rows = self.busy_world().rows
        node_a, node_b = rows[:9], rows[4:]
        merged_a = {row["id"]: row for row in node_a + node_b}
        merged_b = {row["id"]: row for row in node_b + node_a}
        self.assertEqual(staff.replay(list(merged_a.values())), staff.replay(list(merged_b.values())))
        self.assertEqual(staff.replay(list(merged_a.values())), staff.replay(rows))

    def test_rows_that_make_no_sense_are_ignored_and_never_raise(self):
        world = World()
        world.add("ada")
        base = world.rows[:]
        odd = [
            staff.make_row("shift_finished", "ada", {"by": "ada"}, at(1), "CORE", "00000a"),
            staff.make_row("shift_started", "ghost", {}, at(2), "CORE", "00000b"),
            staff.make_row("failure_assigned", "", {"fid": "nope", "people": ["ada"], "by": "super"}, at(3), "CORE", "00000c"),
            staff.make_row("failure_extended", "", {"fid": "nope", "studio": "X", "cause": "y", "display": "z"}, at(4), "CORE", "00000d"),
            staff.make_row("failure_done", "", {"fid": "nope", "note": "", "by": "super"}, at(5), "CORE", "00000e"),
            staff.make_row("person_password", "ghost", {"hash": HASH2}, at(6), "CORE", "00000f"),
            staff.make_row("person_added", "ada", {"display": "Other", "hash": HASH2}, at(7), "CORE", "000010"),
        ]
        state = staff.replay(base + odd)
        self.assertEqual(state["people"]["ada"]["display"], "Ada")
        self.assertEqual(state["shifts"], {})
        self.assertEqual(state["failures"], {})
        self.assertNotIn("ghost", state["people"])

    def test_an_alarm_after_done_never_changes_a_finished_failure(self):
        world = World()
        world.add("ada")
        world.red("SITE-A", 0)
        fid = next(iter(world.state["failures"]))
        world.do(SUPER, "done", 2, fid=fid, note="")
        late = staff.make_row("failure_extended", "", {"fid": fid, "studio": "SITE-A", "cause": "Off Air", "display": "SITE-A"}, at(3), "VPS", "00000a")
        failure = staff.replay(world.rows + [late])["failures"][fid]
        self.assertEqual((failure["alarms"], failure["latest_cause"], failure["last_at"]), (1, "Silence > 60s", at(0).isoformat()))

    def test_an_empty_log_is_an_empty_picture(self):
        state = staff.replay([])
        self.assertEqual((state["people"], state["shifts"], state["failures"]), ({}, {}, {}))


class ViewTests(unittest.TestCase):
    def test_the_public_roster_is_display_names_only(self):
        world = World()
        world.add("ada.o", "Ada O.")
        world.do(person("ada.o"), "start_shift")
        roster = staff.public_roster(world.state)
        self.assertEqual(roster, ["Ada O."])
        self.assertNotIn("ada.o", json.dumps(roster))

    def test_the_public_roster_is_empty_when_nobody_is_on_shift(self):
        world = World()
        world.add("ada")
        self.assertEqual(staff.public_roster(world.state), [])

    def test_the_staff_view_marks_what_is_mine_and_shows_names_not_usernames(self):
        world = World()
        world.add("ada.o", "Ada O.")
        world.add("kemi", "Kemi")
        world.do(person("ada.o"), "start_shift")
        world.do(person("kemi"), "start_shift")
        world.red("SITE-A", 1)
        fid = next(iter(world.state["failures"]))
        world.do(SUPER, "assign", 2, fid=fid, people=["ada.o"])
        mine = staff.staff_view(world.state, "ada.o", at(3))
        theirs = staff.staff_view(world.state, "kemi", at(3))
        self.assertTrue(mine["failures"][0]["mine"])
        self.assertFalse(theirs["failures"][0]["mine"])
        self.assertEqual(mine["failures"][0]["people"], ["Ada O."])
        self.assertEqual(mine["me"]["display"], "Ada O.")

    def test_no_view_ever_carries_a_hash(self):
        world = World()
        world.add("ada")
        world.do(person("ada"), "start_shift")
        world.red("SITE-A", 1)
        for view in (staff.staff_view(world.state, "ada", at(2)), staff.panel_view(world.state, at(2)), staff.public_roster(world.state)):
            self.assertNotIn("scrypt", json.dumps(view))
            self.assertNotIn("tag", json.dumps(view))

    def test_an_open_failure_older_than_a_week_is_hidden_and_newest_comes_first(self):
        world = World()
        world.on_shift("ada")
        world.red("SITE-A", 0)                       # never finished
        world.red("SITE-B", 60 * 24 * 6)
        mid = staff.staff_view(world.state, "ada", at(60 * 24 * 6 + 1))
        self.assertEqual([item["studio"] for item in mid["failures"]], ["SITE-B", "SITE-A"])
        later = staff.staff_view(world.state, "ada", at(60 * 24 * 8))
        self.assertEqual([item["studio"] for item in later["failures"]], ["SITE-B"])
        self.assertEqual(staff.OPEN_SHOWN_SECONDS, 7 * 24 * 3600)

    def test_a_done_failure_is_listed_for_a_day_only(self):
        world = World()
        world.on_shift("ada")
        world.red("SITE-A", 0)
        fid = next(iter(world.state["failures"]))
        world.do(SUPER, "done", 5, fid=fid, note="restarted")
        view = staff.staff_view(world.state, "ada", at(60 * 23))
        self.assertEqual([(item["studio"], item["done_by"], item["note"]) for item in view["done"]], [("SITE-A", "super", "restarted")])
        self.assertEqual(view["failures"], [])
        self.assertEqual(staff.staff_view(world.state, "ada", at(60 * 25))["done"], [])
        self.assertEqual(staff.DONE_SHOWN_SECONDS, 24 * 3600)

    def test_the_panel_lists_people_slots_and_who_can_be_assigned(self):
        world = World()
        world.add("ada")
        world.add("kemi")
        world.do(person("ada"), "start_shift", 1)
        world.do(SUPER, "disable_person", 2, username="kemi")
        panel = staff.panel_view(world.state, at(3))
        self.assertEqual(panel["slots"], {"active": 1, "max": 10})
        self.assertEqual({item["username"]: item["active"] for item in panel["people"]}, {"ada": True, "kemi": False})
        self.assertEqual([item["username"] for item in panel["roster"]], ["ada"])

    def seen_world(self):
        world = World()
        world.add("ada.o", "Ada O.")
        world.add("kemi", "Kemi")
        world.do(person("ada.o"), "start_shift")
        world.do(person("kemi"), "start_shift")
        world.red("SITE-A", 1)                                   # opens at minute 1
        fid = next(iter(world.state["failures"]))
        world.do(person("ada.o"), "acknowledge", 5, fid=fid)      # four minutes after the red
        return world, fid

    def test_the_staff_view_says_who_is_on_it_by_display_name_how_long_after_the_red_and_whether_it_is_me(self):
        world, _ = self.seen_world()
        mine = staff.staff_view(world.state, "ada.o", at(6))["failures"][0]
        theirs = staff.staff_view(world.state, "kemi", at(6))["failures"][0]
        self.assertEqual(mine["seen"], [{"display": "Ada O.", "at": at(5).isoformat(), "after_s": 240.0}])
        self.assertTrue(mine["seen_by_me"])
        self.assertFalse(theirs["seen_by_me"])
        self.assertNotIn("ada.o", json.dumps(mine["seen"]))

    def test_the_panel_view_lists_who_is_on_it_with_the_username_the_owner_already_sees(self):
        world, _ = self.seen_world()
        item = staff.panel_view(world.state, at(6))["failures"][0]
        self.assertEqual(item["seen"], [{"username": "ada.o", "display": "Ada O.", "at": at(5).isoformat(), "after_s": 240.0}])

    def test_a_failure_nobody_has_pressed_for_has_an_empty_list_not_a_missing_one(self):
        world = World()
        world.on_shift("ada")
        world.red("SITE-A", 1)
        item = staff.staff_view(world.state, "ada", at(2))["failures"][0]
        self.assertEqual((item["seen"], item["seen_by_me"]), ([], False))
        self.assertEqual(staff.panel_view(world.state, at(2))["failures"][0]["seen"], [])

    def test_a_press_dated_before_the_red_is_not_measured_rather_than_shown_as_zero_or_negative(self):
        world = World()
        world.on_shift("ada")
        world.red("SITE-A", 10)
        fid = next(iter(world.state["failures"]))
        world.do(person("ada"), "acknowledge", 9, fid=fid)         # the other server's clock is a minute behind
        item = staff.staff_view(world.state, "ada", at(11))["failures"][0]
        self.assertIsNone(item["seen"][0]["after_s"])

    def test_the_time_is_counted_from_the_first_red_of_the_failure_not_from_a_later_alarm(self):
        world = World()
        world.on_shift("ada")
        world.red("SITE-A", 1)
        world.red("SITE-A", 6)                                    # the same failure, still red
        fid = next(iter(world.state["failures"]))
        world.do(person("ada"), "acknowledge", 9, fid=fid)
        self.assertEqual(staff.staff_view(world.state, "ada", at(10))["failures"][0]["seen"][0]["after_s"], 480.0)

    def test_the_finished_list_keeps_its_old_shape(self):
        world, fid = self.seen_world()
        world.do(SUPER, "done", 7, fid=fid, note="")
        done = staff.staff_view(world.state, "kemi", at(8))["done"][0]
        self.assertEqual(sorted(done), ["display", "done_at", "done_by", "fid", "note", "studio"])

    def test_lists_are_capped(self):
        world = World()
        for index in range(130):
            world.red("SITE-A", index * 20)
        self.assertEqual(len(staff.panel_view(world.state, at(130 * 20))["failures"]), 100)


class SessionTests(unittest.TestCase):
    SECRET = "control-key-for-tests-0123456789abcdef"

    def setUp(self):
        self.world = World()
        self.world.add("ada")
        self.tag = self.world.state["people"]["ada"]["tag"]

    def token(self, now=0.0, **kwargs):
        return staff.issue_session(self.SECRET, "ada", self.tag, at(now), **kwargs)

    def test_a_good_token_names_the_person(self):
        self.assertEqual(staff.verify_session(self.SECRET, self.token(), at(60), self.world.state), "ada")

    def test_it_lasts_48_hours_and_not_longer(self):
        token = self.token()
        self.assertEqual(staff.verify_session(self.SECRET, token, at(60 * 47), self.world.state), "ada")
        self.assertIsNone(staff.verify_session(self.SECRET, token, at(60 * 49), self.world.state))
        self.assertEqual(staff.SESSION_SECONDS, 48 * 3600)

    def test_a_token_signed_with_another_key_or_changed_is_refused(self):
        token = self.token()
        self.assertIsNone(staff.verify_session("other-key-for-tests-0123456789abcdef", token, at(1), self.world.state))
        parts = token.split(".")
        for position in range(len(parts)):
            changed = parts[:]
            changed[position] = changed[position][:-1] + ("0" if changed[position][-1] != "0" else "1")
            self.assertIsNone(staff.verify_session(self.SECRET, ".".join(changed), at(1), self.world.state), position)

    def test_swapping_the_name_in_a_token_does_not_make_it_someone_elses(self):
        self.world.add("kemi")
        token = self.token().split(".")
        token[2] = "kemi".encode().hex()
        self.assertIsNone(staff.verify_session(self.SECRET, ".".join(token), at(1), self.world.state))

    def test_a_password_reset_ends_the_session_at_once(self):
        token = self.token()
        reset = World()
        reset.rows = self.world.rows[:]
        self.assertEqual(staff.verify_session(self.SECRET, token, at(4), reset.state), "ada")
        reset.do(SUPER, "reset_password", 5, username="ada", password_hash=HASH2)
        self.assertIsNone(staff.verify_session(self.SECRET, token, at(6), reset.state))

    def test_disabling_ends_the_session_at_once_even_with_the_password_unchanged(self):
        token = self.token()
        gone = World()
        gone.rows = self.world.rows[:]
        self.assertEqual(staff.verify_session(self.SECRET, token, at(4), gone.state), "ada")
        gone.do(SUPER, "disable_person", 5, username="ada")
        self.assertEqual(gone.state["people"]["ada"]["tag"], self.tag)
        self.assertIsNone(staff.verify_session(self.SECRET, token, at(6), gone.state))

    def test_the_staff_signing_key_is_not_the_super_users(self):
        self.assertNotEqual(staff._signing_key(self.SECRET), control_access._signing_key(self.SECRET))

    def test_garbage_never_raises_and_never_verifies(self):
        for junk in (None, 5, "", "x", "s1", "s1.1.2.3", "s1.1.2.3.4.5", "v1.9999999999.abc", b"bytes", ["s1"], "s1." + "9" * 40 + ".61.00.00", "é" * 10):
            self.assertIsNone(staff.verify_session(self.SECRET, junk, at(1), self.world.state), repr(junk))
        self.assertIsNone(staff.verify_session("", self.token(), at(1), self.world.state))
        self.assertIsNone(staff.verify_session(self.SECRET, self.token(), T0.replace(tzinfo=None), self.world.state))

    def test_the_token_is_safe_in_a_cookie(self):
        token = self.token()
        self.assertRegex(token, r"^[0-9a-z.]+$")
        self.assertLess(len(token), 200)

    def test_issue_refuses_bad_input(self):
        for kwargs in ({"ttl": 0}, {"ttl": -5}, {"ttl": 10 ** 9}):
            with self.assertRaises(ValueError):
                self.token(**kwargs)
        with self.assertRaises(ValueError):
            staff.issue_session("", "ada", self.tag, at(0))
        with self.assertRaises(ValueError):
            staff.issue_session(self.SECRET, "Ada!", self.tag, at(0))
        with self.assertRaises(ValueError):
            staff.issue_session(self.SECRET, "ada", self.tag, T0.replace(tzinfo=None))

    def test_a_staff_cookie_is_not_a_super_cookie_and_his_is_not_theirs(self):
        staff_token = self.token()
        self.assertFalse(control_access.verify_session(self.SECRET, staff_token, at(1)))
        for generation in ("", "ab" * 16):
            self.assertFalse(control_access.verify_session(self.SECRET, staff_token, at(1), generation))
        super_token = control_access.issue_session(self.SECRET, at(0), 3600)
        self.assertIsNone(staff.verify_session(self.SECRET, super_token, at(1), self.world.state))

    def test_a_staff_signature_cannot_be_replayed_as_a_super_signature(self):
        expiry = int(at(0).timestamp()) + 3600
        staff_mac = self.token().split(".")[-1]
        forged = f"v1.{expiry}.{staff_mac}"
        self.assertFalse(control_access.verify_session(self.SECRET, forged, at(1)))


class RowValidationTests(unittest.TestCase):
    def good_rows(self):
        world = World()
        world.add("ada")
        world.do(person("ada"), "start_shift", 1)
        world.red("SITE-A", 2)
        world.red("SITE-A", 3)
        fid = next(iter(world.state["failures"]))
        world.do(SUPER, "assign", 4, fid=fid, people=["ada"])
        world.do(SUPER, "unassign", 5, fid=fid, username="ada")
        world.do(SUPER, "assign", 5.5, fid=fid, people=["ada"])
        world.do(person("ada"), "acknowledge", 5.7, fid=fid)
        world.do(person("ada"), "done", 6, fid=fid, note="ok")
        world.do(person("ada"), "finish_shift", 7)
        world.do(SUPER, "reset_password", 8, username="ada", password_hash=HASH2)
        world.do(SUPER, "disable_person", 9, username="ada")
        world.do(SUPER, "delete_person", 9.5, username="ada")
        return world.rows

    def test_every_row_the_code_itself_makes_passes(self):
        rows = self.good_rows()
        self.assertEqual({row["kind"] for row in rows}, set(staff.KINDS))
        for row in rows:
            self.assertIsNone(staff.row_problem(row, at(10)), row)

    def test_a_row_must_be_exactly_the_known_shape(self):
        row = self.good_rows()[0]
        bad = [
            {**row, "id": row["id"].replace("20261101", "20261102")}, {**row, "kind": "person_removed"}, {**row, "extra": 1}, {k: v for k, v in row.items() if k != "data"}, {**row, "id": "nope"},
            {**row, "id": row["id"] + "Z"}, {**row, "at": "yesterday"}, {**row, "at": "2026-11-01T12:00:00"}, {**row, "node": "core"}, {**row, "node": "X" * 9},
            {**row, "person": "Ada!"}, {**row, "data": "text"}, {**row, "data": {**row["data"], "pad": "x" * 3000}}, "text", None, 5, [],
        ]
        for item in bad:
            self.assertIsNotNone(staff.row_problem(item, at(10)), repr(item)[:80])

    def test_a_row_dated_far_in_the_future_is_refused(self):
        row = staff.make_row("shift_started", "ada", {}, at(60 * 24 * 3), "CORE", "00000a")
        self.assertIsNotNone(staff.row_problem(row, at(0)))
        self.assertIsNone(staff.row_problem(row, at(60 * 24 * 3)))

    def test_kind_specific_data_is_checked(self):
        cases = [
            ("person_added", "ada", {"display": "<b>", "hash": HASH}), ("person_added", "ada", {"display": "Ada", "hash": "plain"}), ("person_added", "", {"display": "Ada", "hash": HASH}),
            ("person_password", "ada", {"hash": ""}), ("shift_finished", "ada", {"by": 5}),
            ("failure_opened", "", {"fid": "", "studio": "X", "cause": "y", "display": "z"}), ("failure_opened", "", {"fid": "f", "studio": "X" * 41, "cause": "y", "display": "z"}),
            ("failure_assigned", "", {"fid": "f", "people": [], "by": "super"}), ("failure_assigned", "", {"fid": "f", "people": ["Ada!"], "by": "super"}),
            ("failure_assigned", "", {"fid": "f", "people": ["ada"] * 11, "by": "super"}), ("failure_done", "", {"fid": "f", "note": "x" * 201, "by": "super"}),
            ("failure_unassigned", "", {"fid": "f", "username": 5}),
            ("failure_acknowledged", "", {"fid": "f"}), ("failure_acknowledged", "", {"by": "ada"}), ("failure_acknowledged", "", {"fid": "", "by": "ada"}),
            ("failure_acknowledged", "", {"fid": "f", "by": "super"}), ("failure_acknowledged", "", {"fid": "f", "by": 5}), ("failure_acknowledged", "", {"fid": "f", "by": "Ada!"}),
            ("failure_acknowledged", "ada", {"fid": "f", "by": "ada"}), ("failure_acknowledged", "", {"fid": "f", "by": "ada", "note": "x"}),
        ]
        for kind, person_name, data in cases:
            row = staff.make_row(kind, person_name, data, at(0), "CORE", "00000a")
            self.assertIsNotNone(staff.row_problem(row, at(1)), (kind, data))


class BrokenInputTests(unittest.TestCase):
    def test_plan_never_raises_anything_but_staff_error(self):
        world = World()
        world.on_shift("ada")
        junk = [None, 5, "", [], {}, "x" * 500]
        for action in ("add_person", "reset_password", "change_password", "disable_person", "delete_person", "start_shift", "finish_shift", "assign", "unassign", "done", "acknowledge", "nonsense", None, 7):
            for actor in (SUPER, person("ada"), staff.Actor("visitor", "")):
                for value in junk:
                    args = {"username": value, "display": value, "password_hash": value, "fid": value, "people": value, "note": value}
                    try:
                        staff.plan(world.state, actor, action, args, at(1), "CORE", "00000a")
                    except staff.StaffError:
                        pass


if __name__ == "__main__":
    unittest.main(verbosity=1)
