/**
 * Non-production, dependency-free consumer harness for Pulsar issue #251.
 *
 * The module is deliberately usable from both Node (deterministic evidence and
 * tests) and a browser (consumer.html). It never invents a commit: only a
 * TakeCommitted event can promote Preview to On-Air or append to the ledger.
 */

const TEXT_ENCODER = new TextEncoder();

function sortedValue(value) {
  if (Array.isArray(value)) return value.map(sortedValue);
  if (value && typeof value === "object") {
    return Object.keys(value)
      .sort()
      .reduce((result, key) => {
        result[key] = sortedValue(value[key]);
        return result;
      }, {});
  }
  return value;
}

export function stableStringify(value) {
  return JSON.stringify(sortedValue(value));
}

// Small portable SHA-256 implementation. Keeping the digest local means the
// reducer has the same idempotency key in Node and in a browser, with no secret
// or runtime-specific crypto dependency.
const SHA256_K = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b,
  0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01,
  0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7,
  0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
  0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152,
  0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
  0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819,
  0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08,
  0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f,
  0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

const rotr = (value, bits) => (value >>> bits) | (value << (32 - bits));

export function sha256(value) {
  const bytes = TEXT_ENCODER.encode(String(value));
  const bitLength = bytes.length * 8;
  const paddedLength = (((bytes.length + 9) + 63) >> 6) << 6;
  const data = new Uint8Array(paddedLength);
  data.set(bytes);
  data[bytes.length] = 0x80;
  const lengthOffset = paddedLength - 8;
  for (let index = 0; index < 8; index += 1) {
    data[lengthOffset + index] = Math.floor(bitLength / 2 ** (56 - index * 8)) & 0xff;
  }

  let h0 = 0x6a09e667;
  let h1 = 0xbb67ae85;
  let h2 = 0x3c6ef372;
  let h3 = 0xa54ff53a;
  let h4 = 0x510e527f;
  let h5 = 0x9b05688c;
  let h6 = 0x1f83d9ab;
  let h7 = 0x5be0cd19;

  for (let offset = 0; offset < data.length; offset += 64) {
    const words = new Uint32Array(64);
    for (let index = 0; index < 16; index += 1) {
      const cursor = offset + index * 4;
      words[index] = (data[cursor] << 24) | (data[cursor + 1] << 16) |
        (data[cursor + 2] << 8) | data[cursor + 3];
    }
    for (let index = 16; index < 64; index += 1) {
      const s0 = rotr(words[index - 15], 7) ^ rotr(words[index - 15], 18) ^ (words[index - 15] >>> 3);
      const s1 = rotr(words[index - 2], 17) ^ rotr(words[index - 2], 19) ^ (words[index - 2] >>> 10);
      words[index] = (words[index - 16] + s0 + words[index - 7] + s1) >>> 0;
    }

    let a = h0;
    let b = h1;
    let c = h2;
    let d = h3;
    let e = h4;
    let f = h5;
    let g = h6;
    let h = h7;
    for (let index = 0; index < 64; index += 1) {
      const sigma1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const choice = (e & f) ^ (~e & g);
      const temp1 = (h + sigma1 + choice + SHA256_K[index] + words[index]) >>> 0;
      const sigma0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (sigma0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
    h5 = (h5 + f) >>> 0;
    h6 = (h6 + g) >>> 0;
    h7 = (h7 + h) >>> 0;
  }

  return [h0, h1, h2, h3, h4, h5, h6, h7]
    .map((word) => word.toString(16).padStart(8, "0"))
    .join("");
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

const DEFAULT_ROLE_MAP = {
  on_air_lane_id: "lane-a",
  preview_lane_id: "lane-b",
};

const DEFAULT_ON_AIR = {
  scene_id: "scene-program",
  scene_name: "Programme principal",
  frame_id: 1,
  pts_ns: 0,
};

export function createInitialState(overrides = {}) {
  const initial = {
    runtime_instance_id: "runtime-ux-251-mock",
    phase: "ready",
    operational: true,
    frozen: false,
    tbar: 0,
    on_air: clone(DEFAULT_ON_AIR),
    preview: {
      scene_id: null,
      scene_name: null,
      ready: false,
      frame_id: null,
      pts_ns: null,
    },
    role_map: clone(DEFAULT_ROLE_MAP),
    revisions: { program: 0, preview: 0, role_map: 0 },
    server_seq: 0,
    pending: null,
    unknown_command_id: null,
    command_records: {},
    seen_events: {},
    committed_commands: {},
    committed_intents: {},
    commit_ledger: [],
    commit_count: 0,
    events: [],
    announcements: [],
    last_outcome: null,
    diagnostics: {
      error_code: null,
      error_message: null,
      recovery: null,
    },
    transport: {
      mode: "mock",
      status: "feed",
      real_websocket_attempted: false,
    },
  };
  const merged = { ...initial, ...clone(overrides) };
  merged.role_map = { ...DEFAULT_ROLE_MAP, ...(overrides.role_map || {}) };
  merged.on_air = { ...DEFAULT_ON_AIR, ...(overrides.on_air || {}) };
  merged.revisions = { ...initial.revisions, ...(overrides.revisions || {}) };
  merged.transport = { ...initial.transport, ...(overrides.transport || {}) };
  return merged;
}

export function stateProjection(state) {
  return {
    role_map: clone(state.role_map),
    on_air: clone(state.on_air),
    preview: clone(state.preview),
    revisions: clone(state.revisions),
    frozen: state.frozen,
    pending: clone(state.pending),
    commit_count: state.commit_count,
    commit_ledger: clone(state.commit_ledger),
  };
}

export function snapshotState(state) {
  return {
    phase: state.phase,
    operational: state.operational,
    frozen: state.frozen,
    tbar: state.tbar,
    on_air: clone(state.on_air),
    preview: clone(state.preview),
    role_map: clone(state.role_map),
    revisions: clone(state.revisions),
    server_seq: state.server_seq,
    pending: state.pending ? {
      command_id: state.pending.command_id,
      intent_id: state.pending.intent_id,
      stage: state.pending.stage,
    } : null,
    commit_count: state.commit_count,
    commit_ledger: clone(state.commit_ledger),
    last_outcome: clone(state.last_outcome),
    diagnostics: clone(state.diagnostics),
    transport: clone(state.transport),
  };
}

function commandPayload(event) {
  if (event.command_payload !== undefined) return clone(event.command_payload);
  if (event.payload !== undefined) return clone(event.payload);
  return {
    intent_id: event.intent_id ?? null,
    scene_id: event.scene_id ?? null,
    scene_name: event.scene_name ?? null,
    target_lane_id: event.target_lane_id ?? null,
    expected_revisions: event.expected_revisions ?? null,
  };
}

function commandKey(state, event) {
  return `${event.runtime_instance_id || state.runtime_instance_id}|${event.command_id || "no-command"}`;
}

function eventKey(state, event) {
  return `${event.runtime_instance_id || state.runtime_instance_id}|${event.command_id || "no-command"}|${event.type}|${event.server_seq ?? "no-seq"}`;
}

function publicEvent(event, extra = {}) {
  const fields = [
    "type", "runtime_instance_id", "command_id", "intent_id", "server_seq",
    "target_lane_id", "frame_id", "pts_ns", "first_frame_id", "first_pts_ns",
    "error_code", "reason", "state", "replay", "outcome",
  ];
  return {
    ...fields.reduce((result, field) => {
      if (event[field] !== undefined) result[field] = event[field];
      return result;
    }, {}),
    ...extra,
  };
}

function recordEvent(state, event, extra = {}) {
  const next = state;
  const seq = Number.isInteger(event.server_seq) ? event.server_seq : next.server_seq;
  next.server_seq = Math.max(next.server_seq, seq);
  const key = eventKey(next, event);
  next.seen_events[key] = true;
  next.events.push(publicEvent(event, extra));
}

function expectedRevisionsMatch(state, event) {
  if (!event.expected_revisions) return true;
  return ["program", "preview", "role_map"].every((key) => {
    const expected = event.expected_revisions[key];
    return expected === undefined || expected === state.revisions[key];
  });
}

function setDiagnostics(state, code, message, recovery, event) {
  state.diagnostics = {
    error_code: code || null,
    error_message: message || null,
    recovery: recovery || null,
  };
  state.last_outcome = {
    kind: "rejected",
    replay: false,
    error_code: code,
    message,
    recovery,
    command_id: event.command_id || null,
  };
}

function reject(state, event, code, message, recovery) {
  const next = clone(state);
  const activePending = Boolean(next.pending && next.pending.stage === "accepted");
  if (!activePending) next.phase = code === "OUTCOME_UNKNOWN" ? "outcome_unknown" : "rejected";
  setDiagnostics(next, code, message, recovery, event);
  recordEvent(next, event, { outcome: "rejected" });
  return next;
}

function rememberOutcome(state, event, responseEvent) {
  if (!event.command_id) return;
  const key = commandKey(state, event);
  const existing = state.command_records[key] || {
    payload_sha256: sha256(stableStringify(commandPayload(event))),
    outcomes: {},
  };
  existing.outcomes[event.type] = clone(responseEvent || publicEvent(event));
  state.command_records[key] = existing;
}

function dedupeCommand(state, event) {
  if (!event.command_id) return { state, replay: false, conflict: false };
  const key = commandKey(state, event);
  const digest = sha256(stableStringify(commandPayload(event)));
  const existing = state.command_records[key];
  if (!existing) return { state, replay: false, conflict: false, key, digest };
  if (existing.payload_sha256 !== digest) {
    return { state: reject(state, event, "IDEMPOTENCY_CONFLICT", "This command ID was already used for a different action", "Issue a new command ID after reviewing state"), replay: false, conflict: true };
  }
  const previous = existing.outcomes[event.type];
  if (!previous) return { state, replay: false, conflict: false, key, digest };
  const next = clone(state);
  next.last_outcome = {
    kind: "replay",
    replay: true,
    command_id: event.command_id,
    response: clone(previous),
    message: "Déjà appliqué — résultat original affiché",
  };
  next.diagnostics = {
    error_code: null,
    error_message: null,
    recovery: null,
  };
  recordEvent(next, event, { replay: true, outcome: "replay" });
  return { state: next, replay: true, conflict: false, key, digest };
}

function checkSequence(state, event) {
  if (!Number.isInteger(event.server_seq)) return null;
  if (event.server_seq <= state.server_seq + 1) return null;
  const next = clone(state);
  next.phase = "outcome_unknown";
  next.frozen = true;
  next.pending = null;
  next.unknown_command_id = event.command_id || state.pending?.command_id || null;
  next.transport.status = "reconnecting";
  next.last_outcome = {
    kind: "outcome_unknown",
    replay: false,
    error_code: "OUTCOME_UNKNOWN",
    command_id: next.unknown_command_id,
    message: "Reconnecting — outcome not yet confirmed",
    recovery: "Reconcile with GetState before enabling Take",
  };
  recordEvent(next, event, { outcome: "outcome_unknown" });
  return next;
}

export function reduceState(inputState, inputEvent) {
  const state = clone(inputState);
  const event = clone(inputEvent);
  event.runtime_instance_id = event.runtime_instance_id || state.runtime_instance_id;

  const key = eventKey(state, event);
  if (state.seen_events[key]) return inputState;

  const sequenced = checkSequence(state, event);
  if (sequenced) return sequenced;

  const command = dedupeCommand(state, event);
  if (command.conflict || command.replay) return command.state;
  let next = command.state;

  switch (event.type) {
    case "TransportConnected": {
      next.transport.status = "connected";
      next.transport.mode = event.mode || next.transport.mode;
      recordEvent(next, event);
      return next;
    }
    case "TransportError": {
      next.transport.status = "error";
      next.last_outcome = {
        kind: "transport_error",
        replay: false,
        message: event.message || "WebSocket transport error",
        recovery: "Check the explicit WebSocket URL and reconnect",
      };
      recordEvent(next, event, { outcome: "transport_error" });
      return next;
    }
    case "TBarChanged": {
      if (next.frozen || next.phase === "outcome_unknown" || next.operational === false) {
        return reject(next, event, "PREVIEW_FROZEN", "Preview verrouillée pendant un Take en attente", "Attendez Commit/Abort ou activez Annuler le Take en attente");
      }
      next.tbar = Math.max(0, Math.min(100, Number(event.value) || 0));
      recordEvent(next, event);
      return next;
    }
    case "PrepareRequested": {
      if (next.frozen || next.phase === "outcome_unknown" || next.operational === false) {
        return reject(next, event, "PREVIEW_FROZEN", "Preview verrouillée pendant un Take en attente", "Attendez Commit/Abort ou activez Annuler le Take en attente");
      }
      if (!expectedRevisionsMatch(next, event)) {
        return reject(next, event, "REVISION_STALE", "L’état a changé — votre action n’a pas été appliquée", "Actualisez GetState et préparez à nouveau");
      }
      next.phase = "preparing";
      next.preview = { scene_id: event.scene_id || null, scene_name: event.scene_name || null, ready: false, frame_id: null, pts_ns: null };
      next.last_outcome = { kind: "accepted", replay: false, type: "PrepareRequested", command_id: event.command_id || null };
      next.diagnostics = { error_code: null, error_message: null, recovery: null };
      recordEvent(next, event, { outcome: "accepted" });
      rememberOutcome(next, event, publicEvent(event, { outcome: "accepted" }));
      return next;
    }
    case "PrepareAccepted": {
      if (next.frozen || next.phase === "outcome_unknown" || next.operational === false) {
        return reject(next, event, "PREVIEW_FROZEN", "Preview verrouillée pendant un Take en attente", "Attendez Commit/Abort ou activez Annuler le Take en attente");
      }
      if (!expectedRevisionsMatch(next, event)) {
        return reject(next, event, "REVISION_STALE", "L’état a changé — votre action n’a pas été appliquée", "Actualisez GetState et préparez à nouveau");
      }
      next.phase = "preparing";
      if (event.scene_id || event.scene_name) {
        next.preview.scene_id = event.scene_id || next.preview.scene_id;
        next.preview.scene_name = event.scene_name || next.preview.scene_name;
      }
      next.last_outcome = { kind: "accepted", replay: false, type: "PrepareAccepted", command_id: event.command_id || null };
      next.diagnostics = { error_code: null, error_message: null, recovery: null };
      recordEvent(next, event, { outcome: "accepted" });
      rememberOutcome(next, event, publicEvent(event, { outcome: "accepted" }));
      return next;
    }
    case "PreviewReady": {
      if (!expectedRevisionsMatch(next, event)) {
        return reject(next, event, "REVISION_STALE", "L’état a changé — votre action n’a pas été appliquée", "Actualisez GetState et préparez à nouveau");
      }
      next.preview.ready = true;
      next.preview.frame_id = event.first_frame_id ?? event.frame_id ?? null;
      next.preview.pts_ns = event.first_pts_ns ?? event.pts_ns ?? null;
      next.preview.scene_id = event.scene_id || next.preview.scene_id;
      next.preview.scene_name = event.scene_name || next.preview.scene_name;
      next.phase = "preview_ready";
      next.last_outcome = { kind: "preview_ready", replay: false, command_id: event.command_id || null };
      next.diagnostics = { error_code: null, error_message: null, recovery: null };
      recordEvent(next, event, { outcome: "preview_ready" });
      rememberOutcome(next, event, publicEvent(event, { outcome: "preview_ready" }));
      return next;
    }
    case "TakeRequested": {
      if (next.frozen || next.phase === "outcome_unknown" || next.operational === false) {
        return reject(next, event, "PREVIEW_FROZEN", "Preview verrouillée pendant un Take en attente", "Attendez Commit/Abort ou activez Annuler le Take en attente");
      }
      if (!expectedRevisionsMatch(next, event)) {
        return reject(next, event, "REVISION_STALE", "L’état a changé — votre action n’a pas été appliquée", "Actualisez GetState et préparez à nouveau");
      }
      if (!next.preview.ready) {
        return reject(next, event, "PREVIEW_NOT_READY", "La Preview n’a pas rendu sa première frame", "Attendez PreviewReady ou préparez à nouveau");
      }
      next.phase = "take_requested";
      next.pending = {
        command_id: event.command_id || null,
        intent_id: event.intent_id || null,
        stage: "requested",
        target_scene_id: event.scene_id || next.preview.scene_id,
        target_scene_name: event.scene_name || next.preview.scene_name,
        target_lane_id: event.target_lane_id || next.role_map.preview_lane_id,
        expected_revisions: clone(event.expected_revisions || next.revisions),
      };
      next.last_outcome = { kind: "requested", replay: false, command_id: event.command_id || null };
      next.diagnostics = { error_code: null, error_message: null, recovery: null };
      recordEvent(next, event, { outcome: "requested" });
      rememberOutcome(next, event, publicEvent(event, { outcome: "requested" }));
      return next;
    }
    case "TakeAccepted": {
      if (next.frozen && next.pending?.command_id !== event.command_id) {
        return reject(next, event, "PREVIEW_FROZEN", "Preview verrouillée pendant un Take en attente", "Attendez Commit/Abort ou activez Annuler le Take en attente");
      }
      if (!expectedRevisionsMatch(next, event)) {
        return reject(next, event, "REVISION_STALE", "L’état a changé — votre action n’a pas été appliquée", "Actualisez GetState et préparez à nouveau");
      }
      const pending = next.pending || {
        command_id: event.command_id || null,
        intent_id: event.intent_id || null,
        target_scene_id: event.scene_id || next.preview.scene_id,
        target_scene_name: event.scene_name || next.preview.scene_name,
        target_lane_id: event.target_lane_id || next.role_map.preview_lane_id,
        expected_revisions: clone(event.expected_revisions || next.revisions),
      };
      if (pending.command_id && event.command_id && pending.command_id !== event.command_id) {
        return reject(next, event, "TAKE_INTENT_CONFLICT", "Cette action appartient à une autre intention de Take", "Abandonnez les contrôles obsolètes et préparez une nouvelle intention");
      }
      pending.stage = "accepted";
      pending.command_id = event.command_id || pending.command_id;
      pending.intent_id = event.intent_id || pending.intent_id;
      pending.freeze_until_monotonic_ns = event.freeze_until_monotonic_ns ?? null;
      next.pending = pending;
      next.phase = "take_accepted";
      next.frozen = true;
      next.last_outcome = { kind: "accepted", replay: false, type: "TakeAccepted", command_id: event.command_id || null };
      next.diagnostics = { error_code: null, error_message: null, recovery: "Wait for TakeCommitted or activate Cancel pending Take" };
      recordEvent(next, event, { outcome: "accepted" });
      rememberOutcome(next, event, publicEvent(event, { outcome: "accepted" }));
      return next;
    }
    case "TakeCommitted": {
      if (!next.pending || next.pending.stage !== "accepted" || (event.command_id && next.pending.command_id !== event.command_id)) {
        return reject(next, event, "TAKE_NOT_PENDING", "Ce Take n’est plus en attente", "Réconciliez le flux d’événements et GetState avant de réessayer");
      }
      if (event.intent_id && next.committed_intents[event.intent_id]) {
        return reject(next, event, "TAKE_INTENT_CONFLICT", "Cette intention de Take a déjà été engagée", "Abandonnez le contrôle obsolète et préparez une nouvelle intention");
      }
      const previousOnAir = clone(next.on_air);
      const committedScene = {
        scene_id: event.scene_id || event.target_scene_id || next.pending.target_scene_id,
        scene_name: event.scene_name || event.target_scene_name || next.pending.target_scene_name,
        frame_id: event.frame_id ?? null,
        pts_ns: event.pts_ns ?? null,
      };
      next.on_air = committedScene;
      next.preview = {
        scene_id: previousOnAir.scene_id,
        scene_name: previousOnAir.scene_name,
        ready: false,
        frame_id: null,
        pts_ns: null,
      };
      next.role_map = clone(event.role_map || {
        on_air_lane_id: event.target_lane_id || next.pending.target_lane_id,
        preview_lane_id: next.role_map.on_air_lane_id,
      });
      next.revisions = {
        program: event.revisions_after?.program ?? next.revisions.program + 1,
        preview: event.revisions_after?.preview ?? next.revisions.preview + 1,
        role_map: event.revisions_after?.role_map ?? next.revisions.role_map + 1,
      };
      const ledgerRow = {
        command_id: event.command_id || next.pending.command_id,
        intent_id: event.intent_id || next.pending.intent_id,
        target_lane_id: event.target_lane_id || next.pending.target_lane_id,
        frame_id: committedScene.frame_id,
        pts_ns: committedScene.pts_ns,
        server_seq: event.server_seq ?? next.server_seq,
        replay: false,
      };
      next.commit_ledger.push(ledgerRow);
      next.commit_count += 1;
      next.committed_commands[ledgerRow.command_id] = true;
      if (ledgerRow.intent_id) next.committed_intents[ledgerRow.intent_id] = true;
      next.pending = null;
      next.frozen = false;
      next.phase = "ready";
      next.tbar = 0;
      next.last_outcome = { kind: "committed", replay: false, ...clone(ledgerRow), message: `Take confirmé. On-Air est maintenant ${committedScene.scene_name}.` };
      next.diagnostics = { error_code: null, error_message: null, recovery: null };
      if (!next.announcements.includes(ledgerRow.command_id)) next.announcements.push(ledgerRow.command_id);
      recordEvent(next, event, { outcome: "committed", replay: false });
      rememberOutcome(next, event, publicEvent(event, { outcome: "committed", replay: false }));
      return next;
    }
    case "TakeAborted": {
      if (!next.pending || next.pending.stage !== "accepted" || (event.command_id && next.pending.command_id !== event.command_id)) {
        return reject(next, event, "TAKE_NOT_PENDING", "Ce Take n’est plus en attente", "Réconciliez le flux d’événements et GetState avant de réessayer");
      }
      const commandId = next.pending.command_id;
      next.pending = null;
      next.frozen = false;
      next.phase = "ready";
      next.last_outcome = {
        kind: "aborted",
        replay: false,
        command_id: commandId,
        reason: event.reason || "operator",
        message: event.reason === "timeout" ? "Take expiré — On-Air inchangé" : "Take annulé — On-Air inchangé",
      };
      next.diagnostics = { error_code: null, error_message: null, recovery: "Préparez à nouveau avec l’état courant" };
      recordEvent(next, event, { outcome: "aborted" });
      rememberOutcome(next, event, publicEvent(event, { outcome: "aborted" }));
      return next;
    }
    case "CommandRejected": {
      const messages = {
        REVISION_STALE: ["L’état a changé — votre action n’a pas été appliquée", "Actualisez GetState et préparez à nouveau"],
        SERVER_SEQ_STALE: ["L’état a changé — votre action n’a pas été appliquée", "Actualisez GetState et préparez à nouveau"],
        IDEMPOTENCY_CONFLICT: ["Cet identifiant de commande a déjà servi pour une autre action", "Émettez un nouvel identifiant après vérification de l’état"],
        PREVIEW_FROZEN: ["Preview verrouillée pendant un Take en attente", "Attendez Commit/Abort ou activez Annuler le Take en attente"],
        PREVIEW_NOT_READY: ["La Preview n’a pas rendu sa première frame", "Attendez PreviewReady ou préparez à nouveau"],
        PREPARE_NOT_FOUND: ["Cette Preview n’est plus préparée", "Préparez à nouveau avec les dernières révisions"],
        TAKE_NOT_PENDING: ["Ce Take n’est plus en attente", "Réconciliez le flux d’événements et GetState"],
        TAKE_INTENT_CONFLICT: ["Cette action appartient à une autre intention de Take", "Abandonnez les contrôles obsolètes et préparez une nouvelle intention"],
        RUNTIME_MISMATCH: ["Cette commande n’est pas valide pour ce runtime", "Reconnectez-vous au runtime correspondant"],
        SCHEMA_INVALID: ["Cette commande n’est pas valide pour ce runtime", "Ouvrez Diagnostics et corrigez la requête"],
        TIMEOUT: ["La Preview n’a pas rendu à temps. Aucun output n’a changé.", "Réessayez Préparer avec des révisions actualisées"],
      };
      const [message, recovery] = messages[event.error_code] || ["Action not applied", "Refresh GetState before retrying"];
      return reject(next, event, event.error_code || "REJECTED", event.message || message, event.recovery || recovery);
    }
    case "ReconnectOutcomeUnknown": {
      const pendingId = next.pending?.command_id || event.command_id || null;
      next.phase = "outcome_unknown";
      next.frozen = true;
      next.pending = null;
      next.unknown_command_id = pendingId;
      next.transport.status = "reconnecting";
      next.last_outcome = {
        kind: "outcome_unknown",
        replay: false,
        error_code: "OUTCOME_UNKNOWN",
        command_id: pendingId,
        message: "Résultat inconnu — reconnexion avant de réactiver le Take",
        recovery: "Récupérez GetState ; n’inférez jamais un commit d’un timeout",
      };
      next.diagnostics = { error_code: "OUTCOME_UNKNOWN", error_message: "Résultat inconnu — reconnexion", recovery: "Récupérez GetState avant d’autoriser une mutation" };
      recordEvent(next, event, { outcome: "outcome_unknown" });
      return next;
    }
    case "StateReconciled": {
      next.transport.status = "connected";
      next.frozen = false;
      next.pending = null;
      next.phase = "ready";
      next.last_outcome = {
        kind: "outcome_unknown",
        replay: false,
        reconciled: true,
        command_id: next.unknown_command_id,
        message: "Résultat inconnu — réconcilié depuis le runtime",
        recovery: "Préparez une nouvelle Preview avant le Take",
      };
      next.diagnostics = { error_code: "OUTCOME_UNKNOWN", error_message: "Résultat inconnu — réconcilié depuis le runtime", recovery: "Préparez une nouvelle Preview avant le Take" };
      recordEvent(next, event, { outcome: "reconciled" });
      return next;
    }
    default:
      return reject(next, event, "SCHEMA_INVALID", "Cette commande n’est pas valide pour ce runtime", "Ouvrez Diagnostics et corrigez la requête");
  }
}

export function applyEvents(initialState, events) {
  return events.reduce((state, event) => reduceState(state, event), initialState);
}

function transitionEvent(type, commandId, fields = {}) {
  return {
    type,
    runtime_instance_id: "runtime-ux-251-mock",
    command_id: commandId,
    server_seq: fields.server_seq,
    ...fields,
  };
}

const revisions0 = { program: 0, preview: 0, role_map: 0 };

function prepareEvents(suffix, sceneName = "Lower third", startSeq = 1) {
  const command = `prepare-${suffix}`;
  const payload = { scene_id: `scene-${suffix}`, scene_name: sceneName, expected_revisions: revisions0 };
  return [
    transitionEvent("PrepareRequested", command, { server_seq: startSeq, ...payload, command_payload: payload }),
    transitionEvent("PrepareAccepted", command, { server_seq: startSeq + 1, ...payload, command_payload: payload }),
    transitionEvent("PreviewReady", command, { server_seq: startSeq + 2, ...payload, first_frame_id: 10, first_pts_ns: 1000, command_payload: payload }),
  ];
}

function takeEvents(suffix, sceneName = "Lower third", startSeq = 4) {
  const command = `take-${suffix}`;
  const payload = { scene_id: `scene-${suffix}`, scene_name: sceneName, target_lane_id: "lane-b", expected_revisions: revisions0 };
  return [
    transitionEvent("TakeRequested", command, { server_seq: startSeq, ...payload, intent_id: `intent-${suffix}`, command_payload: payload }),
    transitionEvent("TakeAccepted", command, { server_seq: startSeq + 1, ...payload, intent_id: `intent-${suffix}`, command_payload: payload }),
    transitionEvent("TakeCommitted", command, { server_seq: startSeq + 2, ...payload, intent_id: `intent-${suffix}`, frame_id: 20, pts_ns: 2000, role_map: { on_air_lane_id: "lane-b", preview_lane_id: "lane-a" }, command_payload: payload }),
  ];
}

function coreSnapshot(state) {
  return stateProjection(state);
}

function scenario(id, title, events, options = {}) {
  let state = createInitialState(options.initialState || {});
  const snapshots = [];
  for (const event of events) {
    const before = snapshotState(state);
    state = reduceState(state, event);
    snapshots.push({ event: publicEvent(event), before, after: snapshotState(state) });
  }
  return {
    id,
    title,
    transport: { mode: "mock", real_websocket_attempted: false },
    viewport: options.viewport || { width: 1280, height: 720, zoom: 1 },
    interaction: options.interaction || { input: "deterministic-feed" },
    events: state.events,
    snapshots,
    final: snapshotState(state),
    core: coreSnapshot(state),
    assertions: options.assertions || [],
  };
}

export function runScenario(id) {
  switch (id) {
    case "UX-01": {
      const events = [...prepareEvents("happy"), ...takeEvents("happy")];
      return scenario(id, "Happy path: Preview → TakeAccepted → TakeCommitted", events, {
        assertions: ["preview_and_on_air_distinct_before_commit", "one_unique_commit", "role_map_changes_only_at_commit"],
      });
    }
    case "UX-02": {
      const events = [
        transitionEvent("PrepareRequested", "prepare-timeout", { server_seq: 1, scene_id: "scene-timeout", scene_name: "Lower third", expected_revisions: revisions0, command_payload: { scene_id: "scene-timeout", scene_name: "Lower third", expected_revisions: revisions0 } }),
        transitionEvent("PrepareAccepted", "prepare-timeout", { server_seq: 2, scene_id: "scene-timeout", scene_name: "Lower third", expected_revisions: revisions0, command_payload: { scene_id: "scene-timeout", scene_name: "Lower third", expected_revisions: revisions0 } }),
        transitionEvent("CommandRejected", "prepare-timeout", { server_seq: 3, error_code: "TIMEOUT", message: "Preview did not render before the deadline. No output changed.", command_payload: { scene_id: "scene-timeout", scene_name: "Lower third", expected_revisions: revisions0 } }),
      ];
      return scenario(id, "Prepare wait and timeout", events, { assertions: ["take_never_enabled_without_preview_ready", "on_air_unchanged", "retry_requires_new_guards"] });
    }
    case "UX-03": {
      const events = [...prepareEvents("abort"), ...takeEvents("abort").slice(0, 2), transitionEvent("TakeAborted", "take-abort", { server_seq: 6, intent_id: "intent-abort", reason: "operator", command_payload: { scene_id: "scene-abort", scene_name: "Lower third", target_lane_id: "lane-b", expected_revisions: revisions0 } })];
      return scenario(id, "Pending recovery by keyboard-reachable Abort", events, { assertions: ["accepted_freezes_preview_and_tbar", "abort_leaves_roles_and_commit_count", "controls_recover"] });
    }
    case "UX-04": {
      const base = [...prepareEvents("replay"), ...takeEvents("replay").slice(0, 2)];
      const payload = { scene_id: "scene-replay", scene_name: "Lower third", target_lane_id: "lane-b", expected_revisions: revisions0 };
      const events = [
        ...base,
        transitionEvent("TakeAccepted", "take-replay", { server_seq: 6, intent_id: "intent-replay", ...payload, command_payload: payload }),
        transitionEvent("TakeCommitted", "take-replay", { server_seq: 7, intent_id: "intent-replay", ...payload, frame_id: 20, pts_ns: 2000, role_map: { on_air_lane_id: "lane-b", preview_lane_id: "lane-a" }, command_payload: payload }),
        transitionEvent("TakeCommitted", "take-replay", { server_seq: 8, intent_id: "intent-replay", ...payload, frame_id: 20, pts_ns: 2000, role_map: { on_air_lane_id: "lane-b", preview_lane_id: "lane-a" }, command_payload: payload }),
      ];
      return scenario(id, "Exact retry is a replay, not a second commit", events, { assertions: ["replay_response_matches_original", "commit_count_is_one", "announcement_is_once"] });
    }
    case "UX-05": {
      const events = [...prepareEvents("conflict"), ...takeEvents("conflict").slice(0, 2)];
      const beforeConflictPayload = { scene_id: "scene-conflict", scene_name: "Lower third", target_lane_id: "lane-b", expected_revisions: revisions0 };
      events.push(transitionEvent("TakeRequested", "take-conflict", { server_seq: 6, intent_id: "intent-conflict", scene_id: "scene-other", scene_name: "Autre", target_lane_id: "lane-b", expected_revisions: revisions0, command_payload: { scene_id: "scene-other", scene_name: "Autre", target_lane_id: "lane-b", expected_revisions: revisions0 } }));
      // The accepted event above already registered the original payload; keep
      // this named value in the scenario source to make the intended comparison
      // explicit for reviewers and static evidence readers.
      void beforeConflictPayload;
      return scenario(id, "Idempotency conflict", events, { assertions: ["stable_conflict_code", "no_role_or_commit_mutation", "new_command_id_required"] });
    }
    case "UX-06": {
      const events = [...prepareEvents("stale"), ...takeEvents("stale"), transitionEvent("TakeRequested", "take-loser", { server_seq: 7, scene_id: "scene-stale", scene_name: "Stale", target_lane_id: "lane-b", expected_revisions: revisions0, command_payload: { scene_id: "scene-stale", scene_name: "Stale", target_lane_id: "lane-b", expected_revisions: revisions0 } })];
      return scenario(id, "Stale race", events, { assertions: ["winner_commits_once", "loser_is_revision_stale", "no_optimistic_swap"] });
    }
    case "UX-07": {
      const codes = ["PREVIEW_NOT_READY", "PREPARE_NOT_FOUND", "TAKE_NOT_PENDING", "TAKE_INTENT_CONFLICT"];
      const events = codes.map((code, index) => transitionEvent("CommandRejected", `reject-${index}`, { server_seq: index + 1, error_code: code }));
      return scenario(id, "Admission and intent errors", events, { assertions: ["each_code_and_recovery_is_distinct", "no_core_mutation"] });
    }
    case "UX-08": {
      const events = [transitionEvent("TakeRequested", "take-frozen", { server_seq: 1, scene_id: "scene-frozen", scene_name: "Frozen", expected_revisions: revisions0, command_payload: { scene_id: "scene-frozen", scene_name: "Frozen", expected_revisions: revisions0 } })];
      return scenario(id, "Frozen runtime", events, {
        initialState: { phase: "frozen", frozen: true, operational: false, transport: { status: "connected" } },
        assertions: ["all_mutations_disabled_with_reason", "program_remains_visible", "no_retry_loop"],
      });
    }
    case "UX-09": {
      const events = [transitionEvent("TBarChanged", "tbar-keyboard", { server_seq: 1, value: 40 }), ...prepareEvents("keyboard", "Lower third", 2), ...takeEvents("keyboard", "Lower third", 5)];
      return scenario(id, "Keyboard-only path", events, {
        interaction: { input: "keyboard", keys: ["Tab", "ArrowRight", "Shift+ArrowRight", "Home", "End", "PageUp", "PageDown", "Enter"] },
        assertions: ["documented_focus_order", "slider_never_commits", "explicit_take_is_only_commit_affordance", "one_live_announcement"],
      });
    }
    case "UX-10": {
      const longName = "Scène ".padEnd(256, "x");
      const longMessage = "Détail diagnostique ".repeat(64).slice(0, 1024);
      const events = [
        transitionEvent("PrepareRequested", "prepare-long", { server_seq: 1, scene_id: "scene-long", scene_name: longName, expected_revisions: revisions0, command_payload: { scene_id: "scene-long", scene_name: longName, expected_revisions: revisions0 } }),
        transitionEvent("CommandRejected", "reject-long", { server_seq: 2, error_code: "SCHEMA_INVALID", message: longMessage }),
      ];
      return scenario(id, "Narrow view and long content", events, {
        viewport: { widths: [320, 768, 1280], zoom: 1 },
        assertions: ["on_air_first_below_960px", "labels_wrap_without_clipping", "diagnostic_message_is_available_in_details", "44px_targets"],
      });
    }
    case "UX-11": {
      const events = [...prepareEvents("reconnect"), ...takeEvents("reconnect").slice(0, 2), transitionEvent("ReconnectOutcomeUnknown", "take-reconnect", { server_seq: 6, command_payload: { scene_id: "scene-reconnect", scene_name: "Lower third", target_lane_id: "lane-b", expected_revisions: revisions0 } }), transitionEvent("StateReconciled", "reconcile-reconnect", { server_seq: 7, outcome: "unknown", role_map: clone(DEFAULT_ROLE_MAP), revisions: revisions0 })];
      return scenario(id, "Reconnect with outcome unknown", events, { assertions: ["mutation_disabled_until_reconcile", "no_commit_from_timeout", "get_state_reconciliation_is_explicit"] });
    }
    default:
      throw new Error(`Unknown UX scenario: ${id}`);
  }
}

export const UX_SCENARIO_IDS = Object.freeze(["UX-01", "UX-02", "UX-03", "UX-04", "UX-05", "UX-06", "UX-07", "UX-08", "UX-09", "UX-10", "UX-11"]);

/**
 * Opt-in browser DOM capture. It deliberately accepts only the keyboard
 * scenario so a dump cannot be mistaken for a real runtime/AX capture.
 */
export function browserCaptureConfig(locationLike = globalThis.location) {
  if (!locationLike?.href) return { enabled: false, scenario: null, mode: null };
  const params = new URL(locationLike.href).searchParams;
  const keyboardCapture = params.get("capture") === "keyboard";
  const scenarioCapture = params.get("scenario") === "UX-09";
  return {
    enabled: keyboardCapture || scenarioCapture,
    scenario: keyboardCapture || scenarioCapture ? "UX-09" : null,
    mode: keyboardCapture || scenarioCapture ? "keyboard" : null,
  };
}

export function runAllScenarios() {
  return {
    schema: "pulsar.ux-251.evidence.v1",
    issue: "ZabLaboratory/Pulsar#251",
    adr: "ADR-PULSAR-DUAL-LANE-001@draft-r2-dual-lane-20260828",
    production: false,
    transport: {
      mode: "mock",
      real_websocket_attempted: false,
      statement: "Deterministic local feed only; no authenticated or real WebSocket claim.",
    },
    scenarios: UX_SCENARIO_IDS.map(runScenario),
    limitations: [
      "No real authenticated WebSocket capture is included.",
      "The repository build is headless; browser AX-tree and screenshot capture require a separate browser run.",
      "Mock events prove consumer state semantics, not libobs or deployed runtime behavior.",
    ],
  };
}

export function redactWebSocketUrl(rawUrl) {
  if (!rawUrl) return null;
  try {
    const url = new URL(rawUrl);
    url.username = "";
    url.password = "";
    for (const key of [...url.searchParams.keys()]) {
      if (/token|password|secret|key|auth/i.test(key)) url.searchParams.set(key, "[redacted]");
    }
    return url.toString();
  } catch {
    return "[invalid WebSocket URL]";
  }
}

export function websocketUrlFromLocation(locationLike = globalThis.location) {
  if (!locationLike?.href) return null;
  const raw = new URL(locationLike.href).searchParams.get("ws");
  if (!raw) return null;
  const url = new URL(raw);
  if (!['ws:', 'wss:'].includes(url.protocol)) throw new Error("The ws parameter must use ws: or wss:");
  return url.toString();
}

export function createWebSocketAdapter({ url, WebSocketImpl = globalThis.WebSocket, onEvent = () => {}, onStatus = () => {} } = {}) {
  if (!url) {
    return {
      mode: "mock",
      real: false,
      connect() { onStatus({ type: "TransportConnected", mode: "mock" }); },
      send() { throw new Error("Mock feed does not send commands to a server"); },
      close() {},
    };
  }
  if (typeof WebSocketImpl !== "function") {
    return {
      mode: "real",
      real: true,
      connect() { onStatus({ type: "TransportError", message: "WebSocket is unavailable" }); },
      send() { throw new Error("WebSocket is unavailable"); },
      close() {},
    };
  }
  let socket;
  return {
    mode: "real",
    real: true,
    connect() {
      socket = new WebSocketImpl(url);
      socket.addEventListener("open", () => onStatus({ type: "TransportConnected", mode: "real" }));
      socket.addEventListener("close", () => onEvent({ type: "ReconnectOutcomeUnknown" }));
      socket.addEventListener("error", () => onStatus({ type: "TransportError", message: "WebSocket transport error" }));
      socket.addEventListener("message", (message) => {
        try {
          const parsed = JSON.parse(message.data);
          if (!parsed || typeof parsed.type !== "string") throw new Error("event type missing");
          onEvent(parsed);
        } catch {
          onEvent({ type: "CommandRejected", error_code: "SCHEMA_INVALID" });
        }
      });
    },
    send(command) {
      if (!socket || socket.readyState !== 1) throw new Error("WebSocket is not connected");
      socket.send(JSON.stringify(command));
    },
    close() { socket?.close(); },
  };
}

export function exportEvidence(evidence) {
  return `${JSON.stringify(evidence, null, 2)}\n`;
}

if (typeof process !== "undefined" && process.argv?.[1] && process.argv[1].endsWith("consumer.mjs")) {
  const args = process.argv.slice(2);
  if (args[0] === "--export") {
    const output = args[1];
    if (!output) throw new Error("Usage: node consumer.mjs --export <path>");
    const fs = await import("node:fs/promises");
    await fs.writeFile(output, exportEvidence(runAllScenarios()), "utf8");
  }
}

const browserApi = {
  stableStringify,
  sha256,
  createInitialState,
  stateProjection,
  snapshotState,
  reduceState,
  applyEvents,
  runScenario,
  runAllScenarios,
  browserCaptureConfig,
  redactWebSocketUrl,
  websocketUrlFromLocation,
  createWebSocketAdapter,
  UX_SCENARIO_IDS,
};

if (typeof globalThis !== "undefined") globalThis.PulsarUx251 = browserApi;

function bootConsumer() {
  if (typeof document === "undefined") return;
  const byId = (id) => document.getElementById(id);
  let state = createInitialState();
  let lastStatus = "";
  let statusTimer;
  const transitionStatus = byId("transition-status-message");
  const statusRecovery = byId("transition-status-recovery");
  const statusDetails = byId("status-details");
  const diagnostics = byId("diagnostics-content");
  const transportStatus = byId("transport-status");
  const previewSurface = byId("preview-surface");
  const onAirSurface = byId("on-air-surface");
  const previewState = byId("preview-state");
  const onAirState = byId("on-air-state");
  const sceneName = byId("scene-name");
  const prepareButton = byId("prepare-button");
  const tbar = byId("tbar-slider");
  const tbarValue = byId("tbar-value");
  const takeButton = byId("take-button");
  const abortButton = byId("abort-button");

  function isDisabled(element) { return element?.getAttribute("aria-disabled") === "true"; }
  function setDisabled(element, disabled, reasonId) {
    if (!element) return;
    element.setAttribute("aria-disabled", String(disabled));
    if (reasonId) element.setAttribute("aria-describedby", reasonId);
  }
  function statusFor(next) {
    if (next.phase === "preparing") return ["Préparation de la Preview", "En attente de la première frame rendue.", "polite"];
    if (next.phase === "preview_ready") return ["Preview prête — Take disponible", "Le Take ne changera On-Air qu’après le commit.", "polite"];
    if (next.phase === "take_requested") return ["Take demandé — en attente d’acceptation", "On-Air n’a pas changé.", "polite"];
    if (next.phase === "take_accepted") return ["Take en attente — commit non confirmé", "On-Air n’a pas changé. Activez Annuler le Take en attente pour récupérer.", "polite"];
    if (next.phase === "outcome_unknown") return ["Résultat inconnu — reconnexion", "Réconciliez avec GetState avant de réactiver le Take.", "assertive"];
    if (next.last_outcome?.kind === "replay") return ["Déjà appliqué — résultat original affiché", "Aucun second commit n’a été créé.", "off"];
    if (next.last_outcome?.kind === "committed") return [`Commit confirmé à la frame ${next.last_outcome.frame_id}`, `On-Air : ${next.on_air.scene_name}`, "polite"];
    if (next.last_outcome?.kind === "aborted") return [next.last_outcome.message, "On-Air reste inchangé. Préparez à nouveau avec l’état courant.", "polite"];
    if (next.last_outcome?.kind === "rejected") return [next.last_outcome.message, next.last_outcome.recovery, "assertive"];
    if (next.phase === "frozen" || !next.operational) return ["Contrôles en pause — runtime gelé", "Le Programme reste live ; aucune nouvelle mutation n’est acceptée.", "assertive"];
    return ["Prêt — préparer une Preview", "Aucune Preview n’est préparée. Sélectionnez une scène, puis Préparer.", "polite"];
  }
  function render(next) {
    state = next;
    const [message, recovery, liveMode] = statusFor(next);
    const changed = message !== lastStatus;
    if (changed) {
      transitionStatus.setAttribute("aria-live", liveMode);
      transitionStatus.textContent = message;
      statusRecovery.textContent = recovery || "";
      lastStatus = message;
      if (liveMode !== "off") {
        clearTimeout(statusTimer);
        statusTimer = setTimeout(() => transitionStatus.setAttribute("aria-live", "polite"), 900);
      }
    }
    const onAirLabel = `Scène On-Air : ${next.on_air.scene_name} ; état : en direct, frame ${next.on_air.frame_id ?? "inconnue"}`;
    const previewLabel = next.preview.ready
      ? `Scène Preview : ${next.preview.scene_name} ; état : Preview prête`
      : `Scène Preview : ${next.preview.scene_name || "aucune"} ; état : ${next.phase === "preparing" ? "préparation en cours" : "aucune frame reçue"}`;
    onAirSurface.setAttribute("aria-label", onAirLabel);
    previewSurface.setAttribute("aria-label", previewLabel);
    onAirSurface.textContent = next.on_air.scene_name;
    previewSurface.textContent = next.preview.scene_name || "No frame received";
    onAirState.textContent = `EN DIRECT — ${next.on_air.scene_name}`;
    previewState.textContent = next.preview.ready ? "Preview prête" : (next.preview.scene_name ? "En attente de PreviewReady" : "Aucune frame reçue");
    tbar.value = String(next.tbar);
    tbar.setAttribute("aria-valuenow", String(next.tbar));
    tbar.setAttribute("aria-valuetext", `T-bar ${next.tbar} pour cent`);
    tbarValue.textContent = `${next.tbar}%`;
    const mutationDisabled = next.frozen || next.phase === "outcome_unknown" || !next.operational;
    setDisabled(prepareButton, mutationDisabled || next.phase === "preparing", "mutation-reason");
    setDisabled(tbar, mutationDisabled || !next.preview.ready, "tbar-help");
    setDisabled(takeButton, mutationDisabled || !next.preview.ready || next.phase === "preparing" || next.phase === "take_requested", "mutation-reason");
    setDisabled(abortButton, !next.pending || next.pending.stage !== "accepted", "mutation-reason");
    abortButton.hidden = !(next.pending && next.pending.stage === "accepted");
    const reason = mutationDisabled ? (next.phase === "outcome_unknown" ? "Résultat inconnu : réconciliez avec GetState d’abord." : "Runtime gelé ; le Programme reste live.") : "";
    byId("mutation-reason").textContent = reason;
    statusDetails.textContent = next.last_outcome?.error_code ? `${next.last_outcome.error_code}: ${next.last_outcome.message}` : "No rejection or recovery detail";
    diagnostics.textContent = JSON.stringify({
      runtime_instance_id: next.runtime_instance_id,
      phase: next.phase,
      frozen: next.frozen,
      revisions: next.revisions,
      role_map: next.role_map,
      server_seq: next.server_seq,
      commit_count: next.commit_count,
      last_outcome: next.last_outcome,
      transport: next.transport,
    }, null, 2);
    transportStatus.textContent = next.transport.mode === "mock" ? "Feed local déterministe — aucun serveur connecté" : `WebSocket: ${next.transport.status}`;
  }
  function dispatch(event) { render(reduceState(state, event)); }
  function domFocusSnapshot(element) {
    return {
      id: element?.id || null,
      tag: element?.tagName?.toLowerCase() || null,
      role: element?.getAttribute?.("role") || null,
      aria_disabled: element?.getAttribute?.("aria-disabled") || null,
      aria_live: transitionStatus?.getAttribute("aria-live") || null,
    };
  }
  function runBrowserDomCapture(config) {
    if (!config.enabled) return;
    const section = byId("browser-evidence-section");
    const output = byId("browser-evidence-json");
    const download = byId("browser-evidence-download");
    const marker = byId("browser-evidence-marker");
    const focusTrace = [];
    const actions = [];
    const liveStatus = [];
    const captureFocus = (action, element) => {
      element.focus();
      const focus = domFocusSnapshot(element);
      focus.action = action;
      focusTrace.push(focus);
      return focus;
    };
    const captureKey = (action, element, key, modifiers = {}) => {
      const beforeCommit = state.commit_count;
      const event = new KeyboardEvent("keydown", {
        key,
        shiftKey: Boolean(modifiers.shiftKey),
        bubbles: true,
        cancelable: true,
      });
      element.focus();
      const dispatched = element.dispatchEvent(event);
      const focus = domFocusSnapshot(element);
      actions.push({
        action,
        key,
        shiftKey: Boolean(modifiers.shiftKey),
        default_prevented: !dispatched,
        focus,
        commit_count_before: beforeCommit,
        commit_count_after: state.commit_count,
        live_status: transitionStatus.textContent,
      });
      if (transitionStatus.textContent && !liveStatus.includes(transitionStatus.textContent)) liveStatus.push(transitionStatus.textContent);
    };
    const recordStatus = () => {
      if (transitionStatus.textContent && !liveStatus.includes(transitionStatus.textContent)) liveStatus.push(transitionStatus.textContent);
    };

    // Establish a fixed mock PreviewReady state before exercising the real DOM.
    const payload = { scene_id: "scene-browser-keyboard", scene_name: "Lower third", expected_revisions: { program: 0, preview: 0, role_map: 0 } };
    dispatch({ type: "PrepareRequested", runtime_instance_id: "runtime-ux-251-mock", command_id: "browser-prepare", server_seq: 1, ...payload, command_payload: payload });
    dispatch({ type: "PrepareAccepted", runtime_instance_id: "runtime-ux-251-mock", command_id: "browser-prepare", server_seq: 2, ...payload, command_payload: payload });
    dispatch({ type: "PreviewReady", runtime_instance_id: "runtime-ux-251-mock", command_id: "browser-prepare", server_seq: 3, ...payload, first_frame_id: 10, first_pts_ns: 1000, command_payload: payload });
    recordStatus();

    document.body.tabIndex = -1;
    captureFocus("document-body", document.body);
    captureFocus("diagnostics", byId("diagnostics-disclosure").querySelector("summary"));
    captureFocus("scene-name", sceneName);
    captureFocus("prepare", prepareButton);
    captureFocus("tbar", tbar);

    const sliderValue = (value) => {
      tbar.value = String(Math.max(0, Math.min(100, value)));
      tbar.dispatchEvent(new Event("input", { bubbles: true }));
    };
    captureKey("tbar-step", tbar, "ArrowRight");
    sliderValue(1);
    captureKey("tbar-large-step", tbar, "ArrowRight", { shiftKey: true });
    sliderValue(11);
    captureKey("tbar-home", tbar, "Home");
    sliderValue(0);
    captureKey("tbar-end", tbar, "End");
    sliderValue(100);
    captureKey("tbar-page-up", tbar, "PageUp");
    captureKey("tbar-page-down", tbar, "PageDown");
    captureKey("tbar-space-no-commit", tbar, " ");
    // Keep the invariant visible in the dump: Space on the slider is never a
    // commit affordance.
    if (state.commit_count !== 0) actions.push({ action: "tbar-space-no-commit-failed", commit_count: state.commit_count });
    captureFocus("take", takeButton);
    captureKey("take-explicit", takeButton, "Enter");
    // Dispatching KeyboardEvent does not invoke a browser's default button
    // activation in every headless implementation; this explicit click models
    // the Enter activation while retaining the real event guard.
    takeButton.click();
    recordStatus();
    captureFocus("take-after-commit", takeButton);

    const evidence = {
      schema: "pulsar.ux-251.browser-dom-evidence.v1",
      issue: "ZabLaboratory/Pulsar#251",
      production: false,
      marker: "browser_dom_capture=true",
      browser_dom_capture: true,
      capture_mode: config.mode,
      scenario: config.scenario,
      viewport: { width: globalThis.innerWidth || null, height: globalThis.innerHeight || null, zoom: 1 },
      transport: { mode: state.transport.mode, real_websocket_attempted: state.transport.real_websocket_attempted, real_websocket_capture: false },
      focus_trace: focusTrace,
      actions,
      live_status: liveStatus,
      final: {
        focus: domFocusSnapshot(document.activeElement),
        aria_disabled: {
          prepare: prepareButton.getAttribute("aria-disabled"),
          tbar: tbar.getAttribute("aria-disabled"),
          take: takeButton.getAttribute("aria-disabled"),
          abort: abortButton.getAttribute("aria-disabled"),
        },
        live_status: transitionStatus.textContent,
        live_status_aria_live: transitionStatus.getAttribute("aria-live"),
        commit_count: state.commit_count,
        role_map: clone(state.role_map),
      },
      limitations: [
        "DOM/focus/KeyboardEvent capture only; not an AX-tree, screenshot or screen-reader capture.",
        "Mock feed only; no authenticated or real WebSocket result is claimed.",
      ],
    };
    const serialized = exportEvidence(evidence);
    marker.textContent = "browser_dom_capture=true";
    section.hidden = false;
    output.textContent = serialized;
    download.href = `data:application/json;charset=utf-8,${encodeURIComponent(serialized)}`;
    download.hidden = false;
  }
  function guard(element, callback) {
    return (event) => {
      if (isDisabled(element)) { event.preventDefault(); event.stopPropagation(); return; }
      callback(event);
    };
  }
  prepareButton.addEventListener("click", guard(prepareButton, () => {
    const name = sceneName.value.trim() || "Lower third";
    const payload = { scene_id: `scene-${sha256(name).slice(0, 8)}`, scene_name: name, expected_revisions: clone(state.revisions) };
    const command = { type: "PrepareRequested", command_id: `ui-prepare-${Date.now()}`, ...payload, command_payload: payload };
    dispatch({ ...command, server_seq: state.server_seq + 1 });
    if (state.transport.mode === "mock") {
      dispatch({ ...command, type: "PrepareAccepted", server_seq: state.server_seq + 1 });
      dispatch({ ...command, type: "PreviewReady", server_seq: state.server_seq + 1, first_frame_id: 10, first_pts_ns: 1000 });
    } else {
      try { adapter.send(command); } catch (error) { dispatch({ type: "TransportError", message: error.message }); }
    }
  }));
  tbar.addEventListener("input", guard(tbar, () => dispatch({ type: "TBarChanged", value: tbar.value, server_seq: state.server_seq + 1 })));
  takeButton.addEventListener("click", guard(takeButton, () => {
    const payload = { scene_id: state.preview.scene_id, scene_name: state.preview.scene_name, target_lane_id: state.role_map.preview_lane_id, expected_revisions: clone(state.revisions) };
    const command = { type: "TakeRequested", command_id: `ui-take-${Date.now()}`, intent_id: `ui-intent-${Date.now()}`, ...payload, command_payload: payload };
    dispatch({ ...command, server_seq: state.server_seq + 1 });
    if (state.transport.mode === "mock") {
      dispatch({ ...command, type: "TakeAccepted", server_seq: state.server_seq + 1 });
      dispatch({ ...command, type: "TakeCommitted", server_seq: state.server_seq + 1, frame_id: 20, pts_ns: 2000, role_map: { on_air_lane_id: "lane-b", preview_lane_id: "lane-a" } });
    } else {
      try { adapter.send(command); } catch (error) { dispatch({ type: "TransportError", message: error.message }); }
    }
  }));
  abortButton.addEventListener("click", guard(abortButton, () => dispatch({ type: "TakeAborted", command_id: state.pending.command_id, server_seq: state.server_seq + 1, reason: "operator", command_payload: { scene_id: state.pending.target_scene_id, scene_name: state.pending.target_scene_name, target_lane_id: state.pending.target_lane_id, expected_revisions: state.pending.expected_revisions } })));
  [prepareButton, takeButton, abortButton, tbar].forEach((element) => element.addEventListener("keydown", (event) => {
    if (isDisabled(element)) { event.preventDefault(); return; }
    if (element === tbar && (event.key === " " || event.key === "Enter")) event.stopPropagation();
  }));
  const configuredUrl = websocketUrlFromLocation();
  const adapter = createWebSocketAdapter({
    url: configuredUrl,
    onEvent: (event) => dispatch(event),
    onStatus: (event) => dispatch(event),
  });
  state.transport.mode = adapter.mode;
  state.transport.real_websocket_attempted = adapter.real;
  state.transport.configured_url = redactWebSocketUrl(configuredUrl);
  if (configuredUrl) adapter.connect();
  render(state);
  runBrowserDomCapture(browserCaptureConfig());
}

bootConsumer();
