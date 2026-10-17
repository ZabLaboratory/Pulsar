# CEF coverage chain diagnostic checkpoint

Run evidence 20261017T161417Z is persisted from the private harness. This checkpoint records an incomplete run; it does not qualify the post-mutation native pixels or overall chain.

## Pin and scope

- Worktree: D:\Documents\Zab\Pulsar\.worktrees\eleven-local-20261017-cef-coverage-chain
- Branch / HEAD at run: eleven/local-20261017-cef-coverage-chain / 549b4d954eb4f90a38156c6b87c384730f214341
- Run pin: Solar e8ca9a3a6944fac68430fa9ddb79b93366a2f99a; Prism 321db8b2dbe6b5a429fc372306ac363b48e8900d; Vision presenter assets as recorded in the run JSON.
- Runtime started once in a private fixture at 320x180/30. No transition or switch-duration measurement was made.
- The host and HTTP server stopped; process exit was 0, graceful shutdown markers were ordered, and forced termination was not used. No CEF/GPU host remains running.

## Exact mutation observation

The failing assertion was: mask update did not revise the same scene through the same presenter. The two actual frame-submitted observations are:

| Field | Initial | Updated |
|---|---|---|
| sceneId | vision-frame-mask-browser-proof | vision-frame-mask-browser-proof |
| sceneVersion | sha256:9c8038d08bebe7bb3e847e90b40d1c93dbb78cbc7831f83a176a8e26488edfeb | sha256:ecb1f54aa5acf50d83a3fd92227e3cee4240a33eeacaf751c9544f68ae49041f |
| schema | vision.native-composition-frame/2 | vision.native-composition-frame/2 |
| pixelRevision | 0 | 0 |
| canvasSize | [322, 546] | [322, 546] |
| coverageSurfaceCount | 1 | 1 |
| masked owner | maskPresent=true, maskSurfaceIndex=0, targetNodeId=null | same |

The CEF atlas captured after the mutation is present in this run. At the three in-mask sample points (partial, overlap, unclippedChild), coverage RGBA changed from [127,127,127,127] to [255,255,255,255]; both clipped-overflow points stayed [0,0,0,0]. This proves changed mask pixels reached the actual CEF atlas and that the clipping samples remained stable. It does not prove the updated native composition pixels, because the assertion failed before the post-update native readback. The CEF atlas and native readback remain distinct evidence.

At this checkpoint the observation is intentionally classified as unresolved: the content hash changed, the mask atlas pixels changed, but pixelRevision remained "0". This report does not decide whether Vision defines pixelRevision as scene-local/resettable metadata or requires it to advance for this mask mutation; that must be checked against the Vision presenter/renderer contract before any oracle or product change.

## Other discriminating evidence

- Initial native CommitGroupComposition succeeded at generation 1.
- Initial native pixels matched the actual opaque LSML root band (#0c1119, [12,17,25,255]) outside the mask and [133,136,140,255] at partial, overlap, and unclipped-child points.
- The malformed-mask negative used candidate generation 2 and was rejected with invalid-mask-rect: mask crop and global rect must have exact one-to-one dimensions; decoded screenshot channel count and every pixel were unchanged.
- The current run terminated before claiming updated native pixels, reset behavior, or legacy-presenter negative success. Preserve the failed run as-is.
- The run used Prism pin 321db8b2... Root later reported Prism 09b893dee789519bf63a0726427bd663c1e03104; no re-run is included here and the runtime consumer was reported unchanged. Re-pin and offline preflight are required before any later run.

## Persisted artifact manifest

| File | Bytes | SHA-256 |
|---|---:|---|
| 20261017T161417Z-cef-coverage-chain.json | 44480 | ae31cd2ebe871e68c756abb1747b5041fd4c553e280fd8fcdb177017840cd5e6 |
| 20261017T161417Z-f9c62547-backdrop-source.png | 697 | 14f66420e3fa9965c27f7890a5ed155a711836af83ed10d4a508e982fc9e43f5 |
| 20261017T161417Z-f9c62547-cef-atlas-initial.png | 1748 | 798ab5b72be0fcc49885b5be1d301538c6ccdf234a51b3acfb08611b8db0e80c |
| 20261017T161417Z-f9c62547-cef-atlas-updated.png | 1747 | 279143956b520eddc22b234bfa68a15d6c0bacab2d48902ee9ad499483b48ca2 |
| 20261017T161417Z-f9c62547-initial-chain.json | 30878 | d056d65a12125112537aa61e44723329346164c9fbdff882fdf3f005e9a0a43c |
| 20261017T161417Z-f9c62547-scene-after-negative.png | 735 | 8951b4883a4b8694baf40e73d92e14cf3904a0154bb4ef980513ac5dfc8c2ef9 |
| 20261017T161417Z-f9c62547-scene-mask-050.png | 735 | 8951b4883a4b8694baf40e73d92e14cf3904a0154bb4ef980513ac5dfc8c2ef9 |
| 20261017T161417Z-f9c62547-host-redacted.log | 32998 | 02e8f44abe2a7ebad707073acff5b835cdf370d393e96e6c5d929649fa675edd |

The raw host log remains local and uncommitted because it contains credential-shaped values (password and API-key-like strings). The committed host log is the UTF-8 redacted copy; the report/captures and harness JSON are unchanged. Redactions applied: password fields = 1; key-like strings = 2.