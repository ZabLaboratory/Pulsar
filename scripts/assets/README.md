# M10 stinger demo asset

`stinger-demo.webm` is the demo stinger transition media for the M10
program-scene transition (ADR 003 Amendment 1 §A1.1). It is referenced by the
fork's stinger transition source (#57) and the Prism consumer (#63) by a
**pinned local path / `asset_id`** — never a path taken from a leaf value
(ADR 003 Amendment 2 §A2.1, R7 / C-PATH).

| Field | Value |
|---|---|
| `asset_id` | `stinger-demo` (the allowlist key the contract #59 references) |
| File | `stinger-demo.webm` |
| Codec | VP9 (`libvpx-vp9`), `yuva420p` (alpha plane) |
| Size | 1280×720, 30 fps, 0.6 s, ~4.6 KB |
| `transition_point_ms` | 300 (full opaque cover at mid-point) |
| sha256 | pinned in `stinger-demo.manifest.json` |

## License

**Self-made, royalty-free / public-domain-equivalent.** The asset is generated
100 % from an ffmpeg synthetic filtergraph (`color` + `geq`) — no third-party
footage, no stock asset, no external download. It is therefore trivially
license-clean (C-SCANS).

## Provenance & verification

`scripts/assets/generate-stinger-demo.ps1`:

- **default mode** (`pwsh generate-stinger-demo.ps1`) — hashes the committed
  `stinger-demo.webm` and asserts it against the pinned sha256 in
  `stinger-demo.manifest.json`. No ffmpeg needed; this is the check the build /
  probe run before the media is decoded (R7 hash-pin).
- **`-Repin`** (`pwsh generate-stinger-demo.ps1 -Repin`) — regenerates the
  `.webm` from ffmpeg and re-writes the manifest hash. VP9/libvpx is **not**
  byte-reproducible (internal rate-control timing), so each regeneration yields
  a new hash; the new `.webm` + manifest are committed together and the diff
  reviews the re-pin. The sha256 pins **the committed file**, which is what the
  decoder consumes.

## How the fork resolves the path (#57)

The fork's stinger transition source registers this asset by a **local** path,
resolved at boot in `pulsar-frontend-stub.cpp::resolve_stinger_asset_path()`:

1. `PULSAR_STINGER_ASSET` env var (absolute path) — the canonical mechanism;
   the M10 probe (#61) and Prism consumer (#63) set it to the absolute path of
   this committed asset. **This is a local, operator-pinned value — never a
   path from a leaf/network value (R7 / C-PATH).**
2. Legacy fallback: `<cwd>/../../data/pulsar/stinger-demo.webm`.
   The source still uses this cwd-relative expression, but 3.0.0 runs with a
   private runtime cwd. It is **not reliably the bundle data root**. Hosts
   using a native stinger must provide the explicit absolute environment path.

The current optional dual-lane Stinger path validates a readable local asset
with a recognized container header before use. Missing/invalid input records
explicit refusal/fallback to Cut; do not treat the legacy empty-decoder
behavior as a successful current transition.

The geometry: an opaque sweep bar enters left→right covering the whole frame at
`t = 0.3 s` (the `transition_point`), then exits to reveal the destination
scene. Outside the bar the frame is fully transparent so both scenes composite
underneath — the standard stinger compositing the M10 transition relies on.


## Current scope

This README preserves the M10 demo asset's provenance. The older
`PULSAR_NATIVE_STINGER` path and the 3.0.0
`PULSAR_DUAL_LANE_TRANSITIONS` capability are distinct; both are off by default.
The default production switch is atomic Cut. See
[architecture](../../docs/ARCHITECTURE.md) and
[protocol](../../docs/PROTOCOL.md) for current behavior.
