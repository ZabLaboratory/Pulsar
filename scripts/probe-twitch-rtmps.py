#!/usr/bin/env python3
"""Pulsar TLS-ingest probe (ADR 021 palier 1, Pulsar#113).

Proves that the pinned `twitch` destination really opens a TLS session and
that a TLS/certificate failure is loud and FATAL -- i.e. that there is no
silent downgrade to cleartext `rtmp://`, which would put the Twitch stream
key on the wire at every go-live.

Three destinations are started against the real network:

  A  kind=twitch, invalid key
       -> librtmp must connect to the pinned `rtmps://` ingest on 443 and
          get PAST the TLS stage; the connection then fails at the RTMP
          level (Twitch closes on the bad key). Expected log shape:
            "Connecting to RTMP URL rtmps://ingest.global-contribute..."
            "RTMPSockBuf_Fill, remote host closed connection"
          and NO "TLS_Connect failed" / "Cert verify failed".

  B  negative control: rtmps:// aimed at the ingest's PLAIN rtmp port 1935
       -> expected "RTMP_Connect1, TLS_Connect failed: -0x50", connection
          aborted. Proves the TLS stage is genuinely exercised and that
          librtmp does not retry in cleartext.

  C  negative control: rtmps:// to expired.badssl.com
       -> expected "RTMP_Connect1, Cert verify failed: 9 (...expired)",
          connection aborted. Proves mbedTLS runs with
          MBEDTLS_SSL_VERIFY_REQUIRED against the OS CA store.

B and C are what make A's *absence* of TLS errors meaningful.

Usage -- pulsar.exe must already be running with its stdout captured to a
file (pulsar-headless logs to stdout, not to an obs log file), and the host
needs real outbound network:

    pulsar.exe --headless > pulsar.log 2>&1        # in another shell
    python scripts/probe-twitch-rtmps.py --log pulsar.log \
        [ws://127.0.0.1:4455] [password]

With no ws/password argument the endpoint is read from the rundir
obs-websocket config. `--log` is required: the TLS assertions are read off
librtmp's log lines. Exit code 0 = all three expectations held.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import re
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNDIR = REPO_ROOT / "upstream/build_x64/rundir/RelWithDebInfo/bin/64bit"

PINNED_PREFIX = "rtmps://ingest.global-contribute.live-video.net/"

_spec = importlib.util.spec_from_file_location(
    "probe_multi_stream", REPO_ROOT / "scripts" / "probe-multi-stream.py")
_pms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pms)

CASES = [
    ("A-twitch-pinned",
     {"kind": "twitch", "key": "live_000000000_invalidkeyfortlsprobe"},
     "tls-established"),
    ("B-rtmps-on-plain-1935",
     {"kind": "rtmp_custom",
      "url": "rtmps://ingest.global-contribute.live-video.net:1935/app/",
      "key": "k"},
     "tls-handshake-must-fail"),
    ("C-rtmps-expired-cert",
     {"kind": "rtmp_custom", "url": "rtmps://expired.badssl.com:443/app/", "key": "k"},
     "cert-verify-must-fail"),
]


def read_endpoint() -> tuple[str, str]:
    cfg = json.loads((RUNDIR / "obs-websocket" / "config.json").read_text())
    return f"ws://127.0.0.1:{cfg['server_port']}", cfg["server_password"]


def log_since(path: pathlib.Path | None, offset: int) -> tuple[str, int]:
    """Return the log text appended since `offset`, plus the new offset."""
    if not path or not path.exists():
        return "", offset
    data = path.read_bytes()
    return data[offset:].decode("utf-8", errors="replace"), len(data)


async def probe(url: str, password: str, logfile: pathlib.Path) -> int:
    import websockets

    _, offset = log_since(logfile, 0)

    failures: list[str] = []

    async with websockets.connect(url, subprotocols=["obswebsocket.json"]) as ws:
        hello = json.loads(await ws.recv())
        ident = {"rpcVersion": hello["d"]["rpcVersion"], "eventSubscriptions": 0}
        if "authentication" in hello["d"]:
            a = hello["d"]["authentication"]
            ident["authentication"] = _pms.compute_auth(password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": ident}))
        await ws.recv()
        inbox = _pms.Inbox()

        for label, payload, expectation in CASES:
            body = dict(payload)
            body["name"] = label
            created = await _pms.vendor_call(inbox, ws, "CreateDestination", f"mk-{label}", body)
            dest_id = created.get("id")
            if not dest_id:
                failures.append(f"{label}: CreateDestination failed: {created}")
                continue

            listing = await _pms.vendor_call(inbox, ws, "GetDestinations", f"ls-{label}")
            entry = next((d for d in (listing.get("destinations") or [])
                          if d["id"] == dest_id), None)
            pinned = entry["url"] if entry else ""
            print(f"[{label}] url -> {pinned}")

            if label.startswith("A-"):
                if not pinned.startswith(PINNED_PREFIX):
                    failures.append(f"{label}: twitch url not pinned to the secure "
                                    f"global ingest: {pinned!r}")
                if pinned.startswith("rtmp://"):
                    failures.append(f"{label}: twitch url is cleartext rtmp: {pinned!r}")

            _, offset = log_since(logfile, offset)  # drop pre-connect noise
            await _pms.vendor_call(inbox, ws, "StartDestination", f"go-{label}", {"id": dest_id})
            await asyncio.sleep(10)
            chunk, offset = log_since(logfile, offset)

            tls_failed = "TLS_Connect failed" in chunk
            cert_failed = "Cert verify failed" in chunk
            connecting = f"Connecting to RTMP URL {pinned}" in chunk
            cleartext = "Connecting to RTMP URL rtmp://" in chunk

            if cleartext:
                failures.append(f"{label}: a CLEARTEXT rtmp:// connection was attempted")

            if expectation == "tls-established":
                if not connecting:
                    failures.append(f"{label}: no connect attempt to {pinned!r} in the log")
                if tls_failed or cert_failed:
                    failures.append(f"{label}: TLS stage failed against the real ingest "
                                    f"(tls_failed={tls_failed} cert_failed={cert_failed})")
                else:
                    print(f"[{label}] TLS stage passed (no TLS/cert error); "
                          "failure is RTMP-level, as expected for an invalid key")
            elif expectation == "tls-handshake-must-fail":
                if not tls_failed:
                    failures.append(f"{label}: expected a TLS handshake failure and did not "
                                    "see one -- the TLS stage may not be exercised at all")
                else:
                    print(f"[{label}] TLS handshake failed and aborted the connection, "
                          "as expected -- no cleartext retry")
            elif expectation == "cert-verify-must-fail":
                if not cert_failed:
                    failures.append(f"{label}: expected a certificate verification failure "
                                    "-- mbedTLS may not be verifying the peer")
                else:
                    print(f"[{label}] certificate verification failed and aborted the "
                          "connection, as expected")

            after = await _pms.vendor_call(inbox, ws, "GetDestinations", f"ls2-{label}")
            entry = next((d for d in (after.get("destinations") or [])
                          if d["id"] == dest_id), None)
            if entry and entry.get("active"):
                failures.append(f"{label}: destination unexpectedly still active")

            await _pms.vendor_call(inbox, ws, "StopDestination", f"st-{label}", {"id": dest_id})
            await asyncio.sleep(1)
            await _pms.vendor_call(inbox, ws, "RemoveDestination", f"rm-{label}", {"id": dest_id})

    if failures:
        print("\nFAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nOK: twitch ingest is TLS, and TLS/cert failures are fatal (no cleartext path)")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    logpath: pathlib.Path | None = None
    if "--log" in argv:
        i = argv.index("--log")
        try:
            logpath = pathlib.Path(argv[i + 1])
        except IndexError:
            print("error: --log needs a path")
            return 2
        del argv[i:i + 2]
    if logpath is None or not logpath.exists():
        print("error: --log <pulsar stdout capture> is required and must exist; "
              "the TLS assertions are read off librtmp's log lines")
        print("usage: python scripts/probe-twitch-rtmps.py --log pulsar.log "
              "[ws://host:port] [password]")
        return 2

    if len(argv) >= 2:
        url, password = argv[0], argv[1]
    else:
        try:
            url, password = read_endpoint()
        except Exception as exc:  # noqa: BLE001
            print(f"error: cannot read the rundir obs-websocket config: {exc}")
            print("usage: python scripts/probe-twitch-rtmps.py --log pulsar.log "
                  "[ws://host:port] [password]")
            return 2
    print(f"connecting: {url}  (log: {logpath})")
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("error: pip install websockets")
        return 2
    return asyncio.run(probe(url, password, logpath))


if __name__ == "__main__":
    sys.exit(main())
