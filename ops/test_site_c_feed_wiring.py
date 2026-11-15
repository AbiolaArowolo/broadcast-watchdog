# File:         test_site_c_feed_wiring.py
# Purpose:      Tests of how core-listener/app/main.py USES the Site C feed rules: the heartbeat stores the PCs' readings and judges them, an alarm raises the event, the red
#               e-mail, the push and a staff failure, a restore records its own event, and nothing here can disturb the six playouts' alarms or the rest of the heartbeat.
# Inputs:       None (a throwaway database, a fake mail server, a fake other node). Nothing leaves this machine.
# Outputs:      Printed unittest results; exit code 1 if anything fails.
# Dependencies: core-listener/app/main.py and site_c_feed.py, loaded the way ops/test_staff_wiring.py loads them.
"""Run: python ops/test_site_c_feed_wiring.py

What is proven here, in plain words (the unpublished design notes): a heartbeat that carries the new field updates the Site C feed and one that does not, or carries rubbish, is
still accepted and changes nothing else; a lost stream raises exactly one alarm (an event, an e-mail naming the stream and the cause, a push, and a staff failure keyed
SITE_C-5004 that the staff section can hold and assign like any other); a broken mail server cannot stop the staff failure; a restore is recorded once; and the alarm never
touches the six playouts.
"""
import importlib
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent
sys.path.insert(0, str(OPS))
import test_control_wiring as tcw  # noqa: E402
import test_staff_wiring as tsw  # noqa: E402

listener, run, FakeRequest, FakeSmtp = tcw.listener, tcw.run, tcw.FakeRequest, tcw.FakeSmtp
vf = importlib.import_module("app.site_c_feed")


def named(mail_text):
    """True for a mail about one of the two Site C streams, whichever of the owner's names (main = 5005, backup = 5004) it carries."""
    return "Site C ZIXI Feed main stream (UDP 5005)" in mail_text or "Site C ZIXI Feed backup stream (UDP 5004)" in mail_text


def feed(mbit=5.2, idle=0.0, listening=True):
    return {"listening": listening, "window_seconds": 10.0, "error": "",
            "streams": {s: {"mbit_per_s": mbit, "packets": 500, "idle_seconds": idle} for s in ("5004", "5005")}}


def action(kind="lost", stream="5004", name="raise", cause="Stream lost: no packets for 25 s"):
    return {"action": name, "stream": stream, "kind": kind, "cause": cause,
            "witnesses": [{"source": "MAIN", "state": "lost", "mbit_per_s": 0.0, "idle_seconds": 25, "age_seconds": 3}]}


class SiteCWiring(tsw.StaffWiring):
    def heartbeat(self, source, field=None, extra=None):
        body = {"hostname": source, **(extra or {})}
        if field is not None:
            body["site_c_feed"] = field
        return run(listener.heartbeat(FakeRequest({"content-type": "application/json"}, json.dumps(body).encode())))

    def events(self, event_type):
        with listener.db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM events WHERE event_type = ? ORDER BY id", (event_type,)).fetchall()]

    def mails(self):
        return [text for _recipients, text in FakeSmtp.sent]


class HeartbeatTests(SiteCWiring):
    def test_a_heartbeat_with_the_field_stores_it_and_the_panel_data_shows_it(self):
        self.assertEqual(self.heartbeat("MAIN", feed()).status_code, 200)
        with listener.db() as conn:
            snapshot = vf.snapshot(conn, datetime.now(timezone.utc), listener.SITE_C_SETTINGS)
        self.assertTrue(snapshot["reporting"])
        self.assertEqual({e["stream"]: e["level"] for e in snapshot["streams"]}, {"5004": "ok", "5005": "ok"})

    def test_a_heartbeat_without_the_field_or_with_rubbish_is_still_accepted_and_changes_nothing(self):
        for field in (None, "rubbish", 7, [], {"listening": "x"}, {"listening": True, "window_seconds": 10, "streams": {"5004": {"mbit_per_s": "fast"}}}):
            with self.subTest(field=field):
                self.assertEqual(self.heartbeat("MAIN", field).status_code, 200)
        with listener.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM site_c_feed_latest").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources WHERE source_id = 'MAIN'").fetchone()[0], 1)

    def test_a_heartbeat_with_per_second_samples_stores_them_and_the_panel_data_carries_the_list(self):
        field = feed()
        field["seconds"] = {"5004": [[1.0, 1.0, 4.99, 480], [0.0, 1.0, 5.01, 481]], "5005": [[0.0, 1.0, 5.0, 480]]}
        self.assertEqual(self.heartbeat("MAIN", field).status_code, 200)
        with listener.db() as conn:
            snapshot = listener._site_c_snapshot(conn, datetime.now(timezone.utc))
        fine = {e["stream"]: e["fine"] for e in snapshot["streams"]}
        self.assertEqual((fine["5004"]["mbit"], fine["5005"]["mbit"]), ([4.99, 5.01], [5.0]))
        self.assertEqual(fine["5004"]["step_seconds"], 1)

    def test_a_full_heartbeat_of_thirty_samples_a_stream_is_small_and_accepted(self):
        field = feed()
        field["seconds"] = {s: [[float(age), 1.0, 5.012, 481] for age in range(30)] for s in ("5004", "5005")}
        self.assertLess(len(json.dumps(field, separators=(",", ":"))), 3000, "the per-second samples must stay a small part of the heartbeat")
        self.assertEqual(self.heartbeat("MAIN", field).status_code, 200)

    def test_rubbish_per_second_samples_cost_nothing_else_in_the_heartbeat(self):
        field = feed()
        field["seconds"] = {"5004": ["junk", [1, 2], [0, 0, "x", 1]], "5005": "rubbish"}
        self.assertEqual(self.heartbeat("MAIN", field).status_code, 200)
        with listener.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM site_c_feed_latest").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM site_c_feed_seconds").fetchone()[0], 0)

    def test_a_heartbeat_that_loses_the_stream_raises_one_alarm_and_the_next_ones_raise_none(self):
        for _ in range(3):
            self.heartbeat("MAIN", feed(mbit=0, idle=30))
            self.heartbeat("BACKUP-SERVER", feed(mbit=0, idle=30))
        self.assertEqual(len(self.events("site_c_feed_critical")), 2)               # one per stream, not one per heartbeat
        self.assertEqual({json.loads(e["detail"])["stream"] for e in self.events("site_c_feed_critical")}, {"5004", "5005"})
        self.assertEqual(len([m for m in self.mails() if named(m) and "NOBODY ON SHIFT" not in m]), 2)       # the red mails: one per stream
        # Nobody is on shift in this test, so each new failure also sends the staff section's one "NOBODY ON SHIFT" notice (the unpublished design notes part 16): once per stream, not per heartbeat.
        self.assertEqual(len([m for m in self.mails() if named(m) and "NOBODY ON SHIFT" in m]), 2)

    def test_the_six_playouts_are_untouched_by_a_site_c_alarm(self):
        for _ in range(2):
            self.heartbeat("MAIN", feed(mbit=0, idle=30))
            self.heartbeat("BACKUP-SERVER", feed(mbit=0, idle=30))
        with listener.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM endpoint_alerts WHERE endpoint_id LIKE 'SITE_C%'").fetchone()[0], 0)
        self.assertEqual(len(listener.PLAYOUT_ENDPOINTS), 6)


class AlarmTests(SiteCWiring):
    def test_a_raise_records_the_event_mails_the_cause_pushes_and_opens_a_staff_failure(self):
        self.add("ada")
        self.start_shift()
        run(listener._site_c_alarm(action()))
        events = self.events("site_c_feed_critical")
        self.assertEqual((len(events), events[0]["source_id"], events[0]["severity"]), (1, "SITE-C", "critical"))
        mail = " ".join(self.mails())
        self.assertIn("Site C ZIXI Feed backup stream (UDP 5004)", mail)
        self.assertIn("Stream lost: no packets for 25 s", mail)
        self.assertTrue(any("/super" in url or "/shared" in url for url in self.pushes))
        failures = list(self.failures().values())
        self.assertEqual([(f["studio"], f["cause"], f["display"]) for f in failures], [("SITE_C-5004", "Stream lost: no packets for 25 s", "Site C ZIXI Feed backup stream (UDP 5004)")])

    def test_the_main_stream_alarm_is_named_main_in_the_mail_and_in_the_staff_failure(self):
        self.add("ada")
        self.start_shift()
        run(listener._site_c_alarm(action(stream="5005", cause="Under 1 Mbit/s for 2 min 5 s", kind="floor")))
        self.assertIn("Site C ZIXI Feed main stream (UDP 5005)", " ".join(self.mails()))
        self.assertEqual([(f["studio"], f["display"]) for f in self.failures().values()], [("SITE_C-5005", "Site C ZIXI Feed main stream (UDP 5005)")])

    def test_the_streams_get_separate_staff_failures(self):
        self.add("ada")
        self.start_shift()
        run(listener._site_c_alarm(action(stream="5004")))
        run(listener._site_c_alarm(action(stream="5005")))
        self.assertEqual(sorted(f["studio"] for f in self.failures().values()), ["SITE_C-5004", "SITE_C-5005"])

    def test_a_broken_mail_server_cannot_stop_the_staff_failure_or_the_event(self):
        self.add("ada")
        self.start_shift()
        FakeSmtp.down = True
        run(listener._site_c_alarm(action()))
        self.assertEqual(len(self.failures()), 1)
        self.assertEqual(len(self.events("site_c_feed_critical")), 1)

    def test_a_restore_records_its_own_event_and_opens_no_failure(self):
        self.add("ada")
        self.start_shift()
        run(listener._site_c_alarm(action(name="restore", cause="")))
        events = self.events("site_c_feed_restored")
        self.assertEqual((len(events), events[0]["severity"]), (1, "info"))
        self.assertEqual(len(self.failures()), 0)

    def test_the_failure_values_are_accepted_by_the_staff_rules_themselves(self):
        staff = importlib.import_module("app.staff")
        row = staff.failure_row(self.state(), "SITE_C-5004", "Under 1 Mbit/s for 2 min 5 s", "Site C ZIXI Feed backup stream (UDP 5004)", datetime.now(timezone.utc), "core", "ab12")
        self.assertEqual(row["kind"], "failure_opened")

    def test_the_failure_is_not_recorded_while_access_is_off_like_every_other_red_alarm(self):
        self.add("ada")
        self.start_shift()
        self.switch_off()
        run(listener._site_c_alarm(action()))
        self.assertEqual(len(self.failures()), 0)


class PruneTests(SiteCWiring):
    def test_the_daily_clean_up_also_removes_old_site_c_history(self):
        with listener.db() as conn:
            vf.ingest_locked(conn, "MAIN", vf.parse_report(feed()), datetime.now(timezone.utc) - timedelta(days=9))
            removed = listener.prune_old_site_c_history(conn, datetime.now(timezone.utc))
        self.assertGreater(removed, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
