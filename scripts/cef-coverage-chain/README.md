# Vision → Solar → Prism → CEF atlas chain probe

This private probe binds one real Vision `/2` `frame-submitted` receipt to a
Solar projection and then to Prism's `/2` wire, before Pulsar samples the same
CEF `browser_source` as the paint and coverage atlas. The page uses the adopted
Solar `public/vision` bytes and the Vision contribution WebGL2 fixture; the
fixture's `color.webcam` capture is explicitly a synthetic contribution stand-in,
not physical-device evidence.

page.mjs loads one immutable LSML scene version through one presenter and one
CEF source. Its two mask-owning frames bind opacity to the same state path with
a 0.5 default; the update uses the presenter's existing type=patch / patchJson
path to set mask.opacity to 1.0. The accepted patch must report changed bound
nodes, and its following frame-submitted receipt must keep sceneId and
sceneVersion stable while advancing pixelRevision. The receipt is posted with
the exact original LSML document to the same Solar/Prism emitter path. This is a
Vision state-patch-to-composition proof, not an end-to-end Orion/LSDP proposal
transaction proof.

The emitter loads Solar's `projectNativeCompositionFrame` and
`withNativeCompositionSource`, then Prism's validation, projection, and hashing
functions from the supplied worktrees. It does not recreate either contract.
After both CEF/native pixel captures and reset verification, the active presenter
is terminated and the pinned historical presenter is run in isolation. That
negative receives the exact real `/2` receipt produced for the updated scene and
must reject it without submitting a frame.

For this fixture the LSML root has an opaque `#0c1119` background. The native
wire places its root `solarBand` before the masked owner group, so pixels outside
coverage reveal that real CEF atlas band. The probe samples band 0 from the same
CEF screenshot and verifies its paint order before comparing native overflow
pixels; the separate OBS backdrop source is not the expected pixel in these
opaque-root areas.

The negative mask wire uses the next candidate generation after the initial
commit. Native validation checks the generation fence before resolving mask
dimensions, so reusing an already committed generation would only prove stale
generation rejection. A rejected malformed candidate must leave that fence
unchanged, allowing the valid opacity update to reuse the candidate generation.
The negative asserts the full native `invalid-mask-rect` code and detail, then
compares decoded scene pixel buffers across the rejection; PNG encoding bytes
are not used as a proxy for pixel equality.

`probe.py` defaults to an offline pin/provenance preflight. `--execute` is an
explicit private runtime action and also requires `--gpu-slot-released`; do not
pass it until the parent has released the GPU slot. It reuses the owned-process
shutdown helper from `probe-group-composition.py`, process startup from
`probe-websocket.py`, the obs-websocket RPC helpers from `probe-source-kinds.py`,
the PNG decoder from `probe-browser-m3.py`,
and the CEF module overlay qualified by `probe-cef-shutdown.py`. Staging keeps
the proven native `obs-websocket.dll` and overlays only the CEF browser files;
the different CEF-report websocket hash is recorded, never copied over the
selected native DLL.

The runtime proof is bounded to a private headless scene and localhost page. It
activates that fixture with the ordinary `SetCurrentProgramScene` browser-source
lifecycle so CEF paints the page. It does not test production switching,
transitions, or switch duration, and does not use a physical camera, Live,
recording, Meet, or an installed runtime. It saves the actual CEF source screenshot, the native
composition before/after the mask update, the exact receipts/projections/wires,
and the graceful-shutdown outcome under
`evidence/local-20261017-cef-coverage-chain/eleven/`.

## Authored 1080p profile

`probe.py --profile 1920x1080@60` selects a separately authored scene profile;
the presenter loads a 1920×1080 LSML root and the native host is configured for
1920×1080 at 60 fps. It scales every fixture coordinate and extent by exactly
six before hashing/loading the LSML document. It does not render the 320×180
scene and resize its screenshot. The existing 320×180@30 profile stays the
default and the signed 320-pixel run remains unchanged.

The 1080p frame mask is derived from the same base geometry: the clipped owner
is at (48,96) with size 312×552; its two shapes are 216×552 and 264×552 at
(144,0); the unclipped owner frame is at (480,96), size 120×552, with a
384×552 child. The visible sample points are (96,240) for partial alpha,
(216,240) for the overlap, (372,240) for clipped overflow, (660,240) for the
unclipped child, and (744,240) outside the stencil frame. The same pixel
oracles remain in force: half opacity, full opacity after a state patch,
zero coverage outside either clip, identity-preserving patch, invalid-mask
rejection without advancing the generation fence, reset restoration, and the
exact-receipt old-presenter negative.

Vision's three equal 1920×1080 regions (two paint bands and one coverage band)
pack with one-pixel gutters into 1922×3246. The expected coverage rectangle is
scene [0,0,1920,1080] and atlas [1,2165,1920,1080]. The profile checks that
receipt and the CEF `GetSourceScreenshot` dimensions match. The OBS WebSocket
API documents a 4096-pixel per-axis screenshot request limit, so 3246 fits;
Vision's packer test accepts an 8192 max-dimension argument, but that unit test
does not prove the active GPU supports 8192. The real run must report WebGL's
`MAX_TEXTURE_SIZE` and the actual CEF screenshot. It uses a 36 MiB websocket
message cap for the largest atlas readback.

The planned decoded RGBA allocation is 24,955,248 bytes for one atlas plus
8,294,400 bytes for one native scene readback (33,249,648 bytes together).
That lower bound excludes PNG/base64 copies, the browser and GPU surfaces,
temporary decoder buffers, and allocator overhead; actual peak memory is not
measured by this probe. The expensive calls are Vision's full-size contribution
render, CEF `GetSourceScreenshot` atlas readbacks, and Pulsar native scene
readbacks. The Vision/Solar/Prism receipt and wire contain metadata rather than
the screenshot pixels, but their serial page requests still share the probe's
single execution path. A configured 60 fps and one successful capture do not
establish sustained 60 fps, frame pacing, scanout, or transition performance.

The profile can be inspected without starting a host or using the GPU:

```powershell
python -B scripts/cef-coverage-chain/preflight.py --profile 1920x1080@60
```

The eventual private runtime invocation remains separately gated by both
`--execute` and `--gpu-slot-released`; do not run it until the parent releases
the GPU slot.

The 2026-10-17 run `20261017T165509Z-cef-coverage-chain.json` passed at
1920×1080 with a 1922×3246 CEF atlas. Independent readback in
`20261017T165800Z-independent-1080-pixels.json` checks all 20 expected RGBA
samples and all 2,073,600 scene pixels across the rejected mask update. The
host exited with code 0 after draining CEF. These reports live in this unit's
canonical evidence directory. `20261017T170300Z-1080-runtime-provenance.json`
distinguishes the clean repository reference from the retained OBS binary's
actual CMake source checkout and records the redacted host log. The separate
native reset/deadlock correction is not part of this run; neither sustained
60 fps nor installed-runtime behavior is certified by it.

The subsequent `20261017T171046Z-cef-coverage-chain.json` run also passes with
the native lock correction (`5b28fe10`, plugin `1deaa45c`) and Prism admission
`8933556f`. Its four full atlas/native PNGs are byte-identical to the independently
verified 1080p baseline; all 2,073,600 pixels remain unchanged across malformed
mask rejection. `20261017T171300Z-combined-independent-validation.json` binds
those comparisons to the source/binary provenance and host 64136's clean CEF
shutdown. The same frame-pacing, transport and installed-runtime limits apply.
