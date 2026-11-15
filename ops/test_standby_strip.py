# File:         test_standby_strip.py
# Purpose:      Checks the page-level strip that tells a viewer "this is the STANDBY, Core is UP/DOWN" (standby spec 3.1): present but hidden in the page, filled only from
#               the stream's `standby` block, written with textContent (never HTML), and shown by both the dashboard and the topology view.
# Inputs:       None (reads the page template from core-listener/app/dashboard.py).
# Outputs:      Printed unittest results; exit code 1 if anything fails. The behaviour in a real browser is checked by ops/browser_tests/standby_strip_test.mjs.
# Dependencies: core-listener/app/dashboard.py
import importlib
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core-listener"))
dashboard = importlib.import_module("app.dashboard")
TOKEN = "unit-test-view-token"


class StandbyStripTests(unittest.TestCase):
    def pages(self):
        return {"dashboard": dashboard.render_dashboard_html(TOKEN), "topology": dashboard.render_topology_html(TOKEN)}

    def test_every_page_has_the_strip_and_it_starts_hidden(self):
        for name, html in self.pages().items():
            with self.subTest(page=name):
                self.assertEqual(len(re.findall(r'<div id="standby-strip"[^>]*\shidden', html)), 1)
                self.assertIn('role="status"', html.split('id="standby-strip"')[1].split(">")[0])

    def test_the_strip_sits_under_the_header_so_it_is_the_first_thing_seen(self):
        html = self.pages()["dashboard"]
        self.assertLess(html.index("</header>"), html.index('id="standby-strip"'))
        self.assertLess(html.index('id="standby-strip"'), html.index('<div class="toolbar">'))

    def test_the_page_script_fills_it_from_the_standby_block_of_every_frame(self):
        html = self.pages()["dashboard"]
        self.assertIn("function renderStandbyStrip(", html)
        self.assertRegex(html, r"function renderAll\(data\) \{[\s\S]{0,300}?renderStandbyStrip\(uiState\.lastData\.standby\)")

    def test_it_writes_text_never_html_so_a_hostile_value_cannot_inject_markup(self):
        html = self.pages()["dashboard"]
        body = html.split("function renderStandbyStrip(")[1].split("\n  }\n")[0]
        self.assertIn("textContent", body)
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(forbidden, body)

    def test_it_names_the_state_and_what_the_standby_is_doing_in_plain_words(self):
        body = self.pages()["dashboard"].split("function renderStandbyStrip(")[1].split("\n  }\n")[0]
        self.assertIn("STANDBY", body)
        self.assertIn("sends no alerts", body)          # while Core answers
        self.assertIn("is alerting", body)               # after the takeover
        self.assertIn("standby-down", body)
        self.assertIn("standby-up", body)

    def test_a_page_with_no_standby_block_hides_it_again(self):
        body = self.pages()["dashboard"].split("function renderStandbyStrip(")[1].split("\n  }\n")[0]
        self.assertRegex(body, r'role !== "standby"[^}]*hidden = true')

    def test_the_styles_exist_for_both_states(self):
        html = self.pages()["dashboard"]
        for css_class in (".standby-strip", ".standby-up", ".standby-down"):
            self.assertIn(css_class, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
