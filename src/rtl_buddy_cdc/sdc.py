"""SDC parser — the CDC-relevant subset.

Supported commands:

    create_clock -name <name> -period <p> [get_ports <port>]
    create_generated_clock -name <name> -source <port-or-pin> \\
                           [-master_clock <name>] [-divide_by N] \\
                           [-multiply_by N] [-edges …] [get_pins <pin>]
    set_clock_groups -asynchronous          -group {…} -group {…} …
    set_clock_groups -logically_exclusive   -group {…} -group {…} …
    set_clock_groups -physically_exclusive  -group {…} -group {…} …
    set_false_path  -from [get_clocks A] -to [get_clocks B]
    set_input_delay  -clock <name> … [get_ports <port>]
    set_output_delay -clock <name> … [get_ports <port>]

Numeric delays, slack, drive, load — all silently dropped. The parser
only cares about clock topology and async-partitioning, not timing.

The implementation has two layers (see issue #144 for the design
discussion). Layer 1 is a Tcl-aware word tokenizer: ``{...}`` braces
and ``[...]`` brackets are single tokens with nesting respected, ``\\``
collapses line continuation to a space, ``"..."`` strips quoting, ``#``
at a word boundary comments to end-of-line. Layer 2 is a per-command
:data:`ARG_SPECS` table — each command declares its flags with arity
(:class:`Arity.ZERO` / ``ONE`` / ``GREEDY``); the slicer turns a word
list into a (flags, tail) bag the handlers consume directly. No
per-command "walk forward until the next ``-flag``" loops.

This is **not** a Tcl interpreter. The choice is deliberate: real Tcl
interpreters execute user code, add a non-Python dependency, and
complicate deployment. The documented unsupported constructs are
command substitution beyond ``[get_clocks …]`` / ``[get_ports …]`` /
``[get_pins …]``, ``set`` variables, ``expr``, and ``-filter`` clauses.
When the parser sees a CDC-relevant command it can't fully understand
it appends to :attr:`ClockSpec.partial_warnings` so the caller can
surface a single end-of-parse warning rather than spamming line-by-line.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from rtl_buddy_cdc.netlist import Module

logger = logging.getLogger(__name__)

# Synthetic clock name assigned to input ports that have no
# ``set_input_delay -clock`` typing. Picked to be obviously not a real
# clock identifier (angle brackets are illegal in Verilog/SDC names) so
# it never collides with anything the user could have written. CDC-011
# owns crossings carrying this as ``src_clock``; CDC-001 / CDC-002 /
# CDC-006 skip them to avoid double-firing. ``ClockSpec.are_async``
# treats the sentinel as async to every real clock — the whole point
# is that we *don't know* what domain the port lives in, so any flop
# capture is a potential cross.
UNCONSTRAINED_SENTINEL = "<unconstrained>"


@dataclass(frozen=True)
class Clock:
    name: str
    period: float
    ports: tuple[str, ...]  # top-level port names this clock is associated with
    master: str | None = None  # name of the master clock when generated
    is_generated: bool = False


@dataclass
class ClockSpec:
    """The fully parsed CDC-relevant view of an SDC file."""

    clocks: dict[str, Clock] = field(default_factory=dict)
    # Each entry is one ``set_clock_groups -asynchronous`` invocation,
    # holding the list of groups. Clocks in different groups within
    # the same statement are asynchronous.
    async_groups: list[list[set[str]]] = field(default_factory=list)
    # ``-logically_exclusive`` / ``-physically_exclusive`` groups —
    # treated identically for CDC: clocks in different exclusive
    # groups never coexist at runtime, so a flop→flop crossing
    # between them is unreachable and should be filtered out, not
    # checked.
    exclusive_groups: list[list[set[str]]] = field(default_factory=list)
    # Unordered ``{src, dst}`` pairs declared as false-paths between
    # clock domains. Treated as async hints (CDC equivalent of
    # ``set_clock_groups -asynchronous`` for a specific pair).
    false_path_pairs: set[frozenset[str]] = field(default_factory=set)
    # Top-level port → clock name from ``set_input_delay -clock …`` /
    # ``set_output_delay -clock …``. Lets the caller assign a clock
    # domain to data ports that aren't reached by any flop's CLK
    # tracing.
    port_clock: dict[str, str] = field(default_factory=dict)
    # Internal pin path (e.g. ``"u_a/clk_out"``) → generated clock name,
    # populated when a ``create_generated_clock`` target is a
    # ``[get_pins …]`` expression rather than a top-level port. The
    # clock-trace pass consults this to stop walking back through the
    # netlist at the point where a generated clock takes over, instead
    # of collapsing every flop to whichever top input port feeds the
    # chain. The pin path uses SDC convention (``/`` separator); the
    # consumer normalises to Yosys' flattened netname (``.`` separator).
    pin_clocks: dict[str, str] = field(default_factory=dict)
    # Diagnostics accumulated during parse. Each entry is a single
    # human-readable sentence describing a CDC-relevant command the
    # parser couldn't fully understand. Surfaced once at the end of
    # parsing rather than logged per-line.
    partial_warnings: list[str] = field(default_factory=list)

    # ---- consumer-facing helpers ------------------------------------------

    def clock_for_port(self, port: str) -> str | None:
        """Return the clock name associated with ``port``.

        Checks ``set_*_delay``-derived ``port_clock`` first (a port
        explicitly typed by the user), then falls back to scanning
        ``create_clock`` port lists. Returns ``None`` if the port is
        not associated with any clock.
        """
        if port in self.port_clock:
            return self.port_clock[port]
        for clk in self.clocks.values():
            if port in clk.ports:
                return clk.name
        return None

    def resolve(self, name: str) -> str:
        """Collapse a generated clock to its root master.

        Generated clocks (dividers, PLL outputs, muxes declared via
        ``create_generated_clock``) are synchronous to their master
        from a CDC standpoint — same source, just different period.
        ``resolve`` follows the ``master`` chain transitively. Cycles
        are guarded against (a misdeclared file shouldn't infinite-
        loop the analyzer); the first clock seen twice terminates the
        walk and the most-recent name is returned.
        """
        seen: set[str] = set()
        cur = name
        while cur in self.clocks and self.clocks[cur].master is not None:
            if cur in seen:
                return cur
            seen.add(cur)
            master = self.clocks[cur].master
            if master is None:
                break
            cur = master
        return cur

    def are_async(self, a: str, b: str) -> bool:
        """Return True iff clocks ``a`` and ``b`` are declared asynchronous.

        Order of checks:

        1. **Unresolved-name async groups** — if the SDC explicitly
           lists ``a`` and ``b`` in different groups of the same
           ``set_clock_groups -asynchronous`` statement, that wins
           over generated→master collapse. This is the
           "explicit override" case: the user is telling us a
           generated clock should be treated async to its master,
           and we obey.
        2. **Resolved roots** — collapse generated clocks to their
           masters and compare. Same-root → sync. Otherwise check
           ``false_path_pairs`` and async groups against the
           resolved roots too.
        """
        if a == b:
            return False
        # Sentinel port-clock (synthesised for input ports without
        # ``set_input_delay -clock`` typing) is treated as async to
        # every real clock — we don't know the port's domain, so any
        # flop capture is a potential cross. CDC-011 owns the rule
        # that fires; the async flag here just keeps the crossing in
        # the filtered list so CDC-011 sees it.
        if a == UNCONSTRAINED_SENTINEL or b == UNCONSTRAINED_SENTINEL:
            return True
        # Step 1: explicit override on unresolved names.
        for groups in self.async_groups:
            ga = next((g for g in groups if a in g), None)
            gb = next((g for g in groups if b in g), None)
            if ga is not None and gb is not None and ga is not gb:
                return True
        # Step 2: resolved-root comparison.
        ra = self.resolve(a)
        rb = self.resolve(b)
        if ra == rb:
            return False
        if frozenset({ra, rb}) in self.false_path_pairs:
            return True
        if frozenset({a, b}) in self.false_path_pairs:
            return True
        for groups in self.async_groups:
            ga = next((g for g in groups if ra in g), None)
            gb = next((g for g in groups if rb in g), None)
            if ga is not None and gb is not None and ga is not gb:
                return True
        return False

    def is_unreachable_crossing(self, a: str, b: str) -> bool:
        """Return True iff ``a`` and ``b`` are in different exclusive groups.

        Logically/physically-exclusive clocks never coexist at
        runtime (typical case: a 2:1 clock mux that selects either
        ``ck0`` or ``ck1``). A flop→flop "crossing" between them is a
        static-analysis artifact, not a real path, and should be
        dropped before any rule sees it.
        """
        if a == b:
            return False
        ra = self.resolve(a)
        rb = self.resolve(b)
        if ra == rb:
            return False
        for groups in self.exclusive_groups:
            ga = next((g for g in groups if ra in g or a in g), None)
            gb = next((g for g in groups if rb in g or b in g), None)
            if ga is not None and gb is not None and ga is not gb:
                return True
        return False


# ---- parser entry points ----------------------------------------------------


def parse(text: str) -> ClockSpec:
    spec = ClockSpec()
    for words in _tokenize(text):
        if not words:
            continue
        cmd, args = words[0], words[1:]
        spec_for_cmd = ARG_SPECS.get(cmd)
        if spec_for_cmd is None:
            # Drop noise (set_max_delay, set_load, set_drive, …) at
            # DEBUG level so users can see what was skipped via
            # ``--verbose`` without the report being flooded.
            logger.debug("sdc: ignoring unsupported command %r", cmd)
            continue
        parsed = _slice(args, spec_for_cmd)
        _DISPATCH[cmd](spec, parsed)
    return spec


def parse_file(path: str | Path) -> ClockSpec:
    return parse(Path(path).read_text())


def synthesize_unconstrained_inputs(spec: ClockSpec, module: "Module") -> list[str]:
    """Assign :data:`UNCONSTRAINED_SENTINEL` to input ports not already
    typed by the SDC.

    Mutates ``spec.port_clock`` in place and returns the list of port
    names that received the sentinel (caller may want to surface this
    in verbose output). A port is considered untyped if
    :meth:`ClockSpec.clock_for_port` returns ``None`` — that covers
    both "no ``set_input_delay``" and "``set_input_delay`` without
    ``-clock``" (the parser warning at ``_handle_set_delay`` already
    surfaces the latter as a misuse).

    Called by the CLI after SDC parse and netlist load, before
    :func:`rtl_buddy_cdc.domain.find_crossings`. The sentinel
    propagates through the port-walk as the crossing's ``src_clock``;
    :func:`ClockSpec.are_async` treats it as async to every real
    clock, so the resulting port-sourced crossings reach the rule
    pack. CDC-011 owns them; CDC-001 / CDC-002 / CDC-006 skip them to
    avoid double-firing with a fix-advice mismatch.
    """
    sentinel_ports: list[str] = []
    for port in module.ports.values():
        if port.direction != "input":
            continue
        if spec.clock_for_port(port.name) is not None:
            continue
        spec.port_clock[port.name] = UNCONSTRAINED_SENTINEL
        sentinel_ports.append(port.name)
    return sentinel_ports


# ---- Layer 1: Tcl-aware word tokenizer --------------------------------------


def _tokenize(text: str) -> list[list[str]]:
    """Tokenize SDC source into a list of word lists, one per command line.

    Handles the Tcl-flavoured syntax that SDC actually uses:

    - Whitespace splits words at the top level.
    - ``{...}`` braces — single word, nested braces respected, content
      kept literal (we don't substitute inside).
    - ``[...]`` brackets — single word, nested brackets respected,
      content kept literal (we treat the bracket span as opaque; the
      handler peels ``get_ports`` / ``get_pins`` / ``get_clocks``).
    - ``"..."`` double-quotes — single word; the quotes are stripped
      and ``\\<c>`` escapes the next character inside.
    - ``\\<newline>`` line continuation collapses to a single space.
    - ``#`` at a word boundary starts a comment to end-of-line; the
      partial command (if any) is flushed, matching the existing
      "comments break continuation" behaviour.
    - ``\\n`` ends a logical command.

    Out-of-scope on purpose (see issue #144's "Rejected alternative"
    section): variable expansion (``$x``), ``expr``, ``proc``, command
    substitution evaluation, ``source`` includes. If any of these
    become real requirements, switch to ``tkinter.Tcl()`` rather than
    grow them onto this tokenizer.
    """
    lines: list[list[str]] = []
    current: list[str] = []
    word: list[str] = []
    i = 0
    n = len(text)

    def flush_word() -> None:
        if word:
            current.append("".join(word))
            word.clear()

    def flush_line() -> None:
        flush_word()
        if current:
            lines.append(current.copy())
            current.clear()

    while i < n:
        c = text[i]

        # Line continuation: backslash-newline collapses to whitespace.
        if c == "\\" and i + 1 < n and text[i + 1] == "\n":
            flush_word()
            i += 2
            continue

        # Newline ends the logical command.
        if c == "\n":
            flush_line()
            i += 1
            continue

        # Top-level whitespace splits words.
        if c in " \t\r":
            flush_word()
            i += 1
            continue

        # Comment at word boundary: skip to end-of-line. A comment that
        # appears between continued lines breaks the continuation,
        # matching the previous line-based behaviour.
        if c == "#" and not word:
            flush_line()
            while i < n and text[i] != "\n":
                i += 1
            continue

        # Brace word — nested braces respected, content kept literal.
        if c == "{" and not word:
            depth = 1
            start = i
            i += 1
            while i < n and depth > 0:
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            current.append(text[start:i])
            continue

        # Bracket word — nested brackets respected, content kept literal.
        if c == "[" and not word:
            depth = 1
            start = i
            i += 1
            while i < n and depth > 0:
                ch = text[i]
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                i += 1
            current.append(text[start:i])
            continue

        # Double-quoted word — strip quotes; backslash escapes the next char.
        if c == '"' and not word:
            i += 1
            buf: list[str] = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            current.append("".join(buf))
            if i < n:
                i += 1  # closing quote
            continue

        word.append(c)
        i += 1

    flush_line()
    return lines


# ---- Layer 2: per-command argument specs ------------------------------------


class Arity(enum.Enum):
    """Operand shape a flag carries in an SDC command.

    - :attr:`ZERO` — bare flag with no operand (``-asynchronous``).
    - :attr:`ONE` — flag followed by exactly one word
      (``-name foo``, ``-period 10``). With a Tcl-aware tokenizer
      ``{ck_a ck_b}`` and ``[get_ports clk]`` are *single words*, so
      collection-valued flags still classify as :attr:`ONE`.
    - :attr:`GREEDY` — flag slurps every non-flag word up to the next
      ``-flag`` or end-of-command. Used for endpoint flags
      (``-from``/``-to``) and ``-group`` where SDC files in the wild
      sometimes drop the braces and supply bare names directly.
    """

    ZERO = enum.auto()
    ONE = enum.auto()
    GREEDY = enum.auto()


@dataclass(frozen=True)
class ArgSpec:
    """Per-command flag table.

    :attr:`flags` maps a flag name (with leading dash) to its arity.
    :attr:`repeated` lists the flags whose multiple occurrences are
    semantically distinct (e.g. ``-group``); unlisted flags overwrite
    on re-occurrence, which matches "last write wins" SDC semantics
    for flags like ``-name``.
    """

    flags: dict[str, Arity]
    repeated: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Parsed:
    """Result of slicing a word list against an :class:`ArgSpec`.

    ``flags`` is keyed by every flag in the spec (so a handler can
    distinguish "flag absent" from "flag present with falsy value"
    without a ``KeyError``). Each value is a list of *occurrences*:

    - :attr:`Arity.ZERO`: each occurrence is ``True``.
    - :attr:`Arity.ONE`: each occurrence is the operand word (``str``).
    - :attr:`Arity.GREEDY`: each occurrence is the list of slurped
      non-flag words (``list[str]``).

    Use :meth:`present`, :meth:`first`, and :meth:`all` for ergonomic
    access — handlers should rarely touch ``flags`` directly.
    """

    flags: dict[str, list[Any]]
    tail: list[str]

    def present(self, flag: str) -> bool:
        """Return True if ``flag`` appeared at least once."""
        return bool(self.flags.get(flag))

    def first(self, flag: str) -> Any:
        """Return the first occurrence of ``flag``'s operand, or ``None``."""
        v = self.flags.get(flag)
        return v[0] if v else None

    def all(self, flag: str) -> list[Any]:
        """Return every occurrence of ``flag``'s operand (empty list if absent)."""
        return self.flags.get(flag, []) or []


def _slice(words: list[str], spec: ArgSpec) -> Parsed:
    """Walk a tokenized command and bucket its operands per :class:`ArgSpec`.

    Unknown flags (anything starting with ``-`` not in the spec's flag
    table) are tolerated using a heuristic: if the next word doesn't
    look like another flag, both are skipped (assume one-operand
    arity); otherwise just the flag is skipped. This preserves the
    previous parser's "ignore vendor dialect quietly" behaviour for
    flags the CDC pack doesn't care about.
    """
    flags: dict[str, list[Any]] = {f: [] for f in spec.flags}
    tail: list[str] = []
    i = 0
    n = len(words)
    while i < n:
        w = words[i]
        if w in spec.flags:
            arity = spec.flags[w]
            if arity is Arity.ZERO:
                flags[w].append(True)
                i += 1
            elif arity is Arity.ONE:
                if i + 1 < n:
                    flags[w].append(words[i + 1])
                    i += 2
                else:
                    i += 1
            else:  # GREEDY
                j = i + 1
                slurp: list[str] = []
                while j < n and not words[j].startswith("-"):
                    slurp.append(words[j])
                    j += 1
                flags[w].append(slurp)
                i = j
        elif w.startswith("-"):
            # Unknown flag — apply the conservative skip heuristic.
            if i + 1 < n and not words[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
        else:
            tail.append(w)
            i += 1
    return Parsed(flags=flags, tail=tail)


# Shared spec for set_input_delay / set_output_delay — same flag table,
# same handler, only the docstring differs.
_DELAY_SPEC = ArgSpec(
    flags={
        "-clock": Arity.ONE,
        "-min": Arity.ZERO,
        "-max": Arity.ZERO,
        "-add_delay": Arity.ZERO,
        "-network_latency_included": Arity.ZERO,
        "-source_latency_included": Arity.ZERO,
        "-clock_fall": Arity.ZERO,
        "-rise": Arity.ZERO,
        "-fall": Arity.ZERO,
        "-reference_pin": Arity.ONE,
        "-level_sensitive": Arity.ONE,
    },
)

ARG_SPECS: dict[str, ArgSpec] = {
    "create_clock": ArgSpec(
        flags={
            "-name": Arity.ONE,
            "-period": Arity.ONE,
            "-waveform": Arity.ONE,
            "-add": Arity.ZERO,
            "-comment": Arity.ONE,
        },
    ),
    "create_generated_clock": ArgSpec(
        flags={
            "-name": Arity.ONE,
            "-source": Arity.ONE,
            "-master_clock": Arity.ONE,
            "-divide_by": Arity.ONE,
            "-multiply_by": Arity.ONE,
            "-edges": Arity.ONE,
            "-edge_shift": Arity.ONE,
            "-duty_cycle": Arity.ONE,
            "-invert": Arity.ZERO,
            "-add": Arity.ZERO,
            "-combinational": Arity.ZERO,
            "-comment": Arity.ONE,
        },
    ),
    "set_clock_groups": ArgSpec(
        flags={
            "-asynchronous": Arity.ZERO,
            "-logically_exclusive": Arity.ZERO,
            "-physically_exclusive": Arity.ZERO,
            "-allow_paths": Arity.ZERO,
            "-name": Arity.ONE,
            "-comment": Arity.ONE,
            "-group": Arity.GREEDY,
        },
        repeated=frozenset({"-group"}),
    ),
    "set_false_path": ArgSpec(
        flags={
            "-from": Arity.GREEDY,
            "-to": Arity.GREEDY,
            "-rise_from": Arity.GREEDY,
            "-fall_from": Arity.GREEDY,
            "-rise_to": Arity.GREEDY,
            "-fall_to": Arity.GREEDY,
            "-through": Arity.GREEDY,
            "-rise_through": Arity.GREEDY,
            "-fall_through": Arity.GREEDY,
            "-comment": Arity.ONE,
            "-reset_path": Arity.ZERO,
            "-setup": Arity.ZERO,
            "-hold": Arity.ZERO,
        },
    ),
    "set_input_delay": _DELAY_SPEC,
    "set_output_delay": _DELAY_SPEC,
}


# ---- handlers ---------------------------------------------------------------


def _handle_create_clock(spec: ClockSpec, p: Parsed) -> None:
    name = p.first("-name")
    period_word = p.first("-period")
    if period_word is None:
        return
    try:
        period = float(period_word)
    except ValueError:
        return

    ports: list[str] = []
    saw_filter = False
    for word in p.tail:
        names, sf = _extract_names(word)
        ports.extend(names)
        saw_filter = saw_filter or sf

    if name is None and ports:
        name = ports[0]
    if name is None:
        return

    if saw_filter:
        spec.partial_warnings.append(
            f"create_clock {name}: ignored unsupported [get_ports -filter ...]"
        )
    spec.clocks[name] = Clock(name=name, period=period, ports=tuple(ports))


def _handle_create_generated_clock(spec: ClockSpec, p: Parsed) -> None:
    """Parse ``create_generated_clock``.

    The CDC-relevant fields are ``-name`` and either ``-master_clock``
    or ``-source`` (both indicate the upstream clock). ``-divide_by`` /
    ``-multiply_by`` only matter for the period; CDC treats the
    generated clock as synchronous to its master regardless of ratio.
    The pin/port target is captured for completeness — pin targets
    land in :attr:`ClockSpec.pin_clocks` so the clock-trace pass can
    stop walking at the divider boundary instead of collapsing every
    downstream flop to the upstream port.
    """
    name = p.first("-name")
    master_word = p.first("-master_clock")
    master = _strip_get_clocks(master_word) if master_word is not None else None

    divide_by = _safe_int(p.first("-divide_by"), default=1)
    multiply_by = _safe_int(p.first("-multiply_by"), default=1)

    targets: list[str] = []
    saw_filter = False
    target_is_pin = any("get_pins" in w for w in p.tail)
    for word in p.tail:
        names, sf = _extract_names(word)
        targets.extend(names)
        saw_filter = saw_filter or sf

    if name is None and targets:
        name = targets[0]
    if name is None:
        spec.partial_warnings.append(
            "create_generated_clock: missing -name (and no fallback target)"
        )
        return

    # Derive a placeholder period so downstream code that reads
    # ``clock.period`` doesn't crash on generated clocks. If the master
    # is known and has a period, scale; otherwise default to 0.0 (CDC
    # ignores period anyway).
    period = 0.0
    if master is not None and master in spec.clocks and divide_by > 0:
        period = spec.clocks[master].period * divide_by / max(multiply_by, 1)

    if saw_filter:
        spec.partial_warnings.append(
            f"create_generated_clock {name}: ignored unsupported -filter clause"
        )

    # Pin-targeted generated clocks (e.g. ``[get_pins u_a/clk_out]``)
    # don't belong in ``Clock.ports`` — that field is reserved for
    # top-level port names so ``clock_for_port`` can do a clean port→
    # clock lookup. Pin targets land in ``spec.pin_clocks`` instead,
    # consumed by the clock-trace pass.
    if target_is_pin:
        clock_ports: tuple[str, ...] = ()
        for t in targets:
            spec.pin_clocks[t] = name
    else:
        clock_ports = tuple(targets)

    spec.clocks[name] = Clock(
        name=name,
        period=period,
        ports=clock_ports,
        master=master,
        is_generated=True,
    )


def _handle_set_clock_groups(spec: ClockSpec, p: Parsed) -> None:
    is_async = p.present("-asynchronous")
    is_logical = p.present("-logically_exclusive")
    is_physical = p.present("-physically_exclusive")

    if not (is_async or is_logical or is_physical):
        spec.partial_warnings.append(
            "set_clock_groups: missing -asynchronous / "
            "-logically_exclusive / -physically_exclusive — ignored"
        )
        return

    groups: list[set[str]] = []
    for slurped in p.all("-group"):
        # ``slurped`` is a list[str] of words after ``-group``, up to
        # the next ``-flag``. With a Tcl-aware tokenizer the common
        # case is a single word like ``{ck_a ck_b}`` or
        # ``[get_clocks ck_a]``; the GREEDY arity also tolerates the
        # un-collection form ``-group ck_a ck_b`` real SDC sometimes
        # uses.
        members = _extract_clock_list(" ".join(slurped))
        if members:
            groups.append(set(members))

    if len(groups) < 2:
        spec.partial_warnings.append(
            "set_clock_groups: fewer than 2 -group clauses, ignored"
        )
        return

    if is_async:
        spec.async_groups.append(groups)
    if is_logical or is_physical:
        spec.exclusive_groups.append(groups)


def _handle_set_false_path(spec: ClockSpec, p: Parsed) -> None:
    """Parse ``set_false_path -from [get_clocks A] -to [get_clocks B]``.

    Treated as a pairwise async declaration when both endpoints are
    clock collections. ``-through`` makes this path-specific (not a
    clock-pair hint) — we drop those with a partial-parse warning.
    """
    if (
        p.present("-through")
        or p.present("-rise_through")
        or p.present("-fall_through")
    ):
        spec.partial_warnings.append(
            "set_false_path: -through clauses are path-specific and "
            "not interpreted as clock-pair async hints"
        )
        return

    from_clocks: list[str] = []
    to_clocks: list[str] = []
    saw_non_clock_endpoint = False

    for flag, sink in (
        ("-from", from_clocks),
        ("-rise_from", from_clocks),
        ("-fall_from", from_clocks),
        ("-to", to_clocks),
        ("-rise_to", to_clocks),
        ("-fall_to", to_clocks),
    ):
        for slurped in p.all(flag):
            blob = " ".join(slurped)
            if "get_pins" in blob or "get_cells" in blob or "get_ports" in blob:
                saw_non_clock_endpoint = True
                continue
            names: list[str] = []
            for word in slurped:
                ns, _ = _extract_names(word)
                names.extend(ns)
            sink.extend(names)

    if saw_non_clock_endpoint:
        spec.partial_warnings.append(
            "set_false_path: non-clock endpoints (get_pins/get_cells/"
            "get_ports) are path-specific and not interpreted"
        )
        return

    if not from_clocks or not to_clocks:
        spec.partial_warnings.append(
            "set_false_path: incomplete -from/-to clock list, ignored"
        )
        return

    for a in from_clocks:
        for b in to_clocks:
            if a != b:
                spec.false_path_pairs.add(frozenset({a, b}))


def _handle_set_delay(spec: ClockSpec, p: Parsed) -> None:
    """Parse ``set_input_delay`` / ``set_output_delay``.

    The only CDC-relevant fields are ``-clock <name>`` and the
    trailing ``[get_ports <port>]``. The numeric delay value lands in
    ``p.tail`` alongside the port collection — we pick out anything
    that looks like a collection and ignore the rest.
    """
    clock_word = p.first("-clock")
    clock = _strip_get_clocks(clock_word) if clock_word is not None else None

    ports: list[str] = []
    saw_filter = False
    for word in p.tail:
        if word.startswith("[") or word.startswith("{") or "get_ports" in word:
            names, sf = _extract_names(word)
            ports.extend(names)
            saw_filter = saw_filter or sf

    if saw_filter:
        spec.partial_warnings.append("set_*_delay: ignored unsupported -filter clause")
    if not ports:
        # No port target — delay-only constraint or defaults-applies-
        # to-all-ports usage. Nothing actionable for CDC; stay silent.
        return
    if clock is None:
        # Port target named but no -clock anchor. set_input_delay /
        # set_output_delay are intrinsically clock-relative (the delay
        # is a fraction of a clock period), so without -clock the
        # constraint has no STA semantics and most real timers reject
        # it. Common misuse: users reach for set_input_delay when they
        # meant set_input_transition (slew) or set_load. Warn so the
        # mistake doesn't silently produce an untyped port.
        spec.partial_warnings.append(
            f"set_*_delay on {sorted(ports)} has no -clock anchor; "
            "the constraint is ignored. Add -clock <name>, or use "
            "set_input_transition / set_load for slew or load defaults."
        )
        return
    for port in ports:
        spec.port_clock[port] = clock


_DISPATCH = {
    "create_clock": _handle_create_clock,
    "create_generated_clock": _handle_create_generated_clock,
    "set_clock_groups": _handle_set_clock_groups,
    "set_false_path": _handle_set_false_path,
    "set_input_delay": _handle_set_delay,
    "set_output_delay": _handle_set_delay,
}


# ---- collection-peeling helpers --------------------------------------------


def _extract_names(word: str) -> tuple[list[str], bool]:
    """Peel a single tokenized word into a list of identifier names.

    Accepts the three shapes a port/pin/clock argument can take:

    - ``{ck0 ck1}`` — brace collection, names are whitespace-separated.
    - ``[get_ports ck0 ck1]`` — bracket form, optional ``get_*`` head
      stripped, ``-filter`` clauses dropped (returns ``saw_filter=True``
      so the caller can surface a partial-parse warning).
    - ``ck0`` — bare identifier.

    Returns ``(names, saw_filter)``.
    """
    saw_filter = "-filter" in word
    cleaned = word
    for chunk in ("[", "]", "{", "}"):
        cleaned = cleaned.replace(chunk, " ")
    parts = cleaned.split()
    if parts and parts[0] in {"get_ports", "get_pins", "get_clocks"}:
        parts = parts[1:]
    if saw_filter:
        # Filter expressions can contain anything; drop everything
        # from -filter onwards. Names that appeared before -filter are
        # still valid, but in practice nothing precedes it and the
        # list ends up empty.
        try:
            cut = parts.index("-filter")
            parts = parts[:cut]
        except ValueError:
            pass
    cleaned_names: list[str] = []
    for tok in parts:
        if tok == "-include_generated_clocks":
            continue
        if tok.startswith("-"):
            # Conservative: skip vendor flags inside a get_* expression.
            continue
        cleaned_names.append(tok)
    return cleaned_names, saw_filter


def _strip_get_clocks(token: str) -> str:
    """Reduce ``"[get_clocks foo]"`` / ``"{foo}"`` / ``"foo"`` to ``"foo"``."""
    names, _ = _extract_names(token)
    return names[0] if names else token


def _extract_clock_list(token: str) -> list[str]:
    """Turn ``"{src_clk dst_clk}"`` / ``"src_clk"`` into a name list."""
    names, _ = _extract_names(token)
    return names


def _safe_int(word: Any, *, default: int) -> int:
    """``int(word)`` with a default fallback (mirrors the old try/except)."""
    if word is None:
        return default
    try:
        return int(word)
    except (TypeError, ValueError):
        return default
