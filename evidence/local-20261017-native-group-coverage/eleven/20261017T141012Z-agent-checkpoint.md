# Agent checkpoint: native group coverage and Prism wire

- Worktree: `D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20261017-native-group-coverage`
- Branch: `eleven/local-20261017-native-group-coverage`
- Base/HEAD before checkpoint: `371eb9fed22b75d7ab635c236f8132e65498edec`
- Scope: Pulsar native group composition `/2`, real Prism wire interoperability, private GPU proof and directly owned feature documentation.
- Excluded: transitions, readiness, switch duration, shared-runtime installation, deployment, push and merge.

## Result

The private GPU host accepted the exact `/2` wire object emitted by Prism's
signed projection and hashing implementation, with the real Solar fixture and
private OBS scene item IDs. The probe did not reserialize, rehash, or remap the
wire after Prism emitted it. The plan hash was
`sha256:f5afdb7d87ec52928d7e9a65a9a0f0059334dbd1c1cbb155cd0e83e41abd8b26`.
It used `pulsar.group-composition/2`, `global-scene-texels`, root mask and
capture mask references to atlas item `9`, Solar band item `9`, capture
contribution item `6`, and replaced items `7` and `9`. The capture binding
scene item and capture contribution scene item were distinct.

The Prism source was `fad4ed65bdcf1e0e3360b7dc29bf21f5578b3600`. Its Solar
fixture source was `ebe3e6bb9b049667932a6b5a6e094107718debfa`, fixture SHA-256
`79ced30ef6c235773c3784aa1ffca8ad71f32671ff01fc4730d20f8703dff276`; it is
derived from a synthetic Vision receipt. The GPU scene used a synthetic
`1922x3246` image source and a solid `color_source_v3` capture contribution.
The probe forwarded the output through Pulsar's private WebSocket request; this
does not prove Prism's running transport, live CEF pixels, LSDP behavior, a
physical camera, or other capture effects.

The positive run checked transparent and half-coverage root mask samples,
transparent and half-coverage capture mask samples under the emitted 2x affine,
capture `screen` blend at local opacity `0.75`, root `multiply` blend at
opacity `0.5`, a live atlas replacement while the composition remained active,
and reset to the original pixels, item IDs and hidden source visibility. The
root transparent sample was exact. Other checked Prism-wire samples differed
by at most 1 channel value, under the probe's explicit tolerance of 6. The same
run also passed all 16 admitted group CSS blend modes (tolerance 5; maximum
observed delta 1), dynamic alpha coverage, atomic refusal and dependency-proxy
lifecycle checks.

The old `/1` plugin (SHA-256
`934412dc3629c207bc3fd4d83c6e3b47e3c3b73f43696e5ee1ac5f2c75d97d61`) rejected
the Prism-emitted `/2` request with `unsupported-protocol` before mutation.
Its independently generated wire carried the same Prism plan hash; scene,
runtime and generation remain private run values. Readback pixels and source
visibility stayed unchanged. The positive `/2` plugin SHA-256 was
`9b17835e8db5ed452fd59e0fbe31d07f12e022df888540db6986e3a73918d093`.

## Validation and evidence

- `git diff --check` — passed.
- Python AST parse for `scripts/probe-group-composition.py` — passed; both GPU runs also imported and executed the probe.
- Positive private GPU run — passed: `20261017T140734Z-group-composition-probe.json` and `20261017T140734Z-prism-wire-gpu-probe-raw.txt`. Exact emitter output: `20261017T140739Z-5a958fa18c-prism-wire.json`.
- Old `/1` private GPU negative control — passed: `20261017T140818Z-group-composition-v2-negative-control.json` and `20261017T140818Z-prism-wire-gpu-negative-control-raw.txt`. Exact emitter output: `20261017T140822Z-f6dad25745-prism-wire.json`.
- A prior probe attempt is retained: report `20261017T140658Z-group-composition-probe.json`, raw output `20261017T140657Z-prism-wire-gpu-probe-raw.txt`, and its emitted wire artifact. That attempt failed in the probe harness because it compared the scene to its precommit baseline after the composition had already been committed. The baseline readback was moved before the commit; the subsequent positive and negative runs passed. This was not a native parser or GPU interoperability failure.
- The C++ plugin was already built from the current native sources in this worktree before the final probe-only changes. No C++ source changed after that build, so no rebuild was needed for the Prism-wire probe addition.

The emitter's Vite cache is private to this worktree at
`.cache/native-group-coverage/prism-wire-emitter-vite-cache`. Earlier private
ABI and upstream runtime caches in
`D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20261015-native-group-composition/.cache/native-group-composition/`
were read as inputs and preserved.

## Changed files

- `plugins/pulsar-capture-effects/src/capture-effects.cpp`
- `plugins/pulsar-capture-effects/src/capture-effects.effect`
- `plugins/pulsar-capture-effects/src/group-composition-plan.cpp`
- `plugins/pulsar-capture-effects/src/group-composition-plan.hpp`
- `plugins/pulsar-capture-effects/src/group-composition-world-source.cpp`
- `plugins/pulsar-capture-effects/src/group-composition-world-source.hpp`
- `plugins/pulsar-capture-effects/src/group-composition.cpp`
- `plugins/pulsar-capture-effects/README.md`
- `scripts/probe-group-composition.py`
- This checkpoint and canonical private-run evidence under this directory.

No transition, readiness or duration-switch files were changed. No shared
runtime was installed or replaced. This checkpoint is included in the signed local commit; its SHA will be reported in the handoff.
