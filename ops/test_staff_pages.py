# File:         test_staff_pages.py
# Purpose:      Tests of the staff section's pages as text: the Staff page, the Staff section of the control panel, the shared sign-in page, and the dashboard's "on shift" chip.
# Inputs:       None (the pages are strings made by core-listener/app/staff_pages.py, control_access.py and dashboard.py).
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: Python standard library and the three page modules. A real-browser check of the same pages is ops/browser_tests/staff_pages.mjs.
"""Run: python ops/test_staff_pages.py

What is proven here, in plain words: every page is one self-contained document (nothing fetched from outside, no inline handlers); a name or a note typed by a person is only ever put into
a page as text (never as markup), so it cannot inject anything; every field has a label and every message is announced to a screen reader; the Staff page can reach only the staff doors and
never a super-user action; the control panel keeps its four buttons and gains the Staff section; the sign-in page sends staff to their page and the super user to his panel without ever
following an address the server sent; and the dashboard shows people on shift as plain text and copes with a frame that has no staff part.
"""
import importlib
import re
import sys
import unittest
from pathlib import Path

LISTENER_DIR = Path(__file__).resolve().parent.parent / "core-listener"
sys.path.insert(0, str(LISTENER_DIR))
control_access = importlib.import_module("app.control_access")
staff_pages = importlib.import_module("app.staff_pages")
dashboard = importlib.import_module("app.dashboard")

STAFF = staff_pages.staff_page()
PANEL = control_access.panel_page()
LOGIN = control_access.login_page_password()
DASH = dashboard.render_dashboard_html("view-token-for-tests")
# Patterns the pages must NOT contain. They are only searched for as text here; nothing in this file ever runs them.
FORBIDDEN_SCRIPT = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "javascript:")


def function_body(page, name):
    start = page.index(f"function {name}(")
    depth, index = 0, page.index("{", start)
    for position in range(index, len(page)):
        depth += page[position] == "{"
        depth -= page[position] == "}"
        if depth == 0:
            return page[start:position + 1]
    raise AssertionError(name)


class EveryPageTests(unittest.TestCase):
    PAGES = {"staff": STAFF, "panel": PANEL, "login": LOGIN}

    def test_each_page_is_self_contained(self):
        for name, page in self.PAGES.items():
            self.assertNotRegex(page, r"https?://", name)
            self.assertNotRegex(page, r"<script[^>]*\bsrc=", name)
            self.assertNotRegex(page, r"<link\b", name)
            self.assertNotIn("@import", page, name)

    def test_no_inline_handlers_and_nothing_that_turns_text_into_markup(self):
        for name, page in self.PAGES.items():
            self.assertNotRegex(page, r"\son[a-z]+\s*=", name)
            for word in FORBIDDEN_SCRIPT:
                self.assertNotIn(word, page, (name, word))

    def test_each_page_is_a_proper_accessible_document(self):
        for name, page in self.PAGES.items():
            for needle in ('<html lang="en">', "<title>", 'name="viewport"', 'name="robots" content="noindex, nofollow"', "<h1>", 'role="status"', 'aria-live="polite"'):
                self.assertIn(needle, page, (name, needle))

    def test_every_input_has_a_label_and_every_button_is_a_button(self):
        for name, page in self.PAGES.items():
            for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", page):
                identifier = re.search(r'\bid="([^"]+)"', tag)
                if 'type="checkbox"' in tag:
                    continue
                self.assertTrue("aria-label=" in tag or (identifier and re.search(rf'<label[^>]*\bfor="{identifier.group(1)}"', page)), (name, tag))
            self.assertNotRegex(page, r'<(?:div|span|a)\b[^>]*\brole="button"', name)

    def test_passwords_are_hidden_and_mark_themselves_for_a_password_manager(self):
        for name, page in self.PAGES.items():
            for tag in re.findall(r"<input\b[^>]*type=\"password\"[^>]*>", page):
                self.assertRegex(tag, r'autocomplete="(?:current-password|new-password)"', (name, tag))
                self.assertIn('spellcheck="false"', tag)

    def test_nothing_in_the_pages_uses_characters_he_banned(self):
        for name, page in {**self.PAGES, "dashboard": DASH}.items():
            self.assertNotIn("\u2014", page, name)
            self.assertNotIn("\u00a7", page, name)
            self.assertFalse(any(ord(c) >= 0x1F300 for c in page), name)

    def test_motion_is_switched_off_for_people_who_ask(self):
        for name, page in self.PAGES.items():
            self.assertIn("prefers-reduced-motion", page, name)


class StaffPageTests(unittest.TestCase):
    def test_it_reads_its_data_from_the_staff_door_every_four_seconds_and_sends_the_person_to_sign_in_when_refused(self):
        self.assertIn('"/staff/state"', STAFF)
        self.assertRegex(STAFF, r"setInterval\(load,\s*4000\)")
        self.assertIn('reply.status === 401', STAFF)
        self.assertIn('"/control/login"', STAFF)

    def test_a_refused_password_change_is_shown_on_the_page_and_is_never_taken_for_a_signed_out_session(self):
        # His report (2026-11-02): "change of password didn't work". A wrong CURRENT password comes back as a 401 too, and the page used to send the person to the sign-in page on ANY 401:
        # no message, and they then tried the new password they had typed (never saved). Only the server's own "sign in first" may send anyone away.
        body = function_body(STAFF, "api")
        self.assertIn('"sign in first"', body)
        self.assertIn("data.error", body)
        self.assertIn("reply.json()", body)
        self.assertEqual(body.count('location.href = "/control/login"'), 1)
        redirect = body.index('location.href = "/control/login"')
        condition = body[body.rindex("if (", 0, redirect):redirect]
        self.assertIn('"sign in first"', condition)                                                   # the redirect is guarded by the words, not by the number alone
        self.assertIn("reply.status === 401", condition)

    def test_it_can_only_ask_for_the_five_things_a_staff_person_may_do(self):
        for action in ("start_shift", "finish_shift", "acknowledge", "done", "change_password"):
            self.assertIn(f'"{action}"', STAFF)
        self.assertIn('"/staff/act"', STAFF)
        self.assertIn('"/staff/logout"', STAFF)

    def test_it_never_reaches_a_super_user_door_or_action(self):
        for needle in ("/control/panel", "/control/session", "/control/staff", "add_person", "reset_password", "disable_person", '"assign"', "unassign", "use_vps", "use_core"):
            self.assertNotIn(needle, STAFF, needle)

    def test_it_shows_the_shift_the_roster_the_failures_and_the_finished_ones(self):
        for needle in ('id="shift-text"', 'id="start"', 'id="finish"', 'id="roster"', 'id="failures"', 'id="done"', "Start shift", "Finish shift", "Nobody has it yet", "over 48 hours"):
            self.assertIn(needle, STAFF, needle)

    def test_a_note_being_typed_is_not_wiped_by_the_four_second_refresh(self):
        body = function_body(STAFF, "render")
        self.assertIn("activeElement", body)
        self.assertIn("notes[", STAFF)

    def test_the_list_is_redrawn_after_every_press_and_held_only_while_a_note_is_being_typed(self):
        # Found in a real browser: the button just pressed kept focus inside the list, so a finished failure stayed listed as open.
        self.assertIn("render(result.data, true)", function_body(STAFF, "act"))
        body = function_body(STAFF, "render")
        self.assertIn("force ||", body)
        self.assertIn('active.tagName === "INPUT"', body)
        self.assertIn('active.value !== ""', body)

    def test_a_person_can_mark_done_only_a_failure_that_the_server_says_is_theirs(self):
        body = function_body(STAFF, "renderFailures")
        self.assertIn("failure.mine", body)

    def test_the_i_am_on_it_button_is_offered_only_to_a_person_on_shift_who_has_not_pressed_yet(self):
        body = function_body(STAFF, "renderFailures")
        self.assertIn('"I am on it"', body)
        self.assertIn('{action: "acknowledge", fid: failure.fid}', body)
        # The condition itself, straight in front of the button. (An earlier version compared where the words first appear, and "onShift" is also in the function's own first line,
        # so removing the condition changed nothing: found by the mutation run.)
        self.assertRegex(body, r'if \(onShift && !failure\.seen_by_me\) \{\s+var ack = el\("button", "I am on it"')
        self.assertIn("renderFailures(view.failures || [], me.on_shift)", function_body(STAFF, "render"))

    def test_each_failure_says_who_is_on_it_how_long_after_the_red_and_in_the_offices_zone(self):
        body = function_body(STAFF, "renderFailures")
        for needle in ("failure.seen", "Nobody has said they are on it yet", "seenText("):
            self.assertIn(needle, body, needle)
        text = function_body(STAFF, "seenText")
        self.assertIn("clock(", text)                                                    # Abuja time, labelled WAT, like every other time on the page
        self.assertIn("after the red", text)
        self.assertIn("after_s !== null", text)                                          # a press whose time is not measured is shown without one, never as 0s
        self.assertIn("span(", text)

    def test_the_length_of_time_is_written_the_way_the_weekly_report_writes_it(self):
        body = function_body(STAFF, "span")
        for needle in ('"s"', '"m "', '"h "'):
            self.assertIn(needle, body, needle)

    def test_staff_are_told_what_is_recorded(self):
        self.assertIn("is recorded with the time and appears by name in the Monday report", STAFF)

    def test_the_text_it_builds_is_always_text(self):
        for name in ("el", "renderRoster", "renderFailures", "render"):
            self.assertIn("textContent", function_body(STAFF, name) if name != "el" else function_body(STAFF, "el"))

    def test_a_switched_off_service_is_told_plainly(self):
        self.assertIn("reply.status === 503", STAFF)

    def test_signing_out_does_not_pretend_to_end_the_shift(self):
        self.assertIn("A shift is not ended by signing out", STAFF)


class PanelStaffSectionTests(unittest.TestCase):
    def test_the_four_old_buttons_and_the_log_out_are_still_there(self):
        for identifier in ("off", "on", "core", "vps", "out"):
            self.assertIn(f'id="{identifier}"', PANEL)
        self.assertIn('"/control/panel/act"', PANEL)

    def test_the_staff_section_has_what_he_asked_for(self):
        for needle in ('id="staff"', 'id="slots"', 'id="roster"', 'id="failures"', 'id="people"', 'id="add-user"', 'id="add-display"', 'id="add-pass"', 'id="add-go"', 'id="reset-user"', 'id="reset-pass"',
                       'id="reset-go"', 'id="staffmsg"', "Add a person", "Reset a password"):
            self.assertIn(needle, PANEL, needle)

    def test_it_reaches_only_the_panels_staff_door_with_the_six_actions_it_still_has(self):
        # Since 2026-11-08 the system assigns critical alerts by itself, so his page no longer sends "assign" or "unassign" (ops/test_staff_critical.py pins that they are gone).
        self.assertIn('"/control/panel/staff"', PANEL)
        for action in ("add_person", "reset_password", "disable_person", "delete_person", "finish_shift", "done"):
            self.assertIn(f'"{action}"', PANEL)

    def test_every_person_can_be_deleted_after_a_question_that_says_what_it_does(self):
        # His request (2026-11-02). Delete is offered for EVERY person (a disabled one too: that is the one whose name was stuck), after a question that is not skippable.
        body = function_body(PANEL, "renderPeople")
        self.assertIn('{action: "delete_person", username: person.username}', body)
        self.assertEqual(body.count("confirm("), 2)                                                  # one for Disable, one for Delete
        start = body.index('"delete_person"')
        question = body[body.rindex("confirm(", 0, start):start]
        for words in ("Delete ", "cannot be undone", "history stays", "name can be used again"):
            self.assertIn(words, question, words)
        self.assertIn('"Delete"', body)
        opening = body.index("{", body.index("if (person.active)"))
        depth, closing = 0, None
        for position in range(opening, len(body)):
            depth += body[position] == "{"
            depth -= body[position] == "}"
            if depth == 0:
                closing = position
                break
        self.assertIsNotNone(closing)
        self.assertGreater(body.index('"delete_person"'), closing)                                    # the Delete button sits AFTER the "if (person.active)" block closes, so a disabled person has it too
        between = body[closing:body.index('var remove = el("button", "Delete"')]
        self.assertNotIn("if (", between)                                                             # and nothing between the block and the button can skip it for some people
        self.assertNotIn("return", between)

    def test_he_can_start_a_shift_for_an_active_person_who_is_not_on_one(self):
        # His absolute power (2026-11-02): a broken tailnet must never stop him putting someone on shift and assigning failures.
        body = function_body(PANEL, "renderPeople")
        self.assertIn('{action: "start_shift", username: person.username}', body)
        self.assertIn('"Start shift"', body)
        self.assertLess(body.index("!person.on_shift"), body.index('"start_shift"'))             # offered only to someone who is not already on shift
        self.assertLess(body.index("if (person.active)"), body.index('"start_shift"'))          # and only to an active person

    def test_he_sees_who_is_on_each_open_failure_and_how_long_after_the_red_and_has_no_button_for_it(self):
        body = function_body(PANEL, "renderFailures")
        for needle in ("failure.seen", "Nobody has said they are on it yet", "staffTime(", "staffSpan(", "after the red"):
            self.assertIn(needle, body + function_body(PANEL, "staffSeen"), needle)
        self.assertIn("after_s !== null", function_body(PANEL, "staffSeen"))
        self.assertNotIn('"acknowledge"', PANEL)                                         # the button is staff's; he has Assign and Done

    def test_the_staff_picture_rides_on_the_panels_own_poll(self):
        self.assertIn("renderStaff(state.staff)", function_body(PANEL, "render"))
        self.assertNotIn('"/control/panel/staff", {method: "GET"}', PANEL)

    def test_disabling_someone_asks_first(self):
        self.assertIn("confirm(", function_body(PANEL, "renderPeople"))

    def test_the_forms_are_not_wiped_by_the_refresh_and_typed_notes_survive_it(self):
        self.assertIn("activeElement", function_body(PANEL, "renderStaff"))

    def test_typed_text_is_only_ever_set_as_text(self):
        for name in ("renderStaff", "renderRoster", "renderFailures", "renderPeople"):
            self.assertIn("textContent", function_body(PANEL, name) + function_body(PANEL, "el"))

    def test_a_staff_error_is_shown_in_its_own_announced_message_box(self):
        self.assertIn('id="staffmsg" role="status" aria-live="polite"', PANEL)


class AddPersonFormTests(unittest.TestCase):
    """His screenshot (2026-11-02): the boxes of the Add a person form did not line up, because the Username label was long enough to wrap onto two lines and pushed its box down.
    And staff passwords are simple on purpose (as short as four digits), so no page may talk about eight characters or strength."""
    FIELDS = ("add-user", "add-display", "add-pass", "reset-user", "reset-pass")

    def labels(self):
        import re
        found = dict(re.findall(r'<label for="([a-z-]+)">([^<]*)</label>', PANEL))
        return {name: found[name] for name in self.FIELDS}

    def test_every_label_is_one_short_line_so_none_can_wrap_and_push_a_box_down(self):
        for name, text in self.labels().items():
            self.assertLessEqual(len(text), 24, (name, text))

    def test_the_rules_are_hints_under_the_boxes(self):
        self.assertIn(".hint {", PANEL)
        for needle in ('<div class="hint">3 to 24 lowercase letters, digits, dots, dashes</div>', '<div class="hint">At least 4 characters. A simple word or number is fine.</div>'):
            self.assertIn(needle, PANEL)
        self.assertLess(PANEL.index('id="add-user"'), PANEL.index("3 to 24 lowercase letters"))          # the hint comes after its box, not inside the label

    def test_no_page_asks_for_a_strong_password(self):
        for name, page in (("panel", PANEL), ("staff", STAFF)):
            for words in ("8 characters", "eight characters", "strong password", "too simple", "must not contain your name"):
                self.assertNotIn(words, page, (name, words))


class AbujaTimeTests(unittest.TestCase):
    """His rule (2026-11-02): the two office PCs are in Abuja, so the times people start and finish a shift are Abuja time on his panel and on the Staff page alike, whatever zone the
    browser itself is set to (a PC whose Windows zone was never set, or his own screen at sea), and each time says so."""

    def test_every_shift_time_on_both_pages_is_formatted_in_the_abuja_zone_and_labelled(self):
        for name, body in (("staff page", function_body(STAFF, "clock")), ("panel", function_body(PANEL, "staffTime"))):
            self.assertIn('timeZone: "Africa/Lagos"', body, name)
            self.assertIn('" WAT"', body, name)


class LoginPageTests(unittest.TestCase):
    def test_it_still_posts_to_the_one_sign_in_door_with_the_same_words_on_failure(self):
        self.assertIn('"/control/session"', LOGIN)
        self.assertIn("That name or password was not accepted.", LOGIN)
        self.assertIn("Too many wrong tries", LOGIN)

    def test_it_sends_staff_to_their_page_and_never_follows_an_address_the_server_sent(self):
        self.assertIn('"/staff"', LOGIN)
        self.assertIn('"/control/panel"', LOGIN)
        self.assertNotRegex(LOGIN, r"location\.href\s*=\s*(?:data|body|reply|result)\.")
        self.assertRegex(LOGIN, r'next\s*===\s*"/staff"')

    def test_it_says_it_is_for_staff_and_the_super_user(self):
        self.assertIn("Staff and the super user", LOGIN)
        self.assertNotIn('inputmode="email"', LOGIN)

    def test_it_tells_the_person_what_the_server_really_said_about_a_refusal(self):
        # His report (2026-11-02): staff who typed correct details here were told "That name or password was not accepted" even when the real reason was "staff sign in on the office PCs"
        # or "wait two minutes". The page now shows the server's own sentence (as text) for those two refusals, and the old words for a wrong name or password.
        body = function_body(LOGIN, "signIn")
        for needle in ("reply.status === 403", "reply.status === 429", "data.error", "That name or password was not accepted."):
            self.assertIn(needle, body, needle)
        self.assertIn("textContent", body)
        self.assertNotRegex(body, r"innerHTML")

    def test_the_server_gives_the_staff_address_when_it_makes_the_page_and_the_page_never_invents_one(self):
        address = "https://staff.example.org/staff"
        page = control_access.login_page_password(staff_url=address)
        self.assertIn(f'<a href="{address}">', page)
        self.assertIn("office PC", page)
        for bad in ("javascript:alert(1)", "http://staff.example.org/staff", 'https://x"><script>', "https://a b", "", None, 7):
            shown = control_access.login_page_password(staff_url=bad)
            self.assertNotIn('<a href="', shown, repr(bad))
            self.assertNotIn("office PC", shown, repr(bad))
        self.assertNotIn("__STAFF", control_access.login_page_password(staff_url=address))
        self.assertNotIn("__STAFF", LOGIN)

    def test_the_link_is_a_proper_link_with_a_name_a_screen_reader_can_read(self):
        page = control_access.login_page_password(staff_url="https://staff.example.org/staff")
        self.assertRegex(page, r'<a href="https://staff.example.org/staff">[^<]{6,}</a>')
        self.assertNotRegex(page, r'<a[^>]*target=')


class DashboardChipTests(unittest.TestCase):
    def test_the_chip_sits_in_the_toolbar_and_starts_hidden(self):
        toolbar = DASH[DASH.index('<div class="toolbar">'):]
        toolbar = toolbar[:toolbar.index("</div>")]
        self.assertRegex(toolbar, r'<span id="shift-chip"[^>]*\bhidden\b')

    def test_it_is_updated_from_every_frame_and_copes_with_a_frame_without_staff(self):
        self.assertIn("renderShift(frame.staff)", DASH)
        body = function_body(DASH, "renderShift")
        self.assertIn("(staff && staff.on_shift) || []", body)
        self.assertIn("textContent", body)
        for word in FORBIDDEN_SCRIPT:
            self.assertNotIn(word, body, word)

    def test_it_says_who_is_on_shift_in_plain_words(self):
        self.assertIn("On shift: ", function_body(DASH, "renderShift"))

    def test_the_chip_has_a_style_that_fits_the_toolbar(self):
        self.assertIn(".toolbar .shift-chip", DASH)


if __name__ == "__main__":
    unittest.main(verbosity=1)
