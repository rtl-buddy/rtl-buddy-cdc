"""Per-violation suppression via a small waiver file.

Format (one statement per line; ``#`` and blank lines ignored)::

    waive <RULE-ID|*> <regex> [reason ...]
    waive <RULE-ID|*> inst:<inst-regex> [reason ...]
    waive <RULE-ID|*> <regex> inst:<inst-regex> [reason ...]

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
- ``inst:<inst-regex>`` (optional, #327) scopes the waiver to a part
  of the hierarchy. It is matched against the violation's
  ``instance_path`` joined with ``/`` and no spaces (``u_block_a/u_sync``;
  the empty string for a top-level finding), with ``re.search`` — the
  same unanchored search the object regex uses, so both patterns on one
  line behave alike. Anchor it yourself: ``inst:^u_ip(/|$)`` is
  "``u_ip`` and everything under it", ``inst:^$`` is "top level only".
  A finding anchored on a blackbox instance itself (``CDC-BBX``, or a
  finding lifted out of an analysed boundary module) is scoped to that
  instance — the caller supplies that via ``instance_scope``. Written
  alone (``waive R inst:X``) it waives every finding of the rule in
  scope; written after an object regex both must match. An ``inst:``
  token is only recognised as the third or fourth word, so a line
  without one parses exactly as before.
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
because its scope is a region of that file, not a name pattern. The
region is ``[start_line, end_line)``, open-ended to the end of the
file when no ``enable-rule`` closed it.

A third producer reuses the same record without going through
:func:`apply`: an object-scoped ``set_false_path`` in the SDC that
covers a crossing acknowledges the findings a quasi-static source would
not raise (rtl-buddy-cdc#328, :mod:`rtl_buddy_cdc.sdc_objects`). Such a
:class:`Waiver` carries an :class:`SdcJustification` naming the
statement, so reporters show the constraint as the reason.
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
class SdcJustification:
    """The SDC statement behind a suppression (rtl-buddy-cdc#328).

    ``statement`` is the constraint re-rendered canonically (identical
    on both SDC backends); ``line`` is its line in ``file`` when the
    reader tracked one (the Tcl backend does, the tokenizer does not);
    ``file`` is the ``--sdc`` path as given.
    """

    statement: str
    line: int | None = None
    file: str | None = None


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
    # Line scope, pragmas only. ``[start_line, end_line)`` — half-open,
    # so the ``enable-rule`` line itself is outside. ``start_line`` is
    # the ``disable-rule`` line (identical to ``source_line``);
    # ``end_line`` is ``None`` when no ``enable-rule`` closed the block,
    # meaning "to the end of the file". Both ``None`` on a waiver-file
    # entry, which has no line scope at all.
    start_line: int | None = None
    end_line: int | None = None
    # Instance scope (#327), waiver-file entries only: ``re.search``-ed
    # against the violation's ``/``-joined instance scope (see
    # ``apply``). ``None`` means "anywhere in the hierarchy" — every
    # waiver written before the ``inst:`` form existed.
    inst_regex: re.Pattern[str] | None = None
    # Set when the suppression is an SDC exception rather than a waiver
    # or pragma (rtl-buddy-cdc#328). ``origin`` / ``start_line`` /
    # ``end_line`` are then ``None`` and ``source_line`` is the SDC line
    # (``0`` when the reader tracked none).
    sdc: SdcJustification | None = None


# The ``inst:`` scope prefix on a waiver-file word (#327).
INST_PREFIX = "inst:"

# The object regex an instance-only line (``waive R inst:X``) carries:
# it matches every candidate string, so the instance scope alone
# decides. Kept a real pattern rather than ``None`` so ``Waiver.regex``
# (and the JSON ``waiver.regex`` key) stay non-null.
_ANY_OBJECT = ".*"


def _compile(lineno: int, pattern: str, what: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as e:
        raise ValueError(f"line {lineno}: invalid {what} {pattern!r}: {e}") from e


def _compile_inst(lineno: int, word: str) -> re.Pattern[str]:
    pattern = word[len(INST_PREFIX) :]
    if not pattern:
        raise ValueError(
            f"line {lineno}: empty `{INST_PREFIX}` scope "
            f"(use `{INST_PREFIX}^$` for top-level findings only)"
        )
    return _compile(lineno, pattern, "inst regex")


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
        inst: re.Pattern[str] | None = None
        if pattern.startswith(INST_PREFIX):
            # ``waive R inst:X [reason]`` — instance scope only.
            inst = _compile_inst(lineno, pattern)
            compiled = re.compile(_ANY_OBJECT)
        else:
            compiled = _compile(lineno, pattern, "regex")
            if rest and rest[0].startswith(INST_PREFIX):
                # ``waive R <regex> inst:X [reason]`` — both must match.
                # Re-split the tail: the reason is what follows the scope.
                scope, *rest = rest[0].split(maxsplit=1)
                inst = _compile_inst(lineno, scope)
        reason = rest[0].strip() if rest else ""
        if inst is not None and reason.startswith(INST_PREFIX):
            raise ValueError(
                f"line {lineno}: more than one `{INST_PREFIX}` scope on one line"
            )
        out.append(
            Waiver(
                rule_pattern=rule,
                regex=compiled,
                reason=reason,
                source_line=lineno,
                inst_regex=inst,
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


@dataclass(frozen=True)
class SourceRef:
    """Where the analyzer places a violation: a file, and the line
    within it when one is known. ``line`` is ``None`` for a location
    that names a file only."""

    file: str
    line: int | None = None


def _in_line_scope(w: Waiver, line: int | None) -> bool:
    """Does ``line`` fall inside the pragma's ``[start, end)`` block?

    Two degenerate cases both answer yes, on purpose:

    - ``start_line is None`` — the waiver has no line scope at all
      (a waiver-file entry, or a pragma record built without one).
    - ``line is None`` — the violation's location names a file but no
      line, so there is nothing to compare. Falling back to file scope
      keeps a pragma working wherever line information is thin instead
      of silently doing nothing.
    """
    if w.start_line is None or line is None:
        return True
    if line < w.start_line:
        return False
    return w.end_line is None or line < w.end_line


def instance_path_text(path: tuple[str, ...]) -> str:
    """The canonical text an ``inst:`` scope is matched against:
    components joined with ``/`` and no spaces, ``""`` for the top
    instance. ``/`` rather than ``.`` so a pattern doesn't have to
    escape every separator (hierarchical-reporting §7 item 5)."""
    return "/".join(path)


def _default_instance_scope(v: Violation) -> str:
    return instance_path_text(v.instance_path)


def apply(
    violations: list[Violation],
    waivers: list[Waiver],
    *,
    locate: Callable[[Violation], SourceRef | None] | None = None,
    instance_scope: Callable[[Violation], str] | None = None,
) -> tuple[list[Violation], list[SuppressedViolation]]:
    """Split violations into (still-active, suppressed-by-a-waiver).

    The first matching waiver wins, mirroring user expectation that
    waiver files are read top-to-bottom and the more-specific rule is
    listed first.

    ``locate`` resolves a violation's source location (the analyzer
    keeps it on the offending cell, so only the caller holding the
    ``Module`` can do this). It is consulted for **pragma** waivers
    only — one written in the RTL is scoped to a region of its file, so
    it matches the source path plus the line range and nothing else,
    never the cell name or the message. Without a resolver (or a
    violation whose location is unknown) a pragma waiver simply doesn't
    match: no location, no file-scoped suppression.

    ``instance_scope`` gives the ``/``-joined text an ``inst:`` scope is
    searched in. It defaults to the violation's own ``instance_path``,
    so callers must populate that field *before* applying waivers; the
    CLI also passes a resolver that scopes a finding anchored on a
    blackbox instance to that instance."""
    if not waivers:
        return list(violations), []
    scope_of = instance_scope or _default_instance_scope
    kept: list[Violation] = []
    suppressed: list[SuppressedViolation] = []
    for v in violations:
        match: Waiver | None = None
        cands = _candidates(v)
        loc: SourceRef | None = locate(v) if locate is not None else None
        scope: str | None = None
        for w in waivers:
            if w.rule_pattern != "*" and not _rule_pattern_matches(
                w.rule_pattern, v.rule_id
            ):
                continue
            if w.origin is not None:
                if (
                    loc is not None
                    and w.regex.search(loc.file)
                    and _in_line_scope(w, loc.line)
                ):
                    match = w
                    break
                continue
            if w.inst_regex is not None:
                if scope is None:
                    scope = scope_of(v)
                if not w.inst_regex.search(scope):
                    continue
            if any(w.regex.search(c) for c in cands):
                match = w
                break
        if match is not None:
            suppressed.append(SuppressedViolation(violation=v, waiver=match))
        else:
            kept.append(v)
    return kept, suppressed
