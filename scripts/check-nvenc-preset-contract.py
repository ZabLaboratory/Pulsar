#!/usr/bin/env python3
"""
NVENC preset-property contract check -- hardware-free.

WHAT IT FENCES
--------------
Sibling of scripts/check-qsv-preset-contract.py (#150), same class of bug one
level down: a preset written under a key the encoder does not read.

QSV was a per-FAMILY mismatch. NVENC is a per-ID one -- the name is not even
uniform inside the family. resolveEncoderId's nvenc preference list holds three
ids spanning two spellings of the same knob:

    obs_nvenc_h264_tex   31.0+ encoder, knob "preset"   (nvenc-properties.c)
    jim_nvenc            pre-31.0 compat shim, "preset2" (nvenc-compat.c)
    ffmpeg_nvenc         the same compat object re-registered under the old id

Values (p1..p7) and default (p5) are identical on both sides, so ONLY the name
differs -- which is what made it silent. And writing "preset" to a compat shim
is worse than a no-op: migrate_settings() copies "preset2" OVER "preset" before
rerouting, so the encoder ran at preset2's own default p5 whatever
PULSAR_VIDEO_PRESET said. That hit jim_nvenc, the id resolveEncoderId tries
FIRST -- so this was the live path on any pre-31.0-compat build, not a corner.

As with the QSV check this is a SOURCE-CONSISTENCY test, not a runtime one: no
NVIDIA GPU is required or used. Every upstream fact below is read out of
obs-nvenc's own source at check time, never restated here. It fails the day one
side is edited (or upstream moves a knob) without the other.

Usage (repo root, needs the `upstream` submodule checked out):
    python scripts/check-nvenc-preset-contract.py
Exit 0 = the sides agree. 1 = a divergence. 2 = a source file is missing.
"""
from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NVENC_DIR = REPO_ROOT / "upstream" / "plugins" / "obs-nvenc"
COMPAT_C = NVENC_DIR / "nvenc-compat.c"
PROPS_C = NVENC_DIR / "nvenc-properties.c"
SETTER = REPO_ROOT / "plugins" / "pulsar-frontend-stub" / "src" / "pulsar-frontend-stub.cpp"
GETTER = REPO_ROOT / "plugins" / "pulsar-multi-stream" / "src" / "plugin-main.cpp"

# The upstream property-list registration for the preset knob, whatever it is
# spelled: `obs_properties_add_list(props, "<name>", obs_module_text("Preset"),`
PRESET_LIST_RE = r'obs_properties_add_list\(props,\s*"([^"]+)",\s*obs_module_text\("Preset"\)'


def read(path: pathlib.Path) -> str:
    if not path.is_file():
        print(f"error: {path} missing (submodule not checked out?)", file=sys.stderr)
        raise SystemExit(2)
    return path.read_text(encoding="utf-8", errors="replace")


def string_list(body: str) -> list[str]:
    """The quoted entries of a C initialiser body, in order."""
    return [v for v in re.findall(r'"([^"]*)"', body) if v]


def find(pattern: str, text: str, what: str, problems: list[str]) -> str | None:
    m = re.search(pattern, text)
    if m is None:
        problems.append(f"could not locate {what} -- the source moved, this check is now blind")
        return None
    return m.group(1)


def preset_values(text: str, what: str, problems: list[str]) -> list[str]:
    """The add_preset("pN") block that follows the preset property registration."""
    vals = re.findall(r'add_preset\("([^"]+)"\)', text)
    if not vals:
        problems.append(f"could not locate {what} -- the source moved, this check is now blind")
    return vals


def main() -> int:
    compat, props = read(COMPAT_C), read(PROPS_C)
    setter, getter = read(SETTER), read(GETTER)
    problems: list[str] = []

    # ---- 1. what obs-nvenc actually registers, on both paths ------------
    compat_prop = find(PRESET_LIST_RE, compat, "the compat NVENC preset registration", problems)
    new_prop = find(PRESET_LIST_RE, props, "the 31.0+ NVENC preset registration", problems)

    if compat_prop and new_prop and compat_prop == new_prop:
        problems.append(
            f"both NVENC paths now spell the preset knob {compat_prop!r} -- the premise of this "
            f"fix (two names inside one family) no longer holds, collapse presetPropForId")

    compat_values = preset_values(compat, "the compat add_preset block", problems)
    new_values = preset_values(props, "the 31.0+ add_preset block", problems)
    if compat_values and new_values and compat_values != new_values:
        problems.append(
            f"the two NVENC paths now offer different presets ({compat_values} vs {new_values}) -- "
            f"one whitelist can no longer serve both ids, presetsForFamily must split")

    compat_default = find(
        rf'obs_data_set_default_string\(settings,\s*"{re.escape(compat_prop or "preset2")}",\s*"([^"]+)"\)',
        compat, "the compat NVENC preset default", problems)
    new_default = find(
        rf'obs_data_set_default_string\(settings,\s*"{re.escape(new_prop or "preset")}",\s*"([^"]+)"\)',
        props, "the 31.0+ NVENC preset default", problems)
    if compat_default and new_default and compat_default != new_default:
        problems.append(
            f"the two NVENC paths now default differently ({compat_default!r} vs {new_default!r}) -- "
            f"an unset PULSAR_VIDEO_PRESET would mean different things per id")

    # The clobber is what makes writing the wrong name ACTIVELY wrong rather
    # than inert. If upstream drops it, the severity note above is stale.
    if not re.search(
            rf'obs_data_get_string\(settings,\s*"{re.escape(compat_prop or "preset2")}"\)'
            rf'[\s\S]{{0,120}}?obs_data_set_string\(settings,\s*"{re.escape(new_prop or "preset")}"',
            compat):
        problems.append(
            "nvenc-compat.c no longer copies the compat preset over the 31.0+ key in "
            "migrate_settings() -- re-derive which name a compat spawn actually consumes")

    # ---- 2. which ids the compat (preset2) path registers ---------------
    # Both the struct literal id and the ffmpeg-era re-registration at the
    # bottom of register_compat_encoders(). H.264 only: that is Pulsar's scope.
    compat_ids = set(re.findall(r'\.id\s*=\s*"([^"]+)"', compat))
    compat_ids |= set(re.findall(r'_info\.id\s*=\s*"([^"]+)"', compat))

    # ---- 3. the boot setter (pulsar-frontend-stub) ---------------------
    nvenc_ids = string_list(
        find(r"kNvenc\[\]\s*=\s*\{([^}]*)\}", setter,
             "resolveEncoderId's kNvenc preference list", problems) or "")
    setter_values = string_list(
        find(r"kNvenc\[\]\s*=\s*\{([^}]*)\}", setter[setter.find("PresetSet presetsForFamily"):],
             "the setter's kNvenc preset set", problems) or "")
    nvenc_entry = find(r'family == "nvenc"\)\s*return\s*\{([^}]*)\}', setter,
                       "the setter's nvenc PresetSet", problems)
    setter_default, setter_family_prop = (string_list(nvenc_entry) + ["", ""])[:2] if nvenc_entry else ("", "")

    fn = find(r"const char \*presetPropForId\([^)]*\)\s*\{([\s\S]*?)\n\}", setter,
              "presetPropForId", problems)
    special_ids = set(re.findall(r'std::strcmp\(id,\s*"([^"]+)"\)\s*==\s*0', fn or ""))
    special_prop = find(r'return\s*"([^"]+)"', fn or "", "presetPropForId's overridden name", problems)

    if setter_values and new_values and setter_values != new_values:
        problems.append(
            f"setter kNvenc presets={setter_values} but obs-nvenc offers {new_values} -- "
            f"PULSAR_VIDEO_PRESET would reject a value the encoder accepts, or accept one it does not")
    if setter_default and new_default and setter_default != new_default:
        problems.append(
            f"setter's nvenc default is {setter_default!r}, obs-nvenc's own default is "
            f"{new_default!r} -- an unset PULSAR_VIDEO_PRESET would change the encoder")
    if special_prop and compat_prop and special_prop != compat_prop:
        problems.append(
            f"presetPropForId overrides to {special_prop!r} but the compat encoders read "
            f"{compat_prop!r} -- the env var is silently ignored on that path")
    if setter_family_prop and new_prop and setter_family_prop != new_prop:
        problems.append(
            f"presetsForFamily writes nvenc to {setter_family_prop!r}, the 31.0+ encoder reads "
            f"{new_prop!r} -- the env var is silently ignored on that path")

    # ---- 4. every selectable id gets the name ITS OWN plugin reads ------
    # The point of the fix: this must hold id by id, not family by family.
    for enc_id in nvenc_ids:
        would_write = special_prop if enc_id in special_ids else setter_family_prop
        expected = compat_prop if enc_id in compat_ids else new_prop
        if not (would_write and expected):
            continue
        if would_write != expected:
            where = "the compat shim" if enc_id in compat_ids else "the 31.0+ encoder"
            problems.append(
                f"resolveEncoderId can select {enc_id!r} ({where}, knob {expected!r}) but the setter "
                f"would write {would_write!r} -- PULSAR_VIDEO_PRESET is silently dropped for it")

    # A compat id that the setter does not special-case is the original bug.
    for enc_id in sorted(compat_ids & set(nvenc_ids)):
        if enc_id not in special_ids:
            problems.append(
                f"{enc_id!r} is a compat encoder (knob {compat_prop!r}) and is selectable by "
                f"resolveEncoderId, but presetPropForId does not special-case it")
    # ...and one that stopped being selectable is dead weight worth flagging.
    for enc_id in sorted(special_ids - set(nvenc_ids)):
        problems.append(
            f"presetPropForId special-cases {enc_id!r}, which resolveEncoderId can no longer "
            f"select -- drop it rather than carry a dead branch")

    # ---- 5. the getter (pulsar-multi-stream) ----------------------------
    prop_names = string_list(
        find(r"kPresetPropNames\[\]\s*=\s*\{([^}]*)\}", getter,
             "kPresetPropNames", problems) or "")
    for name in (compat_prop, new_prop):
        if name and name not in prop_names:
            problems.append(
                f"kPresetPropNames={prop_names} does not carry {name!r} -- GetVideoSettings and "
                f"capabilities.encoder_families cannot see the preset of an NVENC spawn using it")

    if problems:
        print("FAIL -- NVENC preset contract divergence:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"NVENC preset contract OK: {new_prop!r} for the 31.0+ encoder, {compat_prop!r} for the "
          f"compat ids {sorted(compat_ids & set(nvenc_ids))}, values {new_values}, "
          f"default {new_default!r} -- boot setter, getter and obs-nvenc agree id by id")
    return 0


if __name__ == "__main__":
    sys.exit(main())
