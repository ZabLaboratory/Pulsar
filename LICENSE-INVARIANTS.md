# License invariants — non-negotiable

The native Pulsar/OBS distribution is **GPL-2.0-or-later**. The published
TypeScript packages declare their own licenses. The four rules below are
the maintainer's engineering/distribution policy, not a legal determination
that a process boundary automatically exempts every consumer from GPL
obligations or that a breach automatically relicenses an application.

This document is the project contract. Every PR — Pulsar-side or
consumer-side — that crosses the boundary must honour it. CI enforces what
can be enforced ; the rest is on reviewers and authors.

If you find yourself thinking *"but this case is special / faster /
cleaner"*, the answer is **no**. Re-read the alternative paths in
[Tempting designs to refuse](#tempting-designs-to-refuse). Pick one.

---

## The four invariants

### 1. Process boundary

Pulsar runs in **its own OS process**, spawned as a child by the consumer.

- ❌ Never embed Pulsar in-process.
- ❌ Never load `obs.dll`, `obs-frontend-api.dll`, or any Pulsar-built
  artifact into the consumer's address space.
- ❌ Never run Pulsar logic on the consumer's threads.

This is a required architectural separation under project policy. It is
not, by itself, a legal test for every distribution scenario.

### 2. WebSocket-only IPC

The host application control API is obs-websocket loopback (port + auth).
Do not add a host integration that binds directly to internal media-memory
or native-control structures.

- ❌ No shared memory.
- ❌ No named pipes.
- ❌ No `mmap` / file-backed handshake-then-read-pixels.
- ❌ No DBus / COM / RPC native bindings.
- ❌ No signals beyond OS lifecycle (`SIGTERM` / `kill` is fine).

Pulsar-internal producer/helper IPC and the shipped DirectShow return
implementation are described in [architecture](docs/ARCHITECTURE.md).
They are not a new host FFI contract or permission to load Pulsar code into
the application process. A proposed new host media boundary requires
explicit architectural and license review.

If a request needs richer plumbing (subscribe to events, push frames,
push audio), add it to the obs-websocket vendor namespace
(`pulsar:*`). The wire format stays JSON-over-WS.

### 3. No FFI / native bindings on the consumer side

The consumer **must not** dynamically link or statically include any
Pulsar-built code or symbol.

- ❌ No `dlopen` / `LoadLibrary` of Pulsar artefacts.
- ❌ No N-API / Node-API addon that wraps libobs or any Pulsar plugin.
- ❌ No NaCl / WebAssembly module importing Pulsar code.
- ❌ No AOT-compiled stub linking Pulsar's `.lib` / `.so` / `.dylib`.

The final executables have no public export API; plugin DLLs expose the
required OBS module ABI. CI checks the allowed surface (see
[Enforcement](#enforcement)).

### 4. No copy-paste of Pulsar source into the consumer

The consumer **must not** vendor Pulsar source files, even helpers that
look attractive (state machine, stream key validator, audio capture
helper, x264 wrapper…).

- ❌ No copy-paste of `.cpp` / `.h` / `.ts` / `.py` from Pulsar into
  consumer source trees.
- ❌ No "I'll just rewrite this exactly" with the Pulsar file open. Read
  the public docs instead, or call a vendor request.
- ✅ Re-implementing from public Microsoft / Linux / standard
  documentation is fine.
- ✅ Asking Pulsar to expose a vendor request that returns the data the
  consumer needs is the recommended path.

---

## Tempting designs to refuse

These come up periodically. Refuse on sight.

| Tempting framing | Crosses | Safe path |
|---|---|---|
| *"Expose the internal frame mapping directly to the host"* | #2 | Use the supported integration boundary; a new media transport needs explicit review. |
| *"Direct dlopen of `obs.dll` for richer device enumeration"* | #3 | Add a vendor request (`pulsar:ListDevices`) and call it via WebSocket. |
| *"Copy this Pulsar Windows audio session helper, it took weeks to debug"* | #4 | Re-implement from public Microsoft docs. Don't open the Pulsar file. |
| *"Bundle obs-browser as an N-API module so the consumer can render scenes inline"* | #1 + #3 | The browser source runs INSIDE Pulsar (CEF subprocess of Pulsar). Consumer just sends the URL via vendor request. |
| *"Run Pulsar's vendor request handler in the consumer's main process for a faster path"* | #1 | Vendor request handlers live in Pulsar plugins, end of story. |
| *"Copy native engine helpers into the host for convenience"* | #4 | Use the published client/bundle APIs under their declared package licenses; do not copy native GPL implementation files. |
| *"Embed CEF in the consumer too for preview, share the runtime with Pulsar"* | Crosses if same artefact | Use the consumer's own CEF runtime (e.g. Electron's bundled one), separate artefact. Never import the Pulsar-vendored CEF DLLs. |

---

## The `@clodocapeo/pulsar-bundle-full` wrapper is the watchdog point

The wrapper bridges Pulsar (GPL) and the consumer's runtime (Node /
Electron / etc.). Every PR that touches it must answer:

> Does this make the bundle dependency more entangled with Pulsar's
> compiled code?

The answer must be **less or unchanged**.

Today the wrapper:

- Downloads/stages the separately distributed native runtime.
- Exposes `spawn()` returning a `SpawnedPulsar` with `client` (WS-backed)
  + `port` + `libobsVersion` (string, served by a WS request) + `shutdown()`
  (disconnects the client and terminates the child; no universal graceful Windows media-finalization guarantee).
- Ships TypeScript **interface declarations** describing the WS contract.
- No vendored Pulsar source files. No `bindings.gyp`. No native deps.

If a PR adds anything beyond *"send WS messages / read child process
stdout / signal child / read bundled binary path"*, it needs license
review. Add a `LICENSE-AUDIT.md` checklist in the PR body. The author
must explain which invariant the change touches and why it's still safe.

---

## Enforcement

### Pulsar-side CI (this repo)

- **Source grep** (code-changing PRs, main and tags) — the license-isolation step in [pipeline.yml](.github/workflows/pipeline.yml)
  fails the build if any of these patterns appear in `plugins/` /
  `scripts/` / top-level (excluding `upstream/` which is the OBS
  submodule, and the documented historical comment whitelist):

  - `__declspec(dllexport)`
  - `EXPORT_SYMBOL`
  - `napi_*`
  - `node-api`
  - `prism` (case-insensitive — Pulsar has no business referencing Prism
    by name)
  - `electron`

- **Binary exports** (Windows build pipeline and release tags) —
  `scripts/check-binary-exports.ps1` runs `dumpbin /exports` on every
  Pulsar-owned binary in the rundir:
    - `pulsar.exe` and `pulsar-browser-page.exe` — export table MUST be
      empty (they are final binaries, no consumer should ever bind to
      them at link-time).
    - Plugin DLLs (`pulsar-*.dll`, `obs-websocket.dll`) — MAY export
      only the OBS module ABI (`obs_module_load`, `obs_module_set_pointer`,
      `obs_module_ver`, …). libobs calls these via `GetProcAddress`,
      so they are unavoidable. ANY other exported symbol is treated as
      an FFI surface and fails the build. Wired into the Windows build in `pipeline.yml`; the compliance
      workflow does not substitute for inspection of compiled binaries.

### Consumer-side audit (Prism, future Pulsar consumers)

Each consumer maintains its own audit covering:

- No `dlopen` / `LoadLibrary` / `require` / `import` of any file under
  `pulsar.exe`'s install dir.
- No native module that depends on Pulsar's `.lib` / `.dylib` / `.so`.
- No copy-pasted Pulsar source.

For Prism specifically, `Prism/CLAUDE.md` records the audit done on
`2026-04-29` and points back to this document.

---

## Authority

These invariants were locked-in by the project maintainer on
`2026-05-01`:

> *"Tant que tu garantis la protection de license, et le fait que tout
> app utilisant consomme Pulsar a sa frontière pour ne jamais confondre
> une partie de Pulsar et protégé d'une dérivation de license. Tu peux
> dev et améliorer Pulsar."*

Translation: dev / improvement of Pulsar is authorised **conditional on
these invariants being preserved**. Breaking one is not a tradeoff
discussion — it requires escalation and review before release. Push back, propose
an alternative path, escalate to the maintainer if the alternative is
not obvious. Do not just merge.
