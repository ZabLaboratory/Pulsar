#!/usr/bin/env python3
"""Emit a redacted copy of a pulsar-websocket config.json.

The pulsar-websocket plugin drops
`<rundir>/bin/64bit/obs-websocket/config.json` on boot, carrying
`server_password` IN CLEAR. That file is genuinely useful for failure
triage -- but the password never is, and CI artefacts are downloadable
by anyone with read access to the repo for the whole retention window.

So: deny by default. Only keys on SAFE_KEYS keep their value; every
other key is replaced by a presence marker (`<redacted:set>` /
`<redacted:empty>`). A key added by a future obs-websocket version
lands redacted until someone vets it here, rather than leaking.

What triage still gets, which is all it ever needed:
  - did pulsar drop a config at all (file present / absent),
  - which port the WS server bound (`server_port`),
  - whether auth was on (`auth_required`) and whether a password was
    actually seeded -- `probe-twitch-live.py` waits for a NON-EMPTY
    password, so "present but empty" is a real, diagnosable failure.

Usage:
    python scripts/redact-websocket-config.py <src.json> <dst.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Non-sensitive obs-websocket keys, kept verbatim. Anything absent from
# this set is redacted -- add a key here only after checking it cannot
# carry a credential.
SAFE_KEYS = frozenset(
    {
        "alerts_enabled",
        "auth_required",
        "first_load",
        "server_enabled",
        "server_port",
    }
)

REDACTED_SET = "<redacted:set>"
REDACTED_EMPTY = "<redacted:empty>"


def redact(cfg: dict[str, Any]) -> dict[str, Any]:
    """Keep SAFE_KEYS verbatim, reduce every other key to a presence marker."""
    return {
        key: (value if key in SAFE_KEYS else (REDACTED_SET if value else REDACTED_EMPTY))
        for key, value in cfg.items()
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <src.json> <dst.json>", file=sys.stderr)
        return 2

    src, dst = Path(argv[1]), Path(argv[2])
    try:
        cfg = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Never fall back to copying the raw file: a config we cannot
        # parse is exactly the case where we must not guess.
        print(f"error: cannot read/parse {src}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(cfg, dict):
        print(f"error: {src} is not a JSON object", file=sys.stderr)
        return 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        json.dumps(redact(cfg), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    kept = sorted(k for k in cfg if k in SAFE_KEYS)
    hidden = sorted(k for k in cfg if k not in SAFE_KEYS)
    print(f"redacted {src} -> {dst} (kept={kept} redacted={hidden})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
