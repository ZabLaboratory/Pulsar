# Runbook — cut a Pulsar release and propagate it to consumers

**Owner**: Keeper. **First written**: 2026-07-26, during the v1.2.2 release
(RTMPS Twitch ingest, #113 / PR #114 — the fix that motivated the runbook).
**Last exercised**: 2026-07-28, v1.5.0 (`youtube` destination kind #162 +
graphics adapters / output scales manifest block #163, PR #164) — release cut
squash-merged by Keeper without review under the hotfix exception
(`docs/rules/git.md`, Gate point 2): urgent (the merged chain is dormant on the
artefact side, and Prism #473 plus the B8/B1 issues are blocked on it),
infra-pure (VERSION / CHANGELOG / 3 × package.json / lockfile, zero code),
documented here. Consumer side: Prism PR #476.
Previously: 2026-07-28, v1.4.0 (nv-filters strip / NS1 + NVENC preset,
PR #155; consumer Prism PR #471) — same exception, urgent because a
DLL-planting surface in the process holding the stream key stayed open on
every operator until an artefact shipped.
Previously: 2026-07-28, v1.3.0 (ADR 027 capability manifest, PR #149; consumer
Prism PR #469) — the run that produced the first four field traps below.

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
   Then assert the *content*, not just the presence — for v1.2.2, grepping
   the shipped `obs-plugins/64bit/pulsar-multi-stream.dll` out of
   `pulsar-windows-x64-v1.2.2.zip` for URL literals returned exactly one
   match, `rtmps://ingest.global-contribute.live-video.net/app/`, and no
   cleartext `rtmp://`. Two minutes of work, and it is the only step that
   actually proves the compiled fix shipped.
9. **Bump every consumer** — Prism `package.json`
   (`@clodocapeo/pulsar-bundle-full`), branch `keeper/<slug>`, `npm install`,
   local gate (Prism CI is disabled: `lint && typecheck && build && test`).
10. **Prove it on the real artefact**, not on the lockfile: re-install /
    re-pack the desktop bundle and confirm the shipped `pulsar.exe` /
    `pulsar-multi-stream.dll` came from the new zip. For v1.2.2 the
    on-air assertion was `pulsar:GetDestinations` reporting an `rtmps://`
    URL for a `twitch` destination.

## Traps met in the field

- **`## [Unreleased]` is not a reliable inventory of what you are about to ship**
  (v1.5.0). #162 merged to `main` with no CHANGELOG entry at all, so cutting on
  the section as written would have published a first-class `youtube`
  destination kind silently. Diff the commits, not the document:
  `git log --oneline v<previous>..main` and reconcile every PR against the
  `Unreleased` body **before** renaming the header. Writing the missing entry is
  part of the release commit, not a follow-up.
- **Pre-empt the broadcast concurrency squeeze instead of recovering from it**
  (v1.5.0). The known trap below is that a *pending* `live broadcast (Twitch)`
  is evicted when a newer run queues into `live-test-twitch`. Cutting a release
  produces three runs within seconds — the PR run, the `main` run, the tag run.
  Cancel the first two as soon as the tag run appears (`gh run cancel <id>`):
  the PR branch is already merged and the runbook's own note says the tag run
  covers the same ground plus the release stages. At v1.5.0 that left the tag
  run alone in the group and `release-attach` landed on the first attempt, with
  no rerun.

- **`live broadcast (Twitch)` serialises across runs** — concurrency group
  `live-test-twitch`, `cancel-in-progress: false`. A push to `main` plus the
  tag plus any open PR each queue a broadcast, so `release-attach` can sit
  waiting well past the build. Not a failure; do not re-run.
- **A *pending* broadcast gets cancelled when a newer run queues into the
  same group** — GitHub keeps at most one pending entry per concurrency
  group, and `cancel-in-progress: false` does not protect it. This bit the
  v1.2.2 release: a PR run queued behind the tag run, the tag run's
  `live broadcast (Twitch)` flipped to `cancelled`, and `release-attach`
  (`needs: [package, live-broadcast]`) was **skipped** — npm had 1.2.2 but
  no Release, so every consumer postinstall would have 404'd and soft-failed
  into a binary-less install. The whole run reads `cancelled`, not `failure`,
  which is easy to skim past. Recovery: `gh run rerun <tag-run-id> --failed`
  (the `pulsar-rundir` artefact is still there, 1-day retention), which
  re-queues the broadcast as the newest entry. Always end a release by
  checking the Release assets, not the npm version.
- **A `push` to `main` cancels the in-flight `main` run** (concurrency
  `pipeline-${{ github.ref }}`, `cancel-in-progress: true`). Cutting a
  release right after a merge cancels that merge's run; the tag run covers
  the same ground plus the release stages.
- **`paths-ignore` covers `CHANGELOG.md` and `docs/**`** — a docs-only
  release commit would not trigger the pipeline at all. The version bump is
  what makes it fire.
- **`gh pr merge --merge` and `--rebase` both fail on this repo** — only
  squash is allowed (`GraphQL: Merge commits are not allowed on this
  repository`). Cost at v1.3.0: two failed attempts, and each one leaves the
  local checkout switched back to a `main` that does not yet carry the bump, so
  `cat VERSION` reads the OLD number and looks like the merge silently did
  nothing. Use `gh pr merge <n> --squash --admin` and re-check `VERSION` after
  the `git pull`.
- **A stale local checkout hides the very code you are releasing.** At v1.3.0
  the working copy was 5 commits behind `origin/main`: `wire.ts` showed no
  `version` / `capabilities` / `regimes` and the release looked pointless.
  `git fetch` first, and read `git show origin/main:<path>` — never the local
  file — before deciding a bump is empty.
- **The consumer's fixture is part of the release, not a follow-up.** Prism
  captures the manifest from the bundled binary
  (`npm run manifest:capture`, ADR 027 RC 9) and an inclusion guard compares its
  registry against it. A release that changes what `GetCapabilities` answers
  makes `npm run manifest:check` go red on the consumer *by design* — and at
  v1.3.0 it also fired `EXPECTED_UNDECLARED`, the guard's dead-man switch, which
  is the intended signal that dormant checks just switched on. Budget the
  re-capture and the guard update in the consumer bump; a red `manifest:check`
  there is the chain working, not a regression.
- **A packaging-time strip is only real in the zip — assert it there** (v1.4.0,
  NS1). `scripts/package-win.ps1` removes plugins *after* the build, so nothing
  in the source tree, the build log or a local `binaries/` proves what shipped.
  The two-command proof, run on the downloaded asset:
  ```sh
  gh release download vX.Y.Z -p 'pulsar-windows-x64-*.zip'
  unzip -l pulsar-windows-x64-full-vX.Y.Z.zip | grep -ci nv-filters   # expect 0
  unzip -l pulsar-windows-x64-v<previous>.zip | grep -ci nv-filters   # expect >0
  ```
  Run the **control on the previous release too**. A `grep -c` returning 0 on a
  misspelt pattern also returns 0, and a strip that silently stopped matching
  looks exactly like a strip that worked. At v1.4.0: 0 entries in both new zips,
  50 in v1.3.0 (incl. `obs-plugins/64bit/nv-filters.dll`), `obs-nvenc` untouched
  at 53 — NVENC is a *different* plugin, check it survived.
- **Re-assert on the consumer's disk, not on its lockfile.** `npm warn
  allow-scripts` lists `@clodocapeo/pulsar-bundle-full` as "not yet covered",
  which reads like the postinstall was skipped and the binary is stale. Settle it
  by fact, not by reading the warning: `binaries/README.txt` carries the version
  line (`Pulsar v1.4.0`), and `find binaries -iname '*nv-filters*'` must be
  empty. Both are one command and neither can be faked by a lockfile.
- **Dropping a plugin need not move the manifest fixture.** At v1.4.0
  `manifest:check` went red on `bundleVersion` / `libobsVersion` only: the filter
  inventory comes from `obs_enum_filter_types`, and `nv-filters` never registers
  on a machine without the NVIDIA SDKs, so it was already absent from the
  capture. Do not infer from a green-after-recapture that the strip was inert —
  the capture reflects the *capturing* machine, the zip reflects the ship.
- **For a manifest-shaped release, the consumer's re-capture *is* the content
  proof** (v1.5.0) — cheaper than unzipping the asset and it exercises the
  binary rather than the archive. `npm run manifest:capture` on Prism after the
  bump returned exactly `bundleVersion` 1.4.0 → 1.5.0, `libobsVersion`,
  `destination_kinds` + `youtube`, and the two new blocks
  (`graphics_adapters`, `output_scales`). A capture that moves only the two
  version lines means the payload did **not** ship: stop and check the zip.
- **A new capability block does not necessarily move the inclusion guard.**
  At v1.5.0 `EXPECTED_UNDECLARED` stayed empty and the guard's six categories
  were untouched, because `graphics_adapters` / `output_scales` are not yet
  enumerated by Prism's registry (that is the B8 / B1 work). Re-capture, run
  the guard, and leave the dead-man switch alone — do not add categories to
  `CATEGORIES` "while you are there": the guard is meant to fire when the
  registry starts driving them, and pre-wiring it disarms exactly that.
- **`npx eslint .` on Prism can run past 30 min on the dev box** while the same
  tree's 2 018 vitest tests finish in 40 s, and it gets dramatically worse with
  two concurrent runs. Never chain `npm run lint && npm run typecheck` behind a
  short timeout and conclude the tree is broken: `tsc` on both tsconfigs, the
  build and the full suite all complete in minutes, and the PR's
  `Lint + typecheck + build` job on CI is the authoritative answer. Scope
  locally (`npx eslint <path>`) if you need a fast signal.
- **Prism's full `vitest run` fails 2 suites under parallel load on the dev
  box** — `broadcast-engine.test.ts` and `scene-server.test.ts`, on 5 s timeouts.
  Both are fully `vi.mock`-ed (they never spawn `pulsar.exe`), both pass in
  isolation (200/200), and both fail identically on the unmodified tree. Confirm
  that triple before spending time on it: it is machine load, not the bump.
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
