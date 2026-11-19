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

``//`` or ``/*``, optional space, ``rbcdc:``, ``disable-rule`` or
``enable-rule``, a comma-separated list of rule ids (or ``*``), then
optional free text captured as the reason. A trailing ``*/`` is
stripped from the reason of the block-comment form. One pragma per
line; the pragma must fit on one line.

Block scoping
-------------

A ``disable-rule`` opens a region that an ``enable-rule`` for the same
rule id closes::

    // rbcdc: disable-rule CDC-001 vetted by hand
    logic q;
    always_ff @(posedge clk) q <= d;
    // rbcdc: enable-rule CDC-001

The region is **half-open** — ``[disable_line, enable_line)`` — so the
``enable-rule`` line itself is outside it, and a violation the
analyzer places on a line inside it is suppressed. Rule ids are
tracked independently, so interleaved blocks for different rules don't
interfere. A ``disable-rule`` never closed by an ``enable-rule`` runs
to the end of the file; re-disabling a rule that is already open ends
the running region and starts a fresh one (so the newer reason
applies from there). A stray ``enable-rule`` with nothing open is
ignored.

This module is deliberately **text-only**: the sources are read with
``Path.read_text``, never handed to Yosys or slang. A pragma therefore
survives elaboration failures, costs nothing, and needs no frontend.

Each region is turned into an ordinary
:class:`rtl_buddy_cdc.waivers.Waiver` so the existing application path
(:func:`rtl_buddy_cdc.waivers.apply`) and the existing report rendering
carry it for free:

- ``rule_pattern`` — one waiver per rule id in the list.
- ``regex`` — the source file's basename, escaped. File scope: the
  pragma covers findings the analyzer attributes to this file.
- ``reason`` — the free text after the rule list.
- ``source_line`` / ``start_line`` — the **line in the SV file** of the
  ``disable-rule``, not a waiver-file line; ``origin`` (the file path)
  is what tells the two apart.
- ``end_line`` — the closing ``enable-rule``'s line, or ``None`` for
  "to the end of the file".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from rtl_buddy_cdc.waivers import Waiver

# ``//`` or ``/*`` — the pragma is a comment, never bare code. The rule
# list is comma-separated; everything after it on the line is the
# reason.
_RULE = r"[A-Za-z0-9_-]+|\*"
_PRAGMA_RE = re.compile(
    r"(?://|/\*)\s*rbcdc:\s*(?P<verb>disable-rule|enable-rule)\s+"
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
    file_regex = _file_pattern(path)
    origin = str(path)
    # rule id → (line the region opened on, its reason). One entry per
    # rule so interleaved blocks for different rules stay independent.
    open_regions: dict[str, tuple[int, str]] = {}
    out: list[Waiver] = []

    def close(rule: str, end_line: int | None) -> None:
        start_line, reason = open_regions.pop(rule)
        out.append(
            Waiver(
                rule_pattern=rule,
                regex=file_regex,
                reason=reason,
                source_line=start_line,
                origin=origin,
                start_line=start_line,
                end_line=end_line,
            )
        )

    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _PRAGMA_RE.search(line)
        if m is None:
            continue
        reason = _clean_reason(m.group("reason"))
        disabling = m.group("verb") == "disable-rule"
        # The rule-list group is anchored on the token pattern, so every
        # comma-separated element is a non-empty rule id after stripping.
        for rule in (r.strip() for r in m.group("rules").split(",")):
            if rule in open_regions:
                # Closes on an `enable-rule`; on a re-`disable-rule` the
                # running region ends here and the new one takes over.
                close(rule, lineno)
            if disabling:
                open_regions[rule] = (lineno, reason)

    # Whatever is still open runs to the end of the file.
    for rule in list(open_regions):
        close(rule, None)
    # Stable sort: regions in the order their `disable-rule` appears,
    # and within one pragma, the order the rule ids were written.
    out.sort(key=lambda w: w.start_line or 0)
    return out


def scan(source_files: list[Path]) -> list[Waiver]:
    """Read each source file as text and return one waiver per
    (pragma region × rule id), in file order then line order."""
    out: list[Waiver] = []
    for src in source_files:
        p = Path(src)
        out.extend(scan_text(p.read_text(encoding="utf-8", errors="replace"), p))
    return out


# --- stub declarations (rtl-buddy-cdc#411) ------------------------------------
#
# A bodyless stub (``--blackbox``) declares nothing about its pins, so the
# analyzer assumes the conservative answer for each output: every data
# input reaches it combinationally (CDC-024 / CDC-025 walk through), and
# a payload it launches is not held for an acknowledge (CDC-012). The
# ``stub`` pragma describes one output instead::
#
#     // rbcdc: stub blk.q feedthrough=none
#     // rbcdc: stub blk.q feedthrough=d,e
#     // rbcdc: stub blk.data hold=ack
#     // rbcdc: stub blk.data feedthrough=none hold=ack  reviewed against the IP spec
#
#     // rbcdc: stub blk.* feedthrough=none
#
# ``feedthrough`` lists the input pins with a combinational path to the
# output (``none``: it is registered); ``hold`` the input pins whose
# acknowledge holds the payload on it until the destination has sampled
# it. ``<module>.*`` sets a key for every output of the module; a pragma
# naming the output itself overrides it, key by key. Free text after the
# last ``key=value`` is a note. The module is
# named explicitly, so the pragma may sit anywhere in the sources (next to
# the stub's declaration is the natural place); it applies to every
# instance of the module, and only when the module is a bodyless stub —
# a greybox has the body, which says what the pins do.

_IDENT = r"[A-Za-z_][A-Za-z0-9_$]*"
_STUB_RE = re.compile(
    r"(?://|/\*)\s*rbcdc:\s*stub\s+"
    rf"(?P<module>{_IDENT})\.(?P<port>{_IDENT}|\*)"
    r"(?P<rest>.*)$"
)
_STUB_KEYS: frozenset[str] = frozenset({"feedthrough", "hold"})


@dataclass(frozen=True)
class StubPortDecl:
    """What a ``// rbcdc: stub`` pragma declares about one stub output.

    ``None`` for a key the pragma did not set (the conservative default
    stands); an empty tuple for ``feedthrough=none`` / ``hold=none``.
    ``origin`` is ``file:line`` of the last pragma that set a key.
    """

    module: str
    port: str
    feedthrough: tuple[str, ...] | None = None
    hold: tuple[str, ...] | None = None
    origin: str = ""


@dataclass(frozen=True)
class StubDecls:
    """Every stub declaration in the sources, keyed by ``(module, port)``,
    and the pragma lines that could not be read (``file:line: why``)."""

    ports: dict[tuple[str, str], StubPortDecl] = field(default_factory=dict)
    problems: tuple[str, ...] = ()


def _pin_list(value: str) -> tuple[str, ...] | None:
    if value == "none":
        return ()
    pins = tuple(p.strip() for p in value.split(","))
    if not pins or not all(re.fullmatch(_IDENT, p) for p in pins):
        return None
    return pins


def scan_stub_text(text: str, path: Path) -> StubDecls:
    """Scan one already-read source *text* for ``// rbcdc: stub`` pragmas."""
    ports: dict[tuple[str, str], StubPortDecl] = {}
    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = _STUB_RE.search(line)
        if m is None:
            continue
        where = f"{path}:{lineno}"
        rest = m.group("rest").strip()
        if rest.endswith("*/"):
            rest = rest[:-2].strip()
        values: dict[str, tuple[str, ...]] = {}
        for word in rest.split():
            key, eq, value = word.partition("=")
            if not eq:
                break  # the note starts here
            pins = _pin_list(value)
            if key not in _STUB_KEYS or pins is None:
                problems.append(f"{where}: cannot read `{word}` in a stub pragma")
                continue
            values[key] = pins
        if not values:
            problems.append(
                f"{where}: stub pragma for {m.group('module')}.{m.group('port')} "
                "sets neither feedthrough= nor hold="
            )
            continue
        key = (m.group("module"), m.group("port"))
        old = ports.get(key, StubPortDecl(module=key[0], port=key[1]))
        ports[key] = StubPortDecl(
            module=key[0],
            port=key[1],
            feedthrough=values.get("feedthrough", old.feedthrough),
            hold=values.get("hold", old.hold),
            origin=where,
        )
    return StubDecls(ports=ports, problems=tuple(problems))


def scan_stubs(source_files: list[Path]) -> StubDecls:
    """Read each source file as text and collect its stub declarations;
    a later pragma for the same output overrides the keys it sets."""
    ports: dict[tuple[str, str], StubPortDecl] = {}
    problems: list[str] = []
    for src in source_files:
        p = Path(src)
        found = scan_stub_text(p.read_text(encoding="utf-8", errors="replace"), p)
        for key, decl in found.ports.items():
            old = ports.get(key)
            if old is not None:
                decl = StubPortDecl(
                    module=decl.module,
                    port=decl.port,
                    feedthrough=(
                        decl.feedthrough
                        if decl.feedthrough is not None
                        else old.feedthrough
                    ),
                    hold=decl.hold if decl.hold is not None else old.hold,
                    origin=decl.origin,
                )
            ports[key] = decl
        problems.extend(found.problems)
    return StubDecls(ports=ports, problems=tuple(problems))
