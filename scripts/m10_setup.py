#!/usr/bin/env python3
r"""M10 SETUP leg — create the two OBS ``monitor_capture`` scenes the
Blue-driven program-scene switch flips between, over obs-websocket v5
(ADR 003 §3.1 / §6 criterion 1, issue #60).

Two OBS program scenes, created the proven way
(``probe-twitch-scene-switch.py`` CreateScene/CreateInput plumbing), each
holding **one** ``monitor_capture`` input:

  - ``scene-screen-1`` — ``monitor_capture`` pinned to **display 1**
  - ``scene-screen-2`` — ``monitor_capture`` pinned to **display 2**

The scene NAMES are EXACTLY the closed allowlist the cross-service contract
freezes (``scripts/contracts/scene_control/`` ``DEFAULT_SCENE_ALLOWLIST`` =
``{scene-screen-1, scene-screen-2}``) and that Prism's consumer validates
``target_scene`` against (``Prism/src/main/scene-control/asset-allowlist.ts``
``SCENE_ALLOWLIST``). Contract, Prism and this harness agree case-for-case;
no name is hard-coded in divergence — they are imported from the contract.

--------------------------------------------------------------------------
U1 / spike #56 RESOLVED HERE — the ``monitor_capture`` display-selection
settings on the Pulsar fork.
--------------------------------------------------------------------------
The fork registers TWO sources both with ``.id = "monitor_capture"`` and
picks ONE at module load (``upstream/plugins/win-capture/plugin-main.c:140``):

  - Win8+ with a D3D11 device (every modern Windows box, incl. the
    operator's Win11 and a Win11 CI runner) → the **DXGI duplicator**
    (``duplicator-monitor-capture.c``). Its display key is the **string**
    ``"monitor_id"`` (``duplicator-monitor-capture.c:298,810``), whose value
    is a host-specific device-interface id (``device.DeviceID``, e.g.
    ``\\?\DISPLAY#...#{GUID}``) enumerated at runtime by ``EnumDisplayMonitors``
    (``:727-758``). It is NOT an index and CANNOT be hard-coded — it differs
    per host. Default is the sentinel ``"DUMMY"`` (``:379``).
  - Pre-D3D11 / legacy fallback → the **GDI** source
    (``monitor-capture.c``). Its display key is the **integer** ``"monitor"``
    (``monitor-capture.c:67,210``), a 0-based display index.

So the harness cannot ship a literal display id. It **enumerates** the
available displays over obs-websocket and pins by position:

  1. ``GetInputPropertiesListPropertyItems`` on a throw-away
     ``monitor_capture`` input for the ``"monitor_id"`` property → the list
     of ``{itemName, itemValue}`` the fork populated from the real monitors.
     Item 0 = display 1, item 1 = display 2; pin ``monitor_id`` = the
     itemValue. (Modern DXGI path.)
  2. If that property is absent (legacy GDI build) → fall back to the
     integer ``"monitor"`` key with index 0 / 1.

Both capture sources are pinned to ``method=2`` (``METHOD_WGC``, Windows
Graphics Capture) — the #78 pivot deblock. SPIKE-GPU
(``probe-spike-gpu-coexist.py``, #72/#77) proved WGC renders a **non-black**
plane in a non-interactive / headless agent context, where the DXGI duplicator
returns ``887A0004`` and the frame goes all-black. The display is STILL pinned
by the same ``monitor_id`` device-id string in WGC mode (the method only swaps
the capture backend, not the display selection — see ``SETTING_METHOD`` below).

``GetInputSettings`` after creation confirms the two scenes carry
**distinct** monitor targets (Resolution criterion 1) and reports the stored
capture method (a forced WGC silently downgrades to DXGI without
``wgc_supported``).

Mono-screen fallback (CI / dev box with one display): if fewer than 2
displays are enumerated, both scenes are pinned to the SAME (only) display.
The harness logs this explicitly and does NOT fail — the nominal 2-display
path is still exercised structurally (two scenes, two monitor_capture
inputs, distinct-target assertion is RELAXED to "both created" and the
single-display reuse is reported). The on-air 2-display test is #61's job.

--------------------------------------------------------------------------
F2 / C-FANOUT (ADR 003 Amendment 2 §A2.3) — scene-declaration of the leaf.
--------------------------------------------------------------------------
Orion's ``Inbox.Write`` fans a leaf delta out ONLY to loaded scenes that
DECLARE the path (``Orion/internal/adapters/inbox.go::sceneAcceptsPath`` —
defaults / operator_inputs / bindings.target_paths); an undeclared path is
**silently dropped** and never reaches Prism's ``/show/stream`` subscriber.
So the leaf ``__inputs.blue.m10-scene-control.scene_control`` reaches the
OBS-socket holder (Prism #63) **only if the active Orion scene declares it**.

The OBS ``monitor_capture`` scenes above are a DIFFERENT artefact (an OBS
scene graph, not an Orion show scene) — they cannot declare an Orion leaf.
The declaration is carried by a separate ORION scene, authored exactly as
``m9_setup`` authored its operator-input scene: ``fixtures/m10-orion-scene.lsml.json``
declares ``__inputs.blue.m10-scene-control.scene_control`` as an
``operator_input``. This module:
  - builds + validates that declaration in-process (``build_orion_declaration``,
    a round-trip that proves the path is the canonical 3-segment form and that
    the declared default passes the frozen ``scene_control`` contract), and
  - OPTIONALLY pushes it through the gateway + drives it active when the M9
    Canvas/Orion authoring toolkit (``m8_setup``/``m9_setup``, lands with #53)
    and a reachable gateway are present (``--declare-orion-scene``). The push
    is gated behind that toolkit being importable so this harness runs on
    ``main`` (where the toolkit is not yet merged) without a hard dependency.

The end-to-end "the delta reaches Prism without silent-drop" proof is #61/#63;
#60's F2 obligation is "the scene declares the exact path", satisfied by the
fixture + the in-process round-trip here.

--------------------------------------------------------------------------
Exit codes (probe-family convention)
--------------------------------------------------------------------------
  0  both scenes created, distinct monitor targets confirmed (or mono-screen
     fallback reported), F2 declaration validated.
  1  a hard failure (CreateScene/CreateInput declined, settings not distinct
     on a multi-display box, declaration malformed).
  2  usage / environment error (pulsar.exe missing, bad args).
  3  TYPED SKIP — ``monitor_capture`` not registered (a broken/headless build
     missing win-capture). NOT a pass. (``monitor_capture`` is a
     ``REQUIRED_KINDS_LIGHT`` member — ``probe-source-kinds.py:64`` — so this
     only fires on a genuinely broken bundle, mirroring the scene-switch
     probe's browser_source skip.)

Usage (from the repo root):
    pip install websockets
    # A) spawn pulsar.exe from the built rundir (default):
    python scripts/m10_setup.py
    # B) drive an already-running pulsar.exe (Prism-spawned), reading the
    #    obs-websocket config it wrote:
    python scripts/m10_setup.py --connect
    # C) also push the F2 Orion declaration scene through the gateway:
    python scripts/m10_setup.py --declare-orion-scene \
        --gateway-url http://127.0.0.1:8099
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import pathlib
import secrets as _secrets
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

# Force UTF-8 on stdout/stderr so the harness's '→' / box-drawing diagnostics
# don't crash on the Windows console default cp1252 ('charmap' codec can't
# encode '→'). Same guard the sibling probe uses
# (probe-spike-gpu-coexist.py:111-115).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

try:
    import websockets
except ImportError:
    print("error: pip install websockets", file=sys.stderr)
    sys.exit(2)

# The frozen cross-service contract is the single source of truth for the
# scene NAMES (the obs-ws target_scene allowlist), the canonical leaf path,
# and the leaf-value shape. Import it — never re-declare names in divergence.
_CONTRACTS_DIR = pathlib.Path(__file__).resolve().parent / "contracts"
if str(_CONTRACTS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS_DIR.parent))
from contracts.scene_control import (  # noqa: E402
    DEFAULT_SCENE_ALLOWLIST,
    assert_canonical_leaf_path,
    build_leaf_path,
    validate_scene_control,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_EXE = (
    REPO_ROOT / "upstream" / "build_x64" / "rundir" / "RelWithDebInfo"
    / "bin" / "64bit" / "pulsar.exe"
)
# The obs-websocket config an already-running pulsar.exe writes (the
# `--connect` path reads port + password from here, same file the
# source-kinds probe reads — probe-source-kinds.py:46).
CONNECT_CONFIG_PATH = (
    REPO_ROOT / "upstream" / "build_x64" / "rundir" / "RelWithDebInfo"
    / "bin" / "64bit" / "obs-websocket" / "config.json"
)

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
ORION_SCENE_FIXTURE = FIXTURES_DIR / "m10-orion-scene.lsml.json"

# The blueprint slug the scene-control output maps under (Blue #58 / the
# contract fixture pin this exact slug). One slug → one leaf subtree.
M10_BLUEPRINT_SLUG = "m10-scene-control"
M10_LEAF_PATH = build_leaf_path(M10_BLUEPRINT_SLUG)  # 3-segment, contract-checked

# The two OBS program scenes. Sorted so screen-1 < screen-2 deterministically;
# these are the EXACT contract allowlist members (assert below makes the
# coupling explicit — if the contract ever renames a scene, this fails loudly).
SCENE_SCREEN_1 = "scene-screen-1"
SCENE_SCREEN_2 = "scene-screen-2"
assert {SCENE_SCREEN_1, SCENE_SCREEN_2} == set(DEFAULT_SCENE_ALLOWLIST), (
    "M10 scene names diverged from the frozen scene_control contract allowlist "
    f"{sorted(DEFAULT_SCENE_ALLOWLIST)} — names are an interface, not a local "
    "constant; align with scripts/contracts/scene_control or escalate the drift."
)

MONITOR_CAPTURE_KIND = "monitor_capture"
# The two display-selection setting keys the fork may use (U1 / #56):
#  - DXGI duplicator (modern, Win8+ D3D11): string device id under "monitor_id"
#  - GDI fallback (legacy):                 integer index under "monitor"
SETTING_MONITOR_ID = "monitor_id"   # duplicator-monitor-capture.c:298,810
SETTING_MONITOR_IDX = "monitor"     # monitor-capture.c:67,210

# --------------------------------------------------------------------------
# WGC capture method (the #78 pivot deblock) — force Windows Graphics Capture.
# --------------------------------------------------------------------------
# The duplicator source exposes an INTEGER "method" selector
# (duplicator-monitor-capture.c:65-69 enum METHOD_AUTO=0 / METHOD_DXGI=1 /
# METHOD_WGC=2). SPIKE-GPU (scripts/probe-spike-gpu-coexist.py, #72/#77)
# PROVED that pinning method=2 (WGC) makes monitor_capture render a NON-BLACK
# plane in a NON-INTERACTIVE / headless agent context — exactly where the DXGI
# duplicator's DuplicateOutput1 returns 887A0004 (DXGI_ERROR_UNSUPPORTED) and
# the frame goes all-black. WGC (the WinRT Windows.Graphics.Capture path) does
# not need the interactive desktop session DXGI duplication requires, so it is
# THE deblock that lets this harness (and the end-to-end #75 antenna run) drive
# pulsar.exe from an agent without a logged-in desktop.
#
# The screen is STILL targeted by the same "monitor_id" device-id string that
# U1's enumerate_monitors resolves: in WGC mode the source resolves the target
# HMONITOR from that same "monitor_id" find_monitor uses for DXGI
# (update_settings:298-306 → video_tick → winrt_capture_init_monitor) — there
# is no separate WGC monitor key. So forcing method=2 changes ONLY the capture
# backend, never the display selection. (See the METHOD_* note in
# probe-spike-gpu-coexist.py:147-156.)
#
# Forced (not AUTO): METHOD_AUTO would let the fork pick the DXGI duplicator on
# a D3D11 box (choose_method), reintroducing the headless black-frame. WGC is
# only honoured if the fork reports wgc_supported (a forced WGC silently
# downgrades to DXGI otherwise — choose_method:250-254); the on-air #75 run
# reads the log's "method:" line to confirm which libobs actually applied.
SETTING_METHOD = "method"           # duplicator-monitor-capture.c:65-69,378
METHOD_WGC = 2                       # enum METHOD_WGC (Windows Graphics Capture)

READY_TIMEOUT_S = 60.0
SHUTDOWN_GRACE_S = 8.0
EVENT_SUBSCRIPTION_ALL = 0x7FF
CANVAS_W = 1920
CANVAS_H = 1080
RESOURCE_ALREADY_EXISTS = 601  # obs-websocket: ResourceAlreadyExists


# --------------------------------------------------------------------------
# Process management — spawn/ready/reap, mirrors probe-twitch-scene-switch.py.
# --------------------------------------------------------------------------
import re  # noqa: E402

READY_RE = re.compile(r"^PULSAR_READY ws=(\S+) password=(\S+)$")


class PulsarProcess:
    """Spawn pulsar.exe, capture the PULSAR_READY line (ws url + per-session
    password), reap on shutdown. Verbatim lifecycle from
    ``probe-twitch-scene-switch.py:195-304`` — kept local so this harness has
    no cross-probe import dependency."""

    def __init__(self, exe: pathlib.Path, port: int, password: str) -> None:
        self.exe = exe
        self.port = port
        self.password = password
        self.proc: Optional[subprocess.Popen] = None
        self._lines: list[str] = []
        self._ready_event = threading.Event()
        self._ready_match: Optional[re.Match[str]] = None

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PULSAR_PORT"] = str(self.port)
        env["PULSAR_PASSWORD"] = self.password
        env["PULSAR_RESOLUTION"] = f"{CANVAS_W}x{CANVAS_H}"
        # No window/mic/process-audio capture wired — setup-only, never live.
        env.pop("PULSAR_CAPTURE_WINDOW", None)
        env.pop("PULSAR_MIC_DEVICE_ID", None)
        env.pop("PULSAR_PROCESS_AUDIO_NAME", None)

        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [str(self.exe), "--disable-gpu", "--no-sandbox"],
            cwd=str(self.exe.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip("\r\n")
            self._lines.append(line)
            m = READY_RE.match(line)
            if m is not None and not self._ready_event.is_set():
                self._ready_match = m
                self._ready_event.set()

    def wait_ready(self, timeout: float) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        while True:
            if self._ready_event.wait(timeout=0.2):
                m = self._ready_match
                assert m is not None
                return m.group(1), m.group(2)
            assert self.proc is not None
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"pulsar.exe exited (code {self.proc.returncode}) before READY.\n"
                    + self._diag()
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"pulsar.exe did not signal READY within {timeout:.0f}s.\n"
                    + self._diag()
                )

    @property
    def lines(self) -> list[str]:
        return list(self._lines)

    def _diag(self) -> str:
        tail = self._lines[-40:]
        return "---- pulsar stdout (last 40) ----\n" + "\n".join(tail) + "\n----"

    def shutdown(self, grace: float = SHUTDOWN_GRACE_S) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=grace)
            return
        except Exception:
            pass
        try:
            self.proc.kill()
            self.proc.wait(timeout=grace)
        except Exception:
            pass


# --------------------------------------------------------------------------
# obs-websocket v5 plumbing — mirrors probe-twitch-scene-switch.py:310-381.
# --------------------------------------------------------------------------
def compute_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


class Inbox:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.responses: list[dict] = []

    async def pump(self, ws, until: Callable[["Inbox"], bool], timeout: float) -> None:
        end = asyncio.get_event_loop().time() + timeout
        while not until(self):
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            msg = json.loads(raw)
            op = msg.get("op")
            if op == 5:
                self.events.append(msg["d"])
            elif op == 7:
                self.responses.append(msg["d"])


async def request(
    inbox: Inbox, ws, request_type: str, request_id: str,
    data: dict | None = None, timeout: float = 30.0,
) -> dict:
    body: dict = {"requestType": request_type, "requestId": request_id}
    if data is not None:
        body["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": body}))

    def has_response(ix: Inbox) -> bool:
        return any(r["requestId"] == request_id for r in ix.responses)

    await inbox.pump(ws, has_response, timeout)
    for i, r in enumerate(inbox.responses):
        if r["requestId"] == request_id:
            return inbox.responses.pop(i)
    raise RuntimeError("unreachable")


def req_ok(resp: dict) -> bool:
    return bool(resp.get("requestStatus", {}).get("result"))


def req_code(resp: dict) -> Optional[int]:
    return resp.get("requestStatus", {}).get("code")


# --------------------------------------------------------------------------
# F2 — Orion scene-declaration of the scene_control leaf (in-process proof).
# --------------------------------------------------------------------------
def build_orion_declaration() -> dict[str, Any]:
    """Load the F2 Orion scene fixture, prove it declares the canonical
    ``scene_control`` leaf path AND that its declared default is a valid
    ``scene_control`` value, and return the (validated) fixture.

    This is the #60 F2 obligation made testable WITHOUT a live wire: the
    scene declares the exact 3-segment path Orion's ``sceneAcceptsPath``
    must match, and the default it seeds is contract-conformant so Orion's
    boot seed is itself a legal payload. Raises on any drift."""
    bundle = json.loads(ORION_SCENE_FIXTURE.read_text(encoding="utf-8"))
    decls = [
        oi for oi in bundle.get("operator_inputs", [])
        if isinstance(oi, dict) and oi.get("path") == M10_LEAF_PATH
    ]
    if not decls:
        raise SystemExit(
            f"F2 FAIL: {ORION_SCENE_FIXTURE.name} declares no operator_input for "
            f"{M10_LEAF_PATH!r} — Orion's sceneAcceptsPath would silent-drop the "
            "Blue scene_control delta (C-FANOUT)."
        )
    # The path is the canonical 3-segment form (C-PATHREAL) — assert via the
    # contract so the fixture cannot drift to the F1 2-segment bug.
    slug = assert_canonical_leaf_path(M10_LEAF_PATH)
    if slug != M10_BLUEPRINT_SLUG:
        raise SystemExit(
            f"F2 FAIL: declared slug {slug!r} != blueprint slug "
            f"{M10_BLUEPRINT_SLUG!r}"
        )
    # The seeded default must itself be a legal scene_control value — Orion
    # seeds it on boot and it must round-trip through the consumer guard.
    default = decls[0].get("default")
    validate_scene_control(default)  # raises if the boot seed is malformed
    return bundle


# --------------------------------------------------------------------------
# The obs-ws scene-setup leg.
# --------------------------------------------------------------------------
async def enumerate_monitors(inbox: Inbox, ws, log) -> tuple[str, list[Any]]:
    """Resolve U1 (#56): return ``(setting_key, [values])`` to pin displays.

    Creates a throw-away ``monitor_capture`` input, asks the fork which
    ``monitor_id`` values it populated from the real monitors via
    ``GetInputPropertiesListPropertyItems``, removes the probe input, and
    returns the ordered list of device-id values (item 0 = display 1, …).
    Falls back to the legacy integer ``monitor`` index when the modern
    ``monitor_id`` property is absent (GDI build)."""
    probe_scene = "m10-monitor-enum-scene"
    probe_input = "m10-monitor-enum"
    r = await request(inbox, ws, "CreateScene", "enum-cs", {"sceneName": probe_scene})
    if not req_ok(r) and req_code(r) != RESOURCE_ALREADY_EXISTS:
        raise RuntimeError(f"CreateScene(enum) failed: {r.get('requestStatus')}")
    try:
        r = await request(inbox, ws, "CreateInput", "enum-ci", {
            "sceneName": probe_scene,
            "inputName": probe_input,
            "inputKind": MONITOR_CAPTURE_KIND,
            "inputSettings": {},
            "sceneItemEnabled": False,
        })
        if not req_ok(r) and req_code(r) != RESOURCE_ALREADY_EXISTS:
            raise RuntimeError(
                f"CreateInput(monitor_capture, enum) failed: {r.get('requestStatus')}"
            )
        # Try the modern DXGI duplicator key first.
        r = await request(inbox, ws, "GetInputPropertiesListPropertyItems", "enum-items", {
            "inputName": probe_input,
            "propertyName": SETTING_MONITOR_ID,
        })
        if req_ok(r):
            items = r["responseData"].get("propertyItems", [])
            # Drop the disabled "Select a display" sentinel (itemValue=="DUMMY").
            values = [
                it["itemValue"] for it in items
                if isinstance(it, dict) and it.get("itemEnabled", True)
                and it.get("itemValue") not in (None, "DUMMY")
            ]
            log(f"   U1: '{SETTING_MONITOR_ID}' property → {len(values)} display(s) "
                f"(DXGI duplicator path; device-id strings)")
            return SETTING_MONITOR_ID, values
        # Legacy GDI fallback: integer index property.
        r = await request(inbox, ws, "GetInputPropertiesListPropertyItems", "enum-items-gdi", {
            "inputName": probe_input,
            "propertyName": SETTING_MONITOR_IDX,
        })
        if req_ok(r):
            items = r["responseData"].get("propertyItems", [])
            values = [
                it["itemValue"] for it in items
                if isinstance(it, dict) and it.get("itemEnabled", True)
            ]
            log(f"   U1: '{SETTING_MONITOR_IDX}' property → {len(values)} display(s) "
                f"(legacy GDI path; integer indices)")
            return SETTING_MONITOR_IDX, values
        raise RuntimeError(
            "neither 'monitor_id' nor 'monitor' is a list property on "
            "monitor_capture — unexpected fork build; cannot resolve U1"
        )
    finally:
        await request(inbox, ws, "RemoveInput", "enum-ri", {"inputName": probe_input})
        await request(inbox, ws, "RemoveScene", "enum-rs", {"sceneName": probe_scene})


async def create_monitor_scene(
    inbox: Inbox, ws, *, scene_name: str, input_name: str,
    setting_key: str, setting_value: Any, log,
) -> dict[str, Any]:
    """Create ``scene_name`` (idempotent — tolerate 601) with one
    ``monitor_capture`` input pinned to ``setting_value`` under
    ``setting_key``, then read the settings back. Returns the read-back
    ``inputSettings`` so the caller can assert distinct targets."""
    r = await request(inbox, ws, "CreateScene", f"cs-{scene_name}",
                      {"sceneName": scene_name})
    if not req_ok(r) and req_code(r) != RESOURCE_ALREADY_EXISTS:
        raise RuntimeError(f"CreateScene({scene_name}) failed: {r.get('requestStatus')}")
    log(f"   scene {scene_name!r} ready"
        f"{' (already existed)' if req_code(r) == RESOURCE_ALREADY_EXISTS else ''}")

    # Force WGC (method=2): non-black in headless/non-interactive context where
    # the DXGI duplicator returns 887A0004 (SPIKE-GPU, #72/#77). `setting_key`
    # (monitor_id / monitor) still pins the display in WGC mode — the method only
    # swaps the capture backend (see SETTING_METHOD note above).
    settings = {
        setting_key: setting_value,
        SETTING_METHOD: METHOD_WGC,
        "capture_cursor": True,
    }
    r = await request(inbox, ws, "CreateInput", f"ci-{scene_name}", {
        "sceneName": scene_name,
        "inputName": input_name,
        "inputKind": MONITOR_CAPTURE_KIND,
        "inputSettings": settings,
        "sceneItemEnabled": True,
    })
    if not req_ok(r):
        if req_code(r) == RESOURCE_ALREADY_EXISTS:
            # Idempotent re-run: the input exists; re-pin its settings so a
            # second run still converges on the intended display.
            log(f"   input {input_name!r} exists — re-pinning {setting_key}")
            r = await request(inbox, ws, "SetInputSettings", f"sis-{scene_name}", {
                "inputName": input_name,
                "inputSettings": settings,
                "overlay": True,
            })
            if not req_ok(r):
                raise RuntimeError(
                    f"SetInputSettings({input_name}) failed: {r.get('requestStatus')}"
                )
        else:
            raise RuntimeError(
                f"CreateInput({input_name}, monitor_capture) failed: "
                f"{r.get('requestStatus')}"
            )
    log(f"   input {input_name!r} → {setting_key}={setting_value!r} "
        f"{SETTING_METHOD}={METHOD_WGC} (WGC — headless non-black, SPIKE-GPU)")

    r = await request(inbox, ws, "GetInputSettings", f"gis-{scene_name}",
                      {"inputName": input_name})
    if not req_ok(r):
        raise RuntimeError(f"GetInputSettings({input_name}) failed: {r.get('requestStatus')}")
    return r["responseData"].get("inputSettings", {})


async def run_obs_setup(url: str, password: str) -> int:
    print(f"connecting: {url}")
    async with websockets.connect(
        url, subprotocols=["obswebsocket.json"], max_size=2**24,
        ping_interval=None, close_timeout=15, open_timeout=10,
    ) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if hello.get("op") != 0:
            print(f"error: expected Hello (op=0), got {hello}")
            return 1
        identify_d: dict = {
            "rpcVersion": hello["d"]["rpcVersion"],
            "eventSubscriptions": EVENT_SUBSCRIPTION_ALL,
        }
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            identify_d["authentication"] = compute_auth(
                password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": identify_d}))
        ident = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if ident.get("op") != 2:
            print(f"error: identify failed: {ident}")
            return 1
        print("identified (v5 auth OK)")

        inbox = Inbox()

        # TYPED SKIP guard: monitor_capture must be registered.
        resp = await request(inbox, ws, "GetInputKindList", "kinds", {})
        kinds = set(resp["responseData"]["inputKinds"])
        if MONITOR_CAPTURE_KIND not in kinds:
            print(f"SKIP: {MONITOR_CAPTURE_KIND} NOT registered — broken/headless "
                  "build missing win-capture. Typed skip, NOT a pass.")
            return 3
        print(f"{MONITOR_CAPTURE_KIND} registered ({len(kinds)} input kinds total)")

        # U1 / #56: resolve the display-selection settings + enumerate displays.
        print("[U1] enumerating displays for monitor_capture selection ...")
        setting_key, values = await enumerate_monitors(inbox, ws, print)
        if not values:
            print("FAIL: no displays enumerated — cannot pin monitor_capture")
            return 1
        n = len(values)
        print(f"   enumerated {n} display(s): {values!r}")

        # Pin display 1 / display 2. Mono-screen fallback: reuse the only one.
        value_1 = values[0]
        if n >= 2:
            value_2 = values[1]
            mono = False
        else:
            value_2 = values[0]
            mono = True
            print("   NOTE: single display present — mono-screen fallback: both "
                  "scenes pin the same display. Nominal 2-display path requires a "
                  "2nd monitor (on-air test is #61).")

        # Create the two scenes.
        print(f"[S1] creating {SCENE_SCREEN_1!r} (display 1) ...")
        settings_1 = await create_monitor_scene(
            inbox, ws, scene_name=SCENE_SCREEN_1, input_name="capture-screen-1",
            setting_key=setting_key, setting_value=value_1, log=print)
        print(f"[S2] creating {SCENE_SCREEN_2!r} (display 2) ...")
        settings_2 = await create_monitor_scene(
            inbox, ws, scene_name=SCENE_SCREEN_2, input_name="capture-screen-2",
            setting_key=setting_key, setting_value=value_2, log=print)

        # Criterion 1: GetInputSettings confirms distinct monitor targets.
        target_1 = settings_1.get(setting_key)
        target_2 = settings_2.get(setting_key)
        print(f"[ASSERT] {setting_key}: screen-1={target_1!r}  screen-2={target_2!r}")
        # Report the capture method actually stored on each source. A forced WGC
        # silently downgrades to DXGI if the fork lacks wgc_supported; surfacing
        # the read-back value lets the #75 antenna run spot a downgrade (which
        # would reintroduce the headless black-frame).
        method_1 = settings_1.get(SETTING_METHOD)
        method_2 = settings_2.get(SETTING_METHOD)
        print(f"[ASSERT] {SETTING_METHOD}: screen-1={method_1!r}  screen-2={method_2!r} "
              f"(requested WGC={METHOD_WGC}; the log 'method:' line states which "
              "libobs applied)")
        if METHOD_WGC not in (method_1, method_2):
            print("   NOTE: neither source read back method=2 — the fork may have "
                  "downgraded WGC to DXGI (no wgc_supported) or omitted the key; "
                  "headless capture may be black. Check the log 'method:' line.")
        if not mono:
            if target_1 == target_2:
                print("FAIL: the two scenes pin the SAME monitor on a multi-display "
                      "box — they must target distinct displays (criterion 1).")
                return 1
            print("   distinct monitor targets confirmed (criterion 1 OK)")
        else:
            print("   mono-screen: distinct-target assertion RELAXED (one display); "
                  "both scenes created + pinned (degraded-but-valid harness state).")

        # Confirm both scenes are present in the OBS scene list.
        resp = await request(inbox, ws, "GetSceneList", "scenes", {})
        names = {s["sceneName"] for s in resp["responseData"].get("scenes", [])}
        missing = {SCENE_SCREEN_1, SCENE_SCREEN_2} - names
        if missing:
            print(f"FAIL: scenes missing after setup: {sorted(missing)}")
            return 1
        print(f"[OK] both scenes present in OBS: {sorted({SCENE_SCREEN_1, SCENE_SCREEN_2})}")
        return 0


# --------------------------------------------------------------------------
# OPTIONAL F2 push leg — author the Orion declaration scene through the
# gateway. Gated behind the m9 Canvas/Orion toolkit being importable so this
# harness has NO hard dependency on the (unmerged) #53 toolkit.
# --------------------------------------------------------------------------
def declare_orion_scene_via_gateway(*, gateway_url: str, log) -> int:
    """Push fixtures/m10-orion-scene.lsml.json through Orion and drive it
    active, reusing the m9_setup authoring toolkit. Returns 0 on success,
    2 if the toolkit/gateway is unavailable (soft-skip), 1 on a hard push
    failure."""
    try:
        import m9_setup  # type: ignore[import-not-found]  # noqa: F401 — lands with #53; soft dep
        from m8_setup import GatewayClient, hash_bundle  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        log(f"[F2-push] SKIP: m9 Canvas/Orion toolkit not importable ({exc!r}). "
            "The F2 declaration fixture is validated in-process; the gateway push "
            "lands once #53 (m8_setup/m9_setup) is merged. Not a failure of #60.")
        return 2

    operator_token = os.environ.get("M8_OPERATOR_TOKEN", "").strip()
    if not operator_token:
        log("[F2-push] SKIP: M8_OPERATOR_TOKEN unset (etage-1 admin JWT). "
            "Cannot author the Orion scene without it.")
        return 2

    bundle = json.loads(ORION_SCENE_FIXTURE.read_text(encoding="utf-8"))
    h = hash_bundle(bundle)
    bundle["scene_version"] = "sha256:" + h
    client = GatewayClient(
        base_url=gateway_url, operator_token=operator_token,
        secrets=[operator_token],
    )
    log(f"[F2-push] storing Orion declaration scene (H={h}) ...")
    client.put_lsml_bundle(bundle, h)
    scene_id = client.ensure_scene("Pulsar M10 — scene-control wire (F2 declaration)")
    definition_id = client.save_definition(scene_id, h, [])
    push = client.push_definition(scene_id, definition_id, h)
    log(f"[F2-push] pushed scene_version={push.get('scene_version')}")
    client.set_active_scene(scene_id)
    show = client.get_show()
    if show.get("active_scene_id") != scene_id:
        log(f"[F2-push] FAIL: active_scene_id={show.get('active_scene_id')!r} "
            f"!= {scene_id!r}")
        return 1
    log(f"[F2-push] OK: scene {scene_id} active, declares {M10_LEAF_PATH}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulsar M10 scene-setup harness")
    ap.add_argument("--exe", type=pathlib.Path,
                    default=pathlib.Path(os.environ.get("PULSAR_EXE", str(DEFAULT_EXE))),
                    help="path to pulsar.exe (default: built rundir)")
    ap.add_argument("--connect", action="store_true",
                    help="connect to an already-running pulsar.exe (read port + "
                         "password from its obs-websocket config) instead of spawning")
    ap.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S)
    ap.add_argument("--declare-orion-scene", action="store_true",
                    help="also push the F2 Orion declaration scene through the "
                         "gateway (needs the #53 m8/m9 toolkit + M8_OPERATOR_TOKEN)")
    ap.add_argument("--gateway-url", default=os.environ.get("M8_GATEWAY_URL", ""),
                    help="gateway base URL for --declare-orion-scene")
    args = ap.parse_args()

    # F2 (always): validate the Orion declaration fixture in-process. This is
    # the #60 obligation that the active M10 scene declares the exact path —
    # provable without a live wire.
    print("[F2] validating Orion scene-control declaration "
          f"(declares {M10_LEAF_PATH}) ...")
    try:
        build_orion_declaration()
    except SystemExit as exc:
        print(str(exc))
        return 1
    print("   F2 OK: fixture declares the canonical leaf path; seeded default is a "
          "valid scene_control value (round-trip clean).")

    # Resolve the obs-websocket endpoint.
    if args.connect:
        if not CONNECT_CONFIG_PATH.exists():
            print(f"error: {CONNECT_CONFIG_PATH} missing — start pulsar.exe first "
                  "(or drop --connect to spawn it).")
            return 2
        cfg = json.loads(CONNECT_CONFIG_PATH.read_text(encoding="utf-8"))
        port = cfg.get("server_port", 4455)
        password = cfg.get("server_password", "")
        ws_url = f"ws://127.0.0.1:{port}"
        print(f"connecting to running pulsar.exe: {ws_url}")
        rc = asyncio.run(run_obs_setup(ws_url, password))
        pulsar = None
    else:
        exe: pathlib.Path = args.exe
        if not exe.exists():
            print(f"error: pulsar.exe not found at {exe}")
            print("Build it first: scripts/build-win.ps1 -Full  (or pass --connect "
                  "to drive a running instance).")
            return 2
        port = _free_port()
        password = _secrets.token_urlsafe(16)
        print(f"spawning: {exe}")
        print(f"  PULSAR_PORT={port}  PULSAR_PASSWORD=<redacted {len(password)} chars>")
        pulsar = PulsarProcess(exe, port, password)
        rc = 1
        try:
            pulsar.spawn()
            ws_url, sentinel_pw = pulsar.wait_ready(args.ready_timeout)
            print(f"READY: {ws_url}")
            rc = asyncio.run(run_obs_setup(ws_url, sentinel_pw))
        except KeyboardInterrupt:
            print("interrupted")
            rc = 130
        except Exception as exc:  # noqa: BLE001 — top-level harness diagnostic
            print(f"FAIL: {exc}")
            if pulsar.proc is not None:
                print(pulsar._diag())  # noqa: SLF001
            rc = 1
        finally:
            pulsar.shutdown()
            if pulsar.proc is not None and pulsar.proc.poll() is None:
                print("error: pulsar.exe still running after shutdown")
                rc = rc or 1
            else:
                print("pulsar.exe reaped cleanly")

    # Optional F2 gateway push (only if the obs leg succeeded).
    if rc == 0 and args.declare_orion_scene:
        gw = args.gateway_url.strip()
        if not gw:
            print("[F2-push] SKIP: --declare-orion-scene set but no --gateway-url "
                  "/ M8_GATEWAY_URL provided.")
        else:
            push_rc = declare_orion_scene_via_gateway(gateway_url=gw, log=print)
            if push_rc == 1:
                rc = 1  # a hard push failure fails the run

    print("PASS" if rc == 0 else (f"SKIPPED (exit {rc})" if rc == 3 else f"FAIL (exit {rc})"))
    return rc


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    sys.exit(main())
