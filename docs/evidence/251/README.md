# Pulsar #251 — consumer UX evidence

This directory contains a non-production, dependency-free consumer harness for
the optional T-bar / Preview / On-Air UX work unit.

## Run

From the repository root:

```text
node --check tests/ux-251/consumer.mjs
node --test tests/ux-251/consumer.test.mjs
node tests/ux-251/consumer.mjs --export docs/evidence/251/ux-scenarios.mock.json
```

Serve the page locally when a browser inspection is useful:

```text
python -m http.server 8765 --directory tests/ux-251
```

Open `http://127.0.0.1:8765/consumer.html`. The page is labelled as a
deterministic local feed and has no authenticated WebSocket claim. A real
transport is opt-in through `?ws=ws://127.0.0.1:4455`; the adapter waits for
actual server events and does not synthesize success.

## Reproducible browser-capture plan (not executed here)

The repository checkout is a headless Pulsar build and this unit adds no
browser automation or axe dependency. To produce a separate browser evidence
bundle, use a browser runner that can save a screenshot and accessibility tree:

1. Serve the directory with the command above and open the page at 100% zoom.
2. Capture the initial AX tree and screenshot at 1280×720, then repeat at
   768px and 320px wide. Confirm On-Air is first/visible below 960px.
3. From the document body, use `Tab` through Diagnostics, the scene field,
   Prepare, T-bar, Take, and (while pending) Abort. On T-bar exercise
   ArrowLeft/Right, Shift+ArrowLeft/Right, Home, End, PageUp and PageDown;
   Space must only scrub. Save before/after AX trees and screenshots.
4. Exercise each deterministic state by calling
   `window.PulsarUx251.runScenario('UX-01')` through `UX-11` in the browser
   harness, or use the page controls in mock mode. Retain the correlated event
   list, visible status copy, role map, commit count and viewport in the
   capture metadata.
5. For a real-path run, reopen with an explicit `?ws=ws://127.0.0.1:4455`
   (or `wss:` endpoint), record the authenticated server provenance separately,
   and only call a commit PASS when the actual `TakeCommitted` event is
   captured. Never merge the mock JSON with a real-WS result.

This plan is intentionally a hand-off: no browser AX-tree or screenshot is
represented as PASS in the committed manifest or mock evidence.

## Contract covered

| Criterion / scenario | Evidence in this checkout |
| --- | --- |
| Preview and On-Air remain distinct; only `TakeCommitted` promotes | `consumer.mjs` reducer + `consumer.test.mjs` reducer assertions + UX-01 snapshots |
| Pending `TakeAccepted` is frozen and recoverable | `consumer.mjs` `take_accepted` state, guarded Abort, UX-03/UX-08 |
| Stale/rejected/conflicting actions do not mutate the core projection | UX-02/UX-05/UX-06/UX-07/UX-08 snapshots and deterministic tests |
| Exact retry is replay, not a second commit or announcement | UX-04 snapshots, `command_records`, `committed_commands`, commit-ledger test |
| Reconnect outcome is unknown until explicit reconciliation | UX-11 snapshots and no-fabricated-commit test |
| WCAG-oriented browser contract | `consumer.html`: French labels, landmarks, one live status region, ARIA slider, visible focus, guard-compatible `aria-disabled`, contrast tokens, 44px targets, reduced motion and responsive stack |

The generated `ux-scenarios.mock.json` includes every UX-01..UX-11 event and
before/after state snapshot. It is explicitly marked `production: false`,
`transport.mode: mock`, and `real_websocket_attempted: false`.

## Evidence boundary and limitations

The event feed is deterministic and local. It proves the consumer reducer and
its accessibility-oriented DOM contract; it does not prove libobs, a deployed
Pulsar runtime, an authenticated WebSocket, or a real on-air output.

This worktree does not include a browser automation/axe dependency and no real
browser AX-tree or screenshot capture is claimed by the JSON evidence. A human
or a separate browser harness can serve `consumer.html`, exercise the documented
keyboard path, and record AX/screenshot artifacts without changing the reducer
contract. The page itself never treats T-bar position, a request response, a
spinner, a timeout, or a changed surface as commit evidence.

Rollback is deleting this non-production harness path or reverting its signed
commit; no runtime or production UI files are changed.
