# @clodocapeo/capture-pgm-compat

Proves Pulsar's capture path and its recorded PGM are compatible for
[ZabLaboratory/Pulsar#231](https://github.com/ZabLaboratory/Pulsar/issues/231):
what a real, rendered source produces IS what gets recorded, measurably --
and a source that reports OK (`CreateInput`/`SetInputSettings` both succeed)
while being visually dead is caught rather than accepted. GPL-2.0-or-later,
not published (internal test tooling, `"private": true`).

## What it proves, and how

A per-frame health measure (`src/frame-health.ts`) over a real recorded
`.mp4`:

- **spatialStddev** -- population stddev of pixel values WITHIN one
  downscaled grayscale frame. Near 0 for a flat/solid-colour frame; high for
  real visual detail.
- **temporalDiff** -- pixel-wise mean absolute difference between
  consecutive frames. Near 0 when nothing changes frame to frame.

Neither axis alone is a sufficient oracle: a **black/absent** source fails
spatial (flat) but a **frozen** source (real detail painted once, never
updated again -- the more vicious of #231's two named CEF failure modes)
scores perfectly normal on spatial and only fails temporal. `src/threshold.ts`
derives an acceptance threshold per axis from the actual observed separation
between a healthy sample and the degraded scenario(s) that axis exists to
catch -- see its docstring for why pooling every degraded scenario into
every axis's threshold is wrong (a frozen sample is spatially healthy BY
CONSTRUCTION and would pollute the spatial threshold toward the healthy
sample itself).

Two proof layers:

1. **`tests/frame-health.test.ts`** -- CI-safe. Builds three real
   ffmpeg-generated fixtures (`testsrc` = healthy-shaped, `color=black` =
   black-shaped, one real `testsrc` frame extracted and held static =
   frozen-shaped) and proves the measure discriminates all three, including
   the combined-oracle logic. No Pulsar needed; runs anywhere ffmpeg does.
2. **`tests/live-capture-compat.test.ts`** -- the real proof. Spawns a REAL
   full Pulsar (`pulsar.exe` + CEF via `@clodocapeo/pulsar-bundle-full`),
   drives a real `browser_source` through three real local pages (healthy:
   `requestAnimationFrame` canvas animation; black: the harness's deliberately degraded local-page scenario; frozen: a page that paints real detail once with
   no further updates), records each with `pulsar.record.start()/stop()`
   (the real x264 path), measures all three, and cross-checks
   `@clodocapeo/pgm-correlator`'s (#230) `extractVisualEvents` verdict
   against the same three real recordings -- concordance is the
   capture&harr;PGM compatibility proof; a divergence would be a real #230
   finding (none found: see the PR / issue thread for pasted real numbers).

## Running the live integration suite

The native suite is explicitly opt-in; run from the repository root after
building its workspace dependencies and a matching full native runtime:

```powershell
$env:PULSAR_LIVE_CAPTURE_COMPAT = "1"
$env:PULSAR_BUNDLE_FULL_BINARIES_PATH = "D:/path/to/Pulsar/upstream/build_x64/rundir/RelWithDebInfo"
npm run test -w @clodocapeo/capture-pgm-compat
```

The explicit path is read by this test harness, not by the public bundle
`spawn()` API. Supply the full rundir root, not `bin/64bit`.
FFmpeg/ffprobe must be available; the tests use the actual native binary and
local pages/recordings, not a published Twitch feed.

## CI coverage in 3.0.0

The pipeline now has a Windows `capture-pgm-compat` job consuming the built
`pulsar-rundir` artifact. It installs/builds workspace dependencies and runs
this package. The old “there is no Windows TS integration stage” statement
is obsolete.

The job detects a physical GPU. With one, it sets
`PULSAR_LIVE_CAPTURE_COMPAT=1`; without one, it runs the portable measurement
tests and explicitly reports that accelerated native CEF proof was not run.
A passing no-GPU job is not an accelerated capture pass. Keep real hardware
evidence separate from hosted CI.

## Interpretation and limits

The spatial/temporal oracle distinguishes this corpus's healthy, black and
frozen scenarios. An HTTP status or a successful source-control request alone
is not that proof. A 404 does not universally imply a black CEF frame.

Temporal correlation with pgm-correlator does not recover an arbitrary
scene's identity from pixels. These are controlled local recordings, not
end-to-end authenticated Orion/Blue production evidence or physical-display
latency measurements.

Record exact runtime revision, selected encoder, hardware, enabled test mode
and resulting files when citing a run. Use the
[development guide](../../docs/DEVELOPMENT.md) for the broader gate matrix.
