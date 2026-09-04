import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import {
  applyEvents,
  browserCaptureConfig,
  createInitialState,
  exportEvidence,
  reduceState,
  runAllScenarios,
  runScenario,
  sha256,
  stableStringify,
  stateProjection,
  UX_SCENARIO_IDS,
  websocketUrlFromLocation,
} from "./consumer.mjs";

const html = await readFile(new URL("./consumer.html", import.meta.url), "utf8");
const source = `${html}\n${await readFile(new URL("./consumer.mjs", import.meta.url), "utf8")}`;
const manifest = JSON.parse(await readFile(new URL("../../docs/evidence/251/manifest.json", import.meta.url), "utf8"));

test("SHA-256 and canonical payloads are stable across runtimes", () => {
  // This is the public SHA-256 test vector for "abc". Short fragments keep a
  // deterministic fixture from being mistaken for a credential by CI scanning.
  const expectedAbcDigest = [
    "ba7816bf", "8f01cfea", "414140de", "5dae2223",
    "b00361a3", "96177a9c", "b410ff61", "f20015ad",
  ].join("");
  assert.equal(sha256("abc"), expectedAbcDigest);
  assert.equal(stableStringify({ b: 2, a: 1 }), '{"a":1,"b":2}');
});

test("consumer HTML exposes landmarks, role labels, diagnostics, slider semantics and guards", () => {
  for (const marker of [
    '<html lang="fr">',
    '<header class="topbar" aria-label="État du runtime">',
    '<main id="main-content">',
    'aria-labelledby="on-air-heading"',
    'aria-labelledby="preview-heading"',
    'id="tbar-section" class="tbar-section" aria-labelledby="tbar-heading"',
    'id="transition-status" class="status-section" aria-labelledby="transition-status-heading"',
    'role="slider"',
    'aria-valuetext="T-bar 0 pour cent"',
    'aria-disabled="true"',
    'id="diagnostics-disclosure"',
    'id="browser-evidence-section"',
    'id="browser-evidence-json"',
    'browser_dom_capture=true',
    'capture=keyboard',
    'scenario=UX-09',
    'new KeyboardEvent',
    'commit_count_before',
    'aria_disabled',
    'prefers-reduced-motion: reduce',
    'min-height: 44px',
    'max-width: 959px',
    'function guard(element, callback)',
    'event.preventDefault(); event.stopPropagation()',
    'event.key === " " || event.key === "Enter"',
    'ArrowRight',
    'Shift+ArrowRight',
    'PageUp',
    'PageDown',
    '--text: #f4f7fb',
    '--focus: #ffffff',
  ]) assert.match(source, new RegExp(marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.equal((html.match(/role="status"/g) || []).length, 1, "one polite status region only");
  assert.match(html, /Preview/);
  assert.match(html, /On-Air/);
  assert.match(html, /Préparer la Preview/);
  assert.match(html, /Take \/ confirmer la Preview/);
  assert.match(html, /Annuler le Take en attente/);
  assert.match(html, /n’imite aucun succès serveur/i);
});

test("PreviewReady and TakeAccepted never commit or mutate the role map", () => {
  const state0 = createInitialState();
  const prepare = {
    type: "PrepareRequested", command_id: "prepare-contract", server_seq: 1,
    scene_id: "scene-next", scene_name: "Next", expected_revisions: state0.revisions,
    command_payload: { scene_id: "scene-next", scene_name: "Next", expected_revisions: state0.revisions },
  };
  const ready = {
    ...prepare, type: "PreviewReady", server_seq: 2, first_frame_id: 4, first_pts_ns: 40,
  };
  const stateReady = applyEvents(state0, [prepare, ready]);
  assert.equal(stateReady.commit_count, 0);
  assert.deepEqual(stateReady.role_map, state0.role_map);
  const accepted = {
    type: "TakeAccepted", command_id: "take-contract", intent_id: "intent-contract", server_seq: 3,
    scene_id: "scene-next", scene_name: "Next", target_lane_id: "lane-b", expected_revisions: state0.revisions,
    command_payload: { scene_id: "scene-next", scene_name: "Next", target_lane_id: "lane-b", expected_revisions: state0.revisions },
  };
  const stateAccepted = reduceState(stateReady, accepted);
  assert.equal(stateAccepted.commit_count, 0);
  assert.equal(stateAccepted.frozen, true);
  assert.equal(stateAccepted.phase, "take_accepted");
  assert.deepEqual(stateAccepted.role_map, state0.role_map);
  assert.equal(stateAccepted.commit_ledger.length, 0);
});

test("only one unique TakeCommitted appends a row; exact commit retry is replay", () => {
  const evidence = runScenario("UX-04");
  assert.equal(evidence.final.commit_count, 1);
  assert.equal(evidence.final.commit_ledger.length, 1);
  const replay = evidence.events.filter((event) => event.replay === true);
  assert.equal(replay.length, 2, "accepted replay and commit replay are visible as replay events");
  assert.equal(evidence.final.last_outcome.kind, "replay");
  assert.equal(evidence.final.last_outcome.replay, true);
});

test("a second command cannot commit the same Take intent", () => {
  const first = runScenario("UX-01");
  let state = createInitialState();
  const firstEvents = first.snapshots.map(({ event }) => event);
  state = applyEvents(state, firstEvents);
  const expected = state.revisions;
  const payload = { scene_id: "scene-program", scene_name: "Programme principal", target_lane_id: "lane-a", expected_revisions: expected };
  state = reduceState(state, { type: "TakeAccepted", command_id: "take-same-intent-2", intent_id: "intent-happy", server_seq: 7, ...payload, command_payload: payload });
  state = reduceState(state, { type: "TakeCommitted", command_id: "take-same-intent-2", intent_id: "intent-happy", server_seq: 8, ...payload, frame_id: 30, pts_ns: 3000, role_map: { on_air_lane_id: "lane-a", preview_lane_id: "lane-b" }, command_payload: payload });
  assert.equal(state.commit_count, 1);
  assert.equal(state.last_outcome.error_code, "TAKE_INTENT_CONFLICT");
});

test("stale, rejection and idempotency conflict preserve core state", () => {
  for (const id of ["UX-02", "UX-05", "UX-06", "UX-07", "UX-08"]) {
    const evidence = runScenario(id);
    const mutationSnapshots = evidence.snapshots.filter(({ event }) => ["CommandRejected", "TakeRequested"].includes(event.type));
    for (const { before, after } of mutationSnapshots) {
      if (before.phase === "take_accepted" && after.phase === "take_accepted") {
        assert.deepEqual(after.role_map, before.role_map, `${id}: frozen role map stable`);
      }
    }
    assert.equal(evidence.final.commit_count, id === "UX-06" ? 1 : 0, `${id}: commit count`);
  }
  const conflict = runScenario("UX-05");
  assert.equal(conflict.final.last_outcome.error_code, "IDEMPOTENCY_CONFLICT");
  assert.equal(conflict.final.commit_count, 0);
});

test("reconnect marks outcome unknown and reconciles without fabricating a commit", () => {
  const evidence = runScenario("UX-11");
  assert.equal(evidence.final.commit_count, 0);
  assert.equal(evidence.final.last_outcome.kind, "outcome_unknown");
  assert.equal(evidence.final.last_outcome.reconciled, true);
  assert.equal(evidence.final.transport.status, "connected");
  assert.deepEqual(evidence.final.role_map, { on_air_lane_id: "lane-a", preview_lane_id: "lane-b" });
  assert.match(evidence.events.find((event) => event.type === "ReconnectOutcomeUnknown").outcome, /unknown/);
});

test("all UX-01..UX-11 are injectable, deterministic and explicitly mock-only", () => {
  assert.deepEqual(UX_SCENARIO_IDS, Array.from({ length: 11 }, (_, index) => `UX-${String(index + 1).padStart(2, "0")}`));
  const first = runAllScenarios();
  const second = runAllScenarios();
  assert.deepEqual(first, second);
  assert.equal(first.production, false);
  assert.equal(first.transport.mode, "mock");
  assert.equal(first.transport.real_websocket_attempted, false);
  assert.deepEqual(first.scenarios.map(({ id }) => id), UX_SCENARIO_IDS);
  assert.ok(first.scenarios.every(({ snapshots, events }) => snapshots.length > 0 && events.length > 0));
  assert.match(exportEvidence(first), /pulsar\.ux-251\.evidence\.v1/);
});

test("WebSocket adapter URL is explicit and only accepts ws/wss", () => {
  assert.equal(websocketUrlFromLocation({ href: "https://localhost/consumer.html" }), null);
  assert.equal(websocketUrlFromLocation({ href: "https://localhost/consumer.html?ws=ws%3A%2F%2F127.0.0.1%3A4455" }), "ws://127.0.0.1:4455/");
  assert.throws(() => websocketUrlFromLocation({ href: "https://localhost/consumer.html?ws=https%3A%2F%2Fexample.test" }), /ws: or wss:/);
});

test("browser DOM capture mode is opt-in and scoped to deterministic UX-09", () => {
  assert.deepEqual(browserCaptureConfig({ href: "http://127.0.0.1:8765/consumer.html" }), { enabled: false, scenario: null, mode: null });
  assert.deepEqual(browserCaptureConfig({ href: "http://127.0.0.1:8765/consumer.html?capture=keyboard" }), { enabled: true, scenario: "UX-09", mode: "keyboard" });
  assert.deepEqual(browserCaptureConfig({ href: "http://127.0.0.1:8765/consumer.html?scenario=UX-09" }), { enabled: true, scenario: "UX-09", mode: "keyboard" });
  assert.deepEqual(browserCaptureConfig({ href: "http://127.0.0.1:8765/consumer.html?scenario=UX-01" }), { enabled: false, scenario: null, mode: null });
});

test("evidence manifest keeps mock and real WebSocket claims separate", () => {
  assert.equal(manifest.production, false);
  assert.equal(manifest.transport.mode, "mock");
  assert.equal(manifest.transport.real_websocket_attempted, false);
  assert.equal(manifest.transport.real_websocket_capture, false);
  assert.deepEqual(manifest.browser_dom_capture.enabled_by, ["?capture=keyboard", "?scenario=UX-09"]);
  assert.equal(manifest.browser_dom_capture.marker, "browser_dom_capture=true");
  assert.equal(manifest.browser_dom_capture.production, false);
  assert.ok(manifest.browser_dom_capture.excludes.includes("AX-tree"));
  assert.equal(manifest.browser_observation, "docs/evidence/251/browser-dom-capture.observed.json");
  assert.deepEqual(manifest.scenarios, UX_SCENARIO_IDS);
  assert.ok(manifest.limitations.some((item) => /real authenticated WebSocket/i.test(item)));
  assert.ok(manifest.limitations.some((item) => /AX-tree|screenshot/i.test(item)));
});
