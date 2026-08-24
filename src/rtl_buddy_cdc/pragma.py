"""In-RTL pragma comments — an inline alternative to the waiver file.

Instead of maintaining an external ``cdc.waivers`` whose regexes are
matched against synthesis-generated cell names (brittle: the names
shift between synth runs), a suppression can be written next to the
RTL it applies to::

    // rbcdc: disable-rule CDC-001
    // rbcdc: disable-rule CDC-001,CDC-002 hand-reviewed handshake
    /* rbcdc: disable-rule CDC-001 hand-reviewed */ logic q;

``rbcdc:`` is the tool's magic-comment namespace (each rtl-buddy tool
owns one: ``rbsch:``, ``rbxeno:``, …). It is the only accepted
spelling — there are no aliases, and no SV-attribute form.

Grammar of a recognised pragma:

``//`` or ``/*``, optional space, ``rbcdc:``, ``disable-rule``, a
comma-separated list of rule ids (or ``*``), then optional free text
captured as the reason. A trailing ``*/`` is stripped from the reason
of the block-comment form. One pragma per line; the pragma must fit on
one line.

This module is deliberately **text-only**: the sources are read with
``Path.read_text``, never handed to Yosys or slang. A pragma therefore
survives elaboration failures, costs nothing, and needs no frontend.

Each pragma is turned into an ordinary :class:`rtl_buddy_cdc.waivers.Waiver`
so the existing application path (:func:`rtl_buddy_cdc.waivers.apply`)
and the existing report rendering carry it for free:

- ``rule_pattern`` — one waiver per rule id in the list.
- ``regex`` — the source file's basename, escaped. File scope: the
  pragma covers findings the analyzer attributes to this file.
- ``reason`` — the free text after the rule list.
- ``source_line`` — the **line in the SV file**, not a waiver-file
  line; ``origin`` (the file path) is what tells the two apart.
"""

from __future__ import annotations

import re
from pathlib import Path

from rtl_buddy_cdc.waivers import Waiver

# ``//`` or ``/*`` — the pragma is a comment, never bare code. The rule
# list is comma-separated; everything after it on the line is the
# reason.
_RULE = r"[A-Za-z0-9_-]+|\*"
_PRAGMA_RE = re.compile(
    r"(?://|/\*)\s*rbcdc:\s*disable-rule\s+"
    rf"(?P<rules>(?:{_RULE})(?:\s*,\s*(?:{_RULE}))*)"
    r"(?P<reason>.*)$"
)


def _clean_reason(raw: str) -> str:
    """Trim the free-text tail: whitespace plus the block-comment
    terminator when the pragma was written as ``/* … */``."""
    reason = raw.strip()
    if reason.endswith("*/"):
        reason = reason[:-2].strip()
    return reason


def _file_pattern(path: Path) -> re.Pattern[str]:
    """File scope: match the file's basename verbatim."""
    return re.compile(re.escape(path.name))


def scan_text(text: str, path: Path) -> list[Waiver]:
    """Scan one already-read source *text*, attributed to *path*."""
    out: list[Waiver] = []
    file_regex = _file_pattern(path)
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _PRAGMA_RE.search(line)
        if m is None:
            continue
        reason = _clean_reason(m.group("reason"))
        # The rule-list group is anchored on the token pattern, so every
        # comma-separated element is a non-empty rule id after stripping.
        for rule in (r.strip() for r in m.group("rules").split(",")):
            out.append(
                Waiver(
                    rule_pattern=rule,
                    regex=file_regex,
                    reason=reason,
                    source_line=lineno,
                    origin=str(path),
                )
            )
    return out


def scan(source_files: list[Path]) -> list[Waiver]:
    """Read each source file as text and return one waiver per
    (pragma × rule id), in file order then line order."""
    out: list[Waiver] = []
    for src in source_files:
        p = Path(src)
        out.extend(scan_text(p.read_text(encoding="utf-8", errors="replace"), p))
    return out
