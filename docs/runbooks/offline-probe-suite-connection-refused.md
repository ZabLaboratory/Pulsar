# Runbook — Offline probe suite: `ConnectionRefused` / `ConnectionClosed`

**Applies to:** the `offline probe suite (CTest)` job of `pipeline.yml`
(`scripts/run-probes.ps1`, Phase 2 connect-only probes).
**Reference incident:** run `30230046422` on `main` (`0d04641`), 2026-07-27.
**Instrumented by:** PR #132.

---

## Symptom

Two (or more) connect-only probes fail at the end of the suite:

```
==> Running probe-adaptive.py
connecting: ws://127.0.0.1:59036
identified
initial: {...}
waiting 7s for worker to sample...
websockets.exceptions.ConnectionClosedError: no close frame received or sent
==> probe-adaptive.py FAILED (exit 1)
==> Running probe-record.py
ConnectionRefusedError: [WinError 1225] The remote computer refused the network connection
==> probe-record.py FAILED (exit 1)
```

---

## Diagnostic — read the failure in this order

**1. Is it a server death or a probe assertion failure?**

Since PR #132 the suite answers this itself. Look for:

```
==> FATAL: the shared pulsar.exe DIED (pid <n>, exit code <n>)
```

- **Banner present** → the shared `pulsar.exe` crashed. The `ConnectionClosed` /
  `ConnectionRefused` lines are consequences. The probe named "last alive" is
  *where* it died, not necessarily *why*. The suite stops there and lists the
  remaining probes as `NOT RUN`. Diagnose from the **stderr tail** printed right
  under the banner — libobs writes its own log to stderr, that is where the crash
  context lives.
- **No banner** → the server stayed up the whole time and the probe assertion
  genuinely failed. Read the probe, not the infra.

**2. What is ruled out (do not re-investigate):**

| Hypothesis | Why it does not apply |
|---|---|
| Zombie `pulsar.exe` from a previous run | The job runs on `runs-on: windows-2022` — a **GitHub-hosted, fresh VM**, not the self-hosted pool. |
| Port collision / `TIME_WAIT` on 4455 | `run-probes.ps1` binds a `TcpListener` on port 0 per session and reseeds `obs-websocket/config.json`; every run uses a fresh random port. |
| Stale `config.json` from a Phase-1 self-spawning probe | The shared instance's boot is the last writer of `config.json`, by design (see the header of `run-probes.ps1`). |

**3. Known trigger (open, upstream).** The runner has **no audio endpoint**:

```
warning: [WASAPISource::TryInitialize]:[default] Failed GetDefaultAudioEndpoint: 80070490
info: Device '' invalidated.  Retrying (source: probe-input-wasapi_output_capture)
```

`probe-source-kinds.py` creates and destroys `wasapi_input_capture` /
`wasapi_output_capture`, so the WASAPI reconnect thread interleaves with source
destruction — the race already tracked as `TODO(upstream-obs)` in the workflow.
This is the leading suspect for the reference incident; it is **not proven**,
because the stderr tail that would prove it was not captured before PR #132.

---

## Rate & retry budget

The step retries **once** (`nick-fields/retry@v3`, `max_attempts: 2`), documented
at ~7 % occurrence. Note the weak independence: **both attempts run on the same
VM, seconds apart**, so an environment-conditioned race (no audio device) can hit
both — as it did on `30230046422`. The workflow's rule stands: *if both attempts
fail, treat it as real*, and the run is now expected to carry a `FATAL` banner
saying which of the two cases it is.

---

## Resolution

- Server crash (`FATAL` present): attach the stderr tail to the upstream-obs
  investigation. **Do not** re-run blind, **do not** widen the retry budget, and
  never add `continue-on-error` — the gate (#121/#128) is blocking on purpose.
- Assertion failure (no `FATAL`): normal red CI, route to the owner of the probe.

## Rollback

PR #132 touches only diagnostics (`scripts/run-probes.ps1`,
comments in `.github/workflows/pipeline.yml`). Reverting the commits restores the
previous behaviour (crash reported as a client-side connection error); no build,
binary or runtime surface is involved.
