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
   `requestAnimationFrame` canvas animation; black: a URL the local page
   server 404s -- `pulsar-bundle-full`'s own README documents this as CEF
   rendering blank/black; frozen: a page that paints real detail once with
   no further updates), records each with `pulsar.record.start()/stop()`
   (the real x264 path), measures all three, and cross-checks
   `@clodocapeo/pgm-correlator`'s (#230) `extractVisualEvents` verdict
   against the same three real recordings -- concordance is the
   capture&harr;PGM compatibility proof; a divergence would be a real #230
   finding (none found: see the PR / issue thread for pasted real numbers).

## Running the live integration suite

Opt-in only (see "CI gap" below):

```bash
PULSAR_LIVE_CAPTURE_COMPAT=1 npm run test -w @clodocapeo/capture-pgm-compat
```

If this package's own `@clodocapeo/pulsar-bundle-full` dependency hasn't
downloaded its ~150MB binaries (e.g. a worktree checkout reusing an
already-downloaded sibling checkout instead of triggering a fresh
postinstall download), point at them explicitly:

```bash
PULSAR_BUNDLE_FULL_BINARIES_PATH=/path/to/pulsar-bundle-full/binaries \
PULSAR_LIVE_CAPTURE_COMPAT=1 npm run test -w @clodocapeo/capture-pgm-compat
```

## CI gap (known, not silently left)

`.github/workflows/pipeline.yml`'s `npm run test -w <pkg>` for every TS
package (including this one) runs ONLY inside the `npm-publish` job --
`runs-on: ubuntu-latest`, gated `if: startsWith(github.ref, 'refs/tags/v')`.
It never runs on PRs, and ubuntu-latest cannot execute a Windows CEF binary
regardless. `frame-health.test.ts` (the ffmpeg-only half) would actually run
there fine; `live-capture-compat.test.ts` never will, anywhere, without a
new `windows-2022` TS-test stage -- adding one is a CI/infra decision
(Keeper's call), intentionally not made by this package. The live suite is
gated behind `PULSAR_LIVE_CAPTURE_COMPAT` so `npm test` stays green
everywhere it currently runs; run it locally on Windows to get real
coverage of the actual capture path.
