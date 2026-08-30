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
import importlib.util
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = ROOT / "scripts" / "probe-dual-lane.py"

# A short looped WAV keeps this integration proof self-contained while making
# reroute_audio exercise CEF's real audio handler.  The source is intentionally
# tiny and synthetic; the handler lifecycle, not audible output, is the contract
# under test.
AUDIO_WAV_DATA_URI = (
    "data:audio/wav;base64,"
    "UklGRuQDAABXQVZFZm10IBAAAAABAAEAgLsAAAB3AQACABAAZGF0YcADAAAAAFwAtwATAW0BxgEdAnMCxwIYA2cDswP7A0EEggTABPoEMAVhBY0FtQXYBfYFDwYjBjIGOwY/Bj4GOAYsBhsGBQbpBckFpAV6BUsFGAXgBKUEZQQiBNsDkQNDA/QCoQJMAvYBngFEAekAjgAyANf/e/8f/8T+av4S/rv9Zv0U/cT8dvws/OX7ofth+yX77fq6+or6YPo6+hn6/vnn+db5yfnC+cH5xPnN+dv57/kH+iX6R/pv+pv6zPoB+zr7ePu5+/77R/yS/OH8Mv2F/dv9Mv6L/uX+QP+c//j/UwCvAAoBZQG+ARYCbAK/AhEDYAOsA/UDOwR9BLsE9QQrBVwFiQWyBdUF9AUNBiIGMQY7Bj8GPgY4Bi0GHQYHBuwFzAWnBX4FTwUdBeUEqgRrBCgE4QOYA0sD+wKpAlQC/gGmAUwB8gCWADoA3/+D/yf/zP5z/hr+w/1u/Rv9y/x9/DL86/un+2f7Kvvy+r76j/pk+j76HPoA+un51/nK+cP5wfnE+cz52vnt+QX6IvpE+mv6l/rH+vz6Nfty+7P7+PtA/Iv82fwq/X390/0q/oP+3f44/5T/8P9LAKcAAgFdAbYBDgJkArgCCgNZA6UD7gM0BHcEtQTwBCYFWAWGBa4F0gXxBQsGIAYwBjoGPwY/BjkGLgYeBgkG7wXPBasFggVUBSEF6wSwBHEELgToA54DUgMCA7ACXAIGAq4BVAH6AJ4AQwDn/4v/MP/V/nv+Iv7L/Xb9I/3S/IT8Ofzx+637bPsw+/f6w/qT+mf6Qfof+gL66/nY+cv5w/nA+cP5y/nY+ev5Avof+kH6Z/qT+sP69/ow+2z7rfvx+zn8hPzS/CP9dv3L/SL+e/7V/jD/i//n/0MAngD6AFQBrgEGAlwCsAICA1IDngPoAy4EcQSwBOsEIQVUBYIFqwXPBe8FCQYeBi4GOQY/Bj8GOgYwBiAGCwbxBdIFrgWGBVgFJgXwBLUEdwQ0BO4DpQNZAwoDuAJkAg4CtgFdAQIBpwBLAPD/lP84/93+g/4q/tP9ff0q/dn8i/xA/Pj7s/ty+zX7/PrH+pf6a/pE+iL6Bfrt+dr5zPnE+cH5w/nK+df56fkA+hz6Pvpk+o/6vvry+ir7Z/un++v7Mvx9/Mv8G/1u/cP9Gv5z/sz+J/+D/9//OgCWAPIATAGmAf4BVAKpAvsCSwOYA+EDKARrBKoE5QQdBU8FfgWnBcwF7AUHBh0GLQY4Bj4GPwY7BjEGIgYNBvQF1QWyBYkFXAUrBfUEuwR9BDsE9QM="
)


def _cef_audio_data_url(lane: str) -> str:
    html = f"""<!doctype html><meta charset='utf-8'>
<style>html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#132238;color:#f6fbff;font:48px Arial}}
main{{height:100%;display:grid;place-items:center}} .tile{{padding:40px;background:{'#ff3da6' if lane == 'A' else '#38e8ff'}}}</style>
<main><div class='tile'>PULSAR CEF / LANE {lane}</div></main>
<audio autoplay loop src='{AUDIO_WAV_DATA_URI}'></audio>
<script>const a=document.querySelector('audio'); a.play().catch(()=>{{}});</script>"""
    return "data:text/html;charset=utf-8," + quote(html, safe="")


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


async def _configure_two_browser_sources(probe, ws_url: str, password: str, process) -> None:
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
                        "url": _cef_audio_data_url(lane),
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
    process = probe.PulsarProcess(
        executable.resolve(),
        "x264",
        record_dir,
        trace_path=None,
        runtime_id=runtime_id,
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
        asyncio.run(_configure_two_browser_sources(probe, ready.group(1), process.password, process))
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
    control_lines = [line for line in lines if line.startswith("PULSAR_SHUTDOWN_CONTROL")]
    if any(
        "PULSAR_SHUTDOWN_EVENT_HANDLE" in line or "handle" in line.lower()
        for line in control_lines
    ):
        _raise_with_sanitized_tail(probe, process, "shutdown handle or its value was logged")
    if process.forced_kill_used:
        _raise_with_sanitized_tail(probe, process, "forced process kill was used")
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
    browser_closed = {}
    for index, line in enumerate(lines):
        for event, destination in (
            ("audio_stream_started", audio_started),
            ("audio_stream_stopped", audio_stopped),
            ("audio_quiescent", audio_quiescent),
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
    ):
        _raise_with_sanitized_tail(
            probe,
            process,
            "CEF audio lifecycle did not reach exactly two stopped/quiescent browser IDs: "
            f"started={sorted(audio_started)} stopped={sorted(audio_stopped)} "
            f"quiescent={sorted(audio_quiescent)}",
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
        if not started < stopped <= quiescent < closed:
            _raise_with_sanitized_tail(
                probe,
                process,
                f"CEF audio/browser close ordering invalid for browser {browser_id}: "
                f"started={started} stopped={stopped} quiescent={quiescent} closed={closed}",
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
