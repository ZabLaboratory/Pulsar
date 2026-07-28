#!/usr/bin/env python3
"""
`webpage_control_level` pinning gate — issue #158 / ADR Prism 028 §3.2.

Hardware-free source-consistency check, run in the `lint` job. It is the
executable form of resolution criterion 1:

    "grep: NO browser-source creation without an explicit
     webpage_control_level. The criterion is about the ABSENCE of an
     unpinned path, not the presence of the right call."

So this gate does not look for a known-good line. It enumerates every
`obs_source_create*` call in the plugins Pulsar owns and demands, for each one
that could produce a `browser_source`, that the enclosing function pins
`webpage_control_level`. A NEW creation path added tomorrow without the pin
fails here, before the 30-minute Windows build.

What is checked
---------------
1. `plugins/pulsar-browser/obs-browser-source.hpp` declares
   `ControlLevel::None` as the FIRST enumerator (ordinal 0) and sets
   `DEFAULT_CONTROL_LEVEL` to it. The default is the floor for the one path
   with no creation call at all: a scene collection loaded from disk.
2. Every mirror of that ordinal outside obs-browser (plugins that must not
   link CEF just to name a constant) is still `0`. Drift between the enum and
   its literals would silently re-grant `ReadObs`.
3. Every `obs_source_create` / `obs_source_create_private` call in
   `plugins/**` — excluding `pulsar-browser` itself, which *implements*
   `browser_source` rather than creating one — either:
     - names a kind that is a string literal other than "browser_source"
       (it can never make a browser source), or
     - pins `webpage_control_level` in the enclosing function.
   A call whose kind is a runtime value (the generic v5 `CreateInput`) counts
   as "could be a browser source" and must carry the pin.

A runtime-kind call is exempt only through a WAIVER that carries its own
evidence: the waiver names the file + regex that proves the kind can never be
`browser_source`, and the gate re-checks that evidence on every run. A waiver
whose proof disappears fails like an unpinned path.

Exit 0 = pinned everywhere. Exit 1 = an unpinned path exists.
"""
from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGINS = REPO_ROOT / "plugins"
BROWSER_SOURCE_HPP = PLUGINS / "pulsar-browser" / "obs-browser-source.hpp"

# obs-browser owns the enum and the property; it is the implementation, not a
# consumer. Everything else that creates sources is in scope.
EXCLUDED_DIRS = {"pulsar-browser"}

CREATE_CALL_RE = re.compile(r"\bobs_source_create(?:_private)?\s*\(")
# First argument of the call, when it is a plain string literal.
LITERAL_KIND_RE = re.compile(r"^\s*\"([^\"]*)\"")
PIN_RE = re.compile(r"webpage_control_level")
MIRROR_RE = re.compile(r"\bkWebpageControlLevelNone\s*=\s*(-?\d+)")

# Runtime-kind calls that provably cannot produce a browser source. Keyed by
# the enclosing function's marker; each carries the evidence the gate re-checks,
# so the exemption cannot outlive the guarantee it rests on.
WAIVERS = {
    "ActionHelper::CreateSourceFilter": {
        "call": "obs_source_create_private",
        "reason": (
            "creates a FILTER, and RequestHandler::CreateSourceFilter rejects "
            "any filterKind absent from GetFilterKindList(), which enumerates "
            "obs_enum_filter_types() — OBS_SOURCE_TYPE_FILTER only. "
            "browser_source is an INPUT type and can never reach this call."
        ),
        "evidence": [
            (
                "plugins/pulsar-websocket/src/requesthandler/RequestHandler_Filters.cpp",
                r"GetFilterKindList\(\)[\s\S]{0,400}?std::find\(kinds\.begin\(\), kinds\.end\(\), filterKind\) == kinds\.end\(\)",
            ),
            (
                "plugins/pulsar-websocket/src/utils/Obs_ArrayHelper.cpp",
                r"GetFilterKindList\(\)[\s\S]{0,400}?obs_enum_filter_types",
            ),
        ],
    },
}

# Lines that open a block but are NOT a function definition.
NON_FUNCTION_KEYWORDS = (
    "if", "for", "while", "switch", "else", "do", "catch",
    "namespace", "struct", "class", "enum", "union", "extern",
)


def fail(msg: str) -> None:
    print(f"::error::{msg}")


def check_enum_and_default() -> list[str]:
    """The enum ordinal and the fork default are the anchor of every literal
    `0` used elsewhere. Verify both, from the source, not from memory."""
    errors: list[str] = []
    text = BROWSER_SOURCE_HPP.read_text(encoding="utf-8")

    m = re.search(r"enum\s+class\s+ControlLevel\s*:\s*int\s*\{(.*?)\}", text, re.S)
    if not m:
        errors.append(f"{BROWSER_SOURCE_HPP}: `enum class ControlLevel : int` not found")
        return errors

    members = [
        part.strip()
        for part in m.group(1).replace("\n", " ").split(",")
        if part.strip() and not part.strip().startswith("//")
    ]
    if not members or members[0] != "None":
        errors.append(
            f"{BROWSER_SOURCE_HPP}: ControlLevel's first enumerator is "
            f"{members[0] if members else '(none)'!r}, expected 'None'. Every "
            "`webpage_control_level = 0` literal in the tree mirrors that "
            "ordinal — reordering the enum silently changes what they mean."
        )
    if any("=" in mem for mem in members):
        errors.append(
            f"{BROWSER_SOURCE_HPP}: ControlLevel assigns explicit values; this "
            "gate assumes implicit 0..N ordinals. Update the gate with the enum."
        )

    d = re.search(r"DEFAULT_CONTROL_LEVEL\s*=\s*ControlLevel::(\w+)", text)
    if not d:
        errors.append(f"{BROWSER_SOURCE_HPP}: DEFAULT_CONTROL_LEVEL not found")
    elif d.group(1) != "None":
        errors.append(
            f"{BROWSER_SOURCE_HPP}: DEFAULT_CONTROL_LEVEL is ControlLevel::"
            f"{d.group(1)}, expected ControlLevel::None (#158). Any browser "
            "source with no creation call — a scene collection loaded from "
            "disk — inherits this value, and above None the page reads this "
            "process's OBS state through window.obsstudio."
        )
    return errors


def check_mirrors(sources: list[pathlib.Path]) -> list[str]:
    errors: list[str] = []
    found = 0
    for path in sources:
        for m in MIRROR_RE.finditer(path.read_text(encoding="utf-8")):
            found += 1
            if m.group(1) != "0":
                errors.append(
                    f"{path.relative_to(REPO_ROOT)}: kWebpageControlLevelNone = "
                    f"{m.group(1)}, expected 0 (ControlLevel::None's ordinal)."
                )
    if found == 0:
        errors.append(
            "no kWebpageControlLevelNone mirror found in plugins/ — the pins "
            "were expected to name the constant, not inline a bare 0."
        )
    return errors


def check_waiver_evidence(key: str) -> list[str]:
    """Re-prove a waiver from the tree. A waiver is a claim about code
    elsewhere; if that code changed, the exemption is void."""
    errors: list[str] = []
    for rel, pattern in WAIVERS[key]["evidence"]:
        path = REPO_ROOT / rel
        if not path.exists():
            errors.append(f"waiver {key!r}: evidence file {rel} is gone")
            continue
        if not re.search(pattern, path.read_text(encoding="utf-8")):
            errors.append(
                f"waiver {key!r}: {rel} no longer matches the guard it rests on "
                f"({pattern!r}). Either restore the guard or pin "
                "`webpage_control_level` at the creation site."
            )
    return errors


def enclosing_function(text: str, pos: int) -> tuple[str, str] | None:
    """Return `(header, body)` of the function containing `pos` — the ~400
    characters preceding its opening brace (the signature) and the body itself.
    Only the body is searched for the pin, so a mention in a doc comment above
    the function can never be mistaken for one.

    Walks backwards tracking brace depth: the first unmatched `{` whose
    signature line is not a control-flow / namespace / type keyword opens our
    function. Then forward-matches to the closing brace. Returns None if no
    such block encloses the position (a call at file scope, which cannot
    happen for these APIs).
    """
    depth = 0
    i = pos
    while i > 0:
        i -= 1
        c = text[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth > 0:
                depth -= 1
                continue
            # Unmatched `{` — is it a function's?
            line_start = text.rfind("\n", 0, i) + 1
            head = text[max(0, line_start - 400):i]
            last = head.strip().splitlines()[-1].strip() if head.strip() else ""
            first_word = last.split("(")[0].strip().split()[-1] if last else ""
            if first_word in NON_FUNCTION_KEYWORDS or last.startswith(tuple(NON_FUNCTION_KEYWORDS)):
                continue
            if "(" not in head[-400:]:
                continue  # a bare block / initialiser list, keep walking out
            # Forward-match the body.
            d = 0
            for j in range(i, len(text)):
                if text[j] == "{":
                    d += 1
                elif text[j] == "}":
                    d -= 1
                    if d == 0:
                        return head, text[i:j + 1]
            return head, text[i:]
    return None


def check_creation_sites(sources: list[pathlib.Path]) -> list[str]:
    errors: list[str] = []
    checked = 0
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for m in CREATE_CALL_RE.finditer(text):
            checked += 1
            rest = text[m.end():]
            lit = LITERAL_KIND_RE.match(rest)
            kind = lit.group(1) if lit else None
            if kind is not None and kind != "browser_source":
                continue  # a literal non-browser kind can never be one
            found = enclosing_function(text, m.start())
            line = text.count("\n", 0, m.start()) + 1
            if found is None:
                errors.append(
                    f"{path.relative_to(REPO_ROOT)}:{line}: could not resolve the "
                    "enclosing function of this obs_source_create call; the gate "
                    "cannot prove the pin — restructure or extend the gate."
                )
                continue
            header, body = found
            waiver_key = next(
                (
                    k
                    for k, w in WAIVERS.items()
                    if k in header and w["call"] in body
                ),
                None,
            )
            if waiver_key is not None:
                errors += check_waiver_evidence(waiver_key)
                print(
                    f"  waived: {path.relative_to(REPO_ROOT)}:{line} "
                    f"({waiver_key}) — {WAIVERS[waiver_key]['reason']}"
                )
                continue
            if not PIN_RE.search(body):
                what = (
                    'kind="browser_source"'
                    if kind == "browser_source"
                    else "a runtime input kind (may be browser_source)"
                )
                errors.append(
                    f"{path.relative_to(REPO_ROOT)}:{line}: obs_source_create with "
                    f"{what} does not pin `webpage_control_level` in its enclosing "
                    "function (#158 / ADR Prism 028 §3.2). An unpinned browser "
                    "source inherits obs-browser's default level, which lets the "
                    "page read this process's OBS state through window.obsstudio."
                )
    print(f"scanned {checked} obs_source_create* call(s) across {len(sources)} file(s)")
    return errors


def main() -> int:
    sources = sorted(
        p
        for p in PLUGINS.rglob("*.cpp")
        if not (set(p.relative_to(PLUGINS).parts) & EXCLUDED_DIRS)
    )
    if not sources:
        fail("no plugin sources found — the gate would pass vacuously")
        return 1

    errors = check_enum_and_default()
    errors += check_mirrors(sources)
    errors += check_creation_sites(sources)

    if errors:
        for e in errors:
            fail(e)
        print(f"\nFAIL: {len(errors)} webpage_control_level defect(s).")
        return 1

    print("OK: ControlLevel::None is the fork default, every mirror is 0, and "
          "every browser-source creation path pins webpage_control_level.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
