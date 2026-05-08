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
calls out the reason. This matches the Spyglass ``.swl`` workflow at
a much smaller surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rtl_buddy_cdc.rules import Violation


@dataclass(frozen=True)
class Waiver:
    rule_pattern: str  # exact rule id or "*"
    regex: re.Pattern[str]
    reason: str
    source_line: int  # 1-based line in the waiver file (for diagnostics)


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
    violations: list[Violation], waivers: list[Waiver]
) -> tuple[list[Violation], list[SuppressedViolation]]:
    """Split violations into (still-active, suppressed-by-a-waiver).

    The first matching waiver wins, mirroring user expectation that
    waiver files are read top-to-bottom and the more-specific rule is
    listed first."""
    if not waivers:
        return list(violations), []
    kept: list[Violation] = []
    suppressed: list[SuppressedViolation] = []
    for v in violations:
        match: Waiver | None = None
        cands = _candidates(v)
        for w in waivers:
            if w.rule_pattern != "*" and w.rule_pattern != v.rule_id:
                continue
            if any(w.regex.search(c) for c in cands):
                match = w
                break
        if match is not None:
            suppressed.append(SuppressedViolation(violation=v, waiver=match))
        else:
            kept.append(v)
    return kept, suppressed
