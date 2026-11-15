# File:         Test-SiteCFeed.ps1
# Purpose:      Tests the Site C feed counting in both senders (the unpublished design notes, section 4): every packet that reaches the sockets is counted, the rate is the average over
#               the window between two heartbeats, the "no packet for N seconds" clock grows by a window for each empty window and returns to 0 at the first packet, the counters
#               start again after each report, a PC that cannot listen says so (and why, briefly) instead of reporting a lost stream, and a sleep that drains keeps the buffer empty.
#               Per-second readings (spec section 12): every drain closes a sample whose rate uses the MEASURED time, a short stretch carries into the next sample, a report carries
#               the newest samples once, oldest first, and a single sample stays a list in the JSON.
#               REAL UDP packets on the local machine play the encoder (the join of a multicast group is left out for a non-multicast address, so this runs anywhere).
# Inputs:       none. Run: powershell -NoProfile -ExecutionPolicy Bypass -File edge\tests\Test-SiteCFeed.ps1
# Outputs:      PASS/FAIL lines; "ALL PASSED" and exit code 0 when everything passed, 1 otherwise.
# Dependencies: both sender scripts, dot-sourced one after the other (their loops do not start when dot-sourced); .NET UdpClient.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

$script:failures = 0
function Assert-Equal($actual, $expected, $label) {
    if ($actual -ne $expected) { Write-Host "FAIL: $label (got '$actual', expected '$expected')"; $script:failures++ }
    else { Write-Host "PASS: $label" }
}
function Assert-True($condition, $label) { Assert-Equal ([bool]$condition) $true $label }

function Get-FreeUdpPort {
    $probe = New-Object System.Net.Sockets.UdpClient(0)
    $port = $probe.Client.LocalEndPoint.Port
    $probe.Close()
    return $port
}

function Send-Packets([int]$Port, [int]$Count, [int]$Size = 1316) {
    $sender = New-Object System.Net.Sockets.UdpClient
    $bytes = New-Object byte[] $Size
    for ($i = 0; $i -lt $Count; $i++) { [void]$sender.Send($bytes, $bytes.Length, "127.0.0.1", $Port) }
    $sender.Close()
    Start-Sleep -Milliseconds 300       # loopback is fast, but the receiving side still needs a moment to queue them
}

foreach ($sender in @("edge\main\insta_heartbeat_sender.ps1", "edge\backup-server\admax_heartbeat_sender.ps1")) {
    Write-Host "--- $sender"
    . (Join-Path $repo $sender)
    $script:SiteCFeed = $null

    $p1 = Get-FreeUdpPort
    $p2 = Get-FreeUdpPort
    $feed = Open-SiteCFeed -Group "127.0.0.1" -Ports @($p1, $p2) -InterfaceIp "127.0.0.1" -MonoSeconds 100.0
    Assert-True ($null -ne $feed) "a listener opens on two ports"

    # --- counting: 10 packets of 1316 bytes on the first port, nothing on the second, read after a 2 second window
    Send-Packets -Port $p1 -Count 10
    $report = Read-SiteCFeed -Feed $feed -MonoSeconds 102.0
    $a, $b = $report.streams["$p1"], $report.streams["$p2"]
    Assert-Equal $report.listening $true "the report says it is listening"
    Assert-Equal $report.error "" "no error when listening"
    Assert-Equal $a.packets 10 "every packet is counted"
    Assert-Equal $a.mbit_per_s ([math]::Round(10 * 1316 * 8 / 2.0 / 1000000, 3)) "the rate is bytes x 8 over the whole window"
    Assert-Equal $a.idle_seconds 0 "a stream that had packets is not idle"
    Assert-Equal $b.packets 0 "an empty stream counts zero"
    Assert-Equal $b.idle_seconds 2 "an empty window adds its length to the idle clock"
    Assert-Equal $report.window_seconds 2 "the window is the time since the last report"

    # --- the counters start again, the idle clock keeps growing, and the first packet resets it
    $report = Read-SiteCFeed -Feed $feed -MonoSeconds 105.0
    Assert-Equal $report.streams["$p1"].packets 0 "counters start again after each report"
    Assert-Equal $report.streams["$p1"].idle_seconds 3 "a stream that has gone quiet starts its idle clock"
    Assert-Equal $report.streams["$p2"].idle_seconds 5 "the idle clock adds up window after window"
    Send-Packets -Port $p2 -Count 1 -Size 188
    $report = Read-SiteCFeed -Feed $feed -MonoSeconds 106.0
    Assert-Equal $report.streams["$p2"].idle_seconds 0 "the first packet returns the idle clock to zero"
    Assert-Equal $report.streams["$p2"].packets 1 "and it is counted"

    # --- the report is what the listener expects once it is JSON
    $json = ($report | ConvertTo-Json -Depth 5 -Compress)
    $back = $json | ConvertFrom-Json
    Assert-Equal $back.listening $true "JSON: listening"
    Assert-True ($null -ne $back.streams.PSObject.Properties["$p1"]) "JSON: the streams are keyed by port number"
    Assert-True ($null -ne $back.streams."$p2".PSObject.Properties["mbit_per_s"] -and $null -ne $back.streams."$p2".PSObject.Properties["packets"] -and $null -ne $back.streams."$p2".PSObject.Properties["idle_seconds"]) "JSON: the three numbers have the names the listener reads"

    # --- a sleep that drains keeps the packets (so a long heartbeat cycle can never overflow the buffer)
    $script:SiteCFeed = $feed
    Send-Packets -Port $p1 -Count 5
    Wait-WithSiteCDrain -Seconds 1
    Assert-Equal $feed.streams["$p1"].packets 5 "draining during the sleep counts the packets already"
    Assert-Equal $feed.streams["$p1"].udp.Available 0 "and leaves nothing waiting in the socket"
    Close-SiteCFeed -Feed $feed
    $script:SiteCFeed = $null
    Wait-WithSiteCDrain -Seconds 1
    Assert-True $true "a sleep with no listener open is just a sleep"

    # --- closing really frees the ports
    $again = New-Object System.Net.Sockets.UdpClient(New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Loopback, $p1))
    Assert-True ($null -ne $again) "the ports are free again after the listener is closed"
    $again.Close()

    # --- per-second readings (the unpublished design notes, "The per-second readings"): a sample is closed every time the sender drains, and each report carries the new ones
    $q1 = Get-FreeUdpPort
    $q2 = Get-FreeUdpPort
    $fine = Open-SiteCFeed -Group "127.0.0.1" -Ports @($q1, $q2) -InterfaceIp "127.0.0.1" -MonoSeconds 500.0
    Send-Packets -Port $q1 -Count 100
    Receive-SiteCFeed -Feed $fine
    Add-SiteCSecond -Feed $fine -MonoSeconds 502.0
    $samples = $fine.streams["$q1"].samples
    Assert-Equal $samples.Count 1 "closing a sample records one"
    Assert-Equal $samples[0].span 2 "the sample knows how long it measured"
    Assert-Equal $samples[0].mbit ([math]::Round(100 * 1316 * 8 / 2.0 / 1000000, 3)) "its rate is bytes x 8 over the MEASURED time, not over a nominal second"
    Assert-Equal $samples[0].packets 100 "and it counts its packets"
    $firstMbit = $samples[0].mbit          # the list is emptied when a report takes its samples, so keep the figure to compare with
    Assert-Equal $fine.streams["$q2"].samples[0].mbit 0 "a silent stream records a real zero, not a gap"

    Send-Packets -Port $q1 -Count 5
    Receive-SiteCFeed -Feed $fine
    Add-SiteCSecond -Feed $fine -MonoSeconds 502.1
    Assert-Equal $fine.streams["$q1"].samples.Count 1 "a stretch under a quarter of a second is not closed as a sample"
    Add-SiteCSecond -Feed $fine -MonoSeconds 503.0
    Assert-Equal $fine.streams["$q1"].samples.Count 2 "the next close records it"
    Assert-Equal $fine.streams["$q1"].samples[1].packets 5 "the packets of the too-short stretch were carried into the next sample, none lost"
    Assert-Equal $fine.streams["$q1"].samples[1].span 1 "and the span runs from the last closed sample"
    Assert-Equal $fine.streams["$q1"].samples[1].mbit ([math]::Round(5 * 1316 * 8 / 1.0 / 1000000, 3)) "and each byte is counted once: the counters start again after every sample"

    Send-Packets -Port $q1 -Count 20
    $report = Read-SiteCFeed -Feed $fine -MonoSeconds 504.0
    $list = @($report.seconds["$q1"])
    Assert-Equal $list.Count 3 "the report carries every sample closed since the last report, the final one closed at report time"
    Assert-Equal $list[0][0] 2 "a sample is described by its age: the oldest was closed two seconds before the report"
    Assert-Equal $list[2][0] 0 "the newest has age 0"
    Assert-Equal $list[0][1] 2 "then its span"
    Assert-Equal $list[0][2] $firstMbit "then its rate"
    Assert-Equal $list[0][3] 100 "then its packets"
    Assert-Equal $list[2][3] 20 "the sample closed at report time holds the latest packets"
    Assert-Equal $list[2][2] ([math]::Round(20 * 1316 * 8 / 1.0 / 1000000, 3)) "and its rate covers only those packets"
    Assert-Equal $report.streams["$q1"].packets 125 "the ten-second figures are unchanged: all packets since the last report"
    $again = Read-SiteCFeed -Feed $fine -MonoSeconds 505.0
    Assert-Equal @($again.seconds["$q1"]).Count 1 "samples handed over once are not handed over again"

    for ($i = 1; $i -le 100; $i++) { Add-SiteCSecond -Feed $fine -MonoSeconds (505.0 + $i) }
    Assert-Equal $fine.streams["$q1"].samples.Count 60 "the sender never keeps more than 60 samples"
    $capped = Read-SiteCFeed -Feed $fine -MonoSeconds 606.0
    Assert-Equal @($capped.seconds["$q1"]).Count 30 "one report carries at most 30 samples"
    Assert-Equal $capped.seconds["$q1"][29][0] 0 "and they are the newest ones"
    Assert-Equal $capped.seconds["$q1"][0][0] 29 "oldest first"

    $one = Read-SiteCFeed -Feed $fine -MonoSeconds 607.0
    $oneJson = ($one | ConvertTo-Json -Depth 5 -Compress)
    Assert-True ($oneJson -match ('"' + $q1 + '":\[\[')) "JSON: a stream with a single sample is still a list of samples, not a flattened list"
    $payload = (@{ hostname = "X"; site_c_feed = $one } | ConvertTo-Json -Depth 5 -Compress)
    Assert-True ($payload -notmatch "System\.") "JSON: nothing is cut off at the depth the heartbeat is written with"
    Close-SiteCFeed -Feed $fine

    $script:SiteCFeed = Open-SiteCFeed -Group "127.0.0.1" -Ports @($q1, $q2) -InterfaceIp "127.0.0.1" -MonoSeconds (Get-MonotonicSeconds)
    Send-Packets -Port $q1 -Count 50
    Wait-WithSiteCDrain -Seconds 2
    $slept = $script:SiteCFeed.streams["$q1"].samples
    Assert-True ($slept.Count -ge 2 -and $slept.Count -le 3) "a two second sleep closes a sample every second"
    $spansOk = $true
    $packetsSlept = 0
    foreach ($sample in $slept) { if ($sample.span -lt 0.9 -or $sample.span -gt 1.8) { $spansOk = $false }; $packetsSlept += $sample.packets }
    Assert-True $spansOk "each sample from the sleep measured about a second"
    Assert-Equal $packetsSlept 50 "and together they hold every packet"
    Close-SiteCFeed -Feed $script:SiteCFeed
    $script:SiteCFeed = $null

    # --- a PC that cannot listen says so briefly and does not leave a half-open listener behind
    $script:SiteCFeed = $null
    $bad = Get-SiteCFeedReport -Group "224.1.1.1" -Ports @($p1) -LanPrefix "203.0.113." -InterfaceIp "203.0.113.9" -MonoSeconds 200.0
    Assert-Equal $bad.listening $false "an address this PC does not have cannot be joined: listening false"
    Assert-True ($bad.error.Length -gt 0 -and $bad.error.Length -le 80) "the reason is short"
    Assert-True ($null -eq $script:SiteCFeed) "no half-open listener is left"
    Assert-Equal $bad.window_seconds 1 "a window is still given so the report stays valid"
    Assert-Equal @($bad.seconds.Keys).Count 0 "a PC that cannot listen reports no per-second samples"
    $none = Get-SiteCFeedReport -Group "224.1.1.1" -Ports @($p1) -LanPrefix "10.255.255." -MonoSeconds 201.0
    Assert-Equal $none.listening $false "no LAN address: listening false"
    Assert-True ($none.error -like "*address*") "and it says why"
    $line = Get-SiteCLogLine -Report $none
    Assert-True ($line -like "*Site C*") "a log line is produced the first time"
    Assert-True ($null -eq (Get-SiteCLogLine -Report $none)) "and not again while the reason stays the same"
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) FAILED"; exit 1 }
Write-Host "ALL PASSED"
exit 0
