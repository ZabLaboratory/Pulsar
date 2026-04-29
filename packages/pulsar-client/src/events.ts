import type { PulsarEventMap, PulsarEventName } from "./types.js";

/**
 * Minimal typed event emitter -- on/off/emit, listeners receive a
 * strongly-typed payload per event name. Avoids pulling in node:events
 * so the package stays browser-friendly (Phase 13b only spawns Pulsar in
 * Node, but a hypothetical browser caller could still use this client).
 */
type Listener<K extends PulsarEventName> = (payload: PulsarEventMap[K]) => void;

export class TypedEventEmitter {
  // Stored as an untyped map internally so the per-event-name push/erase
  // logic doesn't fight the conditional generic of PulsarEventMap.
  // External access goes through the typed methods below.
  private readonly listeners = new Map<PulsarEventName, Array<Listener<PulsarEventName>>>();

  on<K extends PulsarEventName>(event: K, listener: Listener<K>): this {
    const arr = this.listeners.get(event) ?? [];
    arr.push(listener as Listener<PulsarEventName>);
    this.listeners.set(event, arr);
    return this;
  }

  off<K extends PulsarEventName>(event: K, listener: Listener<K>): this {
    const arr = this.listeners.get(event);
    if (!arr) return this;
    const idx = arr.indexOf(listener as Listener<PulsarEventName>);
    if (idx >= 0) arr.splice(idx, 1);
    return this;
  }

  emit<K extends PulsarEventName>(event: K, payload: PulsarEventMap[K]): void {
    const arr = this.listeners.get(event);
    if (!arr) return;
    // Snapshot so a listener mutating the array (off()) mid-dispatch
    // doesn't skip subsequent listeners.
    for (const fn of arr.slice()) {
      try {
        (fn as Listener<K>)(payload);
      } catch (err) {
        // A throwing listener shouldn't take the whole client down.
        // eslint-disable-next-line no-console
        console.error("[pulsar-client] listener for", event, "threw:", err);
      }
    }
  }

  removeAllListeners(): void {
    this.listeners.clear();
  }
}
