#!/usr/bin/env python3
"""nv-filters packaging gate -- issue #167, Prism ADR 023 Amendment 3.

Hardware-free, build-free, seconds long. Two things are checked, and they
pull in opposite directions on purpose:

  criterion 1  the module IS in the `full` bundle -- the variant Prism
               embeds. `nv-filters` must be gone from $baseStrippedPlugins
               and present in $lightOnlyStrippedPlugins: `light` keeps it
               stripped for sobriety, nothing having named a need for
               NVIDIA effects there. The header comment must also no longer
               read as a justification for stripping it outright -- a
               security rationale left pointing at the opposite decision is
               worse than no rationale at all, because the next reader
               trusts it.

  criterion 5  the SDK is NOT in the bundle. No NVIDIA Maxine DLL and no
               .trtpkg model may be shipped: the SDK stays a dependency of
               the host machine, which is what keeps Pulsar out of the
               business of vouching for a TensorRT package. Checked by
               ABSENCE, both in the packaging sources and -- with --dist --
               in an actual packaged tree.

Exit 0 when both hold, 1 otherwise. Wired into the `lint` job (sources) and
the `package` job (built tree).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE_SCRIPT = REPO / "scripts" / "package-win.ps1"

# Files the SDK ships and Pulsar must never redistribute. Matched
# case-insensitively; .trtpkg is matched by extension because the model
# names are the SDK's to change.
NVIDIA_SDK_DLLS = (
    "NVAudioEffects.dll",
    "NVVideoEffects.dll",
    "NVCVImage.dll",
    "nvcuda.dll",
)

# Where those names are legitimately allowed to appear as SOURCE text: the
# loader rules, the patch that wires them in, the CTest gate, the manifest
# publisher, this file, and prose. Anywhere else -- a copy list, a
# packaging manifest -- is a redistribution path and fails.
SOURCE_ALLOWLIST = (
    "plugins/pulsar-nv-secure-load/",
    "plugins/pulsar-multi-stream/src/plugin-main.cpp",
    "patches/",
    "tests/nv-probe/",
    "scripts/check-nv-filters-packaging.py",
    "docs/",
    "CHANGELOG.md",
    "upstream/",
)

SEARCHED_SUFFIXES = {".ps1", ".sh", ".py", ".cmake", ".txt", ".json", ".yml", ".yaml"}


def fail(msg: str) -> None:
    print(f"::error::{msg}")


def _array_literals(text: str, var: str) -> list[str]:
    """Every `$var = @(...)` / `$var += @(...)` body in `text`.

    Paren-matched rather than regex-captured, with per-line comments
    stripped first: a comment holding a `)` -- and one does, "(companion to
    obs-text)" -- silently truncates a non-greedy regex, and a truncated
    list reads as "the entry is absent". A check that can be defeated by a
    parenthesis in a comment is not a check.
    """
    bodies: list[str] = []
    for match in re.finditer(rf"\${re.escape(var)}\s*\+?=\s*@\(", text):
        depth = 1
        i = match.end()
        start = i
        while i < len(text) and depth:
            ch = text[i]
            if ch == "#":  # comment: skip to end of line
                nl = text.find("\n", i)
                i = len(text) if nl < 0 else nl
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth == 0:
            bodies.append(text[start:i])
    return bodies


def check_strip_lists() -> list[str]:
    """Criterion 1: nv-filters ships in `full`, stays out of `light`."""
    errors: list[str] = []
    text = PACKAGE_SCRIPT.read_text(encoding="utf-8")

    # The strip lists are PowerShell array literals; read the assignments
    # rather than grepping the whole file, so the explanatory comment is
    # free to name the plugin (it must, in fact).
    seen: dict[str, bool] = {}
    for var in ("baseStrippedPlugins", "lightOnlyStrippedPlugins"):
        blocks = _array_literals(text, var)
        if not blocks:
            errors.append(f"{PACKAGE_SCRIPT.name}: ${var} not found -- packaging script restructured?")
            continue
        seen[var] = any("nv-filters" in re.findall(r"'([^']+)'", block) for block in blocks)

    if seen.get("baseStrippedPlugins"):
        errors.append(
            f"{PACKAGE_SCRIPT.name}: 'nv-filters' is back in $baseStrippedPlugins, which strips it "
            "from BOTH variants including `full`. ADR 023 Amendment 3 A3.1 has it shipped in `full`; "
            "if that is being reverted on purpose, follow docs/runbooks/nv-filters-rollback.md "
            "and update this check."
        )
    if "lightOnlyStrippedPlugins" in seen and not seen["lightOnlyStrippedPlugins"]:
        errors.append(
            f"{PACKAGE_SCRIPT.name}: 'nv-filters' is missing from $lightOnlyStrippedPlugins, so the "
            "`light` variant would now ship it. `light` carries only what something named a need for, "
            "and nothing has named NVIDIA effects there (#167)."
        )

    # The comment must have been rewritten, not merely left behind.
    if re.search(r"nv-filters\s+--\s+NVIDIA Audio/Video Effects filters\. Same motive", text):
        errors.append(
            f"{PACKAGE_SCRIPT.name}: the old NS1 strip rationale for nv-filters is still there. "
            "It now justifies a decision the script no longer takes (criterion 1)."
        )
    if "Amendment 3" not in text or "pulsar-nv-secure-load" not in text:
        errors.append(
            f"{PACKAGE_SCRIPT.name}: the nv-filters comment must point at ADR 023 Amendment 3 "
            "and at plugins/pulsar-nv-secure-load/ -- what protects the module now that "
            "stripping does not (criterion 1)."
        )
    return errors


def _allowed(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in SOURCE_ALLOWLIST)


def check_no_sdk_in_sources() -> list[str]:
    """Criterion 5, source half: nothing outside the allowlist names an SDK
    binary, so no copy list can be quietly teaching the packager to ship
    one."""
    errors: list[str] = []
    needles = tuple(name.lower() for name in NVIDIA_SDK_DLLS)

    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SEARCHED_SUFFIXES:
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith((".git/", "node_modules/", "dist/", "build/")) or "/node_modules/" in rel:
            continue
        if _allowed(rel):
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for needle in needles:
            if needle in body:
                errors.append(
                    f"{rel}: names {needle} outside the loader/probe/doc allowlist. "
                    "Pulsar redistributes no NVIDIA SDK binary (criterion 5)."
                )
    return errors


def check_dist(dist: Path) -> list[str]:
    """Criterion 5, artefact half: the packaged tree carries no SDK binary
    and no TensorRT model. Plus criterion 1, per variant: `full` ships the
    module, `light` does not.

    The variant is read from the directory name, which package-win.ps1
    builds as pulsar-windows-x64[-full]-v<VERSION>."""
    errors: list[str] = []
    if not dist.is_dir():
        return [f"--dist {dist} is not a directory"]

    is_full = "-full-" in dist.name
    banned = {name.lower() for name in NVIDIA_SDK_DLLS}
    found_plugin = False
    for path in dist.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name in banned or name.endswith(".trtpkg"):
            errors.append(f"{path.relative_to(dist)}: NVIDIA SDK payload inside the package (criterion 5)")
        if name == "nv-filters.dll":
            found_plugin = True

    if is_full and not found_plugin:
        errors.append(
            f"{dist.name}: nv-filters.dll is absent from the `full` package. "
            "ADR 023 Amendment 3 A3.1 has it shipped there (criterion 1)."
        )
    if not is_full and found_plugin:
        errors.append(
            f"{dist.name}: nv-filters.dll is inside the `light` package. "
            "`light` carries only what something named a need for (#167)."
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist",
        type=Path,
        default=None,
        help="also check a packaged distribution directory (the `package` job passes this)",
    )
    args = parser.parse_args()

    errors = check_strip_lists() + check_no_sdk_in_sources()
    if args.dist is not None:
        errors += check_dist(args.dist)

    for err in errors:
        fail(err)

    if errors:
        print(f"\n{len(errors)} problem(s) -- see above.")
        return 1

    print("nv-filters packaging: module bundled, no NVIDIA SDK payload. OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
