# File:         Test-StandbyCopy.ps1
# Purpose:      Tests the best-effort copy of each heartbeat to the standby listener (standby spec, senders): the copy carries `primary_ok`, the same key in the header only,
#               goes out in the background so the sender's loop never waits for it, and can never throw or delay anything even when the standby hangs, refuses the key or
#               does not exist. A REAL local web server (HttpListener on 127.0.0.1) plays the standby, including one that accepts a request and never answers.
# Inputs:       none. Run: powershell -NoProfile -ExecutionPolicy Bypass -File edge\tests\Test-StandbyCopy.ps1
# Outputs:      PASS/FAIL lines; exit code 0 when everything passed, 1 otherwise.
# Dependencies: both sender scripts, dot-sourced one after the other (their loops do not start when dot-sourced); .NET HttpListener and HttpClient.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

$script:failures = 0
function Assert-Equal($actual, $expected, $label) {
    if ($actual -ne $expected) { Write-Host "FAIL: $label (got '$actual', expected '$expected')"; $script:failures++ }
    else { Write-Host "PASS: $label" }
}
function Assert-True($condition, $label) { Assert-Equal ([bool]$condition) $true $label }

function Get-FreePort {
    $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $probe.Start(); $port = $probe.LocalEndpoint.Port; $probe.Stop()
    return $port
}

function Read-Request($context) {
    $reader = New-Object System.IO.StreamReader($context.Request.InputStream, [Text.Encoding]::UTF8)
    return [pscustomobject]@{ Body = $reader.ReadToEnd(); Token = $context.Request.Headers["X-Watchdog-Token"]; Method = $context.Request.HttpMethod; Path = $context.Request.RawUrl; Type = $context.Request.ContentType }
}

function Answer($context, [int]$code) { $context.Response.StatusCode = $code; $context.Response.Close() }

foreach ($sender in @("edge\main\insta_heartbeat_sender.ps1", "edge\backup-server\admax_heartbeat_sender.ps1")) {
    Write-Host "--- $sender"
    . (Join-Path $repo $sender)
    $script:logged = @()
    function Write-Log { param([string]$Message) $script:logged += $Message }
    function Reset-State { $script:StandbyLog = @{}; $script:StandbyPending = $null; $script:logged = @() }

    # --- the flag
    Reset-State
    Assert-Equal (Add-PrimaryFlag -Json '{"hostname":"MAIN","n":1}' -Ok $true) '{"hostname":"MAIN","n":1,"primary_ok":true}' "primary_ok true is added to the end of the object"
    Assert-Equal (Add-PrimaryFlag -Json '{"hostname":"MAIN"}' -Ok $false) '{"hostname":"MAIN","primary_ok":false}' "primary_ok false is added"
    Assert-Equal (Add-PrimaryFlag -Json '{}' -Ok $true) '{"primary_ok":true}' "an empty object gets a valid first member"
    Assert-Equal (Add-PrimaryFlag -Json "  {`"a`":1}  `r`n" -Ok $true) '{"a":1,"primary_ok":true}' "spaces and a newline around the JSON do not matter"
    foreach ($odd in @("", "not json", "[1,2]", '"text"', "{unterminated")) { Assert-Equal (Add-PrimaryFlag -Json $odd -Ok $true) $odd "not a JSON object, returned unchanged: '$odd'" }
    $parsed = (Add-PrimaryFlag -Json '{"hostname":"MAIN","instances":[{"feed":"usa"}]}' -Ok $true) | ConvertFrom-Json
    Assert-Equal $parsed.primary_ok $true "the result is valid JSON"
    Assert-Equal $parsed.instances[0].feed "usa" "and keeps the other fields intact"

    # --- off when no address is given
    Reset-State
    $started = Get-Date
    Send-StandbyCopy -Url "" -Json '{"hostname":"MAIN"}' -PrimaryOk $true -Token "KEY"
    Send-StandbyCopy -Url "   " -Json '{"hostname":"MAIN"}' -PrimaryOk $true -Token "KEY"
    Assert-True ($null -eq $script:StandbyPending) "an empty address sends nothing at all"
    Assert-Equal $script:logged.Count 0 "and logs nothing"

    # --- a real standby (each scenario gets its OWN fresh listener, so a request left unanswered by one can never be picked up by the next)
    function New-FakeStandby {
        $port = Get-FreePort
        $listener = [System.Net.HttpListener]::new()
        $listener.Prefixes.Add("http://127.0.0.1:$port/")
        $listener.Start()
        return [pscustomobject]@{ Http = $listener; Url = "http://127.0.0.1:$port/heartbeat" }
    }
    function Stop-FakeStandby($standby) { try { $standby.Http.Abort() } catch { } }
    function Take-Request($standby, [int]$waitMs = 5000) {
        $waiting = $standby.Http.GetContextAsync()
        if (-not $waiting.Wait($waitMs)) { return $null }
        return $waiting.Result
    }

    # scenario 1: the copy arrives with the flag, the key in the header only, and the call does not wait
    $standby = New-FakeStandby
    try {
        Reset-State
        $started = Get-Date
        Send-StandbyCopy -Url $standby.Url -Json '{"hostname":"TEST","n":1}' -PrimaryOk $true -Token "KEY-123"
        $elapsed = ((Get-Date) - $started).TotalMilliseconds
        $context = Take-Request $standby
        Assert-True ($null -ne $context) "the standby receives the copy"
        $got = Read-Request $context
        Answer $context 200
        Assert-Equal $got.Method "POST" "it is a POST"
        Assert-Equal $got.Path "/heartbeat" "to the address given"
        Assert-Equal $got.Body '{"hostname":"TEST","n":1,"primary_ok":true}' "the body is the heartbeat plus primary_ok"
        Assert-Equal $got.Token "KEY-123" "the key travels in the header"
        Assert-True (-not $got.Path.Contains("KEY-123") -and -not $got.Body.Contains("KEY-123")) "the key is never in the address or the body"
        Assert-True ($got.Type -like "application/json*") "the content type is JSON"
        Assert-True ($elapsed -lt 2500) "the call returned without waiting for the answer (took $([int]$elapsed) ms)"

        Send-StandbyCopy -Url $standby.Url -Json '{"hostname":"TEST"}' -PrimaryOk $false -Token "KEY-123"
        $context = Take-Request $standby
        Assert-True ($null -ne $context) "the second copy arrives"
        $got = Read-Request $context; Answer $context 200
        Assert-Equal $got.Body '{"hostname":"TEST","primary_ok":false}' "a failed post to Core is reported as primary_ok false"
        Start-Sleep -Milliseconds 400
        Send-StandbyCopy -Url $standby.Url -Json '{"hostname":"TEST"}' -PrimaryOk $true -Token "KEY-123"       # picks up the previous result
        $context = Take-Request $standby; if ($context) { Answer $context 200 }
        Assert-Equal $script:logged.Count 0 "good answers log nothing"
    }
    finally { Stop-FakeStandby $standby }

    # scenario 2: the standby accepts the connection and NEVER answers: the sender must not wait, and says so once
    $standby = New-FakeStandby
    try {
        Reset-State
        $started = Get-Date
        Send-StandbyCopy -Url $standby.Url -Json '{"hostname":"TEST"}' -PrimaryOk $true -Token "KEY"
        $elapsed = ((Get-Date) - $started).TotalMilliseconds
        $hung = Take-Request $standby
        Assert-True ($elapsed -lt 1500) "a standby that never answers costs the sender nothing (returned in $([int]$elapsed) ms, the limit is 3000)"
        Start-Sleep -Milliseconds 3600                                                                  # past the 3 s limit
        $started = Get-Date
        Send-StandbyCopy -Url $standby.Url -Json '{"hostname":"TEST"}' -PrimaryOk $true -Token "KEY"    # next cycle: notes the failure
        $elapsed = ((Get-Date) - $started).TotalMilliseconds
        Assert-True ($elapsed -lt 1500) "the next cycle is not slowed either (returned in $([int]$elapsed) ms)"
        Assert-Equal $script:logged.Count 1 "the failure is noted once"
        Assert-True ($script:logged[0] -match "standby" -and $script:logged[0] -match "Core was not affected") "the note says it is the standby copy and that Core was not affected"
        Assert-True (-not ($script:logged[0] -match "KEY")) "the note never contains the key"
        $hung2 = Take-Request $standby 3000
        Start-Sleep -Milliseconds 3600
        Send-StandbyCopy -Url $standby.Url -Json '{"hostname":"TEST"}' -PrimaryOk $true -Token "KEY"
        Assert-Equal $script:logged.Count 1 "a second failure straight after is rate-limited (not logged again)"
    }
    finally { Stop-FakeStandby $standby }

    # scenario 3: the standby refuses this machine's key
    $standby = New-FakeStandby
    try {
        Reset-State
        Send-StandbyCopy -Url $standby.Url -Json '{"hostname":"TEST"}' -PrimaryOk $true -Token "WRONG"
        $context = Take-Request $standby; Answer $context 403
        Start-Sleep -Milliseconds 600
        Send-StandbyCopy -Url $standby.Url -Json '{"hostname":"TEST"}' -PrimaryOk $true -Token "WRONG"
        Assert-Equal $script:logged.Count 1 "a refusal is noted"
        Assert-True ($script:logged[0] -match "403" -and $script:logged[0] -match "refused") "the note names the refusal"
    }
    finally { Stop-FakeStandby $standby }

    # --- nothing listens there, and an address that is not an address: never an exception
    Reset-State
    $deadPort = Get-FreePort
    Send-StandbyCopy -Url "http://127.0.0.1:$deadPort/heartbeat" -Json '{"hostname":"TEST"}' -PrimaryOk $true -Token "KEY"
    Start-Sleep -Milliseconds 3600                      # a refused connection to a closed loopback port takes a second or two to fail on Windows
    Send-StandbyCopy -Url "http://127.0.0.1:$deadPort/heartbeat" -Json '{"hostname":"TEST"}' -PrimaryOk $true -Token "KEY"
    Assert-Equal $script:logged.Count 1 "nothing listening: noted once, no exception"
    # The note must say what really went wrong, not an internal error from reading the result of a failed request, and the cycle that notes it must still send ITS copy.
    Assert-True ($script:logged[0] -match "failed \(.+\)" -and $script:logged[0] -notmatch "Exception getting|Result") "nothing listening: the note gives the real reason ($($script:logged[0]))"
    Assert-True ($null -ne $script:StandbyPending) "nothing listening: noting last cycle's failure does not cost this cycle its copy"
    Reset-State
    $threw = $false
    try { Send-StandbyCopy -Url "not a url at all" -Json '{"hostname":"TEST"}' -PrimaryOk $true -Token "KEY" } catch { $threw = $true }
    Assert-True (-not $threw) "an address that is not an address never throws"
    Assert-True ($script:logged.Count -le 1) "and logs at most one line"
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) FAILED"; exit 1 }
Write-Host "ALL PASSED"
