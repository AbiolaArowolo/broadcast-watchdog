# File:         test_staff_critical.py
# Purpose:      Tests of "a critical alert goes to the people on shift by itself, and no signed-in staff member can miss one" (asked 2026-11-08): the automatic assignment rules,
#               the notice when nobody is on shift, the public alert strip on the dashboard, the alarm strip + siren + 120-second return on the Staff page, and the dashboard address.
# Inputs:       None (made-up rows, a throwaway database, fake mail and push, and the real page scripts run in Node). Nothing leaves this machine.
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app (staff.py, staff_pages.py, dashboard.py, main.py), the helpers of test_staff_logic.py / test_staff_wiring.py, and Node (to run the page rules for real).
"""Run: python ops/test_staff_critical.py

What is proven here, in plain words: when a critical alert opens it is given, by the system and with the same row the super user's Assign makes, to everyone who is on shift; when nobody
is on shift the super user's recipients get a second, clear "NOBODY ON SHIFT" mail and push, and the failure goes to whoever starts a shift next while it is still open and recent;
nothing in that path can stop or delay the red e-mail; the dashboard shows the open failures with the display names that hold them (names already public) and never a username;
the Staff page shows a red alarm strip with the person's own name, plays a siren until they press "I am on it", says so loudly when its live updates stop, and after 120 seconds with no
activity returns to the dashboard address the server gave it, keeping any half-written note.

The page rules are plain functions inside the page scripts. They are cut out of the real page text and run in Node, so what is tested is the code the browser runs, not a copy.
"""
import ast
import email
import importlib
import inspect
import json
import re
import shutil
import subprocess
import sys
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import test_staff_logic as tsl  # noqa: E402 -- the World of made-up rows
import test_staff_pages as tsp  # noqa: E402 -- function_body() and the page texts
import test_staff_wiring as tsw  # noqa: E402 -- the listener harness

staff, listener, run = tsl.staff, tsw.listener, tsw.run
staff_pages = importlib.import_module("app.staff_pages")
dashboard = importlib.import_module("app.dashboard")

HOUR = 60.0                                  # the World's clock counts minutes
DAY = 24 * HOUR


def failure_id(world):
    return next(iter(world.state["failures"]))


# ---- 1. The pure rules ---------------------------------------------------------------------------------------------------------------------------
class AutoAssignRuleTests(unittest.TestCase):
    def setUp(self):
        self.w = tsl.World()

    def auto(self, fid, minutes):
        return staff.auto_assign_row(self.w.state, fid, tsl.at(minutes), "CORE", "feedbeef")

    def test_a_new_failure_goes_to_everyone_on_shift_in_the_order_they_started(self):
        self.w.on_shift("bob", minutes=0)
        self.w.on_shift("ada", minutes=1)
        self.w.red("MAIN-USA", 5)
        row = self.auto(failure_id(self.w), 5)
        self.assertEqual((row["kind"], row["data"]["people"], row["data"]["by"]), ("failure_assigned", ["bob", "ada"], "system"))
        self.w.rows.append(row)
        self.assertEqual(self.w.state["failures"][failure_id(self.w)]["people"], ["bob", "ada"])

    def test_the_marker_is_a_name_nobody_can_have_so_history_stays_honest(self):
        self.assertEqual(staff.AUTO, "system")
        self.assertIsNotNone(staff.username_problem(staff.AUTO))

    def test_the_row_has_the_shape_the_nodes_already_accept_so_no_deploy_order_is_forced(self):
        self.w.on_shift("ada")
        self.w.red("MAIN-USA", 1)
        row = self.auto(failure_id(self.w), 1)
        self.assertIsNone(staff.row_problem(row))
        self.assertEqual(set(row["data"]), {"fid", "people", "by"})
        self.assertIn(row["kind"], staff.KINDS)

    def test_nobody_on_shift_gives_no_row_and_says_so(self):
        self.w.add("ada")
        self.w.red("MAIN-USA", 1)
        self.assertIsNone(self.auto(failure_id(self.w), 1))
        self.assertTrue(staff.nobody_on_shift(self.w.state, tsl.at(1)))

    def test_a_shift_nobody_ended_for_over_48_hours_does_not_count_as_being_on_shift(self):
        self.w.on_shift("ada", minutes=0)
        self.w.red("MAIN-USA", 49 * HOUR)
        self.assertEqual(staff.on_shift_now(self.w.state, tsl.at(49 * HOUR)), [])
        self.assertIsNone(self.auto(failure_id(self.w), 49 * HOUR))
        self.assertTrue(staff.nobody_on_shift(self.w.state, tsl.at(49 * HOUR)))
        self.assertEqual(staff.on_shift_now(self.w.state, tsl.at(47 * HOUR)), ["ada"])        # a shift of 47 hours is still a shift

    def test_a_failure_somebody_already_holds_is_left_alone(self):
        self.w.on_shift("ada", "bob")
        self.w.red("MAIN-USA", 1)
        fid = failure_id(self.w)
        self.w.do(tsl.SUPER, "assign", 2, fid=fid, people=["ada"])
        self.assertIsNone(self.auto(fid, 3))                                                  # Bob is on shift too, but the super user chose Ada

    def test_a_finished_or_unknown_failure_is_never_assigned(self):
        self.w.on_shift("ada")
        self.w.red("MAIN-USA", 1)
        fid = failure_id(self.w)
        self.w.do(tsl.SUPER, "done", 2, fid=fid, note="")
        self.assertIsNone(self.auto(fid, 3))
        self.assertIsNone(self.auto("20990101T000000-NOPE", 3))
        self.assertIsNone(self.auto(None, 3))

    def test_a_failure_older_than_a_day_is_not_handed_to_a_person_who_starts_a_shift_now(self):
        self.w.add("ada")
        self.w.red("MAIN-USA", 0)
        self.w.do(tsl.person("ada"), "start_shift", 25 * HOUR)
        fid = failure_id(self.w)
        self.assertIsNone(self.auto(fid, 25 * HOUR))
        self.w.rows = [r for r in self.w.rows if r["kind"] != "shift_started"]
        self.w.do(tsl.person("ada"), "start_shift", 23 * HOUR)
        self.assertEqual(self.auto(fid, 23 * HOUR)["data"]["people"], ["ada"])

    def test_the_waiting_list_is_open_recent_unheld_failures_oldest_first_and_capped(self):
        self.w.on_shift("ada")
        for index, studio in enumerate(("SITE-A", "SITE-B", "MAIN-USA", "MAIN-Caribbean")):
            self.w.red(studio, 10 + index * 30)
        by_studio = {f["studio"]: f["fid"] for f in self.w.state["failures"].values()}
        self.w.do(tsl.SUPER, "assign", 200, fid=by_studio["SITE-B"], people=["ada"])      # held by somebody
        self.w.do(tsl.SUPER, "done", 201, fid=by_studio["MAIN-USA"], note="")              # finished
        self.assertEqual(staff.waiting_failures(self.w.state, tsl.at(202)), [by_studio["SITE-A"], by_studio["MAIN-Caribbean"]])
        self.assertEqual(staff.waiting_failures(self.w.state, tsl.at(30 * HOUR)), [])      # all older than a day by then

    def test_the_waiting_list_has_a_cap_so_one_shift_start_writes_a_few_rows_not_hundreds(self):
        self.w.add("ada")
        for index in range(staff.MAX_WAITING + 7):
            self.w.red(f"STUDIO{index}", index)
        self.assertEqual(len(staff.waiting_failures(self.w.state, tsl.at(60))), staff.MAX_WAITING)

    def test_the_manual_assign_is_still_the_super_users_alone(self):
        self.w.on_shift("ada", "bob")
        self.w.red("MAIN-USA", 1)
        with self.assertRaises(staff.StaffError):
            self.w.do(tsl.person("ada"), "assign", 2, fid=failure_id(self.w), people=["bob"])


class PublicAlertTests(unittest.TestCase):
    def setUp(self):
        self.w = tsl.World()
        self.w.add("ada.o", "Ada O.")
        self.w.add("bob", "Bob")
        self.w.do(tsl.person("ada.o"), "start_shift", 0)
        self.w.do(tsl.person("bob"), "start_shift", 1)

    def alarm(self, studio, minutes, display):
        self.w.red(studio, minutes, display=display)
        fid = [f for f in self.w.state["failures"].values() if f["studio"] == studio][0]["fid"]
        row = staff.auto_assign_row(self.w.state, fid, tsl.at(minutes), "CORE", "feedbeef")
        if row:
            self.w.rows.append(row)
        return fid

    def test_an_open_recent_failure_is_listed_with_display_names_only(self):
        fid = self.alarm("MAIN-USA", 5, "MAIN - USA")
        self.w.do(tsl.person("ada.o"), "acknowledge", 6, fid=fid)
        shown = staff.public_alerts(self.w.state, tsl.at(15))
        self.assertEqual(shown["more"], 0)
        self.assertEqual(len(shown["items"]), 1)
        item = shown["items"][0]
        self.assertEqual((item["playout"], item["assigned"], item["on_it"], item["age_s"]), ("MAIN - USA", ["Ada O.", "Bob"], ["Ada O."], 600))
        self.assertEqual(set(item), {"playout", "since", "age_s", "assigned", "on_it"})
        text = json.dumps(shown)
        for secret in ("ada.o", "scrypt", "feedbeef", fid):
            self.assertNotIn(secret, text)

    def test_a_failure_nobody_holds_shows_an_empty_assigned_list(self):
        self.w.do(tsl.person("ada.o"), "finish_shift", 2)
        self.w.do(tsl.person("bob"), "finish_shift", 2)
        self.alarm("SITE-A", 5, "SITE-A")
        self.assertEqual(staff.public_alerts(self.w.state, tsl.at(6))["items"][0]["assigned"], [])

    def test_finished_and_stale_failures_are_not_alerts(self):
        done = self.alarm("MAIN-USA", 5, "MAIN - USA")
        self.w.do(tsl.SUPER, "done", 6, fid=done, note="restarted")
        self.alarm("SITE-A", 7, "SITE-A")
        self.assertEqual([i["playout"] for i in staff.public_alerts(self.w.state, tsl.at(8))["items"]], ["SITE-A"])
        self.assertEqual(staff.public_alerts(self.w.state, tsl.at(7 + 25 * HOUR))["items"], [])        # a forgotten failure from yesterday must not paint the dashboard red for ever

    def test_at_most_six_are_listed_newest_first_and_the_rest_are_counted(self):
        for index in range(8):
            self.alarm(f"STUDIO{index}", 10 + index, f"Studio {index}")
        shown = staff.public_alerts(self.w.state, tsl.at(60))
        self.assertEqual(len(shown["items"]), staff.ALERTS_SHOWN)
        self.assertEqual(shown["more"], 2)
        self.assertEqual(shown["items"][0]["playout"], "Studio 7")

    def test_the_staff_view_marks_which_open_failures_are_recent_enough_to_be_alarms(self):
        self.alarm("MAIN-USA", 0, "MAIN - USA")
        recent = staff.staff_view(self.w.state, "ada.o", tsl.at(23 * HOUR))["failures"][0]
        stale = staff.staff_view(self.w.state, "ada.o", tsl.at(25 * HOUR))["failures"][0]
        self.assertIs(recent["active"], True)
        self.assertIs(stale["active"], False)


# ---- 2. The wiring in main.py --------------------------------------------------------------------------------------------------------------------
def readable(raw):
    """Subject and text of a mail the fake server was handed (the real message is MIME, possibly encoded)."""
    message = email.message_from_string(raw)
    parts = [message["Subject"] or ""]
    for part in message.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            parts.append(part.get_payload(decode=True).decode("utf-8", "replace"))
    return "\n".join(parts)


class CriticalWiring(tsw.StaffWiring):
    def setUp(self):
        super().setUp()
        self.push_requests = []
        listener.urllib.request.urlopen = lambda request, timeout=None: (self.push_requests.append(request), tcw_ctx())[1]
        self.add("ada", "Ada")
        self.add("bob", "Bob")

    def alarm(self, endpoint_id="MAIN-USA", cause="Process Missing"):
        run(listener._send_red_alarm(self.real_playout(endpoint_id, cause)))

    def the_failure(self):
        found = list(self.failures().values())
        self.assertEqual(len(found), 1, found)
        return found[0]

    def start_via_door(self, username):
        reply = self.act(self.staff_cookie(username), {"action": "start_shift"})
        self.assertEqual(reply.status_code, 200, reply.content)
        return reply

    def start_quietly(self, username):
        """A shift start that bypasses the staff door's handler, so only the red-alarm hook can be the one that assigns."""
        run(listener._staff_do(staff.Actor("staff", username), "start_shift", {}))


def tcw_ctx():
    return tsw.tcw._Ctx()


class AutoAssignWiringTests(CriticalWiring):
    def test_a_critical_alert_is_assigned_to_everyone_on_shift_and_the_red_mail_still_goes_out_alone(self):
        self.start_quietly("ada")
        self.start_quietly("bob")
        self.alarm()
        self.assertEqual(self.the_failure()["people"], ["ada", "bob"])
        self.assertEqual(self.red_mails, ["MAIN - USA"])
        self.assertEqual(tsw.tcw.FakeSmtp.sent, [])                          # nobody was missing, so there is no second mail
        self.assertEqual(self.push_requests, [])

    def test_the_system_row_is_copied_to_the_other_node_like_any_other_row(self):
        copied = []
        self.fake_peer(copied)
        self.start_quietly("ada")
        copied.clear()
        self.alarm()
        kinds = [(row["kind"], row["data"].get("by")) for row in copied]
        self.assertEqual(kinds, [("failure_opened", None), ("failure_assigned", "system")])

    def test_a_person_on_shift_can_fill_the_resolve_form_for_what_the_system_gave_them_and_the_super_user_can_still_take_it_back(self):
        self.start_quietly("ada")
        self.alarm()
        fid = self.the_failure()["fid"]
        cookie = self.staff_cookie("ada")
        done = self.act(cookie, {"action": "done", "fid": fid, "note": "Restarted the player; it was hung"})
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual(self.failures()[fid]["done"]["note"], "Restarted the player; it was hung")
        self.alarm("SITE-A", "Off Air")
        other = [f for f in self.failures().values() if f["studio"] == "SITE-A"][0]["fid"]
        self.assertEqual(self.panel({"action": "unassign", "fid": other, "username": "ada"}).status_code, 200)
        self.assertEqual(self.failures()[other]["people"], [])

    def test_a_staff_person_still_cannot_assign_anyone_and_cannot_forge_the_system_marker(self):
        self.start_quietly("ada")
        self.alarm()
        fid = self.the_failure()["fid"]
        reply = self.act(self.staff_cookie("ada"), {"action": "assign", "fid": fid, "people": ["bob"], "by": "system"})
        self.assertEqual(reply.status_code, 400)

    def test_a_second_alarm_for_a_failure_nobody_holds_gives_it_to_whoever_is_on_shift_by_then(self):
        self.alarm()
        self.assertEqual(self.the_failure()["people"], [])
        self.start_quietly("ada")
        self.alarm("MAIN-USA", "Off Air")
        self.assertEqual(self.the_failure()["people"], ["ada"])

    def test_a_second_alarm_never_adds_people_to_a_failure_somebody_holds(self):
        self.start_quietly("ada")
        self.alarm()
        self.start_quietly("bob")
        self.alarm("MAIN-USA", "Off Air")
        self.assertEqual(self.the_failure()["people"], ["ada"])

    def test_a_broken_assignment_never_stops_the_red_mail_or_the_failure(self):
        self.start_quietly("ada")
        with mock.patch.object(staff, "auto_assign_row", side_effect=RuntimeError("boom")):
            self.alarm()
        self.assertEqual(self.red_mails, ["MAIN - USA"])
        self.assertEqual(self.the_failure()["people"], [])

    def test_nothing_is_recorded_assigned_or_mailed_while_access_is_off(self):
        self.start_quietly("ada")
        self.switch_off(notify=False)
        tsw.tcw.FakeSmtp.sent.clear()
        self.alarm()
        self.assertEqual(self.failures(), {})
        self.assertEqual(tsw.tcw.FakeSmtp.sent, [])

    def test_a_node_that_may_not_alert_assigns_nothing_and_sends_no_notice(self):
        with mock.patch.object(listener, "_delivery_allowed", lambda: False):
            self.alarm()
        self.assertEqual(self.failures(), {})
        self.assertEqual(tsw.tcw.FakeSmtp.sent, [])

    def order_of_steps(self):
        order = []
        real_write = listener._staff_write

        async def watch_write(make):
            row = await real_write(make)
            order.append(row["kind"])
            return row

        listener._send_endpoint_critical_email = lambda endpoint: order.append("mail")
        with mock.patch.object(listener, "_staff_write", watch_write), mock.patch.object(listener, "send_alert_email", lambda *a, **k: order.append("notice")):
            self.alarm()
        return order

    def test_with_nobody_on_shift_the_order_is_mail_then_failure_then_notice(self):
        self.assertEqual(self.order_of_steps(), ["mail", "failure_opened", "notice"])

    def test_with_someone_on_shift_the_order_is_mail_then_failure_then_assignment_and_there_is_no_notice(self):
        self.start_quietly("ada")
        self.assertEqual(self.order_of_steps(), ["mail", "failure_opened", "failure_assigned"])


class NobodyOnShiftTests(CriticalWiring):
    def test_a_new_failure_with_nobody_on_shift_sends_one_clear_mail_and_push_to_the_normal_recipients(self):
        self.alarm()
        self.assertEqual(len(tsw.tcw.FakeSmtp.sent), 1)
        recipients, raw = tsw.tcw.FakeSmtp.sent[0]
        self.assertEqual(recipients, ["users@x.test"])
        text = readable(raw)
        self.assertIn("NOBODY ON SHIFT", text.split("\n")[0])
        for needle in ("MAIN - USA", "Process Missing", "Nobody is on shift"):
            self.assertIn(needle, text)
        self.assertEqual([r.full_url for r in self.push_requests], ["http://127.0.0.1:1/shared"])
        self.assertIn("NOBODY ON SHIFT", self.push_requests[0].get_header("Title"))

    def test_the_notice_is_sent_once_for_the_failure_not_once_for_every_alarm(self):
        self.alarm()
        self.alarm("MAIN-USA", "Off Air")
        self.alarm("MAIN-USA", "Paused/Frozen > 60s")
        self.assertEqual(len(tsw.tcw.FakeSmtp.sent), 1)

    def test_a_different_failure_gets_its_own_notice(self):
        self.alarm()
        self.alarm("SITE-A", "Off Air")
        self.assertEqual(len(tsw.tcw.FakeSmtp.sent), 2)

    def test_the_notice_is_not_forced_so_it_follows_the_same_delivery_rules_as_the_red_mail(self):
        self.assertNotIn("force=True", inspect.getsource(listener._staff_tell_nobody))
        with mock.patch.object(listener, "_delivery_allowed", lambda: True):
            self.switch_off(notify=False)                                       # access OFF: the red mail goes to the super user only, and so must this
            tsw.tcw.FakeSmtp.sent.clear()
            listener.send_alert_email("MAIN - USA", "NOBODY ON SHIFT", "x", [])
        self.assertEqual([recipients for recipients, _ in tsw.tcw.FakeSmtp.sent], [["super@x.test"]])

    def test_a_failing_notice_never_stops_the_failure_or_the_red_mail(self):
        with mock.patch.object(listener, "send_alert_email", side_effect=OSError("mail server down")):
            self.alarm()
        self.assertEqual(self.red_mails, ["MAIN - USA"])
        self.assertEqual(len(self.failures()), 1)

    def test_a_shift_over_48_hours_old_counts_as_nobody_and_the_notice_goes_out(self):
        forgotten = staff.make_row("shift_started", "ada", {}, datetime.now(timezone.utc) - timedelta(hours=49), listener.NODE, "abcd1234")
        self.assertEqual(run(listener._staff_append_rows([forgotten])), (1, 0))      # a real row in the log, 49 hours old
        self.assertIn("ada", self.state()["shifts"])
        self.alarm()
        self.assertEqual(len(tsw.tcw.FakeSmtp.sent), 1)
        self.assertEqual(self.the_failure()["people"], [])


class LateStartTests(CriticalWiring):
    def test_a_person_who_starts_a_shift_through_the_staff_door_gets_the_failures_nobody_holds(self):
        self.alarm()
        self.alarm("SITE-A", "Off Air")
        reply = self.start_via_door("ada")
        self.assertEqual(sorted(f["studio"] for f in self.failures().values() if f["people"] == ["ada"]), ["MAIN-USA", "SITE-A"])
        self.assertEqual([f["mine"] for f in reply.content["failures"]], [True, True])           # the answer to the Start press already shows them

    def test_the_super_user_starting_a_shift_for_someone_does_the_same(self):
        self.alarm()
        self.assertEqual(self.panel({"action": "start_shift", "username": "bob"}).status_code, 200)
        self.assertEqual(self.the_failure()["people"], ["bob"])

    def test_a_failure_somebody_holds_is_not_given_to_a_late_starter(self):
        self.start_quietly("ada")
        self.alarm()
        self.start_via_door("bob")
        self.assertEqual(self.the_failure()["people"], ["ada"])

    def test_an_old_open_failure_is_not_given_to_the_first_person_in_the_morning(self):
        run(listener._staff_write(lambda state, now: staff.failure_row(state, "MAIN-USA", "Off Air", "MAIN - USA", now - timedelta(days=2), listener.NODE, "abcd1234")))
        self.start_via_door("ada")
        self.assertEqual(self.the_failure()["people"], [])

    def test_the_node_that_took_the_start_does_the_assignment_even_if_it_is_not_the_one_that_alerts(self):
        # The staff door lives on the standby VPS, which usually may not alert: it must still hand waiting failures to the person who just started.
        self.alarm()
        with mock.patch.object(listener, "_delivery_allowed", lambda: False):
            self.start_via_door("ada")
        self.assertEqual(self.the_failure()["people"], ["ada"])

    def test_a_broken_assignment_can_never_fail_the_shift_start(self):
        self.alarm()
        with mock.patch.object(staff, "waiting_failures", side_effect=RuntimeError("boom")):
            reply = self.start_via_door("ada")
        self.assertIn("ada", self.state()["shifts"])
        self.assertTrue(reply.content["me"]["on_shift"])

    def test_a_shift_that_could_not_start_assigns_nothing(self):
        self.alarm()
        self.start_via_door("ada")
        again = self.act(self.staff_cookie("ada"), {"action": "start_shift"})            # already on shift: refused
        self.assertEqual(again.status_code, 400)
        self.assertEqual(self.the_failure()["people"], ["ada"])


class PublicFrameTests(CriticalWiring):
    def test_the_stream_frame_gets_a_staff_alerts_part_and_the_old_staff_part_is_unchanged(self):
        self.start_quietly("ada")
        self.alarm()
        self.assertEqual(listener._staff_public(), {"on_shift": ["Ada"]})
        alerts = listener._staff_alerts_public()
        self.assertEqual([(i["playout"], i["assigned"]) for i in alerts["items"]], [("MAIN - USA", ["Ada"])])
        self.assertEqual((alerts["more"], alerts["form_url"]), (0, listener.STAFF_URL))
        self.assertIn("_staff_alerts_public()", inspect.getsource(listener.stream))
        self.assertIn('"staff_alerts"', inspect.getsource(listener.stream))
        text = json.dumps(alerts)
        for secret in ('"ada"', "scrypt", "Process Missing"):
            self.assertNotIn(secret, text)                                         # no username, no hash, and not the cause text

    def test_the_part_is_empty_while_access_is_off(self):
        self.start_quietly("ada")
        self.alarm()
        self.assertEqual(len(listener._staff_alerts_public()["items"]), 1)             # control: with access ON the very same state is listed
        self.switch_off(notify=False)
        self.assertEqual(listener._staff_alerts_public(), {"items": [], "more": 0, "form_url": ""})

    def test_the_part_is_empty_where_the_control_phase_is_off(self):
        self.start_quietly("ada")
        self.alarm()
        self.assertEqual(len(listener._staff_alerts_public()["items"]), 1)
        listener.CONTROL_KEY = ""
        self.assertEqual(listener._staff_alerts_public(), {"items": [], "more": 0, "form_url": ""})


class DashboardAddressTests(CriticalWiring):
    def test_the_default_is_the_public_dashboard_and_only_a_plain_https_address_is_ever_used(self):
        self.assertEqual(listener._dashboard_url_from({}), "https://watchdog.corp.example/")
        self.assertEqual(listener._dashboard_url_from({"WATCHDOG_DASHBOARD_URL": "https://dash.example.org/"}), "https://dash.example.org/")
        for bad in ("http://dash.example.org/", "javascript:alert(1)", "https://x.example/ y", 'https://x.example/"onmouseover=', "//evil.example", "https://" + "a" * 300):
            self.assertEqual(listener._dashboard_url_from({"WATCHDOG_DASHBOARD_URL": bad}), "https://watchdog.corp.example/", bad)

    def test_the_staff_page_is_served_with_that_address_in_its_back_link(self):
        page =run(listener.staff_page(tsw.greq(cookie=self.staff_cookie("ada")))).content
        self.assertRegex(page, r'<a id="to-dashboard" href="https://watchdog\.corp.example/"')
        with mock.patch.object(listener, "DASHBOARD_URL", "https://dash.example.org/"):
            page = run(listener.staff_page(tsw.greq(cookie=self.staff_cookie("ada")))).content
        self.assertIn('href="https://dash.example.org/"', page)


class AnyRedAlarmTests(CriticalWiring):
    """His words via the multicast task (2026-11-08): a lost Site C multicast stream is a critical red alert too and must be assigned like the rest. The trigger must hang on the alarm
    STATE (a critical alarm whose red e-mail is sent), never on a list of today's kinds, so the next kind is covered without a change here."""

    def new_kind(self, stream="MULTICAST-224.1.1.1-5004", display="Site C multicast 224.1.1.1:5004", **kw):
        """An alarm nobody has written yet, shaped like the endpoint dicts the real gate hands out."""
        base = {"endpoint_id": stream, "display_name": display, "source_id": "SITE-C", "status_level": "critical", "status_label": "Critical Alarm",
                "status_detail": "No packets for 10 s", "root_cause": "Multicast Lost", "email_eligible": True, "now_playing": {"title": "-"}, "pid_text": "-", "motion_text": "-"}
        base.update(kw)
        return base

    def through_the_real_gate(self, *endpoints):
        """The red e-mails the real gate (_sync_endpoint_alerts_locked) asks for, each then handled exactly as the three callers handle one."""
        with listener.db() as conn:
            emails = listener._sync_endpoint_alerts_locked(conn, list(endpoints), datetime.now(timezone.utc).isoformat())
        for endpoint in emails:
            run(listener._send_red_alarm(endpoint))
        return emails

    def test_an_alarm_of_a_kind_nobody_has_written_yet_is_recorded_and_given_to_everyone_on_shift(self):
        self.start_quietly("ada")
        self.start_quietly("bob")
        emails = self.through_the_real_gate(self.new_kind())
        self.assertEqual([e["root_cause"] for e in emails], ["Multicast Lost"])
        failure = self.the_failure()
        self.assertEqual((failure["studio"], failure["cause"], failure["display"]), ("MULTICAST-224.1.1.1-5004", "Multicast Lost", "Site C multicast 224.1.1.1:5004"))
        self.assertEqual(failure["people"], ["ada", "bob"])
        self.assertEqual(self.red_mails, ["Site C multicast 224.1.1.1:5004"])

    def test_with_nobody_on_shift_the_new_kind_also_sends_the_nobody_notice(self):
        self.through_the_real_gate(self.new_kind())
        self.assertEqual(len(tsw.tcw.FakeSmtp.sent), 1)
        self.assertIn("NOBODY ON SHIFT", readable(tsw.tcw.FakeSmtp.sent[0][1]).split("\n")[0])

    def test_the_two_streams_are_two_failures_and_a_later_ongoing_alarm_joins_its_own_stream(self):
        self.start_quietly("ada")
        self.through_the_real_gate(self.new_kind(), self.new_kind("MULTICAST-224.1.1.1-5005", "Site C multicast 224.1.1.1:5005"))
        self.assertEqual(sorted(f["studio"] for f in self.failures().values()), ["MULTICAST-224.1.1.1-5004", "MULTICAST-224.1.1.1-5005"])
        self.assertTrue(all(f["people"] == ["ada"] for f in self.failures().values()))
        self.through_the_real_gate(self.new_kind(root_cause="Multicast Lost 60s"))                   # the same stream, another cause text, soon after
        self.assertEqual(sorted((f["studio"], f["alarms"]) for f in self.failures().values()), [("MULTICAST-224.1.1.1-5004", 2), ("MULTICAST-224.1.1.1-5005", 1)])

    def test_only_the_state_decides_a_critical_alarm_whose_red_mail_is_not_sent_is_not_a_failure(self):
        self.start_quietly("ada")
        self.assertEqual(self.through_the_real_gate(self.new_kind(email_eligible=False)), [])           # an alarm already mailed, or held back, sends no red mail
        self.assertEqual(self.through_the_real_gate(self.new_kind(status_level="warning")), [])
        self.assertEqual(self.failures(), {})
        self.assertEqual(self.red_mails, [])

    def test_no_kind_of_alarm_and_no_playout_is_named_anywhere_in_the_staff_code(self):
        # The structural guarantee: nothing below can be a list of today's alarm kinds, because none of today's kinds is written in it. Only the CODE is read (string constants and
        # names); docstrings and comments may use a playout as an example.
        functions = (listener._staff_note_failure, listener._staff_after_failure, listener._staff_auto_assign, listener._auto_assign_plan, listener._staff_tell_nobody,
                     listener._staff_assign_waiting, staff.failure_row, staff.auto_assign_row, staff.waiting_failures, staff.on_shift_now, staff.public_alerts)
        written = []
        for item in functions:
            tree = ast.parse(textwrap.dedent(inspect.getsource(item)))
            docstrings = {id(node.body[0].value) for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body
                          and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant) and isinstance(node.body[0].value.value, str)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    written.append(node.value)
                elif isinstance(node, ast.Name):
                    written.append(node.id)
                elif isinstance(node, ast.Attribute):
                    written.append(node.attr)
        self.assertGreater(len(written), 100)                                                # the scan really read code
        text = chr(10).join(written)
        for word in ("Process Missing", "Player Down", "Player Not Running", "Off Air", "Heartbeat", "freeze", "black", "silence", "Multicast", "MULTICAST", "SITE-A", "SITE-B", "MAIN",
                     "BACKUP", "Site C", "critical", "status_level", "email_eligible"):
            self.assertIsNone(re.search(r"\b" + re.escape(word) + r"\b", text), word)

    def test_every_red_mail_still_goes_through_the_one_wrapper_that_does_the_assigning(self):
        # If a new alarm ever sent its red mail some other way it would silently miss the assignment. Today every one (heartbeat, posted event, staleness loop) goes through it.
        text = Path(listener.__file__).read_text(encoding="utf-8")
        self.assertEqual(text.count("asyncio.to_thread(_send_endpoint_critical_email"), 1)
        self.assertEqual(text.count("_send_endpoint_critical_email(endpoint"), text.count("asyncio.to_thread(_send_endpoint_critical_email, endpoint)"))
        self.assertGreaterEqual(text.count("await _send_red_alarm(endpoint)"), 3)
        self.assertIn("_staff_note_failure(endpoint)", inspect.getsource(listener._send_red_alarm))

    def test_the_staff_part_needs_only_three_fields_so_a_monitor_that_sends_its_own_mail_can_call_it_directly(self):
        self.start_quietly("ada")
        run(listener._staff_note_failure({"endpoint_id": "MULTICAST-224.1.1.1-5004", "display_name": "Site C multicast 224.1.1.1:5004", "root_cause": "Multicast Lost"}))
        self.assertEqual(self.the_failure()["people"], ["ada"])

    SITE_C_LOST = {"action": "raise", "stream": "5004", "kind": "lost", "cause": "Stream lost: no packets for 25 s",
                    "witnesses": [{"source": "MAIN", "state": "lost", "mbit_per_s": 0.0, "idle_seconds": 25, "age_seconds": 3}]}

    def test_a_lost_site_c_stream_is_given_to_everyone_on_shift_through_its_own_real_alarm_path(self):
        # The Site C feed monitor sends its own red mail and then calls the staff part with just an id, a cause and a name (the unpublished design notes); this is its real function.
        self.start_quietly("ada")
        self.start_quietly("bob")
        run(listener._site_c_alarm(dict(self.SITE_C_LOST)))
        failure = self.the_failure()
        self.assertEqual((failure["studio"], failure["display"], failure["people"]), ("SITE_C-5004", "Site C ZIXI Feed backup stream (UDP 5004)", ["ada", "bob"]))
        self.assertEqual([m for m in tsw.tcw.FakeSmtp.sent if "NOBODY ON SHIFT" in readable(m[1])], [])

    def test_a_lost_site_c_stream_with_nobody_on_shift_sends_the_nobody_notice_once(self):
        run(listener._site_c_alarm(dict(self.SITE_C_LOST)))
        notices = [m for m in tsw.tcw.FakeSmtp.sent if "NOBODY ON SHIFT" in readable(m[1]).split("\n")[0]]
        self.assertEqual(len(notices), 1)
        self.assertEqual(self.the_failure()["people"], [])

    def test_an_id_the_staff_log_cannot_hold_is_skipped_without_an_error_and_the_red_mail_still_goes_out(self):
        # The failure key is the endpoint id: 1 to 40 letters, digits, dots, dashes and underscores. A colon (an address written 224.1.1.1:5004) is NOT allowed there.
        row = staff.failure_row({"failures": {}}, "MULTICAST-224.1.1.1-5004", "Multicast Lost", "Site C multicast 224.1.1.1:5004", datetime.now(timezone.utc), "CORE", "abcd1234")
        self.assertIsNone(staff.row_problem(row))
        self.start_quietly("ada")
        self.through_the_real_gate(self.new_kind("224.1.1.1:5004"))
        self.assertEqual(self.failures(), {})
        self.assertEqual(self.red_mails, ["Site C multicast 224.1.1.1:5004"])


# ---- 3. The page rules, run in Node ---------------------------------------------------------------------------------------------------------------
RUNNER = r"""
const vm = require('vm');
const chunks = [];
process.stdin.on('data', c => chunks.push(c));
process.stdin.on('end', async () => {
  const job = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const context = vm.createContext({setTimeout: setTimeout});
  vm.runInContext(job.code, context, {timeout: 2000});
  const results = [];
  for (const call of job.calls) {
    let value = vm.runInContext(call, context, {timeout: 2000});
    if (value && typeof value.then === 'function') { value = await value; }
    results.push(value === undefined ? null : JSON.parse(JSON.stringify(value)));
  }
  process.stdout.write(JSON.stringify(results));
});
"""


def node_eval(code, calls):
    """Runs the page functions in `code` in Node and returns the value of each expression in `calls`. A missing Node is a FAILURE, never a skip."""
    node = shutil.which("node")
    if not node:
        raise AssertionError("Node is not installed: the page rules cannot be run, which is a failure and not a skip")
    done = subprocess.run([node, "-e", RUNNER], input=json.dumps({"code": code, "calls": calls}), capture_output=True, text=True, timeout=30, encoding="utf-8")
    if done.returncode != 0:
        raise AssertionError(f"node failed: {done.stderr[-800:]}")
    return json.loads(done.stdout)


def failure(**kw):
    base = {"fid": "f1", "display": "MAIN - USA", "active": True, "mine": False, "seen_by_me": False, "people": [], "seen": []}
    base.update(kw)
    return base


STAFF_PAGE = staff_pages.staff_page()
STAFF_RULE_NAMES = ("stopped", "activeFailures", "wantsSiren", "alarmLines", "idleDue", "staleDue", "titleFor", "readDrafts", "writeDrafts")


def staff_rules():
    """The page's own rule functions, cut out of its text when a test asks (so a missing one fails that test alone)."""
    return "\n".join(tsp.function_body(STAFF_PAGE, name) for name in STAFF_RULE_NAMES)


class StaffPageRuleTests(unittest.TestCase):
    def check(self, call):
        return node_eval(staff_rules(), [call])[0]

    def test_the_siren_is_wanted_for_a_person_on_shift_who_has_not_said_they_are_on_it_and_holds_the_failure_or_nobody_does(self):
        me = {"on_shift": True, "display": "Ada"}
        cases = [
            ([failure(mine=True)], True),
            ([failure()], True),                                                                  # nobody holds it: the person on shift must still be told
            ([failure(people=["Bob"])], False),                                                    # Bob has it
            ([failure(mine=True, seen_by_me=True)], False),                                        # pressed "I am on it"
            ([failure(mine=True, active=False)], False),                                           # old failure: no siren
            ([], False),
            ([failure(mine=True), failure(fid="f2", people=["Bob"])], True),
        ]
        for failures, expected in cases:
            self.assertIs(self.check(f"wantsSiren({json.dumps(me)}, {json.dumps(failures)})"), expected, failures)
        self.assertIs(self.check(f"wantsSiren({json.dumps({'on_shift': False})}, {json.dumps([failure(mine=True)])})"), False)         # not on shift: cannot press, no siren

    def test_the_strip_says_the_persons_own_name_and_what_to_do(self):
        me = {"on_shift": True, "display": "Ada"}
        lines = lambda failures: self.check(f"alarmLines({json.dumps(me)}, {json.dumps(failures)})")
        self.assertEqual(lines([failure(mine=True)]), ["Ada, MAIN - USA is assigned to you. Press I am on it, then fill in the resolve form."])
        self.assertEqual(lines([failure(mine=True, seen_by_me=True)]), ["Ada, MAIN - USA is assigned to you and you are on it. Fill in the resolve form when it is fixed."])
        self.assertEqual(lines([failure()]), ["Nobody has MAIN - USA yet."])
        self.assertEqual(lines([failure(people=["Bob", "Cy"])]), ["MAIN - USA is assigned to Bob, Cy."])
        self.assertEqual(lines([failure(active=False, mine=True)]), [])
        self.assertEqual(lines([]), [])
        self.assertEqual(lines([failure(mine=True), failure(fid="f2", display="SITE-A", people=["Bob"])]),
                         ["Ada, MAIN - USA is assigned to you. Press I am on it, then fill in the resolve form.", "SITE-A is assigned to Bob."])

    def test_a_display_name_that_ends_in_a_full_stop_never_gets_a_second_one(self):
        me = {"on_shift": True, "display": "Ada"}
        out = self.check(f"alarmLines({json.dumps(me)}, {json.dumps([failure(people=['Bob', 'Ada O.'])])})")
        self.assertEqual(out, ["MAIN - USA is assigned to Bob, Ada O."])

    def test_the_return_to_the_dashboard_is_due_at_exactly_120_seconds_and_never_when_the_clock_goes_backwards(self):
        for now, expected in ((119_999, False), (120_000, True), (500_000, True), (-5_000, False)):
            self.assertIs(self.check(f"idleDue({now}, 0, 120000)"), expected, now)

    def test_a_silent_feed_is_called_out_after_twelve_seconds(self):
        self.assertIs(self.check("staleDue(11999, 0, 12000)"), False)
        self.assertIs(self.check("staleDue(12000, 0, 12000)"), True)

    def test_the_title_says_alarm_only_while_one_is_wanted(self):
        self.assertEqual(self.check('titleFor("ClarixTech Watchdog - staff", true)'), "ALARM - ClarixTech Watchdog - staff")
        self.assertEqual(self.check('titleFor("ClarixTech Watchdog - staff", false)'), "ClarixTech Watchdog - staff")

    def test_drafts_survive_a_return_and_a_blocked_or_broken_store_changes_nothing(self):
        code = staff_rules() + r"""
        function memory(initial) { var data = {}; if (initial !== undefined) { data.k = initial; } return {getItem: function (key) { return key in data ? data[key] : null; }, setItem: function (key, value) { data[key] = value; }}; }
        var broken = {getItem: function () { throw new Error("blocked"); }, setItem: function () { throw new Error("blocked"); }};
        """
        calls = [
            'readDrafts(memory(JSON.stringify({f1: "Restarted it", f2: "x"})), "k")',
            'readDrafts(memory("not json"), "k")',
            'readDrafts(memory(JSON.stringify(["a"])), "k")',
            'readDrafts(memory(JSON.stringify({f1: 5, f2: "ok"})), "k")',
            'readDrafts(broken, "k")',
            'readDrafts(null, "k")',
            '(function () { var s = memory(); writeDrafts(s, "k", {f1: "hello", f2: ""}); return s.getItem("k"); })()',
            '(function () { writeDrafts(broken, "k", {f1: "hello"}); return "no throw"; })()',
        ]
        out = node_eval(code, calls)
        self.assertEqual(out[0], {"f1": "Restarted it", "f2": "x"})
        self.assertEqual(out[1], {})
        self.assertEqual(out[2], {})
        self.assertEqual(out[3], {"f2": "ok"})                                   # only text is ever restored
        self.assertEqual((out[4], out[5]), ({}, {}))
        self.assertEqual(json.loads(out[6]), {"f1": "hello"})                    # an empty note is not worth keeping
        self.assertEqual(out[7], "no throw")


class StaffPageWiringTests(unittest.TestCase):
    def test_the_alarm_strip_is_the_first_thing_in_the_page_announced_by_a_screen_reader_and_hidden_while_clear(self):
        main = STAFF_PAGE[STAFF_PAGE.index("<main>"):]
        self.assertRegex(main, r'^<main>\s*<div id="alarm" role="alert"[^>]*\bhidden\b')
        self.assertRegex(STAFF_PAGE, r'<p id="stale" role="alert"[^>]*\bhidden\b')

    def test_the_back_link_carries_the_address_the_server_gave_and_a_bad_one_falls_back_to_this_site(self):
        self.assertRegex(STAFF_PAGE, r'<a id="to-dashboard" href="/"')
        good = staff_pages.staff_page("https://dash.example.org/")
        self.assertRegex(good, r'<a id="to-dashboard" href="https://dash\.example\.org/"')
        for bad in ("http://dash.example.org/", "javascript:alert(1)", 'https://x.example/"x', "//evil.example", "https://" + "a" * 300, None, 5):
            page = staff_pages.staff_page(bad)
            self.assertRegex(page, r'<a id="to-dashboard" href="/"', repr(bad))
            self.assertNotIn("evil.example", page)

    def test_after_120_seconds_without_activity_the_page_goes_to_the_same_address_as_the_back_link(self):
        self.assertRegex(STAFF_PAGE, r"var IDLE_MS = 120000;")
        for event in ("pointerdown", "pointermove", "keydown", "touchstart", "wheel", "scroll", "input"):
            self.assertIn(f'"{event}"', STAFF_PAGE, event)
        check = tsp.function_body(STAFF_PAGE, "checkIdle")
        self.assertIn("idleDue(", check)
        self.assertIn('location.replace($("to-dashboard").href)', check)
        self.assertRegex(STAFF_PAGE, r"setInterval\(checkIdle,\s*1000\)")
        self.assertIn("performance.now()", check)                                            # a monotonic clock: a corrected PC clock must neither trigger nor hide it
        self.assertNotIn("Date.now()", check)

    def test_activity_is_noted_by_every_kind_of_input_and_resets_the_clock(self):
        self.assertRegex(STAFF_PAGE, r"function noteActivity\(\)\s*\{\s*lastActivity = performance\.now\(\);")
        self.assertRegex(STAFF_PAGE, r"addEventListener\(name, noteActivity")

    def test_the_siren_is_one_oscillator_made_once_only_after_a_touch_and_the_locked_state_is_said_out_loud(self):
        self.assertEqual(STAFF_PAGE.count("createOscillator("), 1)
        self.assertIn("AudioContext", STAFF_PAGE)
        self.assertRegex(STAFF_PAGE, r'addEventListener\("pointerdown", unlockSound')
        self.assertRegex(STAFF_PAGE, r'addEventListener\("keydown", unlockSound')
        self.assertIn('id="alarm-sound"', STAFF_PAGE)                                         # "tap here to turn the alarm sound on"
        self.assertIn("Tap anywhere on this page to turn the alarm sound on", STAFF_PAGE)
        self.assertNotRegex(STAFF_PAGE, r"\.play\(\)")                                         # nothing is ever started without a touch first

    def test_the_siren_follows_the_rule_and_stops_when_the_person_says_they_are_on_it(self):
        render = tsp.function_body(STAFF_PAGE, "render")
        self.assertIn("wantsSiren(me, view.failures", render)
        self.assertIn("setSiren(", render)
        self.assertIn("titleFor(", render)
        self.assertIn("alarmLines(me, view.failures", render)

    def test_a_feed_that_stops_is_never_silent(self):
        load = tsp.function_body(STAFF_PAGE, "load")
        self.assertIn("lastGood = performance.now()", load)
        self.assertIn("staleDue(", STAFF_PAGE)
        self.assertIn("Live updates have stopped", STAFF_PAGE)
        self.assertNotRegex(load, r"catch\(function \(\) \{\}\)")                             # the old code swallowed the failure

    def test_drafts_are_kept_in_this_tab_only_and_dropped_once_the_failure_is_finished(self):
        self.assertIn("sessionStorage", STAFF_PAGE)
        self.assertRegex(STAFF_PAGE, r"var notes = readDrafts\(")
        self.assertIn("writeDrafts(", tsp.function_body(STAFF_PAGE, "saveNote"))
        self.assertIn("delete notes[failure.fid]", STAFF_PAGE)

    def test_the_resolve_form_button_goes_to_the_note_box_of_the_persons_own_failure(self):
        self.assertIn('id="alarm-form"', STAFF_PAGE)
        self.assertIn("focusResolveForm", STAFF_PAGE)
        self.assertIn(".focus()", tsp.function_body(STAFF_PAGE, "focusResolveForm"))


DASH_PAGE = dashboard.render_dashboard_html("view-token-for-tests")


def dashboard_rules():
    return "\n".join(tsp.function_body(DASH_PAGE, name) for name in ("stopped", "alertAge", "alertLines"))


class DashboardAlertTests(unittest.TestCase):
    def check(self, call):
        return node_eval(dashboard_rules(), [call])[0]

    def item(self, **kw):
        base = {"playout": "MAIN - USA", "since": "2026-11-08T10:00:00+00:00", "age_s": 720, "assigned": ["Ada O.", "Bob"], "on_it": ["Ada O."]}
        base.update(kw)
        return base

    def lines(self, data):
        return self.check(f"alertLines({json.dumps(data)})")

    def test_each_open_failure_says_how_long_who_holds_it_and_who_is_on_it(self):
        self.assertEqual(self.lines({"items": [self.item()], "more": 0}), ["MAIN - USA: critical failure open for 12m 00s. Assigned to Ada O., Bob. On it: Ada O."])

    def test_a_failure_nobody_holds_or_nobody_has_taken_is_said_plainly(self):
        self.assertEqual(self.lines({"items": [self.item(assigned=[], on_it=[])], "more": 0}), ["MAIN - USA: critical failure open for 12m 00s. Nobody is assigned yet."])
        self.assertEqual(self.lines({"items": [self.item(on_it=[])], "more": 0}), ["MAIN - USA: critical failure open for 12m 00s. Assigned to Ada O., Bob. Nobody has said they are on it yet."])

    def test_a_display_name_that_ends_in_a_full_stop_never_gets_a_second_one(self):
        out = self.lines({"items": [self.item(assigned=["Bob", "Ada O."], on_it=[])], "more": 0})
        self.assertEqual(out, ["MAIN - USA: critical failure open for 12m 00s. Assigned to Bob, Ada O. Nobody has said they are on it yet."])

    def test_the_length_of_time_is_written_the_way_the_staff_page_writes_it(self):
        for seconds, text in ((0, "0s"), (45, "45s"), (59.6, "1m 00s"), (3725, "1h 02m"), (-5, "0s")):
            self.assertEqual(self.check(f"alertAge({seconds})"), text, seconds)

    def test_more_than_the_list_shows_is_counted(self):
        self.assertEqual(self.lines({"items": [self.item()], "more": 2})[-1], "and 2 more open failures.")
        self.assertEqual(self.lines({"items": [self.item()], "more": 1})[-1], "and 1 more open failure.")

    def test_nothing_or_garbage_gives_no_lines_and_never_throws(self):
        for data in (None, {}, {"items": []}, {"items": "x"}, {"items": [None, 5, "a", {}]}, {"items": [{"playout": 5}]}, 7, "text", []):
            self.assertEqual(self.lines(data), [], data)

    def test_the_strip_is_a_hidden_live_region_before_the_toolbar_and_is_fed_by_every_frame(self):
        self.assertRegex(DASH_PAGE, r'<div id="staff-alert" class="staff-alert" role="alert"[^>]*\bhidden\b')
        self.assertLess(DASH_PAGE.index('id="staff-alert"'), DASH_PAGE.index('<div class="toolbar">'))
        self.assertIn("renderStaffAlerts(frame.staff_alerts)", DASH_PAGE)

    def test_the_topology_page_carries_the_same_strip_and_is_fed_the_same_way(self):
        topology = dashboard.render_topology_html("view-token-for-tests")           # the other page a signed-in staff member can sit on
        self.assertRegex(topology, r'<div id="staff-alert" class="staff-alert" role="alert"[^>]*\bhidden\b')
        self.assertIn("renderStaffAlerts(frame.staff_alerts)", topology)

    def test_it_writes_text_only_and_links_to_the_resolve_form_only_over_https(self):
        body = tsp.function_body(DASH_PAGE, "renderStaffAlerts")
        self.assertIn("textContent", body)
        for word in tsp.FORBIDDEN_SCRIPT:
            self.assertNotIn(word, body, word)
        self.assertRegex(body, r'/\^https:\\/\\/')
        self.assertIn('"noopener"', body)
        self.assertIn(".toolbar .shift-chip", DASH_PAGE)                                      # the old chip is still there beside it
        self.assertIn(".staff-alert", DASH_PAGE)


# ---- 4. The real Staff page script, run end to end in Node ------------------------------------------------------------------------------------------
# A fake page (elements, events, a clock the test moves, a store, a network that answers when the test says, and an audio device that refuses sound until it is touched) around the
# REAL script text of the Staff page. This is what catches "the rule is right but the page never calls it" (found by a mutation run: string checks had passed while the page
# could have dropped the strip or the stale warning).
PRELUDE = r"""
var __now = 0, __intervals = [], __listeners = {}, __replaced = [], __pending = [], __oscCount = 0, __silenced = 0, __beeps = 0, __focused = null, __els = {};
var __data = __STORE__;
function __El(tag) { this.tag = tag; this.children = []; this.hidden = false; this.className = ""; this._text = ""; this.attrs = {}; this.value = ""; this.style = {}; this.disabled = false; this.listeners = {}; }
__El.prototype.appendChild = function (c) { this.children.push(c); return c; };
__El.prototype.removeChild = function (c) { var i = this.children.indexOf(c); if (i >= 0) { this.children.splice(i, 1); } return c; };
__El.prototype.setAttribute = function (n, v) { this.attrs[n] = String(v); };
__El.prototype.addEventListener = function (n, f) { this.listeners[n] = f; };
__El.prototype.contains = function () { return false; };
__El.prototype.focus = function () { __focused = this; };
__El.prototype.scrollIntoView = function () {};
Object.defineProperty(__El.prototype, "lastChild", {get: function () { return this.children[this.children.length - 1]; }});
Object.defineProperty(__El.prototype, "textContent", {get: function () { return this._text; }, set: function (v) { this._text = String(v); this.children = []; }});
function __FakeCtx() { this.state = "suspended"; this.currentTime = 0; this.destination = {}; }
__FakeCtx.prototype.resume = function () { this.state = "running"; };
__FakeCtx.prototype.createOscillator = function () { __oscCount += 1; return {type: "", frequency: {setValueAtTime: function () { __beeps += 1; }}, connect: function () {}, start: function () {}}; };
__FakeCtx.prototype.createGain = function () { return {gain: {value: 1, setValueAtTime: function () {}, cancelScheduledValues: function () {}, setTargetAtTime: function () { __silenced += 1; }}, connect: function () {}}; };
var document = {
  title: "ClarixTech Watchdog - staff", activeElement: null,
  getElementById: function (id) { return __els[id] || (__els[id] = new __El("#" + id)); },
  createElement: function (tag) { return new __El(tag); },
  querySelector: function () { return null; },
  addEventListener: function (name, fn) { (__listeners[name] = __listeners[name] || []).push(fn); }
};
var window = {sessionStorage: {getItem: function (k) { return k in __data ? __data[k] : null; }, setItem: function (k, v) { __data[k] = String(v); }}, AudioContext: __AUDIO__};
var performance = {now: function () { return __now; }};
var location = {replace: function (u) { __replaced.push(u); }};
var setInterval = function (fn, ms) { __intervals.push({fn: fn, ms: ms}); return __intervals.length; };
var fetch = function (path) { return new Promise(function (resolve, reject) { __pending.push({path: path, resolve: resolve, reject: reject}); }); };
document.getElementById("to-dashboard").href = __ADDRESS__;
["alarm", "alarm-form", "alarm-sound", "stale"].forEach(function (id) { document.getElementById(id).hidden = true; });          // they start hidden in the real page
var __detailsOpen = (typeof __DETAILS__ === "undefined") ? {} : __DETAILS__;                                                  // which sections start open, read from the real markup
Object.keys(__detailsOpen).forEach(function (id) { document.getElementById(id).open = __detailsOpen[id]; });
function $(id) { return document.getElementById(id); }                                                                       // the Staff page has its own; the panel script has none
function __text(n) { return [n._text].concat(n.children.map(__text)).join(" ").split(" ").filter(Boolean).join(" "); }
function __buttons(n) { var out = n.tag === "button" ? [n._text] : []; n.children.forEach(function (c) { out = out.concat(__buttons(c)); }); return out; }
function __answer(data, status) {
  var p = __pending.shift();
  p.resolve({status: status || 200, json: function () { return Promise.resolve(data); }});
  return new Promise(function (done) { setTimeout(function () { done(p.path); }, 5); });
}
function __fire(name) { (__listeners[name] || []).forEach(function (fn) { fn({}); }); }
function __tick() { __intervals.forEach(function (i) { if (i.ms === 1000) { i.fn(); } }); }
function __at(ms) { __now = ms; __tick(); return __replaced.slice(); }
function __find(node, test) {
  if (test(node)) { return node; }
  for (var i = 0; i < node.children.length; i++) { var hit = __find(node.children[i], test); if (hit) { return hit; } }
  return null;
}
function __state() {
  return {alarmHidden: $("alarm").hidden, alarmLines: $("alarm-list").children.map(function (c) { return c.textContent; }), formHidden: $("alarm-form").hidden,
          soundPromptHidden: $("alarm-sound").hidden, title: document.title, staleHidden: $("stale").hidden, oscillators: __oscCount, silenced: __silenced, beeps: __beeps, replaced: __replaced.slice()};
}
"""

FAKE_AUDIO = "__FakeCtx"


def page_script(page):
    return re.search(r"<script>(.*)</script>", page, re.S).group(1)


def page_eval(calls, store=None, address="https://dash.example.org/", audio=True):
    code = ("var __DETAILS__ = " + json.dumps(details_state(STAFF_PAGE)) + ";\n"
            + PRELUDE.replace("__STORE__", json.dumps(store or {})).replace("__ADDRESS__", json.dumps(address)).replace("__AUDIO__", FAKE_AUDIO if audio else "undefined")
            + page_script(STAFF_PAGE))
    return node_eval(code, calls)


def view(failures, on_shift=True):
    return {"ok": True, "me": {"username": "ada", "display": "Ada", "on_shift": on_shift, "since": "2026-11-08T10:00:00+00:00", "long": False}, "roster": [], "failures": failures, "done": []}


def sfail(**kw):
    base = {"fid": "f1", "studio": "MAIN-USA", "cause": "Process Missing", "latest_cause": "Process Missing", "display": "MAIN - USA", "first_at": "2026-11-08T10:00:00+00:00",
            "last_at": "2026-11-08T10:00:00+00:00", "alarms": 1, "people": ["Ada"], "seen": [], "mine": True, "seen_by_me": False, "active": True}
    base.update(kw)
    return base


def answer(data):
    return f"__answer({json.dumps(data)})"


class StaffPageEndToEndTests(unittest.TestCase):
    def test_a_failure_given_to_me_shows_the_strip_with_my_name_the_alarm_title_and_the_sound_prompt_until_i_touch_the_page(self):
        out = page_eval([answer(view([sfail()])), "__state()"])[1]
        self.assertFalse(out["alarmHidden"])
        self.assertEqual(out["alarmLines"], ["Ada, MAIN - USA is assigned to you. Press I am on it, then fill in the resolve form."])
        self.assertFalse(out["formHidden"])
        self.assertEqual(out["title"], "ALARM - ClarixTech Watchdog - staff")
        self.assertFalse(out["soundPromptHidden"])                      # the browser has not been touched: the page says so instead of sitting silent
        self.assertEqual(out["oscillators"], 0)

    def test_one_touch_starts_the_siren_it_stays_one_oscillator_through_every_refresh_and_stops_when_i_say_i_am_on_it(self):
        touched = page_eval([answer(view([sfail()])), "__fire('pointerdown'), __state()"])[1]
        self.assertTrue(touched["soundPromptHidden"])
        self.assertEqual(touched["oscillators"], 1)
        self.assertGreater(touched["beeps"], 0)

        def refresh(failures):
            return "render(" + json.dumps(view(failures)) + "), 0"

        out = page_eval([answer(view([sfail()])), "__fire('pointerdown'), 0", refresh([sfail()]), refresh([sfail()]), "__state()",
                         refresh([sfail(seen_by_me=True)]), "__state()", refresh([sfail()]), "__state()"])
        running, silent, again = out[4], out[6], out[8]
        self.assertEqual(running["oscillators"], 1)                          # three refreshes, still one oscillator
        self.assertEqual(running["silenced"], 0)
        self.assertGreaterEqual(silent["silenced"], 1)                       # "I am on it": silenced for me
        self.assertEqual(silent["alarmLines"], ["Ada, MAIN - USA is assigned to you and you are on it. Fill in the resolve form when it is fixed."])
        self.assertFalse(silent["alarmHidden"])                              # the strip stays until the failure is finished
        self.assertEqual(silent["title"], "ALARM - ClarixTech Watchdog - staff")      # silenced for me, but the failure is still open: the title still says so
        self.assertEqual(again["oscillators"], 1)                            # wanted again later: the same oscillator, not a new one

    def test_when_the_failure_is_finished_the_strip_goes_and_the_title_is_plain_again(self):
        out = page_eval([answer(view([sfail()])), "render(" + json.dumps(view([])) + "), 0", "__state()"])[2]
        self.assertTrue(out["alarmHidden"])
        self.assertTrue(out["formHidden"])
        self.assertEqual(out["title"], "ClarixTech Watchdog - staff")
        self.assertEqual(out["alarmLines"], [])

    def test_an_old_failure_is_listed_but_is_not_an_alarm(self):
        out = page_eval([answer(view([sfail(active=False)])), "__state()"])[1]
        self.assertTrue(out["alarmHidden"])
        self.assertEqual(out["title"], "ClarixTech Watchdog - staff")

    def test_a_person_not_on_shift_sees_the_strip_but_hears_no_siren(self):
        out = page_eval([answer(view([sfail(mine=False, people=[])], on_shift=False)), "__fire('pointerdown'), __state()"])[1]
        self.assertFalse(out["alarmHidden"])
        self.assertEqual(out["alarmLines"], ["Nobody has MAIN - USA yet."])
        self.assertEqual(out["title"], "ALARM - ClarixTech Watchdog - staff")       # an alarm is open even though this person is not the one to hear a siren
        self.assertEqual(out["oscillators"], 0)
        self.assertTrue(out["soundPromptHidden"])

    def test_the_resolve_form_button_focuses_the_note_box_of_my_failure(self):
        calls = [answer(view([sfail()])), "$('alarm-form').listeners.click(), __focused && __focused.tag"]
        # The fake page's querySelector finds nothing by itself, so give it the real note box the page built.
        code = PRELUDE.replace("__STORE__", "{}").replace("__ADDRESS__", json.dumps("/")).replace("__AUDIO__", FAKE_AUDIO) + page_script(STAFF_PAGE)
        code += "\ndocument.querySelector = function () { return __find($('failures'), function (n) { return n.tag === 'input'; }); };"
        self.assertEqual(node_eval(code, calls)[1], "input")

    def test_two_minutes_without_a_touch_go_to_the_dashboard_address_and_any_touch_starts_the_count_again(self):
        out = page_eval(["__at(119999)", "__at(120000)"], address="https://dash.example.org/")
        self.assertEqual(out[0], [])
        self.assertEqual(out[1], ["https://dash.example.org/"])
        later = page_eval(["__now = 100000, __fire('keydown'), __at(219999)", "__at(220000)", "Object.keys(__listeners).sort()"])
        self.assertEqual(later[0], [])
        self.assertEqual(later[1], ["https://dash.example.org/"])
        self.assertEqual(later[2], sorted(["input", "keydown", "pointerdown", "pointermove", "scroll", "touchstart", "wheel"]))

    def test_a_pc_clock_that_goes_backwards_neither_fires_the_return_early_nor_holds_it_back(self):
        out = page_eval(["__at(60000)", "__at(-500000)", "__at(180000)"])
        self.assertEqual(out[1], [])
        self.assertEqual(out[2], ["https://dash.example.org/"])

    def test_a_feed_that_stops_is_called_out_after_12_seconds_and_a_good_refresh_clears_it(self):
        out = page_eval(["__at(11999), __state().staleHidden", "__at(12000), __state().staleHidden", answer(view([])), "__at(12001), __state().staleHidden"])
        self.assertTrue(out[0])
        self.assertFalse(out[1])
        self.assertTrue(out[3])

    def test_a_refresh_that_cannot_reach_the_server_calls_the_feed_stale_by_itself(self):
        # No timer tick is run here: only the failure path of the refresh itself can say it (the `catch`).
        lost = ("(function () { __now = 15000; __pending.shift().reject(new Error('down')); "
                "return new Promise(function (done) { setTimeout(done, 5); }).then(function () { return __state().staleHidden; }); })()")
        self.assertFalse(page_eval([lost])[0])

    def test_a_half_written_note_is_kept_in_the_tab_and_comes_back_after_the_return(self):
        type_it = ("(function () { var box = __find($('failures'), function (n) { return n.tag === 'input'; }); box.value = 'Restarted the player, it had hung'; box.listeners.input(); "
                   "return window.sessionStorage.getItem('watchdog-staff-notes'); })()")
        out = page_eval([answer(view([sfail()])), type_it])
        self.assertEqual(json.loads(out[1]), {"f1": "Restarted the player, it had hung"})
        back = page_eval([answer(view([sfail()])), "__find($('failures'), function (n) { return n.tag === 'input'; }).value"], store={"watchdog-staff-notes": out[1]})
        self.assertEqual(back[1], "Restarted the player, it had hung")

    def test_marking_done_forgets_the_draft(self):
        press = ("(function () { var box = __find($('failures'), function (n) { return n.tag === 'input'; }); box.value = 'fixed'; box.listeners.input(); "
                 "__find($('failures'), function (n) { return n.tag === 'button' && n.textContent === 'Mark done'; }).listeners.click(); return window.sessionStorage.getItem('watchdog-staff-notes'); })()")
        out = page_eval([answer(view([sfail()])), press, answer(view([])), "window.sessionStorage.getItem('watchdog-staff-notes')"])
        self.assertEqual(json.loads(out[1]), {"f1": "fixed"})
        self.assertEqual(json.loads(out[3]), {})

    def test_without_any_audio_support_the_page_still_works_and_still_shows_the_strip(self):
        out = page_eval([answer(view([sfail()])), "__fire('pointerdown'), __state()"], audio=False)[1]
        self.assertFalse(out["alarmHidden"])
        self.assertEqual(out["oscillators"], 0)
        self.assertFalse(out["soundPromptHidden"])                           # still says the sound is not on, rather than pretending

    def test_a_blocked_session_store_changes_nothing_else(self):
        code = PRELUDE.replace("__STORE__", "{}").replace("__ADDRESS__", json.dumps("/")).replace("__AUDIO__", FAKE_AUDIO)
        code = code.replace("var window = {sessionStorage: {getItem: function (k) { return k in __data ? __data[k] : null; }, setItem: function (k, v) { __data[k] = String(v); }}",
                            "var window = {get sessionStorage() { throw new Error('blocked'); }")
        self.assertIn("get sessionStorage", code)                           # the edit above really made the store throw
        out = node_eval(code + page_script(STAFF_PAGE), [answer(view([sfail()])), "__state()"])[1]
        self.assertFalse(out["alarmHidden"])


# ---- 5. The dashboard strip's drawing, run in Node with a fake page --------------------------------------------------------------------------------------
STRIP_PRELUDE = r"""
function __El(tag) { this.tag = tag; this.children = []; this.hidden = false; this.className = ""; this._text = ""; this.attrs = {}; }
__El.prototype.appendChild = function (c) { this.children.push(c); return c; };
__El.prototype.removeChild = function (c) { var i = this.children.indexOf(c); if (i >= 0) { this.children.splice(i, 1); } return c; };
__El.prototype.getAttribute = function (n) { return n in this.attrs ? this.attrs[n] : null; };
Object.defineProperty(__El.prototype, "lastChild", {get: function () { return this.children[this.children.length - 1]; }});
Object.defineProperty(__El.prototype, "textContent", {get: function () { return this._text; }, set: function (v) { this._text = String(v); this.children = []; }});
Object.defineProperty(__El.prototype, "href", {get: function () { return this.attrs.href; }, set: function (v) { this.attrs.href = String(v); }});
var __box = new __El("div"); __box.hidden = true;
var document = {getElementById: function (id) { return id === "staff-alert" ? __box : null; }, createElement: function (tag) { return new __El(tag); }};
var staffAlertParts = null;
function __shape() { return {hidden: __box.hidden, rows: __box.children.length ? __box.children[1].children.map(function (r) { return r.textContent; }) : [],
                             link: __box.children.length ? {hidden: __box.children[2].hidden, href: __box.children[2].href || null} : null}; }
"""


def strip_eval(calls):
    return node_eval(STRIP_PRELUDE + "\n".join(tsp.function_body(DASH_PAGE, name) for name in ("stopped", "alertAge", "alertLines", "renderStaffAlerts")), calls)


class DashboardStripDrawingTests(unittest.TestCase):
    DATA = {"items": [{"playout": "MAIN - USA", "since": "x", "age_s": 65, "assigned": ["Ada O."], "on_it": []}], "more": 0, "form_url": "https://staff.example.org/staff"}

    def test_the_strip_shows_the_lines_and_a_link_to_the_staff_page(self):
        out = strip_eval([f"renderStaffAlerts({json.dumps(self.DATA)}), __shape()"])[0]
        self.assertFalse(out["hidden"])
        self.assertEqual(out["rows"], ["MAIN - USA: critical failure open for 1m 05s. Assigned to Ada O. Nobody has said they are on it yet."])
        self.assertEqual(out["link"], {"hidden": False, "href": "https://staff.example.org/staff"})

    def test_it_hides_when_nothing_is_open_and_shows_again_when_something_is(self):
        out = strip_eval([f"renderStaffAlerts({json.dumps(self.DATA)}), 0", "renderStaffAlerts({items: [], more: 0, form_url: ''}), __shape().hidden",
                          f"renderStaffAlerts({json.dumps(self.DATA)}), __shape().hidden", "renderStaffAlerts(undefined), __shape().hidden"])
        self.assertTrue(out[1])
        self.assertFalse(out[2])
        self.assertTrue(out[3])                                                # a frame from an older server has no such part

    def test_the_same_elements_are_updated_in_place_so_a_hovered_link_and_selected_text_survive_a_frame(self):
        out = strip_eval([f"renderStaffAlerts({json.dumps(self.DATA)}), 0",
                          "(function () { var row = __box.children[1].children[0], link = __box.children[2]; "
                          + f"renderStaffAlerts({json.dumps({**self.DATA, 'items': [{**self.DATA['items'][0], 'age_s': 70}]})}); "
                          "return [row === __box.children[1].children[0], link === __box.children[2], __box.children[1].children[0].textContent]; })()"])[1]
        self.assertEqual(out[:2], [True, True])
        self.assertIn("1m 10s", out[2])

    def test_rows_follow_the_number_of_open_failures(self):
        three = {**self.DATA, "items": [dict(self.DATA["items"][0], playout=name) for name in ("A", "B", "C")]}
        out = strip_eval([f"renderStaffAlerts({json.dumps(three)}), __shape().rows.length", f"renderStaffAlerts({json.dumps(self.DATA)}), __shape().rows.length"])
        self.assertEqual(out, [3, 1])

    def test_a_link_is_offered_only_for_a_plain_https_address(self):
        for address, shown in (("https://staff.example.org/staff", True), ("http://staff.example.org/", False), ("javascript:alert(1)", False), ("", False)):
            data = {**self.DATA, "form_url": address}
            out = strip_eval([f"renderStaffAlerts({json.dumps(data)}), __shape().link"])[0]
            self.assertEqual(out["hidden"], not shown, address)
            if shown:
                self.assertEqual(out["href"], address)

    def test_text_typed_by_a_person_is_only_ever_text(self):
        hostile = "<img src=x onerror=alert(1)>"
        data = {**self.DATA, "items": [dict(self.DATA["items"][0], playout=hostile, assigned=[hostile], on_it=[hostile])]}
        out = strip_eval([f"renderStaffAlerts({json.dumps(data)}), __shape().rows[0]", "__box.children[1].children[0].children.length"])
        self.assertIn(hostile, out[0])
        self.assertEqual(out[1], 0)                                            # no element was made out of it

    def test_a_count_of_more_failures_is_a_line_of_its_own_and_only_with_something_to_count_it_against(self):
        out = strip_eval([f"alertLines({json.dumps({'items': [], 'more': 3})})", f"alertLines({json.dumps({'items': [None], 'more': 3})})"])
        self.assertEqual(out, [[], []])


# ---- 6. His panel has nothing to assign by hand; long sections fold away (his words via the parent session, 2026-11-08) ---------------------------------------------------
# "I do not need to assign any errors to anybody on the shift, it should auto assign, I do not need all that on my page." and his standing rule: long sections are collapsible
# (native details and summary, the one-line status in the summary, the urgent item open).
PANEL = tsp.control_access.panel_page()


def details_state(page):
    """{id: open at the start} read from the REAL markup, so the fake page starts exactly as the real one does."""
    return {found.group(1): bool(re.search(r"\sopen[\s>]", found.group(0))) for found in re.finditer(r'<details\b[^>]*\bid="([^"]+)"[^>]*>', page)}


def panel_eval(calls):
    code = ("var __DETAILS__ = " + json.dumps(details_state(PANEL)) + ";\n" + PRELUDE.replace("__STORE__", "{}").replace("__ADDRESS__", json.dumps("/")).replace("__AUDIO__", "undefined")
            + page_script(PANEL))
    return node_eval(code, calls)


def panel_failure(**kw):
    base = {"fid": "f1", "studio": "MAIN-USA", "cause": "Process Missing", "latest_cause": "Process Missing", "display": "MAIN - USA", "first_at": "2026-11-08T10:00:00+00:00",
            "last_at": "2026-11-08T10:00:00+00:00", "alarms": 1, "people": [{"username": "ada", "display": "Ada"}, {"username": "kemi", "display": "Kemi"}],
            "seen": [{"username": "ada", "display": "Ada", "at": "2026-11-08T10:01:00+00:00", "after_s": 60}]}
    base.update(kw)
    return base


def panel_view(failures=(), roster=(), people=None, done=()):
    people = people if people is not None else [{"username": "ada", "display": "Ada", "active": True, "on_shift": True, "since": "x", "long": False},
                                                {"username": "kemi", "display": "Kemi", "active": True, "on_shift": False, "since": None, "long": False},
                                                {"username": "old", "display": "Old", "active": False, "on_shift": False, "since": None, "long": False}]
    return {"ok": True, "slots": {"active": 2, "max": 10}, "people": people, "roster": list(roster), "failures": list(failures), "done": list(done)}


class HisPanelHasNothingToAssignTests(unittest.TestCase):
    def test_there_is_no_assign_picker_no_assign_button_and_no_remove_button_on_his_page(self):
        for needle in ('action: "assign"', 'action: "unassign"', 'el("button", "Assign"', '" (remove)"', 'type = "checkbox"', "Nobody is on shift to assign", "Tick at least one person"):
            self.assertNotIn(needle, PANEL, needle)
        self.assertNotIn("Only you add, reset, disable and assign", PANEL)
        self.assertIn("assigned automatically", PANEL)                                  # and the page says why there is nothing to press

    def test_he_still_sees_who_holds_each_failure_and_who_is_on_it_and_can_mark_done_but_the_only_button_on_a_failure_is_done(self):
        out = panel_eval(["renderStaff(" + json.dumps(panel_view([panel_failure()], roster=[{"username": "ada", "display": "Ada", "since": "x", "long": False}])) + ", true), 0",
                          "__text($('failures'))", "__buttons($('failures'))", "__buttons($('roster'))"])
        self.assertIn("With Ada, Kemi.", out[1])
        self.assertIn("On it: Ada at", out[1])
        self.assertEqual(out[2], ["Mark done"])
        self.assertEqual(out[3], ["Finish shift"])                                      # his power over shifts is untouched

    def test_a_failure_nobody_holds_says_so_and_has_no_assign_control_either(self):
        out = panel_eval(["renderStaff(" + json.dumps(panel_view([panel_failure(people=[], seen=[])], roster=[{"username": "ada", "display": "Ada", "since": "x", "long": False}])) + ", true), 0",
                          "__text($('failures'))", "__buttons($('failures'))"])
        self.assertIn("Nobody has it yet.", out[1])
        self.assertEqual(out[2], ["Mark done"])

    def test_the_people_table_keeps_start_disable_and_delete(self):
        out = panel_eval(["renderStaff(" + json.dumps(panel_view()) + ", true), 0", "__buttons($('people'))"])[1]
        self.assertEqual(sorted(set(out)), ["Delete", "Disable", "Start shift"])

    def test_the_server_door_is_left_alone_so_his_control_tool_can_still_assign_by_hand(self):
        self.assertIn('"assign"', inspect.getsource(listener).split("STAFF_PANEL_ACTIONS = ")[1].split("\n")[0])


class CollapsibleSectionTests(unittest.TestCase):
    PANEL_SECTIONS = ("sec-roster", "sec-failures", "sec-people", "sec-add", "sec-reset", "sec-done")
    STAFF_SECTIONS = ("sec-roster", "sec-failures", "sec-done")

    def test_the_long_sections_are_native_details_with_a_one_line_status_in_the_summary_and_only_the_urgent_one_starts_open(self):
        for page, sections in ((PANEL, self.PANEL_SECTIONS), (STAFF_PAGE, self.STAFF_SECTIONS)):
            state = details_state(page)
            for name in sections:
                self.assertIn(name, state, name)
                self.assertRegex(page, rf'<details[^>]*id="{name}"[^>]*>\s*<summary[^>]*>')
            self.assertEqual({name for name, opened in state.items() if opened and name.startswith("sec-")}, {"sec-failures"})      # the urgent item is open, the rest fold away
            for name in ("sum-roster", "sum-failures", "sum-done"):
                self.assertRegex(page, rf'<summary[^>]*id="{name}"')

    def test_the_urgent_item_is_never_folded_away_on_the_dashboard(self):
        self.assertNotIn("<details", DASH_PAGE[DASH_PAGE.index('id="staff-alert"') - 200:DASH_PAGE.index('id="staff-alert"') + 200])

    def run_pure(self, page, name, expression):
        return node_eval(tsp.function_body(page, name), [expression])[0]

    def test_the_status_lines_read_the_same_on_both_pages(self):
        for page in (PANEL, STAFF_PAGE):
            self.assertEqual(self.run_pure(page, "rosterSummary", 'rosterSummary(["Ada", "Kemi"])'), "On shift now: Ada, Kemi")
            self.assertEqual(self.run_pure(page, "rosterSummary", "rosterSummary([])"), "On shift now: nobody")
            self.assertEqual(self.run_pure(page, "failuresSummary", "failuresSummary(0)"), "Open failures: none")
            self.assertEqual(self.run_pure(page, "failuresSummary", "failuresSummary(3)"), "Open failures: 3")
            self.assertEqual(self.run_pure(page, "doneSummary", "doneSummary(0)"), "Finished in the last 24 hours: none")
            self.assertEqual(self.run_pure(page, "doneSummary", "doneSummary(2)"), "Finished in the last 24 hours: 2")

    def test_the_urgent_section_opens_on_first_sight_and_again_on_every_new_alarm_but_otherwise_leaves_the_persons_choice_alone(self):
        for page in (PANEL, STAFF_PAGE):
            rule = lambda previous, current: self.run_pure(page, "urgentOpen", f"urgentOpen({previous}, {current})")
            self.assertIs(rule("null", 2), True)           # first look, something urgent: open
            self.assertIs(rule("null", 0), False)          # first look, nothing urgent: folded
            self.assertIs(rule(1, 2), True)                # another alarm: open again even if they had folded it
            self.assertIsNone(rule(2, 2))                  # nothing new: their choice stands
            self.assertIsNone(rule(2, 1))                  # one was finished: their choice stands
            self.assertIsNone(rule(1, 0))

    def test_the_panel_summarises_its_people_in_one_line(self):
        people = [{"active": True, "on_shift": True}, {"active": True, "on_shift": False}, {"active": False, "on_shift": False}]
        self.assertEqual(node_eval(tsp.function_body(PANEL, "peopleSummary"), [f"peopleSummary({json.dumps(people)})"])[0], "People: 2 active, 1 on shift")

    def test_on_the_staff_page_a_new_alarm_opens_the_open_failures_section_even_if_the_person_folded_it(self):
        view_one = view([sfail()])
        view_two = view([sfail(), sfail(fid="f2", display="SITE-A")])
        out = page_eval([answer(view_one), "[$('sec-failures').open, $('sum-failures').textContent]",
                         "(function () { $('sec-failures').open = false; render(" + json.dumps(view_one) + "); return $('sec-failures').open; })()",
                         "(function () { render(" + json.dumps(view_two) + "); return [$('sec-failures').open, $('sum-failures').textContent]; })()"])
        self.assertEqual(out[1], [True, "Open failures: 1"])
        self.assertFalse(out[2])                                                      # same count: the person's fold is respected
        self.assertEqual(out[3], [True, "Open failures: 2"])                          # a new alarm: open again

    def test_on_the_staff_page_nothing_urgent_means_the_section_starts_folded_and_the_summary_says_none(self):
        out = page_eval([answer(view([])), "[$('sec-failures').open, $('sum-failures').textContent, $('sum-roster').textContent, $('sum-done').textContent]"])[1]
        self.assertEqual(out, [False, "Open failures: none", "On shift now: nobody", "Finished in the last 24 hours: none"])

    def test_an_old_open_failure_is_listed_in_the_count_but_does_not_force_the_section_open_on_the_staff_page(self):
        out = page_eval([answer(view([sfail(active=False)])), "[$('sec-failures').open, $('sum-failures').textContent]"])[1]
        self.assertEqual(out, [False, "Open failures: 1"])

    def test_on_his_panel_every_open_failure_is_urgent_and_the_summaries_follow_the_data(self):
        roster = [{"username": "ada", "display": "Ada", "since": "x", "long": False}]
        done = [{"fid": "f9", "display": "SITE-A", "done_by": "ada", "done_at": "2026-11-08T11:00:00+00:00", "note": "restarted"}]
        out = panel_eval(["renderStaff(" + json.dumps(panel_view([panel_failure()], roster=roster, done=done)) + ", true), 0",
                          "[$('sec-failures').open, $('sum-failures').textContent, $('sum-roster').textContent, $('sum-people').textContent, $('sum-done').textContent]",
                          "(function () { $('sec-failures').open = false; renderStaff(" + json.dumps(panel_view([panel_failure()], roster=roster, done=done)) + ", true); return $('sec-failures').open; })()",
                          "(function () { renderStaff(" + json.dumps(panel_view([panel_failure(), panel_failure(fid='f2', display='SITE-A')], roster=roster, done=done)) + ", true); return $('sec-failures').open; })()"])
        self.assertEqual(out[1], [True, "Open failures: 1", "On shift now: Ada", "People: 2 active, 1 on shift", "Finished in the last 24 hours: 1"])
        self.assertFalse(out[2])
        self.assertTrue(out[3])


if __name__ == "__main__":
    unittest.main(verbosity=1)
