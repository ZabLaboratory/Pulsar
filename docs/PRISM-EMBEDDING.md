# Embedding Pulsar in Prism (and other consumers)

This is the contract Prism (and any other consumer that bundles Pulsar)
must honour to stay on the safe side of the GPL boundary and reach a
working obs-websocket v5 session.

This doc supersedes scattered notes in `docs/ARCHITECTURE.md` and
`docs/PROTOCOL.md`. Anything below is non-negotiable for V1.

## 1. The boundary

Pulsar is **GPL-2.0-or-later**. Prism stays on its own license by
respecting four invariants (see [`LICENSE-INVARIANTS.md`](../LICENSE-INVARIANTS.md)):

1. **Process boundary.** Pulsar runs as a separate OS-level child
   process. Prism never loads `pulsar.exe`, `libcef.dll`, any
   `pulsar-*.dll`, or any libobs binary into its own address space.
2. **WebSocket-only IPC.** The only channel between Prism and Pulsar
   is the obs-websocket v5 connection on a localhost port. No FFI,
   no shared memory, no native bindings.
3. **No FFI surface on Pulsar's side.** `pulsar.exe` and
   `pulsar-browser-page.exe` export zero symbols; plugin DLLs export
   only the OBS module ABI. Enforced by
   `scripts/check-binary-exports.ps1` in CI.
4. **No source copy-paste.** Prism doesn't include code copied from
   the Pulsar / libobs / obs-websocket / obs-browser source trees.

Breaking any of these four turns Prism into a derivative work and
forces it under GPL.

## 2. What ships in the bundle

Use `scripts/package-win.ps1 -Zip [-Full]` (run on the Pulsar side) to
produce a self-contained zip — light by default, full with `-Full`. The
extracted directory layout is:

```
pulsar-windows-x64-v<version>/         (light, ~100 MB, 989 files)
pulsar-windows-x64-full-v<version>/    (full,  ~370 MB, 1252 files)
├── README.txt
├── bin/
│   └── 64bit/
│       ├── pulsar.exe              <-- service entry point
│       ├── obs.dll
│       ├── Qt6Core.dll, Qt6Gui.dll, ... (Qt6 runtime)
│       ├── avcodec-61.dll, ... (FFmpeg suite)
│       └── libx264-164.dll, ...
├── obs-plugins/
│   └── 64bit/
│       ├── pulsar-multi-stream.dll  <-- Pulsar plugins
│       ├── pulsar-scene-source.dll
│       ├── pulsar-browser.dll       (full variant only)
│       ├── pulsar-browser-page.exe  (full variant only)
│       ├── obs-websocket.dll        (Pulsar fork)
│       ├── libcef.dll, chrome_elf.dll, ... (full variant only)
│       └── ... upstream OBS plugins (image-source, obs-x264, ...)
└── data/
    ├── libobs/                  <-- effects (default.effect, ...)
    └── obs-plugins/<name>/      <-- per-plugin assets + locales
```

**Choose the variant Prism needs.**

- **`light`** if Prism never uses HTML/CSS/JS scenes via `browser_source`.
  Window capture, monitor capture, game capture, image, NDI, video
  files, RTMP/RTMPS, multi-destination, and adaptive bitrate all work.
- **`full`** if Prism wants `browser_source` (CEF-rendered web overlays
  / scene composition). Adds CEF runtime and `pulsar-browser-page.exe`
  helper.

Drop the chosen folder into `Prism/resources/pulsar/` preserving the
internal layout exactly. Do not flatten or reshuffle — libobs resolves
data paths relative to `pulsar.exe`.

## 3. Spawning the service

```ts
// Pseudocode -- Electron main-process side.

import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { join } from 'node:path';

const RESOURCES   = join(app.getAppPath(), '..', 'resources', 'pulsar');
const PULSAR_BIN  = join(RESOURCES, 'bin', '64bit');
const PULSAR_EXE  = join(PULSAR_BIN, 'pulsar.exe');

// Fresh credentials per Prism launch. Pulsar will pick them up via
// env vars and seed obs-websocket/config.json before plugins load.
const sessionPassword = randomBytes(16).toString('base64url');
const sessionPort     = 4455;  // pin or pick a free port; see below.

const child = spawn(PULSAR_EXE, [], {
  cwd: PULSAR_BIN,                       // <-- MANDATORY: libobs resolves
                                         //     data/ paths relative to cwd.
  env: {
    ...process.env,
    PULSAR_PORT:     String(sessionPort),
    PULSAR_PASSWORD: sessionPassword,
    // Optional V1 knobs:
    // PULSAR_FPS:           '60',
    // PULSAR_RESOLUTION:    '1920x1080',
    // PULSAR_VIDEO_BITRATE: '6000',
    // PULSAR_RECORD_DIR:    '/path/to/recordings',
  },
  stdio: ['ignore', 'pipe', 'pipe'],
  windowsHide: true,                     // <-- MANDATORY on Windows.
                                         //     pulsar.exe is a CONSOLE
                                         //     subsystem binary; without
                                         //     this flag the OS allocates
                                         //     a visible cmd.exe window
                                         //     for it. Setting windowsHide
                                         //     keeps the subprocess
                                         //     headless from the user's
                                         //     point of view.
});
```

### Mandatory `cwd`

`pulsar.exe` MUST be spawned with `cwd` set to its containing
directory (`bin/64bit/`). libobs walks `..\..\data\libobs\*.effect` to
find its core shaders; any other working directory yields:

```
error: Failed to find file 'default.effect' in libobs data directory
pulsar-headless: obs_reset_video failed (-1)
```

This is the same constraint OBS Studio classique enforces. Documented
once, easy to follow, easy to forget — automate the cwd in your spawn
helper and never look back.

### `windowsHide` is recommended but not strictly required

`pulsar.exe` is built `/SUBSYSTEM:WINDOWS` (no console subsystem),
so Windows never allocates a visible cmd.exe window for it -- the
absence of `windowsHide: true` does not bring back the flash.
Passing `windowsHide: true` is still the correct convention: it sets
the `CREATE_NO_WINDOW` flag which avoids any chance of a transient
flicker on older Windows / certain antivirus drivers, costs zero, and
makes intent clear.

At process boot, `pulsar.exe` calls
`AttachConsole(ATTACH_PARENT_PROCESS)` and rebinds stdio to `CONOUT$`
when the parent has a console. This makes direct invocation from
cmd.exe / PowerShell still print to the operator's terminal for
debugging. Spawned with `stdio: 'pipe'` from Prism, the inherited
pipes take precedence and the AttachConsole call is a no-op.

### Picking a port

`PULSAR_PORT` defaults to `4455`. If Prism may run alongside an OBS
Studio install (which also defaults to 4455), or alongside a second
Prism instance, pick a free port in the ephemeral range and pass it
explicitly. Common pattern:

```ts
import { createServer } from 'node:net';
function pickFreePort(): Promise<number> {
  return new Promise((res, rej) => {
    const s = createServer();
    s.listen(0, '127.0.0.1', () => {
      const port = (s.address() as any).port;
      s.close(() => res(port));
    });
    s.on('error', rej);
  });
}
```

Then pass `PULSAR_PORT: String(await pickFreePort())`.

## 4. The READY handshake

Pulsar prints a single sentinel line on stdout once obs-websocket is
listening and ready to accept the v5 Hello/Identify exchange:

```
PULSAR_READY ws=ws://127.0.0.1:4455 password=8JvK56CjHa0-LYqS3dNC9n
```

The line format is stable and machine-parseable:

```
PULSAR_READY ws=<url> password=<password>
```

A consumer reads stdout line-by-line until it matches `^PULSAR_READY `,
extracts `url` and `password`, and uses them to open the WebSocket.
Lines printed before the sentinel are libobs / plugin boot logs (info /
warning / error / debug); they may be forwarded to Prism's own log
aggregator but MUST NOT block the boot — the sentinel always arrives
last (or not at all, in which case spawn timed out and the consumer
should kill the child).

```ts
// Pseudocode -- handshake side.

import readline from 'node:readline';

const rl = readline.createInterface({ input: child.stdout! });

const ready = new Promise<{ url: string; password: string }>((resolve, reject) => {
  const timeout = setTimeout(() => reject(new Error('pulsar boot timed out')),
                             60_000);
  rl.on('line', (line) => {
    forwardToLog(line);                    // optional: pipe to Prism's log
    const m = line.match(/^PULSAR_READY ws=(\S+) password=(\S+)$/);
    if (m) {
      clearTimeout(timeout);
      resolve({ url: m[1], password: m[2] });
    }
  });
  child.on('exit', (code) =>
    reject(new Error(`pulsar exited with code ${code} before READY`)));
});

const { url, password } = await ready;
// → ws://127.0.0.1:<port>, <password>. Drive obs-websocket v5 from here.
```

The 60-second timeout is generous: a clean Pulsar boot reaches READY in
under 3 s on a warm cache, ~6 s on a cold start with all plugins loading.

## 5. Lifecycle

### Connection

Open one obs-websocket v5 connection. Hold it for the lifetime of
broadcast work. Reconnect with the same `password` on transient drops
(network blip, sleep/wake) — Pulsar keeps the same auth across reconnects
within a session.

### Health

Send `GetVersion` periodically (every 30s) to confirm the service is
responsive. Treat 3 consecutive failures as "Pulsar is dead, respawn."

### Shutdown

When Prism quits:

1. Send WebSocket close frame (`1000` normal closure).
2. Wait up to 5s for `child.exit`.
3. If still alive: `taskkill /F /T /PID <pid>` (Windows) — Pulsar handles
   `CTRL_CLOSE_EVENT` internally and releases libobs cleanly.

Never `taskkill /F` as the first step — it skips libobs's `obs_shutdown`
and leaks the encoder threads.

## 6. What you DON'T do

- ❌ `LoadLibrary("pulsar.exe")` / `LoadLibrary("libcef.dll")` /
  `LoadLibrary("pulsar-*.dll")`. **Never.** Use the WebSocket.
- ❌ `require("pulsar.node")` or any native module that links against
  Pulsar's `.lib`. There is no such module — if you find one, it's a
  bug. Do not write one.
- ❌ Open the `pulsar.exe` binary, parse its memory layout, attach a
  debugger to inject calls, etc. The process is a black box.
- ❌ Read `obs-websocket/config.json` from disk to recover the password.
  Trust the `PULSAR_READY` stdout sentinel only — disk reads race with
  Pulsar's own writes.
- ❌ Set `PULSAR_PASSWORD=""` (empty string) thinking auth is disabled.
  Pulsar treats an empty value as "generate a random password" and
  Prism would have no way to know what was generated. Set a value or
  unset the variable.

## 7. Consumer-side audit

Prism (and every future consumer) maintains its own audit confirming
the four invariants above. Use [`CONSUMER-AUDIT.md`](../CONSUMER-AUDIT.md)
as the empirical checklist — every claim is a runnable check, every
check has pass/fail signal. CI must run those checks on every PR.

## Quick reference

| Step | Action |
|---|---|
| Bundle | Drop `pulsar-windows-x64-<variant>-v<x>/` into `resources/pulsar/`. |
| Spawn | `bin/64bit/pulsar.exe`, `cwd=bin/64bit/`, env `PULSAR_PORT` + `PULSAR_PASSWORD`. |
| Wait | Read stdout until `PULSAR_READY ws=<url> password=<pw>` arrives (≤60s). |
| Connect | Open WebSocket v5 to `<url>`, authenticate with `<pw>`. |
| Use | Send v5 requests + Pulsar vendor extensions (`pulsar:*`). |
| Stop | WS close → wait 5s → `taskkill /F /T /PID` only as fallback. |
