# File:         Test-SendFallback.ps1
# Purpose:      Tests how the senders send a heartbeat: the encrypted Tailscale address first, the LAN address only if that fails, never a retry on the
#               other path after the listener has answered "no" to the key. A fake Invoke-RestMethod stands in for the network (a function shadows
#               the cmdlet), so nothing real is contacted.
# Inputs:       none. Run: powershell -NoProfile -ExecutionPolicy Bypass -File edge\tests\Test-SendFallback.ps1
# Outputs:      PASS/FAIL lines; exit code 0 when everything passed, 1 otherwise.
# Dependencies: both sender scripts, dot-sourced one after the other (their loops do not start when dot-sourced).
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

$script:failures = 0
function Assert-Equal($actual, $expected, $label) {
    if ($actual -ne $expected) { Write-Host "FAIL: $label (got '$actual', expected '$expected')"; $script:failures++ }
    else { Write-Host "PASS: $label" }
}

# --- the fake network ---------------------------------------------------------------------------------------------------------------------------
$script:calls = @()
$script:behaviour = @{}       # url -> "ok" | "down" | 401 | 403 | 500
function Invoke-RestMethod {
    param($Uri, $Method, $Body, $ContentType, $Headers, $TimeoutSec)
    $script:calls += [pscustomobject]@{ Uri = $Uri; Timeout = $TimeoutSec; Token = $Headers["X-Watchdog-Token"]; Bytes = $Body.Length }
    $what = $script:behaviour[$Uri]
    if ($what -eq "ok") { return "ok" }
    $exception = New-Object System.Exception "fake failure for $what"
    if ($what -is [int]) { Add-Member -InputObject $exception -NotePropertyName Response -NotePropertyValue ([pscustomobject]@{ StatusCode = $what }) }
    throw $exception
}

$tailscale = "http://100.64.0.10:8420/heartbeat"
$lan = "http://192.168.7.183:8420/heartbeat"
$bytes = [Text.Encoding]::UTF8.GetBytes('{"hostname":"TEST"}')

function Reset-Fake($tail, $lanBehaviour) { $script:calls = @(); $script:behaviour = @{ $tailscale = $tail; $lan = $lanBehaviour } }

foreach ($sender in @("edge\main\insta_heartbeat_sender.ps1", "edge\backup-server\admax_heartbeat_sender.ps1")) {
    Write-Host "--- $sender"
    . (Join-Path $repo $sender)
    function Write-Log { param([string]$Message) }

    # the address list
    Assert-Equal ((Get-ListenerUrls -Primary $tailscale -Fallback $lan) -join "|") "$tailscale|$lan" "two addresses: Tailscale first, LAN second"
    Assert-Equal @(Get-ListenerUrls -Primary $tailscale -Fallback "").Count 1 "an empty fallback means no fallback (no cleartext path at all)"
    Assert-Equal @(Get-ListenerUrls -Primary $tailscale -Fallback "   ").Count 1 "a blank fallback means no fallback"
    Assert-Equal @(Get-ListenerUrls -Primary $tailscale -Fallback $tailscale).Count 1 "the same address twice is one address"

    # healthy: Tailscale answers, the LAN is never touched
    Reset-Fake "ok" "ok"
    $r = Send-Heartbeat -Urls @($tailscale, $lan) -BodyBytes $bytes -Token "KEY"
    Assert-Equal $r.ok $true "healthy: sent"
    Assert-Equal $r.index 0 "healthy: over the first (Tailscale) address"
    Assert-Equal $script:calls.Count 1 "healthy: the LAN address is not even tried"
    Assert-Equal $script:calls[0].Token "KEY" "the key travels in the header"
    Assert-Equal $script:calls[0].Bytes $bytes.Length "the whole body is sent"

    # Tailscale is down: the LAN carries it, and the very next cycle tries Tailscale first again
    Reset-Fake "down" "ok"
    $r = Send-Heartbeat -Urls @($tailscale, $lan) -BodyBytes $bytes -Token "KEY"
    Assert-Equal $r.ok $true "Tailscale down: still sent"
    Assert-Equal $r.index 1 "Tailscale down: over the second (LAN) address"
    Assert-Equal $script:calls.Count 2 "Tailscale down: two attempts"
    Assert-Equal $script:calls[0].Timeout 3 "a dead first path only costs 3 s"
    Assert-Equal $script:calls[1].Timeout 8 "the last path gets the full 8 s"
    Reset-Fake "ok" "ok"
    $r = Send-Heartbeat -Urls @($tailscale, $lan) -BodyBytes $bytes -Token "KEY"
    Assert-Equal $r.index 0 "Tailscale back: the next heartbeat goes over it again (no sticking to the LAN)"

    # the listener said NO to the key: the other path would say the same, so do not try it
    foreach ($code in @(401, 403)) {
        Reset-Fake $code "ok"
        $r = Send-Heartbeat -Urls @($tailscale, $lan) -BodyBytes $bytes -Token "KEY"
        Assert-Equal $r.ok $false "HTTP ${code}: not sent"
        Assert-Equal $r.refused $code "HTTP ${code}: reported as refused"
        Assert-Equal $script:calls.Count 1 "HTTP ${code}: the LAN is NOT tried (the key would be offered twice for nothing)"
    }

    # a 500 or any other answer is not a verdict on the key: try the other path
    Reset-Fake 500 "ok"
    $r = Send-Heartbeat -Urls @($tailscale, $lan) -BodyBytes $bytes -Token "KEY"
    Assert-Equal $r.ok $true "HTTP 500 on the first path: the second path is tried and works"

    # both down
    Reset-Fake "down" "down"
    $r = Send-Heartbeat -Urls @($tailscale, $lan) -BodyBytes $bytes -Token "KEY"
    Assert-Equal $r.ok $false "both paths down: not sent"
    Assert-Equal ($null -eq $r.refused) $true "both paths down: not a refusal"
    Assert-Equal ([string]::IsNullOrEmpty($r.error)) $false "both paths down: the error is kept for the log"
    Assert-Equal $script:calls.Count 2 "both paths down: each tried once"

    # no fallback configured: one address, the full timeout
    Reset-Fake "down" "ok"
    $r = Send-Heartbeat -Urls @($tailscale) -BodyBytes $bytes -Token "KEY"
    Assert-Equal $r.ok $false "no fallback: a dead path means not sent"
    Assert-Equal $script:calls.Count 1 "no fallback: only the one address is used (the LAN is never contacted)"
    Assert-Equal $script:calls[0].Timeout 8 "no fallback: the single address gets the full 8 s"
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) FAILED"; exit 1 }
Write-Host "ALL PASSED"
