# Pulsar — development (3.0.0)

Build and test Pulsar from a dedicated checkout/worktree. The native target is
Windows x64; the WebSocket client and pure analysis tests can run elsewhere.
The [architecture](ARCHITECTURE.md) explains ownership and the
[OBS/libobs inventory](LIBOBS-CHANGES.md) explains the complete patch stack.

## Toolchain

| Tool | Requirement / use |
|---|---|
| Windows | Windows 10/11 x64 for the native runtime. |
| Visual Studio | VS 2022 C++ desktop tools and Windows SDK. ATL is optional for a reduced build, but required for QSV/DirectShow/virtual-camera coverage. |
| CMake | 3.28 or newer; the script resolves its executable from its configured local candidate or PATH. There is no `-CMakeExe` parameter. |
| Git | Submodule access to the pinned OBS fork and nested dependencies. The configured submodule URL uses SSH. |
| Node.js / npm | Node 18+ package contract; CI uses Node 20. npm workspaces, ESM and TypeScript. |
| Python | 3.11+ for protocol/probe tooling; install each probe's declared dependencies. |
| FFmpeg / ffprobe | Real media validation, recording inspection and correlation tests. |
| PowerShell | Windows build/package/probe orchestration. |
| Physical GPU | Required for accelerated CEF/hardware qualification; software-only CI cannot substitute for that evidence. |

Upstream CMake provisions its pinned dependency archives. Use the repository's
actual script and preset requirements; old phase comments are not a second
build interface.

## Install package dependencies

From the repository root:

```powershell
$env:PULSAR_BUNDLE_SKIP_POSTINSTALL = "1"
npm ci
npm run build -w @clodocapeo/pulsar-client
npm run build -w @clodocapeo/pgm-correlator
npm run build -w @clodocapeo/pulsar-bundle
npm run build -w @clodocapeo/pulsar-bundle-full
npm run build -w @clodocapeo/capture-pgm-compat
npm run lint
```

Skipping postinstall avoids downloading an old/missing released binary while
building this source candidate. It does not make `spawn()` usable without
a native runtime; supply its `binariesPath` explicitly for local work.

## Native build

```powershell
.\scripts\build-win.ps1 -Full
```

The default output is:

```text
upstream/build_x64/rundir/RelWithDebInfo/
  bin/64bit/pulsar.exe
  obs-plugins/64bit/
  data/
```

The build:

1. Resolves the recorded OBS pin and the lexical patch sets.
2. Reuses an exact clean fingerprinted patched checkout, or reconstructs it
   from the pin and replays root and nested-browser patches separately.
3. Configures/builds the upstream runtime with OBS Studio frontend/UI disabled.
   `-Full` enables browser compilation; it does **not** enable the OBS UI.
4. Configures/builds the Pulsar CMake targets against those headers/libraries.
5. Stages the matching Qt/runtime/module resources in the rundir.

**Preserve local upstream work before running this script.** Reconstructing a
generated patched checkout can reset it. Never use a shared/dirty upstream
tree as a scratch space for changes that have not been exported as patches.

### Build switches

| Switch | Meaning |
|---|---|
| no switch | Complete headless build without full browser capability. |
| `-Full` | Complete headless build including browser/CEF; release prerequisite. |
| `-Stage configure` | Configure path, including patch preparation. |
| `-Stage build` | Build using the relevant existing/configured state; not a promise to ignore changed patch inputs. |
| `-Fast` | Narrow incremental target loop using an existing compatible headless cache. |
| `-RefreshPatches` | Force reconstruction/replay even when fingerprints would allow reuse. |
| `-Clean` | Remove the selected build output/cache before configuration; preserve dependency downloads. Use only for an intended cold rebuild. |
| `-UpstreamBuildDir <path>` | Alternate upstream build directory; relative paths resolve from the repository. `PULSAR_UPSTREAM_BUILD_DIR` is the environment alternative. |
| `-CI` | Select the CI upstream preset. |
| `-GuiBuild` | Upstream GUI comparison/debug build, not the distributed Pulsar product. |

`-Fast` builds libobs, D3D11, frontend API, DirectShow/filter, NVENC, x264 and
the Pulsar headless target/dependencies. It inherits the existing browser
capability, but does not rebuild every independent Pulsar DLL.
Do not use it to validate WebSocket/browser/plugin changes that are outside
that target closure. It cannot be combined with `-Full`, `-GuiBuild`,
`-Clean` or configure-only mode.

Packaging currently reads the **default** `upstream/build_x64` rundir.
An alternate build directory is not automatically consumed by the packager;
verify the selected runtime before packaging.

## Run a local build

Prefer the bundle API with an explicit binary path:

```js
import { spawn } from "./packages/pulsar-bundle-full/dist/index.js";

const pulsar = await spawn({
  binariesPath: "D:/path/to/Pulsar/upstream/build_x64/rundir/RelWithDebInfo",
  readyTimeoutMs: 60_000,
});
try {
  console.log(await pulsar.client.capabilities.get());
} finally {
  await pulsar.shutdown();
}
```

Use a path for **your** checkout. The example reads capabilities only; a real
recording/streaming application must finalize outputs before shutdown.

Direct launch uses `bin/64bit/pulsar.exe` and environment configuration,
not a documented `--service --port --config` CLI. The executable resolves
its modules/data from its location and creates/uses a private runtime cwd.
The Node helper allocates a per-child port; a manual supervisor should also
assign one when starting concurrent instances.

## Validation matrix

| Layer | Command / location | What it proves |
|---|---|---|
| TypeScript | `npm run lint` and per-workspace `npm run build` | Types and package builds, not native execution. |
| Client/bundle/correlator tests | `npm run test -w <package>` | SDK behavior, fake-child lifecycle tests, real local WS/FFmpeg tests where named. |
| Contract tests | `scripts/contracts/`, workflow `contract-tests` | Schema, ordering, guards and deterministic state behavior. |
| Native CTest | `ctest --test-dir build -C RelWithDebInfo --output-on-failure` | Registered native/lifecycle/queue/encoder and offline integration gates. |
| Offline runtime | `.\scripts\run-probes.ps1` | Self-spawn and shared-instance v5/media/capability checks. |
| Capture/PGM | [package guide](../packages/capture-pgm-compat/README.md) | Actual CEF capture recorded/decoded, when hardware/opt-in requirements are met. |
| Dual-lane hardware | [canary](runbooks/pulsar-dual-lane-canary.md) and [latency probe](runbooks/probe-take-latency.md) | Same-candidate workload, timing and resource gates. |
| Release broadcast | Tag/authorized dispatch workflow | Real Twitch output plus recording/diagnostics; not a physical-display measurement. |

The offline suite is not “seven probes.” The orchestrator includes startup,
recording, CEF/control lifecycle, service settings, capabilities/presets,
multi-track audio, source inventories, scene truth, monitoring, adaptive
bitrate and record splitting. Its source lists the exact current sequence.
The standalone multi-stream probe is not part of the shared-instance loop.

A hardware skip is **not a pass**. Keep that limitation in the report.
For a failure, inspect the first failed stage and process exit evidence;
later connection-refused errors can be consequences of an earlier crash.

## Packaging

After a complete fresh full build of the intended revision:

```powershell
.\scripts\package-win.ps1 -Variant light -Zip -SkipBuild
.\scripts\package-win.ps1 -Variant full -Zip -SkipBuild
```

Without `-SkipBuild`, the packaging script invokes its required build path,
but it expects the default runtime directory to exist when resolving input.
The explicit build-then-package sequence above is the reliable first-run
procedure. There is no packager `-Full` switch.

Artifacts are created under `dist/`. The full package retains the browser
fork/CEF, text/VLC modules and nv-filters; light strips them. NVIDIA SDK
binaries/models are not included. Check module presence and assets on the ZIP,
not only on the build tree.

## CI and release

[pipeline.yml](../.github/workflows/pipeline.yml) runs on PRs, main pushes,
version tags and explicit dispatch. Its jobs are:

- change classification, lint/source/package audits and contract tests;
- full Windows build, binary export gate, native/offline probes;
- capture/PGM compatibility with explicit hardware skip reporting;
- authorized tag/dispatch live broadcast;
- package, npm publish, release attachment and broadcast-page publication
  when their trigger/dependency conditions are satisfied.

Tag broadcast duration is 600 seconds. Dispatch uses `duration_seconds`
(default 300), `fps` and the explicit package/release/npm toggles.
Do not copy old `live_test_duration_seconds` examples.

Do not skip CI or bypass a required check to publish a documentation/version
refresh. Read the [release runbook](runbooks/cut-a-release-and-propagate.md)
for version/tag consistency, complete changelog, matching assets and readback.

## Maintaining native patches

[patches/README.md](../patches/README.md) is the authoring entry point.
Use a dedicated worktree, preserve the full source lineage and export the
change with `git format-patch`. A nested-browser patch belongs to the nested
repository, not the root OBS checkout.

Do not reset unexported work, renumber the existing stack casually or delete
an intermediate patch as a runtime rollback. Verify replay from the recorded
pin and run the controls invalidated by the change. A core ABI or lifecycle
change needs native validation as well as source application.

## Adding or changing a component

Keep ownership in the existing component when possible. A new CMake target
must be registered in the top-level build, documented in the
[plugin inventory](../plugins/README.md), packaged if appropriate, and covered
by the right native/protocol tests.

A wire change also updates [PROTOCOL.md](PROTOCOL.md), the canonical contract
when applicable, typed client mapping and consumer-facing documentation.
Do not silently change an approved ADR or add unimplemented methods to a
README as if they already ship.

## Versioning and migration

`VERSION` is the native/release version source. The client and two bundle
package versions and exact client dependencies must match it. Refresh the
lockfile and internal tooling dependencies deliberately. Other packages,
such as pgm-correlator, do not become version 3.0.0 merely because Pulsar does.

A source merge, npm publication, release ZIP and installed consumer runtime
are four different delivery states. Verify the actual asset and consumer
deployment only when that consumer is in the authorized scope.

## Troubleshooting

- Missing ATL: [ATL runbook](runbooks/atl-missing-build-failure.md). A reduced
  build cannot prove DirectShow/QSV coverage.
- Missing `default.effect`: check complete executable-relative data layout;
  do not move process state back into a shared binary directory.
- Missing bundle binary after npm success: postinstall can soft-fail a
  download; check release availability and explicit `binariesPath`.
- Native process died: [offline probe diagnosis](runbooks/offline-probe-suite-connection-refused.md).
- Go-live failed: [output diagnosis](runbooks/diagnose-a-failed-go-live.md).
- Wrong return instance or stale reader: [lease watcher](runbooks/directshow-lease-watcher.md).
