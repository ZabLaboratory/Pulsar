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
4. Every `obs_source_update` / `obs_source_reset_settings` call in the same
   scope pins too. Creation is not the whole surface: a settings blob applied
   to an ALREADY-LIVE source re-writes the very key the creation pin set, and
   `obs_source_reset_settings` clears the user settings first — an unpinned
   reset drops the pin and falls back to obs-browser's default. Pinning only at
   creation would be self-cancelling (Bastion, PR #161).

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
# Settings applied to an ALREADY-LIVE source. `obs_source_update_properties`
# only asks the UI to refresh and carries no settings, so it is excluded.
MUTATE_CALL_RE = re.compile(r"\bobs_source_(?:update|reset_settings)\s*\(")
# First argument of the call, when it is a plain string literal.
LITERAL_KIND_RE = re.compile(r"^\s*\"([^\"]*)\"")
# A site pins either by writing the key itself, or by delegating to the shared
# helper. The helper's own body is verified separately (check_pin_helper), so
# accepting its name here is not a hole.
PIN_HELPER = "PinBrowserControlLevel"
PIN_RE = re.compile(r"webpage_control_level|" + PIN_HELPER)
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
    "RequestHandler::SetSourceFilterSettings": {
        "call": "obs_source_reset_settings",
        "reason": (
            "updates a FILTER. Request::AcquireFilter resolves it with "
            "obs_source_get_filter_by_name(), which walks a source's filter "
            "chain and can only ever return a filter — never the input the "
            "filter is attached to, and never a browser_source."
        ),
        "evidence": [
            (
                "plugins/pulsar-websocket/src/requesthandler/rpc/Request.cpp",
                r"FilterPair Request::AcquireFilter[\s\S]{0,900}?obs_source_get_filter_by_name",
            ),
            (
                "plugins/pulsar-websocket/src/requesthandler/RequestHandler_Filters.cpp",
                r"SetSourceFilterSettings[\s\S]{0,300}?request\.AcquireFilter",
            ),
        ],
    },
    "RequestHandler::SetCurrentSceneTransitionSettings": {
        "call": "obs_source_reset_settings",
        "reason": (
            "updates the CURRENT TRANSITION, resolved by "
            "obs_frontend_get_current_transition(). A transition is not an "
            "input; browser_source can never be the value it returns."
        ),
        "evidence": [
            (
                "plugins/pulsar-websocket/src/requesthandler/RequestHandler_Transitions.cpp",
                r"SetCurrentSceneTransitionSettings[\s\S]{0,400}?obs_frontend_get_current_transition\(\)",
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
    text = read_code(BROWSER_SOURCE_HPP)

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


def check_pin_helper(sources: list[pathlib.Path]) -> list[str]:
    """`PinBrowserControlLevel` is accepted as a pin at call sites, so it must
    actually be one. Prove its body writes the key to the None ordinal, and
    that it does so UNCONDITIONALLY for a browser source — a helper that only
    filled a gap would leave an explicit wire value standing."""
    errors: list[str] = []
    for path in sources:
        text = read_code(path)
        m = re.search(r"void\s+[\w:]*" + PIN_HELPER + r"\s*\(", text)
        if not m:
            continue
        # Forward-match the definition's own body from the `{` that follows the
        # signature. (enclosing_function walks OUTWARDS from a position inside a
        # body; here we are standing on the signature itself.)
        open_brace = text.find("{", m.end())
        if open_brace == -1:
            errors.append(f"{path.relative_to(REPO_ROOT)}: {PIN_HELPER} has no body")
            return errors
        depth, close = 0, -1
        for j in range(open_brace, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    close = j
                    break
        if close == -1:
            errors.append(f"{path.relative_to(REPO_ROOT)}: cannot read {PIN_HELPER}'s body")
            return errors
        body = text[open_brace:close + 1]
        if not re.search(
            r"obs_data_set_int\s*\(\s*settings\s*,\s*\"webpage_control_level\"\s*,\s*"
            r"kWebpageControlLevelNone\s*\)",
            body,
        ):
            errors.append(
                f"{path.relative_to(REPO_ROOT)}: {PIN_HELPER} does not set "
                "`webpage_control_level` to kWebpageControlLevelNone. Call sites "
                "are accepted on the strength of calling it — it must pin."
            )
        if re.search(r"if\s*\([^)]*has_user_value[\s\S]{0,80}?return", body):
            errors.append(
                f"{path.relative_to(REPO_ROOT)}: {PIN_HELPER} returns early when "
                "the caller supplied a value — that is fill-if-absent, not a pin. "
                "An explicit level on the wire must be overridden (#158)."
            )
        return errors
    errors.append(
        f"{PIN_HELPER} is not defined anywhere in plugins/ — call sites accepted "
        "on its name would be vacuous."
    )
    return errors


def check_mirrors(sources: list[pathlib.Path]) -> list[str]:
    errors: list[str] = []
    found = 0
    for path in sources:
        for m in MIRROR_RE.finditer(read_code(path)):
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


def strip_comments(src: str) -> str:
    """Blank out `//` and `/* */` comments, leaving string literals intact.

    Everything the gate asserts is asserted against CODE. Searching raw source
    would let a doc comment mentioning the pin stand in for the pin itself —
    which is exactly how the first draft of this gate passed a hand-removed
    `SetInputSettings` pin. Whitespace is preserved so reported line numbers
    stay meaningful.
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                out.append(src[i])
                if src[i] == "\\":
                    if i + 1 < n:
                        out.append(src[i + 1])
                        i += 2
                        continue
                elif src[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def read_code(path: pathlib.Path) -> str:
    """File contents with comments blanked out — the gate asserts on code."""
    return strip_comments(path.read_text(encoding="utf-8"))


def check_waiver_evidence(key: str) -> list[str]:
    """Re-prove a waiver from the tree. A waiver is a claim about code
    elsewhere; if that code changed, the exemption is void."""
    errors: list[str] = []
    for rel, pattern in WAIVERS[key]["evidence"]:
        path = REPO_ROOT / rel
        if not path.exists():
            errors.append(f"waiver {key!r}: evidence file {rel} is gone")
            continue
        if not re.search(pattern, read_code(path)):
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
        text = read_code(path)
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


def check_mutation_sites(sources: list[pathlib.Path]) -> list[str]:
    """Same demand, applied to settings written to an ALREADY-LIVE source.

    The target is a `obs_source_t *`, so its kind is never a literal here: every
    call is "could be a browser source" and must pin, or carry a waiver whose
    evidence proves the source it touches cannot be one.
    """
    errors: list[str] = []
    checked = 0
    for path in sources:
        text = read_code(path)
        for m in MUTATE_CALL_RE.finditer(text):
            checked += 1
            found = enclosing_function(text, m.start())
            line = text.count("\n", 0, m.start()) + 1
            call = text[m.start():m.end() - 1]
            if found is None:
                errors.append(
                    f"{path.relative_to(REPO_ROOT)}:{line}: could not resolve the "
                    f"enclosing function of this {call} call; the gate cannot "
                    "prove the pin — restructure or extend the gate."
                )
                continue
            header, body = found
            waiver_key = next(
                (k for k, w in WAIVERS.items() if k in header and w["call"] in body),
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
                errors.append(
                    f"{path.relative_to(REPO_ROOT)}:{line}: {call} writes settings "
                    "to a live source without pinning `webpage_control_level` in "
                    "its enclosing function (#158 / ADR Prism 028 §3.2, Bastion on "
                    "PR #161). Pinning only at creation is self-cancelling: this "
                    "path re-writes the same key on an already-sandboxed page, and "
                    "obs_source_reset_settings additionally clears the pin before "
                    "applying."
                )
    print(f"scanned {checked} settings-mutation call(s) across {len(sources)} file(s)")
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
    errors += check_pin_helper(sources)
    errors += check_creation_sites(sources)
    errors += check_mutation_sites(sources)

    if errors:
        for e in errors:
            fail(e)
        print(f"\nFAIL: {len(errors)} webpage_control_level defect(s).")
        return 1

    print("OK: ControlLevel::None is the fork default, every mirror is 0, and "
          "every path that can hand obs-browser a settings object — creation "
          "AND update — pins webpage_control_level.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
