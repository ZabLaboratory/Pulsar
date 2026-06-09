# Runbook — M10 Twitch broadcast: prove REAL ingest, not StartDestination

**Applies to:** the Keeper antenna run that broadcasts the `zab-transition`
playout to the porteur's Twitch channel (`scripts/probe-m10-canvas-live.py
--transition-scene --broadcast`).
**Fixed by:** commit `6aaefa1` on branch `keeper/m10-real-ingest-proof`
(probe instrumentation), antenna run 2026-06-09.

---

## Symptom / incident

Earlier M10 antenna runs printed **`StartDestination started=true — LIVE on
Twitch`** and exited, yet the porteur saw **no stream** on the channel.

Two compounding causes:

1. **False-positive "LIVE".** `StartDestination` calls `obs_output_start()`,
   which only means the start was *accepted*. The RTMP connect to
   `rtmp://live.twitch.tv/app/<key>` happens **asynchronously** on the output's
   worker thread. `started=true` proves **nothing** about whether Twitch
   ingested a single byte (refused/dead key would still return `started=true`).
2. **Run too short (~10s).** Even when ingest worked, a ~10s VOD never had time
   to appear on the channel; the porteur saw nothing.

## Root cause

The probe declared success on the wrong signal and held the stream for far too
little time. There was **no measurement of real bytes pushed to Twitch**.

## The real proof (what to read)

The pulsar multi-stream plugin creates one `rtmp_output` per destination, named
**`PulsarDest_<id>`** (`plugins/pulsar-multi-stream/src/plugin-main.cpp`,
`ensure_output`); twitch kind targets `rtmp://live.twitch.tv/app/`
(`TWITCH_INGEST_URL`). The pulsar-websocket fork exposes
**`GetOutputStatus{outputName}`** returning `obs_output_get_total_bytes`
(`outputBytes`), `outputActive`, `outputDuration`
(`plugins/pulsar-websocket/src/requesthandler/RequestHandler_Outputs.cpp`).

So real ingest is read straight off the Twitch output over obs-ws:

- **`outputBytes` MUST grow monotonically** = bytes really reach Twitch. Flat 0
  = nothing ingested (key refused/dropped).
- **`outputActive` MUST stay true.** A flip to false post-connect = Twitch
  dropped us.
- **RTMP stdout** (`upstream/plugins/obs-outputs/rtmp-stream.c`) logs verbatim:
  `Connecting to RTMP URL ...` (embeds the key → **redact**),
  `Connection to ... successful` (real handshake), and any post-connect
  `Disconnected from ...` / `Connection to ... failed` (refused/dead key).

The probe now polls all of this every 5s for the whole `--on-air-secs` hold and
gates the verdict on measured growth — never on `started=true`.

## Fix / procedure (the antenna run)

```bash
PY="C:/Users/Mathias/AppData/Local/Programs/Python/Python311/python.exe"
# Load the etage-1 key into env WITHOUT echoing it:
export TWITCH_STREAM_KEY=$("$PY" -c "import re,pathlib;\
 t=pathlib.Path('D:/Documents/Zab/.env.pulsar').read_text(encoding='utf-8');\
 print(re.search(r'^TWITCH_STREAM_KEY=(.*)$',t,re.M).group(1).strip())")
# Go on air, hold ~80s, prove real ingest:
"$PY" scripts/probe-m10-canvas-live.py --transition-scene --broadcast \
    --allow-blank --hold-ms 800 --on-air-secs 80
```

A clean result prints the **REAL-INGEST VERDICT** block:
`outputBytes grew = True`, `outputActive stayed_true = True`, RTMP
`successful = True`, `disconnected/failed = False`, and
`==> PROVEN: Twitch INGESTED the stream`.

## Verification — measured antenna run (2026-06-09)

- `outputBytes`: 5,490,744 → 65,429,817 = **59.94 MB pushed** over **83s** at a
  steady **~755–878 KiB/s** (~6 Mbps, the configured bitrate).
- `outputActive` true for all 16 samples; never dropped. Congestion 0.0, 0
  skipped frames.
- RTMP verbatim: `Connecting to RTMP URL rtmp://live.twitch.tv/app/...` →
  `Connection to rtmp://live.twitch.tv/app/ (2600:...) successful`; **no**
  post-connect disconnect/failure.
- VOD `build/m10-live-vod/pulsar-20260609-142403.mp4` = 70.79 MB (the full hold).
- C-MECH clean (two bare `SetCurrentProgramScene` per cut, zero native
  transition); C-SEC clean (raw key in zero stdout/PNG/log on disk).

## Failure mode — if the key is dead

If `outputBytes` stagnates, `outputActive` flips false, or an RTMP
`Disconnected`/`failed` appears **after** connect, the verdict prints
`==> NOT PROVEN ... request a FRESH Twitch key` and the run **exits 1**. That is
a real failure, not a "LIVE" false positive — ask the porteur for a fresh key.

## Notes / scope decisions

- **Orion active scene was NOT swapped.** The brief mentioned pushing+activating
  `zab-transition` on live Orion (`POST /scenes/{id}/push` + `POST
  /show/active-scene`) with rollback to `18fecbd4`. Orion exposes no
  GET-list/active route to read the current pushed-version, so a verified
  rollback baseline could not be captured. Since real ingest is independent of
  whether the browser_source renders from VPS-Solar or the faithful local page,
  the run used the local white+logo render (engine=local-static-page) and left
  Orion untouched (7 scenes loaded, health ok before and after). A live
  active-scene swap is shared-topology and belongs to a Conduit-validated step.
- **Visual MID render headless.** The transition MID frame is black headless
  (CEF/Solar does not paint without a desktop GPU surface); `--allow-blank`
  defers the white+logo *visual* proof. This run's contract is **real ingest**,
  which is independent of CEF paint.
- **Rollback for this fix:** none destructive. The change is probe
  instrumentation; revert the commit to restore prior behaviour. No VPS or Orion
  state was modified.
