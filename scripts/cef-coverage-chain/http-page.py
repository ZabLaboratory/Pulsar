"""Loopback-only routes and receipt mailbox for the actual CEF page."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
from datetime import datetime, timezone
from threading import Condition, Thread
from urllib.parse import urlsplit

BASELINE_PRESENTER = "85c49664a029744369b063ddffaa92415a59c8a8"
MAX_POST_BYTES = 8 * 1024 * 1024


class ChainPageServer:
    """Serve pinned local bytes and retain actual frame receipts in memory."""

    def __init__(self, page_root: Path, solar_root: Path, vision_root: Path, profile: dict):
        self.page_root = page_root.resolve(strict=True)
        self.solar_root = solar_root.resolve(strict=True)
        self.vision_root = vision_root.resolve(strict=True)
        self.profile = profile
        baseline = subprocess.check_output(
            [
                "git",
                "-C",
                str(self.vision_root),
                "show",
                f"{BASELINE_PRESENTER}:crates/vision-web/ui/mainpresenter.mjs",
            ],
            text=True,
            encoding="utf-8",
        )
        import_path = 'from "./fonts.mjs"'
        if baseline.count(import_path) != 1:
            raise RuntimeError("pinned old presenter font import changed")
        self.baseline_presenter = baseline.replace(
            import_path, 'from "/vision/ui/fonts.mjs"'
        ).encode("utf-8")
        self.opacity = 0.5
        self.run_legacy_negative = False
        self.frames: dict[str, dict] = {}
        self.events: list[dict] = []
        self.requests: list[dict] = []
        self.condition = Condition()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_type())
        self.server.daemon_threads = True
        self.thread: Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def url(self) -> str:
        suffix = "" if self.profile["name"] == "320x180@30" else f"?profile={self.profile['name']}"
        return f"http://127.0.0.1:{self.port}/{suffix}"

    def start(self) -> None:
        if self.thread is not None:
            raise RuntimeError("page server already started")
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        thread = self.thread
        if thread is None:
            return
        self.server.shutdown()
        self.server.server_close()
        thread.join(timeout=5)
        self.thread = None
        if thread.is_alive():
            raise RuntimeError("page server thread did not stop")

    def set_opacity(self, value: float) -> None:
        if value not in (0.5, 1.0):
            raise ValueError("only the authored 0.5 → 1.0 mask update is admitted")
        with self.condition:
            self.opacity = value
            self.condition.notify_all()

    def request_legacy_negative(self) -> None:
        with self.condition:
            self.run_legacy_negative = True
            self.condition.notify_all()

    def wait_for_frame(self, variant: str, timeout: float = 60.0) -> dict:
        from time import monotonic

        deadline = monotonic() + timeout
        with self.condition:
            while variant not in self.frames:
                error = next((event["error"] for event in self.events if "error" in event), None)
                if error is not None:
                    raise RuntimeError(error)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Vision presenter did not submit {variant} frame")
                self.condition.wait(remaining)
            return self.frames[variant]

    def wait_for_event(self, predicate, timeout: float = 60.0) -> dict:
        from time import monotonic

        deadline = monotonic() + timeout
        with self.condition:
            while True:
                error = next((event["error"] for event in self.events if "error" in event), None)
                if error is not None:
                    raise RuntimeError(error)
                found = next((event for event in self.events if predicate(event)), None)
                if found is not None:
                    return found
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("Vision page did not report the expected event")
                self.condition.wait(remaining)

    def _handler_type(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args):
                return

            def send_bytes(self, status: int, content_type: str, data: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)

            def send_json(self, status: int, value) -> None:
                self.send_bytes(
                    status,
                    "application/json; charset=utf-8",
                    json.dumps(value, separators=(",", ":")).encode("utf-8"),
                )

            def do_GET(self):
                route = urlsplit(self.path).path
                with owner.condition:
                    owner.requests.append({
                        "method": "GET",
                        "path": route,
                        "utc": datetime.now(timezone.utc).isoformat(),
                    })
                if route == "/control":
                    with owner.condition:
                        self.send_json(200, {
                            "opacity": owner.opacity,
                            "runLegacy": owner.run_legacy_negative,
                        })
                    return
                if route == "/events":
                    with owner.condition:
                        self.send_json(200, {"events": owner.events})
                    return
                if route == "/profile.json":
                    self.send_json(200, owner.profile)
                    return
                routes = {
                    "/": (owner.page_root / "index.html", "text/html; charset=utf-8"),
                    "/page.mjs": (owner.page_root / "page.mjs", "text/javascript; charset=utf-8"),
                    "/vision/ui/mainpresenter.mjs": (owner.solar_root / "public/vision/ui/mainpresenter.mjs", "text/javascript; charset=utf-8"),
                    "/vision/ui/fonts.mjs": (owner.solar_root / "public/vision/ui/fonts.mjs", "text/javascript; charset=utf-8"),
                    "/vision/pkg/lumencast_vision_web.js": (owner.solar_root / "public/vision/pkg/lumencast_vision_web.js", "text/javascript; charset=utf-8"),
                    "/vision/pkg/lumencast_vision_web_bg.wasm": (owner.solar_root / "public/vision/pkg/lumencast_vision_web_bg.wasm", "application/wasm"),
                    "/vision-fixture/contribution-scene.lsml": (owner.vision_root / "crates/vision-web/tests/contribution-webgl2-proof/contribution-scene.lsml", "application/json; charset=utf-8"),
                }
                if route == "/baseline/mainpresenter.mjs":
                    self.send_bytes(200, "text/javascript; charset=utf-8", owner.baseline_presenter)
                    return
                selected = routes.get(route)
                if selected is None:
                    self.send_json(404, {"error": "route-not-found", "path": route})
                    return
                path, content_type = selected
                try:
                    data = path.read_bytes()
                except OSError as error:
                    self.send_json(500, {"error": "asset-read-failed", "detail": str(error)})
                    return
                self.send_bytes(200, content_type, data)

            def do_POST(self):
                route = urlsplit(self.path).path
                if route not in ("/frame", "/negative"):
                    self.send_json(404, {"error": "route-not-found", "path": route})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if size < 2 or size > MAX_POST_BYTES:
                        raise ValueError("request-size-out-of-range")
                    body = json.loads(self.rfile.read(size))
                    if route == "/frame":
                        variant = body.get("variant")
                        if variant not in ("initial", "updated"):
                            raise ValueError("unknown-frame-variant")
                        if body.get("profileName") != owner.profile.get("name"):
                            raise ValueError("submitted-frame-profile-mismatch")
                        if body.get("message", {}).get("type") != "frame-submitted":
                            raise ValueError("frame-submitted-required")
                        if body.get("message", {}).get("composition", {}).get("schema") != "vision.native-composition-frame/2":
                            raise ValueError("vision-frame-v2-required")
                        with owner.condition:
                            owner.frames[variant] = body
                            owner.condition.notify_all()
                    else:
                        with owner.condition:
                            owner.events.append(body)
                            owner.condition.notify_all()
                    self.send_json(202, {"accepted": True})
                except Exception as error:
                    self.send_json(400, {"error": str(error)})

        return Handler
