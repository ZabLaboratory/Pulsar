param(
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string]$CaptureWindow,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [string]$Revision = 'ad2c290bb7ab1f73edc68add30fc31d35a577adc',
    [ValidateSet('x264','nvenc')][string[]]$Encoders = @('x264','nvenc'),
    [int]$Takes = 100,
    [ValidateSet('packet','candidate','marker')][string]$ReceiverMode = 'candidate',
    [switch]$FreshFramePoll
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$output = [IO.Path]::GetFullPath($OutputDir)
if (Test-Path -LiteralPath $output) { throw "Output already exists: $output" }
New-Item -ItemType Directory -Path $output | Out-Null
$keyPath = Join-Path $repo '.env.253-trace-key.dpapi'
Add-Type -AssemblyName System.Security
if (-not (Test-Path -LiteralPath $keyPath)) {
    $key = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    $protected = [Security.Cryptography.ProtectedData]::Protect($key, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    [IO.File]::WriteAllBytes($keyPath, $protected)
}
$key = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($keyPath), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
$previousKey = $env:PULSAR_TRACE_HMAC_KEY
$previousPoll = $env:PULSAR_DSHOW_FRESH_FRAME_POLL
$env:PULSAR_TRACE_HMAC_KEY = [Convert]::ToHexString($key).ToLowerInvariant()
$env:PULSAR_DSHOW_FRESH_FRAME_POLL = if ($FreshFramePoll) { '1' } else { '0' }
try {
    $bin = Split-Path $Exe -Parent
    $runtime = Split-Path (Split-Path $bin -Parent) -Parent
    $binaries = @($Exe, (Join-Path $bin 'obs.dll'), (Join-Path $runtime 'obs-plugins/64bit/obs-nvenc.dll'), (Join-Path $runtime 'data/obs-plugins/win-dshow/obs-virtualcam-module64.dll'))
    Get-FileHash -Algorithm SHA256 -LiteralPath $binaries | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'binary.json')
    @{ receiver_mode = $ReceiverMode; fresh_frame_poll = [bool]$FreshFramePoll;
       binary_revision = $Revision; takes_per_codec = $Takes; workload_layout = 'visible-wgc-left-cef-right-v1';
       scripts = @(Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $PSScriptRoot 'probe-dual-lane.py'), (Join-Path $PSScriptRoot 'probe-take-latency.py'))
     } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $output 'run-settings.json')
    foreach ($codec in $Encoders) {
        $trace = Join-Path $output "$codec.jsonl"
        $decoderArgs = @()
        if ($ReceiverMode -ne 'packet') { $decoderArgs += '--decode-rtmp' }
        if ($ReceiverMode -eq 'marker') { $decoderArgs += '--audit-decoded-marker' }
        & python (Join-Path $PSScriptRoot 'probe-dual-lane.py') --exe $Exe --encoder $codec --takes $Takes --trace $trace --record-dir (Join-Path $output "$codec-recordings") --build-revision $Revision --capture-window $CaptureWindow --cef-workload --rtmp-receiver @decoderArgs --return-transport cpu *> (Join-Path $output "$codec.log")
        $probeExit = $LASTEXITCODE
        Write-Host "$codec probe exit=$probeExit"
        if ($probeExit -ne 0) {
            Get-Content -LiteralPath (Join-Path $output "$codec.log") -Tail 12
            throw "$codec probe failed; evidence preserved in $output"
        }
        & python (Join-Path $PSScriptRoot 'probe-take-latency.py') --trace $trace --min-takes $Takes --output (Join-Path $output "$codec-report.json") *> (Join-Path $output "$codec-parser.log")
        Write-Host "$codec analyzer exit=$LASTEXITCODE; inspect report for latency failures and unproven capacity (no single-lane reference run)"
    }
} finally {
    $env:PULSAR_TRACE_HMAC_KEY = $previousKey
    $env:PULSAR_DSHOW_FRESH_FRAME_POLL = $previousPoll
}
