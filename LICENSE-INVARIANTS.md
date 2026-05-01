# License invariants — non-negotiable

Pulsar is licensed **GPL-2.0-or-later** (forced by libobs). Any application
that bundles Pulsar (today: Prism ; tomorrow: any consumer) keeps its own
license **only because of the four invariants below**. Each invariant being
broken is enough to contaminate the consumer with GPL retroactively.

This document is the contract. Every PR — Pulsar-side or
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

The boundary is what makes Pulsar's GPL "mere aggregation" with the
consumer rather than a derivative work. Lose the boundary, lose the
license.

### 2. WebSocket-only IPC

The **only** communication channel between Pulsar and the consumer is the
obs-websocket loopback (port + auth). No exceptions.

- ❌ No shared memory.
- ❌ No named pipes.
- ❌ No `mmap` / file-backed handshake-then-read-pixels.
- ❌ No DBus / COM / RPC native bindings.
- ❌ No signals beyond OS lifecycle (`SIGTERM` / `kill` is fine).

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

Pulsar exports zero symbols on purpose. CI verifies it (see
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
| *"Shared memory for video frames, RTMP encode-decode is wasteful"* | #2 | Keep RTMP/H264. Or have Pulsar serve a private RTSP/HLS endpoint the consumer fetches as data. |
| *"Direct dlopen of `obs.dll` for richer device enumeration"* | #3 | Add a vendor request (`pulsar:ListDevices`) and call it via WebSocket. |
| *"Copy this Pulsar Windows audio session helper, it took weeks to debug"* | #4 | Re-implement from public Microsoft docs. Don't open the Pulsar file. |
| *"Bundle obs-browser as an N-API module so the consumer can render scenes inline"* | #1 + #3 | The browser source runs INSIDE Pulsar (CEF subprocess of Pulsar). Consumer just sends the URL via vendor request. |
| *"Run Pulsar's vendor request handler in the consumer's main process for a faster path"* | #1 | Vendor request handlers live in Pulsar plugins, end of story. |
| *"Import the `SpawnedPulsar` TypeScript class with method bodies for type-safe scripting"* | #4 (method bodies = code = derivative) | Type declarations alone are the grey-zone safe path the bundle-full wrapper already uses. Method bodies live in the wrapper as non-GPL JS, never imported from Pulsar source. |
| *"Embed CEF in the consumer too for preview, share the runtime with Pulsar"* | Crosses if same artefact | Use the consumer's own CEF runtime (e.g. Electron's bundled one), separate artefact. Never import the Pulsar-vendored CEF DLLs. |

---

## The `@clodocapeo/pulsar-bundle-full` wrapper is the watchdog point

The wrapper bridges Pulsar (GPL) and the consumer's runtime (Node /
Electron / etc.). Every PR that touches it must answer:

> Does this make the bundle dependency more entangled with Pulsar's
> compiled code?

The answer must be **less or unchanged**.

Today the wrapper:

- Bundles `pulsar.exe` as a binary blob (mere aggregation — fine).
- Exposes `spawn()` returning a `SpawnedPulsar` with `client` (WS-backed)
  + `port` + `libobsVersion` (string, served by a WS request) + `shutdown()`
  (sends SIGTERM).
- Ships TypeScript **interface declarations** describing the WS contract.
- No vendored Pulsar source files. No `bindings.gyp`. No native deps.

If a PR adds anything beyond *"send WS messages / read child process
stdout / signal child / read bundled binary path"*, it needs license
review. Add a `LICENSE-AUDIT.md` checklist in the PR body. The author
must explain which invariant the change touches and why it's still safe.

---

## Enforcement

### Pulsar-side CI (this repo)

- **Source grep** (every PR + push to main) — `.github/workflows/ci.yml`
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

- **Binary exports** (every Windows build, every release tag) —
  `scripts/check-binary-exports.ps1` runs `dumpbin /exports` on every
  Pulsar-owned binary in the rundir:
    - `pulsar.exe` and `pulsar-browser-page.exe` — export table MUST be
      empty (they are final binaries, no consumer should ever bind to
      them at link-time).
    - Plugin DLLs (`pulsar-*.dll`, `obs-websocket.dll`) — MAY export
      only the OBS module ABI (`obs_module_load`, `obs_module_set_pointer`,
      `obs_module_ver`, …). libobs calls these via `GetProcAddress`,
      so they are unavoidable. ANY other exported symbol is treated as
      an FFI surface and fails the build. Wired into `build.yml`,
      `release.yml`, `live-test.yml`. (`ci.yml` skips this gate because
      it does not build binaries — patch-lint + plugin-metadata only.)

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
discussion — it is a license incident that retroactively re-licenses
every consumer that has shipped against the breach. Push back, propose
an alternative path, escalate to the maintainer if the alternative is
not obvious. Do not just merge.
