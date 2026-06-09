#!/usr/bin/env python3
r"""Loopback Orion-WS stand-in for the M10 overlay probe (#79).

WHY THIS EXISTS
  The REAL Solar host bundle (``Solar/dist/host``) does NOT bake the
  ``wipe-cover`` node into its JS. On boot it calls
  ``mount({orionUrl, token, mode:"broadcast"})`` and the scene — the render
  bundle whose ``root`` carries the wipe-cover ``RenderNode`` — arrives over
  the wire from Orion, exactly as on the antenna. The probe used to serve only
  static files + a ``/leaf.json`` poll endpoint, so the bundle connected to
  NOTHING (``wss://${location.host}/orion/api/v1/show/stream``), ``#scene``
  stayed a black div, and the overlay never painted. The C5″ proof then read a
  black ``#scene`` as a "cover" — a FALSE POSITIVE on a blank frame.

  This module is the missing peer: a local asyncio server that speaks the
  exact slice of the LSDP/1.1 wire the Solar runtime (``@lumencast/runtime``,
  via ``@lumencast/protocol``) expects, so the REAL bundle subscribes, fetches
  its scene, renders the wipe-cover cover, and REPLAYS the reveal/hold/retract
  animation on a leaf delta — the M9 reactive path, end to end, with no VPS.

WHAT THE SOLAR RUNTIME ACTUALLY DOES (read from the bundle source, not guessed)
  ``@lumencast/runtime`` ``WsClient`` (transport/ws.ts):
    1. opens the WS at ``orionUrl`` advertising subprotocols
       ``["lsdp.v1.1", "lsdp.v1"]`` (server MUST pick one);
    2. on open sends a ``subscribe`` frame ``{v:1,type:"subscribe",token,...}``;
    3. expects a ``snapshot`` frame ``{v:1,type:"snapshot",seq>=1,scene_id,
       scene_version,state:{...}}`` — ``seq < 1`` is a hard transport error;
    4. then applies ``delta`` frames ``{v:1,type:"delta",seq,patches:[...]}``
       whose ``seq`` MUST be exactly ``prev_seq + 1`` (a gap closes the socket
       and reconnects — so the stand-in MUST keep seq monotonic & contiguous).
  ``mount()`` (mount.ts): on the snapshot it derives ``baseUrl`` from the WS
  URL host (``ws://h:p`` -> ``http://h:p``) and GETs the render bundle from
    ``${baseUrl}/lsdp/v1/scenes/{scene_id}/bundle?v={scene_version}`` .
  So the bundle endpoint MUST live on the SAME host:port as the WS. This
  server therefore serves BOTH the WS handshake/stream AND that one bundle GET
  on a single port (via ``process_request``), and the probe points the host's
  ``orion=`` query param at this port.

THE LEAF VALUE IS A PRIMITIVE ON THE WIRE — NOT THE scene_control OBJECT
  The frozen ``scene_control`` contract (#82) makes the leaf VALUE an *object*
  (``{target_scene, overlay{...}, cut_at_ms}``). But the LSDP codec
  (``@lumencast/protocol`` ``codec.ts`` ``assertLeafValue``) FORBIDS objects in
  snapshot ``state`` and ``delta.patches[].value`` — "objects are forbidden in
  patch values, push leaf-grain instead" (``INVALID_VALUE``). Feeding the real
  runtime the object verbatim would make it REJECT the frame at decode time
  (transport error -> reconnect loop) and never render. The runtime's
  ``KeyframePlayer`` keys the replay purely on *whether the value at the
  keyframes.key path CHANGED* (``lastKeyValue.current !== v``) — it never reads
  the object's fields. So the stand-in drives the wipe-cover replay with an
  LSDP-LEGAL leaf-grain PRIMITIVE (a monotonic revision string) at the
  canonical path. This is the faithful render trigger; the OBJECT-vs-leaf-grain
  contract tension is a real Conduit/Solar question, flagged in the probe
  report (it does NOT belong to this probe to resolve).

NO SECRETS. The token is a dummy viewer string; this server never authenticates
  anything and serves only the loopback wipe-cover scene. It binds 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import json
import threading
import urllib.parse
from typing import Any, Optional

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

# The exact subprotocols @lumencast/protocol advertises, 1.1 preferred.
WS_SUBPROTOCOL_V1_1 = "lsdp.v1.1"
WS_SUBPROTOCOL_V1_0 = "lsdp.v1"
PROTOCOL_VERSION = 1

# The WS upgrade path the Solar host's default orionUrl uses, and the bundle
# path @lumencast/runtime's BundleFetcher GETs (FetcherImpl: pathPrefix
# "/lsdp/v1/scenes", url ".../{id}/bundle?v={hash}").
SHOW_STREAM_PATH = "/orion/api/v1/show/stream"
BUNDLE_PATH_PREFIX = "/lsdp/v1/scenes"

# The loopback scene identity. scene_version is opaque to us; the runtime only
# checks the fetched bundle's scene_version equals what the snapshot declared.
SCENE_ID = "m10-wipe-cover"
SCENE_VERSION = "sha256:m10standin0000000000000000000000000000000000000000000000000000000"


def build_wipe_cover_bundle(leaf_path: str, *, fill: str = "#000000") -> dict[str, Any]:
    """The RenderBundle the host fetches — its ``root`` is the wipe-cover node.

    This is the JSON-of-record equivalent of Solar's ``buildWipeCoverNode``
    (``Solar/src/overlay/wipe-cover.ts``): a full-screen, absolute, opaque
    ``frame`` whose ``opacity`` is driven by a 4-step keyframe sequence
    (0 -> reveal opaque -> hold opaque -> retract transparent), keyed off
    ``leaf_path`` so the runtime's ``KeyframePlayer`` REPLAYS reveal/hold/
    retract on every value change at that path (the M9 reactive trigger).

    The keyframe ``at`` boundaries here mirror the DEMO leaf timings the probe
    delivers (reveal 400 / hold 500 / retract 400 ms, total 1300) so the
    opaque plateau the cut hides under is wide and the cover is fully opaque
    well before ``cut_at_ms``. The runtime needs first.at==0 and last.at==1
    (``compileForFramer`` rejects otherwise) — both hold.

    NOTE on the value type: the node only needs ``keyframes.key`` to flip to
    replay; the keyed value on the wire is a leaf-grain PRIMITIVE (see module
    docstring), so this node is correct regardless of the leaf value's shape.
    """
    # reveal 400 / hold 500 / retract 400 == 1300 total; boundaries normalised.
    reveal_ms, hold_ms, retract_ms = 400, 500, 400
    total = reveal_ms + hold_ms + retract_ms
    reveal_at = reveal_ms / total
    hold_end_at = (reveal_ms + hold_ms) / total
    return {
        "scene_version": SCENE_VERSION,
        "root": {
            "kind": "frame",
            "id": "wipe-cover",
            "props": {
                "width": "100%",
                "height": "100%",
                "background": fill,
            },
            "keyframes": {
                "key": leaf_path,
                "duration_ms": total,
                "easing": "ease-in-out",
                "steps": [
                    {"at": 0, "opacity": 0},
                    {"at": reveal_at, "opacity": 1},
                    {"at": hold_end_at, "opacity": 1},
                    {"at": 1, "opacity": 0},
                ],
            },
        },
    }


class OrionStandIn:
    """A single-port loopback Orion: LSDP/1.1 WS at ``/orion/api/v1/show/stream``
    + the bundle GET at ``/lsdp/v1/scenes/{id}/bundle``.

    Lifecycle the probe drives:
      - ``start()`` binds 127.0.0.1:port and serves in a background thread loop.
      - the Solar host connects, subscribes, gets the snapshot, fetches the
        bundle, renders the (transparent) cover.
      - ``deliver_leaf()`` pushes a contiguous ``delta`` at the canonical path
        with a fresh primitive revision -> the host's KeyframePlayer replays
        the wipe-cover animation. Returns once the frame has been sent to every
        live subscriber (so the probe's clock for the cut starts here).
    """

    def __init__(self, *, port: int, leaf_path: str, log) -> None:
        self.port = port
        self.leaf_path = leaf_path
        self._log = log
        self._bundle = build_wipe_cover_bundle(leaf_path)
        self._bundle_bytes = json.dumps(self._bundle).encode("utf-8")

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

        # Per-connection mutable state lives on the connection object; we keep
        # the set of live subscribers (those past the snapshot) for fan-out.
        self._subscribers: set[ServerConnection] = set()
        self._seq_by_conn: dict[int, int] = {}
        self._lock = threading.Lock()

        # Observability for the probe's assertions (all thread-safe scalars).
        self.subscribe_count = 0
        self.bundle_fetch_count = 0
        self.deltas_sent = 0
        self._rev = 0

    # -- public, called from the probe (sync) -------------------------------

    def start(self, timeout: float = 10.0) -> None:
        self._thread = threading.Thread(
            target=self._run, name="m10-orion-standin", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(
                f"Orion stand-in did not bind 127.0.0.1:{self.port} within "
                f"{timeout:.0f}s")

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._shutdown)
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def deliver_leaf(self, *, timeout: float = 5.0) -> int:
        """Fan a fresh contiguous ``delta`` at the canonical leaf path to every
        live subscriber. The value is a NEW primitive revision each call so the
        KeyframePlayer always sees a changed key and replays. Returns the number
        of subscribers the delta reached. Safe to call from the probe thread."""
        loop = self._loop
        if loop is None:
            raise RuntimeError("Orion stand-in not started")
        fut = asyncio.run_coroutine_threadsafe(self._deliver(), loop)
        return fut.result(timeout=timeout)

    @property
    def orion_ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{SHOW_STREAM_PATH}"

    # -- server internals (run on the background loop) ----------------------

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve_forever())

    async def _serve_forever(self) -> None:
        assert self._loop is not None
        self._server = await serve(
            self._ws_handler,
            "127.0.0.1",
            self.port,
            subprotocols=[WS_SUBPROTOCOL_V1_1, WS_SUBPROTOCOL_V1_0],
            select_subprotocol=self._select_subprotocol,
            process_request=self._process_request,
            ping_interval=None,
            max_size=2**24,
        )
        self._ready.set()
        # Block until close() is requested; closing the server makes
        # wait_closed() return, which lets run_until_complete() finish cleanly
        # (no loop.stop() — that would abort the pending future and leak tasks).
        try:
            await self._server.wait_closed()
        except asyncio.CancelledError:
            pass

    def _shutdown(self) -> None:
        # Scheduled onto the server loop via call_soon_threadsafe. Closing the
        # server resolves wait_closed() and unwinds _serve_forever / _run.
        if self._server is not None:
            self._server.close()

    def _select_subprotocol(self, connection: ServerConnection,
                            subprotocols: list[str]) -> Optional[str]:
        # Prefer 1.1 so the runtime's resume path is available; accept 1.0.
        for proto in (WS_SUBPROTOCOL_V1_1, WS_SUBPROTOCOL_V1_0):
            if proto in subprotocols:
                return proto
        return None

    def _process_request(self, connection: ServerConnection,
                         request: Request) -> Optional[Response]:
        """Short-circuit non-WS HTTP GETs. The ONLY one the runtime issues is
        the render-bundle fetch; serve it here so the WS port also answers
        ``${baseUrl}/lsdp/v1/scenes/{id}/bundle?v=...`` (baseUrl == this host).
        Returning ``None`` lets the WS upgrade proceed for the stream path."""
        path = urllib.parse.urlparse(request.path).path
        if path == SHOW_STREAM_PATH:
            return None  # proceed to the WebSocket handshake
        if path.startswith(BUNDLE_PATH_PREFIX) and path.endswith("/bundle"):
            with self._lock:
                self.bundle_fetch_count += 1
            headers = Headers()
            headers["Content-Type"] = "application/json"
            headers["Cache-Control"] = "no-store"
            headers["Content-Length"] = str(len(self._bundle_bytes))
            self._log(f"   [orion] bundle GET {path} -> wipe-cover RenderBundle "
                      f"({len(self._bundle_bytes)} bytes)")
            return Response(200, "OK", headers, self._bundle_bytes)
        # Anything else is not part of the contract — 404, do not upgrade.
        headers = Headers()
        headers["Content-Type"] = "text/plain"
        body = b"not found"
        headers["Content-Length"] = str(len(body))
        return Response(404, "Not Found", headers, body)

    async def _ws_handler(self, connection: ServerConnection) -> None:
        """One subscriber. Read the ``subscribe`` frame, send the snapshot
        (seq=1) carrying the wipe-cover scene's leaf state, then keep the socket
        open so we can fan deltas to it. Tolerant of input/ping/unsubscribe."""
        try:
            raw = await connection.recv()
        except websockets.ConnectionClosed:
            return
        try:
            frame = json.loads(raw)
        except Exception:
            await connection.close(1002, "bad subscribe frame")
            return
        if not isinstance(frame, dict) or frame.get("type") != "subscribe":
            await connection.close(1002, "expected subscribe")
            return

        with self._lock:
            self.subscribe_count += 1
        self._log(f"   [orion] subscribe received (subprotocol="
                  f"{connection.subprotocol!r}); sending snapshot seq=1.")

        # The initial leaf state. A primitive (leaf-grain, LSDP-legal); the
        # KeyframePlayer's first delta will flip it to a fresh revision.
        self._rev += 1
        snapshot = {
            "v": PROTOCOL_VERSION,
            "type": "snapshot",
            "seq": 1,
            "scene_id": SCENE_ID,
            "scene_version": SCENE_VERSION,
            "state": {self.leaf_path: f"rev-{self._rev}"},
        }
        await connection.send(json.dumps(snapshot))
        self._seq_by_conn[id(connection)] = 1
        self._subscribers.add(connection)

        try:
            async for message in connection:
                # The runtime is read-only for our purposes (a viewer token
                # never writes). Accept and ignore input/ping; reply pong so a
                # keep-alive ping never trips a timeout.
                try:
                    msg = json.loads(message)
                except Exception:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    pong: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": "pong"}
                    if isinstance(msg.get("nonce"), str):
                        pong["nonce"] = msg["nonce"]
                    await connection.send(json.dumps(pong))
        except websockets.ConnectionClosed:
            pass
        finally:
            self._subscribers.discard(connection)
            self._seq_by_conn.pop(id(connection), None)

    async def _deliver(self) -> int:
        """Send one contiguous delta (fresh primitive revision) to every live
        subscriber. seq is per-connection prev+1 so the runtime never faults a
        gap. Runs on the server loop."""
        self._rev += 1
        value = f"rev-{self._rev}"
        targets = list(self._subscribers)
        sent = 0
        for conn in targets:
            cid = id(conn)
            seq = self._seq_by_conn.get(cid, 1) + 1
            delta = {
                "v": PROTOCOL_VERSION,
                "type": "delta",
                "seq": seq,
                "patches": [{"path": self.leaf_path, "value": value}],
                "cause": {"source": "probe:m10-orion-standin"},
            }
            try:
                await conn.send(json.dumps(delta))
                self._seq_by_conn[cid] = seq
                sent += 1
            except websockets.ConnectionClosed:
                self._subscribers.discard(conn)
                self._seq_by_conn.pop(cid, None)
        with self._lock:
            self.deltas_sent += sent
        self._log(f"   [orion] delta fanned to {sent} subscriber(s) "
                  f"(path={self.leaf_path}, value={value!r}) — KeyframePlayer "
                  "replays wipe-cover.")
        return sent
