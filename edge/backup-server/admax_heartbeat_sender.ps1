<#
.SYNOPSIS
  Watches AdMax on this machine (backup-server) and sends a heartbeat to the central Watchdog
  listener on Core. Detection only: reads process state, never touches AdMax's own files or
  configuration, never restarts anything.

.NOTES
  PLAYERS. Matches by EXECUTABLE PATH (anything under the Unimedia\Admax install tree), not by
  process name: the real executables are renamed per stream (STATION_BOX_OFFICE_BACKUP.exe,
  STATION_NETWORK_BACKUP.exe, under admax3\ and admax4\). Per the owner, "Network" = USA and
  "Box Office" = Caribbean: a confirmed mapping, applied by process name, not by slot number.

  Each feed also has a "_CLIENT" window process (about 19 threads, the control UI). It used to be
  reported as a second "usa" player. It is not a player, so it is listed separately under
  aux_processes and never counts as an instance, and never carries a motion measurement.

  MOTION. Measured on air (2026-10-10): AdMax rewrites Settings.ini on a timer with the same
  values, and no other file moves with playback, so file times say nothing about whether the
  player is playing. What is steady is each engine's own work (0.8-1.2 cores in every 10 s
  window). So each cycle this script measures how much CPU time each engine used since the last
  cycle and tracks "seconds since the engine last did real work"; the listener turns that into
  yellow at 45 s and red at 60 s.
  This proves the player is WORKING, not that the picture is moving. Whether a paused player's
  CPU falls below the floor has not been measured (it needs a supervised pause), so see
  the unpublished design notes before relying on it for pauses specifically.

  NOW PLAYING. Read from the engine's own Logs\Playout log: AdMax writes an "Open File" line, with
  the time, when it starts an item, so the newest such line is the item on air. Only those lines
  count (other lines that mention a file, such as errors, do not).
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
    [string]$AdmaxRoot = "C:\Program Files (x86)\Unimedia\Admax One2.0",
    # Shared listener token, in a locked-down file beside this script (same design as the main sender).
    [string]$TokenFile = "C:\ClarixWatchdog\edge\watchdog.token"
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

$script:ActivityState = @{}
# Pause detection: each AdMax engine's own CPU and file reads (see the shared block above). Thresholds from the supervised pauses of
# 2026-10-11: engine CPU under 0.45 cores AND reads under 0.20 MB/s for a 15 s window is a pause (normal playback never did both at once).
$MediaQuietCores = 0.45
$MediaQuietReadMBps = 0.20
$script:MediaCounters = @{}
$script:MediaState = @{}

$script:NowPlayingCache = @{}

function Get-AdmaxSlotRoot {
    param([string]$ExecutablePath)
    $binDir = Split-Path -Path $ExecutablePath -Parent
    # ...\admax3\bin\64bit\x.exe -> the slot root is the folder above "bin"
    $probe = $binDir
    for ($i = 0; $i -lt 3 -and $probe; $i++) {
        if ([IO.Path]::GetFileName($probe) -ieq "bin") { return Split-Path -Path $probe -Parent }
        $probe = Split-Path -Path $probe -Parent
    }
    return $binDir
}

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

function Get-AdmaxNowPlayingFromLines {
    # Pure: the log's lines in, the item that started most recently out (or $null).
    # AdMax logs "|[FillerMedia]|<path>|Open File and media in/out ..." at the moment it opens an
    # item to play it (checked 2026-10-10: 20:15:51 for a clock of 20:15:58), so the newest such
    # line IS the item on air. Other lines that happen to mention a file (errors, graphics) do not count.
    # When the log gives no play range (fillers log "[0,0]"), -LengthLookup, a script block that takes the logged path and returns seconds or $null, is
    # asked ONCE for the item on air (never for every line in the window). In production it reads the file's own length from Windows.
    param([string[]]$Lines, [scriptblock]$LengthLookup = $null)
    $latest = $null
    $latestPath = $null
    foreach ($line in $Lines) {
        $m = [regex]::Match($line, '^(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}) .*?\|\[[^\]]+\]\|([^|]+)\|Open File')
        if (-not $m.Success) { continue }
        $name = $null
        try { $name = [IO.Path]::GetFileName($m.Groups[2].Value.Trim()) } catch { $name = $m.Groups[2].Value.Trim() }
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        # The same line carries the play range, "in/out [0,7192.64]": the item plays from 0 s to 7192.64 s, which Windows confirms is the file's
        # 01:59:52. Out minus in is the length for the dashboard's progress bar. [0,0] (the fillers) means "unknown", so no bar rather than a wrong one.
        $durationSeconds = $null
        $range = [regex]::Match($line, 'Open File[^\[]*\[\s*([0-9]+(?:\.[0-9]+)?)\s*,\s*([0-9]+(?:\.[0-9]+)?)\s*\]')
        if ($range.Success) {
            $inPoint = 0.0; $outPoint = 0.0
            $inv = [Globalization.CultureInfo]::InvariantCulture
            if ([double]::TryParse($range.Groups[1].Value, [Globalization.NumberStyles]::Float, $inv, [ref]$inPoint) -and
                [double]::TryParse($range.Groups[2].Value, [Globalization.NumberStyles]::Float, $inv, [ref]$outPoint) -and
                $outPoint -gt $inPoint -and ($outPoint - $inPoint) -le 259200) {
                $durationSeconds = [math]::Round($outPoint - $inPoint, 1)
            }
        }
        $startedUtc = $null
        try {
            $startLocal = [datetime]::ParseExact($m.Groups[1].Value, "dd.MM.yyyy HH:mm:ss", [Globalization.CultureInfo]::InvariantCulture)
            $startedUtc = ConvertTo-IsoUtcFromLocal -LocalTime $startLocal
        }
        catch { $startedUtc = $null }
        $latest = [PSCustomObject]@{
            filename    = $name
            result      = "Started " + $m.Groups[1].Value.Substring(11)
            duration_s  = $durationSeconds
            started_utc = $startedUtc
        }
        $latestPath = $m.Groups[2].Value.Trim()
    }
    if ($latest -and $null -eq $latest.duration_s -and $LengthLookup) {
        try {
            $found = & $LengthLookup $latestPath
            if ($null -ne $found -and [double]$found -gt 0 -and [double]$found -le 259200) { $latest.duration_s = [math]::Round([double]$found, 1) }
        }
        catch { }   # a length that cannot be read means no bar, never a failed heartbeat
    }
    return $latest
}

$script:LengthCache = @{}

function Get-MediaLengthSeconds {
    # Windows' own "Length" property of a media file, the number Explorer shows (read-only; nothing is written, no extra tool is needed). Used only for
    # items whose log line gives no play range. Measured on BACKUP-SERVER 2026-10-14 through a non-interactive session: 00:00:51 for a filler and
    # 01:59:52 for a program, equal to AdMax's own range. Asked once per file (the answer is cached); a file that could not be read is tried again
    # after 5 minutes, since a Dropbox file can appear a moment after it is queued. $null when it cannot be read.
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    $clean = [regex]::Replace($Path.Trim(), '(?<!^)\\{2,}', '\')   # the log sometimes writes the folder separators doubled, even eight deep (a UNC path keeps its two leading ones)
    $cached = $script:LengthCache[$clean]
    if ($cached -and ($null -ne $cached.seconds -or ([datetime]::UtcNow - $cached.at).TotalSeconds -lt 300)) { return $cached.seconds }
    $seconds = $null
    try {
        if (Test-Path -LiteralPath $clean) {
            $shell = New-Object -ComObject Shell.Application
            $folder = $shell.Namespace((Split-Path -Path $clean -Parent))
            $item = $null
            if ($folder) { $item = $folder.ParseName((Split-Path -Path $clean -Leaf)) }
            if ($item) {
                # Column 27 is "Length"; the text must still look like a clock before it is believed. Explorer wraps it in invisible direction marks.
                $seconds = ConvertTo-ClockSeconds -Text (([string]$folder.GetDetailsOf($item, 27)) -replace '[^0-9:]', '')
            }
        }
    }
    catch { $seconds = $null }
    if ($script:LengthCache.Count -gt 500) { $script:LengthCache = @{} }
    $script:LengthCache[$clean] = @{ seconds = $seconds; at = [datetime]::UtcNow }
    return $seconds
}

function Get-AdmaxNowPlaying {
    # The engine's own playout log (read-only). The last 600 lines cover many items even on a chatty
    # day; if one is somehow not in that window the last good answer for the same player is kept.
    param([string]$SlotRoot, [string]$Feed, [int]$ProcessId)
    $found = $null
    # No -Encoding on purpose: AdMax writes this log in the system ANSI code page. Forcing UTF-8
    # turns the e-acute in "Beyonce" into a replacement character (verified on the real log, 2026-10-10).
    foreach ($dir in @((Join-Path $SlotRoot "bin\64bit\Logs\Playout"), (Join-Path $SlotRoot "Logs\Playout"), (Join-Path $SlotRoot "logs\Playout"))) {
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        $log = Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        if (-not $log) { continue }
        $found = Get-AdmaxNowPlayingFromLines -Lines @(Get-Content -LiteralPath $log.FullName -Tail 600 -ErrorAction SilentlyContinue) -LengthLookup { param($p) Get-MediaLengthSeconds -Path $p }
        if ($found) { break }
    }
    $cached = $script:NowPlayingCache[$Feed]
    if ($found) { $script:NowPlayingCache[$Feed] = @{ pid = $ProcessId; value = $found }; return $found }
    if ($cached -and $cached.pid -eq $ProcessId) { return $cached.value }
    return $null
}

function Get-FeedFromProcessName {
    # Confirmed mapping (per the owner): Network = USA, Box Office = Caribbean.
    param([string]$Name)
    if ($Name -match "NETWORK") { return "usa" }
    if ($Name -match "BOX_OFFICE") { return "caribbean" }
    return "unknown"
}

function Select-AdmaxEngines {
    # From every process under the AdMax tree, the ones that are players (not "_CLIENT" windows).
    # If two processes ever claim the same feed, the one with more threads is the engine.
    param([object[]]$Processes)
    $players = @($Processes | Where-Object { $_.Name -notmatch "_CLIENT\.exe$" })
    $byFeed = @{}
    foreach ($p in $players) {
        $feed = Get-FeedFromProcessName -Name $p.Name
        $key = if ($feed -eq "unknown") { "unknown-" + $p.ProcessId } else { $feed }
        if (-not $byFeed.ContainsKey($key) -or $p.ThreadCount -gt $byFeed[$key].ThreadCount) { $byFeed[$key] = $p }
    }
    $rank = @{ usa = 0; caribbean = 1; unknown = 2 }
    return @($byFeed.Values | Sort-Object { $rank[(Get-FeedFromProcessName -Name $_.Name)] }, ProcessId)
}

function Get-AdmaxInstances {
    # Read-only: process list and the engine's own log; never writes anywhere.
    param([datetime]$NowUtc)
    $all = @(Get-CimInstance Win32_Process -Filter "ExecutablePath IS NOT NULL" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -like "$AdmaxRoot*" })
    $engines = @(Select-AdmaxEngines -Processes $all)
    $aux = @($all | Where-Object { $_.Name -match "_CLIENT\.exe$" } |
        ForEach-Object { [PSCustomObject]@{ process_name = $_.Name; pid_value = $_.ProcessId; feed = (Get-FeedFromProcessName -Name $_.Name) } })

    $instances = @()
    $liveFeeds = @{}
    foreach ($p in $engines) {
        $feed = Get-FeedFromProcessName -Name $p.Name
        $liveFeeds[$feed] = $true
        $slot = "unknown"
        if ($p.ExecutablePath -match '\\(admax\d*)\\bin\\') { $slot = $matches[1] }
        $slotRoot = Get-AdmaxSlotRoot -ExecutablePath $p.ExecutablePath
        $instance = [ordered]@{
            slot            = $slot
            feed            = $feed
            process_name    = $p.Name
            pid_value       = $p.ProcessId
            executable_path = $p.ExecutablePath
            slot_root       = $slotRoot
        }
        $nowPlaying = Get-AdmaxNowPlaying -SlotRoot $slotRoot -Feed $feed -ProcessId ([int]$p.ProcessId)
        if ($nowPlaying) { $instance["now_playing"] = $nowPlaying }

        $cpu = $null
        try { $cpu = ([double]$p.KernelModeTime + [double]$p.UserModeTime) / 10000000.0 } catch { $cpu = $null }
        $started = $null
        try { $started = $p.CreationDate.ToUniversalTime() } catch { $started = $NowUtc }
        $mono = Get-MonotonicSeconds
        $activity = Update-EngineActivity -State $script:ActivityState -Feed $feed -ProcessId ([int]$p.ProcessId) `
            -ProcessStartUtc $started -CpuSeconds $cpu -NowUtc $NowUtc -MonoSeconds $mono
        $reads = $null
        try { $reads = [double]$p.ReadTransferCount } catch { $reads = $null }
        $counterKey = "$feed-$($p.ProcessId)"
        foreach ($stale in @($script:MediaCounters.Keys)) { if ($stale -like "$feed-*" -and $stale -ne $counterKey) { $script:MediaCounters.Remove($stale) } }
        $rates = Get-MediaRates -Counters $script:MediaCounters -Key $counterKey -CpuSecondsTotal $cpu -ReadBytesTotal $reads -MonoSeconds $mono
        $engineMedia = $null
        if ($null -ne $rates) {
            $engineMedia = Update-MediaActivity -State $script:MediaState -Feed $feed -Cores $rates.cores -ReadMBps $rates.read_mbps `
                -IntervalSeconds $rates.dt -MonoSeconds $mono -QuietCores $MediaQuietCores -QuietReadMBps $MediaQuietReadMBps
        }
        Merge-MediaActivity -Activity $activity -Media $engineMedia -NowUtc $NowUtc
        foreach ($key in $activity.Keys) { $instance[$key] = $activity[$key] }
        $instances += [PSCustomObject]$instance
    }
    # A feed with no engine process has no baseline: a replacement engine starts a fresh measurement.
    foreach ($feed in @($script:ActivityState.Keys)) {
        if (-not $liveFeeds.ContainsKey($feed)) { $script:ActivityState.Remove($feed) }
        if (-not $liveFeeds.ContainsKey($feed)) { $script:MediaState.Remove($feed) }
    }
    return @{ instances = $instances; aux = $aux }
}

function Start-HeartbeatLoop {
    Write-Log "AdMax heartbeat sender starting. Listener=$ListenerUrl Fallback=$FallbackListenerUrl Standby=$StandbyListenerUrl Interval=${IntervalSeconds}s Root=$AdmaxRoot Floor=${BusyFloorCores}cores"

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

    while ($true) {
        try {
            if ((Get-Date).Date -ne $script:LogPruneDay) {
                $null = Remove-OldLogs -Dir (Join-Path $PSScriptRoot "logs") -KeepDays $LogKeepDays
                $script:LogPruneDay = (Get-Date).Date
            }
            $nowUtc = [DateTime]::UtcNow
            $found = Get-AdmaxInstances -NowUtc $nowUtc
            # @() forces an array even for 0 or 1 items; otherwise PowerShell unwraps a single
            # object and ConvertTo-Json writes instance_count as null.
            $instances = @($found.instances)
            $fields = @{
                hostname                  = $env:COMPUTERNAME
                timestamp                 = $nowUtc.ToString("o")
                expected_interval_seconds = $IntervalSeconds
                instance_count            = $instances.Count
                instances                 = $instances
                aux_processes             = @($found.aux)
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
        Wait-WithSiteCDrain -Seconds $IntervalSeconds
    }
}

# Dot-sourcing (". script.ps1", as the tests do) loads the functions without starting the loop.
if ($MyInvocation.InvocationName -ne ".") { Start-HeartbeatLoop }
