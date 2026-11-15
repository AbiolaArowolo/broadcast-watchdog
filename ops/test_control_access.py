# File:         test_control_access.py
# Purpose:      Tests of core-listener/app/control_access.py: the control key, the short-lived browser session, which requests the OFF gate lets through, and the page users see.
# Inputs:       None (made-up keys, tokens and a fixed clock).
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/control_access.py (pure logic: no network, no clock of its own).
"""Run: python ops/test_control_access.py

What is proven here, in plain words: the control key is checked safely and an unset key opens nothing; a browser session is signed, expires, dies when the key is
rotated and can be neither forged nor extended; while access is OFF a user gets the branded page or a 503 for everything except what the system itself needs
(ingest, health, the control door, harmless icons), and an unknown path is closed, not open; the page is one self-contained file with no outside resources.
"""
import importlib
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

LISTENER_DIR = Path(__file__).resolve().parent.parent / "core-listener"
sys.path.insert(0, str(LISTENER_DIR))
access = importlib.import_module("app.control_access")

T0 = datetime(2026, 10, 25, 14, 30, 0, tzinfo=timezone.utc)
KEY = "k" * 40
OTHER = "o" * 40


class KeyTests(unittest.TestCase):
    def test_the_right_key_matches(self):
        self.assertTrue(access.key_matches(KEY, KEY))

    def test_a_wrong_key_does_not(self):
        for offered in (OTHER, KEY + "x", KEY[:-1], KEY.upper(), " " + KEY, ""):
            self.assertFalse(access.key_matches(KEY, offered), offered)

    def test_nothing_offered_never_matches(self):
        for offered in (None, 5, b"x", ["k"]):
            self.assertFalse(access.key_matches(KEY, offered))

    def test_an_unset_key_opens_nothing_not_even_an_empty_offer(self):
        for configured in ("", None, "   "):
            for offered in ("", None, "anything", "   "):
                self.assertFalse(access.key_matches(configured, offered))

    def test_a_non_ascii_offer_is_a_miss_not_a_crash(self):
        self.assertFalse(access.key_matches(KEY, "ké" * 20))


class SessionGenerationTests(unittest.TestCase):
    """A session is signed under a generation the server can change: that is how a log out or a new password ends every open session at once."""
    G1, G2 = "a" * 32, "b" * 32

    def test_a_session_verifies_only_under_the_generation_it_was_signed_with(self):
        token = access.issue_session(KEY, T0, 3600, self.G1)
        self.assertTrue(access.verify_session(KEY, token, T0, self.G1))
        self.assertFalse(access.verify_session(KEY, token, T0, self.G2))
        self.assertFalse(access.verify_session(KEY, token, T0))                       # the default generation is a different one

    def test_the_default_generation_still_works_as_before(self):
        token = access.issue_session(KEY, T0, 3600)
        self.assertTrue(access.verify_session(KEY, token, T0))
        self.assertFalse(access.verify_session(KEY, token, T0, self.G1))

    def test_the_generation_is_not_a_part_of_the_token_so_it_cannot_be_read_or_forged_from_it(self):
        self.assertNotIn(self.G1, access.issue_session(KEY, T0, 3600, self.G1))

    def test_a_generation_that_is_not_lowercase_hex_is_refused_and_never_raises_when_verifying(self):
        for bad in ("not hex!", "A" * 8, "a" * 65, None, 5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                access.issue_session(KEY, T0, 3600, bad)
            self.assertFalse(access.verify_session(KEY, access.issue_session(KEY, T0, 3600), T0, bad))


class KeyStrengthTests(unittest.TestCase):
    def test_unset_is_fine_the_feature_is_simply_off(self):
        self.assertIsNone(access.control_key_problem("", ()))

    def test_a_short_key_is_a_problem(self):
        self.assertIn("at least", access.control_key_problem("short", ()))

    def test_a_key_that_equals_another_secret_is_a_problem(self):
        self.assertIn("different", access.control_key_problem(KEY, (OTHER, KEY)))

    def test_a_good_distinct_key_is_fine(self):
        self.assertIsNone(access.control_key_problem(KEY, (OTHER, "", None)))

    def test_the_problem_text_never_contains_the_key(self):
        for key, others in (("short", ()), (KEY, (KEY,))):
            self.assertNotIn(key, access.control_key_problem(key, others))


class SessionTests(unittest.TestCase):
    def test_a_fresh_session_verifies(self):
        token = access.issue_session(KEY, T0, 3600)
        self.assertTrue(access.verify_session(KEY, token, T0))
        self.assertTrue(access.verify_session(KEY, token, T0 + timedelta(seconds=3599)))

    def test_it_expires_exactly_at_the_end(self):
        token = access.issue_session(KEY, T0, 3600)
        self.assertFalse(access.verify_session(KEY, token, T0 + timedelta(seconds=3600)))
        self.assertFalse(access.verify_session(KEY, token, T0 + timedelta(days=2)))

    def test_rotating_the_key_kills_every_session(self):
        token = access.issue_session(KEY, T0, 3600)
        self.assertFalse(access.verify_session(OTHER, token, T0))

    def test_a_changed_expiry_is_refused(self):
        version, expiry, mac = access.issue_session(KEY, T0, 3600).split(".")
        later = str(int(expiry) + 86400)
        self.assertFalse(access.verify_session(KEY, ".".join((version, later, mac)), T0))

    def test_a_changed_signature_is_refused(self):
        version, expiry, mac = access.issue_session(KEY, T0, 3600).split(".")
        flipped = ("0" if mac[0] != "0" else "1") + mac[1:]
        self.assertFalse(access.verify_session(KEY, ".".join((version, expiry, flipped)), T0))

    def test_garbage_is_false_and_never_raises(self):
        for token in ("", "x", "v1..", "v1.abc.def", "v1.99999999999.zz", "v2.1.2", "a.b.c.d", None, 5, b"v1.1.2", "v1.-5.00"):
            self.assertFalse(access.verify_session(KEY, token, T0), repr(token))

    def test_an_unset_key_verifies_nothing(self):
        token = access.issue_session(KEY, T0, 3600)
        for empty in ("", None):
            self.assertFalse(access.verify_session(empty, token, T0))

    def test_an_unset_key_cannot_issue(self):
        with self.assertRaises(ValueError):
            access.issue_session("", T0, 3600)

    def test_the_lifetime_has_bounds(self):
        for ttl in (0, 59, 86401, -1):
            with self.assertRaises(ValueError):
                access.issue_session(KEY, T0, ttl)

    def test_the_token_is_cookie_safe_and_does_not_contain_the_key(self):
        token = access.issue_session(KEY, T0, 3600)
        self.assertRegex(token, r"^[A-Za-z0-9._-]+$")
        self.assertNotIn(KEY, token)

    def test_two_sessions_issued_at_different_times_differ(self):
        self.assertNotEqual(access.issue_session(KEY, T0, 3600), access.issue_session(KEY, T0 + timedelta(seconds=1), 3600))

    def test_a_naive_time_is_refused(self):
        with self.assertRaises(ValueError):
            access.issue_session(KEY, datetime(2026, 10, 25), 3600)
        self.assertFalse(access.verify_session(KEY, access.issue_session(KEY, T0, 3600), datetime(2026, 10, 25)))


class OfficePassTests(unittest.TestCase):
    """The office PC's door pass (his choice, 2026-11-10): a signed, short-lived token the standby hands to a browser that showed a listed certificate at the staff door, so the same PC
    is recognised on the watchdog address. It proves that and nothing else, and it can never be taken for a login session."""

    def test_a_fresh_pass_verifies_until_the_moment_it_ends(self):
        token = access.issue_pass(KEY, T0, 3600)
        self.assertTrue(access.verify_pass(KEY, token, T0))
        self.assertTrue(access.verify_pass(KEY, token, T0 + timedelta(seconds=3599)))
        self.assertFalse(access.verify_pass(KEY, token, T0 + timedelta(seconds=3600)))

    def test_a_pass_lasts_between_a_minute_and_a_week(self):
        for seconds in (59, 604801, 0, -5):
            with self.assertRaises(ValueError):
                access.issue_pass(KEY, T0, seconds)
        for seconds in (60, 604800, 259200):
            self.assertTrue(access.verify_pass(KEY, access.issue_pass(KEY, T0, seconds), T0))

    def test_another_key_cannot_verify_it_and_it_cannot_be_forged_or_extended(self):
        token = access.issue_pass(KEY, T0, 3600)
        self.assertFalse(access.verify_pass(OTHER, token, T0))
        version, expiry, mac = token.split(".")
        self.assertFalse(access.verify_pass(KEY, f"{version}.{int(expiry) + 86400}.{mac}", T0))          # a later expiry with the old signature
        self.assertFalse(access.verify_pass(KEY, f"{version}.{expiry}.{'0' * 64}", T0))
        self.assertFalse(access.verify_pass(KEY, f"{version}.{expiry}.{mac.upper()}", T0))

    def test_a_login_session_is_not_a_pass_and_a_pass_is_not_a_login_session(self):
        session = access.issue_session(KEY, T0, 3600, "")
        self.assertFalse(access.verify_pass(KEY, session, T0))
        self.assertFalse(access.verify_session(KEY, access.issue_pass(KEY, T0, 3600), T0))
        self.assertFalse(access.verify_session(KEY, access.issue_pass(KEY, T0, 3600), T0, "ab"))

    def test_the_pass_carries_no_name_and_no_secret(self):
        token = access.issue_pass(KEY, T0, 3600)
        self.assertNotIn(KEY, token)
        self.assertRegex(token, r"^p1\.[0-9]{10}\.[0-9a-f]{64}$")

    def test_rubbish_never_raises_and_never_verifies(self):
        for junk in (None, 5, b"x", ["p1"], "", "p1", "p1.1.2", "p1..", "x" * 500, "v1." + "9" * 12 + "." + "a" * 64, "p1." + "9" * 13 + "." + "a" * 64):
            with self.subTest(junk=junk):
                self.assertFalse(access.verify_pass(KEY, junk, T0))
        token = access.issue_pass(KEY, T0, 3600)
        self.assertFalse(access.verify_pass("", token, T0))
        self.assertFalse(access.verify_pass(None, token, T0))
        self.assertFalse(access.verify_pass(KEY, token, datetime(2026, 10, 25)))                  # a clock without a time zone is refused, not guessed
        with self.assertRaises(ValueError):
            access.issue_pass("", T0, 3600)
        with self.assertRaises(ValueError):
            access.issue_pass(KEY, datetime(2026, 10, 25), 3600)


class GateTests(unittest.TestCase):
    ALWAYS_OPEN =[("POST", "/heartbeat"), ("POST", "/event"), ("GET", "/health"), ("POST", "/alerts/reset"), ("GET", "/control"), ("POST", "/control"),
                   ("POST", "/control/session"), ("GET", "/control/login"), ("GET", "/robots.txt"), ("GET", "/manifest.json"), ("GET", "/sw.js"),
                   ("GET", "/offline.html"), ("GET", "/favicon.ico"), ("GET", "/icon-192.png"), ("GET", "/icon-512.png"), ("GET", "/icon-512-maskable.png"),
                   ("GET", "/apple-touch-icon.png")]

    def test_while_access_is_on_everything_is_allowed(self):
        for method, path in self.ALWAYS_OPEN + [("GET", "/"), ("GET", "/status"), ("GET", "/stream"), ("GET", "/topology"), ("GET", "/whatever")]:
            self.assertEqual(access.gate_request(method, path, access_on=True, is_super=False), access.ALLOW)

    def test_the_super_user_is_allowed_everything_while_off(self):
        for method, path in [("GET", "/"), ("GET", "/status"), ("GET", "/stream"), ("GET", "/topology"), ("GET", "/anything")]:
            self.assertEqual(access.gate_request(method, path, access_on=False, is_super=True), access.ALLOW)

    def test_while_off_the_system_doors_stay_open(self):
        for method, path in self.ALWAYS_OPEN:
            self.assertEqual(access.gate_request(method, path, access_on=False, is_super=False), access.ALLOW, path)

    def test_while_off_the_two_pages_show_the_branded_page(self):
        for path in ("/", "/topology"):
            self.assertEqual(access.gate_request("GET", path, access_on=False, is_super=False), access.PAGE)

    def test_while_off_data_is_closed(self):
        for path in ("/status", "/stream"):
            self.assertEqual(access.gate_request("GET", path, access_on=False, is_super=False), access.DATA)

    def test_while_off_an_unknown_or_look_alike_path_is_closed_not_open(self):
        for path in ("/whatever", "/status/", "/STATUS", "/health/x", "/healthz", "//status", "/controls", "/control2", "/heartbeat/x", "/icon-192.png.js", ""):
            self.assertEqual(access.gate_request("GET", path, access_on=False, is_super=False), access.DATA, path)

    def test_the_page_decision_applies_only_to_a_page_get(self):
        self.assertEqual(access.gate_request("POST", "/", access_on=False, is_super=False), access.DATA)

    def test_a_query_string_cannot_change_the_decision(self):
        self.assertEqual(access.gate_request("GET", "/status?x=/health", access_on=False, is_super=False), access.DATA)


class PageTests(unittest.TestCase):
    PAGE = access.unavailable_page()

    def test_it_says_what_was_agreed(self):
        for text in ("Service temporarily unavailable", "Access to this monitoring service is switched off for now.",
                     "To find out more, or to have access restored, please contact ClarixTech.", "Contact ClarixTech", "support@corp.example"):
            self.assertIn(text, self.PAGE)

    def test_the_contact_address_is_clarixtechs_and_never_the_old_one(self):
        self.assertEqual(access.SUPPORT_EMAIL, "support@corp.example")
        self.assertIn('href="mailto:support@corp.example"', self.PAGE)
        self.assertNotIn("clientmedia", self.PAGE)

    def test_the_button_goes_to_clarixtech_and_nowhere_else_leaves_the_page(self):
        self.assertIn('href="https://corp.example"', self.PAGE)
        urls = set(re.findall(r"https?://[^\s\"'<>)]+", self.PAGE))
        self.assertEqual(urls, {"https://corp.example"})

    def test_it_offers_staff_a_way_in(self):
        self.assertIn('<a class="staff" href="/control/login">Staff sign in</a>', self.PAGE)

    def test_it_is_one_self_contained_file_with_one_small_script_that_asks_only_this_site(self):
        self.assertEqual(self.PAGE.lower().count("<script"), 1)
        script = self.PAGE.split("<script>")[1].split("</script>")[0]
        self.assertNotIn("http", script)
        self.assertIn('fetch("/health"', script)
        self.assertNotIn("@import", self.PAGE)
        self.assertNotIn("<link", self.PAGE.lower())
        self.assertNotIn("<img", self.PAGE.lower())

    def test_it_comes_back_to_the_dashboard_by_itself_when_access_is_on_again(self):
        script = self.PAGE.split("<script>")[1].split("</script>")[0]
        self.assertIn("setInterval", script)
        self.assertIn('access === "on"', script)
        self.assertEqual(script.count("location.reload()"), 1)
        self.assertIn('sessionStorage.getItem("wd-resume")', script)        # it never reloads more than once in 20 s, so an odd answer cannot make it loop
        self.assertIn("Date.now() - last < 20000", script)
        self.assertIn('cache: "no-store"', script)
        self.assertIn(".catch(function () {})", script)                 # a failed check never reloads and never shows an error

    def test_it_is_a_proper_accessible_page(self):
        for text in ("<!doctype html>", '<html lang="en">', 'name="viewport"', "<title>", "<main", "<h1", ":focus-visible", "prefers-reduced-motion"):
            self.assertIn(text, self.PAGE)

    def test_it_asks_not_to_be_indexed(self):
        self.assertIn("noindex", self.PAGE)

    def test_it_holds_no_dash_no_section_sign_no_secret(self):
        self.assertNotIn(chr(0x2014), self.PAGE)
        self.assertNotIn(chr(0xA7), self.PAGE)
        self.assertTrue(self.PAGE.isascii())

    def test_it_is_small(self):
        self.assertLess(len(self.PAGE.encode("utf-8")), 12000)

    def test_the_data_answer_is_a_short_plain_message(self):
        self.assertEqual(access.unavailable_data(), {"error": "service temporarily unavailable", "detail": "Access is switched off. Contact ClarixTech."})


class OffScreenTests(unittest.TestCase):
    """The same message as a layer that a dashboard which is already open puts over itself the moment access is switched OFF: no refresh, and the page (with the
    browser's permission to play the siren) stays alive underneath."""
    HTML = access.off_overlay_html()

    def test_it_starts_hidden_and_keeps_hidden_hidden(self):
        self.assertIn('id="off-screen"', self.HTML)
        self.assertRegex(self.HTML, r'<div id="off-screen"[^>]* hidden')
        self.assertIn("#off-screen[hidden]", self.HTML)                    # a display rule must never override the hidden attribute

    def test_it_says_the_same_things_as_the_page(self):
        for text in ("Service temporarily unavailable", "Access to this monitoring service is switched off for now.",
                     "To find out more, or to have access restored, please contact ClarixTech.", "Contact ClarixTech", "support@corp.example", "Staff sign in"):
            self.assertIn(text, self.HTML)

    def test_it_covers_the_page_and_sits_above_the_banner(self):
        self.assertIn("position:fixed", self.HTML)
        self.assertIn("inset:0", self.HTML)
        self.assertIn("z-index:100000", self.HTML)                          # the OFF banner is 99999

    def test_it_has_no_script_and_nothing_outside(self):
        self.assertNotIn("<script", self.HTML.lower())
        self.assertEqual(set(re.findall(r"https?://[^\s\"'<>)]+", self.HTML)), {"https://corp.example"})
        self.assertNotIn("clientmedia", self.HTML)

    def test_it_is_announced_politely_and_plain_ascii_without_dashes(self):
        self.assertIn('role="status"', self.HTML)
        self.assertTrue(self.HTML.isascii())
        self.assertNotIn(chr(0x2014), self.HTML)
        self.assertNotIn(chr(0xA7), self.HTML)


class PanelLivePollTests(unittest.TestCase):
    def test_the_panel_asks_for_both_nodes_every_four_seconds_so_a_change_from_anywhere_shows_without_a_refresh(self):
        page = access.panel_page()
        self.assertIn("setInterval(load, 4000);", page)
        self.assertNotIn("setInterval(load, 10000);", page)


class LoginPageTests(unittest.TestCase):
    PAGE = access.login_page()

    def test_it_trades_the_key_for_a_session_and_nothing_else(self):
        self.assertIn('"/control/session"', self.PAGE)
        self.assertIn('type="password"', self.PAGE)
        self.assertIn('<label for="key">', self.PAGE)
        self.assertIn('aria-live="polite"', self.PAGE)

    def test_it_has_no_form_and_no_outside_resource(self):
        self.assertNotIn("<form", self.PAGE.lower())           # the security policy forbids form posts
        self.assertNotIn("http://", self.PAGE)
        self.assertNotIn("https://", self.PAGE)
        self.assertNotIn("<link", self.PAGE.lower())

    def test_the_key_never_goes_into_the_address(self):
        self.assertNotIn("location.search", self.PAGE)
        self.assertNotIn("?key=", self.PAGE)

    def test_it_is_small_plain_ascii_without_dashes(self):
        self.assertTrue(self.PAGE.isascii())
        self.assertNotIn(chr(0x2014), self.PAGE)
        self.assertLess(len(self.PAGE.encode("utf-8")), 6000)


class PasswordLoginPageTests(unittest.TestCase):
    PAGE = access.login_page_password()

    def test_it_asks_for_a_name_and_a_password_and_nothing_else(self):
        self.assertIn('autocomplete="username"', self.PAGE)
        self.assertIn('autocomplete="current-password"', self.PAGE)
        self.assertIn('type="password"', self.PAGE)
        self.assertEqual(self.PAGE.count("<input"), 2)
        self.assertNotIn("control key", self.PAGE.lower())

    def test_it_trades_them_for_a_session_and_opens_the_panel(self):
        self.assertIn('"/control/session"', self.PAGE)
        self.assertIn("user:", self.PAGE)
        self.assertIn("password:", self.PAGE)
        self.assertIn('"/control/panel"', self.PAGE)

    def test_it_says_so_plainly_when_the_try_was_refused_or_the_login_is_locked(self):
        self.assertIn("not accepted", self.PAGE)
        self.assertIn("429", self.PAGE)
        self.assertIn('aria-live="polite"', self.PAGE)

    def test_it_is_self_contained_small_ascii_and_accessible(self):
        self.assertNotIn("<form", self.PAGE.lower())
        self.assertNotIn("http://", self.PAGE)
        self.assertNotIn("https://", self.PAGE)
        self.assertTrue(self.PAGE.isascii())
        self.assertNotIn(chr(0x2014), self.PAGE)
        self.assertLess(len(self.PAGE.encode("utf-8")), 8000)
        for text in ("<!doctype html>", '<html lang="en">', 'name="viewport"', "<label", ":focus-visible", "noindex"):
            self.assertIn(text, self.PAGE)


class PanelPageTests(unittest.TestCase):
    PAGE = access.panel_page()

    def test_the_four_buttons_and_their_wording(self):
        for text in ("Switch access OFF", "Switch access ON", "Use Core", "Use the VPS", "Log out"):
            self.assertIn(text, self.PAGE)

    def test_switching_off_asks_first(self):
        self.assertIn("confirm(", self.PAGE)
        self.assertIn("everyone except you", self.PAGE)

    def test_it_talks_only_to_its_own_site_and_the_four_known_paths(self):
        for path in ('"/control/panel/state"', '"/control/panel/act"', '"/control/logout"', '"/control/login"'):
            self.assertIn(path, self.PAGE)
        self.assertNotIn("http://", self.PAGE)
        self.assertNotIn("https://", self.PAGE)
        self.assertNotIn("<form", self.PAGE.lower())
        self.assertNotIn("localStorage", self.PAGE)           # nothing is kept in the browser: the session cookie is HttpOnly and the page holds no secret

    def test_it_sends_the_four_actions_the_server_understands(self):
        for action in ("off", "on", "use_core", "use_vps"):
            self.assertIn(f'"{action}"', self.PAGE)

    def test_it_shows_each_node_and_the_studios_side_by_side(self):
        for text in ("Access", "Preferred", "Alerting now", "heard", "studios"):
            self.assertIn(text, self.PAGE)

    def test_a_refused_session_sends_the_person_back_to_the_login(self):
        self.assertIn("401", self.PAGE)

    def test_it_is_self_contained_small_ascii_and_accessible(self):
        self.assertTrue(self.PAGE.isascii())
        self.assertNotIn(chr(0x2014), self.PAGE)
        self.assertLess(len(self.PAGE.encode("utf-8")), 40000)       # was 16000 before the Staff section (about 3 KB of markup and 8 KB of script), 24000 before the AV matrix card (about 8 KB) was added
        for text in ("<!doctype html>", '<html lang="en">', 'name="viewport"', "<main", "<h1", ":focus-visible", 'aria-live="polite"', "prefers-reduced-motion", "noindex"):
            self.assertIn(text, self.PAGE)

    def test_there_is_a_way_back_to_the_dashboard(self):
        self.assertIn('<a href="/">Back to the dashboard</a>', self.PAGE)

    def test_every_button_is_a_real_button_with_a_big_touch_target(self):
        self.assertEqual(self.PAGE.count("<button"), 8)       # the five old buttons, plus Add person and Reset password, plus the AV matrix card's Refresh (the rest builds its buttons in script)
        self.assertIn("min-height:48px", self.PAGE.replace(" ", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
