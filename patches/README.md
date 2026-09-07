# Pulsar OBS patch stack

Pulsar 3.0.0 carries **52 patches** above the pinned
`ZabLaboratory/obs-studio` revision
`bd73b922891e56839b0bc86bdc519802802f9d68`.
Five foundational changes are already in that fork revision.

The [complete libobs/OBS reference](../docs/LIBOBS-CHANGES.md) documents every
patch, affected file, purpose, default/experimental status and the integrated
baseline. Start there when reviewing what Pulsar changes in OBS.

## Targets and order

[build-win.ps1](../scripts/build-win.ps1) and the CI apply gate split patches
by the **complete filename**:

- filenames containing `obs-browser` target the pinned nested
  `upstream/plugins/obs-browser/` repository;
- all other patches target `upstream/`;
- each set applies in lexical filename order, root first, nested browser next.

There are intentionally two `0022-` files. Do not select patches by numeric
prefix alone. The historical “apply every patch to upstream in a shell loop”
instruction is wrong for the nested browser patch.

## Format and authoring

Use a descriptive four-digit-prefixed `NNNN-name.patch` filename and a
reviewable `git format-patch` artifact. Preserve existing names/order.
The header should explain the behavioral change, why a plugin cannot express
it, upstream eligibility and the issue/work-unit provenance.

Work in a dedicated checkout. Export source changes before running a build
that may reconstruct/reset generated upstream state. Commit/tag operations
follow the workspace's signature and provenance requirements.

A source rebase is not complete because `git am` succeeds: inspect semantic
changes, ABI, lifecycle and regression evidence. Update the complete change
reference in the same PR.

## Build-cache behavior

The build records the upstream pin, patch-content fingerprint and applied
HEAD. It reuses only an exact clean match; `-RefreshPatches` forces replay.
This preserves incremental object caches without accepting a different
patched source tree as the same candidate.

Use the normal full build for qualification. `-Fast` is only a local target
loop and does not validate every plugin or the complete release package.

## Removing or replacing a patch

Only remove a patch after proving that its behavior is supplied by the new
pin or an explicitly approved replacement. Replay and validate the **whole
dependent stack**. Never delete an old patch from the middle as an operational
rollback: subsequent patches may depend on its source, symbols or ABI.

For current CPU readback, `PULSAR_RAW_CURRENT_READBACK=0` is a bounded
diagnostic rollback. A product rollback uses a complete previously validated
release with matching binaries and packages.
