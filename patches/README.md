# patches/

Pulsar's patches against the vendored obs-studio in `../upstream/`.

## Naming

`NNNN-short-name.patch` — four-digit zero-padded sequence + dash +
descriptive slug. Example: `0001-headless-mode.patch`.

The sequence determines apply order. Reserve gaps (skip 0010 if you
expect a follow-up) so subsequent inserts do not require renumbering.

## Format

Each patch is a `git format-patch` output. Include the standard header:

```
From: Pulsar maintainer <maintainer@zablab.tld>
Subject: [PATCH NNNN/####] Short imperative summary

Multi-line rationale: what this changes, why it cannot live as a
plugin, and whether it is a candidate for upstream submission.
```

If a patch is **upstream-eligible**, mark it in the rationale and
mirror the corresponding obs-studio PR number once submitted. The goal
is to keep `patches/` shrinking over time as upstream absorbs the
useful changes.

## Apply

The build pipeline (Phase 1) applies patches in lexical order before
configuring CMake on `../upstream/`. Manual application during
development:

```
cd ../upstream
for p in ../patches/*.patch; do git am "$p"; done
```

## Drop a patch

If a patch becomes obsolete (upstream merged it, or the feature moved
to a Pulsar plugin), delete the file and renumber subsequent patches
only if the gap is awkward. Sequence gaps are fine.

Patches whose filename contains `obs-browser` target the nested `upstream/plugins/obs-browser` submodule. The build script and CI apply those patches inside that pinned submodule after applying the root OBS patches; they must not be pushed to the upstream OBS repository.
