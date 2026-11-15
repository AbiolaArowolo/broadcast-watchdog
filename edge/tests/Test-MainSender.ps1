# File:         Test-MainSender.ps1
# Purpose:      Tests the pure logic of edge/main/insta_heartbeat_sender.ps1 (engine-activity tracking, process
#               pairing, feed identity) with synthetic processes. Touches no real process, file or network.
# Inputs:       none. Run: powershell -NoProfile -ExecutionPolicy Bypass -File edge\tests\Test-MainSender.ps1
# Outputs:      PASS/FAIL lines; exit code 0 when everything passed, 1 otherwise.
# Dependencies: the sender script, dot-sourced (its loop does not start when dot-sourced).
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$stateDir = Join-Path ([IO.Path]::GetTempPath()) ("wd-test-" + [Guid]::NewGuid().ToString("N"))
. (Join-Path $repo "edge\main\insta_heartbeat_sender.ps1") -StateDir $stateDir
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

$t0 = [datetime]::new(2026, 10, 10, 12, 0, 0, [DateTimeKind]::Utc)
$started = $t0.AddHours(-5)

# --- 1. A normal engine, then a stall, then a recovery
$state = @{}
$r = Update-EngineActivity -State $state -Feed "usa" -ProcessId 100 -ProcessStartUtc $started -CpuSeconds 1000.0 -NowUtc $t0
Assert-Equal $r.advance_alarm_confirmed $true "first look is a confirmed measurement"
Assert-Null $r.advancing "first look has no rate yet, so no claim of advancing"
Assert-Equal $r.advance_age_seconds 0 "first look starts the stall clock at zero"
for ($i = 1; $i -le 3; $i++) {
    $r = Update-EngineActivity -State $state -Feed "usa" -ProcessId 100 -ProcessStartUtc $started -CpuSeconds (1000.0 + 12 * $i) -NowUtc $t0.AddSeconds(10 * $i)
}
Assert-Equal $r.advancing $true "1.2 cores is advancing"
Assert-Equal $r.engine_cpu_cores 1.2 "reports the measured cores"
Assert-Equal $r.advance_confirmed_seconds 30 "30 s of steady activity so far"
Assert-Equal $r.advance_age_seconds 0 "busy engine has stall age 0"
Assert-Equal $r.advance_status "advancing" "status word while busy"
for ($i = 4; $i -le 10; $i++) {
    $r = Update-EngineActivity -State $state -Feed "usa" -ProcessId 100 -ProcessStartUtc $started -CpuSeconds 1036.0 -NowUtc $t0.AddSeconds(10 * $i)
    if ($i -eq 7) { Assert-Equal $r.advance_age_seconds 40 "40 s without activity" ; Assert-Equal $r.advance_status "advancing" "still under the 45 s ladder" }
    if ($i -eq 8) { Assert-Equal $r.advance_age_seconds 50 "50 s without activity"; Assert-Equal $r.advance_status "warning" "50 s is past 45 s" }
}
Assert-Equal $r.advance_age_seconds 70 "70 s without activity"
Assert-Equal $r.advance_status "stale" "70 s is past 60 s"
Assert-Equal $r.advancing $false "no activity is not advancing"
Assert-Equal $r.advance_confirmed_seconds 0 "steady count restarts at zero"

# --- 2. A restart does not reset the stall clock; fresh engines are not "confirmed"
$r = Update-EngineActivity -State $state -Feed "usa" -ProcessId 101 -ProcessStartUtc $t0.AddSeconds(105) -CpuSeconds 3.0 -NowUtc $t0.AddSeconds(110)
Assert-Equal $r.advance_age_seconds 80 "restart keeps the stall clock running (age 80, not 0)"
Assert-Null $r.advancing "a new process has no rate on its first look"
$r = Update-EngineActivity -State $state -Feed "usa" -ProcessId 101 -ProcessStartUtc $t0.AddSeconds(105) -CpuSeconds 15.0 -NowUtc $t0.AddSeconds(120)
Assert-Equal $r.advancing $true "restarted engine is busy loading"
Assert-Equal $r.advance_age_seconds 0 "stall clock resets once it is busy"
Assert-Equal $r.engine_uptime_seconds 15 "uptime is reported so the listener can refuse to clear on a restart"
Assert-Equal $r.advance_confirmed_seconds 10 "only 10 s of steady activity so far"

# --- 3. An unreadable CPU counter is no claim at all, and never looks like a stall
$r = Update-EngineActivity -State $state -Feed "usa" -ProcessId 101 -ProcessStartUtc $t0.AddSeconds(105) -CpuSeconds $null -NowUtc $t0.AddSeconds(130)
Assert-Equal $r.advance_alarm_confirmed $false "unreadable counter: not confirmed"
Assert-Null $r.advancing "unreadable counter: no claim"
$r = Update-EngineActivity -State $state -Feed "usa" -ProcessId 101 -ProcessStartUtc $t0.AddSeconds(105) -CpuSeconds 40.0 -NowUtc $t0.AddSeconds(140)
Assert-Null $r.advancing "after an unreadable cycle the next one is only a new baseline"

# --- 4. A counter that goes backwards and a zero-length interval are baselines, never negative rates
$state2 = @{}
$null = Update-EngineActivity -State $state2 -Feed "usa" -ProcessId 5 -ProcessStartUtc $started -CpuSeconds 500.0 -NowUtc $t0
$r = Update-EngineActivity -State $state2 -Feed "usa" -ProcessId 5 -ProcessStartUtc $started -CpuSeconds 100.0 -NowUtc $t0.AddSeconds(10)
Assert-Null $r.advancing "counter went backwards: baseline"
$r = Update-EngineActivity -State $state2 -Feed "usa" -ProcessId 5 -ProcessStartUtc $started -CpuSeconds 200.0 -NowUtc $t0.AddSeconds(10)
Assert-Null $r.advancing "zero-length interval: baseline"

# --- 5. Two players are measured independently
$state3 = @{}
foreach ($i in 0..3) {
    $now = $t0.AddSeconds(10 * $i)
    $usa = Update-EngineActivity -State $state3 -Feed "usa" -ProcessId 1 -ProcessStartUtc $started -CpuSeconds (10.0 * $i) -NowUtc $now
    $car = Update-EngineActivity -State $state3 -Feed "caribbean" -ProcessId 2 -ProcessStartUtc $started -CpuSeconds (14.0 * $i) -NowUtc $now
}
Assert-Equal $usa.engine_cpu_cores 1 "usa measured on its own"
Assert-Equal $car.engine_cpu_cores 1.4 "caribbean measured on its own"

# --- 6. Pairing launcher + engine processes
function New-Proc($id, $startOffset, $threads) {
    [PSCustomObject]@{ Id = $id; StartTime = $t0.ToLocalTime().AddSeconds($startOffset); Threads = $threads; CpuSeconds = 1.0 }
}
$procs = @((New-Proc 10 0 3), (New-Proc 11 2 120), (New-Proc 20 40 3), (New-Proc 21 41 110), (New-Proc 30 90 3), (New-Proc 31 91 100))
$pairs = @(Get-InstaPairs -Procs $procs)
Assert-Equal $pairs.Count 3 "six processes make three pairs"
Assert-Equal $pairs[0].EnginePid 11 "the big process is the engine"
Assert-Equal $pairs[0].LauncherPid 10 "the small process is the launcher"
$lone = @(Get-InstaPairs -Procs @((New-Proc 50 0 120)))
Assert-Equal $lone[0].Unpaired $true "an engine with no launcher is flagged unpaired"
Assert-Equal $lone[0].LauncherPid 0 "and has no launcher pid"

# --- 7. Feed identity: a dead player is absent, never replaced by its sibling
function New-Pair($launcher, $engine, $offset) {
    [PSCustomObject]@{ Key = "$launcher-$engine"; StartUtc = $t0.AddSeconds($offset); LauncherPid = $launcher; EnginePid = $engine; Engine = $null; Unpaired = $false }
}
$a = New-Pair 10 11 0
$b = New-Pair 20 21 40
$first = Resolve-FeedBindings -Pairs @($a, $b) -Bindings @{}
Assert-Equal $first.assigned["usa"].EnginePid 11 "first sight: earliest start is usa"
Assert-Equal $first.assigned["caribbean"].EnginePid 21 "first sight: second start is caribbean"
$usaDied = Resolve-FeedBindings -Pairs @($b) -Bindings $first.bindings
Assert-Equal $usaDied.assigned.Contains("usa") $false "usa died: usa is absent"
Assert-Equal $usaDied.assigned["caribbean"].EnginePid 21 "usa died: the survivor is still caribbean"
$c = New-Pair 30 31 500
$usaBack = Resolve-FeedBindings -Pairs @($b, $c) -Bindings $usaDied.bindings
Assert-Equal $usaBack.assigned["usa"].EnginePid 31 "a new process fills the empty usa slot"
Assert-Equal $usaBack.assigned["caribbean"].EnginePid 21 "caribbean keeps its own process"
$d = New-Pair 40 41 600
$crowded = Resolve-FeedBindings -Pairs @($b, $c, $d) -Bindings $usaBack.bindings
Assert-Equal $crowded.extra 1 "a third pair is counted but not assigned"
$launcherOnly = New-Pair 20 0 40
$engineLost = Resolve-FeedBindings -Pairs @($launcherOnly) -Bindings $first.bindings
Assert-Equal $engineLost.assigned["caribbean"].EnginePid 0 "engine died, launcher alive: still caribbean, engine absent"

# --- 8. The bindings survive a restart of the sender itself
$path = Join-Path $stateDir "feed-bindings.json"
Save-FeedBindings -Path $path -Bindings $first.bindings
$reloaded = Read-FeedBindings -Path $path
$afterRestart = Resolve-FeedBindings -Pairs @($b) -Bindings $reloaded
Assert-Equal $afterRestart.assigned.Contains("usa") $false "after a sender restart with usa dead: usa is still absent"
Assert-Equal $afterRestart.assigned["caribbean"].EnginePid 21 "after a sender restart: the survivor is still caribbean"
Assert-Equal (Read-FeedBindings -Path (Join-Path $stateDir "missing.json")).Count 0 "no saved file means no bindings"
Set-Content -LiteralPath (Join-Path $stateDir "broken.json") -Value "{not json"
Assert-Equal (Read-FeedBindings -Path (Join-Path $stateDir "broken.json")).Count 0 "a corrupt file falls back to start order, never crashes"

# --- 9. Now playing comes from the Olive row of the channel's own playlist
function New-PlaylistItem($no, $start, $name, $dur, $colour) {
    "<Item><Text>$no</Text><SubItem_0>$no</SubItem_0><SubItem_1>$start</SubItem_1><SubItem_2>$name</SubItem_2><SubItem_5>$dur</SubItem_5><SubItem_11>D:\media\$name</SubItem_11><BackgroundColor>$colour</BackgroundColor></Item>"
}
# the real shape seen on MAIN at 20:11 on 2026-10-10: the finished row, the Olive (on air) row, the SteelBlue (cued) row
$live = "<Items>" + (New-PlaylistItem 38 "19:47:39" "VUJALOK.mp4" "00:18:18" "BurlyWood") +
    (New-PlaylistItem 39 "20:06:27" "DUWEW NIV WUT. 4.mp4" "00:26:42" "Olive") +
    (New-PlaylistItem 40 "20:33:09" "WABINI KEMUMASID PABID 2 .mp4" "00:04:25" "SteelBlue") + "</Items>"
$np = Get-InstaNowPlayingFromXml -Xml $live
Assert-Equal $np.filename "DUWEW NIV WUT. 4.mp4" "the Olive row is the file on air (not the last row, not the finished one)"
Assert-Equal $np.result "Started 20:06:27" "reports when it started"
Assert-Equal $np.duration "00:26:42" "reports how long it runs"
# the progress bar: the start (UTC), the length in seconds and what is cued next, all from the same playlist
# the playlist's clock is this machine's wall clock: take "now" as 20:10:00 on 2026-10-10 local
$localNow = [datetime]::SpecifyKind([datetime]::ParseExact("10.10.2026 20:10:00", "dd.MM.yyyy HH:mm:ss", [Globalization.CultureInfo]::InvariantCulture), [DateTimeKind]::Local)
$npTimed = Get-InstaNowPlayingFromXml -Xml $live -NowUtc $localNow.ToUniversalTime()
Assert-Equal $npTimed.duration_s 1602 "the length 00:26:42 is 1602 seconds"
Assert-Equal ([string]$npTimed.started_utc).EndsWith("Z") $true "the start is reported in UTC"
Assert-Equal ([datetime]$npTimed.started_utc).ToLocalTime().ToString("dd.MM.yyyy HH:mm:ss") "10.10.2026 20:06:27" "the playlist's 20:06:27 is read as this machine's wall clock today"
Assert-Equal $npTimed.next "WABINI KEMUMASID PABID 2 .mp4" "the cued row after the one on air is the next item"
Assert-Equal $npTimed.result "Started 20:06:27" "the old result text is still there for older listeners"
# an item that began before midnight, seen just after midnight, started YESTERDAY
$afterMidnight = [datetime]::SpecifyKind([datetime]::ParseExact("11.10.2026 00:05:00", "dd.MM.yyyy HH:mm:ss", [Globalization.CultureInfo]::InvariantCulture), [DateTimeKind]::Local)
$overnight = "<Items>" + (New-PlaylistItem 7 "23:58:00" "LATE FILM.mp4" "02:00:00" "Olive") + "</Items>"
$npOvernight = Get-InstaNowPlayingFromXml -Xml $overnight -NowUtc $afterMidnight.ToUniversalTime()
Assert-Equal ([datetime]$npOvernight.started_utc).ToLocalTime().ToString("dd.MM.yyyy HH:mm:ss") "10.10.2026 23:58:00" "after midnight an item that started at 23:58 began the day before"
Assert-Null $npOvernight.next "nothing cued after it: no next item is invented"
$badClock = "<Items>" + (New-PlaylistItem 1 "99:99:99" "A.mp4" "00:aa:00" "Olive") + "</Items>"
$npBad = Get-InstaNowPlayingFromXml -Xml $badClock -NowUtc $localNow.ToUniversalTime()
Assert-Null $npBad.started_utc "an unreadable start gives no start"
Assert-Null $npBad.duration_s "an unreadable length gives no length"
Assert-Equal $npBad.filename "A.mp4" "and the file name is still reported"
Assert-Equal (Get-InstaNowPlayingFromXml -Xml $live).filename "DUWEW NIV WUT. 4.mp4" "called without a time (as before) it still works"
# Found 2026-10-15: from midnight MAIN's Caribbean channel plays Insta's FileBlock schedule, the playlist file is no longer rewritten, and its last
# on-air row (23:57:41, 9 min 49 s long, so over at 00:07:30) stayed "on air" for ten hours. A row more than 15 minutes past its own end is not on air.
function New-LocalNow($text) { [datetime]::SpecifyKind([datetime]::ParseExact($text, "dd.MM.yyyy HH:mm:ss", [Globalization.CultureInfo]::InvariantCulture), [DateTimeKind]::Local).ToUniversalTime() }
$ghost = "<Items>" + (New-PlaylistItem 75 "23:57:41" "MUDI TATUTA ZULA DOVI.mp4" "00:09:49" "Olive") + (New-PlaylistItem 76 "00:07:30" "Station.mp4" "00:00:07" "SteelBlue") + "</Items>"
Assert-Null (Get-InstaNowPlayingFromXml -Xml $ghost -NowUtc (New-LocalNow "15.10.2026 10:35:00")) "a row ten hours past its end is not on air: nothing is reported, not a finished item"
Assert-Equal (Get-InstaNowPlayingFromXml -Xml $ghost -NowUtc (New-LocalNow "15.10.2026 00:03:00")).filename "MUDI TATUTA ZULA DOVI.mp4" "the same row, four minutes before its end, is on air"
Assert-Equal (Get-InstaNowPlayingFromXml -Xml $ghost -NowUtc (New-LocalNow "15.10.2026 00:17:30")).filename "MUDI TATUTA ZULA DOVI.mp4" "ten minutes past the end (the playlist is rewritten late, or the item was paused) it is still reported"
Assert-Null (Get-InstaNowPlayingFromXml -Xml $ghost -NowUtc (New-LocalNow "15.10.2026 00:24:00")) "sixteen and a half minutes past the end it is not"
$noLength = "<Items>" + (New-PlaylistItem 1 "10:00:00" "NoLength.mp4" "" "Olive") + "</Items>"
Assert-Equal (Get-InstaNowPlayingFromXml -Xml $noLength -NowUtc (New-LocalNow "15.10.2026 20:00:00")).filename "NoLength.mp4" "a row with no length cannot be judged old, so it is still reported"
$cuedOnly = "<Items>" + (New-PlaylistItem 1 "10:00:00" "A.mp4" "00:01:00" "BurlyWood") + (New-PlaylistItem 2 "10:01:00" "B.mp4" "00:01:00" "SteelBlue") + "</Items>"
Assert-Null (Get-InstaNowPlayingFromXml -Xml $cuedOnly) "no Olive row: nothing is reported, never a guess"
Assert-Null (Get-InstaNowPlayingFromXml -Xml "") "empty file: nothing"
Assert-Null (Get-InstaNowPlayingFromXml -Xml "<Items><Item><SubItem_2>half writ") "a half-written file: nothing"
$escaped = "<Items>" + (New-PlaylistItem 1 "10:00:00" "R&amp;B Mix.mp4" "00:03:00" "Olive") + "</Items>"
Assert-Equal (Get-InstaNowPlayingFromXml -Xml $escaped).filename "R&B Mix.mp4" "XML entities are decoded"
$two = "<Items>" + (New-PlaylistItem 1 "10:00:00" "Old.mp4" "00:03:00" "Olive") + (New-PlaylistItem 2 "10:03:00" "New.mp4" "00:03:00" "Olive") + "</Items>"
Assert-Equal (Get-InstaNowPlayingFromXml -Xml $two).filename "New.mp4" "if two rows were ever Olive, the later one wins"
$lastItemOfDay = "<Items>" + (New-PlaylistItem 1 "10:00:00" "Playing.mp4" "00:30:00" "Olive") + (New-PlaylistItem 2 "10:30:00" "Last of the day.mp4" "00:30:00" "BurlyWood") + "</Items>"
Assert-Equal (Get-InstaNowPlayingFromXml -Xml $lastItemOfDay).filename "Playing.mp4" "the old bug: the last row of the day is NOT the file on air"

# the file read, and what happens when Insta is rewriting it
$root = Join-Path $stateDir "Insta Playout"
New-Item -ItemType Directory -Path (Join-Path $root "Settings") -Force | Out-Null
$playlistPath = Join-Path $root "Settings\Mainplaylist.xml"
Set-Content -LiteralPath $playlistPath -Value $live -Encoding UTF8
$script:NowPlayingCache = @{}
$fixtureNow = New-LocalNow "10.10.2026 20:10:00"   # the playlist fixture is from 2026-10-10: judge it at a time inside its on-air row
Assert-Equal (Get-InstaNowPlaying -Feed "usa" -RootPath $root -NowUtc $fixtureNow).filename "DUWEW NIV WUT. 4.mp4" "reads the playlist file from the channel folder"
$lock = [IO.File]::Open($playlistPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
Assert-Equal (Get-InstaNowPlaying -Feed "usa" -RootPath $root -NowUtc $fixtureNow.AddSeconds(10)).filename "DUWEW NIV WUT. 4.mp4" "file locked mid-rewrite: the last good answer is kept for a minute"
# Two minutes after the fixture's own clock (NOT after $t0: the fixture's "now" is a LOCAL time converted to UTC, so measuring from the fixed $t0 only worked on a machine
# whose time zone happened to be about UTC+9 or further east, and failed on the UTC build runner).
Assert-Null (Get-InstaNowPlaying -Feed "usa" -RootPath $root -NowUtc $fixtureNow.AddSeconds(120)) "still unreadable after two minutes: the tile goes blank rather than show stale data"
$lock.Close()
Assert-Null (Get-InstaNowPlaying -Feed "caribbean" -RootPath (Join-Path $stateDir "no-such-channel") -NowUtc $t0) "a channel folder that does not exist: nothing, no error"

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

# --- MAIN: the media servers of each Insta engine
function New-MServer($id, $parent, $user100ns, $readBytes) { [PSCustomObject]@{ ProcessId = $id; ParentProcessId = $parent; KernelModeTime = 0; UserModeTime = $user100ns; ReadTransferCount = $readBytes } }
$cc = @{}
$look1 = Get-MServerRates -Counters $cc -Children @((New-MServer 11 100 0 0), (New-MServer 12 100 0 0), (New-MServer 21 200 0 0)) -MonoSeconds 100.0
Assert-Equal $look1.ready $false "mservers: the first look claims nothing"
$look2 = Get-MServerRates -Counters $cc -Children @((New-MServer 11 100 20000000 10000000), (New-MServer 12 100 20000000 10000000), (New-MServer 21 200 0 0)) -MonoSeconds 110.0
Assert-Equal $look2.ready $true "mservers: ready from the second look"
Assert-Equal $look2.dt 10 "mservers: interval in stopwatch seconds"
Assert-Equal $look2.engines[100].cores 0.4 "mservers: two children of one engine are summed (cores)"
Assert-Equal $look2.engines[100].read_mbps 2 "mservers: two children of one engine are summed (reads)"
Assert-Equal $look2.engines[200].read_mbps 0 "mservers: a child that reads nothing reads 0.00 (a paused channel)"
Assert-Equal $look2.engines.ContainsKey(300) $false "mservers: an engine with no children is simply absent (the caller reads zero)"
$look3 = Get-MServerRates -Counters $cc -Children @((New-MServer 11 100 40000000 20000000), (New-MServer 31 100 0 0)) -MonoSeconds 120.0
Assert-Equal $look3.engines[100].read_mbps 1 "mservers: a brand-new child is a baseline, an exited child is dropped"
Assert-Equal $cc.ContainsKey("ms12") $false "mservers: state of an exited child is forgotten"
$look4 = Get-MServerRates -Counters $cc -Children $null -MonoSeconds 130.0
Assert-Equal $look4.ready $false "mservers: a failed query claims nothing"
Assert-Equal $cc.Count 0 "mservers: and forgets everything"

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

# --- 10. FileBlock hours (found 2026-10-15): from midnight MAIN's Caribbean channel plays Insta's day schedule instead of the playlist. The schedule
# (FileBlock\<start>-<weekday>,<month><day>,<year>\Schedule.xml, UTF-16) lists every item with its length, played back to back from the stated start; the
# sender works out which item is on air from the clock and says so is an ESTIMATE (it could not be checked against the screen).
function New-FileBlockChannel($root, $dayDate, $items, [string]$state = "ON", [string]$startText = "12:00 AM", [int]$statedTotal = -1, [string]$folderStart = "1200AM", [string]$folderDay = "", [string]$totalLine = "") {
    # $totalLine pins the LITERAL text of Schedule.txt (read off MAIN). Without it the line is built here, which can only ever produce the "hh:mm:ss" form: the format of a schedule over
    # 24 hours ("1.00:11:37") cannot come out of that helper, which is how the 2026-11-08 bug got past these tests.
    # Insta zero-pads the day: "Sunday,November08,2026". $folderDay pins a literal name (the real ones read off MAIN); without it the name is built here, padded, NOT with the sender's own format string.
    $dayTag = $folderDay
    if ($dayTag -eq "") { $dayTag = $dayDate.ToString("dddd,MMMM", [Globalization.CultureInfo]::InvariantCulture) + $dayDate.ToString("dd", [Globalization.CultureInfo]::InvariantCulture) + "," + $dayDate.ToString("yyyy", [Globalization.CultureInfo]::InvariantCulture) }
    $fbDir = Join-Path $root "FileBlock"
    $folder = Join-Path $fbDir ($folderStart + "-" + $dayTag)
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $fbDir "settings.txt") -Value $state
    $xml = "<m_config path='MPlaylist'>"
    $sum = 0.0
    foreach ($it in $items) {
        $q = "'"
        if ($it.name.Contains("'")) { $q = '"' }   # Insta double-quotes a path that contains an apostrophe
        $xml += "<file in='0.0' path=" + $q + "D:\media\" + $it.name + $q + " start_timecode='' img.default_duration='3600.0' output.10bit='false' duration='" + $it.dur.ToString([Globalization.CultureInfo]::InvariantCulture) + "' loop='false' ref_id='0x1'><info streams='2' duration='1.5' size='1'/><mitem_props><file/></mitem_props></file>"
        $sum += $it.dur
    }
    $xml += "</m_config>"
    [IO.File]::WriteAllText((Join-Path $folder "Schedule.xml"), $xml, [Text.Encoding]::Unicode)
    $stated = [int][math]::Round($sum)
    if ($statedTotal -ge 0) { $stated = $statedTotal }
    $totalValue = "Total duration: " + [TimeSpan]::FromSeconds($stated).ToString("hh\:mm\:ss")
    if ($totalLine -ne "") { $totalValue = $totalLine }
    Set-Content -LiteralPath (Join-Path $folder "Schedule.txt") -Value $totalValue
    Set-Content -LiteralPath (Join-Path $folder "Scheduledatetime.xml") -Value ("<?xml version=`"1.0`" encoding=`"utf-8`"?><FileSchedule><Entry><StartTime>" + $startText + "</StartTime><EndTime>11:59:24 PM</EndTime></Entry></FileSchedule>")
    return $folder
}
$day = [datetime]::ParseExact("15.10.2026", "dd.MM.yyyy", [Globalization.CultureInfo]::InvariantCulture)
$blockItems = @(
    @{ name = "Station.mp4"; dur = 7.0 },
    @{ name = "Delogatum Pasesujuk Hed 1.mp4"; dur = 7247.24 },
    @{ name = "DJ'G DUWO KAHUK.mp4"; dur = 1500.5 },
    @{ name = "Last.mp4"; dur = 600.0 }
)
$fbRoot = Join-Path $stateDir "FileBlock Channel"
New-FileBlockChannel $fbRoot $day $blockItems | Out-Null
$script:FileBlockCache = @{}
$b0 = Get-InstaFileBlockNowPlaying -RootPath $fbRoot -NowUtc (New-LocalNow "15.10.2026 00:00:03")
Assert-Equal $b0.filename "Station.mp4" "at 00:00:03 the first item of the day's schedule is on air"
Assert-Equal $b0.duration_s 7 "with its length from the schedule"
Assert-Equal $b0.estimated $true "and it is marked as an estimate"
Assert-Equal ([datetime]$b0.started_utc).ToLocalTime().ToString("dd.MM.yyyy HH:mm:ss") "15.10.2026 00:00:00" "which started at the schedule's start (UTC in the report, local on this machine)"
$b1 = Get-InstaFileBlockNowPlaying -RootPath $fbRoot -NowUtc (New-LocalNow "15.10.2026 00:00:08")
Assert-Equal $b1.filename "Delogatum Pasesujuk Hed 1.mp4" "eight seconds in, the second item"
Assert-Equal $b1.duration_s 7247.2 "its length (rounded to a tenth)"
Assert-Equal ([datetime]$b1.started_utc).ToLocalTime().ToString("HH:mm:ss") "00:00:07" "started when the 7 s item before it ended"
Assert-Equal $b1.next "DJ'G DUWO KAHUK.mp4" "the next item is named (a path with an apostrophe is double-quoted in the file and still read)"
Assert-Equal (Get-InstaFileBlockNowPlaying -RootPath $fbRoot -NowUtc (New-LocalNow "15.10.2026 02:05:00")).filename "DJ'G DUWO KAHUK.mp4" "at 02:05 (7500 s in) the third item"
$b3 = Get-InstaFileBlockNowPlaying -RootPath $fbRoot -NowUtc (New-LocalNow "15.10.2026 02:30:00")
Assert-Equal $b3.filename "Last.mp4" "the last item of the day"
Assert-Null $b3.next "and nothing after it is invented"
Assert-Null (Get-InstaFileBlockNowPlaying -RootPath $fbRoot -NowUtc (New-LocalNow "15.10.2026 02:40:00")) "after the schedule's total the block is over: nothing is reported"
Assert-Null (Get-InstaFileBlockNowPlaying -RootPath $fbRoot -NowUtc (New-LocalNow "16.10.2026 00:00:03")) "tomorrow has no schedule folder yet: nothing is reported"
New-FileBlockChannel $fbRoot $day $blockItems "OFF" | Out-Null
Assert-Null (Get-InstaFileBlockNowPlaying -RootPath $fbRoot -NowUtc (New-LocalNow "15.10.2026 00:00:08")) "FileBlock switched OFF: the schedule is not what is playing"
New-FileBlockChannel $fbRoot $day $blockItems "ON" | Out-Null
$second = New-FileBlockChannel $fbRoot $day $blockItems "ON" "12:00 AM" -1 "0600AM"
Assert-Null (Get-InstaFileBlockNowPlaying -RootPath $fbRoot -NowUtc (New-LocalNow "15.10.2026 00:00:08")) "two schedules for the same day: it cannot tell which is on, so it reports nothing"
Remove-Item -LiteralPath $second -Recurse -Force
$fbBad = Join-Path $stateDir "FileBlock Bad Total"
New-FileBlockChannel $fbBad $day $blockItems "ON" "12:00 AM" 500 | Out-Null
$script:FileBlockCache = @{}
Assert-Null (Get-InstaFileBlockNowPlaying -RootPath $fbBad -NowUtc (New-LocalNow "15.10.2026 00:00:08")) "a schedule whose items do not add up to the total it states is not trusted"
$fbLate = Join-Path $stateDir "FileBlock Six AM"
New-FileBlockChannel $fbLate $day $blockItems "ON" "6:00 AM" | Out-Null
$script:FileBlockCache = @{}
Assert-Equal (Get-InstaFileBlockNowPlaying -RootPath $fbLate -NowUtc (New-LocalNow "15.10.2026 06:00:03")).filename "Station.mp4" "a schedule that starts at 6:00 AM starts its first item at 6:00"
Assert-Null (Get-InstaFileBlockNowPlaying -RootPath $fbLate -NowUtc (New-LocalNow "15.10.2026 00:00:03")) "and before 6:00 it is not on"
Assert-Null (Get-InstaFileBlockNowPlaying -RootPath (Join-Path $stateDir "no such channel") -NowUtc (New-LocalNow "15.10.2026 00:00:03")) "a channel with no FileBlock folder: nothing, no error"
$fbBroken = Join-Path $stateDir "FileBlock Broken"
$brokenFolder = New-FileBlockChannel $fbBroken $day $blockItems
[IO.File]::WriteAllText((Join-Path $brokenFolder "Schedule.xml"), "<m_config><file in='0.0' path='half writ", [Text.Encoding]::Unicode)
$script:FileBlockCache = @{}
Assert-Null (Get-InstaFileBlockNowPlaying -RootPath $fbBroken -NowUtc (New-LocalNow "15.10.2026 00:00:08")) "a damaged schedule file: nothing, no error"

# --- 10b. A schedule LONGER THAN 24 HOURS (found 2026-11-08 on MAIN USA, read off the PC): Insta wrote "Total duration: 1.00:11:37" (1 day, 11 min 37 s) and its 97 items added up to
# 87129.6 s (24:12:09). The sender read only "00:11:37" = 697 s, saw items far from the stated total, trusted nothing, and MAIN USA showed no item and no progress bar all day. The
# items below are shortened to four with the same total; the folder name and the total line are the real, literal text. (The helper's own "hh:mm:ss" form could never have made this.)
$day8 = [datetime]::ParseExact("08.11.2026", "dd.MM.yyyy", [Globalization.CultureInfo]::InvariantCulture)
$longItems = @(
    @{ name = "C_Station.mp4"; dur = 7.0 },
    @{ name = "R_Rugomevocirik Tok Hetur Hed 1 (3jag).mp4"; dur = 10221.12 },
    @{ name = "R_COLIBEHO GIWUSO NABOSAHUZ Lokajo.mp4"; dur = 3518.72 },
    @{ name = "R_Long Filler.mp4"; dur = 73382.76 }
)
$fbLong = Join-Path $stateDir "FileBlock Over 24 Hours"
New-FileBlockChannel $fbLong $day8 $longItems "ON" "12:00 AM" -1 "1200AM" "Sunday,November08,2026" "Total duration: 1.00:11:37" | Out-Null
$script:FileBlockCache = @{}
$l0 = Get-InstaFileBlockNowPlaying -RootPath $fbLong -NowUtc (New-LocalNow "08.11.2026 07:25:00")
Assert-Equal $l0.filename "R_Long Filler.mp4" "a schedule over 24 hours (total written 1.00:11:37) is trusted: at 07:25 (26700 s in) the fourth item is on air"
Assert-Equal $l0.estimated $true "and it is still marked as an estimate"
Assert-Equal (Get-InstaFileBlockNowPlaying -RootPath $fbLong -NowUtc (New-LocalNow "08.11.2026 00:00:03")).filename "C_Station.mp4" "and its first item is on air at 00:00:03"
Assert-Equal (Get-InstaFileBlockNowPlaying -RootPath $fbLong -NowUtc (New-LocalNow "08.11.2026 03:00:00")).filename "R_COLIBEHO GIWUSO NABOSAHUZ Lokajo.mp4" "and at 03:00 (10800 s in) the third"
$fbLongWrong = Join-Path $stateDir "FileBlock Over 24 Hours Wrong Day"
New-FileBlockChannel $fbLongWrong $day8 $longItems "ON" "12:00 AM" -1 "1200AM" "Sunday,November08,2026" "Total duration: 2.00:11:37" | Out-Null
$script:FileBlockCache = @{}
Assert-Null (Get-InstaFileBlockNowPlaying -RootPath $fbLongWrong -NowUtc (New-LocalNow "08.11.2026 07:25:00")) "the day part is really counted: 2.00:11:37 does not match the items, so the schedule is not trusted"
$fbShortDay = Join-Path $stateDir "FileBlock Day Zero Form"
New-FileBlockChannel $fbShortDay $day8 $blockItems "ON" "12:00 AM" -1 "1200AM" "Sunday,November08,2026" "Total duration: 02:35:55" | Out-Null
$script:FileBlockCache = @{}
Assert-Equal (Get-InstaFileBlockNowPlaying -RootPath $fbShortDay -NowUtc (New-LocalNow "08.11.2026 00:00:03")).filename "Station.mp4" "a schedule under 24 hours (02:35:55, no day part) is still read as before"

# the playlist stays the first choice; the schedule is the fallback when the playlist's on-air row is long over (the 2026-10-15 ghost)
$chan = Join-Path $stateDir "Channel Two"
New-Item -ItemType Directory -Path (Join-Path $chan "Settings") -Force | Out-Null
New-FileBlockChannel $chan $day $blockItems | Out-Null
$script:FileBlockCache = @{}
Set-Content -LiteralPath (Join-Path $chan "Settings\Mainplaylist.xml") -Value $ghost -Encoding UTF8
$script:NowPlayingCache = @{}
$viaSchedule = Get-InstaNowPlaying -Feed "caribbean" -RootPath $chan -NowUtc (New-LocalNow "15.10.2026 02:05:00")
Assert-Equal $viaSchedule.filename "DJ'G DUWO KAHUK.mp4" "playlist row ten hours old + FileBlock on: the schedule's item is reported"
Assert-Equal $viaSchedule.estimated $true "marked as an estimate"
$script:NowPlayingCache = @{}
Assert-Equal (Get-InstaNowPlaying -Feed "caribbean" -RootPath $chan -NowUtc (New-LocalNow "15.10.2026 00:03:00")).filename "MUDI TATUTA ZULA DOVI.mp4" "a playlist row still on air wins over the schedule"
New-FileBlockChannel $chan $day $blockItems "OFF" | Out-Null
$script:NowPlayingCache = @{}
Assert-Null (Get-InstaNowPlaying -Feed "caribbean" -RootPath $chan -NowUtc (New-LocalNow "15.10.2026 02:05:00")) "stale playlist row and FileBlock off: nothing is reported"

# --- FileBlock folder names (2026-11-08). Insta zero-pads the day of the month in the folder name ("1200AM-Sunday,November08,2026"). The sender looked for "November8",
# so on days 1 to 9 of a month its FileBlock estimate could never be found, and MAIN USA showed "No file reported" through a whole FileBlock night. The tests used only 15 Oct
# (two digits, where padded and unpadded are the same) and built their fixture with the sender's own format string, so they agreed with the bug. These names are the real ones read off MAIN.
$realFolderDays = @(
    @{ day = "08.11.2026"; name = "Sunday,November08,2026" },
    @{ day = "01.11.2026"; name = "Sunday,November01,2026" },
    @{ day = "09.10.2026"; name = "Friday,October09,2026" },
    @{ day = "06.10.2026"; name = "Tuesday,October06,2026" },
    @{ day = "15.10.2026"; name = "Thursday,October15,2026" },
    @{ day = "28.10.2026"; name = "Wednesday,October28,2026" }
)
foreach ($case in $realFolderDays) {
    $caseDate = [datetime]::ParseExact($case.day, "dd.MM.yyyy", [Globalization.CultureInfo]::InvariantCulture)
    $caseRoot = Join-Path $stateDir ("Real folder " + $case.day)
    New-FileBlockChannel $caseRoot $caseDate $blockItems "ON" "12:00 AM" -1 "1200AM" $case.name | Out-Null
    $script:FileBlockCache = @{}
    $caseFound = Get-InstaFileBlockNowPlaying -RootPath $caseRoot -NowUtc (New-LocalNow ($case.day + " 00:00:03"))
    Assert-Equal $caseFound.filename "Station.mp4" ("the folder Insta really names '" + $case.name + "' is found on its own day")
}
$otherDayRoot = Join-Path $stateDir "Other day folder"
New-FileBlockChannel $otherDayRoot ([datetime]::ParseExact("18.11.2026", "dd.MM.yyyy", [Globalization.CultureInfo]::InvariantCulture)) $blockItems "ON" "12:00 AM" -1 "1200AM" "Sunday,November18,2026" | Out-Null
$script:FileBlockCache = @{}
Assert-Null (Get-InstaFileBlockNowPlaying -RootPath $otherDayRoot -NowUtc (New-LocalNow "08.11.2026 00:00:03")) "another day's folder (the 18th) is never taken for the 8th"
# --- Item-change wait (2026-11-08). MAIN used to sleep a flat 10 s between looks at the playlist, so every item reached Core 8 to 16 s after it started
# (measured live on 2026-11-07) and a 7 s ident only after it had ended. The wait now ends early when Insta rewrites the playlist or status file, and only once
# the new state has held for a second look, so a file that is still being written is never read half done.
$watchDir = Join-Path $stateDir "watch"
New-Item -ItemType Directory -Path $watchDir -Force | Out-Null
$watchPlaylist = Join-Path $watchDir "Mainplaylist.xml"
$watchStatus = Join-Path $watchDir "runningstatus.txt"
$sigNone = Get-ItemChangeSignature -Paths @($watchPlaylist, $watchStatus)
Assert-Equal $sigNone "missing|missing" "two files that do not exist have a fixed 'missing' signature and raise no error"
Set-Content -LiteralPath $watchPlaylist -Value "one" -Encoding UTF8
$sigOne = Get-ItemChangeSignature -Paths @($watchPlaylist, $watchStatus)
Assert-Equal ($sigOne -ne $sigNone) $true "a file appearing changes the signature"
Assert-Equal (Get-ItemChangeSignature -Paths @($watchPlaylist, $watchStatus)) $sigOne "an untouched file keeps the same signature"
$watchItem = Get-Item -LiteralPath $watchPlaylist
$watchItem.LastWriteTimeUtc = $watchItem.LastWriteTimeUtc.AddSeconds(3)
Assert-Equal ((Get-ItemChangeSignature -Paths @($watchPlaylist, $watchStatus)) -ne $sigOne) $true "a rewrite of the same size at a later time changes the signature"
Assert-Equal (Get-ItemChangeSignature -Paths @("")) "unreadable" "a path that cannot even be looked at is 'unreadable' (its own answer), never an error and never 'missing'"

function Invoke-FakeWait {
    # Runs the real wait against a fake clock: each sleep moves the clock by exactly what was asked, and each look at the files returns the next scripted signature (the last one repeats).
    param($Seen, [double]$Interval = 10, $Baseline = "A")
    $script:fakeNow = 100.0
    $script:fakeSeen = @($Seen)
    $script:fakeLooks = 0
    $clock = { $script:fakeNow }
    $sleep = { param($seconds) $script:fakeNow += $seconds }
    $look = { $i = [math]::Min($script:fakeLooks, $script:fakeSeen.Count - 1); $script:fakeLooks++; $script:fakeSeen[$i] }
    $reason = Wait-ForItemChangeOrInterval -Baseline $Baseline -IntervalSeconds $Interval -GetSignature $look -Sleep $sleep -Clock $clock
    return @{ reason = $reason; waited = ($script:fakeNow - 100.0) }
}
$w = Invoke-FakeWait -Seen @("A")
Assert-Equal $w.reason "interval" "nothing changes: the wait runs the whole interval"
Assert-Equal $w.waited 10 "and lasts exactly the 10 s it always did, so the heartbeat cadence is unchanged"
$w = Invoke-FakeWait -Seen @("A", "A", "B", "B")
Assert-Equal $w.reason "item-change" "a change that holds for a second look ends the wait"
Assert-Equal $w.waited 4 "after the look that saw it and the look that confirmed it (1 s apart)"
$w = Invoke-FakeWait -Seen @("B")
Assert-Equal $w.reason "item-change" "a change that happened while the cycle was still working is seen at once"
Assert-Equal $w.waited 2 "and costs only the two looks that confirm it"
$w = Invoke-FakeWait -Seen @("A", "B", "A")
Assert-Equal $w.reason "interval" "a change seen once and gone on the next look is not an item change"
$w = Invoke-FakeWait -Seen @("B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M")
Assert-Equal $w.reason "interval" "a file that never settles (still being written) never ends the wait early"
Assert-Equal $w.waited 10 "so the worst case is the old 10 s cadence, not a flood of heartbeats"
$w = Invoke-FakeWait -Seen @("B") -Baseline $null
Assert-Equal $w.reason "interval" "no baseline (the files could not be looked at before the cycle): nothing to compare, the full interval"
$w = Invoke-FakeWait -Seen @("B") -Interval 0.5
Assert-Equal $w.waited 0.5 "an interval shorter than one look is slept exactly, never overshot"
Assert-Equal $w.reason "interval" "and cannot be cut short"
# The wait composed with main's real one-second step, exactly as the loop uses it (Wait-WithSiteCDrain sleeps a second and empties the Site C sockets; none are open here).
$composedStart = Get-Date
$script:composedLooks = 0
$composedLook = { $script:composedLooks++; "B" }
$composedReason = Wait-ForItemChangeOrInterval -Baseline "A" -IntervalSeconds 10 -GetSignature $composedLook -Sleep { param($seconds) Wait-WithSiteCDrain -Seconds ([math]::Max(1, [int][math]::Round($seconds))) }
Assert-Equal $composedReason "item-change" "composed with the real drain step, a held change still ends the wait"
Assert-Equal ($script:composedLooks -eq 2) $true "after exactly two looks, one second apart"
Assert-Equal ((((Get-Date) - $composedStart).TotalSeconds) -lt 6) $true "and in a couple of real seconds, not the 10 s interval"

$throwing = { throw "disk gone" }
$script:fakeNow = 100.0
$reasonAfterError = Wait-ForItemChangeOrInterval -Baseline "A" -IntervalSeconds 3 -GetSignature $throwing -Sleep { param($seconds) $script:fakeNow += $seconds } -Clock { $script:fakeNow }
Assert-Equal $reasonAfterError "interval" "a look that throws is no change and no crash: the wait just runs its interval"

Remove-Item -LiteralPath $stateDir -Recurse -Force -ErrorAction SilentlyContinue
if ($script:failures -gt 0) { Write-Host "$($script:failures) FAILED"; exit 1 }
Write-Host "ALL PASSED"
exit 0
