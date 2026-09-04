# Pulsar #251 — T-bar Preview / On-Air UX and accessibility

<!-- AGENT_CHECKPOINT: UX specification complete; relevant contract tests green;
full-suite limitation recorded in §9. -->

Status: `READY` — UX specification and executable validation plan; no production
UI code is changed by this work unit.

Work unit: `pulsar-251-ux-accessibility`
Role: `atelier` (`Atelier-251`)
Thread: `atelier-251-ux-accessibility`
ADR: `ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane-20260828`
Issue: `ZabLaboratory/Pulsar#251`

## 1. Evidence boundary and product promise

Pulsar is a headless runtime. The repository explicitly builds with
`ENABLE_FRONTEND=OFF` and `ENABLE_UI=OFF` ([`docs/DEVELOPMENT.md`](DEVELOPMENT.md)),
and describes one headless `pulsar.exe` with no OBS desktop UI
([`README.md`](../README.md)). There is therefore no operator cockpit, browser
route, AX tree, or screenshot to inspect at this revision. The `upstream`
submodule is also not populated in this worktree. This document is a design
and verification contract, not a claim that a visual implementation already
exists.

The operator promise is deliberately narrow:

> I can see what is currently On-Air, prepare and scrub the next scene in
> Preview, and know whether a Take is only accepted, actually committed at a
> frame boundary, aborted, or rejected. A retry can never look like a second
> commit.

Primary users and failure cost:

| Perspective | Need | Failure to prevent |
| --- | --- | --- |
| New operator | Persistent labels and plain-language state | Confuses Preview with the live output |
| Experienced TD | Fast keyboard/pointer path with a safe commit boundary | Fires a Take while the preview is stale or frozen |
| Accessibility user | Full equivalent path without colour, motion, or pointer | Cannot tell pending from committed, or cannot recover |
| Support/operator on recovery | Stable code, correlation, and a next action | Retries a race and creates a duplicate-looking commit |
| Mobile/narrow viewport | On-Air stays visible and controls remain reachable | Safety-critical controls are clipped or too small |

## 2. Direction: one stable operator surface

The cockpit must keep the *logical* roles in fixed positions. Do not label a
primary pane `Lane A` or `Lane B`; those are diagnostics, not operator roles.
The physical lane may permute after a commit while the `On-Air` and `Preview`
surfaces remain stable (ADR I3/I4).

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Runtime status  ● Connected   Runtime ID …        [Diagnostics ▾]    │
├─────────────────────────────┬────────────────────────────────────────┤
│ PREVIEW                     │ ON-AIR                                 │
│ scene name                  │ scene name                             │
│ [video / empty / loading]   │ [video / stable program]               │
│ Preview state                │ LIVE — never optimistic                │
│                             │                                        │
│ [Prepare]  [T-bar slider]   │ commit ledger / last frame (details ▾) │
├─────────────────────────────┴────────────────────────────────────────┤
│ T-bar: 0% ────────────────●────────────── 100%                       │
│ [Cancel pending Take]                         [Take / Commit Preview] │
│ Status: human-readable state + recovery action    live region         │
└──────────────────────────────────────────────────────────────────────┘
```

The layout is a direction, not a request to add a new native frontend to this
repository. Forge may map it to the owning consumer when a UI work unit exists.

Required hierarchy:

1. `On-Air` label, scene name, and stable program preview are always visible.
2. `Preview` label, scene name, readiness, and preparation controls are always
   distinguishable from On-Air.
3. The T-bar is a transition *input*; its position is never proof of commit.
4. The status region says what happened and what the operator can do next.
5. Frame/PTS, lane IDs, revisions, `server_seq`, and payload digest are behind
   an accessible `Diagnostics` disclosure, not the primary visual hierarchy.

Safety decision: releasing or dragging the T-bar must not silently claim a
commit. A committed change is confirmed only by the correlated
`TakeCommitted` event. If the product later chooses “reach 100% to Take”, the
same pending/commit states and event guard remain mandatory.

### 2.1 Controller hand-off to Prism (observed consumer boundary)

The available Prism cockpit evidence gives the consumer a useful visual grammar:
the central stage already distinguishes `Préparation` from `À l’antenne`, the
scene rail is on the left, the pilot rail is on the right, and the target chip
uses explicit copy (`Cible : préparation, les gestes restent locaux` versus
`Cible : ANTENNE, les gestes partent en direct`). This is observed Prism
evidence outside this repository, not proof that Pulsar v1 is integrated.

Prism’s current `pilotage-cockpit.tsx` is explicitly `PREVIEW ONLY` and its
operator calls use the existing Orion/HTTP path; no Pulsar scene-switch
controller or T-bar is present in that consumer at this revision. The
implementation hand-off is therefore a controller adapter, not a second
cockpit lifecycle:

- Main process owns one `PulsarSceneSwitchController` (v1 `GetState` snapshot
  plus ordered event subscription) and remains the only source of On-Air truth.
- Preload exposes a normalized view model and typed commands; the renderer
  maps it into Prism’s existing stage, scene rail, pilot rail, mode tabs, and
  target chip. It must not infer commit from HTTP `202`, slider position, or a
  local scene selection.
- Add the T-bar, pending/commit status, and commit ledger beside the existing
  transport actions or in the pilot rail. If the stage remains tabbed, the
  `Preview`/`On-Air` labels and current On-Air scene remain persistent while a
  Take is pending; a tab must not hide the safety-critical state.
- Preserve Prism’s preview-first guard: live actions are explicit and target
  `ANTENNE`; Preview remains usable for preparation, while a pending Take
  freezes Preview mutation until Commit/Abort/reconciliation.

No Prism files are changed by this work unit. The actual adapter, browser AX
tree, and keyboard/contrast evidence belong to a Prism UI work unit and must
consume the component and scenario contract below.

## 3. Observable state model and exact copy

The v1 lifecycle is `Prepare → PrepareAccepted → PreviewReady → Take →
TakeAccepted → TakeCommitted` or `TakeAborted`. `TakeAccepted` is a pending
reservation and freeze, never success. `GetState` additionally exposes
`operational`, `frozen`, `revisions`, `role_map`, `server_seq`, and bounded
idempotency-cache counters.

The UI reducer must derive one phase from the event stream and state snapshot;
it must not infer a commit from a request response, T-bar position, spinner
completion, or a changed screenshot.

| Runtime/event condition | Visible label | Controls | Announcement and recovery |
| --- | --- | --- | --- |
| `ready` with no prepared scene | `Ready — prepare a Preview` | Scene selection and Prepare; Take disabled | “No Preview is prepared. Select a scene, then Prepare.” |
| `PrepareAccepted` / `state=preparing` | `Preparing Preview` | Prepare and Take disabled; no fake progress percentage | “Preparing Preview. Waiting for the first rendered frame.” On `TIMEOUT`, show Retry Prepare with refreshed guards. |
| `PreviewReady` / `state=preview_ready` | `Preview ready — Take available` | T-bar and Take enabled | “Preview ready. Take will change On-Air only after commit.” |
| `TakeAccepted` / `state=take_accepted` | `Take pending — commit not confirmed` | Preview mutation and second Take disabled; Abort enabled | “Take accepted and waiting for the frame boundary. On-Air has not changed.” |
| `TakeCommitted` / terminal `state=ready` | `Committed at frame <id>` (transient) then `On-Air: <scene>` | New Preview preparation enabled after event | “Take committed. On-Air is now <scene>. Frame <id>, PTS <pts>.” Announce once per unique command. |
| `TakeAborted(reason=operator)` | `Take cancelled — On-Air unchanged` | Prepare/Take enabled with current snapshot | “Take cancelled. On-Air remains <scene>.” |
| `TakeAborted(reason=timeout)` | `Take timed out — On-Air unchanged` | Retry from a new Prepare/guards | “Take timed out before commit. On-Air remains <scene>.” |
| `TIMEOUT` for Prepare | `Preview timed out` | Retry Prepare | “Preview did not render before the deadline. No output changed.” |
| `operational=false` or `frozen=true` / `state=frozen` | `Controls paused — runtime frozen` | Prepare, T-bar, Take disabled; observation/Diagnostics available | “Runtime is frozen. Program remains live; no new mutation is accepted. Follow the restart/recovery runbook.” Never offer a retry that cannot succeed. |

Every state uses text plus an icon/pattern; red/green/amber/blue alone are
never meaningful. `frame_id` and `pts_ns` are evidence details, not a promise
that the downstream decoder or antenna has changed (ADR I14).

### Rejection copy and recovery

The machine-readable `error_code` is retained in the accessible details. The
diagnostic `error_message` is shown as supporting text and is never parsed by
the UI. No rejection mutates role map, revisions, or surfaces.

| Code | Operator-facing copy | Required recovery |
| --- | --- | --- |
| `REVISION_STALE` / `SERVER_SEQ_STALE` | `State changed — your action was not applied` | Refresh `GetState`; show current On-Air/Preview; prepare again. No optimistic scene swap. |
| `IDEMPOTENCY_CONFLICT` | `This command ID was already used for a different action` | Do not retry automatically; issue a new command ID after reviewing state. |
| `PREVIEW_FROZEN` | `Preview locked while a Take is pending` | Wait for Commit/Abort, or activate the visible Abort pending Take control. |
| `PREVIEW_NOT_READY` | `Preview has not rendered its first frame` | Keep On-Air unchanged; wait for PreviewReady or prepare again. |
| `PREPARE_NOT_FOUND` | `This Preview is no longer prepared` | Prepare again using the latest revisions. |
| `TAKE_NOT_PENDING` | `That Take is no longer pending` | Reconcile the event stream and `GetState`; do not present a commit unless a matching `TakeCommitted` event exists. |
| `TAKE_INTENT_CONFLICT` | `This action belongs to another Take intent` | Discard the stale control state and prepare a new intent. |
| `PREVIEW_LANE_MISMATCH` | `The selected Preview is no longer current` | Refresh roles and prepare the current Preview lane. |
| `RUNTIME_MISMATCH` / `SCHEMA_INVALID` | `This command is not valid for this runtime` | Keep output unchanged; surface Diagnostics and reconnect/configure the matching runtime. |

## 4. T-bar interaction contract

The control is an ARIA slider with an adjacent numeric value and a visible
`Preview → On-Air` direction. The slider is a scrub/transition control, not an
independent source of truth.

- Minimum pointer target: 44×44 CSS px for the thumb and action buttons.
- Keyboard: `Tab` enters the slider; `Left/Right` changes by one step;
  `Shift+Left/Right` changes by a larger step; `Home` and `End` go to 0% and
  100%; `PageUp/PageDown` changes by a page step. The value is announced as
  `T-bar 40 percent`, not only by a moving colour.
- `Enter` on `Take / Commit Preview` is the only keyboard commit affordance.
  `Space` on the slider scrubs; it never commits. `Escape` does not silently
  abort; the pending state exposes a focused `Cancel pending Take` button.
- During `take_accepted`, the slider has `aria-disabled=true`, retains its
  last value, and explains the freeze. It must not jump to 100% as a fake
  success animation.
- Pointer release is safe and idempotent. A network retry must retain the
  original `command_id` and payload; an exact replay displays `Already
  applied — showing the original result`, not a second toast or commit row.
- On commit, reset the T-bar only after the correlated terminal event and
  recompute its value against the new role map. On abort/rejection, retain a
  recoverable control state and keep the current role map.

## 5. Event reducer invariants

Implement the following in the consuming UI model/test harness. They are the
UI-facing restatement of ADR I3/I7/I10/I11:

1. Maintain `Map<(runtime_instance_id, command_id), payload_sha256, outcome>`.
   A same-ID/same-payload response replays the stored outcome byte-for-byte;
   render it as a replay, never as a new commit.
2. Increment the visible commit counter and append a commit-ledger row only
   when a unique `TakeCommitted` event arrives. `TakeAccepted` and a request
   response cannot increment either value.
3. Promote Preview to On-Air only from `TakeCommitted.target_lane_id` and its
   resulting `role_map`; never from intent, T-bar position, or optimistic UI.
4. Ignore a duplicate event by `(runtime_instance_id, command_id, event_type,
   server_seq)` without a second announcement. If a sequence gap is detected,
   label the surface `Reconnecting — outcome not yet confirmed`, disable
   mutation, and reconcile with `GetState` before enabling Take.
5. A stale or rejected command leaves the last known `role_map`, scene names,
   and commit count unchanged. Show the code and next action.
6. On reconnect, clear local pending affordances, fetch `GetState`, and mark
   any unconfirmed command `Outcome unknown — reconciled from runtime`; never
   fabricate a commit from a client timeout.

## 6. Accessibility and responsive acceptance bar

### Semantics, focus, and announcements

- Use landmarks/sections with accessible names `Preview`, `On-Air`, `T-bar`
  and `Transition status`. Each video/snapshot gets a text alternative such as
  `Preview scene: Lower third; status: Preview ready`.
- Use a single `role=status` polite live region for normal lifecycle updates.
  Rejections, freeze, and outcome-unknown messages use assertive announcement
  once; repeated identical events are silent. Never stream every frame or
  countdown tick to a screen reader.
- The focus order is deterministic: connection/diagnostics → Preview scene
  and Prepare → T-bar → Take → Cancel pending Take (when present) → On-Air
  details. Focus remains on the initiating control after a non-terminal error;
  after commit it moves to the status message only if the user initiated via
  keyboard and then returns to Take when ready.
- Every disabled control has a reason adjacent in text or via
  `aria-describedby`; `aria-disabled` is paired with a real guard in the
  event handler. A disabled Take never accepts pointer or keyboard activation.
- Meet WCAG 2.2 AA contrast: 4.5:1 for normal text, 3:1 for large text and
  control boundaries, and a focus indicator with at least 3:1 contrast. Do
  not use red/green as the only distinction.
- Respect `prefers-reduced-motion`: remove preview swaps, pulsing, and
  animated progress; retain text state and a one-time announcement.

### Responsive and content stress

- At wide desktop (`≥960px`): two panes remain side by side, with status and
  Take controls visible without scrolling.
- Below 960px: stack `On-Air` first, then `Preview`, then sticky T-bar/status;
  this keeps the live output above the editable candidate. The primary Take
  target remains reachable without horizontal scrolling.
- At phone width: controls are full-width and 44px high; diagnostics is
  collapsed; video panes may be aspect-ratio reduced but their labels never
  disappear.
- Scene IDs/names up to the v1 256-character limit wrap to two visible lines,
  then ellipsize with an accessible full-name tooltip/details. Messages up to
  1024 characters are summarized in the status region with the complete
  diagnostic available under Details; no layout shift may hide Take or Abort.
- Empty, loading, failed, success, disabled, and frozen states all retain the
  same pane labels and dimensions. A blank video is explicitly labelled
  `No frame received`, never presented as a healthy black Preview.

## 7. Instrumented validation scenarios

The future cockpit or harness must record, for each scenario: viewport and
zoom, keyboard/pointer actions, AX tree before/after, screenshot before/after,
the correlated v1 event list, `GetState` before/after, and the final visible
copy. Redact passwords, scene content not needed for the assertion, and full
payloads; retain IDs, error codes, revisions, role maps, `server_seq`, frame ID,
PTS, and hashes.

| ID | Setup and action | Required proof |
| --- | --- | --- |
| UX-01 happy path | Ready → Prepare → PreviewReady → Take → TakeAccepted → TakeCommitted | Preview and On-Air are distinct before commit; pending copy appears; only after commit does the role map/scene label change; one commit ledger row and one announcement. |
| UX-02 prepare wait/timeout | Prepare without a rendered first frame until `TIMEOUT` | Spinner is text-equivalent; Take remains disabled; On-Air unchanged; Retry Prepare uses refreshed revisions and no silent replacement. |
| UX-03 pending recovery | TakeAccepted, then operator activates Abort | Preview/T-bar freeze is explained; Abort is keyboard reachable; `TakeAborted` leaves roles/revisions/commit count unchanged; controls recover. |
| UX-04 duplicate retry | Replay exact Take command after acceptance and after commit | Same outcome shown as replay; no second `TakeCommitted`, commit row, revision increment, or live announcement. |
| UX-05 idempotency conflict | Reuse command ID with changed payload | `IDEMPOTENCY_CONFLICT` copy, no mutation, one recovery action requiring a new command ID. |
| UX-06 stale race | Two commands use the same expected revisions; one wins | Losing command shows `State changed — your action was not applied`; no optimistic swap, no second commit; state refresh is visible. |
| UX-07 admission/intent errors | `PREVIEW_NOT_READY`, `PREPARE_NOT_FOUND`, `TAKE_NOT_PENDING`, `TAKE_INTENT_CONFLICT` | Each stable code is exposed in Details with a distinct next step; no generic “failed” badge only. |
| UX-08 frozen runtime | `GetState` returns `frozen=true`, `operational=false` | All mutations disabled with reason; Program still visible; no Retry loop; recovery/runbook link is reachable. |
| UX-09 keyboard only | Start at document body; use Tab and documented keys | Every core action is reachable in order; visible focus never disappears; no key commits except explicit Take; live status is announced once. |
| UX-10 narrow/long content | 320px, 768px, 1280px; 256-char scene and long error | On-Air remains first/visible on narrow view; no clipping/overlap; full names/details are available; target sizes and contrast pass. |
| UX-11 reconnect ambiguity | Drop event transport after TakeAccepted before terminal event | UI says outcome unknown/reconciling, disables mutation, calls GetState; it never changes On-Air or claims commit from a timeout. |

## 8. Criteria → evidence matrix

| Issue criterion | Specification proof | Current executable/observed proof | Remaining proof |
| --- | --- | --- | --- |
| Distinguish Preview, On-Air, prepare, acceptance, commit, rejection | §§2–3 fixed role panes, state table, copy, event reducer | Contract lifecycle and event fields are present in [`scripts/contracts/scene_switch_v1/README.md`](../scripts/contracts/scene_switch_v1/README.md) | UX-01/02/03/06 screenshots + AX captures on an actual consumer UI |
| Stale/idempotent never appears as a second commit | §5 invariants; UX-04/05/06; dedupe key and commit counter rule | `test_scene_switch_contract.py` covers duplicate replay, stale guards, concurrent Takes, and non-mutating rejection; 37 relevant tests pass | UI reducer test and real event-stream capture proving one visible commit |
| Pending/failed recoverable and accessible | §3 copy/recovery; §4 freeze; §6 disabled/live-region rules; UX-02/03/07/08/11 | Contract tests cover timeout, abort, `PREVIEW_FROZEN`, and late callback non-commit; 37 relevant tests pass | AX state and keyboard captures for pending, timeout, rejected, frozen, reconnect |
| Keyboard, focus, announcements, contrast, disabled/error | §4 and §6 measurable requirements; UX-09/10 | No UI exists in this headless checkout; CUA found no running app or browser tab to inspect | Browser/consumer implementation plus automated axe/contrast and manual keyboard evidence |

## 9. Validation run and limitations

Commands run from the dedicated worktree:

```text
python -m pytest -q scripts/contracts/scene_switch_v1/test_scene_switch_contract.py scripts/contracts/scene_switch_v1/test_runtime_transport_contract.py
37 passed in 0.55s

python -m pytest -q scripts/contracts/scene_switch_v1
72 passed, 1 failed
```

The full directory run's single failure is an unrelated fixture-presence
assertion in `test_runtime_telemetry_producer_contract.py`: the non-populated
`upstream/shared/obs-shared-memory-queue/CMakeLists.txt` is absent. It is not a
failure of the scene-switch state machine or this UX document.

Observed source facts used by this specification:

- [`docs/PROTOCOL.md`](PROTOCOL.md) documents the v1 vendor lifecycle, strict
  guards, idempotent replay, `GetState`, and the distinction between commit
  evidence and decoder/antenna observations.
- [`plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp`](../plugins/pulsar-frontend-stub/src/pulsar-frontend-stub.cpp)
  exposes `GetState` with `state`, `operational`, `frozen`, revisions, role map,
  sequence, and cache counters; it rejects stale, frozen, not-ready, and
  idempotency-conflict commands without mutation.
- The same source only exposes a frontend callback setter/getter for the T-bar
  position; it does not provide a rendered operator UI in the headless build.

Not claimed here: libobs implementation, low-level protocol changes, audio/AFV,
encoder tuning, a native Qt frontend, a browser deployment, or live/on-air
validation. Those remain outside this work unit or require a separate UI
consumer and its own evidence.

## 10. Hand-off to Forge / UI consumer

Implement the direction in the owning consumer only after the UI work unit is
assigned. Preserve this contract exactly:

- Components: `RuntimeStatus`, `PreviewPane`, `OnAirPane`, `ScenePrepare`,
  `TBarSlider`, `TakeAction`, `AbortPendingTake`, `TransitionStatus`,
  `CommitLedger`, `DiagnosticsDisclosure`, and the main/preload
  `PulsarSceneSwitchController` adapter.
- Data: v1 event envelope and `GetState`; no parsing of diagnostic message
  text; dedupe by runtime/command/payload and unique terminal commit.
- Interactions: no optimistic role swap; freeze controls on `TakeAccepted`;
  enable recovery only from explicit terminal event or refreshed state;
  preserve focus; use standard slider semantics.
- Tokens: role colours must have text/icon equivalents; 44px action targets;
  visible 3:1 focus; 4.5:1 text; reduced-motion variant; two-pane and stacked
  breakpoints at 960px.
- Acceptance: UX-01 through UX-11, with evidence bundle described in §7 and
  all three issue criteria proven in §8.

Status of this work unit: `READY_FOR_REVIEW`; issue remains open and is not
closed by this document.
