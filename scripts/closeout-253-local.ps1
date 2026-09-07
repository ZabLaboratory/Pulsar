param([ValidateSet('summarize','archive')][string]$Stage = 'summarize')
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ($repo -ne 'D:\Documents\Zab\Pulsar\.worktrees\eleven-253-latency') { throw 'This local evidence helper is pinned to its owning worktree' }
$ev = Join-Path $repo 'evidence/253/eleven'
$delivery = 'D:\Documents\Zab\Artifacts\2026-09-07\pulsar-253-optimized'
$summaryName = '20260907T163200Z-native-study.json'
$archiveName = '20260907T163000Z-native-run-evidence.zip'
function Read-Json([string]$path) { Get-Content -LiteralPath $path -Raw | ConvertFrom-Json }
if ($Stage -eq 'summarize') {
    $runs = foreach ($dir in Get-ChildItem -LiteralPath $ev -Directory) {
        foreach ($codec in @('x264','nvenc')) {
            $file = Join-Path $dir.FullName "$codec-report.json"
            if (-not (Test-Path -LiteralPath $file)) { continue }
            $r = Read-Json $file
            $settings = Read-Json (Join-Path $dir.FullName 'run-settings.json')
            $criteria = [ordered]@{}
            foreach ($prop in $r.criteria.PSObject.Properties) { $criteria[$prop.Name] = $prop.Value.status }
            [ordered]@{ run=$dir.Name; encoder=$codec; report_sha256=(Get-FileHash -LiteralPath $file).Hash;
                settings=$settings; status=$r.status; latency=$r.latency; criteria=$criteria;
                callback_to_demux=$r.ac12a; native_encoder_stages=$r.ac12b.stage_distributions }
        }
    }
    $av = foreach ($dir in Get-ChildItem -LiteralPath $ev -Directory -Filter '*av*') {
        $file=Join-Path $dir.FullName 'report.json'
        if (-not(Test-Path -LiteralPath $file)){continue}
        $r=Read-Json $file
        $values=@($r.measured.pairs.video_minus_audio_ms)
        $maxAbs=($values | ForEach-Object {[math]::Abs($_)} | Measure-Object -Maximum).Maximum
        [ordered]@{run=$dir.Name; current_readback=$r.current_readback; default_readback=$r.default_readback;
            binary_sha256=$r.binary_sha256; pairs=$values.Count; video_minus_audio_min_ms=($values|Measure-Object -Minimum).Minimum;
            video_minus_audio_max_ms=($values|Measure-Object -Maximum).Maximum; within_one_frame_plus_detector_tick=($maxAbs -le (1000/60+1));
            source_pairs=$r.source.pairs.Count; source_max_abs_offset_ms=($r.source.pairs.video_minus_audio_ms|ForEach-Object {[math]::Abs($_)}|Measure-Object -Maximum).Maximum;
            note='One-frame tolerance includes 1 ms audio envelope sampling; no optical/display or phase-zero claim.'}
    }
    $audio = foreach($codec in @('x264','nvenc')) {
        $file=Join-Path $ev "20260907T161400Z-audio-$codec.json"
        $r=Read-Json $file
        $log=Get-Content -LiteralPath (Join-Path $ev "20260907T161400Z-audio-$codec.log")
        if(-not ($log -match '^PASS: 100 Cuts')){throw "Missing audio success for $codec"}
        [ordered]@{encoder=$codec; cuts=$r.commits.Count; route_snapshots=$r.route_snapshots.Count; route_id=$r.route_id;
            results=@($log | Where-Object {$_ -match 'AAC audio verified|^PASS:'}); evidence_sha256=(Get-FileHash -LiteralPath $file).Hash}
    }
    $native=Get-Content -LiteralPath (Join-Path $ev '20260907T152600Z-native-decoder.jsonl') | ForEach-Object {$_|ConvertFrom-Json} | Where-Object kind -eq 'frame'
    $reference=@(Get-Content -LiteralPath (Join-Path $ev '20260907T152800Z-native-reference-video-framemd5.txt')|Where-Object {$_ -notmatch '^#' -and $_.Trim()})
    if($native.Count -ne 876 -or $reference.Count -ne 876){throw 'Offline video frame count mismatch'}
    for($i=0;$i -lt $native.Count;$i++) {
        $parts=$reference[$i].Split(',') | ForEach-Object {$_.Trim()}
        if($parts[0] -ne '0' -or $native[$i].md5 -ne $parts[5] -or $native[$i].pts_ms -ne [math]::Round([double]$parts[2]*1000/60)) {
            throw "Offline pixel/PTS reference mismatch at video frame $i"
        }
    }
    $runtime=Join-Path $delivery 'pulsar-windows-x64-full-v2.0.0'
    $runtimeHashes=@(Get-ChildItem -LiteralPath $runtime -Recurse -File | ForEach-Object {
        [ordered]@{path=[IO.Path]::GetRelativePath($runtime,$_.FullName).Replace('\','/'); bytes=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName).Hash}
    })
    $summary=[ordered]@{schema='pulsar.253.native-optimization.v1'; code_revision='5fba882446741f863744f68d1c5726aeb2dacc94';
        generated_at_utc=[DateTime]::UtcNow.ToString('o'); scope='Windows 1080p60 NV12 CPU/x264; local fully decoded first changed marker, not display';
        promoted='current readback plus media-timestamp alignment, automatic on the qualified CPU path';
        not_promoted=@('NVENC async output','lower B-frame quality profiles'); runs=@($runs); audio=@($audio); av=@($av);
        receiver_reference_video_frames=$native.Count; receiver_pixel_md5_and_pts_match=$true; runtime_files=$runtimeHashes;
        python_regression=(Get-Content -LiteralPath (Join-Path $ev '20260907T161400Z-python-regression.log') | Select-Object -Last 1);
        ctest=@{passed=21; total=22; failed='pulsar-dir-hardening-probe'; reason='SeRestorePrivilege absent: two owner-change gestures unexercised; requirement not disabled'; log='20260907T162000Z-final-ctest.log'};
        limits=@('No NVENC end-to-end latency improvement claimed','No single-lane capacity qualification','No 4K qualification','No remote service or physical display proof','Local candidate, no remote push/merge/deploy')}
    $summary | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath (Join-Path $ev $summaryName) -Encoding utf8
    Write-Output "Summary: $($runs.Count) latency runs, $($av.Count) AV runs, $($runtimeHashes.Count) runtime hashes"
    return
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zipPath=Join-Path $ev $archiveName
if(Test-Path -LiteralPath $zipPath){throw 'Archive exists'}
$dirs=@(Get-ChildItem -LiteralPath $ev -Directory)
$flat=@(Get-ChildItem -LiteralPath $ev -File | Where-Object {$_.Name -ge '20260907T144500Z' -and $_.Name -notin @($archiveName,$summaryName,'20260907T144500Z-native-async-plan.md')})
$files=@($flat)+@($dirs | ForEach-Object {Get-ChildItem -LiteralPath $_.FullName -Recurse -File})
$archiveFiles=@($files | Where-Object {$_.Extension -notin @('.mp4','.mkv','.flv','.wav','.yuv')})
$zip=[IO.Compression.ZipFile]::Open($zipPath,[IO.Compression.ZipArchiveMode]::Create)
try {
    foreach($file in $archiveFiles) {
        $rel=[IO.Path]::GetRelativePath($ev,$file.FullName).Replace('\','/')
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip,$file.FullName,$rel,[IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
} finally {$zip.Dispose()}
$zip=[IO.Compression.ZipFile]::OpenRead($zipPath)
try {
    if($zip.Entries.Count -ne $archiveFiles.Count){throw 'Archive count mismatch'}
    foreach($entry in $zip.Entries) {
        $original=Join-Path $ev $entry.FullName
        $stream=$entry.Open()
        try{$hash=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($stream))}finally{$stream.Dispose()}
        if($hash -ne (Get-FileHash -LiteralPath $original).Hash){throw "Archive mismatch $($entry.FullName)"}
    }
} finally {$zip.Dispose()}
$raw=Join-Path $delivery 'raw-evidence'
if(Test-Path -LiteralPath $raw){throw 'Raw evidence destination exists'}
New-Item -ItemType Directory -Path $raw | Out-Null
foreach($item in @($dirs)+@($flat)) {
    $resolved=(Resolve-Path -LiteralPath $item.FullName).Path
    if(-not $resolved.StartsWith($ev+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Evidence path escaped owner'}
    Move-Item -LiteralPath $resolved -Destination $raw
}
Copy-Item -LiteralPath $zipPath -Destination (Join-Path $delivery 'run-evidence.zip')
Copy-Item -LiteralPath (Join-Path $ev $summaryName) -Destination (Join-Path $delivery 'study.json')
Write-Output "Verified $($archiveFiles.Count) archive entries; original evidence and media retained in $raw"
