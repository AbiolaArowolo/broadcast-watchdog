# File:         Test-BackupSender.ps1
# Purpose:      Tests the pure logic of edge/backup-server/admax_heartbeat_sender.ps1 (which processes are players,
#               feed mapping, slot root) with synthetic processes. Touches no real process, file or network.
# Inputs:       none. Run: powershell -NoProfile -ExecutionPolicy Bypass -File edge\tests\Test-BackupSender.ps1
# Outputs:      PASS/FAIL lines; exit code 0 when everything passed, 1 otherwise.
# Dependencies: the sender script, dot-sourced (its loop does not start when dot-sourced).
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $repo "edge\backup-server\admax_heartbeat_sender.ps1")
function Write-Log { param([string]$Message) }   # keep test runs out of the real log folder

$script:failures = 0
function Assert-Equal($actual, $expected, $label) {
    if ($actual -ne $expected) { Write-Host "FAIL: $label (got '$actual', expected '$expected')"; $script:failures++ }
    else { Write-Host "PASS: $label" }
}

function Assert-Null($actual, $label) {
    if ($null -ne $actual) { Write-Host "FAIL: $label (got '$actual', expected null)"; $script:failures++ }
    else { Write-Host "PASS: $label" }
}

function New-Proc($name, $processId, $threads) {
    [PSCustomObject]@{ Name = $name; ProcessId = $processId; ThreadCount = $threads }
}

Assert-Equal (Get-FeedFromProcessName -Name "STATION_NETWORK_BACKUP.exe") "usa" "Network is USA"
Assert-Equal (Get-FeedFromProcessName -Name "STATION_BOX_OFFICE_BACKUP.exe") "caribbean" "Box Office is Caribbean"
Assert-Equal (Get-FeedFromProcessName -Name "GLOBAL_2.exe") "unknown" "anything else is unknown"

# The real process list on BACKUP-SERVER: two engines and one client window
$real = @(
    (New-Proc "STATION_BOX_OFFICE_BACKUP.exe" 7292 60),
    (New-Proc "STATION_NETWORK_BACKUP.exe" 290936 61),
    (New-Proc "STATION_NETWORK_BACKUP_CLIENT.exe" 303504 19)
)
$engines = @(Select-AdmaxEngines -Processes $real)
Assert-Equal $engines.Count 2 "the client window is not a player: two players, not three"
Assert-Equal $engines[0].Name "STATION_NETWORK_BACKUP.exe" "usa engine first"
Assert-Equal $engines[1].Name "STATION_BOX_OFFICE_BACKUP.exe" "caribbean engine second"

$clientOnly = @(Select-AdmaxEngines -Processes @((New-Proc "STATION_NETWORK_BACKUP_CLIENT.exe" 1 19), (New-Proc "STATION_BOX_OFFICE_BACKUP.exe" 2 60)))
Assert-Equal $clientOnly.Count 1 "a client with no engine does not keep the usa tile alive"
Assert-Equal $clientOnly[0].Name "STATION_BOX_OFFICE_BACKUP.exe" "only caribbean is reported"

$dupes = @(Select-AdmaxEngines -Processes @((New-Proc "STATION_NETWORK_BACKUP.exe" 1 20), (New-Proc "STATION_NETWORK_BACKUP.exe" 2 61)))
Assert-Equal $dupes.Count 1 "two processes claiming one feed are one player"
Assert-Equal $dupes[0].ProcessId 2 "the one with more threads is the engine"
Assert-Equal @(Select-AdmaxEngines -Processes @()).Count 0 "no AdMax processes, no players"

Assert-Equal (Get-AdmaxSlotRoot -ExecutablePath "C:\Program Files (x86)\Unimedia\Admax One2.0\admax4\bin\64bit\STATION_NETWORK_BACKUP.exe") "C:\Program Files (x86)\Unimedia\Admax One2.0\admax4" "slot root is the folder above bin"

# --- Now playing: the newest "Open File" line in AdMax's own log (real excerpt from 2026-10-10)
$e = [string][char]0xE9
$log = @(
    "10.10.2026 20:13:52 (INFO): Admax_SDI_Playout.Main |[FillerMedia]|C:\Users\x\Desktop\FILLER\LOW.mp4|Open File and media in/out [0,0]. ",
    "10.10.2026 20:15:06 (INFO): Admax_SDI_Playout.Main PreCompleteFire. ",
    "10.10.2026 20:15:06 (ERROR): Admax_SDI_Playout.Main startxxx1. ",
    "10.10.2026 20:15:06 (INFO): Admax_SDI_Playout.Main |[FillerMedia]|C:\Users\x\Desktop\FILLER\Documentry 2.mp4|Open File and media in/out [0,0]. ",
    "10.10.2026 20:15:06 (INFO): Admax_SDI_Playout.Main Graphics load - -1. ",
    "10.10.2026 20:15:30 (ERROR): Admax_SDI_Playout.Main could not preview D:\other\Wrong File.mp4 "
)
$np = Get-AdmaxNowPlayingFromLines -Lines $log
Assert-Equal $np.filename "Documentry 2.mp4" "the newest Open File line is the item on air"
Assert-Equal $np.result "Started 20:15:06" "reports when it started"
Assert-Equal (Get-AdmaxNowPlayingFromLines -Lines @($log[0])).filename "LOW.mp4" "a single Open File line is enough"
Assert-Equal ($null -eq (Get-AdmaxNowPlayingFromLines -Lines @($log[1], $log[2], $log[4], $log[5]))) $true "error lines that merely mention a file are not now playing"
Assert-Equal ($null -eq (Get-AdmaxNowPlayingFromLines -Lines @())) $true "no lines, nothing"
$accent = "10.10.2026 20:15:51 (INFO): Admax_SDI_Playout.Main |[FillerMedia]|C:\f\Beyonc$e - Video, with commas.mp4|Open File and media in/out [0,0]. "
Assert-Equal (Get-AdmaxNowPlayingFromLines -Lines @($accent)).filename "Beyonc$e - Video, with commas.mp4" "accents and commas in a file name survive"

# --- The progress bar: when the item started (UTC) and how long it runs. Real lines from BACKUP-SERVER, 2026-10-14. AdMax writes the
# item's play range into the same "Open File" line: "in/out [0,7192.64]" is the file from 0 s to 7192.64 s, and Windows itself says that
# file is 01:59:52 long.
$long = "14.10.2026 09:00:06 (INFO): Admax_SDI_Playout.Main |[PlaylistMedia]|D:\station Dropbox\ADMAX PLAYOUTS\USA PLAYOUT PROGRAMS\Spanking New\Likuvume Zat Luzemoliv 119.mp4|Open File and media in/out [0,7192.64013671875]. "
$npLong = Get-AdmaxNowPlayingFromLines -Lines @($long)
Assert-Equal $npLong.duration_s 7192.6 "the length comes from the play range in the Open File line"
Assert-Equal ([string]$npLong.started_utc).EndsWith("Z") $true "the start is reported in UTC"
Assert-Equal ([datetime]$npLong.started_utc).ToLocalTime().ToString("dd.MM.yyyy HH:mm:ss") "14.10.2026 09:00:06" "the start is the log line's own time, read as this machine's wall clock"
Assert-Equal $npLong.result "Started 09:00:06" "the old result text is still there for older listeners"
$npFiller = Get-AdmaxNowPlayingFromLines -Lines @($log[0])
Assert-Null $npFiller.duration_s "a filler logged as in/out [0,0] has no known length: no bar is better than a wrong one"
$npTrim = Get-AdmaxNowPlayingFromLines -Lines @("14.10.2026 09:00:06 (INFO): Admax_SDI_Playout.Main |[PlaylistMedia]|D:\m\Trimmed.mp4|Open File and media in/out [10,70.5]. ")
Assert-Equal $npTrim.duration_s 60.5 "a trimmed item plays out minus in"
$npBackwards = Get-AdmaxNowPlayingFromLines -Lines @("14.10.2026 09:00:06 (INFO): Admax_SDI_Playout.Main |[PlaylistMedia]|D:\m\Bad.mp4|Open File and media in/out [90,30]. ")
Assert-Null $npBackwards.duration_s "an out point before the in point is not a length"
$npNoRange = Get-AdmaxNowPlayingFromLines -Lines @("14.10.2026 09:00:06 (INFO): Admax_SDI_Playout.Main |[PlaylistMedia]|D:\m\NoRange.mp4|Open File and media in/out [abc]. ")
Assert-Null $npNoRange.duration_s "an unreadable range gives no length and does not crash"
Assert-Equal $npNoRange.filename "NoRange.mp4" "an unreadable range still reports the file name"
$doubled = "14.10.2026 09:00:01 (INFO): Admax_SDI_Playout.Main |[PlaylistMedia]|D:\\station Dropbox\\ADMAX PLAYOUTS\\Movies for Caribbean Feed\\CEPE LEM (HUC HAMIBAMAV).mp4|Open File and media in/out [0,4991.019042]. "
Assert-Equal (Get-AdmaxNowPlayingFromLines -Lines @($doubled)).filename "CEPE LEM (HUC HAMIBAMAV).mp4" "doubled backslashes in the logged path (seen on the real log) still give the file name"
Assert-Equal (Get-AdmaxNowPlayingFromLines -Lines @($doubled)).duration_s 4991 "and the length (4991.019042 s is 83 minutes)"
# Fillers are logged as in/out [0,0] (found live on 2026-10-14: the Caribbean player plays 30 to 50 s fillers all day). Their length is read from the
# file itself, through a lookup the caller supplies (in production: Windows' own "Length" property), but only when the log has no range.
$fillerLine = "14.10.2026 10:40:20 (INFO): Admax_SDI_Playout.Main |[FillerMedia]|C:\Users\x\Desktop\FILLER\TADESAJORE NUVA.mp4|Open File and media in/out [0,0]. "
$script:lookedUp = @()
$npFill = Get-AdmaxNowPlayingFromLines -Lines @($fillerLine) -LengthLookup { param($p) $script:lookedUp += $p; 51 }
Assert-Equal $npFill.duration_s 51 "a filler logged as [0,0] gets its length from the file"
Assert-Equal $script:lookedUp.Count 1 "the lookup is asked once, for the item on air"
Assert-Equal $script:lookedUp[0] "C:\Users\x\Desktop\FILLER\TADESAJORE NUVA.mp4" "and is given the logged path"
$script:lookedUp = @()
$npKnown = Get-AdmaxNowPlayingFromLines -Lines @($long) -LengthLookup { param($p) $script:lookedUp += $p; 999 }
Assert-Equal $npKnown.duration_s 7192.6 "when the log gives the range the lookup is not used"
Assert-Equal $script:lookedUp.Count 0 "and is not even asked"
$npManyLines = Get-AdmaxNowPlayingFromLines -Lines @($log[0], $log[3], $fillerLine) -LengthLookup { param($p) $script:lookedUp += $p; 51 }
Assert-Equal $script:lookedUp.Count 1 "only the newest Open File line is looked up, not every line in the window"
Assert-Null (Get-AdmaxNowPlayingFromLines -Lines @($fillerLine) -LengthLookup { param($p) $null }).duration_s "a lookup that finds nothing means no bar, not a guess"
Assert-Null (Get-AdmaxNowPlayingFromLines -Lines @($fillerLine) -LengthLookup { param($p) throw "boom" }).duration_s "a lookup that throws never breaks the heartbeat"
Assert-Null (Get-AdmaxNowPlayingFromLines -Lines @($fillerLine) -LengthLookup { param($p) -5 }).duration_s "a nonsense length is ignored"
# the real lookup: Windows' own Length property
Assert-Null (Get-MediaLengthSeconds -Path "C:\no\such\folder\ghost.mp4") "a file that is not there has no length and no error"
Assert-Null (Get-MediaLengthSeconds -Path "") "an empty path has no length"
$wav = "C:\Windows\Media\Alarm01.wav"
if (Test-Path -LiteralPath $wav) {
    $len = Get-MediaLengthSeconds -Path $wav
    Assert-Equal ($len -gt 0 -and $len -lt 60) $true "the real Windows lookup reads a real file's length ($len s)"
    $quadrupled = ($wav -replace '\\', '\\\\')
    Assert-Equal (Get-MediaLengthSeconds -Path $quadrupled) $len "doubled backslashes in a logged path (seen on the real log) are collapsed"
}
else { Write-Host "SKIP: no sample media file on this machine for the real lookup" }
$unreadableDate = "99.99.2026 99:99:99 (INFO): Admax_SDI_Playout.Main |[PlaylistMedia]|D:\m\Odd.mp4|Open File and media in/out [0,60]. "
Assert-Null (Get-AdmaxNowPlayingFromLines -Lines @($unreadableDate)).started_utc "an unreadable timestamp gives no start (no bar) but the file name is kept"

# the cache keeps the last answer for the same player only
$script:NowPlayingCache = @{}
$script:NowPlayingCache["usa"] = @{ pid = 10; value = [PSCustomObject]@{ filename = "Cached.mp4"; result = "Started 10:00:00" } }
Assert-Equal (Get-AdmaxNowPlaying -SlotRoot "C:\no\such\slot" -Feed "usa" -ProcessId 10).filename "Cached.mp4" "log window without an Open File line: the same player keeps its last answer"
Assert-Equal ($null -eq (Get-AdmaxNowPlaying -SlotRoot "C:\no\such\slot" -Feed "usa" -ProcessId 99)) $true "a restarted player (new pid) does not inherit the old answer"

# --- Pause detection by media reads (supervised pauses of 2026-10-11: a paused player keeps its engine CPU busy but stops reading media)
$t0m = [datetime]::new(2026, 10, 11, 13, 0, 0, [DateTimeKind]::Utc)
$mq = @{}
$st = $null
for ($i = 1; $i -le 12; $i++) {
    $st = Update-MediaActivity -State $mq -Feed "usa" -Cores 1.0 -ReadMBps 2.0 -IntervalSeconds 10 -MonoSeconds (1000 + 10 * $i) -QuietCores 0.15 -QuietReadMBps 0.05
}
Assert-Equal $st.pause_quiet $false "media: normal playback is not quiet"
Assert-Equal $st.pause_quiet_age_seconds 0 "media: no quiet age while playing"
Assert-Equal $st.media_reads_mb_per_s 2 "media: reports the window's read rate"
$st = Update-MediaActivity -State $mq -Feed "usa" -Cores 0.0 -ReadMBps 0.0 -IntervalSeconds 10 -MonoSeconds 1130 -QuietCores 0.15 -QuietReadMBps 0.05
Assert-Equal $st.pause_quiet $false "media: one read-free 10 s interval (an item change) is not a pause"
# the pause: reads and CPU stay at zero from here on
$ages = @{}
for ($k = 2; $k -le 6; $k++) {
    $st = Update-MediaActivity -State $mq -Feed "usa" -Cores 0.0 -ReadMBps 0.0 -IntervalSeconds 10 -MonoSeconds (1120 + 10 * $k) -QuietCores 0.15 -QuietReadMBps 0.05
    $ages[$k] = $st
}
Assert-Equal $ages[2].pause_quiet $true "media: two read-free intervals in a row is quiet"
Assert-Equal $ages[2].pause_quiet_age_seconds 26 "media: quiet age starts where the quiet began (plus the 6 s settle allowance)"
Assert-Equal $ages[4].pause_quiet_age_seconds 46 "media: past 45 s of quiet age by the fourth interval"
Assert-Equal $ages[6].pause_quiet_age_seconds 66 "media: past 60 s of quiet age by the sixth interval"
# merged with an engine that is still burning CPU, as measured
$act = [ordered]@{ advance_alarm_confirmed = $true; advance_age_seconds = 0; advance_confirmed_seconds = 4000; advancing = $true; advance_status = "advancing"; advance_source = "engine CPU activity" }
Merge-MediaActivity -Activity $act -Media $ages[6] -NowUtc $t0m
Assert-Equal $act.advance_age_seconds 66 "merge: a quiet player's stall age is the quiet age even though the engine CPU is busy"
Assert-Equal $act.advance_status "stale" "merge: stale (past 60 s)"
Assert-Equal $act.advancing $false "merge: not advancing"
Assert-Equal $act.advance_confirmed_seconds 0 "merge: no steady activity while quiet"
Assert-Equal $act.advance_last_write_utc $t0m.AddSeconds(-66).ToString("o") "merge: last-activity time agrees with the age"
$act = [ordered]@{ advance_alarm_confirmed = $true; advance_age_seconds = 0; advance_confirmed_seconds = 4000; advancing = $true; advance_status = "advancing"; advance_source = "x" }
Merge-MediaActivity -Activity $act -Media $ages[4] -NowUtc $t0m
Assert-Equal $act.advance_status "warning" "merge: 46 s of quiet is amber (45 s) not yet red"
# resume: reads come back
$st = Update-MediaActivity -State $mq -Feed "usa" -Cores 1.0 -ReadMBps 2.0 -IntervalSeconds 10 -MonoSeconds 1190 -QuietCores 0.15 -QuietReadMBps 0.05
Assert-Equal $st.pause_quiet $false "media: reads back, the window average is above the floor, no longer quiet"
Assert-Equal $st.pause_quiet_age_seconds 0 "media: the quiet age resets at once"
$st = Update-MediaActivity -State $mq -Feed "usa" -Cores 1.0 -ReadMBps 2.0 -IntervalSeconds 10 -MonoSeconds 1200 -QuietCores 0.15 -QuietReadMBps 0.05
$act = [ordered]@{ advance_alarm_confirmed = $true; advance_age_seconds = 0; advance_confirmed_seconds = 4000; advancing = $true; advance_status = "advancing"; advance_source = "x" }
Merge-MediaActivity -Activity $act -Media $st -NowUtc $t0m
Assert-Equal $act.advance_confirmed_seconds 30 "merge: steady activity counts only from when the media resumed, so a red alarm clears after 30 s, not at the instant of resume"
# the longer of the two tests wins
$act = [ordered]@{ advance_alarm_confirmed = $true; advance_age_seconds = 90; advance_confirmed_seconds = 0; advancing = $false; advance_status = "stale"; advance_source = "x" }
Merge-MediaActivity -Activity $act -Media ([ordered]@{ pause_quiet = $true; pause_quiet_age_seconds = 30; media_active_seconds = 0 }) -NowUtc $t0m
Assert-Equal $act.advance_age_seconds 90 "merge: an engine stalled longer than the media test says keeps its longer age"
# nothing to say, nothing changes
$act = [ordered]@{ advance_alarm_confirmed = $true; advance_age_seconds = 5; advance_confirmed_seconds = 40; advancing = $true; advance_status = "advancing"; advance_source = "x" }
Merge-MediaActivity -Activity $act -Media $null -NowUtc $t0m
Assert-Equal $act.advance_age_seconds 5 "merge: no media reading leaves the engine result untouched"
$act = [ordered]@{ advance_alarm_confirmed = $false }
Merge-MediaActivity -Activity $act -Media ([ordered]@{ pause_quiet = $true; pause_quiet_age_seconds = 80; media_active_seconds = 0 }) -NowUtc $t0m
Assert-Equal $act.Contains("advance_age_seconds") $false "merge: an engine whose CPU could not be read is not given a claim by the media test"
# an unreadable reading wipes the history: no claim until the window is full again
$mu = @{}
foreach ($i in 1..6) { $null = Update-MediaActivity -State $mu -Feed "usa" -Cores 0.0 -ReadMBps 0.0 -IntervalSeconds 10 -MonoSeconds (2000 + 10 * $i) -QuietCores 0.15 -QuietReadMBps 0.05 }
$gone = Update-MediaActivity -State $mu -Feed "usa" -Cores $null -ReadMBps $null -IntervalSeconds 10 -MonoSeconds 2070 -QuietCores 0.15 -QuietReadMBps 0.05
Assert-Null $gone "media: an unreadable counter makes no claim"
$first = Update-MediaActivity -State $mu -Feed "usa" -Cores 0.0 -ReadMBps 0.0 -IntervalSeconds 10 -MonoSeconds 2080 -QuietCores 0.15 -QuietReadMBps 0.05
Assert-Null $first "media: after an unreadable cycle one reading is not enough to claim anything"
# clock steps: the tracker only ever sees the stopwatch, so a wall-clock correction in the middle changes nothing
$mc = @{}
foreach ($i in 1..6) { $st = Update-MediaActivity -State $mc -Feed "usa" -Cores 0.0 -ReadMBps 0.0 -IntervalSeconds 10 -MonoSeconds (3000 + 10 * $i) -QuietCores 0.15 -QuietReadMBps 0.05 }
Assert-Equal $st.pause_quiet_age_seconds 66 "media: quiet age counts stopwatch seconds (no wall clock is read at all)"
# counters to rates
$rc = @{}
Assert-Null (Get-MediaRates -Counters $rc -Key "a" -CpuSecondsTotal 100.0 -ReadBytesTotal 1000000.0 -MonoSeconds 500.0) "rates: the first look is only a baseline"
$r = Get-MediaRates -Counters $rc -Key "a" -CpuSecondsTotal 112.0 -ReadBytesTotal 21000000.0 -MonoSeconds 510.0
Assert-Equal $r.cores 1.2 "rates: CPU cores from two readings"
Assert-Equal $r.read_mbps 2 "rates: read MB/s from two readings"
Assert-Null (Get-MediaRates -Counters $rc -Key "a" -CpuSecondsTotal 5.0 -ReadBytesTotal 30000000.0 -MonoSeconds 520.0) "rates: a CPU counter that went backwards (restart) is only a new baseline"
Assert-Null (Get-MediaRates -Counters $rc -Key "a" -CpuSecondsTotal $null -ReadBytesTotal 1.0 -MonoSeconds 530.0) "rates: an unreadable counter gives nothing"
Assert-Equal $rc.ContainsKey("a") $false "rates: and forgets that key"

# --- Log hygiene: old logs are removed, a day's log is capped, a repeating failure is noted rarely
$lh = Join-Path ([IO.Path]::GetTempPath()) ("wd-log-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $lh | Out-Null
$lhNow = [datetime]::new(2026, 10, 11, 12, 0, 0)
foreach ($spec in @(@("heartbeat-2026-09-01.log", 40), @("heartbeat-2026-09-28.log", 13), @("heartbeat-2026-10-10.log", 1), @("notes.txt", 60))) {
    $p = Join-Path $lh $spec[0]
    Set-Content -LiteralPath $p -Value "x"
    (Get-Item -LiteralPath $p).LastWriteTime = $lhNow.AddDays(-$spec[1])
}
Assert-Equal (Remove-OldLogs -Dir $lh -KeepDays 14 -Now $lhNow) 1 "logs: only the one file older than 14 days is removed"
Assert-Equal (Test-Path -LiteralPath (Join-Path $lh "heartbeat-2026-09-01.log")) $false "logs: the 40-day-old file is gone"
Assert-Equal (Test-Path -LiteralPath (Join-Path $lh "heartbeat-2026-09-28.log")) $true "logs: a 13-day-old file is kept"
Assert-Equal (Test-Path -LiteralPath (Join-Path $lh "heartbeat-2026-10-10.log")) $true "logs: yesterday's file is kept"
Assert-Equal (Test-Path -LiteralPath (Join-Path $lh "notes.txt")) $true "logs: a file that is not a heartbeat log is never touched, however old"
Assert-Equal (Remove-OldLogs -Dir (Join-Path $lh "no-such-folder") -KeepDays 14 -Now $lhNow) 0 "logs: a missing folder is fine"
$big = Join-Path $lh "big.log"
[IO.File]::WriteAllBytes($big, (New-Object byte[] 2000))
Assert-Equal (Test-LogFull -Path $big -MaxBytes 1000) $true "logs: a file past the cap is full"
Assert-Equal (Test-LogFull -Path $big -MaxBytes 5000) $false "logs: a file under the cap is not"
Assert-Equal (Test-LogFull -Path (Join-Path $lh "absent.log") -MaxBytes 1000) $false "logs: a file that does not exist yet is not full"
$fl = @{}
$d = Get-FailureLogDecision -State $fl -MonoSeconds 1000
Assert-Equal $d.log $true "failure notes: the first failure is logged"
Assert-Equal $d.suppressed 0 "failure notes: nothing was skipped before it"
Assert-Equal (Get-FailureLogDecision -State $fl -MonoSeconds 1010).log $false "failure notes: the same failure 10 s later is not logged again"
Assert-Equal (Get-FailureLogDecision -State $fl -MonoSeconds 1290).log $false "failure notes: nor 290 s later"
$d = Get-FailureLogDecision -State $fl -MonoSeconds 1300
Assert-Equal $d.log $true "failure notes: 5 minutes later it is logged once more"
Assert-Equal $d.suppressed 2 "failure notes: and says how many were skipped"
Assert-Equal $LogKeepDays 14 "logs: 14 days are kept"
Assert-Equal $LogMaxBytes 1048576 "logs: a day's file is capped at 1 MB"
Remove-Item -LiteralPath $lh -Recurse -Force -ErrorAction SilentlyContinue

# --- Clock steps: correcting the PC's clock must never look like an engine stall
# (MAIN's clock was found 222 s slow on 2026-10-11; without this, fixing it would have raised a false critical alarm)
$clockStart = [datetime]::new(2026, 10, 11, 12, 0, 0, [DateTimeKind]::Utc)
$clockProc = $clockStart.AddHours(-5)
$mono = 5000.0
$clk = @{}
$null = Update-EngineActivity -State $clk -Feed "usa" -ProcessId 7 -ProcessStartUtc $clockProc -CpuSeconds 1000.0 -NowUtc $clockStart -MonoSeconds $mono
foreach ($i in 1..3) {
    $r = Update-EngineActivity -State $clk -Feed "usa" -ProcessId 7 -ProcessStartUtc $clockProc -CpuSeconds (1000.0 + 12 * $i) -NowUtc $clockStart.AddSeconds(10 * $i) -MonoSeconds ($mono + 10 * $i)
}
Assert-Equal $r.advance_confirmed_seconds 30 "clock-step setup: 30 s of steady activity"
# the PC's clock is stepped FORWARD by 222 s while the engine keeps working normally
$r = Update-EngineActivity -State $clk -Feed "usa" -ProcessId 7 -ProcessStartUtc $clockProc -CpuSeconds 1048.0 -NowUtc $clockStart.AddSeconds(40 + 222) -MonoSeconds ($mono + 40)
Assert-Equal $r.advancing $true "a 222 s forward clock step does not make a busy engine look idle"
Assert-Equal $r.advance_age_seconds 0 "and does not create a stall age"
Assert-Equal $r.engine_cpu_cores 1.2 "the CPU rate is still measured over the real 10 s"
Assert-Equal $r.advance_confirmed_seconds 40 "the steady-activity count carries on through the step"
# then stepped BACKWARD by 5 minutes
$r = Update-EngineActivity -State $clk -Feed "usa" -ProcessId 7 -ProcessStartUtc $clockProc -CpuSeconds 1060.0 -NowUtc $clockStart.AddSeconds(50 + 222 - 300) -MonoSeconds ($mono + 50)
Assert-Equal $r.advancing $true "a backward clock step does not either"
Assert-Equal $r.advance_age_seconds 0 "no stall age after a backward step"
# a REAL stall is still measured in real time, straight through a clock step
foreach ($i in 6..11) {
    $r = Update-EngineActivity -State $clk -Feed "usa" -ProcessId 7 -ProcessStartUtc $clockProc -CpuSeconds 1060.0 -NowUtc $clockStart.AddSeconds(1000 + 10 * $i) -MonoSeconds ($mono + 10 * $i)
}
Assert-Equal $r.advance_age_seconds 60 "a real 60 s stall is 60 s, not 60 plus the clock step"
Assert-Equal $r.advance_status "stale" "and it is still caught as stale"

if ($script:failures -gt 0) { Write-Host "$($script:failures) FAILED"; exit 1 }
Write-Host "ALL PASSED"
exit 0
