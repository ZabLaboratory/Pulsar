# pulsar-browser

The full distribution's headless CEF browser-source implementation, maintained
from obs-browser source. It builds `pulsar-browser.dll` and
`pulsar-browser-page.exe`. It is distinct from the nested upstream
obs-browser patch in [patches/](../../patches/README.md).

## Why a maintained fork

OBS Studio browser integration assumes UI surfaces and lifecycle conventions
that a headless media process does not provide. Pulsar removes browser
docks/tooltips and Qt UI linkage from this component while retaining the
`browser_source` model used by libobs and v5 clients.

The helper drops exported GPU-preference symbols and uses Pulsar's helper
name. Removing those symbols does not guarantee which adapter Windows selects.
CEF is built without its sandbox SDK and receives `--no-sandbox`; loaded
web content is therefore part of the application's trust decision.

## Current changes

| Area | Files / behavior |
|---|---|
| Build and helper | `CMakeLists.txt`, helper entry: explicit headless source/linkage, no browser Qt UI loop, correct runtime staging and no application FFI exports. |
| CEF command line | `browser-app.cpp`: required headless switches while preserving the supported GPU-accelerated path. |
| Software/accelerated render callbacks | `browser-client.cpp`: rendering behavior and callback admission coordinated with source/texture lifetime. |
| Render gate | `browser-render-callback-gate.hpp`: callback leases, pause/drain and close admission share synchronized ownership. |
| Async task lifetime | `browser-source-task-state.hpp`: posted tasks retain source ownership through destruction; late work cannot use a deleted BrowserSource. |
| Source lifecycle | `obs-browser-source.cpp/.hpp`: create/update/destroy and source-owned browser/task state. |
| Module shutdown | `obs-browser-plugin.cpp`: browser pre-shutdown barrier used by the headless bootstrap before libobs/audio teardown. |
| Webpage control | Managed sources are pinned to None by Pulsar's source-creation boundary; this plugin is not permission for a rendered page to control the engine. |

The fork is not byte-identical to upstream rendering/lifecycle code.
Source registration compatibility does not mean every internal implementation
or runtime capability is unchanged.

## Rendering paths

Accelerated offscreen rendering uses shared D3D11 textures on supported
hardware. The software path has separate deterministic handling and tests.
A source may be registered and a URL accepted while its frames are black,
frozen or stale; validate actual rendered/recorded output.

A no-physical-GPU hosted runner cannot establish accelerated CEF behavior.
Do not disable acceleration in the product merely to turn that missing proof
into a green test.

## Lifetime invariants

- Callback admission and destruction are coordinated; a callback cannot pass
  a validity check and then race texture destruction unchecked.
- A posted CEF task can outlive the OBS callback that posted it. Source-task
  ownership remains until the last admitted task releases.
- Browser close completion is observed before final source deletion.
- Native shutdown drains browser work before frontend/libobs/audio teardown.
  A failed barrier refuses unsafe continuation.

Hot-lane role exchanges preserve active producers; actual source replacement
and session shutdown have different ownership transitions.

## Runtime layout

The helper and CEF dependencies/resources belong together under
`obs-plugins/64bit/`. The build/packager removes upstream
`obs-browser.dll` and `obs-browser-page.exe` from the distribution so only
the Pulsar implementation registers the source.

Only the full bundle includes this capability. Preserve CEF locales/resource
files when restaging. Do not infer one exact cause from a helper exit code;
check dependency layout, duplicate loaders, rendering capability and logs.

## Source API and trust

The standard browser settings (URL, dimensions, FPS, CSS and audio routing)
remain the integration surface. Prefer the managed
[pulsar-scene helper](../pulsar-scene-source/README.md) for one composed page,
or the [scene-switch contract](../../scripts/contracts/scene_switch_v1/README.md)
for independent hot Preview/Program production.

Keep scene content/server within the application's intended trust boundary.
Webpage control None prevents that control surface; it is not equivalent to
a sandboxed renderer or a general security claim about arbitrary URLs.

## Validation and maintenance

The native shutdown/lifecycle tests force callback/task/close interleavings.
The [capture/PGM package](../../packages/capture-pgm-compat/README.md) tests
real healthy/black/frozen browser recordings when the native hardware path
is available. The full build/package and binary-export gate cover staging.

An upstream rebase must preserve all current lifecycle, render and control
invariants, not just the original “remove Qt and rename helper” edits.
Do not overwrite the fork with a blind recursive copy. Review the upstream
diff, apply changes in an isolated worktree, rebuild and requalify both
callback-level and real rendered-output paths.

### Private CEF qualification without a stream

`scripts/probe-cef-shutdown.py` complements the full two-browser/recording
integration test in `tests/pulsar-headless-shutdown/test_graceful_shutdown.py`.
It reuses M3's HTTP page, WebSocket protocol and decoded-pixel oracle, and the
group-composition probe's inherited shutdown-event process owner. The optional
`--with-audio` case reuses the full shutdown suite's synthetic looped WAV page.
It does not open physical audio devices or start an output.

Build the actual product browser targets with
`tests/pulsar-headless-shutdown/private-browser-build/CMakeLists.txt`, setting
`PULSAR_SOURCE_ROOT` to this worktree and `PULSAR_UPSTREAM_BUILD_DIR` to an isolated
build overlay. Its read-only `libobs`, `config` and `frontend` dependencies must
come from the same ABI as the supplied runtime and host. The product CMake writes
the module, helper and complete CEF resources into that overlay's `rundir`;
never point this output at an installed or shared runtime.

Run the probe with explicit `--runtime` (the known no-browser ABI base), `--host`,
`--browser-runtime` (the freshly built `obs-plugins/64bit` directory), and a new
canonical `--output` JSON path under the qualification unit's evidence directory.
The private copy must contain only `pulsar-browser.dll`, never the upstream
`obs-browser.dll` beside it. The source remains active through shutdown. Required
evidence includes real page pixels, one browser entering close, the drained
barrier, ordered CEF shutdown, exit zero and no forced termination. The audio
case also requires stream start/stop and quiescence before CEF teardown.

The report records native binary and browser-source hashes; logs redact the
private WebSocket password. Scratch runtime/state is removed after the owned
child exits. This proves a private CEF lifecycle, not installed Prism, Solar/LSDP
integration, physical captures, performance parity or streaming endurance.

License and upstream notices: see [LICENSE](../../LICENSE) and this source
tree's notices. The headless runtime distribution follows
[LICENSE-INVARIANTS.md](../../LICENSE-INVARIANTS.md).
