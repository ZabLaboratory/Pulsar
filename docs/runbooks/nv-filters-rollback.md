# Runbook — putting `nv-filters` back in the strip list

Issue **#167**, Prism **ADR 023 Amendment 3**. Criterion 7 of #167: the way
back is written **before** it is needed, not after.

`nv-filters` was stripped from the Pulsar bundle under **NS1** because its
two loaders resolved an NVIDIA SDK DLL by bare name off a path read from the
inherited environment. Amendment 3 §A3.1 overrode that strip, on the
condition (§A3.4) that the invariant survive by other means. This runbook is
what to do when it does not — the module is implicated in an incident, the
hardening is found wanting, or the porteur simply withdraws the override.

## When to use it

Any of:

- a load path is found that reaches an NVIDIA DLL or a `.trtpkg` model
  without going through `plugins/pulsar-nv-secure-load/`;
- `tests/nv-probe` fails on `main` and the cause is not the test;
- Bastion withdraws the clearance, or a CVE lands on the Maxine SDK;
- the capability manifest reports `nv_filters.module_loaded: true` with both
  `usable` false — gate and loader have drifted, and the gate is the thing
  keeping the module inert.

**Stripping is a mitigation, not a fix.** It removes the module from the
*bundle*; it does not remove it from a Pulsar someone already installed.
Rolling back therefore means shipping a new bundle **and** getting the
embedder to stop pointing at the old one — the second half is the Prism
step below, and skipping it leaves the situation unchanged in production.

## Pulsar side

1. `scripts/package-win.ps1` — put `'nv-filters'` back in
   `$baseStrippedPlugins` (the array around line 106), and restore a strip
   rationale in the comment block above it. Do not leave the Amendment 3
   paragraph standing next to a strip: a rationale pointing at the opposite
   decision is what #167 called the worst of the two states.
2. `scripts/check-nv-filters-packaging.py` — this gate asserts the module is
   **not** stripped and will now fail. It has to be updated in the **same
   commit**, not disabled: invert `check_strip_lists()` (assert presence in
   the list, and that the comment carries a strip rationale) and keep
   `check_no_sdk_in_sources()` / `check_dist()` exactly as they are — the
   "no SDK payload" half holds either way. In `check_dist()`, the
   `nv-filters.dll` presence assertion becomes an absence assertion.
3. Keep `patches/0003-nv-filters-pin-sdk-loads-to-validated-directory.patch`
   and `tests/nv-probe/`. The module still **builds**; only the packaging
   changes. Dropping the hardening at the same time would leave a build in
   which the old bare-name loads are back, and any consumer building from
   source — or any existing install — gets them.
4. `docs/PROTOCOL.md` — `capabilities.nv_filters` keeps being published.
   It is a probe of the host machine, not of the bundle, and with the module
   stripped it is precisely what tells a consumer why the filters vanished.
   Do not remove it: removing a manifest entry is a structural change and
   would bump `version`.
5. Full sweep: `python scripts/check-nv-filters-packaging.py`, then a
   `-Full` build, then `ctest --test-dir build -C RelWithDebInfo`.

## Prism side (the half that actually stops it)

The embedder pins `NVAFX_SDK_DIR` / `NV_VIDEO_EFFECTS_PATH` for the
`pulsar.exe` it spawns (Amendment 3 §A3.4 layer ii, Prism's twin issue).

6. Un-pin them — spawn Pulsar with **neither variable set**, and make sure
   neither is inherited from Prism's own environment. Pulsar's probe then
   finds no designated directory, `pulsar_nv_module_should_load()` is false,
   and the module refuses to load **even on an installed bundle that still
   contains it**. This is the step that changes behaviour on machines
   already in the field, and it takes effect at the next Pulsar spawn.
7. Re-pin the bundle version: bump the `@clodocapeo/pulsar-bundle-full`
   dependency to the release built above, and rebuild the desktop artefact.
   Cf. [[rebuild-native-artefacts-when-touching-orion-solar-lumencast]] — the
   bundle is embedded, not resolved at runtime.
8. Remove `nvidia_audiofx_filter` / `nv_greenscreen_filter` /
   `nv_blur_filter` / `nv_background_blur_filter` from the filter whitelist
   (Prism ADR 023 §3.3) so the cockpit stops offering a filter that will
   never instantiate.

## Verifying the rollback

- `capabilities.nv_filters.module_loaded` is `false` on a freshly spawned
  Pulsar — **and** `afx.directory_designated` / `vfx.directory_designated`
  are both `false`, which is what proves step 6 took, rather than step 1.
- `capabilities.filters` no longer lists any `nv*` filter id.
- The packaged tree has no `nv-filters.dll`:
  `python scripts/check-nv-filters-packaging.py --dist dist/pulsar-windows-x64-full-v<VERSION>`.
- Pulsar's log carries `[NVIDIA filters]: not loaded -- no validated NVIDIA SDK`.

## What this does not undo

Nothing in this rollback un-hardens the loaders, and that is deliberate. If
the reason for rolling back is a flaw in the hardening itself, fix the
hardening in `plugins/pulsar-nv-secure-load/` — the strip is a blunt
instrument that protects the bundle Pulsar ships and nothing else.
