#!/usr/bin/env python3
"""Windows integration proof for pipe-backed Pulsar graceful shutdown.

The test deliberately uses the same ``PulsarProcess`` shape as the canary:
stdout is a pipe, the child is a ``/SUBSYSTEM:WINDOWS`` executable, and the
shutdown request travels through an anonymous inherited event. A forced kill
or a missing release marker is always a failure. CTest invokes this script
only on Windows after the real ``pulsar-headless`` target has been built.
"""

from __future__ import annotations

import asyncio
import base64
import http.server
import io
import importlib.util
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import struct
import sys
import tempfile
import threading
from urllib.parse import parse_qs, quote, urlsplit
import wave


ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = ROOT / "scripts" / "probe-dual-lane.py"

# Build the short looped WAV at runtime so security scanners do not mistake
# deterministic test media for a credential or opaque token.


def _audio_wav_data_uri() -> str:
    sample_rate = 48_000
    frames = 480  # 10 ms, looped by the page to keep CEF's audio stream active.
    pcm = b"".join(
        struct.pack("<h", int(1600 * math.sin(2 * math.pi * 440 * index / sample_rate)))
        for index in range(frames)
    )
    payload = io.BytesIO()
    with wave.open(payload, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return "data:audio/wav;base64," + base64.b64encode(payload.getvalue()).decode("ascii")


AUDIO_WAV_DATA_URI = _audio_wav_data_uri()


def _cef_audio_page(lane: str) -> str:
    html = f"""<!doctype html><meta charset='utf-8'>
<style>html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#132238;color:#f6fbff;font:48px Arial}}
main{{height:100%;display:grid;place-items:center}} .tile{{padding:40px;background:{'#ff3da6' if lane == 'A' else '#38e8ff'}}}</style>
<main><div class='tile'>PULSAR CEF / LANE {lane}</div></main>
<audio autoplay loop src='{AUDIO_WAV_DATA_URI}'></audio>
<script>const a=document.querySelector('audio'); a.play().catch(()=>{{}});</script>"""
    return html


def _cef_audio_data_url(lane: str) -> str:
    """Retain a data-URI helper for local unit checks; CTest uses HTTP below."""

    return "data:text/html;charset=utf-8," + quote(_cef_audio_page(lane), safe="")


class _CefAudioHandler(http.server.BaseHTTPRequestHandler):
    """Serve deterministic, lane-tagged CEF HTML over loopback HTTP."""

    def _serve_page(self, include_body: bool) -> None:
        request = urlsplit(self.path)
        if request.path != "/pulsar-cef-shutdown.html":
            self.send_error(404)
            return
        lane = parse_qs(request.query).get("lane", [None])[0]
        if lane not in ("A", "B"):
            self.send_error(400)
            return
        body = _cef_audio_page(lane).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._serve_page(True)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._serve_page(False)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _CefAudioServer:
    """Bounded loopback server kept alive through the CEF shutdown request."""

    def __init__(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CefAudioHandler)
        self.server.daemon_threads = True
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/pulsar-cef-shutdown.html"

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="pulsar-cef-shutdown-http",
            daemon=True,
        )
        self.thread.start()

    def close(self) -> None:
        if self.thread is None:
            self.server.server_close()
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("CEF test HTTP server thread did not join within 5 seconds")
        self.thread = None


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "pulsar_shutdown_integration_probe", PROBE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load probe module: {PROBE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _raise_with_sanitized_tail(probe, process, message: str) -> None:
    tail = probe.failure_tail(process.snapshot(), 40)
    raise RuntimeError(f"{message}\nSanitized Pulsar log tail:\n{tail}")


async def _configure_two_browser_sources(probe, ws_url: str, password: str, process, server) -> None:
    """Create two real CEF browser_source instances and keep recording active."""

    async with probe.websockets.connect(
        ws_url, subprotocols=["obswebsocket.json"], open_timeout=15
    ) as ws:
        await probe.identify(ws, password)
        inbox = probe.Inbox()
        scene = "cef-shutdown-real-scene"
        response = await probe.request(
            inbox, ws, "CreateScene", "cef-shutdown-create-scene", {"sceneName": scene}
        )
        probe.assert_success(response, "CreateScene(cef-shutdown-real-scene)")

        # Exercise the immediate post-READY lifecycle before the two durable
        # browser instances: CreateInput may return before its first video tick,
        # so RemoveInput must safely destroy a source with no CEF browser yet.
        readiness_input = "cef-readiness-immediate"
        readiness_settings = {
            "url": f"{server.url}?lane=A",
            "is_local_file": False,
            "width": probe.CANVAS_W,
            "height": probe.CANVAS_H,
            "fps_custom": True,
            "fps": 30,
            "shutdown": False,
            "reroute_audio": True,
            "restart_when_active": False,
            "webpage_control_level": 0,
        }
        response = await probe.request(
            inbox,
            ws,
            "CreateInput",
            "cef-shutdown-readiness-create-1",
            {
                "sceneName": scene,
                "inputName": readiness_input,
                "inputKind": "browser_source",
                "inputSettings": readiness_settings,
                "sceneItemEnabled": True,
            },
        )
        probe.assert_success(response, "CreateInput(immediate readiness)")
        response = await probe.request(
            inbox,
            ws,
            "RemoveInput",
            "cef-shutdown-readiness-remove-1",
            {"inputName": readiness_input},
        )
        probe.assert_success(response, "RemoveInput(immediate readiness)")
        response = await probe.request(
            inbox,
            ws,
            "CreateInput",
            "cef-shutdown-readiness-create-2",
            {
                "sceneName": scene,
                "inputName": readiness_input,
                "inputKind": "browser_source",
                "inputSettings": readiness_settings,
                "sceneItemEnabled": True,
            },
        )
        probe.assert_success(response, "CreateInput(immediate recreate)")
        response = await probe.request(
            inbox,
            ws,
            "RemoveInput",
            "cef-shutdown-readiness-remove-2",
            {"inputName": readiness_input},
        )
        probe.assert_success(response, "RemoveInput(immediate recreate)")

        for lane in ("A", "B"):
            input_name = f"cef-shutdown-browser-{lane}"
            response = await probe.request(
                inbox,
                ws,
                "CreateInput",
                f"cef-shutdown-create-input-{lane}",
                {
                    "sceneName": scene,
                    "inputName": input_name,
                    "inputKind": "browser_source",
                    "inputSettings": {
                        "url": f"{server.url}?lane={lane}",
                        "is_local_file": False,
                        "width": probe.CANVAS_W,
                        "height": probe.CANVAS_H,
                        "fps_custom": True,
                        "fps": 30,
                        "shutdown": False,
                        "reroute_audio": True,
                        "restart_when_active": False,
                        "webpage_control_level": 0,
                    },
                    "sceneItemEnabled": True,
                },
            )
            probe.assert_success(response, f"CreateInput(browser_source {lane})")

        response = await probe.request(
            inbox,
            ws,
            "SetCurrentProgramScene",
            "cef-shutdown-set-program",
            {"sceneName": scene},
        )
        probe.assert_success(response, "SetCurrentProgramScene(cef-shutdown-real-scene)")

        # Source screenshots are independent of scene visibility and prove that
        # both CreateInput calls reached genuine CEF browser instances.
        for lane in ("A", "B"):
            await probe.wait_for_nonblack_source(
                inbox,
                ws,
                f"cef-shutdown-browser-{lane}",
                require_variance=True,
            )

        deadline = asyncio.get_running_loop().time() + 20
        started_ids: set[str] = set()
        while len(started_ids) < 2 and asyncio.get_running_loop().time() < deadline:
            for line in process.snapshot():
                match = re.search(r"event=audio_stream_started browser_id=(\d+)(?:\s|$)", line)
                if match:
                    started_ids.add(match.group(1))
            if len(started_ids) < 2:
                await asyncio.sleep(0.1)
        if len(started_ids) != 2:
            raise RuntimeError(
                "CEF audio handler did not start for both browser sources: "
                f"observed={sorted(started_ids)}"
            )

        response = await probe.request(inbox, ws, "StartRecord", "cef-shutdown-start-record")
        probe.assert_success(response, "StartRecord(cef-shutdown-real-output)")
        await probe.wait_event(
            inbox,
            ws,
            "RecordStateChanged",
            lambda data: data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED",
            timeout=20,
        )


def main() -> int:
    if os.name != "nt":
        print("SKIP: graceful-shutdown integration proof is Windows-only")
        return 0
    executable = Path(
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PULSAR_HEADLESS_EXE", "")
    )
    if not executable.is_file():
        raise RuntimeError(f"Pulsar executable not found: {executable}")

    probe = _load_probe()
    runtime_id = f"shutdown-probe-{os.getpid()}-{secrets.token_hex(4)}"
    previous_runtime_id = os.environ.get("PULSAR_RUNTIME_INSTANCE_ID")
    os.environ["PULSAR_RUNTIME_INSTANCE_ID"] = runtime_id
    record_dir = Path(tempfile.mkdtemp(prefix="pulsar-shutdown-record-"))
    cef_server = _CefAudioServer()
    cef_server.start()
    process = probe.PulsarProcess(
        executable.resolve(),
        "x264",
        record_dir,
        trace_path=None,
        runtime_id=runtime_id,
        cef_url=cef_server.url,
    )
    started = False
    real_output_started = False
    cleanup_error: Exception | None = None
    try:
        process.spawn()
        started = True
        # The child-side ACK must precede acceptance of PULSAR_READY.
        process.wait_for_shutdown_control_ready(timeout=60)
        ready = process.wait_for(probe.READY_RE, timeout=60)
        asyncio.run(_configure_two_browser_sources(probe, ready.group(1), process.password, process, cef_server))
        real_output_started = True
    finally:
        try:
            process.shutdown()
        except Exception as exc:  # preserve the primary startup error
            cleanup_error = exc
        try:
            if started and cleanup_error is None:
                process.assert_shutdown_clean(require_runtime_lease=True)
        except Exception as exc:
            cleanup_error = cleanup_error or exc
        finally:
            if previous_runtime_id is None:
                os.environ.pop("PULSAR_RUNTIME_INSTANCE_ID", None)
            else:
                os.environ["PULSAR_RUNTIME_INSTANCE_ID"] = previous_runtime_id
            try:
                shutil.rmtree(record_dir)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
            try:
                cef_server.close()
            except Exception as exc:
                cleanup_error = cleanup_error or exc

    if cleanup_error is not None:
        _raise_with_sanitized_tail(probe, process, str(cleanup_error))
    if not started:
        raise RuntimeError("Pulsar process never spawned")
    lines = process.snapshot()
    required = (
        "PULSAR_SHUTDOWN_CONTROL event=ready",
        "PULSAR_SHUTDOWN_CONTROL event=signaled",
        "PULSAR_RUNTIME_INSTANCE runtime_dir_lease=released",
        "PULSAR_RUNTIME_INSTANCE lease=released",
        "[pulsar-headless] shutting down",
    )
    for marker in required:
        if not any(marker in line for line in lines):
            _raise_with_sanitized_tail(
                probe, process, f"missing graceful-shutdown marker: {marker}"
            )
    readiness_markers = (
        "PULSAR_CEF_SHUTDOWN event=manager_started",
        "PULSAR_CEF_SHUTDOWN event=cef_ready",
    )
    readiness_positions = {}
    for marker in readiness_markers:
        matches = [index for index, line in enumerate(lines) if marker in line]
        if not matches:
            _raise_with_sanitized_tail(probe, process, f"missing CEF readiness marker: {marker}")
        readiness_positions[marker] = matches[-1]
    first_browser_created = next(
        (index for index, line in enumerate(lines) if "event=browser_created" in line),
        None,
    )
    if first_browser_created is None or not (
        readiness_positions[readiness_markers[0]]
        < readiness_positions[readiness_markers[1]]
        < first_browser_created
    ):
        _raise_with_sanitized_tail(probe, process, "CEF readiness did not precede browser creation")
    control_lines = [line for line in lines if line.startswith("PULSAR_SHUTDOWN_CONTROL")]
    if any(
        "PULSAR_SHUTDOWN_EVENT_HANDLE" in line or "handle" in line.lower()
        for line in control_lines
    ):
        _raise_with_sanitized_tail(probe, process, "shutdown handle or its value was logged")
    if process.forced_kill_used:
        _raise_with_sanitized_tail(probe, process, "forced process kill was used")
    if any(
        "event=source_destroy_failed" in line
        or "event=source_create_rejected" in line
        or "event=post_rejected reason=cef_not_ready" in line
        for line in lines
    ):
        _raise_with_sanitized_tail(
            probe,
            process,
            "immediate CEF source lifecycle reported a readiness or destroy failure",
        )
    if process.proc is None or process.proc.returncode != 0:
        status = process.proc.returncode if process.proc is not None else None
        _raise_with_sanitized_tail(probe, process, f"Pulsar exited unsuccessfully: {status}")
    if not real_output_started:
        _raise_with_sanitized_tail(probe, process, "real recording output was not active before shutdown")

    # The ordering is the regression contract for the CEF fix: two actual
    # browsers are observed, every one reaches OnBeforeClose, and only then
    # does the manager thread leave the CEF loop and call CefShutdown.
    required_markers = (
        "PULSAR_CEF_SHUTDOWN event=browser_created",
        "PULSAR_CEF_SHUTDOWN event=begin ",
        "PULSAR_CEF_SHUTDOWN event=close_requested ",
        "PULSAR_CEF_SHUTDOWN event=browser_closed",
        "PULSAR_CEF_SHUTDOWN event=barrier_released phase=Drained browser_count=0",
        "PULSAR_CEF_SHUTDOWN event=cef_shutdown_begin browser_count=0",
        "PULSAR_CEF_SHUTDOWN event=cef_shutdown_complete browser_count=0",
    )
    count_constraints = {
        required_markers[0]: 2,
        required_markers[3]: 0,
    }
    lines = process.snapshot()
    positions: dict[str, int] = {}
    for marker in required_markers:
        matches = [index for index, line in enumerate(lines) if marker in line]
        if marker in count_constraints:
            # The browser id is emitted between the event and count.  Match
            # both fields on one structured line so a count from another
            # lifecycle event cannot satisfy this criterion.
            count = count_constraints[marker]
            matches = [
                index
                for index in matches
                if re.search(rf"\bbrowser_count={count}(?:\s|$)", lines[index]) is not None
            ]
        if not matches:
            _raise_with_sanitized_tail(probe, process, f"missing CEF lifecycle marker: {marker}")
        positions[marker] = matches[-1]
    # BrowserSource destruction may request CloseBrowser before libobs unloads
    # the plugin, so the ``begin`` marker can legitimately follow the final
    # OnBeforeClose callback.  The non-negotiable ordering is that real browser
    # creation is observed, every browser reaches count zero, and CefShutdown
    # begins only after the barrier has released.
    if positions[required_markers[0]] > positions[required_markers[3]]:
        _raise_with_sanitized_tail(
            probe,
            process,
            f"CEF browser count did not reach zero after both creations: {positions}",
        )
    barrier = positions[required_markers[4]]
    shutdown_begin = positions[required_markers[5]]
    shutdown_complete = positions[required_markers[6]]
    if not barrier < shutdown_begin < shutdown_complete:
        _raise_with_sanitized_tail(probe, process, f"CEF barrier/shutdown ordering invalid: {positions}")
    audio_started = {}
    audio_stopped = {}
    audio_quiescent = {}
    browser_detached = {}
    browser_closed = {}
    browser_close_observed = {}
    browser_finalization_intent = {}
    client_keepalive_acquired = {}
    client_keepalive_released = {}
    for index, line in enumerate(lines):
        for event, destination in (
            ("audio_stream_started", audio_started),
            ("audio_stream_stopped", audio_stopped),
            ("audio_quiescent", audio_quiescent),
            ("browser_detached", browser_detached),
            ("browser_close_observed", browser_close_observed),
            ("browser_finalization_intent", browser_finalization_intent),
            ("client_keepalive_acquired", client_keepalive_acquired),
            ("client_keepalive_released", client_keepalive_released),
        ):
            match = re.search(rf"event={event} browser_id=(\d+)(?:\s|$)", line)
            if match:
                destination.setdefault(match.group(1), []).append(index)
        match = re.search(r"event=browser_closed browser_id=(\d+)(?:\s|$)", line)
        if match:
            browser_closed.setdefault(match.group(1), []).append(index)
    if (
        len(audio_started) != 2
        or set(audio_started) != set(audio_stopped)
        or set(audio_started) != set(audio_quiescent)
        or set(audio_started) != set(browser_detached)
        or set(audio_started) != set(browser_close_observed)
        or set(audio_started) != set(browser_finalization_intent)
        or set(audio_started) != set(client_keepalive_acquired)
        or set(audio_started) != set(client_keepalive_released)
    ):
        _raise_with_sanitized_tail(
            probe,
            process,
            "CEF client/audio lifecycle did not reach exactly two IDs: "
            f"started={sorted(audio_started)} stopped={sorted(audio_stopped)} "
            f"quiescent={sorted(audio_quiescent)} detached={sorted(browser_detached)} "
            f"observed={sorted(browser_close_observed)} intent={sorted(browser_finalization_intent)} "
            f"acquired={sorted(client_keepalive_acquired)} released={sorted(client_keepalive_released)}",
        )
    for browser_id in sorted(audio_started):
        if browser_id not in browser_closed:
            _raise_with_sanitized_tail(
                probe, process, f"CEF browser {browser_id} has no browser_closed marker"
            )
        started = min(audio_started[browser_id])
        stopped_candidates = [index for index in audio_stopped[browser_id] if index > started]
        if not stopped_candidates:
            _raise_with_sanitized_tail(
                probe, process, f"CEF browser {browser_id} has no post-start audio stop marker"
            )
        stopped = min(stopped_candidates)
        quiescent_candidates = [index for index in audio_quiescent[browser_id] if index >= stopped]
        if not quiescent_candidates:
            _raise_with_sanitized_tail(
                probe, process, f"CEF browser {browser_id} has no post-stop audio quiescent marker"
            )
        quiescent = min(quiescent_candidates)
        closed_candidates = [index for index in browser_closed[browser_id] if index >= quiescent]
        if not closed_candidates:
            _raise_with_sanitized_tail(
                probe, process, f"CEF browser {browser_id} has no post-quiescent browser_closed marker"
            )
        closed = min(closed_candidates)
        observed_candidates = [
            index for index in browser_close_observed[browser_id] if index > started
        ]
        acquired_candidates = [
            index for index in client_keepalive_acquired[browser_id] if index > started
        ]
        detached_candidates = [
            index for index in browser_detached[browser_id] if index > started
        ]
        intent_candidates = [
            index for index in browser_finalization_intent[browser_id] if index > started
        ]
        released_candidates = [
            index for index in client_keepalive_released[browser_id] if index >= closed
        ]
        if (
            not observed_candidates
            or not detached_candidates
            or not intent_candidates
            or not acquired_candidates
            or not released_candidates
        ):
            _raise_with_sanitized_tail(
                probe,
                process,
                f"CEF client keepalive markers missing for browser {browser_id}",
            )
        observed = min(observed_candidates)
        acquired = min(acquired_candidates)
        detached = min(detached_candidates)
        intent = min(intent_candidates)
        released = min(released_candidates)
        if not (
            started < stopped
            and started < acquired <= detached <= observed
            and max(stopped, observed) <= intent <= quiescent < closed < released
        ):
            _raise_with_sanitized_tail(
                probe,
                process,
                f"CEF audio/browser close ordering invalid for browser {browser_id}: "
                f"acquired={acquired} detached={detached} observed={observed} intent={intent} "
                f"started={started} stopped={stopped} "
                f"quiescent={quiescent} closed={closed} released={released}",
            )
    if any("event=cef_shutdown_skipped" in line or "event=timeout" in line for line in lines):
        _raise_with_sanitized_tail(probe, process, "CEF shutdown reported a fail-closed timeout on the healthy path")

    print(
        "PASS: pipe-backed Windows graceful shutdown closed two real CEF browsers "
        "before CefShutdown with an active recording output and no forced kill"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
