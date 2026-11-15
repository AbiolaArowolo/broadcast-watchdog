"""Tests for the two edge senders (the PowerShell scripts that run on MAIN and BACKUP-SERVER).

The logic is PowerShell, so the behaviour tests live in edge/tests/*.ps1 and are run here through
powershell.exe (skipped, loudly, where PowerShell is not available). What is checked in Python:
the activity-tracking code both senders carry must be byte-identical (it is deliberately
duplicated, one file per machine, so a deploy never depends on a shared module), both scripts
must parse, and the payload contract the listener depends on must be present.

Run: python test_edge_senders.py
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "edge" / "main" / "insta_heartbeat_sender.ps1"
BACKUP = REPO / "edge" / "backup-server" / "admax_heartbeat_sender.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def activity_region(path: Path) -> str:
    match = re.search(r"#region engine-activity.*?#endregion engine-activity", path.read_text(encoding="utf-8"), re.S)
    assert match, f"{path.name} has no engine-activity region"
    return match.group(0)


class SharedActivityCodeTests(unittest.TestCase):
    def test_both_senders_carry_identical_activity_code(self):
        self.assertEqual(activity_region(MAIN), activity_region(BACKUP))

    def test_both_senders_carry_identical_log_hygiene_code(self):
        def region(path):
            match = re.search(r"#region log-hygiene.*?#endregion log-hygiene", path.read_text(encoding="utf-8"), re.S)
            self.assertTrue(match, f"{path.name} has no log-hygiene region")
            return match.group(0)
        self.assertEqual(region(MAIN), region(BACKUP))
        for path in (MAIN, BACKUP):
            text = path.read_text(encoding="utf-8")
            self.assertIn("Test-LogFull -Path $file -MaxBytes $LogMaxBytes", text)   # the daily cap is applied where lines are written
            self.assertIn("Remove-OldLogs -Dir", text)                              # and old files are removed from the loop
            self.assertIn("Get-FailureLogDecision", text)                           # and repeated failures are rate-limited

    def test_both_senders_carry_identical_send_code(self):
        def region(path):
            match = re.search(r"#region send-with-fallback.*?#endregion send-with-fallback", path.read_text(encoding="utf-8"), re.S)
            self.assertTrue(match, f"{path.name} has no send-with-fallback region")
            return match.group(0)
        self.assertEqual(region(MAIN), region(BACKUP))

    def test_both_senders_carry_identical_standby_copy_code(self):
        def region(path):
            match = re.search(r"#region standby-copy.*?#endregion standby-copy", path.read_text(encoding="utf-8"), re.S)
            self.assertTrue(match, f"{path.name} has no standby-copy region")
            return match.group(0)

        self.assertEqual(region(MAIN), region(BACKUP))

    def test_the_copy_to_the_standby_goes_out_after_core_has_been_posted_to_and_inside_the_error_handler(self):
        for path in (MAIN, BACKUP):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn('[string]$StandbyListenerUrl = "https://standby.corp.example/heartbeat"', text)
                call = "Send-StandbyCopy -Url $StandbyListenerUrl -Json $payload -PrimaryOk ([bool]$sent.ok) -Token $WatchdogToken"
                self.assertEqual(text.count(call), 1)
                self.assertLess(text.index("$sent = Send-Heartbeat -Urls"), text.index(call), "Core is posted to first, the copy second")
                loop = text.split("function Start-HeartbeatLoop")[1]
                self.assertLess(loop.index("try {"), loop.index(call), "the copy is inside the loop's try block")
                self.assertLess(loop.index(call), loop.index("catch {"))
                self.assertNotRegex(text, r"Invoke-RestMethod[^\r\n]*StandbyListenerUrl", "the copy never uses the call that blocks")

    def test_both_senders_use_tailscale_first_and_the_lan_only_as_the_fallback(self):
        # The key crosses a network on every heartbeat: Tailscale (WireGuard) is encrypted, the LAN is not.
        for path in (MAIN, BACKUP):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn('[string]$ListenerUrl = "http://100.64.0.10:8420/heartbeat"', text)
                self.assertIn('[string]$FallbackListenerUrl = "http://192.168.7.183:8420/heartbeat"', text)
                self.assertIn("Send-Heartbeat -Urls (Get-ListenerUrls -Primary $ListenerUrl -Fallback $FallbackListenerUrl)", text)
                self.assertNotRegex(text, r"Invoke-RestMethod -Uri \$ListenerUrl")        # no direct post that skips the fallback logic
                self.assertIn("HTTP 403", text)                                           # a key bound to another machine is reported, not retried

    def test_both_senders_carry_identical_site_c_feed_code(self):
        def region(path):
            match = re.search(r"#region site_c-feed.*?#endregion site_c-feed", path.read_text(encoding="utf-8"), re.S)
            self.assertTrue(match, f"{path.name} has no site_c-feed region")
            return match.group(0).replace("\r\n", "\n")

        self.assertEqual(region(MAIN), region(BACKUP))

    def test_both_senders_put_the_site_c_feed_in_the_heartbeat_and_drain_it_while_they_sleep(self):
        for path in (MAIN, BACKUP):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("[bool]$SiteCFeedMonitor = $true", text)                       # on by default, switchable per PC
                self.assertIn('[int[]]$SiteCFeedPorts = @(5004, 5005)', text)
                self.assertIn("$fields.site_c_feed = $site_c", text)                         # the field the listener reads
                self.assertIn("Wait-WithSiteCDrain -Seconds $IntervalSeconds", text)         # the sleep drains the sockets, so a long cycle cannot overflow the buffer
                self.assertNotIn("Start-Sleep -Seconds $IntervalSeconds", text)
                self.assertIn("$payload = $fields | ConvertTo-Json -Depth 5 -Compress", text)  # the standby copy and the Core post both send this one payload

    def test_both_senders_close_a_per_second_sample_whenever_they_drain_and_send_the_new_ones_with_the_heartbeat(self):
        for path in (MAIN, BACKUP):
            text = path.read_text(encoding="utf-8")
            region = re.search(r"#region site_c-feed.*?#endregion site_c-feed", text, re.S).group(0)
            with self.subTest(path=path.name):
                drain = re.search(r"function Wait-WithSiteCDrain \{.*?\n\}", region, re.S).group(0)
                self.assertIn("Add-SiteCSecond -Feed $script:SiteCFeed -MonoSeconds (Get-MonotonicSeconds)", drain)     # every one-second step closes a sample
                self.assertIn("function Add-SiteCSecond", region)
                self.assertIn("seconds = ", region)                                                                         # the report field the listener reads
                self.assertRegex(region, r"SiteCKeepSamples = 60")                                                        # the most the sender holds
                self.assertRegex(region, r"SiteCReportSamples = 30")                                                      # the most one heartbeat carries
                self.assertRegex(region, r"SiteCMinSampleSeconds = 0\.25")                                                # a shorter stretch is carried into the next sample
                self.assertIn("$payload = $fields | ConvertTo-Json -Depth 5 -Compress", text)                               # deep enough for list -> sample -> number

    def test_both_senders_carry_identical_item_timing_code(self):
        # The progress bar's start/length helpers: one copy per machine on purpose, so they must not drift apart.
        def region(path):
            match = re.search(r"#region item-timing.*?#endregion item-timing", path.read_text(encoding="utf-8"), re.S)
            self.assertTrue(match, f"{path.name} has no item-timing region")
            return match.group(0)
        self.assertEqual(region(MAIN), region(BACKUP))

    def test_both_senders_report_start_and_length_inside_now_playing(self):
        # The listener reads started_utc and duration_s from the now_playing object (the unpublished design notes); the dashboard's bar depends on them.
        for path in (MAIN, BACKUP):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("started_utc", text)
                self.assertIn("duration_s", text)

    def test_working_floor_sits_well_below_measured_on_air_activity(self):
        # Measured 2026-10-10: the quietest 10 s window of any engine was 0.81 cores.
        floor = float(re.search(r"\$BusyFloorCores = ([0-9.]+)", activity_region(MAIN)).group(1))
        self.assertLessEqual(floor, 0.81 / 2)
        self.assertGreater(floor, 0.05)


class PayloadContractTests(unittest.TestCase):
    def test_both_senders_announce_activity_monitoring(self):
        for path in (MAIN, BACKUP):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIn("activity_monitor", text)  # the listener only holds alarms for senders that announce this
                self.assertIn("expected_interval_seconds", text)
                self.assertIn("advance_alarm_confirmed", text)

    def test_both_senders_apply_the_media_read_pause_test_with_the_measured_thresholds(self):
        main = MAIN.read_text(encoding="utf-8")
        backup = BACKUP.read_text(encoding="utf-8")
        for name, text in (("main", main), ("backup", backup)):
            self.assertIn("Merge-MediaActivity -Activity $activity", text, name)
            self.assertIn("Update-MediaActivity -State $script:MediaState", text, name)
        # thresholds measured in the supervised pauses of 2026-10-11 (the unpublished design notes); changing them needs a new replay
        self.assertRegex(main, r"\$MediaQuietCores = 0\.15\s")
        self.assertRegex(main, r"\$MediaQuietReadMBps = 0\.05\s")
        self.assertRegex(backup, r"\$MediaQuietCores = 0\.45\s")
        self.assertRegex(backup, r"\$MediaQuietReadMBps = 0\.20\s")
        self.assertIn("Name='MServer.exe'", main, "MAIN measures the media-server children of each Insta engine")
        self.assertIn("ReadTransferCount", backup, "BACKUP measures each AdMax engine's own reads")

    def test_files_that_do_not_exist_or_do_not_move_are_not_used_as_motion(self):
        for path in (MAIN, BACKUP):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("playlist_position.txt", text.split("#>")[1])  # measured: written once at start
                self.assertNotIn("advisory_live_motion_file_unverified", text)

    def test_main_now_playing_is_the_olive_row_never_the_last_item_of_the_day(self):
        body = MAIN.read_text(encoding="utf-8").split("#>", 1)[1]
        self.assertIn("<BackgroundColor>Olive</BackgroundColor>", body)
        # the old guess (last file named anywhere in the playlist) must not come back
        self.assertNotIn("Get-MediaFilenameFromText", body)
        self.assertNotIn("$MediaFilePattern", body)
        self.assertIn("$instance[\"now_playing\"]", body)

    def test_backup_now_playing_uses_open_file_lines_only(self):
        body = BACKUP.read_text(encoding="utf-8").split("#>", 1)[1]
        self.assertIn("Open File", body)
        self.assertNotIn("Get-MediaFilenameFromText", body)

    def test_backup_reads_the_ansi_log_without_forcing_utf8(self):
        # Forcing UTF-8 turned the e-acute in "Beyonce" into U+FFFD on the real log.
        read_line = next(line for line in BACKUP.read_text(encoding="utf-8").splitlines() if "-Tail 600" in line)
        self.assertNotIn("-Encoding", read_line)

    def test_backup_client_window_is_not_counted_as_a_player(self):
        self.assertIn('_CLIENT\\.exe$', BACKUP.read_text(encoding="utf-8"))


class FileBlockFolderNameTests(unittest.TestCase):
    """Insta names its FileBlock folders with a ZERO-PADDED day: '1200AM-Sunday,November08,2026' (read off MAIN on 2026-11-08). The sender looked for 'November8', so on days 1 to 9
    of a month its FileBlock estimate was never found. edge/tests/Test-MainSender.ps1 pins the real names; these two keep the format string itself honest."""

    def test_the_sender_looks_for_the_zero_padded_day(self):
        text = MAIN.read_text(encoding="utf-8")
        self.assertIn('ToString("dddd,MMMMdd,yyyy"', text)
        self.assertNotIn('"dddd,MMMMd,yyyy"', text, "a single d drops the leading zero Insta writes")

    def test_the_test_fixture_never_borrows_the_unpadded_format(self):
        # the old fixture used the sender's own (wrong) format string, so the test agreed with the bug
        fixture = (REPO / "edge" / "tests" / "Test-MainSender.ps1").read_text(encoding="utf-8")
        self.assertNotIn("dddd,MMMMd", fixture)
class MainItemChangeWaitTests(unittest.TestCase):
    """MAIN used to sleep a flat 10 s between looks at the playlist, so each item reached Core 8 to 16 s after it started (measured live 2026-11-07).
    The loop now waits for Insta to rewrite the playlist or status file (edge/tests/Test-MainSender.ps1 tests the wait itself); these checks pin how the loop uses it."""

    def loop(self) -> str:
        return MAIN.read_text(encoding="utf-8").split("function Start-HeartbeatLoop", 1)[1]

    def test_the_loop_waits_with_the_item_change_wait_and_no_longer_sleeps_a_flat_interval(self):
        loop = self.loop()
        self.assertIn("Wait-ForItemChangeOrInterval -Baseline $signature -IntervalSeconds $IntervalSeconds", loop)
        self.assertNotIn("Start-Sleep -Seconds $IntervalSeconds", loop)

    def test_the_files_are_looked_at_before_the_playlist_is_read_so_a_change_during_the_cycle_is_not_lost(self):
        loop = self.loop()
        self.assertLess(loop.index("$signature = Get-ItemChangeSignature"), loop.index("Get-InstaInstances -Bindings $bindings"))
        self.assertLess(loop.index("Send-Heartbeat -Urls"), loop.index("Wait-ForItemChangeOrInterval"), "the wait comes after Core has been posted to")

    def test_both_channels_playlist_and_status_files_are_watched(self):
        text = MAIN.read_text(encoding="utf-8")
        self.assertIn(r"Settings\Mainplaylist.xml", text)
        self.assertIn(r"Settings\runningstatus.txt", text)
        watched = re.search(r"function Get-WatchedItemPaths.*?\n}\n", text, re.S)
        self.assertTrue(watched, "the list of watched files is built in one place")
        self.assertIn("$InstaRoots", watched.group(0), "from the same two channel folders the playlist is read from, so the two can never disagree")

    def test_the_early_wait_still_drains_the_site_c_sockets_every_second(self):
        # main added the Site C feed monitor: its sockets must be emptied once a second while the sender waits (a reader that wakes only every ten seconds loses packets and
        # reports a falsely low rate). The item-change wait must therefore take its one-second steps through that same drain, and a missing baseline must use it for the whole interval.
        loop = self.loop()
        self.assertIn("Wait-WithSiteCDrain -Seconds $IntervalSeconds", loop)
        self.assertRegex(loop, r"-Sleep \{ param\(\$seconds\) Wait-WithSiteCDrain -Seconds ")

    def test_the_declared_cadence_and_the_wait_ceiling_stay_ten_seconds(self):
        text = MAIN.read_text(encoding="utf-8")
        self.assertRegex(text, r"\[int\]\$IntervalSeconds = 10,")                 # an early heartbeat is extra, never a changed promise to Core
        self.assertIn("expected_interval_seconds = $IntervalSeconds", text)

    def test_the_backup_sender_is_untouched_by_this_change(self):
        self.assertNotIn("Wait-ForItemChangeOrInterval", BACKUP.read_text(encoding="utf-8"))


@unittest.skipUnless(POWERSHELL, "PowerShell is not installed here; the edge logic tests cannot run")
class PowerShellTests(unittest.TestCase):
    def run_ps(self, *args):
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", *args], capture_output=True, text=True, timeout=180,
        )

    def test_scripts_parse(self):
        for path in (MAIN, BACKUP):
            with self.subTest(path=path.name):
                result = self.run_ps(
                    "-Command",
                    f"$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$e); "
                    "if ($e.Count -gt 0) { $e | ForEach-Object { $_.Message }; exit 1 }",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_main_sender_logic(self):
        result = self.run_ps("-File", str(REPO / "edge" / "tests" / "Test-MainSender.ps1"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ALL PASSED", result.stdout)

    def test_send_fallback_logic(self):
        result = self.run_ps("-File", str(REPO / "edge" / "tests" / "Test-SendFallback.ps1"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ALL PASSED", result.stdout)

    def test_standby_copy_logic(self):
        result = self.run_ps("-File", str(REPO / "edge" / "tests" / "Test-StandbyCopy.ps1"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ALL PASSED", result.stdout)

    def test_site_c_feed_counting_logic(self):
        result = self.run_ps("-File", str(REPO / "edge" / "tests" / "Test-SiteCFeed.ps1"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ALL PASSED", result.stdout)

    def test_backup_sender_logic(self):
        result = self.run_ps("-File", str(REPO / "edge" / "tests" / "Test-BackupSender.ps1"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ALL PASSED", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
