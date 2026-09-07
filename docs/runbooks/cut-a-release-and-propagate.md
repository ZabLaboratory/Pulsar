# Cut a Pulsar release and propagate it to consumers

Current procedure for the 3.0.0 pipeline. Earlier release incidents are useful
history, not instructions to bypass today's checks.

## Authority and completion

The release owner needs explicit authority to merge, sign/push the release
tag and publish. Eleven uses the configured `ClodoCapeo` account through
`gh` in Git Bash. A delegated specialist uses its own verified role App
and stops at its assigned authority boundary. Follow the active workspace
policy; do not infer administrative bypass permission from this runbook.

A release is complete only when its source revision, signed tag, npm packages,
binary assets and release notes agree, and the tag's required validation
stages have succeeded. Consumer deployment is a separate authorized change.

## Distribution chain

```text
signed source → merged main revision → signed vX.Y.Z tag
                                      ├─ npm client + light/full wrappers
                                      └─ Windows build → light/full ZIPs
                                                       + live proof
                                                       + runtime manifest
consumer install → wrapper postinstall → matching ZIP → actual pulsar.exe
```

The wrappers may warn and let installation complete when binary download
fails. A successful `npm install` alone does not prove a runnable engine.

## 1. Reconcile the exact release scope

Fetch remote refs and resolve the previous release tag to its commit. Compare
that commit with the candidate, not merely the `Unreleased` heading:

```sh
git fetch origin --tags
git log --reverse --format='%H %s' vPREVIOUS..origin/main
git diff --stat vPREVIOUS..origin/main
```

Classify features, fixes, compatibility changes, operational defaults, removed
behavior and known limitations. Keep benchmark workload/codec/hardware bounds.
Record the source boundary and include a full commit inventory when requested.

Use a dedicated repository-local worktree. Preserve dirty canonical checkouts
and active worktrees. Create its `evidence/` directory immediately.

## 2. Prepare one coherent candidate

Update:

- `VERSION`, the native version source;
- versions of `packages/pulsar-client`, `pulsar-bundle` and
  `pulsar-bundle-full`;
- the two bundles' exact `@clodocapeo/pulsar-client` dependency;
- other workspace references when a major version changes their range;
- `package-lock.json`, `CHANGELOG.md` and versioned release notes;
- README/secondary documentation affected by the released behavior.

Do not automatically version or publish independent internal packages.
Refresh the lockfile without running binary-download lifecycle hooks:

```sh
npm install --package-lock-only --ignore-scripts --no-audit --no-fund
npm ci --ignore-scripts --no-audit --no-fund
```

Follow [development](../DEVELOPMENT.md) for workspace builds, typechecks and
tests. Keep source, native/contract tests and hardware-only validation distinct.
Review the final diff, sign the commit with required provenance trailers, push,
and open/update the release PR.

## 3. Merge the validated candidate

Inspect the current PR head, required checks and mergeability. The workflow
now always triggers: its `changes` job routes docs-only changes. Old notes
about `paths-ignore` and mandatory `--admin` are obsolete.

Publish the merge attestation for the exact head. Use the repository's allowed
merge method (squash) and an exact-head guard:

```sh
gh pr merge PR_NUMBER --squash --match-head-commit FULL_HEAD_SHA
```

Do not bypass a missing/red gate. Verify the resulting GitHub commit is
`Verified`, fetch it, and compare its tree/version with the reviewed candidate.
A local canonical checkout can remain stale or dirty; it is not the authority
for choosing the tag target.

## 4. Sign and push the immutable version tag

Verify that `vX.Y.Z` does not already exist locally or remotely, and that the
selected merged commit contains the intended `VERSION`.

```sh
git tag -s vX.Y.Z MERGED_SHA -m "Pulsar X.Y.Z"
git verify-tag vX.Y.Z
git push origin vX.Y.Z
```

Never move an already published release tag to a different binary. Do not
print credentials or repository secret values.

## 5. Follow the tag pipeline to completion

Inspect [.github/workflows/pipeline.yml](../../.github/workflows/pipeline.yml)
for the precise dependencies and current job names.

| Stage | Required result |
|---|---|
| Contract/lint/TypeScript checks | Matching tag/version and green checks. |
| Windows Full build and native probes | Fresh release-grade runtime; no `-Fast` shortcut. |
| Browser/capture compatibility | Explicit result and hardware limitations, not a skipped-hardware claim of coverage. |
| Package light + full | Both ZIPs from the same validated runtime. |
| Live broadcast (Twitch) | Tag run requests 600 seconds of real broadcast and retains its proof. |
| npm publish | Client, light wrapper and full wrapper at the intended version. |
| Release attach | ZIPs, proof video, diagnostic JSON and runtime manifest attached. |

The npm job can finish before packaging/live broadcast. Do not announce
availability until the GitHub assets are present as well. Release attachment
depends on packaging and live broadcast.

Live broadcast is selected for tags and manual dispatch, not ordinary PR/main
pushes. It is serialized by `live-test-twitch`. A queued job is not a failure;
do not cancel unrelated runs or blindly rerun. Diagnose an actual failure,
then retry only the invalidated stages when their retained artifacts suffice.

## 6. Verify published content

Read back all three packages with explicit versions and their `latest` tags,
and inspect the actual release:

```sh
npm view @clodocapeo/pulsar-client@X.Y.Z version
npm view @clodocapeo/pulsar-bundle@X.Y.Z version
npm view @clodocapeo/pulsar-bundle-full@X.Y.Z version
gh release view vX.Y.Z --json tagName,name,isDraft,isPrerelease,assets,url
```

Download both archives and `prism-pulsar-runtime-manifest.json` to an explicit
artifact directory. Verify the manifest's version/tag and full-ZIP SHA-256,
archive version stamps and expected module inventory.

The **full** variant contains `nv-filters`; light strips it. NVIDIA SDK
payloads are not shipped. Full contains Pulsar's CEF browser module/helper;
do not use an old “nv-filters absent from both” assertion.

Run a smoke test against the downloaded runtime and matching SDK, preferably
the README example. Check startup/authentication, reported version, a read-only
request and shutdown. This complements CI; it is not a new live broadcast.

Set the release title and complete notes after the workflow creates the release:

```sh
gh release edit vX.Y.Z --title "Pulsar X.Y.Z" --notes-file docs/releases/X.Y.Z.md --latest
```

Read back the notes and asset list. Retain CI URLs, commit/tag identities,
hashes and diagnostic result in the release closeout.

## 7. Propagate only to authorized consumers

A Pulsar release does not automatically upgrade Prism or another consumer.
For each separately authorized consumer:

1. Update the selected wrapper dependency and lockfile.
2. Reinstall/repackage the desktop application.
3. Verify the actual installed binary version/hash, not only the lockfile.
4. Recapture a capability fixture if its contract requires one and run that
   consumer's current checks.
5. Validate startup, scene/control compatibility and the relevant real output.

Do not assume a consumer's CI is disabled based on a historical incident.
Do not weaken capability inclusion guards to accept a changed fixture.

## Recovery and rollback

Before any publish, a failed run may be retried at the same immutable commit
after its infrastructure cause is resolved. If source changes are necessary,
prepare and validate a new release candidate/version.

After publication, fix forward with a new patch release. Do not delete the
existing GitHub release, replace its assets, unpublish npm versions or retag:
already pinned installations rely on those objects.

An authorized operator can move npm `latest` back to a known-good version,
but pinned consumers still require their own rollback. Restore each consumer's
complete previous wrapper/binary set and rebuild its artifact.

Never treat an existing npm version as proof that the current tag's payload
was published: the pipeline can skip an already-existing version. Verify the
published metadata and binary hashes before declaring recovery.
