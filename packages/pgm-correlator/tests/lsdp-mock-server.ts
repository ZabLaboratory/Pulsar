// Minimal local WS server speaking plain JSON LSDP envelope frames --
// no Orion, no auth, no real correlation_id. Exists ONLY to drive
// OrionObserver's parsing/filtering logic under a real WebSocket
// transport. It is not, and does not claim to be, a stand-in for a live
// Orion instance or a source of PGM proof.

import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { WebSocketServer, type WebSocket } from "ws";

export class MockLsdpServer {
  readonly httpServer: Server;
  readonly wss: WebSocketServer;
  readonly clients = new Set<WebSocket>();

  static async create(): Promise<MockLsdpServer> {
    const m = new MockLsdpServer();
    await new Promise<void>((resolve, reject) => {
      m.httpServer.once("error", reject);
      m.httpServer.listen(0, "127.0.0.1", () => resolve());
    });
    return m;
  }

  private constructor() {
    this.httpServer = createServer();
    this.wss = new WebSocketServer({
      server: this.httpServer,
      handleProtocols: (protocols) => {
        const list = Array.from(protocols as Iterable<string>);
        return list.includes("lsdp.v1.1") ? "lsdp.v1.1" : false;
      },
    });
    this.wss.on("connection", (ws) => this.clients.add(ws));
    this.wss.on("connection", (ws) => ws.on("close", () => this.clients.delete(ws)));
  }

  get url(): string {
    const addr = this.httpServer.address() as AddressInfo | null;
    if (!addr) throw new Error("MockLsdpServer not listening yet");
    return `ws://127.0.0.1:${addr.port}`;
  }

  /** Sends a raw frame object (any shape) to every connected client, JSON-encoded. */
  broadcast(frame: unknown): void {
    const payload = JSON.stringify(frame);
    for (const c of this.clients) {
      if (c.readyState === c.OPEN) c.send(payload);
    }
  }

  /** Sends a non-JSON frame (e.g. a stray ping payload) to exercise the parser's tolerance. */
  broadcastRaw(text: string): void {
    for (const c of this.clients) {
      if (c.readyState === c.OPEN) c.send(text);
    }
  }

  async close(): Promise<void> {
    for (const c of this.clients) c.terminate();
    await new Promise<void>((resolve) => this.wss.close(() => resolve()));
    await new Promise<void>((resolve) => this.httpServer.close(() => resolve()));
  }
}
