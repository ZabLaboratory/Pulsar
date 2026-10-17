# Vision → Solar → Prism → CEF atlas chain probe

This private probe binds one real Vision `/2` `frame-submitted` receipt to a
Solar projection and then to Prism's `/2` wire, before Pulsar samples the same
CEF `browser_source` as the paint and coverage atlas. The page uses the adopted
Solar `public/vision` bytes and the Vision contribution WebGL2 fixture; the
fixture's `color.webcam` capture is explicitly a synthetic contribution stand-in,
not physical-device evidence.

`page.mjs` serves two versions of one `scene_id` through one presenter and one
CEF source. Its mask opacity changes from 0.5 to 1, and each accepted WASM
`frame-submitted` receipt is posted with the exact LSML document. The emitter
loads Solar's `projectNativeCompositionFrame` and
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
