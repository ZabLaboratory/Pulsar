# ADR 001 — ATL build-gate & CI compliance gates

- **Status**: accepted
- **Date**: 2026-06-08
- **Decided**: 2026-06-08
- **Deciders**: @ClodoCapeo (maintainer)
- **Author**: Atlas (architect agent)
- **Supersedes**: —
- **Superseded by**: —

---

## 1. Context

Pulsar is a hard fork / patched build of OBS Studio that produces `pulsar.exe`
(see `scripts/build-win.ps1`, `patches/0001-*`, `patches/0002-*`). PR #43
(`forge/pulsar-twitch-scene-switch`) brought the Windows build green on the
`windows-2022` CI runner, wired the org-wide étage-0 merge gate
(`docs/rules/git.md §1`, `docs/rules/security.md §Détection`) into the repo, and
shipped a 30-second live broadcast smoke test for Twitch scene switching.

Three structural build/CI decisions were taken during that work and need to be
recorded. This is the **first ADR of the Pulsar repo** — it also sets the house
ADR format for the repo (numbered sections, `proposed` status set by Atlas,
flipped to `accepted` by Vigil).

The decisions below are **recorded, not re-arbitrated**: they were made and
landed in PR #43. This ADR makes them traceable and carries the residual-risk
ledger that Bastion cleared.

## 2. Decision drivers

- **CI must stay green and unchanged for the canonical build.** The default path
  (full plugin set, ATL present) must behave exactly as upstream OBS + Pulsar
  patches did before PR #43.
- **A build on a machine without the VS2022 "C++ ATL" workload must not silently
  produce a different binary.** Either it degrades explicitly, or it fails loud —
  never an undetected partial build.
- **The étage-0 merge gate is non-negotiable.** `docs/rules/git.md §1` requires
  secret scanning, dependency audit, lockfile check and CODEOWNERS check as
  blocking CI, with no `continue-on-error`.
- **Single responsibility per failure surface.** Governance checks must not be
  tangled with the C++ build graph so a reviewer reads `gh pr checks` cleanly.
- **The live smoke test must prove the broadcast path end-to-end** even where the
  headless OBS build constrains what scene primitives are available.

## 3. Decision

### 3.1 Conditional ATL build-gate (`PULSAR_HAVE_ATL`, default ON)

`scripts/build-win.ps1` detects ATL availability (via `vswhere` plus a probe of
the three `atlmfc/include` headers) and injects the CMake flag accordingly:

| Condition | Flag injected | Plugin set |
|---|---|---|
| ATL present (CI `windows-2022`, canonical dev box) | `-DPULSAR_HAVE_ATL=ON` | full — identical to upstream/CI before PR #43 |
| ATL absent | `-DPULSAR_HAVE_ATL=OFF` | `obs-qsv11`, `win-dshow`, `virtualcam` excluded by `patches/0002-*` |

The default is **ON**, so CI and the canonical dev environment build the full
plugin set unchanged. Only an ATL-less box takes the reduced path.

**Hardening:** when running under CI (`$env:GITHUB_ACTIONS` / `$env:CI`), a
missing ATL is a `throw` (red build), **not** a warning. CI is contractually an
ATL-present environment; ATL absence there means the toolchain regressed and the
run must fail rather than silently ship a reduced binary. The operator-facing
diagnosis and recovery for the local (non-CI) failure is documented in
`docs/runbooks/atl-missing-build-failure.md`.

### 3.2 CI compliance gates (`.github/workflows/compliance.yml`)

A workflow **separate** from `pipeline.yml` (governance vs build — different
concern, cheaper ubuntu runners, independent failure surface) carries the
étage-0 merge gate as four blocking jobs, plus the ownership file:

| Job | Check | Source rule |
|---|---|---|
| `secret-scan` | trufflehog (fs + git history) + detect-secrets against `.secrets.baseline` | `security.md §Détection` |
| `deps-audit` | `npm audit --omit=dev --audit-level=high` | `git.md §1`, `security.md §Détection` |
| `lockfile-check` | `package-lock.json` in sync | `git.md §1` |
| `codeowners-check` | `.github/CODEOWNERS` valid | `git.md §1` |

Plus `.github/CODEOWNERS` itself (maintainer-only on governance/CI/licence paths,
catch-all elsewhere). **No `continue-on-error` anywhere** — every job is allowed
and intended to turn the PR red and block the merge, per `git.md`.

### 3.3 Scene-switch live test uses URL-swap (not OBS multi-scene)

The 30-second live broadcast smoke test switches "scenes" via a **URL-swap
fallback** (`scripts/live-test/scene-a.html` ↔ `scene-b.html`) rather than real
OBS multi-scene orchestration. The headless build **declines `CreateScene`
(returns code 204)** over obs-websocket, so true multi-scene OBS switching is not
available in the headless context. The URL-swap proves the broadcast path
(encode → ingest → Twitch) end-to-end without depending on a scene primitive the
headless build refuses. The underlying multi-scene limitation is a **deferred
architecture item** — see §5 / Deferred, not resolved here.

## 4. Consequences

- **Canonical build unchanged.** ATL present → full plugin set, byte-for-byte the
  same flags as before PR #43; CI behaviour is unchanged.
- **Reduced builds are explicit and reproducible.** An ATL-less box gets a
  documented, flagged subset (`obs-qsv11` / `win-dshow` / `virtualcam` excluded),
  never a silent partial binary; CI can never accidentally ship that subset
  (it `throw`s instead).
- **Merge gate is enforced in-repo.** Every PR to `main` runs the four compliance
  jobs; a leaked secret, a `high`+ npm CVE, a lockfile drift or a broken
  CODEOWNERS each independently blocks the merge.
- **Build vs governance failures are decoupled.** A red `secret-scan` does not
  entangle the C++ build graph and vice versa; the reviewer sees exactly which
  gate broke.
- **Live test is honest about its scope.** It proves the broadcast pipeline, not
  OBS scene graph manipulation — which is correctly flagged as future work.

## 5. Risks

### Residual risks accepted (Bastion clearance)

- **R2 — deps-audit is npm-only.** The Python probes (`websockets`) are dev-only,
  not shipped, and have no pinned lockfile, so they sit outside `pip-audit` by
  choice. **Guard:** as soon as a `requirements.txt` / `uv.lock` is committed to
  this repo, a `pip-audit` job MUST be added to `compliance.yml`. Assumed blind
  spot until then.
- **R3 — `trufflehog --only-verified` + baseline dependency.** Verified-only
  trufflehog catches credentials it can validate against a live service;
  non-verifiable secrets are only caught by detect-secrets against
  `.secrets.baseline`. **Guard:** every future addition to `.secrets.baseline`
  MUST be re-audited (Bastion clearance) before commit — the baseline is a
  suppression surface and must not grow unreviewed.

### Resolved during this work

- **R1 — `ws@8.20.0`** (GHSA-58qx-3vcg-4xpx, moderate, CVSS 4.4, memory
  info-disclosure). Below the `high` threshold of the `deps-audit` gate, so it
  would not have blocked the merge. **Resolved** by bumping to `ws@8.20.1`
  (parallel Forge commit, Refs #43); `package-lock.json` now pins `ws@8.20.1`.

### Deferred (no decision taken here)

- **Headless multi-scene limitation.** The headless OBS build declines
  `CreateScene` (code 204), so there is no real OBS multi-scene switching in the
  headless context — only the URL-swap fallback (§3.3). This is **traced, not
  decided**: if/when true multi-scene OBS switching becomes a requirement, it
  warrants its own ADR (root-cause the 204 — headless module load order, plugin
  set, or a `PULSAR_HAVE_ATL=OFF` interaction — and choose a path). Not in scope
  for ADR-001.

> Security-classed risks are owned by Bastion. R2 and R3 above are accepted with
> their guards as written by Bastion; no further security risk is opened by this
> ADR.

## 6. Resolution criteria

1. `compliance.yml` runs on every PR to `main` with the four jobs
   (`secret-scan`, `deps-audit`, `lockfile-check`, `codeowners-check`) and **no
   `continue-on-error`**; each can independently red the PR.
2. The canonical build (ATL present, CI `windows-2022`) produces the full plugin
   set, with flags identical to pre-PR-#43.
3. An ATL-absent build emits `-DPULSAR_HAVE_ATL=OFF`, excludes the three
   ATL-dependent plugins via `patches/0002-*`, and — **under CI only** — `throw`s
   instead of warning.
4. `package-lock.json` pins `ws@8.20.1` (R1 closed); `npm audit --omit=dev
   --audit-level=high` is clean.
5. `.github/CODEOWNERS` exists and passes `codeowners-check`.
6. The deferred multi-scene item is recorded (this §5) and not silently treated
   as done.

## 7. Alternatives considered (rejected)

- **No ATL gate — require the C++ ATL workload unconditionally.** Rejected: hard
  toolchain dependency that breaks any contributor box lacking the workload, with
  a cryptic C1083 instead of a flagged, documented degradation.
- **ATL absent = warning everywhere (incl. CI).** Rejected: CI would silently
  ship a reduced binary on a toolchain regression. CI is contractually
  ATL-present, so absence there is an error (`throw`), not a warning.
- **One CI workflow for build + governance.** Rejected: tangles a red secret-scan
  with the C++ build graph, slows governance behind MSVC/Windows runners, and
  muddies `gh pr checks`. Split into `pipeline.yml` (build) + `compliance.yml`
  (governance).
- **Implement real OBS multi-scene for the live test.** Rejected for PR #43: the
  headless build declines `CreateScene` (204); resolving it is out of scope and
  deferred to a future ADR (§5). URL-swap proves the broadcast path today.

## 8. References

- PR #43 — `forge/pulsar-twitch-scene-switch`.
- `scripts/build-win.ps1` — ATL detection + flag injection.
- `patches/0002-build-gate-ATL-dependent-plugins-behind-PULSAR_HAVE_ATL.patch`.
- `.github/workflows/compliance.yml`, `.github/CODEOWNERS`, `.secrets.baseline`.
- `docs/runbooks/atl-missing-build-failure.md` — local ATL-missing recovery.
- `docs/rules/git.md §1` (merge gate), `docs/rules/security.md §Détection`
  (secret scanning / deps audit).
