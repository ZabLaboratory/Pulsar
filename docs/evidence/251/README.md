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

To produce the deterministic browser-DOM capture, add `?capture=keyboard` (or
`?scenario=UX-09`). The page then executes the DOM/focus/`KeyboardEvent` path,
reveals a visible JSON block marked `browser_dom_capture=true`, and exposes a
download link. With Chrome available, a reproducible dump is:

```text
python -m http.server 8765 --directory tests/ux-251
chrome --headless=new --disable-gpu --dump-dom --virtual-time-budget=2500 "http://127.0.0.1:8765/consumer.html?capture=keyboard" > browser-dom-evidence.html
```

The dumped DOM is still only DOM/focus/keyboard evidence. It must not be
reported as an AX-tree, screenshot, screen-reader, authenticated WebSocket or
deployed-runtime result.

The command was exercised with Chrome headless on this work unit. The dumped
DOM contained `browser_dom_capture=true`, `capture_mode: keyboard`, a complete
focus trace and 16 actions. T-bar keyboard/Space actions kept
`commit_count_before: 0` and `commit_count_after: 0`; the explicit Take Enter
path was the only commit (`commit_count_after: 1`), followed by the role-map
swap and visible `Commit confirmé` status. The compact observation is stored
in [`browser-dom-capture.observed.json`](./browser-dom-capture.observed.json);
it is still mock transport evidence. A paired CDP inspection captured the
browser AX tree (1,347 nodes, including landmarks, labels, slider, buttons and
status regions) and a 20,622-byte PNG render in memory; the compact record is
[`browser-ax-capture.observed.json`](./browser-ax-capture.observed.json) and
the image is [`browser-ax-capture.png`](./browser-ax-capture.png). The initial
pre-Tab capture observed 843 AX nodes; the current count is larger because the
required 16-action Tab/Shift+Tab trace is serialized in the visible evidence.

## Reproducible screen-reader/axe follow-up plan (not executed here)

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

This plan is intentionally a hand-off for screen-reader/assistive-technology
and axe validation. The committed CDP record is browser AX-tree and screenshot
evidence for the mock consumer only; it is not a screen-reader result.

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

The event feed is deterministic and local. Together with the opt-in Chrome DOM
dump and paired CDP record, it proves the consumer reducer and the exercised
DOM/focus/keyboard/browser-AX/render contract for this mock page; it does not
prove libobs, a deployed Pulsar runtime, an authenticated WebSocket, a screen
reader, assistive-technology behavior, or a real on-air output.

This worktree does not include a browser automation/axe dependency. A human or
a separate browser harness can serve `consumer.html`, exercise the documented
keyboard path, and record screen-reader/axe artifacts without changing the
reducer contract. The page itself never treats T-bar position, a request
response, a spinner, a timeout, or a changed surface as commit evidence.

Rollback is deleting this non-production harness path or reverting its signed
commit; no runtime or production UI files are changed.
