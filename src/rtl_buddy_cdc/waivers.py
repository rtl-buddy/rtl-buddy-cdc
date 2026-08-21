"""Per-violation suppression via a small waiver file.

Format (one statement per line; ``#`` and blank lines ignored)::

    waive <RULE-ID|*> <regex> [reason ...]

Examples::

    waive CDC-001 .*procdff\\$9.*       hand-reviewed handshake
    waive CDC-005 .*known_good_sync.*   library cell
    waive *       .*generated_codegen.* auto-generated, ignore

Matching:

- ``RULE-ID`` either equals the violation's ``rule_id`` exactly, or is
  ``*`` (matches every rule).
- ``regex`` is a Python regex matched against any of:
  the violation's ``cell_name``, the offending crossing's
  ``"src_flop -> dst_flop"`` text (when present), or the violation
  ``message``. A hit anywhere counts.
- ``reason`` is free text echoed in the report next to suppressed
  entries — required in spirit, optional in syntax so partial files
  still parse.

Waivers are applied *after* the rule pass: each ``Violation`` either
survives or is moved into a ``suppressed`` list along with the
matching waiver. They are not silent — the report keeps a tally and
calls out the reason. This matches the common ``.swl`` waiver-file
workflow at a much smaller surface.

A second producer feeds the same path: an in-RTL ``// rbcdc:`` pragma
(see :mod:`rtl_buddy_cdc.pragma`). Such a waiver carries an ``origin``
— the source file it was written in — and is matched against the
violation's *source location* instead of the three strings above,
because its scope is the file, not a name pattern.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rtl_buddy_cdc.rules import Violation


# Renamed rule IDs — keep accepting the legacy name in waiver files so
# users don't have to mass-edit on the bump. Each entry is
# ``{legacy_id: canonical_id}``. A waiver line ``waive CDC-007 …`` now
# matches violations whose ``rule_id == "RDC-001"`` (the post-#107
# canonical name). The legacy id is otherwise a fully synonym for the
# canonical one — first-match-wins is unchanged.
_LEGACY_RULE_ALIASES: dict[str, str] = {
    "CDC-007": "RDC-001",
}


@dataclass(frozen=True)
class Waiver:
    rule_pattern: str  # exact rule id, a legacy alias, or "*"
    regex: re.Pattern[str]
    reason: str
    source_line: int  # 1-based line in the file below (for diagnostics)
    # Where the waiver was written. ``None`` for a waiver-file entry
    # (``source_line`` is then a line in that waiver file). A path
    # means the waiver came from an in-RTL ``// rbcdc:`` pragma in that
    # source file, and ``source_line`` is the line of the pragma —
    # see :mod:`rtl_buddy_cdc.pragma`.
    origin: str | None = None


def parse(text: str) -> list[Waiver]:
    out: list[Waiver] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(maxsplit=3)
        if len(parts) < 3 or parts[0] != "waive":
            raise ValueError(
                f"line {lineno}: expected `waive <rule|*> <regex> [reason]`, "
                f"got: {raw!r}"
            )
        _, rule, pattern, *rest = parts
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"line {lineno}: invalid regex {pattern!r}: {e}") from e
        reason = rest[0].strip() if rest else ""
        out.append(
            Waiver(
                rule_pattern=rule,
                regex=compiled,
                reason=reason,
                source_line=lineno,
            )
        )
    return out


def parse_file(path: str | Path) -> list[Waiver]:
    return parse(Path(path).read_text())


@dataclass(frozen=True)
class SuppressedViolation:
    """A violation that survived the rule pass but was waived. Carries
    the matching waiver so reporters can show *why* it's suppressed."""

    violation: Violation
    waiver: Waiver


def _rule_pattern_matches(pattern: str, rule_id: str) -> bool:
    """Exact match, or legacy alias resolves to the canonical id."""
    if pattern == rule_id:
        return True
    aliased = _LEGACY_RULE_ALIASES.get(pattern)
    return aliased is not None and aliased == rule_id


def _candidates(v: Violation) -> tuple[str, ...]:
    """Strings the waiver regex is matched against. The caller passes
    ANY one of these; a hit on any string suppresses the violation.

    Order is roughly "most specific" → "least specific" so users can
    write tight regexes (cell names) without fighting ambient text."""
    parts: list[str] = []
    if v.cell_name:
        parts.append(v.cell_name)
    if v.crossing is not None:
        parts.append(f"{v.crossing.src_name} -> {v.crossing.dst_flop.cell.name}")
    parts.append(v.message)
    return tuple(parts)


def apply(
    violations: list[Violation],
    waivers: list[Waiver],
    *,
    source_file: Callable[[Violation], str | None] | None = None,
) -> tuple[list[Violation], list[SuppressedViolation]]:
    """Split violations into (still-active, suppressed-by-a-waiver).

    The first matching waiver wins, mirroring user expectation that
    waiver files are read top-to-bottom and the more-specific rule is
    listed first.

    ``source_file`` resolves a violation's source file (the analyzer
    keeps it on the offending cell, so only the caller holding the
    ``Module`` can do this). It is consulted for **pragma** waivers
    only — one written in the RTL is scoped to its file, so it matches
    the source path and nothing else, never the cell name or the
    message. Without a resolver (or a violation whose location is
    unknown) a pragma waiver simply doesn't match: no location, no
    file-scoped suppression."""
    if not waivers:
        return list(violations), []
    kept: list[Violation] = []
    suppressed: list[SuppressedViolation] = []
    for v in violations:
        match: Waiver | None = None
        cands = _candidates(v)
        src: str | None = source_file(v) if source_file is not None else None
        for w in waivers:
            if w.rule_pattern != "*" and not _rule_pattern_matches(
                w.rule_pattern, v.rule_id
            ):
                continue
            if w.origin is not None:
                if src is not None and w.regex.search(src):
                    match = w
                    break
                continue
            if any(w.regex.search(c) for c in cands):
                match = w
                break
        if match is not None:
            suppressed.append(SuppressedViolation(violation=v, waiver=match))
        else:
            kept.append(v)
    return kept, suppressed
