import { WebSocket } from "ws";

import type { ProjectionIdentity, StateEvent } from "./types.js";

export interface OrionObserverOptions {
  /** Full LSDP WS URL, e.g. `wss://host/api/v1/show/stream.lsdp?token=<show-token>`.
   *  The token is a viewer/show-token: this observer never sends an `input`
   *  frame, matching the role Orion's own `TestWS_ViewerCannotInput` enforces
   *  server-side. Zero modification of Orion -- this is the connection any
   *  show-token holder can already open. */
  url: string;
  /** Injected for tests; defaults to Date.now. */
  now?: () => number;
}

type StateListener = (e: StateEvent) => void;

const IDENTITY_KEYS = [
  ["schema_version", "schemaVersion"],
  ["scene_digest", "sceneDigest"],
  ["runtime_instance_id", "runtimeInstanceId"],
  ["target", "target"],
  ["render_revision", "renderRevision"],
  ["correlation_id", "correlationId"],
] as const;

/** Reads the six identity fields off a raw LSDP envelope. Returns
 *  `undefined` when none are present -- a frame with no identity carries
 *  nothing to correlate, mirroring Orion's own `recordIdentity` (mirror.go)
 *  which likewise treats an all-empty projection as "no metadata". */
function extractIdentity(frame: Record<string, unknown>): ProjectionIdentity | undefined {
  const identity: Record<string, string> = {};
  let any = false;
  for (const [wireKey, tsKey] of IDENTITY_KEYS) {
    const v = frame[wireKey];
    if (typeof v === "string" && v !== "") {
      identity[tsKey] = v;
      any = true;
    }
  }
  return any ? (identity as ProjectionIdentity) : undefined;
}

/**
 * Passive LSDP viewer (ADR-BLUE-012 R6 §16.1). Opens exactly the
 * connection any show-token holder can open today -- subprotocol
 * `lsdp.v1.1`, read-only. Never writes to Orion; never sends an `input`
 * frame. Emits a `StateEvent` for every `delta`/`snapshot` frame that
 * carries at least one of the six projection-identity fields.
 */
export class OrionObserver {
  private ws: WebSocket | undefined;
  private readonly listeners = new Set<StateListener>();
  private readonly now: () => number;

  constructor(private readonly opts: OrionObserverOptions) {
    this.now = opts.now ?? Date.now;
  }

  onState(cb: StateListener): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this.opts.url, ["lsdp.v1.1"]);
      this.ws = ws;
      const onOpen = () => {
        ws.off("error", onError);
        resolve();
      };
      const onError = (err: Error) => {
        ws.off("open", onOpen);
        reject(err);
      };
      ws.once("open", onOpen);
      ws.once("error", onError);
      ws.on("message", (raw) => this.handleMessage(raw));
    });
  }

  private handleMessage(raw: unknown): void {
    let frame: Record<string, unknown>;
    try {
      frame = JSON.parse(String(raw)) as Record<string, unknown>;
    } catch {
      return; // Not a JSON envelope frame -- ignore rather than throw.
    }
    const type = frame["type"];
    if (type !== "delta" && type !== "snapshot") return;
    const identity = extractIdentity(frame);
    if (!identity) return;
    const sequence = typeof frame["seq"] === "number" ? (frame["seq"] as number) : 0;
    const sceneId = typeof frame["scene_id"] === "string" ? (frame["scene_id"] as string) : "";
    const event: StateEvent = {
      receivedAtMs: this.now(),
      frameType: type,
      sequence,
      sceneId,
      identity,
    };
    for (const cb of this.listeners) cb(event);
  }

  close(): void {
    this.ws?.close();
    this.ws = undefined;
  }
}
