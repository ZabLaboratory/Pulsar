# Runbook — cut a Pulsar release and propagate it to consumers

**Owner**: Keeper. **First written**: 2026-07-26, during the v1.2.2 release
(RTMPS Twitch ingest, #113 / PR #114 — the fix that motivated the runbook).

## Why this runbook exists

Merging a fix into `main` does **not** put it in front of users. Pulsar's
value ships as a compiled Windows binary, and consumers (Prism) get it
through npm:

```
plugins/**.cpp  ──build──►  pulsar.exe + *.dll
                            │
                   package  ▼   pulsar-windows-x64-full-v<VERSION>.zip
                            │   attached to the GitHub Release v<VERSION>
                            ▼
   @clodocapeo/pulsar-bundle-full@<VERSION>  (postinstall downloads that zip)
                            ▼
   Prism package.json  "@clodocapeo/pulsar-bundle-full": "^<VERSION>"
```

Three things must all be true for a fix to be live: the npm version is
published, the GitHub Release carries the matching zip, and the consumer's
range resolves to that version. Miss any one and the operator keeps running
the old binary — silently, because
`packages/pulsar-bundle-full/scripts/postinstall.mjs` **soft-fails** a 404
(by design, so `npm install` completes) and only warns.

## Procedure

1. **Decide the semver bump** — `docs/DEVELOPMENT.md` § Versioning. A
   compiled-behaviour fix with no protocol change is a patch.
2. **CHANGELOG.md** — new `## [X.Y.Z] - YYYY-MM-DD` section above the
   previous one. For a security fix, say what was exposed and add an
   explicit *consumers must upgrade* note: the source merge alone is not
   the mitigation.
3. **Bump the version in all four places** (`VERSION` is the source of
   truth, C++ and npm both read from it):
   - `VERSION`
   - `packages/pulsar-client/package.json` → `version`
   - `packages/pulsar-bundle/package.json` → `version` **and**
     `dependencies["@clodocapeo/pulsar-client"]` (exact pin)
   - `packages/pulsar-bundle-full/package.json` → same two fields
4. **Refresh the lockfile without side effects**:
   `npm install --package-lock-only --ignore-scripts --no-audit --no-fund`.
   A plain `npm install` on Windows runs the postinstall, which `rm -rf`s
   `binaries/` **before** fetching — and the new release does not exist
   yet, so you would delete a working local binary for nothing.
5. **Commit** `chore(release): vX.Y.Z` on a `keeper/release-vX.Y.Z-<slug>`
   branch, then fast-forward `main`. Expected diff shape: CHANGELOG,
   VERSION, 3 × package.json, package-lock.json — nothing else.
6. **Push `main`, then push the tag** `vX.Y.Z` (annotated). The tag is what
   unlocks the release-grade stages; `pipeline.yml` gates them on
   `startsWith(github.ref, 'refs/tags/v')`.
7. **Watch `pipeline.yml` on the tag** — four tag-only outcomes matter:
   - `publish @clodocapeo/pulsar-* to npm` (needs only `lint`, so it lands
     in ~2 min) — requires `secrets.NPM_TOKEN`; the job re-checks that the
     tag matches `VERSION` and fails loudly otherwise.
   - `package light + full distros` → the two zips.
   - `live broadcast (Twitch)` → 600 s real broadcast on tag.
   - `GitHub Release attach` → `needs: [package, live-broadcast]`, creates
     the Release and uploads the zips + proof MP4 + `diagnostic.json`.
8. **Verify, do not assume**:
   ```sh
   npm view @clodocapeo/pulsar-bundle-full version          # == X.Y.Z
   gh release view vX.Y.Z --json assets -q '.assets[].name' # full zip present
   ```
9. **Bump every consumer** — Prism `package.json`
   (`@clodocapeo/pulsar-bundle-full`), branch `keeper/<slug>`, `npm install`,
   local gate (Prism CI is disabled: `lint && typecheck && build && test`).
10. **Prove it on the real artefact**, not on the lockfile: re-install /
    re-pack the desktop bundle and confirm the shipped `pulsar.exe` /
    `pulsar-multi-stream.dll` came from the new zip. For v1.2.2 the
    on-air assertion was `pulsar:GetDestinations` reporting an `rtmps://`
    URL for a `twitch` destination.

## Traps met in the field

- **`live broadcast (Twitch)` serialises across runs** — concurrency group
  `live-test-twitch`, `cancel-in-progress: false`. A push to `main` plus the
  tag plus any open PR each queue a broadcast, so `release-attach` can sit
  waiting well past the build. Not a failure; do not re-run.
- **A `push` to `main` cancels the in-flight `main` run** (concurrency
  `pipeline-${{ github.ref }}`, `cancel-in-progress: true`). Cutting a
  release right after a merge cancels that merge's run; the tag run covers
  the same ground plus the release stages.
- **`paths-ignore` covers `CHANGELOG.md` and `docs/**`** — a docs-only
  release commit would not trigger the pipeline at all. The version bump is
  what makes it fire.
- **Vendor shims that overwrite `pulsar.exe`** silently defeat the whole
  chain (historically `scripts/vendor-pulsar-virtualcam.mjs` in Prism, now
  removed). If a consumer postinstall touches the binary, the npm version
  proves nothing — check the file on disk.

## Rollback

The Release/npm publish is additive; nothing is mutated in place, so
rollback is a **forward** move, never an unpublish.

- **Bad binary, npm already published**: `npm dist-tag add
  @clodocapeo/pulsar-bundle-full@<previous> latest`, then cut `X.Y.Z+1`
  with the revert. Do **not** `npm unpublish` (breaks every lockfile
  pinning that version) and do **not** delete the Release (any consumer
  that already resolved `X.Y.Z` would start 404-ing its postinstall and
  soft-fail into a binary-less install).
- **Consumers**: revert the consumer bump commit (Prism `package.json` +
  lockfile) and `npm install`. That is enough — the postinstall re-fetches
  the previous zip and rewrites `binaries/`.
- **Tag pushed by mistake, nothing published yet**: delete the tag
  (`git push --delete origin vX.Y.Z`) before `npm-publish` finishes, then
  re-tag. After publication, the version number is burnt — go forward.
- **Pipeline red after the tag**: the tag is harmless on its own. Fix on a
  branch, merge, delete + re-push the tag (npm publish is idempotent-fail:
  a second publish of the same version errors, so bump the patch instead if
  npm already succeeded).
