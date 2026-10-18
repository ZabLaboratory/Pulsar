# CEF coverage-chain checkpoint

Status: harness corrected and diagnostic projection saved; waiting for the exclusive GPU slot. The end-to-end native chain is not qualified.

## Workspace and pins

- Worktree: `D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20261017-cef-coverage-chain`
- Branch and baseline: `eleven/local-20261017-cef-coverage-chain`, `392f9d94c7e24fadc5dae9dbfb08e43e4a5406d1`
- Latest offline preflight: `20261017T155034Z-chain-preflight.json`, passed without starting a runtime or using the GPU.
- Pins in that preflight: Solar `e8ca9a3a6944fac68430fa9ddb79b93366a2f99a`, Prism `8eb3f0d32b5f658e5f6cc6159c867d153eae40ae`, Prism product baseline `49080662c422eded328bcf0e7fdf8dd16febb0d2`, Vision `044ddb62df050eee9869150a69a1b88fd26f2962`, native Pulsar source `6328ff8b3c29f10f29b7e1fad75f170efc41e198`, and CEF source `47cb08c56e010c5da45923a5eee147f294216756`.
- Prism's actual composition consumer blob at `8eb3f0d` is `016a579264550a4377d932ed447259da55e69ea0`, identical to `49080662`. The merge from `55aa9d71` added rail test, README, and evidence files; no product module changed. Two unrelated dirty Prism files remain untouched: `resources/blue/runtime/manifest.json` and the action-compiler snapshot.

## Runtime attempt and retained pixels

`20261017T154424Z-cef-coverage-chain.json` is intentionally `passed: false`. The private headless host reached CEF, created and activated the localhost browser source, loaded the pinned Vision JS/WASM/fonts, and submitted the real masked Vision `/2` frame. Before the emitter ran, the real `GetSourceScreenshot` atlas was captured at 322×546 RGBA and passed all five alpha/clip checks:

- partial mask: alpha 127 (128 expected),
- overlapping children: alpha 127 (128 expected),
- unclipped child inside its stencil frame: alpha 127 (128 expected),
- clipped child overflow: alpha 0,
- stencil-frame overflow: alpha 0.

The atlas is `20261017T154426Z-6776dea6-cef-atlas-initial.png`, SHA256 `798ab5b72be0fcc49885b5be1d301538c6ccdf234a51b3acfb08611b8db0e80c`. The matching backdrop screenshot and host log are retained next to it. Host PID 6476 exited with code 0, no forced termination; CEF shutdown markers were ordered and the follow-up process inventory found no Pulsar/CEF child alive.

The first integrated attempt stopped after atlas capture and before Solar projection because the harness had an undefined output-property shorthand at `emit-wire.mjs:212` (`captureContributionSceneItemId`). That run also logged a Vite HMR WebSocket port collision while opening two SSR servers. It did not reach Prism wire, native commit, native pixels, the legacy presenter check, or reset. This is recorded as a harness failure, not a product failure.

## Harness correction and offline diagnostic

- `emit-wire.mjs` now maps `captureContributionSceneItemId` from its correctly named local variable and disables Vite's WebSocket server for both middleware-only SSR instances. An offline replay after these changes produced no port warning.
- The old-presenter negative now follows Vision's already-qualified isolated lifecycle: finish both real frames on one current presenter, terminate and flush it, then load the pinned legacy presenter alone with explicit sequences 0/1/2 and inject the exact `/2` receipt. The runner triggers this only after initial and updated CEF/native captures and reset verification, so a failing negative cannot erase earlier pixel evidence. This path is not yet runtime-tested.
- `preflight.py` pins the actual Prism integration HEAD `8eb3f0d`; the intervening test-only merge was checked for unchanged consumer bytes. The run report from the earlier attempt truthfully retains the then-current Prism `55aa9d71` provenance.
- `20261017T154947Z-cef-receipt-projection-diagnostic.json` records the offline replay of the exact frame input captured by the real CEF page, run while Prism was at `55aa9d71`. It passed Vision `/2` → Solar plan `/2` → Prism wire `/2`, with atlas 322×546, the indexed coverage for `vision.owner.1`, two groups, and a mask-crop negative derived from the projected wire. The subsequent `8eb3f0d` merge changed tests, README, and evidence only; the consumer blob remains identical to the `49080662` product baseline, as the later preflight verifies. This diagnostic is explicitly non-qualifying for native acceptance or pixels.

The original emitter input and both replays remain in the ignored private scratch directory `.cache/cef-coverage-chain/run-6776dea671df437da93c8a828b19340f/`. Preserve those files and the CEF/runtime artifacts while the GPU slot is held by the native-reset unit.

## Validation and remaining work

Passed without a Pulsar host or GPU:

- `node --check` for `page.mjs` and `emit-wire.mjs`,
- Python AST parse for `http-page.py`, `probe.py`, and `preflight.py`,
- offline provenance preflight at Prism HEAD `8eb3f0d`,
- offline emitter replay on the actual CEF-posted frame input.

No native `CommitGroupComposition`, native scene screenshot, mask-update 0.5→1.0, strict legacy-presenter negative, reset/readback, or 1080p60 proof has been completed. The 320×180/30 attempt is a fixture and pixel-contract diagnostic only; it is not general output-parity evidence. Do not launch another CEF/native host until the native-reset owner explicitly releases the GPU slot. Then run the bounded 320×180 probe; if its base contract passes, run the requested 1080p60 proof. Preserve failures and qualify only the exact stages that actually pass; no switch-duration test is in scope.
