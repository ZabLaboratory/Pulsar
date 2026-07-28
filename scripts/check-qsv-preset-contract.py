#!/usr/bin/env python3
"""
QSV preset-property contract check -- hardware-free.

WHAT IT FENCES
--------------
`PULSAR_VIDEO_PRESET` was written to the obs_data key `"preset"` for every
encoder family. obs-qsv11 registers no such key: its knob is `target_usage`
(upstream/plugins/obs-qsv11/obs-qsv11.c, the OBS_COMBO added with TEXT_SPEED),
its values are `TU1..TU7` (QSV_Encoder.h `qsv_usage_names`) and its default is
`TU4`. So a QSV spawn ignored the env var entirely and always ran at TU4 --
silently, since the setter logged nothing and the getter read back `""`.

The fix is a property NAME per family in the boot setter, and a getter that
reads the preset under whichever name the bound encoder uses. Neither half can
be exercised on a machine without an Intel QSV device (scripts/probe-qsv-preset
.py exits 3 there). What CAN be proven anywhere, and is proven here, is that
Pulsar's two halves still agree WITH THE UPSTREAM SOURCE they are derived from:
the property name, the seven values and the default are READ OUT of
obs-qsv11's own source at check time, not restated here.

This is a source-consistency test, not a runtime test. It fails the day someone
edits one side (or upstream moves the knob) without the other.

Usage (repo root, needs the `upstream` submodule checked out):
    python scripts/check-qsv-preset-contract.py
Exit 0 = the four facts agree. 1 = a divergence. 2 = a source file is missing.
"""
from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
QSV_C = REPO_ROOT / "upstream" / "plugins" / "obs-qsv11" / "obs-qsv11.c"
QSV_H = REPO_ROOT / "upstream" / "plugins" / "obs-qsv11" / "QSV_Encoder.h"
SETTER = REPO_ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp"
GETTER = REPO_ROOT / "plugins" / "pulsar-multi-stream" / "src" / "plugin-main.cpp"


def string_list(body: str) -> list[str]:
    """The quoted entries of a C initialiser body, in order."""
    return re.findall(r'"([^"]*)"', body)


def read(path: pathlib.Path) -> str:
    if not path.is_file():
        print(f"error: {path} missing (submodule not checked out?)", file=sys.stderr)
        raise SystemExit(2)
    return path.read_text(encoding="utf-8", errors="replace")


def find(pattern: str, text: str, what: str, problems: list[str]) -> str | None:
    m = re.search(pattern, text)
    if m is None:
        problems.append(f"could not locate {what} -- the source moved, this check is now blind")
        return None
    return m.group(1)


def main() -> int:
    qsv_c, qsv_h = read(QSV_C), read(QSV_H)
    setter, getter = read(SETTER), read(GETTER)
    problems: list[str] = []

    # ---- 1. what obs-qsv11 actually registers ---------------------------
    upstream_prop = find(
        r'obs_properties_add_list\(props,\s*"([^"]+)",\s*TEXT_SPEED',
        qsv_c, "the QSV preset property registration", problems)
    upstream_default = find(
        r'obs_data_set_default_string\(settings,\s*"target_usage",\s*"([^"]+)"\)',
        qsv_c, "the QSV target_usage default", problems)
    usage_body = find(
        r"qsv_usage_names\[\]\s*=\s*\{([^}]*)\}", qsv_h,
        "qsv_usage_names", problems)
    upstream_values = string_list(usage_body) if usage_body else []

    if upstream_prop and upstream_prop != "target_usage":
        problems.append(
            f"upstream now spells the QSV preset knob {upstream_prop!r}, not 'target_usage' -- "
            f"the boot setter writes the stale name")
    if re.search(r'"preset2?"', qsv_c) or re.search(r'"preset2?"', qsv_h):
        problems.append(
            "obs-qsv11 now mentions a \"preset\" key -- the premise of this fix (QSV has none) "
            "no longer holds, re-derive the mapping")

    # ---- 2. the boot setter (pulsar-frontend-stub) ----------------------
    setter_values = string_list(
        find(r"kQsvPresets\[\]\s*=\s*\{([^}]*)\}", setter,
             "the setter's kQsvPresets set", problems) or "")
    setter_values = [v for v in setter_values if v]
    qsv_entry = find(r'family == "qsv"\)\s*return\s*\{([^}]*)\}', setter,
                     "the setter's qsv PresetSet", problems)
    setter_default, setter_prop = (string_list(qsv_entry) + ["", ""])[:2] if qsv_entry else ("", "")

    if setter_values != upstream_values:
        problems.append(
            f"setter kQsv={setter_values} but obs-qsv11 offers {upstream_values} -- "
            f"PULSAR_VIDEO_PRESET would reject a value the encoder accepts, or accept one it does not")
    if setter_prop != upstream_prop:
        problems.append(
            f"setter writes the preset to {setter_prop!r} for qsv, obs-qsv11 reads {upstream_prop!r} "
            f"-- the env var is silently ignored again")
    if setter_default != upstream_default:
        problems.append(
            f"setter's qsv default is {setter_default!r}, obs-qsv11's own default is "
            f"{upstream_default!r} -- an unset PULSAR_VIDEO_PRESET would change the encoder")

    # ---- 3. the getter (pulsar-multi-stream) ----------------------------
    prop_names = string_list(
        find(r"kPresetPropNames\[\]\s*=\s*\{([^}]*)\}", getter,
             "kPresetPropNames", problems) or "")
    if upstream_prop and upstream_prop not in prop_names:
        problems.append(
            f"kPresetPropNames={prop_names} does not carry {upstream_prop!r} -- GetVideoSettings and "
            f"capabilities.encoder_families cannot see the QSV preset")
    if re.search(r'"video_preset",\s*obs_data_get_string\(s,\s*"preset"\)', getter):
        problems.append(
            "on_get_video_settings still reads the hardcoded \"preset\" key -- it reports \"\" for "
            "every QSV spawn, whatever the setter wrote")
    if "applied_preset(s)" not in getter:
        problems.append(
            "on_get_video_settings no longer goes through applied_preset() -- the read side must "
            "iterate kPresetPropNames, not pick one name")

    if problems:
        print("FAIL -- QSV preset contract divergence:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"QSV preset contract OK: property {upstream_prop!r}, values {upstream_values}, "
          f"default {upstream_default!r} -- boot setter, getter and obs-qsv11 agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
