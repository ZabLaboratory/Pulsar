// Fake pulsar.exe for the bundle's vitest suite.
//
// Mimics what the real binary does at boot:
//   1. Open a WebSocket server on a random localhost port.
//   2. Write obs-websocket/config.json under cwd with that port +
//      a fresh password.
//   3. Print the exact ready marker pulsar-headless emits ("ready, idling").
//   4. Stay alive until SIGTERM / SIGINT.
//
// The WS handler honours both obswebsocket.json and obswebsocket.msgpack
// subprotocols and answers the v5 Hello/Identify dance, which is enough
// for a PulsarClient to consider the connection up and ready. Vendor
// requests are stubbed; the bundle tests don't exercise them (those
// live in pulsar-client's own suite).

import { createServer } from "node:http";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { WebSocketServer } from "ws";
import { encode as msgpackEncode } from "@msgpack/msgpack";

const httpServer = createServer();

httpServer.listen(0, "127.0.0.1", () => {
  const port = httpServer.address().port;
  const password = `fake-${Date.now()}`;

  // Write the config.json the spawn() helper reads.
  const cfgDir = join(process.cwd(), "obs-websocket");
  mkdirSync(cfgDir, { recursive: true });
  writeFileSync(
    join(cfgDir, "config.json"),
    JSON.stringify({ server_port: port, server_password: password }, null, 2),
  );

  const wss = new WebSocketServer({
    server: httpServer,
    handleProtocols: (protocols) => {
      const list = Array.from(protocols);
      if (list.includes("obswebsocket.msgpack")) return "obswebsocket.msgpack";
      if (list.includes("obswebsocket.json")) return "obswebsocket.json";
      return false;
    },
  });

  wss.on("connection", (ws) => {
    const isMsgpack = ws.protocol === "obswebsocket.msgpack";
    const send = (frame) => {
      if (isMsgpack) {
        const buf = msgpackEncode(frame);
        ws.send(Buffer.from(buf.buffer, buf.byteOffset, buf.byteLength));
      } else {
        ws.send(JSON.stringify(frame));
      }
    };

    // Hello
    send({ op: 0, d: { obsWebSocketVersion: "5.7.3", rpcVersion: 1 } });

    ws.on("message", (raw, isBinary) => {
      let frame;
      try {
        if (isBinary) {
          // Skip msgpack decode here -- we only need to recognise op
          // numbers, which we don't actually need; just answer Identified
          // on the first incoming message.
          send({ op: 2, d: { negotiatedRpcVersion: 1 } });
          return;
        }
        frame = JSON.parse(raw.toString());
      } catch {
        return;
      }
      if (frame.op === 1) {
        send({ op: 2, d: { negotiatedRpcVersion: 1 } });
      }
    });
  });

  // The marker spawn() greps for. Format must match exactly.
  process.stdout.write(
    "pulsar-headless: libobs 32.1.2-fake ready, idling (Ctrl+C to exit)\n",
  );
});

const close = () => {
  httpServer.close();
  process.exit(0);
};
process.on("SIGTERM", close);
process.on("SIGINT", close);
