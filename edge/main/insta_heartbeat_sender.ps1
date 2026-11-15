<#
.SYNOPSIS
  Watches Insta Playout on this machine (main) and sends a heartbeat to the central Watchdog
  listener on Core. Detection only: reads process state, never touches Insta Playout's own
  files or configuration, never restarts anything. vmix and ZIXI are explicitly ignored by design.

.NOTES
  PLAYERS. Each Insta Playout "instance" is a pair of processes sharing one exe path: a small
  ~3-thread launcher and a large ~100-150-thread engine, started within seconds of each other.
  Nothing about a process says which feed it is (same exe, empty command line, no window title).

  Per the owner, "player2 is Caribbean": the pair that started first is "usa" and the second is
  "caribbean". That start-order rule is only used the FIRST time a pair is seen. After that the
  feed is remembered by process id (and saved to state\feed-bindings.json, so it survives a
  restart of this script). The old rule re-labelled by position every cycle, so when the USA
  player died the surviving Caribbean player was reported as USA and the wrong tile went red.
  Now a dead player is simply absent, and its tile reports "Player Down".
  If BOTH players die and one comes back, the start-order rule applies again; nothing on the
  machine can tell them apart.

  Only "Insta Playout" and "Insta Playout 2" are live. Folders 3 and 4 exist but are dormant,
  and their processes look identical. A third pair is counted (extra_instances_ignored) and never
  named or shown.

  MOTION. Measured on air (2026-10-10): Insta writes no file continuously (the playlist file
  changes at an item change, and playlist_position.txt / runningstatus.txt are written once at
  start or not at all), so file times say nothing about playback. What IS steady is the engine's
  own work: 1.0-1.6 cores per engine, every 10 s window. So each cycle this script measures how
  much CPU time each engine used since the last cycle and tracks "seconds since the engine last
  did real work". The listener turns that into yellow at 45 s and red at 60 s.
  This proves the player is WORKING, not that the picture is moving. Whether a paused player's
  CPU falls below the floor has not been measured (it needs a supervised pause), so see
  the unpublished design notes before relying on it for pauses specifically.

  NOW PLAYING. Each channel's own Settings\Mainplaylist.xml is the live playlist (the whole day,
  e.g. 65 items). Insta colours the rows: the item ON AIR is BackgroundColor "Olive" and the
  one cued next is "SteelBlue"; its SubItem_1 is the time it started and SubItem_5 its length.
  Checked 2026-10-10 against the shared daily log on both channels: the Olive item started at
  the exact second the previous item's log line ended (20:06:27 and 19:30:33). Until 2026-10-11
  this code took the LAST file named in the XML, which is just the last item of the day, and
  showed it as "now playing"; that was wrong. runningstatus.txt holds the index of the last
  FINISHED item, so it is not used. The shared daily log is written when an item ENDS, so it
  cannot name the current item either. If no row is Olive, nothing is reported (never a guess).

  ITEM CHANGES (2026-11-08). Measured live on 2026-11-07, each new item reached Core 8 to 16 s after it started, because the loop slept a flat 10 s. The wait
  now ends early when Insta rewrites a channel's Mainplaylist.xml or runningstatus.txt (their time and size are looked at once a second; the status file is
  only a trigger to look again sooner, never read for its content) and the change has held for a second look. See the item-change-wait region.
#>

param(
    # Tailscale first: WireGuard encrypts the key and the heartbeat end to end. The LAN is only the fallback below.
    [string]$ListenerUrl = "http://100.64.0.10:8420/heartbeat",
    # Used only when the first address fails, and left again on the next cycle. Cleartext but private: set "" to refuse any cleartext path.
    [string]$FallbackListenerUrl = "http://192.168.7.183:8420/heartbeat",
    # A second listener on another network (the standby) gets a best-effort copy of each heartbeat, so it can take over alerting if Core dies. "" switches the copy off.
    [string]$StandbyListenerUrl = "https://standby.corp.example/heartbeat",
    [int]$IntervalSeconds = 10,
    # The Site C feed (the unpublished design notes): the encoder's two multicast streams, counted on this PC and reported inside the heartbeat. $false switches it off here.
    [bool]$SiteCFeedMonitor = $true,
    [string]$SiteCFeedGroup = "224.1.1.1",
    [int[]]$SiteCFeedPorts = @(5004, 5005),
    [string]$SiteCFeedLanPrefix = "192.168.7.",
    [int]$PairWindowSeconds = 10,  # processes starting within this many seconds of each other are one pair
    # Shared secret the listener requires in the X-Watchdog-Token header, kept in a locked-down file.
    [string]$TokenFile = "C:\ClarixWatchdog\edge\watchdog.token",
    [string]$StateDir = (Join-Path $PSScriptRoot "state")
)

$ErrorActionPreference = "Stop"

function Write-Log {
    param([string]$Message)
    $dir = Join-Path $PSScriptRoot "logs"
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $file = Join-Path $dir ("heartbeat-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    if (Test-LogFull -Path $file -MaxBytes $LogMaxBytes) { return }   # a day's log never grows past the cap
    Add-Content -Path $file -Value ("{0} {1}" -f (Get-Date -Format "o"), $Message)
}

#region log-hygiene
# KEEP THIS REGION IDENTICAL IN BOTH SENDERS (ops/test_edge_senders.py checks it).
# The senders write only a small daily log of start-up and failure lines, but nothing used to delete it. Now: a day's file is capped,
# files older than $LogKeepDays are removed once a day, and a repeating failure is noted at most once every 5 minutes.
$LogKeepDays = 14
$LogMaxBytes = 1048576        # 1 MB per day
$script:LogPruneDay = $null
$script:FailureLog = @{}

function Remove-OldLogs {
    # Deletes this sender's OWN daily files (heartbeat-*.log) older than $KeepDays days and nothing else. Returns how many were removed.
    param([string]$Dir, [int]$KeepDays, [datetime]$Now = (Get-Date))
    if (-not (Test-Path -LiteralPath $Dir)) { return 0 }
    $cutoff = $Now.AddDays(-$KeepDays)
    $removed = 0
    foreach ($file in @(Get-ChildItem -LiteralPath $Dir -File -Filter "heartbeat-*.log" -ErrorAction SilentlyContinue)) {
        if ($file.LastWriteTime -lt $cutoff) {
            try { Remove-Item -LiteralPath $file.FullName -Force -ErrorAction Stop; $removed++ } catch { }
        }
    }
    return $removed
}

function Test-LogFull {
    param([string]$Path, [long]$MaxBytes)
    return ((Test-Path -LiteralPath $Path) -and ((Get-Item -LiteralPath $Path).Length -gt $MaxBytes))
}

function Get-FailureLogDecision {
    # A send failure repeats every cycle during an outage. Say so the first time, then at most once per $EverySeconds with a count.
    param([hashtable]$State, [double]$MonoSeconds, [double]$EverySeconds = 300)
    $last = $State["last"]
    if ($null -eq $last -or ($MonoSeconds - $last) -ge $EverySeconds) {
        $suppressed = [int]$State["suppressed"]
        $State["last"] = $MonoSeconds
        $State["suppressed"] = 0
        return @{ log = $true; suppressed = $suppressed }
    }
    $State["suppressed"] = [int]$State["suppressed"] + 1
    return @{ log = $false; suppressed = 0 }
}
#endregion log-hygiene

#region send-with-fallback
# KEEP THIS REGION IDENTICAL IN BOTH SENDERS (ops/test_edge_senders.py checks it; edge/tests/Test-SendFallback.ps1 tests how it behaves).
# The key crosses a network on every heartbeat. Tailscale (WireGuard) is encrypted end to end; the LAN is not. So the sender tries the Tailscale address
# first and uses the LAN address only when that fails, and goes back to Tailscale on the very next cycle (it never sticks to the fallback).
# A 401 or 403 is the LISTENER answering "not this key / not for this machine": the other path would say the same, so it is not tried.
$script:FallbackLog = @{}       # rate-limits the "using the fallback address" note, like $script:FailureLog does for failures

function Get-ListenerUrls {
    param([string]$Primary, [string]$Fallback)
    $urls = @($Primary)
    if (-not [string]::IsNullOrWhiteSpace($Fallback) -and $Fallback.Trim() -ne $Primary) { $urls += $Fallback.Trim() }
    return $urls
}

function Send-Heartbeat {
    # Posts $BodyBytes to each address in turn until one accepts it. Returns @{ ok; url; index; refused; error }.
    # The token travels in a header, not the URL: URLs end up in proxy and access logs.
    param([string[]]$Urls, [byte[]]$BodyBytes, [string]$Token)
    $lastError = $null
    for ($i = 0; $i -lt $Urls.Count; $i++) {
        $timeout = if ($i -lt $Urls.Count - 1) { 3 } else { 8 }      # a dead first path costs 3 s, not the whole cycle
        try {
            Invoke-RestMethod -Uri $Urls[$i] -Method Post -Body $BodyBytes -ContentType "application/json; charset=utf-8" -Headers @{ "X-Watchdog-Token" = $Token } -TimeoutSec $timeout | Out-Null
            return @{ ok = $true; url = $Urls[$i]; index = $i; refused = $null; error = $null }
        }
        catch {
            $statusCode = $null
            if ($_.Exception.Response) { $statusCode = [int]$_.Exception.Response.StatusCode }
            if ($statusCode -eq 401 -or $statusCode -eq 403) {
                return @{ ok = $false; url = $Urls[$i]; index = $i; refused = $statusCode; error = $_.Exception.Message }
            }
            $lastError = $_.Exception.Message
        }
    }
    return @{ ok = $false; url = $null; index = -1; refused = $null; error = $lastError }
}
#endregion send-with-fallback

#region standby-copy
# KEEP THIS REGION IDENTICAL IN BOTH SENDERS (ops/test_edge_senders.py checks it; edge/tests/Test-StandbyCopy.ps1 tests how it behaves).
# A second, independent listener (the STANDBY, on another network) gets a copy of every heartbeat so it can take over alerting if Core dies. The copy is best effort
# and must never hurt the real post to Core, which has already been sent when this runs: it goes out in the BACKGROUND (this function does not wait for the answer),
# with a 3 second limit, once (no retry), and a failure is only a rate-limited note in the log. It carries the same key this machine already uses, in the header only,
# plus `primary_ok`: whether THIS machine's post to Core was accepted, which the standby weighs before it ever takes over. An empty address switches the copy off.
$script:StandbyLog = @{}          # rate-limits the "standby copy failed" note, like $script:FailureLog does for Core
$script:StandbyPending = $null    # the copy still in flight from the previous cycle (its result is read at the start of the next one)
$script:StandbyClient = $null

function Add-PrimaryFlag {
    # The heartbeat JSON with "primary_ok": true/false added at the end. Anything that is not a JSON object is returned unchanged.
    param([string]$Json, [bool]$Ok)
    $text = ([string]$Json).Trim()
    if (-not $text.StartsWith("{") -or -not $text.EndsWith("}")) { return $Json }
    $head = $text.Substring(0, $text.Length - 1).TrimEnd()
    $flag = if ($Ok) { "true" } else { "false" }
    if ($head.EndsWith("{")) { return $head + '"primary_ok":' + $flag + "}" }
    return $head + ',"primary_ok":' + $flag + "}"
}

function Get-StandbyClient {
    if ($null -eq $script:StandbyClient) {
        Add-Type -AssemblyName System.Net.Http
        try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
        $client = [System.Net.Http.HttpClient]::new()
        $client.Timeout = [TimeSpan]::FromSeconds(3)
        $script:StandbyClient = $client
    }
    return $script:StandbyClient
}

function Get-StandbyProblem {
    # What went wrong with the copy sent in the previous cycle, as a short sentence; $null if it went fine or is still in flight.
    param($Task)
    if ($null -eq $Task -or -not $Task.IsCompleted) { return $null }
    if ($Task.IsFaulted) { return $Task.Exception.GetBaseException().Message }
    if ($Task.IsCanceled) { return "no answer within 3 seconds" }
    $code = [int]$Task.Result.StatusCode
    $Task.Result.Dispose()
    if ($code -ge 400) {
        if ($code -eq 401 -or $code -eq 403) { return "HTTP $code, the standby refused this machine's key" }
        return "HTTP $code"
    }
    return $null
}

function Write-StandbyNote {
    param([string]$Problem)
    $decision = Get-FailureLogDecision -State $script:StandbyLog -MonoSeconds (Get-MonotonicSeconds)
    if ($decision.log) { Write-Log "NOTE: the copy to the standby listener failed ($Problem); Core was not affected ($($decision.suppressed) more since the last note)." }
}

function Send-StandbyCopy {
    param([string]$Url, [string]$Json, [bool]$PrimaryOk, [string]$Token)
    if ([string]::IsNullOrWhiteSpace($Url)) { return }
    try {
        $problem = Get-StandbyProblem -Task $script:StandbyPending
        $script:StandbyPending = $null
        if ($problem) { Write-StandbyNote -Problem $problem }
        $client = Get-StandbyClient          # loads the web library first: its types do not exist until then
        $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Post, $Url.Trim())
        $request.Content = [System.Net.Http.StringContent]::new((Add-PrimaryFlag -Json $Json -Ok $PrimaryOk), [Text.Encoding]::UTF8, "application/json")
        [void]$request.Headers.TryAddWithoutValidation("X-Watchdog-Token", $Token)
        $script:StandbyPending = $client.SendAsync($request)
    }
    catch {
        $script:StandbyPending = $null
        try { Write-StandbyNote -Problem $_.Exception.Message } catch { }
    }
}
#endregion standby-copy

#region site_c-feed
# KEEP THIS REGION IDENTICAL IN BOTH SENDERS (ops/test_edge_senders.py checks it; edge/tests/Test-SiteCFeed.ps1 tests how it behaves).
# The Site C feed (the unpublished design notes): the encoder sends two multicast streams (UDP 5004 and 5005) that this PC can hear. This region listens to them all the time and
# puts what it heard since the last heartbeat into the heartbeat itself (field `site_c_feed`), so Core and the standby both learn about the streams from the one message that
# already goes to both. It only LISTENS: it sends nothing, and a fault in it can never stop the heartbeat (the caller reads a short report either way).
# Two details that matter: the sockets stay open between heartbeats with a big buffer and are drained every second while the sender sleeps (a reader that wakes up only once
# every ten seconds would lose packets and report a falsely low rate), and the rate is the average over the WHOLE window since the last report.
$script:SiteCFeed = $null          # the open listener, or $null
$script:SiteCLastError = ""        # the last reason it could not listen, so the log says it once instead of every ten seconds
# The per-second readings (the unpublished design notes, "The per-second readings"): every one-second drain closes a SAMPLE per stream, the rate over the time it really measured,
# and each heartbeat carries the new samples next to the ten-second figures. They are for the dashboard's chart only: the alarm rules still read the ten-second figures.
$script:SiteCMinSampleSeconds = 0.25   # a stretch shorter than this is not closed as a sample; its bytes go into the next one, none are lost
$script:SiteCKeepSamples = 60          # the most samples held per stream if no report is made for a while (a stuck cycle can never grow the list)
$script:SiteCReportSamples = 30        # the most one heartbeat carries per stream: the newest ones

function Test-MulticastAddress {
    param([string]$Address)
    $first = ([System.Net.IPAddress]::Parse($Address)).GetAddressBytes()[0]
    return ($first -ge 224 -and $first -le 239)
}

function Get-SiteCLanIp {
    param([string]$Prefix)
    try {
        $found = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object { $_.IPAddress -like "$Prefix*" } | Select-Object -First 1
        if ($found) { return [string]$found.IPAddress }
    } catch { }
    return $null
}

function Close-SiteCFeed {
    param($Feed)
    if ($null -eq $Feed) { return }
    foreach ($key in @($Feed.streams.Keys)) {
        $udp = $Feed.streams[$key].udp
        try { if ($Feed.multicast) { $udp.DropMulticastGroup([System.Net.IPAddress]::Parse($Feed.group)) } } catch { }
        try { $udp.Close() } catch { }
    }
}

function Open-SiteCFeed {
    param([string]$Group, [int[]]$Ports, [string]$InterfaceIp, [double]$MonoSeconds)
    $multicast = Test-MulticastAddress -Address $Group
    $feed = @{ ip = $InterfaceIp; group = $Group; multicast = $multicast; streams = [ordered]@{}; mark = $MonoSeconds; sampleMark = $MonoSeconds }
    try {
        foreach ($port in $Ports) {
            $udp = New-Object System.Net.Sockets.UdpClient
            $feed.streams["$port"] = @{ udp = $udp; packets = 0; bytes = 0; idle = 0.0; sPackets = 0; sBytes = 0; samples = (New-Object System.Collections.Generic.List[object]) }       # registered first, so a failure further down still closes it
            $udp.ExclusiveAddressUse = $false
            $udp.Client.SetSocketOption([System.Net.Sockets.SocketOptionLevel]::Socket, [System.Net.Sockets.SocketOptionName]::ReuseAddress, $true)
            try { $udp.Client.ReceiveBufferSize = 16 * 1024 * 1024 } catch { }
            $bindTo = if ($multicast) { [System.Net.IPAddress]::Any } else { [System.Net.IPAddress]::Parse($Group) }
            $udp.Client.Bind((New-Object System.Net.IPEndPoint($bindTo, $port)))
            if ($multicast) { $udp.JoinMulticastGroup([System.Net.IPAddress]::Parse($Group), [System.Net.IPAddress]::Parse($InterfaceIp)) }
        }
    }
    catch {
        Close-SiteCFeed -Feed $feed
        throw
    }
    return $feed
}

function Receive-SiteCFeed {
    param($Feed)
    $remote = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
    foreach ($key in @($Feed.streams.Keys)) {
        $stream = $Feed.streams[$key]
        while ($stream.udp.Available -gt 0) {
            $data = $stream.udp.Receive([ref]$remote)
            $stream.packets++
            $stream.bytes += $data.Length
            $stream.sPackets++
            $stream.sBytes += $data.Length
        }
    }
}

# Closes the stretch since the last sample: for each stream the rate is bytes x 8 over the time MEASURED on the monotonic clock (a busy second is not pretended to be a full
# one). A stretch under a quarter of a second is left open, so the next call covers it and no byte goes missing.
function Add-SiteCSecond {
    param($Feed, [double]$MonoSeconds)
    $span = $MonoSeconds - $Feed.sampleMark
    if ($span -lt $script:SiteCMinSampleSeconds) { return }
    $Feed.sampleMark = $MonoSeconds
    foreach ($key in @($Feed.streams.Keys)) {
        $stream = $Feed.streams[$key]
        $stream.samples.Add(@{ end = $MonoSeconds; span = $span; mbit = [math]::Round($stream.sBytes * 8 / $span / 1000000, 3); packets = [int]$stream.sPackets })
        if ($stream.samples.Count -gt $script:SiteCKeepSamples) { $stream.samples.RemoveAt(0) }
        $stream.sBytes = 0
        $stream.sPackets = 0
    }
}

# The newest samples of one stream as [age, span, Mbit/s, packets] rows (age = seconds before this report), oldest first; the list is emptied, so a sample is sent once.
# The leading comma keeps PowerShell from flattening a single row into plain numbers.
function Get-SiteCSampleRows {
    param($Stream, [double]$MonoSeconds)
    $rows = New-Object System.Collections.Generic.List[object]
    $count = $Stream.samples.Count
    for ($i = [math]::Max(0, $count - $script:SiteCReportSamples); $i -lt $count; $i++) {
        $sample = $Stream.samples[$i]
        $rows.Add(@([math]::Round([math]::Max($MonoSeconds - $sample.end, 0), 1), [math]::Round($sample.span, 2), $sample.mbit, $sample.packets))
    }
    $Stream.samples.Clear()
    return ,$rows.ToArray()
}

function Read-SiteCFeed {
    param($Feed, [double]$MonoSeconds)
    Receive-SiteCFeed -Feed $Feed
    Add-SiteCSecond -Feed $Feed -MonoSeconds $MonoSeconds        # the newest sample ends at report time, so the chart is as fresh as this report
    $window = [math]::Max($MonoSeconds - $Feed.mark, 0.001)
    $Feed.mark = $MonoSeconds
    $streams = @{}
    $seconds = @{}
    foreach ($key in @($Feed.streams.Keys)) {
        $stream = $Feed.streams[$key]
        # How long there has been no packet at all: it grows by one window for each empty window and goes back to zero at the first packet.
        $stream.idle = if ($stream.packets -gt 0) { 0.0 } else { $stream.idle + $window }
        $streams[$key] = @{ mbit_per_s = [math]::Round($stream.bytes * 8 / $window / 1000000, 3); packets = [int]$stream.packets; idle_seconds = [math]::Round($stream.idle, 1) }
        $stream.packets = 0
        $stream.bytes = 0
        $seconds[$key] = Get-SiteCSampleRows -Stream $stream -MonoSeconds $MonoSeconds
    }
    return @{ listening = $true; window_seconds = [math]::Round($window, 1); error = ""; streams = $streams; seconds = $seconds }
}

function Get-SiteCFeedReport {
    param([string]$Group, [int[]]$Ports, [string]$LanPrefix, [double]$MonoSeconds, [string]$InterfaceIp = "")
    $ip = if ($InterfaceIp) { $InterfaceIp } else { Get-SiteCLanIp -Prefix $LanPrefix }
    if ($null -ne $script:SiteCFeed -and $script:SiteCFeed.ip -ne $ip) { Close-SiteCFeed -Feed $script:SiteCFeed; $script:SiteCFeed = $null }
    try {
        if ($null -eq $script:SiteCFeed) {
            if (-not $ip) { throw "no LAN address found on this PC" }
            $script:SiteCFeed = Open-SiteCFeed -Group $Group -Ports $Ports -InterfaceIp $ip -MonoSeconds $MonoSeconds
        }
        return (Read-SiteCFeed -Feed $script:SiteCFeed -MonoSeconds $MonoSeconds)
    }
    catch {
        Close-SiteCFeed -Feed $script:SiteCFeed
        $script:SiteCFeed = $null
        $reason = ([string]$_.Exception.Message) -replace "[^\x20-\x7e]", "?"
        if ($reason.Length -gt 80) { $reason = $reason.Substring(0, 80) }
        # "cannot listen" is its own answer, never "stream lost": the listener ignores this PC for the judgement until it can hear again.
        return @{ listening = $false; window_seconds = 1; error = $reason; streams = @{}; seconds = @{} }
    }
}

function Wait-WithSiteCDrain {
    param([int]$Seconds)
    for ($i = 0; $i -lt $Seconds; $i++) {
        Start-Sleep -Seconds 1
        if ($null -ne $script:SiteCFeed) {
            try {
                Receive-SiteCFeed -Feed $script:SiteCFeed
                Add-SiteCSecond -Feed $script:SiteCFeed -MonoSeconds (Get-MonotonicSeconds)       # one sample per stream for every second slept
            } catch { }
        }
    }
}

function Get-SiteCLogLine {
    param($Report)
    if ($Report.listening) { $script:SiteCLastError = ""; return $null }
    if ($Report.error -eq $script:SiteCLastError) { return $null }
    $script:SiteCLastError = $Report.error
    return "NOTE: the Site C feed monitor cannot listen ($($Report.error)). The heartbeat says so; nothing else is affected."
}
#endregion site_c-feed

#region engine-activity
# Measured on air 2026-10-10: Insta engines used 1.0-1.6 cores and AdMax engines 0.8-1.2 cores,
# steadily, in every 10 s window. 0.30 is far below that and far above an engine that has stopped.
# KEEP THIS REGION IDENTICAL IN BOTH SENDERS (ops/test_edge_senders.py checks it).
$BusyFloorCores = 0.30
$StartupGraceSeconds = 30

function Get-MonotonicSeconds {
    # Seconds on a stopwatch that only ever counts forward at a steady rate. Nobody can move it: not an NTP
    # correction, not daylight saving, not someone setting the clock by hand. Every "how long has it been"
    # in this region uses it, never the wall clock.
    # LEARN: a wall clock answers "what time is it" and may be corrected at any moment; a monotonic clock
    # answers "how much time has passed". Measuring a duration with the wrong one turns a clock correction
    # into a fake stall (MAIN's clock was found 222 s slow on 2026-10-11, and correcting it would have read
    # as 222 s of "no engine activity" and raised a false critical alarm).
    return [double][System.Diagnostics.Stopwatch]::GetTimestamp() / [double][System.Diagnostics.Stopwatch]::Frequency
}

function Update-EngineActivity {
    param(
        [hashtable]$State,        # per-feed memory, owned by the caller and kept between heartbeats
        [string]$Feed,
        [int]$ProcessId,
        [datetime]$ProcessStartUtc,
        $CpuSeconds,              # total processor seconds used by the engine so far, or $null if unreadable
        [datetime]$NowUtc,
        [double]$MonoSeconds = [double]::NaN   # the caller passes Get-MonotonicSeconds; left out (tests), it is derived from $NowUtc
    )
    $tick = if ([double]::IsNaN($MonoSeconds)) { $NowUtc.Ticks / 10000000.0 } else { $MonoSeconds }
    # Uptime still comes from the process start time on the wall clock; it only has to be roughly right
    # (the listener refuses to clear an alarm on an engine under 90 s old), so a clock step is harmless here.
    $uptime = [math]::Max(0, ($NowUtc - $ProcessStartUtc).TotalSeconds)
    $mem = $State[$Feed]
    if ($null -eq $mem) {
        $mem = @{ pid = $null; cpu = $null; wallTick = $null; lastBusyTick = $tick; steady = 0.0 }
        $State[$Feed] = $mem
    }

    $telemetry = [ordered]@{
        advance_monitoring_model = "engine_cpu_activity"
        advance_alarm_eligible   = $true
        engine_uptime_seconds    = [math]::Round($uptime, 0)
    }

    if ($null -eq $CpuSeconds) {
        # Unreadable this cycle: claim nothing either way, and start over, so a bad reading can
        # never look like a stall (and never look like activity).
        $mem.pid = $null; $mem.cpu = $null; $mem.wallTick = $null; $mem.steady = 0.0
        $telemetry["advance_alarm_confirmed"] = $false
        $telemetry["advancing"] = $null
        return $telemetry
    }

    $rate = $null
    $dt = 0.0
    $baseline = ($mem.pid -ne $ProcessId) -or ($null -eq $mem.cpu) -or ($null -eq $mem.wallTick)
    if (-not $baseline) {
        $dt = $tick - $mem.wallTick
        $delta = $CpuSeconds - $mem.cpu
        if ($dt -lt 1 -or $delta -lt 0) { $baseline = $true } else { $rate = $delta / $dt }
    }

    if ($baseline) {
        # A new process, or the first look: nothing to compare against yet. lastBusyTick is left
        # alone on purpose, so a restart cannot reset how long a stall has lasted.
        $mem.steady = 0.0
    }
    elseif ($rate -ge $BusyFloorCores) {
        $mem.lastBusyTick = $tick
        $mem.steady = $mem.steady + $dt
    }
    else {
        $mem.steady = 0.0
    }
    $mem.pid = $ProcessId; $mem.cpu = $CpuSeconds; $mem.wallTick = $tick

    $age = [math]::Max(0, $tick - $mem.lastBusyTick)
    $telemetry["advance_alarm_confirmed"] = $true
    $telemetry["advance_age_seconds"] = [math]::Round($age, 0)
    $telemetry["advance_last_write_utc"] = $NowUtc.AddSeconds(-$age).ToString("o")
    $telemetry["advance_source"] = "engine CPU activity (pid $ProcessId, working floor $BusyFloorCores cores)"
    $telemetry["engine_cpu_cores"] = if ($null -ne $rate) { [math]::Round($rate, 2) } else { $null }
    $telemetry["advancing"] = if ($null -ne $rate) { [bool]($rate -ge $BusyFloorCores) } else { $null }
    $telemetry["advance_confirmed_seconds"] = [math]::Round($mem.steady, 0)
    $telemetry["advance_status"] = if ($age -ge 60) { "stale" } elseif ($age -ge 45) { "warning" } else { "advancing" }
    return $telemetry
}
# --- Pause detection by media file reads (added 2026-10-11) -------------------------------------------------------------
# Measured in four supervised pauses: a paused player keeps its engine CPU ABOVE the 0.30-core floor (BACKUP 0.32-0.41), so the
# CPU test alone cannot see a pause. What does change is the media being read from disk: MAIN's MServer.exe children read exactly
# 0.00 MB/s for the whole pause (normal playback never below about 0.4 MB/s per channel), and AdMax engines fall from 0.3-5 MB/s to
# 0.08-0.26 MB/s with CPU down to 0.32-0.41 cores. Replayed over about 3 h of normal playback per engine, the rules used by the
# senders (BACKUP: under 0.45 cores AND under 0.20 MB/s; MAIN: under 0.15 cores AND under 0.05 MB/s, averaged over 15 s) raised no
# false alarm, and caught every real pause within 2-9 s of the press.
# LIMIT: an item that legitimately reads no file (a live input, a still frame) would look like a pause.
$PauseWindowSeconds = 15      # smoothing window for the quiet test
$PauseSettleSeconds = 6       # reads take a few seconds to drain after a press; counted so the 45/60 s ladder lines up with the press

function Get-MediaRates {
    # Two readings of cumulative counters become rates since the previous call for the same key: CPU cores and file-read MB/s.
    # The first look, a counter that went backwards (process restarted) and a zero-length interval are only baselines.
    param([hashtable]$Counters, [string]$Key, $CpuSecondsTotal, $ReadBytesTotal, [double]$MonoSeconds)
    if ($null -eq $CpuSecondsTotal -or $null -eq $ReadBytesTotal) { $Counters.Remove($Key); return $null }
    $previous = $Counters[$Key]
    $Counters[$Key] = @{ cpu = [double]$CpuSecondsTotal; read = [double]$ReadBytesTotal; tick = $MonoSeconds }
    if ($null -eq $previous) { return $null }
    $dt = $MonoSeconds - $previous.tick
    $cpuDelta = [double]$CpuSecondsTotal - $previous.cpu
    $readDelta = [double]$ReadBytesTotal - $previous.read
    if ($dt -lt 1 -or $cpuDelta -lt 0 -or $readDelta -lt 0) { return $null }
    return @{ cores = $cpuDelta / $dt; read_mbps = $readDelta / $dt / 1000000.0; dt = $dt }
}

function Update-MediaActivity {
    # Keeps the last $PauseWindowSeconds of one player's media readings and says whether it is QUIET: reading almost nothing and
    # using almost no CPU. quiet-since is where the quiet stretch began (not where it was noticed), so the age lines up with the press.
    # Returns $null when there is nothing trustworthy to say (not enough readings yet, or a reading could not be taken).
    param([hashtable]$State, [string]$Feed, $Cores, $ReadMBps, [double]$IntervalSeconds, [double]$MonoSeconds,
          [double]$QuietCores, [double]$QuietReadMBps)
    $mem = $State[$Feed]
    if ($null -eq $mem) {
        $mem = @{ samples = [System.Collections.ArrayList]::new(); quietSince = $null; activeSince = $null }
        $State[$Feed] = $mem
    }
    if ($null -eq $Cores -or $null -eq $ReadMBps -or $IntervalSeconds -le 0) {
        $mem.samples.Clear(); $mem.quietSince = $null; $mem.activeSince = $null
        return $null
    }
    $null = $mem.samples.Add(@{ tick = $MonoSeconds; dt = $IntervalSeconds; cores = [double]$Cores; read = [double]$ReadMBps })
    while ($mem.samples.Count -gt 0 -and ($MonoSeconds - $mem.samples[0].tick) -ge $PauseWindowSeconds) { $mem.samples.RemoveAt(0) }
    $covered = 0.0; $coresSum = 0.0; $readSum = 0.0
    foreach ($s in $mem.samples) { $covered += $s.dt; $coresSum += $s.cores * $s.dt; $readSum += $s.read * $s.dt }
    if ($covered -lt $PauseWindowSeconds * 0.8) { return $null }
    $meanCores = $coresSum / $covered
    $meanRead = $readSum / $covered
    $quiet = ($meanCores -lt $QuietCores) -and ($meanRead -lt $QuietReadMBps)
    if ($quiet) {
        if ($null -eq $mem.quietSince) { $mem.quietSince = $MonoSeconds - $covered - $PauseSettleSeconds }
        $mem.activeSince = $null
    }
    else {
        $mem.quietSince = $null
        if ($null -eq $mem.activeSince) { $mem.activeSince = $MonoSeconds - $covered }
    }
    $age = if ($quiet) { [math]::Max(0, $MonoSeconds - $mem.quietSince) } else { 0 }
    $active = if ($quiet) { 0 } else { [math]::Max(0, $MonoSeconds - $mem.activeSince) }
    return [ordered]@{
        pause_monitoring_model  = "media_read_activity"
        media_cpu_cores         = [math]::Round($meanCores, 2)
        media_reads_mb_per_s    = [math]::Round($meanRead, 2)
        pause_quiet             = [bool]$quiet
        pause_quiet_age_seconds = [math]::Round($age, 0)
        media_active_seconds    = [math]::Round($active, 0)
    }
}

function Merge-MediaActivity {
    # An engine can keep burning CPU while the picture is paused, so a player counts as advancing only when BOTH tests agree.
    # A quiet player's stall age is the longer of the two tests, and "steady activity" cannot be longer than the time it has been
    # reading media again (so a red alarm clears 30 s after the media resumes, never at the instant of a resume).
    param($Activity, $Media, [datetime]$NowUtc)
    if ($null -eq $Media -or $Activity["advance_alarm_confirmed"] -ne $true) { return }
    foreach ($key in $Media.Keys) { $Activity[$key] = $Media[$key] }
    $Activity["advance_confirmed_seconds"] = [math]::Round([math]::Min([double]$Activity["advance_confirmed_seconds"], [double]$Media["media_active_seconds"]), 0)
    if ($Media["pause_quiet"] -eq $true) {
        $age = [math]::Max([double]$Activity["advance_age_seconds"], [double]$Media["pause_quiet_age_seconds"])
        $Activity["advance_age_seconds"] = [math]::Round($age, 0)
        $Activity["advance_last_write_utc"] = $NowUtc.AddSeconds(-$age).ToString("o")
        $Activity["advancing"] = $false
        $Activity["advance_status"] = if ($age -ge 60) { "stale" } elseif ($age -ge 45) { "warning" } else { "advancing" }
        $Activity["advance_source"] = "engine CPU activity and media file reads"
    }
}
#endregion engine-activity

$FeedOrder = @("usa", "caribbean")
$script:ActivityState = @{}
# The channel folders: "Insta Playout" is USA and "Insta Playout 2" is Caribbean (the owner). This is
# by folder, so the playlist read for a feed is certain even though the process-to-feed binding
# (see Resolve-FeedBindings) starts from start order.
$InstaRoots = @{
    usa       = "C:\Program Files\Indytek\Insta Playout"
    caribbean = "C:\Program Files\Indytek\Insta Playout 2"
}
$script:NowPlayingCache = @{}

#region item-timing
# Shared by both senders (one copy per machine on purpose, see the engine-activity region): turning a playout log or playlist's wall-clock text
# into the two things the dashboard's progress bar needs, a UTC start and a length in seconds. Pure; never touches a file or the network.
function ConvertTo-IsoUtcFromLocal {
    # The playout PCs write local wall-clock times with no zone. This machine's own zone turns one into an ISO UTC instant, or $null if it cannot.
    param([datetime]$LocalTime)
    try { return [datetime]::SpecifyKind($LocalTime, [DateTimeKind]::Local).ToUniversalTime().ToString("o") } catch { return $null }
}

function ConvertTo-ClockSeconds {
    # "00:26:42" or "26:42" -> 1602. $null when the text is not a clock length or is zero (an unknown length must not draw a bar).
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $m = [regex]::Match($Text.Trim(), '^(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,]\d+)?$')
    if (-not $m.Success) { return $null }
    $hours = 0
    if ($m.Groups[1].Success) { $hours = [int]$m.Groups[1].Value }
    $total = ($hours * 3600) + ([int]$m.Groups[2].Value * 60) + [int]$m.Groups[3].Value
    if ($total -le 0 -or $total -gt 259200) { return $null }
    return $total
}
#endregion item-timing

function Get-InstaNowPlayingFromXml {
    # Pure: the playlist text in, the on-air item out (or $null). On air = BackgroundColor "Olive".
    # Also reports what the dashboard's progress bar needs: started_utc (the playlist's clock is this machine's wall clock, so it is read as today in
    # this machine's zone, or yesterday when that would be in the future, i.e. an item that began before midnight), duration_s, and the next row's name.
    param([string]$Xml, [datetime]$NowUtc = [datetime]::UtcNow)
    if ([string]::IsNullOrWhiteSpace($Xml)) { return $null }
    $rows = @()
    foreach ($m in [regex]::Matches($Xml, '(?s)<Item>(.*?)</Item>')) {
        $body = $m.Groups[1].Value
        $rows += [PSCustomObject]@{
            onAir  = ($body -match '<BackgroundColor>Olive</BackgroundColor>')
            name   = [System.Net.WebUtility]::HtmlDecode([regex]::Match($body, '<SubItem_2>(.*?)</SubItem_2>').Groups[1].Value).Trim()
            start  = [regex]::Match($body, '<SubItem_1>(.*?)</SubItem_1>').Groups[1].Value.Trim()
            length = [regex]::Match($body, '<SubItem_5>(.*?)</SubItem_5>').Groups[1].Value.Trim()
        }
    }
    $at = -1
    for ($i = 0; $i -lt $rows.Count; $i++) {
        if ($rows[$i].onAir -and -not [string]::IsNullOrWhiteSpace($rows[$i].name)) { $at = $i }   # if two rows were ever Olive, the later one wins
    }
    if ($at -lt 0) { return $null }
    $row = $rows[$at]
    $startedUtc = $null
    try {
        if ($row.start -match '^\d{1,2}:\d{2}:\d{2}$') {
            $clock = [TimeSpan]::Parse($row.start)
            if ($clock.TotalHours -lt 24) {
                $nowLocal = $NowUtc.ToLocalTime()
                $startLocal = $nowLocal.Date.Add($clock)
                if ($startLocal -gt $nowLocal.AddMinutes(2)) { $startLocal = $startLocal.AddDays(-1) }
                $startedUtc = ConvertTo-IsoUtcFromLocal -LocalTime $startLocal
            }
        }
    }
    catch { $startedUtc = $null }
    # Insta stops rewriting the playlist file while a FileBlock schedule is on (found 2026-10-15: MAIN's Caribbean channel from midnight), so its last
    # on-air row can be hours old. A row more than 15 minutes past its own end is not on air: report nothing rather than a finished item. The margin
    # covers a late playlist rewrite (seconds) and a pause (the item ends later than its length says).
    # (Only judged when the caller says what time it is: the sender always does; a caller that does not has no "now" to compare against.)
    $lengthSeconds = ConvertTo-ClockSeconds -Text $row.length
    if ($PSBoundParameters.ContainsKey('NowUtc') -and $startedUtc -and $lengthSeconds) {
        try {
            $endLocal = $startLocal.AddSeconds($lengthSeconds)
            if ($NowUtc.ToLocalTime() -gt $endLocal.AddSeconds(900)) { return $null }
        }
        catch { }
    }
    $next = $null
    for ($j = $at + 1; $j -lt $rows.Count; $j++) {
        if (-not [string]::IsNullOrWhiteSpace($rows[$j].name)) { $next = $rows[$j].name; break }
    }
    return [PSCustomObject]@{
        filename    = $row.name
        result      = "Started " + $row.start
        duration    = $row.length
        duration_s  = (ConvertTo-ClockSeconds -Text $row.length)
        started_utc = $startedUtc
        next        = $next
    }
}

#region fileblock-schedule
# FileBlock hours (found 2026-10-15). Insta can play a day schedule (FileBlock\<start>-<weekday>,<month><day>,<year>\Schedule.xml) instead of the
# playlist; MAIN's Caribbean channel did from midnight, and from then Mainplaylist.xml is no longer rewritten. The schedule lists every item with its
# length and Insta plays them back to back from the stated start (checked 2026-10-15: 77 items adding up to 23:59:55 against the 23:59:24 the schedule
# states). The sender therefore works out which item is on air from the clock. It is an ESTIMATE (it could not be compared with the screen) and is
# marked as one, so nothing downstream presents it as certain.
$script:FileBlockCache = @{}

function Read-TextAutoEncoding {
    # Insta writes these files as UTF-16; other versions may write UTF-8. Look at the first bytes rather than assume.
    param([string]$Path)
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) { return [Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2) }
    if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) { return [Text.Encoding]::BigEndianUnicode.GetString($bytes, 2, $bytes.Length - 2) }
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { return [Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3) }
    if ($bytes.Length -ge 4 -and $bytes[1] -eq 0 -and $bytes[3] -eq 0) { return [Text.Encoding]::Unicode.GetString($bytes) }   # UTF-16 without a byte-order mark
    return [Text.Encoding]::UTF8.GetString($bytes)
}

function Get-FileBlockItems {
    # Pure: the schedule's text in, its items (file name and length in seconds) out, in order. A path that contains an apostrophe is written
    # with double quotes. Returns $null when anything is not understood (a trimmed item, an unreadable length), because a wrong schedule is worse than none.
    param([string]$Text)
    $items = @()
    foreach ($seg in ($Text -split "(?=<file in=')")) {
        if (-not $seg.StartsWith("<file in='")) { continue }
        $pathAt = $seg.IndexOf(" path=")
        if ($pathAt -lt 0) { continue }
        $quote = $seg.Substring($pathAt + 6, 1)
        $pathEnd = $seg.IndexOf($quote + " start_timecode=", $pathAt + 7)
        if ($pathEnd -lt 0) { return $null }
        $path = $seg.Substring($pathAt + 7, $pathEnd - $pathAt - 7)
        $inPoint = [regex]::Match($seg, "^<file in='([^']*)'").Groups[1].Value
        if ($inPoint -notmatch '^0(\.0+)?$') { return $null }   # a trimmed item plays for less than its length: not understood
        $lengthMatch = [regex]::Match($seg, "\sduration='([0-9.]+)'\sloop=")
        $length = 0.0
        if (-not $lengthMatch.Success) { return $null }
        if (-not [double]::TryParse($lengthMatch.Groups[1].Value, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$length) -or $length -le 0) { return $null }
        $name = $null
        try { $name = [IO.Path]::GetFileName($path) } catch { $name = $path }
        if ([string]::IsNullOrWhiteSpace($name)) { return $null }
        $items += [PSCustomObject]@{ name = $name; dur = $length }
    }
    return $items
}

function Get-InstaFileBlockNowPlaying {
    # The item the FileBlock schedule says is on air at NowUtc, or $null. Only when FileBlock is ON, exactly one schedule exists for today, the items add
    # up to the total the schedule states (within 2 minutes), and the clock is inside the block. Never throws.
    param([string]$RootPath, [datetime]$NowUtc)
    try {
        $fbDir = Join-Path $RootPath "FileBlock"
        $settings = Join-Path $fbDir "settings.txt"
        if (-not (Test-Path -LiteralPath $settings)) { return $null }
        if ((Get-Content -LiteralPath $settings -Raw).Trim() -ne "ON") { return $null }
        $nowLocal = $NowUtc.ToLocalTime()
        # Insta zero-pads the day in the folder name: "1200AM-Sunday,November08,2026" (read off MAIN on 2026-11-08; also October06, October09, November01). This used to be
        # "MMMMd", which drops the zero, so on days 1 to 9 of a month the schedule could never be found and MAIN USA showed "No file reported" for a whole FileBlock night.
        # The one 15 Oct check could not see it: with two digits padded and unpadded are the same.
        # LEARN: a test that builds its fixture with the code under test's own helper or format string can only agree with it; pin real, literal names from the real system.
        $dayTag = $nowLocal.ToString("dddd,MMMMdd,yyyy", [Globalization.CultureInfo]::InvariantCulture)
        $folders = @(Get-ChildItem -LiteralPath $fbDir -Directory -ErrorAction Stop | Where-Object { $_.Name -like "*-$dayTag" })
        if ($folders.Count -ne 1) { return $null }
        $dir = $folders[0].FullName
        $starts = @([regex]::Matches((Read-TextAutoEncoding (Join-Path $dir "Scheduledatetime.xml")), "<StartTime>\s*([^<]*?)\s*</StartTime>"))
        if ($starts.Count -ne 1) { return $null }
        $startAt = [datetime]::MinValue
        $parsed = $false
        foreach ($format in @("h:mm tt", "hh:mm tt", "h:mm:ss tt", "hh:mm:ss tt", "HH:mm", "HH:mm:ss")) {
            if ([datetime]::TryParseExact($starts[0].Groups[1].Value, $format, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$startAt)) { $parsed = $true; break }
        }
        if (-not $parsed) { return $null }
        $startOffset = $startAt.TimeOfDay.TotalSeconds
        $file = Get-Item -LiteralPath (Join-Path $dir "Schedule.xml") -ErrorAction Stop
        $cached = $script:FileBlockCache[$file.FullName]
        if (-not $cached -or $cached.stamp -ne $file.LastWriteTimeUtc.Ticks -or $cached.size -ne $file.Length) {
            $parsedItems = Get-FileBlockItems -Text (Read-TextAutoEncoding $file.FullName)   # about 600 KB: parsed once per change of the file, not every heartbeat
            $script:FileBlockCache = @{}
            $script:FileBlockCache[$file.FullName] = @{ stamp = $file.LastWriteTimeUtc.Ticks; size = $file.Length; items = $parsedItems }
            $cached = $script:FileBlockCache[$file.FullName]
        }
        $items = @($cached.items)
        if ($null -eq $cached.items -or $items.Count -eq 0) { return $null }
        $total = ($items | Measure-Object -Property dur -Sum).Sum
        # .NET writes a TimeSpan of a day or more as "d.hh:mm:ss": the real "Total duration: 1.00:11:37" is 1 day, 11 min 37 s (read off MAIN USA on 2026-11-08). Reading only "00:11:37" made a
        # schedule over 24 hours look 24 hours too short, so it was never trusted and the channel showed no item and no progress bar all day. The day part is optional: "02:35:55" has none.
        $totalText = [regex]::Match((Get-Content -LiteralPath (Join-Path $dir "Schedule.txt") -Raw), "(?:(\d+)\.)?(\d+):(\d{2}):(\d{2})")
        if (-not $totalText.Success) { return $null }
        $statedDays = 0
        if ($totalText.Groups[1].Success) { $statedDays = [int]$totalText.Groups[1].Value }
        $stated = ($statedDays * 86400) + ([int]$totalText.Groups[2].Value * 3600) + ([int]$totalText.Groups[3].Value * 60) + [int]$totalText.Groups[4].Value
        if ([math]::Abs($stated - $total) -gt 120) { return $null }
        $position = ($nowLocal - $nowLocal.Date).TotalSeconds - $startOffset
        if ($position -lt 0 -or $position -ge $total) { return $null }
        $offset = 0.0
        for ($i = 0; $i -lt $items.Count; $i++) {
            if ($position -lt ($offset + $items[$i].dur)) {
                $next = $null
                if ($i + 1 -lt $items.Count) { $next = $items[$i + 1].name }
                return [PSCustomObject]@{
                    filename    = $items[$i].name
                    result      = "Block schedule (estimated)"
                    duration    = ""
                    duration_s  = [math]::Round($items[$i].dur, 1)
                    started_utc = (ConvertTo-IsoUtcFromLocal -LocalTime $nowLocal.Date.AddSeconds($startOffset + $offset))
                    next        = $next
                    estimated   = $true
                }
            }
            $offset += $items[$i].dur
        }
        return $null
    }
    catch { return $null }
}
#endregion fileblock-schedule

function Get-InstaNowPlaying {
    # Reads one channel's playlist (read-only). Insta rewrites the file at each item change, so a
    # failed read keeps the last good answer for up to a minute instead of blanking the tile. When the
    # playlist names no item that is still on air (Insta stopped rewriting it: FileBlock hours), the
    # day's FileBlock schedule is the fallback.
    param([string]$Feed, [string]$RootPath, [datetime]$NowUtc)
    $path = Join-Path $RootPath "Settings\Mainplaylist.xml"
    $found = $null
    try {
        if (Test-Path -LiteralPath $path) { $found = Get-InstaNowPlayingFromXml -Xml (Get-Content -LiteralPath $path -Raw -ErrorAction Stop) -NowUtc $NowUtc }
        if ($null -eq $found) { $found = Get-InstaFileBlockNowPlaying -RootPath $RootPath -NowUtc $NowUtc }
        $readOk = $true
    }
    catch { $readOk = $false }
    $cached = $script:NowPlayingCache[$Feed]
    if ($readOk) {
        $script:NowPlayingCache[$Feed] = @{ value = $found; atUtc = $NowUtc }
        return $found
    }
    if ($cached -and ($NowUtc - $cached.atUtc).TotalSeconds -le 60) { return $cached.value }
    return $null
}

function Get-InstaPairs {
    # Groups the "Insta Playout" processes into launcher+engine pairs by start time.
    param([object[]]$Procs)
    $pairs = @()
    $used = @{}
    foreach ($p in $Procs) {
        if ($used.ContainsKey($p.Id)) { continue }
        $partner = $Procs | Where-Object {
            -not $used.ContainsKey($_.Id) -and $_.Id -ne $p.Id -and
            [math]::Abs(($_.StartTime - $p.StartTime).TotalSeconds) -le $PairWindowSeconds
        } | Select-Object -First 1
        $used[$p.Id] = $true
        if ($partner) { $used[$partner.Id] = $true }

        # @(...) around the whole pipeline: a lone process would otherwise come back as a bare
        # object with no Count, and "unpaired" would read false.
        $members = @(@($p, $partner) | Where-Object { $_ -ne $null })
        $launcher = $members | Where-Object { $_.Threads -lt 20 } | Select-Object -First 1
        $engine = $members | Where-Object { $_.Threads -ge 20 } | Select-Object -First 1
        $pairs += [PSCustomObject]@{
            Key         = "{0}-{1}" -f $(if ($launcher) { $launcher.Id } else { 0 }), $(if ($engine) { $engine.Id } else { 0 })
            StartUtc    = $p.StartTime.ToUniversalTime()
            LauncherPid = if ($launcher) { [int]$launcher.Id } else { 0 }
            EnginePid   = if ($engine) { [int]$engine.Id } else { 0 }
            Engine      = $engine
            Unpaired    = ($members.Count -eq 1)
        }
    }
    # Callers wrap the call in @() so zero or one pair still arrives as an array. (A leading comma
    # here would nest the whole array as a single element, which is exactly the bug a test caught.)
    return $pairs
}

function Resolve-FeedBindings {
    # Decides which feed each pair is. A pair keeps the feed it already had (matched by process id);
    # only a pair never seen before takes the first free feed, in start order.
    param([object[]]$Pairs, [hashtable]$Bindings)
    $assigned = [ordered]@{}
    $used = @{}
    foreach ($feed in $FeedOrder) {
        $bound = $Bindings[$feed]
        if ($null -eq $bound) { continue }
        foreach ($pair in $Pairs) {
            if ($used.ContainsKey($pair.Key)) { continue }
            $sameEngine = ($bound.engine_pid -gt 0) -and ($pair.EnginePid -eq $bound.engine_pid)
            $sameLauncher = ($bound.launcher_pid -gt 0) -and ($pair.LauncherPid -eq $bound.launcher_pid)
            if ($sameEngine -or $sameLauncher) { $assigned[$feed] = $pair; $used[$pair.Key] = $true; break }
        }
    }
    $remaining = @($Pairs | Where-Object { -not $used.ContainsKey($_.Key) } | Sort-Object StartUtc)
    foreach ($feed in $FeedOrder) {
        if ($assigned.Contains($feed)) { continue }
        if ($remaining.Count -eq 0) { break }
        $assigned[$feed] = $remaining[0]
        $used[$remaining[0].Key] = $true
        $remaining = @($remaining | Select-Object -Skip 1)
    }
    $newBindings = @{}
    foreach ($feed in $assigned.Keys) {
        $newBindings[$feed] = @{ engine_pid = $assigned[$feed].EnginePid; launcher_pid = $assigned[$feed].LauncherPid }
    }
    return @{ assigned = $assigned; bindings = $newBindings; extra = $remaining.Count }
}

function Read-FeedBindings {
    param([string]$Path)
    $result = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $result }
    try {
        $saved = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        foreach ($feed in $FeedOrder) {
            $b = $saved.$feed
            if ($b) { $result[$feed] = @{ engine_pid = [int]$b.engine_pid; launcher_pid = [int]$b.launcher_pid } }
        }
    }
    catch { Write-Log "NOTE: could not read $Path ($($_.Exception.Message)); starting from start order." }
    return $result
}

function Save-FeedBindings {
    param([string]$Path, [hashtable]$Bindings)
    try {
        $dir = Split-Path -Path $Path -Parent
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        ($Bindings | ConvertTo-Json -Depth 3 -Compress) | Set-Content -LiteralPath $Path -Encoding UTF8
    }
    catch { Write-Log "NOTE: could not save $Path ($($_.Exception.Message))." }
}

# Pause detection: MAIN measures the MServer.exe media-server children of each Insta engine (they read the media file; an Insta
# engine itself reads nothing). Thresholds from the supervised pauses of 2026-10-11, see the shared block above.
$MediaQuietCores = 0.15
$MediaQuietReadMBps = 0.05
$script:ChildCounters = @{}
$script:MediaState = @{}

function Get-MServerRates {
    # Per engine process id: summed CPU cores and media-read MB/s of its MServer.exe children since the last look. "ready" stays false
    # until one earlier look exists (and after a failed query), so nothing is claimed from a first look. An engine with no media-server
    # child at all, once ready, reads zero (and is judged quiet if that lasts).
    param([hashtable]$Counters, $Children, [double]$MonoSeconds)
    $result = @{ ready = $false; dt = 0.0; engines = @{} }
    if ($null -eq $Children) { $Counters.Clear(); return $result }
    $wasReady = $Counters.ContainsKey("__tick")
    $seen = @{}
    foreach ($child in $Children) {
        $id = [int]$child.ProcessId
        $seen["ms$id"] = $true
        $cpu = $null; $reads = $null
        try { $cpu = ([double]$child.KernelModeTime + [double]$child.UserModeTime) / 10000000.0; $reads = [double]$child.ReadTransferCount } catch { }
        $rates = Get-MediaRates -Counters $Counters -Key "ms$id" -CpuSecondsTotal $cpu -ReadBytesTotal $reads -MonoSeconds $MonoSeconds
        if ($null -eq $rates) { continue }
        $parent = [int]$child.ParentProcessId
        if (-not $result.engines.ContainsKey($parent)) { $result.engines[$parent] = @{ cores = 0.0; read_mbps = 0.0 } }
        $result.engines[$parent].cores += $rates.cores
        $result.engines[$parent].read_mbps += $rates.read_mbps
    }
    foreach ($key in @($Counters.Keys)) { if ($key -like "ms*" -and -not $seen.ContainsKey($key)) { $Counters.Remove($key) } }
    if ($wasReady) { $result.ready = $true; $result.dt = $MonoSeconds - $Counters["__tick"] }
    $Counters["__tick"] = $MonoSeconds
    return $result
}

function Get-InstaInstances {
    # Only ever reads process state -- never touches Insta's own files.
    param([hashtable]$Bindings, [datetime]$NowUtc)
    $procs = @(Get-Process -Name "Insta Playout" -ErrorAction SilentlyContinue |
        Select-Object Id, StartTime,
            @{N = "Threads"; E = { $_.Threads.Count } },
            @{N = "CpuSeconds"; E = { try { $_.TotalProcessorTime.TotalSeconds } catch { $null } } } |
        Sort-Object StartTime)
    $pairs = @(Get-InstaPairs -Procs $procs)
    $resolved = Resolve-FeedBindings -Pairs $pairs -Bindings $Bindings
    $mono = Get-MonotonicSeconds
    $children = $null
    try { $children = @(Get-CimInstance Win32_Process -Filter "Name='MServer.exe'" -ErrorAction Stop) } catch { $children = $null }
    $media = Get-MServerRates -Counters $script:ChildCounters -Children $children -MonoSeconds $mono

    $instances = @()
    for ($i = 0; $i -lt $FeedOrder.Count; $i++) {
        $feed = $FeedOrder[$i]
        if (-not $resolved.assigned.Contains($feed)) { continue }
        $pair = $resolved.assigned[$feed]
        $instance = [ordered]@{
            instance_index   = $i
            feed             = $feed
            start_time       = $pair.StartUtc.ToString("o")
            launcher_pid     = if ($pair.LauncherPid -gt 0) { $pair.LauncherPid } else { $null }
            engine_pid       = if ($pair.EnginePid -gt 0) { $pair.EnginePid } else { $null }
            launcher_present = ($pair.LauncherPid -gt 0)
            engine_present   = ($pair.EnginePid -gt 0)
            unpaired         = $pair.Unpaired
        }
        $nowPlaying = Get-InstaNowPlaying -Feed $feed -RootPath $InstaRoots[$feed] -NowUtc $NowUtc
        if ($nowPlaying) { $instance["now_playing"] = $nowPlaying }
        if ($pair.Engine) {
            $activity = Update-EngineActivity -State $script:ActivityState -Feed $feed -ProcessId $pair.EnginePid `
                -ProcessStartUtc $pair.Engine.StartTime.ToUniversalTime() -CpuSeconds $pair.Engine.CpuSeconds -NowUtc $NowUtc -MonoSeconds $mono
            $engineMedia = $null
            if ($media.ready) {
                $reading = if ($media.engines.ContainsKey([int]$pair.EnginePid)) { $media.engines[[int]$pair.EnginePid] } else { @{ cores = 0.0; read_mbps = 0.0 } }
                $engineMedia = Update-MediaActivity -State $script:MediaState -Feed $feed -Cores $reading.cores -ReadMBps $reading.read_mbps `
                    -IntervalSeconds $media.dt -MonoSeconds $mono -QuietCores $MediaQuietCores -QuietReadMBps $MediaQuietReadMBps
            }
            else { $script:MediaState.Remove($feed) }
            Merge-MediaActivity -Activity $activity -Media $engineMedia -NowUtc $NowUtc
            foreach ($key in $activity.Keys) { $instance[$key] = $activity[$key] }
        }
        else {
            # No engine process: the listener already reports that as Player Down. Forget the old
            # baseline so a replacement engine starts a fresh measurement.
            $script:ActivityState.Remove($feed)
            $script:MediaState.Remove($feed)
        }
        $instances += [PSCustomObject]$instance
    }
    foreach ($feed in $FeedOrder) {
        if (-not $resolved.assigned.Contains($feed)) { $script:ActivityState.Remove($feed) }
    }
    return @{ instances = $instances; extra_count = $resolved.extra; bindings = $resolved.bindings }
}

#region item-change-wait
# Found 2026-11-07 (live sample of 8 item changes): MAIN told Core about each new item 8 to 16 s after it started, because the loop slept a flat $IntervalSeconds
# between looks at the playlist, and a 7 s ident was first seen after it had already ended. SITE-B's detector avoids this by posting an extra heartbeat the instant
# the item changes. This does the same for MAIN: while waiting out the interval it looks once a second at the modification time and size of each channel's
# Settings\Mainplaylist.xml (Insta repaints the on-air row there) and Settings\runningstatus.txt (Insta writes it as an item starts), and ends the wait early when
# either changed AND the new state held for a second look. That second look means a file Insta is still writing is never read half done, and a file that keeps changing
# cannot turn this into a flood: it simply falls back to the old cadence. The wait never lasts longer than $IntervalSeconds, which stays the ceiling that
# expected_interval_seconds promises Core.
# LEARN: a poll every N seconds is simple but its worst-case lateness is N plus the work done in each cycle; watching the cheap thing that changes (two file
# timestamps, a stat call) lets the expensive work run only when there is something new to report.
$WatchPollSeconds = 1.0

function Get-WatchedItemPaths {
    # The two files per channel whose rewrite means "the item on air may have changed", built from the same channel folders the playlist is read from.
    $paths = @()
    foreach ($feed in $FeedOrder) {
        $root = $InstaRoots[$feed]
        $paths += (Join-Path $root "Settings\Mainplaylist.xml")
        $paths += (Join-Path $root "Settings\runningstatus.txt")
    }
    return $paths
}

function Get-ItemChangeSignature {
    # One line of text that changes when any watched file is created, removed or rewritten: "time:size" per file, "missing" for no file, and "unreadable" when the
    # path cannot even be looked at (its own answer, never mistaken for "missing"). Never throws.
    param([string[]]$Paths)
    $parts = @()
    foreach ($path in $Paths) {
        try {
            $info = [System.IO.FileInfo]::new($path)
            if ($info.Exists) { $parts += ("{0}:{1}" -f $info.LastWriteTimeUtc.Ticks, $info.Length) } else { $parts += "missing" }
        }
        catch { $parts += "unreadable" }
    }
    return ($parts -join "|")
}

function Wait-ForItemChangeOrInterval {
    # Waits up to $IntervalSeconds and says why it stopped: "item-change" when the watched files differ from $Baseline and the changed state held for a second look,
    # otherwise "interval". No baseline ($null: the files could not be looked at before this cycle) means nothing to compare, so it is a plain wait.
    param(
        $Baseline,
        [double]$IntervalSeconds,
        [scriptblock]$GetSignature,
        [scriptblock]$Sleep = { param($seconds) Start-Sleep -Milliseconds ([int][math]::Round($seconds * 1000)) },
        [scriptblock]$Clock = { Get-MonotonicSeconds }
    )
    $started = & $Clock
    $pending = $null          # a changed state seen once, waiting for a second look to confirm it
    while ($true) {
        $remaining = $IntervalSeconds - ((& $Clock) - $started)
        if ($remaining -le 0) { return "interval" }
        & $Sleep ([math]::Min($WatchPollSeconds, $remaining))
        if ($null -eq $Baseline) { continue }
        $seen = $null
        try { $seen = & $GetSignature } catch { $seen = $null }          # a look that fails is "no change", never a crash
        if ($null -eq $seen -or $seen -eq $Baseline) { $pending = $null; continue }
        if ($seen -eq $pending) { return "item-change" }
        $pending = $seen
    }
}
#endregion item-change-wait

function Start-HeartbeatLoop {
    Write-Log "Heartbeat sender starting. Listener=$ListenerUrl Fallback=$FallbackListenerUrl Standby=$StandbyListenerUrl Interval=${IntervalSeconds}s Floor=${BusyFloorCores}cores"

    # Fail fast if the token is missing. Retrying with no token would only produce 401s forever,
    # and the scheduled task's restart policy cannot create a missing file.
    if (-not (Test-Path -LiteralPath $TokenFile)) {
        Write-Log "FATAL: token file not found at $TokenFile. Cannot authenticate to the listener. Exiting."
        exit 2
    }
    $WatchdogToken = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($WatchdogToken)) {
        Write-Log "FATAL: token file $TokenFile is empty. Exiting."
        exit 2
    }
    Write-Log "Token file loaded from $TokenFile (value not logged)."

    $bindingsPath = Join-Path $StateDir "feed-bindings.json"
    $bindings = Read-FeedBindings -Path $bindingsPath
    $lastSaved = ($bindings | ConvertTo-Json -Depth 3 -Compress)
    $watchedPaths = Get-WatchedItemPaths

    while ($true) {
        $signature = $null          # what the watched files looked like just before this cycle reads the playlist; stays $null if that failed
        try {
            if ((Get-Date).Date -ne $script:LogPruneDay) {
                $null = Remove-OldLogs -Dir (Join-Path $PSScriptRoot "logs") -KeepDays $LogKeepDays
                $script:LogPruneDay = (Get-Date).Date
            }
            $signature = Get-ItemChangeSignature -Paths $watchedPaths          # BEFORE the playlist is read, so a change that lands during this cycle still ends the wait
            $nowUtc = [DateTime]::UtcNow
            $found = Get-InstaInstances -Bindings $bindings -NowUtc $nowUtc
            $bindings = $found.bindings
            $serialized = ($bindings | ConvertTo-Json -Depth 3 -Compress)
            if ($serialized -ne $lastSaved) { Save-FeedBindings -Path $bindingsPath -Bindings $bindings; $lastSaved = $serialized }
            $instances = @($found.instances)
            if ($found.extra_count -gt 0) {
                # Informational only. Channels 3 and 4 are confirmed dormant, so this firing at all is worth a human glance.
                Write-Log "NOTE: $($found.extra_count) extra Insta Playout pair(s) beyond the two live players detected and excluded."
            }
            $fields = @{
                hostname                  = $env:COMPUTERNAME
                timestamp                 = $nowUtc.ToString("o")
                expected_interval_seconds = $IntervalSeconds
                instance_count            = $instances.Count
                instances                 = $instances
                extra_instances_ignored   = $found.extra_count
                activity_monitor          = @{ version = 1; model = "engine_cpu_activity"; floor_cores = $BusyFloorCores; startup_grace_seconds = $StartupGraceSeconds }
            }
            if ($SiteCFeedMonitor) {
                $site_c = Get-SiteCFeedReport -Group $SiteCFeedGroup -Ports $SiteCFeedPorts -LanPrefix $SiteCFeedLanPrefix -MonoSeconds (Get-MonotonicSeconds)
                $fields.site_c_feed = $site_c
                $site_cLine = Get-SiteCLogLine -Report $site_c
                if ($site_cLine) { Write-Log $site_cLine }
            }
            $payload = $fields | ConvertTo-Json -Depth 5 -Compress

            $bodyBytes = [Text.Encoding]::UTF8.GetBytes($payload)
            $sent = Send-Heartbeat -Urls (Get-ListenerUrls -Primary $ListenerUrl -Fallback $FallbackListenerUrl) -BodyBytes $bodyBytes -Token $WatchdogToken
            # Core has been posted to. Now the standby gets its copy: in the background, once, never able to delay or fail anything above.
            Send-StandbyCopy -Url $StandbyListenerUrl -Json $payload -PrimaryOk ([bool]$sent.ok) -Token $WatchdogToken
            if ($sent.ok) {
                if ($sent.index -gt 0) {
                    # The encrypted address failed and the fallback carried this heartbeat. Rate-limited: it repeats every cycle during an outage.
                    $decision = Get-FailureLogDecision -State $script:FallbackLog -MonoSeconds (Get-MonotonicSeconds)
                    if ($decision.log) { Write-Log "NOTE: the first listener address failed; sending over the fallback address instead ($($decision.suppressed) more since the last note)." }
                }
            }
            elseif ($sent.refused -eq 401) {
                # The listener is reachable but rejects our token (a configuration problem), unlike a connectivity failure.
                Write-Log "SEND REJECTED (HTTP 401): listener refused the key. Check that $TokenFile holds the key issued for this machine on Core."
            }
            elseif ($sent.refused -eq 403) {
                Write-Log "SEND REJECTED (HTTP 403): the listener refused this key for this machine's name. The key in $TokenFile belongs to a different machine."
            }
            else {
                # A send failure is a connectivity problem, not a fault: log and retry next cycle.
                $decision = Get-FailureLogDecision -State $script:FailureLog -MonoSeconds (Get-MonotonicSeconds)
                if ($decision.log) {
                    $more = if ($decision.suppressed -gt 0) { " (and $($decision.suppressed) more since the last note)" } else { "" }
                    Write-Log ("SEND FAILED: $($sent.error)" + $more)
                }
            }
        }
        catch {
            Write-Log "LOOP ERROR (non-fatal, continuing): $($_.Exception.Message)"
        }
        # Up to $IntervalSeconds, but an item change (Insta rewriting the playlist or status file) ends it within a couple of seconds. Every one-second step goes through
        # Wait-WithSiteCDrain, so the Site C sockets are emptied once a second exactly as before; with no baseline it is that plain wait for the whole interval.
        if ($null -eq $signature) { Wait-WithSiteCDrain -Seconds $IntervalSeconds }
        else { $null = Wait-ForItemChangeOrInterval -Baseline $signature -IntervalSeconds $IntervalSeconds -GetSignature { Get-ItemChangeSignature -Paths $watchedPaths } -Sleep { param($seconds) Wait-WithSiteCDrain -Seconds ([math]::Max(1, [int][math]::Round($seconds))) } }
    }
}

# Dot-sourcing (". script.ps1", as the tests do) loads the functions without starting the loop.
if ($MyInvocation.InvocationName -ne ".") { Start-HeartbeatLoop }
