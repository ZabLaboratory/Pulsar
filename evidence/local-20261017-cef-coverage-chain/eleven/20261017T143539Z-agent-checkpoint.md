# CEF coverage-chain preparation checkpoint

Timestamp: 2026-10-17T14:35:39Z

## Worktree and scope

- Repository: `D:/Documents/Zab/Pulsar`
- Worktree: `D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20261017-cef-coverage-chain`
- Branch: `eleven/local-20261017-cef-coverage-chain`
- HEAD: `2738e3646fe5be32f10cd7d25aa8cf4ff5a292ee`
- Requested base: same as HEAD.
- Product source changes: none. The only worktree change is this checkpoint and the earlier `20261017T142755Z-agent-start.md`.

## Preparation result

The full real CEF chain is not yet ready to run. No CEF process, GPU process, build, or test was launched in this work unit. The external GPU-profile slot remains a hard gate. The parent has now authorized a separate Vision presenter correction; that correction must be integrated and Solar's adopted browser assets refreshed before this Pulsar harness can prove the real Solar presenter route.

The blocking source fact is that Solar's adopted `public/vision/ui/mainpresenter.mjs` (same pinned byte as `dist/host/vision/ui/mainpresenter.mjs`) has a strict `/1` check in `validateNativeCompositionReceipt()`. It rejects `vision.native-composition-frame/2` before `frame-submitted` can carry an actual masked frame to Solar. The corresponding Vision source is `crates/vision-web/ui/mainpresenter.mjs`, with the same check. Vision Rust emits `/2` when masks are present (`crates/vision-web/src/lib.rs`, around the schema selection at line 3623). The adopted presenter also derives atlas presence from paint `surfaces` only, so a valid coverage-only atlas needs explicit handling. Bypassing the presenter with a direct WASM call would produce pixels but would not qualify the requested `mountVisionScene`/presenter path; that bypass is therefore excluded from success criteria.

## Qualified artifacts available for the later private run

Solar adoption commit `11323fb7949fac0b039964de9c25fa614e1f3e85` is present in `D:/Documents/Zab/Solar/.worktrees/eleven-local-20261015-project-integration`, clean at inspection. Its signed adoption report records these dist bytes (the corresponding `public/vision` files were adopted in the same commit):

| File under Solar `dist/host/vision` | Bytes | SHA-256 |
|---|---:|---|
| `ui/mainpresenter.mjs` | 32,634 | `607eee787387a887e22b24573e5a33d60a8cee1e6233feef5f1d21a32e188b3d` |
| `ui/fonts.mjs` | 1,977 | `d49ed45181a8aa714f675aaa24f5439e9547b274d6eba6cd2bb995c981765c5f` |
| `pkg/lumencast_vision_web.js` | 157,228 | `2661fc24146f703adcfa1b94769d9c789b707abf0027792b6f4287fb40bc04e6` |
| `pkg/lumencast_vision_web_bg.wasm` | 10,618,773 | `39c64876a92c0d8d5a104fa8366dc437520140be92e8376db59ed127420ab4a9` |

The adoption report says three actual WASM frames were admitted directly by Solar's projection code (nominal `/1`, masked owner/capture `/2`, frame mask/clip/opacity `/2`) and five independent canvas pixels passed. It explicitly does not qualify the Solar presenter-to-Prism-to-CEF/native chain.

Prism's current `project-integration` checkout was `6d767c04f5d5ff7413e251b9ab21531094e673ad`. Its `native-composition-consumer.ts` exports the real `validateSolarNativeCompositionPlan`, `buildNativeCompositionProjection`, and `nativeCompositionWireHash`; the existing `20261017T140000Z-prism-wire-emitter.mjs` uses these APIs but loads the stale synthetic fixture from another worktree. That fixture is not valid evidence for this task. The Prism worktree also had two unrelated modified files and two untracked integration reports; they were left untouched.

The CEF runtime checkpoint `20261017T121331Z-agent-checkpoint.json` records source pin `25ed780b7f392a1690dfca929cd57679959cf3ad`, with `pulsar.exe` SHA-256 `69ab22a5829bee254d53fa9edc463cfbdb33886a23781ae67fdc40fb1b36e973`, `obs.dll` `891f6c58b1a9e865836fb94f03f8d90b238198b55c70d2656214cde4790478b1`, `pulsar-browser.dll` `0602fdc46559109ad24a57fc769580480fe8b2794930cc591bf5a450129df8e6`, page helper `9148fb2f5e1cc8867b19ab1cfd033eaea4e647e05ff0ac313f883cf281ab65e6`, and `libcef.dll` `551bea07c46c9a330eff7861105321f3c7d47e3c0412ced4b0ff35aa130f2536`. That run produced genuine CEF/libobs pixels and exited gracefully, but contained no native composition or Solar atlas.

The current private native coverage DLL is `D:/Documents/Zab/Pulsar/.worktrees/eleven-local-20261017-native-group-coverage/.cache/native-group-coverage/plugin-build/RelWithDebInfo/pulsar-capture-effects.dll`, SHA-256 `9b17835e8db5ed452fd59e0fbe31d07f12e022df888540db6986e3a73918d093` (provided and qualified by the parent). The earlier native-group probe report used a different capture-DLL byte and is not treated as the current qualification record. Its runtime `obs-websocket.dll` hash is `d3ecaf55d8adc4a9e593589d0ec232ac4974db00980a41361a3fb8df989ef26b`, while the CEF checkpoint records `3130c2cf8485bfbf80b67b4ee09126f21a38a90229457aabdb03126526374f3a`. These differing WebSocket bytes are not presumed ABI-compatible and must not be mixed without source/API provenance and runtime checks. The two completed source qualifications point to the same upstream revision and the CEF `obs.dll` hash matches native group host provenance; this still does not establish a combined runtime.

## Reuse and remaining proof

- Reuse `scripts/probe-group-composition.py`'s private process owner, OBS protocol request/readback helpers, exact plan hash/wire handling, and cleanup strategy; its generated PIL atlas is synthetic and cannot be used as the coverage source.
- Reuse `scripts/probe-cef-shutdown.py`'s actual `browser_source` setup and decoded `GetSourceScreenshot` path; its independent page pixels do not qualify composition.
- The actual future source must be the CEF browser source rendering the Vision WASM atlas from the current Solar `public/vision` bytes. The same source item must be referenced by the current Solar projection emitted through the real Prism consumer. The proof must update coverage in the same scene/source identity and check a discriminating negative, coverage pixels, no duplicate paint/opacity/blend, and a graceful owned-process shutdown.
- Parent reported the GPU profiler still owns the launch slot and has a separate native screenshot timeout under diagnosis. No CEF/GPU run may start until the parent explicitly releases the slot.

## Validation and handoff

- Git preflight verified the exact worktree, `.git` common directory, branch, HEAD, status, and all registered Pulsar worktrees before preparation.
- Solar adoption and Prism source were inspected read-only. No external repository or worktree was written.
- No test/build was started; therefore there is no CEF-chain pass claim.
- Next dependency: finish the authorized Vision presenter `/2` validator correction and adoption, then resume this Pulsar WT to complete the private harness and wait for explicit GPU-slot release before any CEF/GPU launch.
