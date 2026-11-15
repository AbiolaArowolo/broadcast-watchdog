# File:         test_control_login.py
# Purpose:      Tests of core-listener/app/control_login.py: how the super user's password is stored and checked, which passwords are refused, and how wrong tries are locked out.
# Inputs:       None (made-up passwords, fixed times; a tiny scrypt cost so the tests run fast).
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/control_login.py (pure logic: standard library only, no clock of its own).
"""Run: python ops/test_control_login.py

What is proven here, in plain words: a password is stored only as a salted scrypt hash in a form that is safe to put in a settings file; the right password verifies and every other
input (wrong, empty, odd, a damaged stored value, a hash set to cost the server a fortune) is a plain "no" that never raises; a weak password is refused with a reason that never
repeats it; five wrong tries from one address lock that address, thirty from anywhere lock everyone, a good login clears the address, and old failures stop counting.
"""
import importlib
import re
import sys
import unittest
from pathlib import Path

LISTENER_DIR = Path(__file__).resolve().parent.parent / "core-listener"
sys.path.insert(0, str(LISTENER_DIR))
login = importlib.import_module("app.control_login")

FAST = 2 ** 4            # a tiny cost for the tests; the real default is login.DEFAULT_N
PASSWORD = "correct horse battery staple 42"


def fast_hash(password=PASSWORD, salt=None):
    return login.hash_password(password, salt=salt, n=FAST)


class HashTests(unittest.TestCase):
    def test_the_stored_form_is_plain_safe_text_with_six_parts(self):
        stored = fast_hash()
        self.assertRegex(stored, r"^[A-Za-z0-9:_-]+$")        # no dollar sign (compose would read it as a variable), no equals sign, no space, no quote
        parts = stored.split(":")
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0], "scrypt")
        self.assertEqual(parts[1:4], [str(FAST), "8", "1"])

    def test_the_password_is_not_in_the_stored_form(self):
        self.assertNotIn(PASSWORD, fast_hash())
        self.assertNotIn("horse", fast_hash())

    def test_two_hashes_of_the_same_password_differ_because_of_the_salt(self):
        self.assertNotEqual(fast_hash(), fast_hash())

    def test_the_same_salt_gives_the_same_hash(self):
        self.assertEqual(fast_hash(salt=b"0123456789abcdef"), fast_hash(salt=b"0123456789abcdef"))

    def test_the_default_cost_is_real_but_bounded(self):
        self.assertGreaterEqual(login.DEFAULT_N, 2 ** 14)
        self.assertLessEqual(login.DEFAULT_N, login.MAX_N)


class VerifyTests(unittest.TestCase):
    def test_the_right_password_verifies(self):
        self.assertTrue(login.verify_password(fast_hash(), PASSWORD))

    def test_anything_else_does_not(self):
        stored = fast_hash()
        for wrong in ("", " ", PASSWORD + " ", " " + PASSWORD, PASSWORD.upper(), PASSWORD[:-1], "x" * 400, None, 5, b"bytes", ["a"]):
            self.assertFalse(login.verify_password(stored, wrong), repr(wrong))

    def test_a_unicode_password_round_trips(self):
        stored = fast_hash("pässwörd-éè-12345")
        self.assertTrue(login.verify_password(stored, "pässwörd-éè-12345"))
        self.assertFalse(login.verify_password(stored, "passwörd-éè-12345"))

    def test_a_damaged_or_unknown_stored_value_is_a_no_and_never_raises(self):
        for stored in ("", None, 5, "x", "scrypt:1:2", "scrypt:16:8:1:AAAA", "md5:16:8:1:AAAA:BBBB", "scrypt:x:8:1:AAAA:BBBB", "scrypt:16:8:1:!!!!:????",
                       "scrypt:15:8:1:AAAAAAAAAAAAAAAA:BBBB", "scrypt:0:8:1:AAAAAAAAAAAAAAAA:" + "B" * 43, "scrypt:16:0:1:AAAAAAAAAAAAAAAA:" + "B" * 43):
            self.assertFalse(login.verify_password(stored, PASSWORD), repr(stored))

    def test_a_hash_that_would_cost_the_server_a_fortune_is_refused_without_computing_it(self):
        real, calls = login.hashlib.scrypt, []
        login.hashlib.scrypt = lambda *a, **k: calls.append(1) or real(*a, **k)
        try:
            costly = f"scrypt:{login.MAX_N * 2}:8:1:" + "A" * 22 + ":" + "B" * 43
            self.assertFalse(login.verify_password(costly, PASSWORD))
            huge_r = f"scrypt:16:{login.MAX_R * 2}:1:" + "A" * 22 + ":" + "B" * 43
            self.assertFalse(login.verify_password(huge_r, PASSWORD))
            not_a_power = "scrypt:24:8:1:" + "A" * 22 + ":" + "B" * 43
            self.assertFalse(login.verify_password(not_a_power, PASSWORD))
        finally:
            login.hashlib.scrypt = real
        self.assertEqual(calls, [], "the expensive hash was never even started")

    def test_a_one_character_change_in_the_stored_hash_fails(self):
        # A character in the MIDDLE of the hash (the last base64 character carries two padding bits, so changing only that one can leave the bytes unchanged).
        stored = fast_hash()
        at = len(stored) - 10
        changed = stored[:at] + ("A" if stored[at] != "A" else "B") + stored[at + 1:]
        self.assertNotEqual(changed, stored)
        self.assertFalse(login.verify_password(changed, PASSWORD))


class PasswordRuleTests(unittest.TestCase):
    def test_a_good_passphrase_is_accepted(self):
        self.assertIsNone(login.password_problem(PASSWORD, "owner@mail.example"))

    def test_weak_ones_are_refused_with_a_reason_that_never_repeats_them(self):
        weak = ["short", "a" * 20, "11111111111111", "x" * 300, "", None, 5, "has owner inside 123456"]
        for password in weak:
            problem = login.password_problem(password, "owner@mail.example")
            self.assertIsInstance(problem, str, repr(password))
            if isinstance(password, str) and password:
                self.assertNotIn(password, problem)

    def test_the_minimum_length_is_exactly_eight(self):
        # The super user chose eight as the shortest he will accept (2026-10-26); the lockout and the slow hash are what make that safe enough.
        self.assertEqual(login.MIN_PASSWORD_CHARS, 8)
        self.assertIsNotNone(login.password_problem("abcdefg", ""))
        self.assertIsNone(login.password_problem("abcdefgh", ""))

    def test_a_short_password_is_still_refused_when_it_is_too_simple_or_holds_the_name(self):
        self.assertIsNotNone(login.password_problem("aaaabbbb", ""))                  # eight long but only two different characters
        self.assertIsNotNone(login.password_problem("owner1", "owner@mail.example"))
        self.assertIsNone(login.password_problem("Tr4in-Ok", "owner@mail.example"))

    def test_the_refusal_names_the_minimum_without_repeating_the_password(self):
        problem = login.password_problem("abcdefg", "")
        self.assertIn("8", problem)
        self.assertNotIn("abcdefg", problem)

    def test_the_maximum_length_is_exactly_the_hashing_limit(self):
        longest = ("abcdefghij" * (login.MAX_PASSWORD_CHARS // 10))[:login.MAX_PASSWORD_CHARS]
        self.assertEqual(len(longest), login.MAX_PASSWORD_CHARS)
        self.assertIsNone(login.password_problem(longest, ""))
        self.assertIsNotNone(login.password_problem(longest + "k", ""))


class CheckLoginTests(unittest.TestCase):
    USER = "owner@mail.example"

    def setUp(self):
        self.stored = fast_hash()
        self.calls = 0
        self.real = login.verify_password

        def counting(stored, password):
            self.calls += 1
            return self.real(stored, password)
        login.verify_password = counting

    def tearDown(self):
        login.verify_password = self.real

    def test_the_right_name_and_password_log_in(self):
        self.assertTrue(login.check_login(self.USER, self.stored, self.USER, PASSWORD))

    def test_the_name_ignores_case_and_surrounding_spaces(self):
        self.assertTrue(login.check_login(self.USER, self.stored, "  owner@mail.example ", PASSWORD))

    def test_the_name_ignores_case_on_both_sides(self):
        self.assertTrue(login.check_login("owner@mail.example", self.stored, "owner@mail.example", PASSWORD))
        self.assertTrue(login.check_login("owner@mail.example", self.stored, "owner@mail.example", PASSWORD))

    def test_a_wrong_name_or_a_wrong_password_does_not(self):
        self.assertFalse(login.check_login(self.USER, self.stored, "someone@else.test", PASSWORD))
        self.assertFalse(login.check_login(self.USER, self.stored, self.USER, "wrong password here 1"))
        self.assertFalse(login.check_login(self.USER, self.stored, None, None))

    def test_the_password_is_checked_even_when_the_name_is_wrong(self):
        # So a wrong name and a wrong password take the same time: nothing tells an outsider which of the two was right.
        login.check_login(self.USER, self.stored, "someone@else.test", PASSWORD)
        self.assertEqual(self.calls, 1)

    def test_nothing_set_up_means_nobody_gets_in(self):
        for user, stored in ((self.USER, ""), ("", self.stored), ("", ""), (None, None)):
            self.assertFalse(login.check_login(user, stored, self.USER, PASSWORD))
        self.assertEqual(self.calls, 0)


class ThrottleTests(unittest.TestCase):
    def test_five_wrong_tries_lock_that_address_for_fifteen_minutes(self):
        throttle = login.LoginThrottle()
        for second in range(4):
            throttle.record_failure("a", 100 + second)
            self.assertEqual(throttle.locked_for("a", 100 + second), 0)
        throttle.record_failure("a", 104)
        self.assertEqual(throttle.locked_for("a", 104), 900)
        self.assertEqual(throttle.locked_for("a", 104 + 899), 1)
        self.assertEqual(throttle.locked_for("a", 104 + 900), 0)

    def test_another_address_is_not_affected(self):
        throttle = login.LoginThrottle()
        for second in range(5):
            throttle.record_failure("a", 100 + second)
        self.assertEqual(throttle.locked_for("b", 105), 0)

    def test_a_good_login_clears_that_addresses_failures(self):
        throttle = login.LoginThrottle()
        for second in range(4):
            throttle.record_failure("a", 100 + second)
        throttle.record_success("a")
        throttle.record_failure("a", 110)
        self.assertEqual(throttle.locked_for("a", 110), 0)

    def test_old_failures_stop_counting(self):
        throttle = login.LoginThrottle()
        for second in range(4):
            throttle.record_failure("a", 100 + second)
        throttle.record_failure("a", 100 + 901)               # the first four are older than the 15 minute window
        self.assertEqual(throttle.locked_for("a", 100 + 901), 0)

    def test_after_the_lock_ends_the_address_starts_fresh(self):
        throttle = login.LoginThrottle()
        for second in range(5):
            throttle.record_failure("a", 100 + second)
        later = 104 + 901
        self.assertEqual(throttle.locked_for("a", later), 0)
        throttle.record_failure("a", later)
        self.assertEqual(throttle.locked_for("a", later), 0)

    def test_thirty_wrong_tries_from_many_addresses_lock_everyone(self):
        throttle = login.LoginThrottle()
        for index in range(30):
            throttle.record_failure(f"visitor-{index}", 100 + index)       # one try each: no single address is locked
        self.assertGreater(throttle.locked_for("a-brand-new-visitor", 130), 0)
        self.assertEqual(throttle.locked_for("a-brand-new-visitor", 129 + 899), 1)
        self.assertEqual(throttle.locked_for("a-brand-new-visitor", 129 + 900), 0)

    def test_twenty_nine_are_not_enough_to_lock_everyone(self):
        throttle = login.LoginThrottle()
        for index in range(29):
            throttle.record_failure(f"visitor-{index}", 100 + index)
        self.assertEqual(throttle.locked_for("new", 130), 0)

    def test_memory_stays_bounded_under_a_flood_of_addresses(self):
        throttle = login.LoginThrottle(max_visitors=50)
        for index in range(500):
            throttle.record_failure(f"v{index}", 100 + index * 0.001)
        self.assertLessEqual(throttle.tracked(), 50)

    def test_the_wait_is_never_less_than_one_second_while_locked(self):
        throttle = login.LoginThrottle()
        for second in range(5):
            throttle.record_failure("a", 100 + second)
        self.assertGreaterEqual(throttle.locked_for("a", 104 + 899.7), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
