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

The implementation has two layers (issue #144 for the original design
discussion, rtl-buddy-cdc#298 for the reader split). Layer 1 *reads*
the file into one word list per command; Layer 2 is a per-command
:data:`ARG_SPECS` table — each command declares its flags with arity
(:class:`Arity.ZERO` / ``ONE`` / ``GREEDY``); the slicer turns a word
list into a (flags, tail) bag the handlers consume directly. No
per-command "walk forward until the next ``-flag``" loops.

Layer 1 has **two interchangeable backends**, selected by
:func:`backend` (override with the ``backend=`` argument to
:func:`parse` / :func:`parse_file`, or the ``RB_CDC_SDC_BACKEND``
environment variable):

``tcl`` (preferred, used whenever ``_tkinter`` imports)
    ``tkinter.Tcl()`` → ``interp create -safe`` → an ``unknown``
    handler aliased back into Python. Real Tcl evaluation, so ``set``
    variables, ``expr``, ``\\`` continuation and nested command
    substitution all work; ``get_*`` / ``all_*`` collections are
    recorded, never resolved against the design. The safe interp has
    no ``exec`` / ``open`` / ``file`` / ``socket`` / ``load`` /
    ``source``, so a constraints file cannot read, write or run
    anything — those names land in ``unknown`` with everything else
    and are reported, not executed.

``tokenizer`` (fallback, when ``_tkinter`` is missing)
    :func:`rtl_buddy_cdc.tcl_tokenizer._tokenize`, the hand-written
    Tcl-aware word splitter: ``{...}`` braces and ``[...]`` brackets
    are single opaque tokens with nesting respected, ``\\`` collapses
    line continuation to a space, ``"..."`` strips quoting, ``#`` at a
    word boundary comments to end-of-line. It does **not** evaluate
    ``$var``, ``expr`` or command substitution, so a clock declared
    through a variable is invisible to it; the first such drop per
    file raises a ``sdc.tokenizer_skipped`` warning, and a missing
    ``_tkinter`` raises one ``sdc.tcl_unavailable`` warning per run
    naming the fix.

Both backends feed the same slicer, handlers and :class:`ClockSpec`,
so every downstream consumer is backend-agnostic. When the parser sees
a CDC-relevant command it can't fully understand it appends to
:attr:`ClockSpec.partial_warnings` so the caller can surface a single
end-of-parse warning rather than spamming line-by-line.
"""

from __future__ import annotations

import enum
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

# The tokenizer reader lives in its own stdlib-only module so rtl-buddy
# can vendor it verbatim (rtl-buddy-cdc#298). Re-exported here because
# every existing caller and test imports ``sdc._tokenize`` /
# ``sdc._extract_names``.
from rtl_buddy_cdc.tcl_tokenizer import _extract_names, _tokenize

if TYPE_CHECKING:
    from rtl_buddy_cdc.netlist import Module

__all__ = [
    "Arity",
    "ARG_SPECS",
    "BACKENDS",
    "BACKEND_ENV_VAR",
    "Clock",
    "ClockSpec",
    "TKINTER_AVAILABLE",
    "TclReadError",
    "TclResourceLimitError",
    "UNCONSTRAINED_SENTINEL",
    "backend",
    "backend_description",
    "parse",
    "parse_file",
    "synthesize_unconstrained_inputs",
    "validate_clock_graph",
    "_extract_names",
    "_tokenize",
]

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


def parse(text: str, *, backend: str | None = None) -> ClockSpec:
    """Parse SDC ``text`` into a :class:`ClockSpec`.

    ``backend`` forces a Layer-1 reader (``"tcl"`` or ``"tokenizer"``);
    ``None`` (the default) resolves it from ``RB_CDC_SDC_BACKEND`` and
    then from ``_tkinter`` availability. Asking for ``"tcl"`` without
    ``_tkinter`` degrades to the tokenizer with a warning rather than
    raising — see :func:`_resolve_backend`.

    Line endings are normalised first: a Windows-authored file ends a
    ``\\``-continued line with ``\\\r\n``, which neither backend
    treats as a continuation (Tcl sees a backslash-escaped ``\r`` and
    then a newline), so the continued command silently lost its
    arguments.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    spec = ClockSpec()
    effective = _resolve_backend(backend)
    commands: list[_Command] | None = None
    if effective == "tcl":
        try:
            commands = _read_tcl(text)
        except TclReadError as exc:
            # A genuine Tcl error (bad syntax, undefined variable, a
            # command erroring out) or a resource limit aborts the
            # script part-way, so the capture is incomplete. Discard it
            # and re-read the whole file with the tokenizer, which
            # never fails and never loops, and say so.
            if isinstance(exc, TclResourceLimitError):
                what = (
                    f"Tcl interpreter backend hit a resource limit "
                    f"({_one_line(exc)}) and the file was cut off part-way "
                    f"— a runaway loop or recursion in the constraints?"
                )
            else:
                what = (
                    f"Tcl interpreter backend could not evaluate this file "
                    f"({_one_line(exc)})"
                )
            spec.partial_warnings.append(
                f"{what}; re-read with the word tokenizer, so "
                f"$var / expr / command substitution were not evaluated"
            )
            effective = "tokenizer"
            commands = None
    if commands is None:
        commands = _read_tokenizer(text)

    skip_reported = False

    def note_tokenizer_skip(what: str) -> None:
        nonlocal skip_reported
        if effective != "tokenizer" or skip_reported:
            return
        skip_reported = True
        logger.warning(
            "sdc.tokenizer_skipped: %s. The word-tokenizer backend does not "
            "evaluate $var, expr or command substitution, so constraints "
            "expressed through them are invisible to the CDC analysis.%s",
            what,
            "" if TKINTER_AVAILABLE else f" {_TKINTER_FIX_HINT}",
        )

    for command in commands:
        words = command.words
        if not words:
            continue
        cmd, args = words[0], words[1:]
        unsupported = _UNSUPPORTED_COMMANDS.get(cmd)
        if unsupported is not None:
            target = f" {' '.join(args)!r}" if args else ""
            spec.partial_warnings.append(
                f"{cmd}{target}{_at_line(command.line)}: {unsupported}"
            )
            continue
        spec_for_cmd = ARG_SPECS.get(cmd)
        if spec_for_cmd is None:
            # Drop noise (set_max_delay, set_load, set_drive, …) at
            # DEBUG level so users can see what was skipped via
            # ``--verbose`` without the report being flooded.
            logger.debug("sdc: ignoring unsupported command %r", cmd)
            note_tokenizer_skip(f"dropped unrecognised command {cmd!r}")
            continue
        if any("$" in w for w in words):
            note_tokenizer_skip(f"{cmd}: kept a literal $-word unevaluated")
        parsed = _slice(args, spec_for_cmd)
        _DISPATCH[cmd](spec, parsed)
    return spec


def parse_file(path: str | Path, *, backend: str | None = None) -> ClockSpec:
    return parse(Path(path).read_text(), backend=backend)


def _one_line(exc: Exception) -> str:
    """Flatten a multi-line Tcl error message into a single sentence."""
    return " ".join(str(exc).split())


def validate_clock_graph(spec: ClockSpec) -> list[str]:
    """G-11 (rtl-buddy-cdc#218): cross-statement clock-graph diagnostics.

    The per-command parsers (:func:`_handle_create_clock` etc.) catch
    line-local issues like missing ``-name`` or bare ``-filter``
    clauses; this validator looks at the *collective* clock graph
    after every statement has been parsed and surfaces shapes that
    individually-valid statements compose into an inconsistent or
    undefined whole.

    Returns a list of human-readable diagnostic sentences (same shape
    as :attr:`ClockSpec.partial_warnings`); the caller is expected to
    merge them into ``spec.partial_warnings`` so they flow through
    the existing CLI warning surface.

    Checks:

    1. **Same port in multiple clocks** — two ``create_clock``s
       declaring different clock names but listing the same top-level
       port. ``clock_for_port`` is deterministic (it returns the first
       match found while scanning ``spec.clocks.values()``) but the
       user's intent is ambiguous; the value-derived rules (CDC-001,
       CDC-011, CDC-021, RDC-008) silently see one clock and not the
       other.

    2. **Unresolved master clock** — a ``create_generated_clock`` whose
       ``-master_clock`` names a clock not present in ``spec.clocks``.
       :meth:`ClockSpec.resolve` returns the unknown name unchanged,
       so :meth:`are_async` falls back to "different roots, not
       declared async" and the generated clock effectively floats.

    3. **Generated-clock master cycle** — a chain of
       ``create_generated_clock``s whose master chain never terminates
       at a primary clock (A → B → A is the smallest cycle).
       :meth:`ClockSpec.resolve` has a cycle guard that prevents
       infinite-loop, but the SDC is methodology-broken; surface it.

    Duplicate clock names (two ``create_clock -name X``, or
    ``create_clock -name X`` followed by ``create_generated_clock -name
    X``) are caught inline by the per-command handlers — there the
    "this is overriding the previous" detection has both sides
    visible, which is cheaper than re-deriving from the final spec.
    """
    out: list[str] = []

    # Check 1: same port in multiple clocks.
    port_owners: dict[str, list[str]] = {}
    for clk in spec.clocks.values():
        for port in clk.ports:
            port_owners.setdefault(port, []).append(clk.name)
    for port, owners in sorted(port_owners.items()):
        if len(owners) < 2:
            continue
        out.append(
            f"port '{port}' is claimed by multiple clocks: "
            f"{', '.join(sorted(owners))} (clock_for_port returns "
            f"'{owners[0]}')"
        )

    # Check 2: unresolved master.
    declared = set(spec.clocks.keys())
    for clk in spec.clocks.values():
        if clk.master is None:
            continue
        if clk.master not in declared:
            out.append(
                f"create_generated_clock '{clk.name}': master "
                f"'{clk.master}' is not declared elsewhere — async "
                f"classification will misbehave"
            )

    # Check 3: master cycle. Walk every generated clock's master chain
    # with a per-walk visited set; if we revisit a node we hit a cycle.
    seen_cycle: set[frozenset[str]] = set()
    for start_name, start_clk in spec.clocks.items():
        if not start_clk.is_generated or start_clk.master is None:
            continue
        visited: list[str] = [start_name]
        cur: str | None = start_clk.master
        cycle_found: list[str] | None = None
        while cur is not None:
            if cur in visited:
                # Found a cycle. Slice from the first occurrence to the
                # end to get the cycle members.
                cycle_found = visited[visited.index(cur) :]
                break
            visited.append(cur)
            nxt_clk = spec.clocks.get(cur)
            if nxt_clk is None or not nxt_clk.is_generated:
                break
            cur = nxt_clk.master
        if cycle_found:
            cycle_key = frozenset(cycle_found)
            if cycle_key in seen_cycle:
                continue
            seen_cycle.add(cycle_key)
            out.append(
                f"create_generated_clock cycle detected involving "
                f"{', '.join(sorted(cycle_found))} (master chain "
                f"doesn't terminate at a primary clock)"
            )

    return out


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


# ---- Layer 1: readers (Tcl safe interp → tokenizer fallback) ----------------
#
# Two readers produce the same shape — a list of :class:`_Command`
# records, one per SDC command, each holding a word list. Everything
# downstream (:func:`_slice`, :data:`ARG_SPECS`, the handlers,
# :class:`ClockSpec`) is reader-agnostic.


#: Reader backends :func:`parse` understands.
BACKENDS = ("tcl", "tokenizer")

#: Environment override for the reader backend, e.g.
#: ``RB_CDC_SDC_BACKEND=tokenizer``. An explicit ``backend=`` argument
#: to :func:`parse` / :func:`parse_file` wins over the variable.
BACKEND_ENV_VAR = "RB_CDC_SDC_BACKEND"

# ``tkinter`` itself is imported lazily (it is slow to import and pulls
# in a Tcl runtime); ``_tkinter`` is the C extension that decides
# whether ``tkinter`` can work at all, and probing it is cheap. Every
# uv-managed / python-build-standalone interpreter bundles it; a
# Homebrew python without ``python-tk``, or an EL8 system python
# without ``python3-tkinter``, does not.
try:  # pragma: no cover - availability is environment-dependent
    import _tkinter as _tkinter_probe
except ImportError:  # pragma: no cover - availability is environment-dependent
    _tkinter_probe = None  # type: ignore[assignment]

#: True when ``tkinter.Tcl()`` can be constructed in this interpreter.
TKINTER_AVAILABLE = _tkinter_probe is not None

_TKINTER_FIX_HINT = (
    "_tkinter is not importable, so the Tcl safe-interp SDC reader is "
    "unavailable and the word tokenizer is used instead ($var, expr and "
    "command substitution are not evaluated). Fix by running under a "
    "uv-managed Python (`uv python install 3.12`), or install the "
    "distro package `python3-tkinter` / `python3-tk`, or Homebrew's "
    "`python-tk@<X.Y>` matching your interpreter."
)

# Once-per-run latch for the ``sdc.tcl_unavailable`` warning.
_warned_tcl_unavailable = False


def _reset_backend_warnings() -> None:
    """Re-arm the once-per-run backend warnings (test hook)."""
    global _warned_tcl_unavailable
    _warned_tcl_unavailable = False


def _warn_tcl_unavailable() -> None:
    global _warned_tcl_unavailable
    if _warned_tcl_unavailable:
        return
    _warned_tcl_unavailable = True
    logger.warning("sdc.tcl_unavailable: %s", _TKINTER_FIX_HINT)


def _resolve_backend(explicit: str | None = None) -> str:
    """Pick the reader backend for one parse.

    Precedence: explicit argument → :data:`BACKEND_ENV_VAR` → auto
    (``"tcl"`` when ``_tkinter`` imports, else ``"tokenizer"``). A
    request for ``"tcl"`` on an interpreter without ``_tkinter``
    degrades to the tokenizer with the once-per-run
    ``sdc.tcl_unavailable`` warning rather than raising — the analysis
    should still run, just with the documented subset.
    """
    requested = explicit if explicit is not None else os.environ.get(BACKEND_ENV_VAR)
    if requested is not None and requested.strip():
        name = requested.strip().lower()
        if name not in BACKENDS:
            raise ValueError(
                f"unknown SDC backend {requested!r}; expected one of "
                f"{', '.join(BACKENDS)}"
            )
    elif TKINTER_AVAILABLE:
        return "tcl"
    else:
        name = "tokenizer"
    if name == "tcl" and not TKINTER_AVAILABLE:
        _warn_tcl_unavailable()
        return "tokenizer"
    if name == "tokenizer" and not TKINTER_AVAILABLE:
        _warn_tcl_unavailable()
    return name


def backend() -> str:
    """Return the reader backend this process will use: ``"tcl"`` or
    ``"tokenizer"``. Surfaced by ``rtl-buddy-cdc version``."""
    return _resolve_backend(None)


def backend_description() -> str:
    """One-line human-readable backend summary for ``version`` output."""
    if backend() == "tcl":
        return "tcl (tkinter.Tcl() safe interp; $var / expr / [cmd] evaluated)"
    if TKINTER_AVAILABLE:
        return f"tokenizer (forced via {BACKEND_ENV_VAR}; $var / expr not evaluated)"
    return "tokenizer (_tkinter not importable; $var / expr not evaluated)"


@dataclass(frozen=True)
class _Command:
    """One SDC command: its words, plus the source line when known.

    ``line`` is ``None`` for the tokenizer reader (it tracks no line
    numbers) and for any Tcl frame that did not report one; every
    diagnostic that consumes it goes through :func:`_at_line`, which
    renders ``None`` as an empty suffix.
    """

    words: list[str]
    line: int | None = None


def _at_line(line: int | None) -> str:
    """Render a ``(line N)`` diagnostic suffix, or nothing for ``None``."""
    return f" (line {line})" if line is not None else ""


def _read_tokenizer(text: str) -> list[_Command]:
    """Fallback reader: the hand-written Tcl word tokenizer."""
    return [_Command(words=words) for words in _tokenize(text)]


class TclReadError(RuntimeError):
    """The safe Tcl interpreter refused to evaluate the SDC source."""


class TclResourceLimitError(TclReadError):
    """The safe Tcl interpreter was cut off by a resource limit.

    ``interp create -safe`` sandboxes *capability* (no ``exec``, no
    filesystem) but not *cost*: a constraints file containing
    ``while 1 {}`` would otherwise spin forever inside
    ``Tcl_EvalEx`` with no way for Python to interrupt it. The limits
    set in :func:`_read_tcl` turn that into an ordinary read error,
    which :func:`parse` degrades to a tokenizer re-read.
    """


#: Maximum commands the safe child may execute for one SDC file.
#: Generous — the largest real constraints files are a few thousand
#: commands, and a ``-granularity 1`` counter check is cheap. Note
#: this does *not* catch ``while 1 {}``: an empty loop body dispatches
#: no commands at all, which is what the wall-clock limit is for.
TCL_COMMAND_LIMIT = 1_000_000

#: Wall-clock budget, in seconds, for evaluating one SDC file. Covers
#: the bootstrap script too (both limits are armed before either runs).
TCL_TIME_LIMIT_SECONDS = 30


# Commands whose *names* are collections in SDC/Tcl ("get_ports",
# "all_inputs", …). Under the interp reader they are aliased through
# ``unknown`` and must hand back something ``_extract_names`` peels the
# same way it peels the tokenizer's opaque ``[get_ports clk]`` word —
# so we re-wrap the evaluated operands in the canonical bracket form.
def _is_collection_command(cmd: str) -> bool:
    return cmd.startswith("get_") or cmd.startswith("all_")


_NESTED_COLLECTION_RE = re.compile(r"\[(?:get|all)_[A-Za-z0-9_]*\s*([^][]*)\]")


def _strip_collection_wrappers(word: str) -> str:
    """Collapse nested ``[get_cells u/*]`` wrappers back to bare names.

    ``[get_pins [get_cells u/*]/C]`` evaluates inside-out: ``get_cells``
    returns ``[get_cells u/*]``, Tcl concatenates ``/C`` onto it, and
    ``get_pins`` receives ``[get_cells u/*]/C``. Peeling the inner
    wrapper yields ``u/*/C``, which is what a timer would resolve the
    nested collection to.
    """
    prev = None
    out = word
    while prev != out:
        prev = out
        out = _NESTED_COLLECTION_RE.sub(r"\1", out)
    return out


# Evaluated inside the safe child before the SDC text. ``puts`` is
# stubbed out because a safe interp has no stdout channel and vendor
# SDC files print progress; ``unknown`` catches every command the safe
# interp does not define — every SDC command, every ``get_*``
# collection, and the absent-by-construction ``exec`` / ``open`` /
# ``file`` / ``socket`` / ``source``.
_TCL_BOOTSTRAP = r"""
proc puts args {}
proc unknown args {
    set rb_line {}
    catch {
        set rb_frame [info frame [expr {[info frame] - 1}]]
        if {[dict exists $rb_frame line]} {
            set rb_line [dict get $rb_frame line]
        }
    }
    return [rb_cdc_record $rb_line {*}$args]
}
"""


def _read_tcl(text: str) -> list[_Command]:
    """Primary reader: evaluate ``text`` in a ``tkinter.Tcl()`` safe interp.

    ``tkinter.Tcl()`` needs no display (never call ``Tk()`` here). The
    child is created with ``interp create -safe``, so it has no
    ``exec`` / ``open`` / ``file`` / ``socket`` / ``load`` / ``source``
    and cannot touch the filesystem or spawn a process; those names
    fall through to ``unknown`` like any other undefined command and
    are recorded, never run.

    Raises :class:`TclReadError` when the script does not evaluate
    (genuine Tcl syntax errors, undefined variables, …); :func:`parse`
    catches it and re-reads with the tokenizer.
    """
    import tkinter

    records: list[_Command] = []

    def record(line: str, *words: str) -> str:
        if not words:  # pragma: no cover - Tcl never dispatches an empty command
            return ""
        cmd, args = words[0], list(words[1:])
        if _is_collection_command(cmd):
            inner = " ".join(
                s for s in (_strip_collection_wrappers(a) for a in args) if s
            )
            return f"[{cmd} {inner}]" if inner else f"[{cmd}]"
        try:
            lineno: int | None = int(line)
        except (TypeError, ValueError):
            lineno = None
        records.append(_Command(words=[cmd, *args], line=lineno))
        # Mirror the reference ``unknown`` from issue #298: hand back
        # the last word so a command used as a nested substitution
        # still produces something printable.
        return args[-1] if args else ""

    try:
        interp = tkinter.Tcl()
    except Exception as exc:  # pragma: no cover - broken Tcl install
        raise TclReadError(f"tkinter.Tcl() failed: {exc}") from exc
    try:
        interp.createcommand("rb_cdc_record", record)
        interp.eval("interp create -safe rb_cdc_sdc")
        # Bound the *cost* of the child, not just its capabilities. The
        # command counter stops runaway recursion and million-iteration
        # loops; the wall-clock deadline (an absolute epoch second, per
        # ``interp limit``'s contract) stops a bare ``while 1 {}``,
        # whose empty body never ticks the command counter. Exceeding
        # either raises an ordinary TclError out of the eval below.
        interp.eval(
            f"interp limit rb_cdc_sdc command "
            f"-value {int(TCL_COMMAND_LIMIT)} -granularity 1"
        )
        interp.eval(
            f"interp limit rb_cdc_sdc time -granularity 10 "
            f"-seconds [expr {{[clock seconds] + {int(TCL_TIME_LIMIT_SECONDS)}}}]"
        )
        interp.eval("interp alias rb_cdc_sdc rb_cdc_record {} rb_cdc_record")
        interp.call("rb_cdc_sdc", "eval", _TCL_BOOTSTRAP)
        interp.call("rb_cdc_sdc", "eval", text)
    except tkinter.TclError as exc:
        if "limit exceeded" in str(exc):
            raise TclResourceLimitError(str(exc)) from exc
        raise TclReadError(str(exc)) from exc
    finally:
        try:
            interp.eval("interp delete rb_cdc_sdc")
            interp.deletecommand("rb_cdc_record")
        except Exception:  # pragma: no cover - teardown is best-effort
            pass
    return records


# Commands that can never do what an SDC author expects here: the safe
# interp does not define them, so they land in ``unknown`` and are
# recorded rather than executed. The tokenizer drops them just as
# silently. Either way we say so once, with the line when we have it.
_UNSUPPORTED_COMMANDS: dict[str, str] = {
    "source": (
        "include files are not supported — the reader evaluates a single "
        "file in a safe Tcl interpreter with no filesystem access; inline "
        "the included constraints instead"
    ),
    "exec": "process execution is not supported and was not executed",
    "open": "filesystem access is not supported and was not executed",
    "socket": "network access is not supported and was not executed",
    "file": "filesystem access is not supported and was not executed",
    "load": "loading Tcl extensions is not supported and was not executed",
}

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
    if name in spec.clocks:
        # G-11 (rtl-buddy-cdc#218): duplicate clock name silently
        # overrides; surface as a partial warning so the user sees the
        # second declaration won.
        spec.partial_warnings.append(
            f"duplicate clock name '{name}': second create_clock silently "
            f"overrides the first"
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

    if name in spec.clocks:
        # G-11 duplicate-name check, mirroring create_clock's surface.
        spec.partial_warnings.append(
            f"duplicate clock name '{name}': second create_generated_clock "
            f"silently overrides the first"
        )
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
        # The tail mixes the numeric delay value with the port
        # collection. Collection *shapes* (``[get_ports …]``, ``{a b}``)
        # only survive the tokenizer reader — the Tcl reader evaluates
        # ``{d_in}`` down to a bare ``d_in`` — so the discriminator that
        # works on both is "anything that isn't a number is a target".
        if (
            word.startswith("[")
            or word.startswith("{")
            or "get_ports" in word
            or not _looks_numeric(word)
        ):
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


def _strip_get_clocks(token: str) -> str:
    """Reduce ``"[get_clocks foo]"`` / ``"{foo}"`` / ``"foo"`` to ``"foo"``."""
    names, _ = _extract_names(token)
    return names[0] if names else token


def _extract_clock_list(token: str) -> list[str]:
    """Turn ``"{src_clk dst_clk}"`` / ``"src_clk"`` into a name list."""
    names, _ = _extract_names(token)
    return names


def _looks_numeric(word: str) -> bool:
    """True when ``word`` is a plain number (an SDC delay value)."""
    try:
        float(word)
    except ValueError:
        return False
    return True


def _safe_int(word: Any, *, default: int) -> int:
    """``int(word)`` with a default fallback (mirrors the old try/except)."""
    if word is None:
        return default
    try:
        return int(word)
    except (TypeError, ValueError):
        return default
