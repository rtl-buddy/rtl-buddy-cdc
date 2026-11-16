"""SDC parser — the CDC-relevant subset.

Supported commands:

    create_clock -name <name> -period <p> [-add] [get_ports <port>]
    create_generated_clock -name <name> -source <port-or-pin> \\
                           [-master_clock <name>] [-divide_by N] \\
                           [-multiply_by N] [-edges …] [-add] [get_pins <pin>]
    set_clock_groups -asynchronous          -group {…} -group {…} …
    set_clock_groups -logically_exclusive   -group {…} -group {…} …
    set_clock_groups -physically_exclusive  -group {…} -group {…} …
    set_false_path  -from [get_clocks A] -to [get_clocks B]
    set_false_path  -from <clocks|objects> -through <objects> -to <…>
    set_input_delay  -clock <name> … [get_ports <port>]
    set_output_delay -clock <name> … [get_ports <port>]
    set_max_delay <v> -datapath_only -from <clocks|objects> -to <…>
    set_case_analysis <0|1|zero|one|rising|falling> [get_ports <port>]

A ``set_*_delay`` target may also be ``remove_from_collection`` /
``add_to_collection`` over port collections (rtl-buddy-cdc#382). Any
other command used as a collection is not evaluated: the operand naming
it is dropped with a warning (:func:`_drop_unrecognised_operands`).

A clock defined on a port that already carries one replaces it unless
``-add`` is given; with ``-add`` the port carries both
(:attr:`ClockSpec.multi_clock_ports`, rtl-buddy-cdc#330).

Numeric delays, slack, drive, load — all silently dropped. The parser
only cares about clock topology and async-partitioning, not timing.
``set_max_delay`` *without* ``-datapath_only`` is still dropped as STA
noise; with it, the constraint is CDC intent (rtl-buddy-cdc#325).

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

``tcl`` (preferred, used whenever the worker probe succeeds)
    ``tkinter.Tcl()`` → ``interp create -safe`` → an ``unknown``
    handler aliased back into Python. Real Tcl evaluation, so ``set``
    variables, ``expr``, ``\\`` continuation and nested command
    substitution all work; ``get_*`` / ``all_*`` collections are
    recorded, never resolved against the design. The safe interp has
    no ``exec`` / ``open`` / ``file`` / ``socket`` / ``load`` /
    ``source``, so a constraints file cannot read, write or run
    anything — those names land in ``unknown`` with everything else
    and are reported, not executed.

    **The interp runs in a subprocess**
    (:mod:`rtl_buddy_cdc.tcl_worker`, one per SDC file), and this
    module never imports ``_tkinter``. That is a correctness
    requirement, not a preference: loading ``_tkinter`` starts Tcl's
    ``NotifierThreadProc``, a native thread that never retires, and on
    macOS a later ``subprocess`` fork+exec from a process carrying it
    can wedge the forked child inside ``close()`` in uninterruptible
    kernel state — forever — with the parent blocked reading the exec
    errpipe. rb-cdc spawns yosys and the slang frontend right after
    parsing the SDC, so an in-process interp is a live hang hazard on
    the main analysis path. The worker's docstring holds the sampled
    stack and the reproduction (rtl-buddy-cdc#298). **Do not move the
    interp back in-process.**

``tokenizer`` (fallback, when the worker has no ``_tkinter``)
    :func:`rtl_buddy_cdc.tcl_tokenizer._tokenize`, the hand-written
    Tcl-aware word splitter: ``{...}`` braces and ``[...]`` brackets
    are single opaque tokens with nesting respected, ``\\`` collapses
    line continuation to a space, ``"..."`` strips quoting, ``#`` at a
    word boundary comments to end-of-line. It does **not** evaluate
    ``$var``, ``expr`` or command substitution, so a clock declared
    through a variable is invisible to it; the first such drop per
    file raises a ``sdc.tokenizer_skipped`` warning, and an
    unavailable worker raises one ``sdc.tcl_unavailable`` warning per
    run naming the fix.

Both backends feed the same slicer, handlers and :class:`ClockSpec`,
so every downstream consumer is backend-agnostic. When the parser sees
a CDC-relevant command it can't fully understand it appends to
:attr:`ClockSpec.partial_warnings` so the caller can surface a single
end-of-parse warning rather than spamming line-by-line.
"""

from __future__ import annotations

import dataclasses
import enum
import functools
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING, TypedDict

# The tokenizer reader lives in its own stdlib-only module so rtl-buddy
# can vendor it verbatim (rtl-buddy-cdc#298). Re-exported here because
# every existing caller and test imports ``sdc._tokenize`` /
# ``sdc._extract_names``.
from rtl_buddy_cdc.tcl_tokenizer import _extract_names, _tokenize

# The Tcl safe interp — the recorder, the bootstrap and the collection
# re-wrapping — runs OUT OF PROCESS, in :mod:`rtl_buddy_cdc.tcl_worker`
# (see this module's docstring for why). That module imports
# ``tkinter`` lazily inside its own ``main()``, so naming it here costs
# nothing and, critically, does not pull ``_tkinter`` into this process.
from rtl_buddy_cdc.tcl_worker import (
    COLLECTION_OPERATORS,
    WORKER_MODULE,
    _strip_collection_wrappers,
)

if TYPE_CHECKING:
    from rtl_buddy_cdc.netlist import Module

__all__ = [
    "Arity",
    "ARG_SPECS",
    "BACKENDS",
    "BACKEND_ENV_VAR",
    "CASE_ANALYSIS_VALUES",
    "Clock",
    "ClockSpec",
    "DatapathOnlyDelay",
    "FalsePathException",
    "PortDelay",
    "SdcObject",
    "TclReadError",
    "TclResourceLimitError",
    "UNCONSTRAINED_SENTINEL",
    "backend",
    "backend_description",
    "parse",
    "parse_file",
    "synthesize_unconstrained_inputs",
    "tcl_available",
    "tcl_patchlevel",
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
    # ``create_generated_clock -source`` objects, as written (rtl-buddy-cdc#330).
    # Recorded for diagnostics only: the master comes from ``-master_clock``.
    source: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortDelay:
    """One ``set_input_delay`` / ``set_output_delay -clock`` statement
    (rtl-buddy-cdc#357).

    ``targets`` are the port operands as written, a bus index kept on its
    name (``d[3]``) and globs (``d*``, ``a?``) unexpanded: the parser has
    no netlist, so :func:`rtl_buddy_cdc.sdc_objects.resolve_port_delays`
    resolves them against the top-level ports and rewrites
    :attr:`ClockSpec.port_clock`. File order is application order.

    ``excludes`` (rtl-buddy-cdc#382) holds, per target, the patterns a
    ``remove_from_collection`` took out of it, as written; empty when the
    statement removed nothing. ``[remove_from_collection [all_inputs]
    [get_ports clk]]`` is the target ``*`` with the exclude ``clk``,
    and ``[add_to_collection [remove_from_collection A B] C]`` the
    targets ``A`` (excluding ``B``) and ``C``. :meth:`terms` pairs them.

    ``line`` is diagnostic (Tcl backend only), so it takes no part in
    equality: the two backends produce equal records.
    """

    command: str  # "set_input_delay" | "set_output_delay"
    clock: str
    targets: tuple[str, ...]
    line: int | None = field(default=None, compare=False)
    excludes: tuple[tuple[str, ...], ...] = ()

    def terms(self) -> Iterator[tuple[str, tuple[str, ...]]]:
        """Each target with the patterns removed from it."""
        for i, target in enumerate(self.targets):
            yield target, self.excludes[i] if i < len(self.excludes) else ()


@dataclass(frozen=True)
class DatapathOnlyDelay:
    """One ``set_max_delay -datapath_only`` constraint (rtl-buddy-cdc#325).

    ``from_clocks`` / ``to_clocks`` hold the endpoints given as clocks
    (``[get_clocks A]``, or a bare / braced name — the same reading
    ``set_false_path`` gives a bare endpoint). Every other collection
    (``[get_cells …]``, ``[get_pins …]``, ``[get_ports …]``,
    ``[get_nets …]``) lands in ``from_objects`` / ``to_objects`` as a
    ``(kind, name)`` pair — ``kind`` is the collection noun (``"cell"``,
    ``"pin"``, ``"port"``, ``"net"``) and ``name`` keeps the SDC ``/``
    hierarchy separator. Objects are **recorded, not resolved** against
    the netlist: object-scoped endpoints are rtl-buddy-cdc#328's seam.

    ``value`` is the max-delay bound in SDC time units, or ``None`` when
    the command carried no numeric operand.
    """

    value: float | None
    from_clocks: tuple[str, ...] = ()
    to_clocks: tuple[str, ...] = ()
    from_objects: tuple[tuple[str, str], ...] = ()
    to_objects: tuple[tuple[str, str], ...] = ()
    # Source line of the statement (Tcl backend only; the tokenizer
    # tracks none). Diagnostic, so it takes no part in equality: the two
    # backends must produce equal records (rtl-buddy-cdc#328).
    line: int | None = field(default=None, compare=False)

    @property
    def is_clock_pair(self) -> bool:
        """True when both sides are clocks only — a clock-pair constraint."""
        return bool(
            self.from_clocks
            and self.to_clocks
            and not self.from_objects
            and not self.to_objects
        )

    @property
    def statement(self) -> str:
        """The constraint re-rendered canonically, for reports.

        Built from the parsed record rather than the source words, so it
        is the same text on both SDC backends.
        """
        value = "" if self.value is None else f" {self.value:g}"
        return (
            f"set_max_delay{value} -datapath_only"
            f"{_render_side('-from', self.from_clocks, _as_objects(self.from_objects))}"
            f"{_render_side('-to', self.to_clocks, _as_objects(self.to_objects))}"
        )


@dataclass(frozen=True)
class SdcObject:
    """One design-object pattern from a ``get_*`` collection (#328).

    ``kind`` is the collection noun — ``"cell"``, ``"pin"``, ``"port"``,
    ``"net"`` — or ``"any"`` for a bare name in a ``-through`` list,
    which a timer would look up as a pin, net or port. ``name`` keeps
    the SDC spelling (``/`` hierarchy separator, ``*`` / ``?`` globs,
    ``[3]`` bus index). ``hierarchical`` records ``get_* -hierarchical``:
    the pattern then matches at any depth of the hierarchy.
    """

    kind: str
    name: str
    hierarchical: bool = False

    def render(self) -> str:
        """``[get_cells {u/q_reg[3]}]`` — the pattern as SDC text."""
        return _render_collection(self.kind, (self.name,), self.hierarchical)


def _render_collection(kind: str, names: tuple[str, ...], hierarchical: bool) -> str:
    # Brace a name carrying a bus index: unbraced, Tcl would read ``[3]``
    # as command substitution.
    body = " ".join(f"{{{n}}}" if "[" in n else n for n in names)
    if kind == "any":
        return f"{{{body}}}"
    flag = " -hierarchical" if hierarchical else ""
    return f"[get_{kind}s{flag} {body}]"


@dataclass(frozen=True)
class FalsePathException:
    """One ``set_false_path`` that is not a pure clock pair (#328).

    Recorded when any endpoint is a design object (``[get_cells …]``,
    ``[get_pins …]``, ``[get_ports …]``, ``[get_nets …]``) or the
    statement carries ``-through``. Clock endpoints given alongside —
    ``-from [get_cells u/q_reg] -to [get_clocks B]`` — stay in
    ``from_clocks`` / ``to_clocks`` and constrain that side's domain.

    Unlike a clock-pair false path (:attr:`ClockSpec.false_path_pairs`,
    which makes the pair *asynchronous* — more checking), this record
    never changes :meth:`ClockSpec.are_async`. It names specific paths
    whose timing check the constraints author removed; the netlist
    resolution and its effect on findings live outside the parser, in
    :mod:`rtl_buddy_cdc.sdc_objects`.

    ``through`` holds one tuple per ``-through`` flag (a path must pass
    every group, and any one object within a group). ``qualifiers`` are
    the flags that narrow the exception to part of the timing check —
    ``-rise_from`` / ``-fall_to`` / … (one edge) and ``-setup`` /
    ``-hold`` (one check) — edge flags in ``-from`` / ``-to`` /
    ``-through`` order, then the check flags. A statement with any
    qualifier never acknowledges findings (see ``sdc_objects``).
    """

    from_clocks: tuple[str, ...] = ()
    to_clocks: tuple[str, ...] = ()
    from_objects: tuple[SdcObject, ...] = ()
    to_objects: tuple[SdcObject, ...] = ()
    through: tuple[tuple[SdcObject, ...], ...] = ()
    qualifiers: tuple[str, ...] = ()
    line: int | None = field(default=None, compare=False)

    @property
    def statement(self) -> str:
        """The constraint re-rendered canonically (same on both backends).

        Each side is rendered under the first flag spelling it was given
        with (``-from`` unless only ``-rise_from`` / ``-fall_from`` was
        used, and so on); ``-setup`` / ``-hold`` lead.
        """

        def flag(side: str) -> str:
            return next(
                (q for q in self.qualifiers if q.endswith(f"_{side}")), f"-{side}"
            )

        checks = "".join(f" {q}" for q in self.qualifiers if "_" not in q)
        through = "".join(
            _render_side(flag("through"), (), group) for group in self.through
        )
        return (
            f"set_false_path{checks}"
            f"{_render_side(flag('from'), self.from_clocks, self.from_objects)}"
            f"{through}"
            f"{_render_side(flag('to'), self.to_clocks, self.to_objects)}"
        )


def _as_objects(pairs: tuple[tuple[str, str], ...]) -> tuple[SdcObject, ...]:
    return tuple(SdcObject(kind, name) for kind, name in pairs)


def _render_side(
    flag: str, clocks: tuple[str, ...], objects: tuple[SdcObject, ...]
) -> str:
    """`` -from [get_clocks a] [get_cells u/q*]`` — empty when nothing given."""
    words: list[str] = []
    if clocks:
        words.append(f"[get_clocks {' '.join(clocks)}]")
    by_kind: dict[tuple[str, bool], list[str]] = {}
    for obj in objects:
        by_kind.setdefault((obj.kind, obj.hierarchical), []).append(obj.name)
    for (kind, hier), names in by_kind.items():
        words.append(_render_collection(kind, tuple(names), hier))
    return f" {flag} {' '.join(words)}" if words else ""


#: ``set_case_analysis`` value spellings → the normalised value stored on
#: :attr:`ClockSpec.case_analysis`. ``"0"`` / ``"1"`` are constants the
#: clock tracer resolves a mux select with; ``"rising"`` / ``"falling"``
#: are recorded but are not constants (the pin still toggles).
CASE_ANALYSIS_VALUES: dict[str, str] = {
    "0": "0",
    "1": "1",
    "zero": "0",
    "one": "1",
    "logic_zero": "0",
    "logic_one": "1",
    "rise": "rising",
    "rising": "rising",
    "fall": "falling",
    "falling": "falling",
}


class ClockRootInputs(TypedDict):
    """Keyword arguments of every clock-root resolver in
    :mod:`rtl_buddy_cdc.domain` — see :meth:`ClockSpec.clock_root_inputs`."""

    pin_clocks: dict[str, str]
    clock_for_port: Callable[[str], str | None]
    declared_clock_for_port: Callable[[str], str | None]
    case_analysis: dict[str, str]


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
    # Every ``set_*_delay -clock`` statement with a port target, in file
    # order (rtl-buddy-cdc#357). ``port_clock`` holds their targets as
    # written until ``sdc_objects.resolve_port_delays`` expands bus
    # indices and globs against the netlist's ports.
    port_delays: list[PortDelay] = field(default_factory=list)
    # Internal pin path (e.g. ``"u_a/clk_out"``) → generated clock name,
    # populated when a ``create_generated_clock`` target is a
    # ``[get_pins …]`` expression rather than a top-level port. The
    # clock-trace pass consults this to stop walking back through the
    # netlist at the point where a generated clock takes over, instead
    # of collapsing every flop to whichever top input port feeds the
    # chain. The pin path uses SDC convention (``/`` separator); the
    # consumer normalises to Yosys' flattened netname (``.`` separator).
    pin_clocks: dict[str, str] = field(default_factory=dict)
    # Every ``set_max_delay -datapath_only`` constraint, in file order
    # (rtl-buddy-cdc#325). Recorded whatever its endpoints are; only
    # the clock-pair ones feed ``datapath_only_pairs``.
    datapath_only_delays: list[DatapathOnlyDelay] = field(default_factory=list)
    # Unordered ``{from, to}`` clock pairs carrying a clock-to-clock
    # ``set_max_delay -datapath_only``. A datapath-only bound exists
    # only where the launch and capture clocks are not timed against
    # each other, so the pair is an intended asynchronous crossing:
    # ``are_async`` treats it as async and, unlike ``false_path_pairs``,
    # lets it override a generated→master collapse and an exclusive
    # group — the more-reporting reading (see ``are_async``).
    datapath_only_pairs: set[frozenset[str]] = field(default_factory=set)
    # Every ``set_false_path`` that is not a pure clock pair — an object
    # endpoint or a ``-through`` — in file order (rtl-buddy-cdc#328).
    # Never feeds ``are_async``: such a statement acknowledges specific
    # paths, it does not declare a clock pair asynchronous. Resolved
    # against the netlist by :mod:`rtl_buddy_cdc.sdc_objects`.
    false_path_exceptions: list[FalsePathException] = field(default_factory=list)
    # Top-level port → normalised ``set_case_analysis`` value ("0", "1",
    # "rising", "falling"; see ``CASE_ANALYSIS_VALUES``). A bus bit is
    # keyed in its SDC spelling, ``"sel[3]"``; a whole-bus key applies
    # to every bit. Insertion order is application order (last write
    # wins). Consumed by the clock tracer, which resolves a clock mux
    # whose select is a case-analysed constant to the selected leg.
    case_analysis: dict[str, str] = field(default_factory=dict)
    # Top-level port → every clock declared on it, in declaration order,
    # for ports that carry more than one clock (``create_clock -add``,
    # rtl-buddy-cdc#330). The first entry is the port's *primary* clock —
    # the one ``clock_for_port`` returns, so the one the flops behind the
    # port are named after; the rest are co-located with it, and
    # ``are_async`` checks a crossing against each of them. Filled at the
    # end of :func:`parse`; empty for every SDC without such a port.
    multi_clock_ports: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Diagnostics accumulated during parse. Each entry is a single
    # human-readable sentence describing a CDC-relevant command the
    # parser couldn't fully understand. Surfaced once at the end of
    # parsing rather than logged per-line.
    partial_warnings: list[str] = field(default_factory=list)
    # Which netlist :attr:`port_delays` were last resolved against, so the
    # resolution runs once per spec and netlist however many entry points
    # ask for it (``sdc_objects.resolve_port_delays``, rtl-buddy-cdc#386).
    # Bookkeeping only: not an argument, not compared, not printed.
    _port_delays_resolved: object = field(
        default=None, init=False, repr=False, compare=False
    )
    # Every clock name the parsed file declares, in declaration order —
    # what ``[all_clocks]`` and a ``[get_clocks <glob>]`` resolve against
    # (rtl-buddy-cdc#390). Set by :func:`parse` before the handlers run,
    # so a clock declared below the statement that names it counts, as it
    # does for every other clock reference. ``None`` on a spec built
    # without :func:`parse`: the clocks declared on it so far are used.
    _clock_universe: tuple[str, ...] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    # ---- consumer-facing helpers ------------------------------------------

    def clock_for_port(self, port: str) -> str | None:
        """Return the clock name associated with ``port``.

        Checks ``set_*_delay``-derived ``port_clock`` first (a port
        explicitly typed by the user), then falls back to scanning
        ``create_clock`` port lists. Returns ``None`` if the port is
        not associated with any clock.

        On a port carrying several clocks (``create_clock -add``) this is
        the first-declared one — the port's primary clock, see
        :attr:`multi_clock_ports` (rtl-buddy-cdc#330).
        """
        if port in self.port_clock:
            return self.port_clock[port]
        return self.declared_clock_for_port(port)

    def declared_clock_for_port(self, port: str) -> str | None:
        """The ``create_clock`` declared on ``port`` (or on the internal
        pin of that name), or ``None``.

        Unlike :meth:`clock_for_port` this ignores ``set_input_delay``
        typing: a data port launched by a clock is not itself a clock.
        It is the clock-*identity* question the clock tracer asks when a
        gate or latch combines several legs (rtl-buddy-cdc#358): only a
        leg that carries a declared clock competes, so ``en & clk``
        resolves to ``clk`` even when ``en`` is typed to some clock. On
        a ``create_clock -add`` port it is the primary clock, as
        :meth:`clock_for_port` is.
        """
        for clk in self.clocks.values():
            if port in clk.ports:
                return clk.name
        return None

    def clock_root_inputs(self) -> ClockRootInputs:
        """The SDC inputs every clock-root walk on a run takes.

        Keyword arguments for :func:`~rtl_buddy_cdc.domain.assign_domains`,
        :func:`~rtl_buddy_cdc.domain.find_crossings` and the other
        resolvers in :mod:`rtl_buddy_cdc.domain`: generated-clock pin
        targets, the canonical-name normaliser, the declared-clock
        identity predicate and the ``set_case_analysis`` constants. Every
        view of a flop's clock domain (the crossing walk, the rule
        context, the clock-network and boundary walks) is built from
        this one bundle, so two views of one flop cannot disagree because
        one of them was handed fewer inputs (rtl-buddy-cdc#358).
        """
        return {
            "pin_clocks": self.pin_clocks,
            "clock_for_port": self.clock_for_port,
            "declared_clock_for_port": self.declared_clock_for_port,
            "case_analysis": self.case_analysis,
        }

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

        A clock-pair ``set_max_delay -datapath_only``
        (``datapath_only_pairs``, rtl-buddy-cdc#325) is an async hint
        like a false path, but it is also an explicit override in step
        1: it wins over the same-root collapse of step 2. Bounding only
        the datapath is what a constraints author does when the two
        clocks are *not* timed against each other, so when that
        contradicts a generated→master relationship (or two clocks
        sharing one ``-group``, which ``are_async`` already reads as
        synchronous) the analyzer takes the reading that reports more:
        the crossing is checked for a synchroniser.

        **Multi-clock ports** (``create_clock -add``, rtl-buddy-cdc#330).
        A flop behind a port that carries several clocks is named after
        the port's primary clock (see :attr:`multi_clock_ports`), and that
        name stands for every clock on the port. When ``a`` or ``b`` is
        such a primary, the pair is async iff *some* combination of their
        co-located clocks is async and not unreachable (exclusive) — a
        crossing is synchronous only if it is synchronous to every clock
        on the port. :meth:`async_witness` names the combination. For
        clocks on single-clock ports this is exactly the per-pair answer.
        """
        if a == b:
            return False
        ca = self.colocated_clocks(a)
        cb = self.colocated_clocks(b)
        if len(ca) == 1 and len(cb) == 1:
            return self._are_async_pair(a, b)
        return self._first_async_pair(ca, cb) is not None

    def colocated_clocks(self, name: str) -> tuple[str, ...]:
        """Every clock a domain named ``name`` stands for (rtl-buddy-cdc#330).

        ``(name,)`` unless ``name`` is the primary clock of a port in
        :attr:`multi_clock_ports`; then ``name`` first, followed by the
        other clocks on that port (or ports) in declaration order. A
        non-primary clock on such a port — ``clk_a2`` named by
        ``set_input_delay -clock clk_a2`` — means exactly itself.
        """
        if not self.multi_clock_ports:
            return (name,)
        out = [name]
        for clocks in self.multi_clock_ports.values():
            if clocks[0] == name:
                out.extend(c for c in clocks[1:] if c not in out)
        return tuple(out)

    def async_witness(self, a: str, b: str) -> tuple[str, str] | None:
        """The clock pair that makes a crossing ``a`` → ``b`` checkable.

        ``None`` when the crossing is synchronous or unreachable — the
        crossings :func:`rtl_buddy_cdc.domain.filter_async` drops.
        Otherwise ``(a, b)`` itself when that pair is async and
        reachable, else the first async, reachable pair of co-located
        clocks (:meth:`colocated_clocks`), scanning ``a``'s clocks then
        ``b``'s in declaration order — so the answer is deterministic and
        the same on both SDC backends.
        """
        if a == b:
            return None
        ca = self.colocated_clocks(a)
        cb = self.colocated_clocks(b)
        if len(ca) == 1 and len(cb) == 1:
            if self._is_unreachable_pair(a, b) or not self._are_async_pair(a, b):
                return None
            return (a, b)
        return self._first_async_pair(ca, cb)

    def _first_async_pair(
        self, ca: tuple[str, ...], cb: tuple[str, ...]
    ) -> tuple[str, str] | None:
        for x in ca:
            for y in cb:
                if x == y or self._is_unreachable_pair(x, y):
                    continue
                if self._are_async_pair(x, y):
                    return (x, y)
        return None

    def _are_async_pair(self, a: str, b: str) -> bool:
        """:meth:`are_async` for two single clocks (no port expansion)."""
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
        if self._is_datapath_only_pair(a, b):
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

        A pair carrying a clock-pair ``set_max_delay -datapath_only`` is
        never unreachable (rtl-buddy-cdc#325): bounding a path's delay
        says its author believes the path is live, and dropping it would
        hide the crossing — the less-reporting reading.

        When ``a`` or ``b`` is the primary clock of a multi-clock port
        (rtl-buddy-cdc#330), the crossing is unreachable only if every
        combination of their co-located clocks is — the usual
        ``-physically_exclusive`` between two modes on one pin removes
        just the pairs it names.
        """
        if a == b:
            return False
        ca = self.colocated_clocks(a)
        cb = self.colocated_clocks(b)
        if len(ca) == 1 and len(cb) == 1:
            return self._is_unreachable_pair(a, b)
        return all(x != y and self._is_unreachable_pair(x, y) for x in ca for y in cb)

    def _is_unreachable_pair(self, a: str, b: str) -> bool:
        """:meth:`is_unreachable_crossing` for two single clocks."""
        if a == b:
            return False
        if self._is_datapath_only_pair(a, b):
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

    def _is_datapath_only_pair(self, a: str, b: str) -> bool:
        """True when a clock-pair ``-datapath_only`` bound links ``a`` and ``b``.

        Checked on the names as given and on their resolved roots, so a
        constraint written between two masters also covers crossings
        between clocks generated from them.
        """
        if not self.datapath_only_pairs:
            return False
        if frozenset({a, b}) in self.datapath_only_pairs:
            return True
        ra = self.resolve(a)
        rb = self.resolve(b)
        return ra != rb and frozenset({ra, rb}) in self.datapath_only_pairs


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
            commands = _read_tcl(text, spec.partial_warnings)
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
            "" if tcl_available() else f" {_TKINTER_FIX_HINT}",
        )

    spec._clock_universe = _declared_clock_names(commands)

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
        gate = _COMMAND_GATES.get(cmd)
        if gate is not None and not gate(args):
            # A command that is CDC-relevant only in one form (plain
            # ``set_max_delay`` is STA noise; ``-datapath_only`` is CDC
            # intent) is dropped exactly like an unrecognised one.
            spec_for_cmd = None
        if spec_for_cmd is None:
            # Drop noise (set_max_delay, set_load, set_drive, …) at
            # DEBUG level so users can see what was skipped via
            # ``--verbose`` without the report being flooded.
            logger.debug("sdc: ignoring unsupported command %r", cmd)
            note_tokenizer_skip(f"dropped unrecognised command {cmd!r}")
            if cmd.isdigit():
                # ``[get_ports sel[3]]`` with an unbraced bus index is
                # Tcl command substitution of ``[3]``: the Tcl reader
                # runs a command named ``3``. Its ``unknown`` hands the
                # call back verbatim, so the target still reads
                # ``sel[3]`` (rtl-buddy-cdc#382), but a real Tcl
                # interpreter fails on it — say so.
                spec.partial_warnings.append(
                    f"command {cmd!r}{_at_line(command.line)} looks like an "
                    f"unbraced bus index evaluated as Tcl command substitution "
                    f"— brace the indexed name, e.g. {{p[{cmd}]}}; a Tcl "
                    f"interpreter without this reader's fallback rejects it"
                )
            continue
        if any("$" in w for w in words):
            note_tokenizer_skip(f"{cmd}: kept a literal $-word unevaluated")
        parsed = dataclasses.replace(_slice(args, spec_for_cmd), line=command.line)
        parsed = _drop_unrecognised_operands(cmd, parsed, spec.partial_warnings)
        _DISPATCH[cmd](spec, parsed)
    spec.multi_clock_ports = _index_multi_clock_ports(spec.clocks)
    spec.partial_warnings.extend(_undeclared_delay_clocks(spec))
    return spec


#: Commands that declare clocks — the first pass of :func:`parse` runs
#: only these, to learn the file's clock names (rtl-buddy-cdc#390).
_CLOCK_DECLARATIONS: tuple[str, ...] = ("create_clock", "create_generated_clock")


def _declared_clock_names(commands: list[_Command]) -> tuple[str, ...]:
    """Every clock name ``commands`` leave declared, in declaration order.

    A first pass over the clock declarations alone, on a scratch spec
    whose diagnostics are discarded (the real pass reports them). A
    clock collection in a clock position — ``[all_clocks]``,
    ``[get_clocks clk_*]`` — resolves against this set rather than the
    clocks declared *above* it: every other clock reference (a ``-group``
    or ``-clock`` name) already counts a clock declared further down, and
    an ``all_clocks`` that silently left such a clock out would make the
    two spellings of one group disagree. The handler warns when the
    expansion picks up a clock declared below the statement, because a
    timer evaluates the file in order and would not
    (:func:`_resolve_clock_terms`).
    """
    scratch = ClockSpec()
    for command in commands:
        words = command.words
        if not words or words[0] not in _CLOCK_DECLARATIONS:
            continue
        cmd = words[0]
        parsed = _drop_unrecognised_operands(cmd, _slice(words[1:], ARG_SPECS[cmd]), [])
        _DISPATCH[cmd](scratch, parsed)
    return tuple(scratch.clocks)


def _undeclared_delay_clocks(spec: ClockSpec) -> list[str]:
    """One warning per ``set_*_delay -clock`` naming no declared clock.

    Decided after the whole file is read, so a clock declared below the
    delay that uses it counts; a virtual clock (``create_clock -name V
    -period P`` with no source port) is a declared clock. A clock that a
    later ``create_clock`` without ``-add`` replaced off its only port no
    longer exists and is reported too (rtl-buddy-cdc#357).
    """
    out: list[str] = []
    for d in spec.port_delays:
        if d.clock in spec.clocks:
            continue
        out.append(
            f"{d.command} -clock {d.clock}{_at_line(d.line)}: no create_clock "
            f"or create_generated_clock declares clock '{d.clock}', so "
            f"{', '.join(d.targets)} {'is' if len(d.targets) == 1 else 'are'} "
            f"typed to a clock that does not exist — a typo, or a missing "
            f"(virtual) clock declaration?"
        )
    return out


def _index_multi_clock_ports(clocks: dict[str, Clock]) -> dict[str, tuple[str, ...]]:
    """Port → its clocks in declaration order, for ports carrying ≥ 2.

    Without ``-add`` a later clock on a port replaces the earlier one
    (:func:`_claim_ports`), so after a parse a port can only carry
    several clocks through ``-add``. The order is ``spec.clocks``
    order, which is what :meth:`ClockSpec.clock_for_port` scans — so
    the first entry is always the clock the port's flops are named
    after.
    """
    owners: dict[str, list[str]] = {}
    for clk in clocks.values():
        for port in clk.ports:
            names = owners.setdefault(port, [])
            if clk.name not in names:
                names.append(clk.name)
    return {p: tuple(names) for p, names in owners.items() if len(names) > 1}


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
       other. Since rtl-buddy-cdc#330 a parsed SDC never reaches this
       shape by accident: without ``-add`` the later clock replaces the
       earlier one (warned at parse time), and a port shared through
       ``-add`` is a legitimate multi-clock port
       (:attr:`ClockSpec.multi_clock_ports`) that is not flagged. The
       check still guards specs built without :func:`parse`.

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

    4. **Generated clock on a multi-clock source** (rtl-buddy-cdc#330)
       — a ``create_generated_clock -source`` port that carries several
       clocks, with no ``-master_clock`` to pick one (ambiguous), or with
       a ``-master_clock`` that names none of them.

    Duplicate clock names (two ``create_clock -name X``, or
    ``create_clock -name X`` followed by ``create_generated_clock -name
    X``) are caught inline by the per-command handlers — there the
    "this is overriding the previous" detection has both sides
    visible, which is cheaper than re-deriving from the final spec.
    """
    out: list[str] = []

    # Check 1: same port in multiple clocks. A port the parser recorded
    # in ``multi_clock_ports`` got there through ``-add`` (a replacement
    # without it already warned at parse time and left one clock), so
    # it is a legitimate declaration and is not flagged. What remains is
    # a spec assembled without :func:`parse` — no -add / replace record.
    port_owners: dict[str, list[str]] = {}
    for clk in spec.clocks.values():
        for port in clk.ports:
            port_owners.setdefault(port, []).append(clk.name)
    for port, owners in sorted(port_owners.items()):
        if len(owners) < 2:
            continue
        if spec.multi_clock_ports.get(port) == tuple(owners):
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

    # Check 4 (rtl-buddy-cdc#330): a generated clock whose -source is a
    # port carrying several clocks. The master comes from -master_clock
    # only (never inferred from -source), so without it the generated
    # clock has no master on ANY source — but on a multi-clock source the
    # missing choice is genuinely ambiguous, so say so; and a
    # -master_clock that names none of the source's clocks contradicts it.
    for clk in spec.clocks.values():
        if not clk.is_generated:
            continue
        for src in clk.source:
            on_src = spec.multi_clock_ports.get(src)
            if on_src is None:
                continue
            listed = ", ".join(on_src)
            if clk.master is None:
                out.append(
                    f"create_generated_clock '{clk.name}': -source port "
                    f"'{src}' carries clocks {listed} and no -master_clock "
                    f"picks one — the generated clock is left without a "
                    f"master; add -master_clock"
                )
            elif clk.master not in on_src:
                out.append(
                    f"create_generated_clock '{clk.name}': -master_clock "
                    f"'{clk.master}' is not one of the clocks on -source "
                    f"port '{src}' ({listed})"
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

    The ``set_*_delay`` targets are resolved against ``module`` first
    (:func:`rtl_buddy_cdc.sdc_objects.resolve_port_delays`, once per spec
    and netlist; its warnings land on :attr:`ClockSpec.partial_warnings`),
    so a bus-index or glob target (``[get_ports {d[*]}]``) types its
    port here exactly as on the CLI path rather than leaving it to the
    sentinel (rtl-buddy-cdc#386).
    """
    # Imported here: ``sdc_objects`` imports this module.
    from rtl_buddy_cdc.sdc_objects import resolve_port_delays

    spec.partial_warnings.extend(resolve_port_delays(module, spec))
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

# Availability cannot be decided with ``import _tkinter`` here: this
# process must never import it (see the module docstring — the Tcl
# notifier thread wedges a later ``subprocess`` fork+exec on macOS).
# Instead the worker is probed once per process with an empty request.
# It answers with its Tcl ``info patchlevel`` when it can build an
# interp, and ``kind="unavailable"`` when ``_tkinter`` does not import
# over there. The probe costs one short-lived subprocess per run.


@dataclass(frozen=True)
class _TclProbe:
    """Cached answer to "can this installation run the Tcl reader?"."""

    available: bool
    patchlevel: str | None = None
    reason: str | None = None


_TCL_PROBE: _TclProbe | None = None


def _probe_tcl() -> _TclProbe:
    """Ask the worker whether it can construct a Tcl interp (cached)."""
    global _TCL_PROBE
    if _TCL_PROBE is not None:
        return _TCL_PROBE
    try:
        response = _run_worker("", TCL_COMMAND_LIMIT, TCL_TIME_LIMIT_SECONDS)
    except TclReadError as exc:
        _TCL_PROBE = _TclProbe(available=False, reason=_one_line(exc))
    else:
        if response.get("ok"):
            _TCL_PROBE = _TclProbe(
                available=True,
                patchlevel=str(response.get("patchlevel") or "") or None,
            )
        else:
            _TCL_PROBE = _TclProbe(
                available=False,
                reason=str(response.get("error") or "") or None,
            )
    return _TCL_PROBE


def tcl_available() -> bool:
    """True when the out-of-process Tcl safe-interp reader can run."""
    return _probe_tcl().available


def tcl_patchlevel() -> str | None:
    """The worker's Tcl ``info patchlevel``, or ``None`` when unavailable."""
    return _probe_tcl().patchlevel


def _reset_tcl_probe() -> None:
    """Drop the cached availability probe (test hook)."""
    global _TCL_PROBE
    _TCL_PROBE = None


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
    (``"tcl"`` when the worker probe succeeds, else ``"tokenizer"``).
    A request for ``"tcl"`` on an installation without ``_tkinter``
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
    elif tcl_available():
        return "tcl"
    else:
        name = "tokenizer"
    if name == "tcl" and not tcl_available():
        _warn_tcl_unavailable()
        return "tokenizer"
    if name == "tokenizer" and not tcl_available():
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
    if tcl_available():
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


#: Extra wall-clock slack, in seconds, on top of
#: :data:`TCL_TIME_LIMIT_SECONDS` before the parent gives up on the
#: worker and kills it. The interp's own ``time`` limit should always
#: fire first and produce a clean ``resource_limit`` response; this is
#: the outer deadline for a worker that wedged somewhere Tcl's limit
#: cannot reach. Blowing it is handled exactly like a resource limit.
TCL_WORKER_TIMEOUT_MARGIN_SECONDS = 10


def _worker_env() -> dict[str, str]:
    """Environment for the worker: this package must be importable.

    ``sys.executable`` is normally the interpreter that already
    imports ``rtl_buddy_cdc``, but a ``sys.path`` tweak in the parent
    (an editable overlay, a ``conftest`` insertion, a zipapp) does not
    carry over to a fresh ``-m`` invocation, so the directory holding
    the package is prepended to ``PYTHONPATH``.
    """
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{package_root}{os.pathsep}{existing}" if existing else package_root
    )
    return env


def _worker_detail(stderr: str | None) -> str:
    """Render the worker's stderr as a short, single-line suffix."""
    text = " ".join((stderr or "").split())
    if not text:
        return ""
    if len(text) > 200:
        text = text[:200] + "…"
    return f"; stderr: {text}"


def _run_worker(
    text: str, command_limit: int, time_limit_seconds: int
) -> dict[str, Any]:
    """Run one JSON request through ``python -m rtl_buddy_cdc.tcl_worker``.

    Returns the decoded response object (``ok`` true *or* false — an
    evaluation failure is a valid response). Raises
    :class:`TclReadError` when the worker itself misbehaved, and
    :class:`TclResourceLimitError` when it had to be killed.
    """
    request = json.dumps(
        {
            "text": text,
            "command_limit": int(command_limit),
            "time_limit_seconds": int(time_limit_seconds),
        }
    )
    timeout = int(time_limit_seconds) + TCL_WORKER_TIMEOUT_MARGIN_SECONDS
    try:
        proc = subprocess.run(
            [sys.executable, "-m", WORKER_MODULE],
            input=request,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_worker_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise TclResourceLimitError(
            f"the Tcl worker process did not answer within {timeout}s and was killed"
        ) from exc
    except OSError as exc:  # pragma: no cover - the interpreter is right there
        raise TclReadError(f"could not start the Tcl worker process: {exc}") from exc
    if proc.returncode != 0:
        raise TclReadError(
            f"the Tcl worker process exited with status {proc.returncode}"
            f"{_worker_detail(proc.stderr)}"
        )
    try:
        response = json.loads(proc.stdout)
    except ValueError as exc:
        raise TclReadError(
            f"the Tcl worker process wrote output that is not JSON ({exc})"
            f"{_worker_detail(proc.stderr)}"
        ) from exc
    if not isinstance(response, dict):
        raise TclReadError(
            f"the Tcl worker process wrote a {type(response).__name__}, "
            f"not a JSON object"
        )
    return response


def _read_tcl(text: str, warnings: list[str] | None = None) -> list[_Command]:
    """Primary reader: evaluate ``text`` in a Tcl safe interp.

    The interp lives in a **separate process** —
    :mod:`rtl_buddy_cdc.tcl_worker`, spawned per call. That is load
    bearing, not incidental: importing ``_tkinter`` into *this* process
    starts a Tcl notifier thread that can wedge a later
    ``subprocess`` fork+exec on macOS forever, and rb-cdc shells out to
    yosys / slang right after parsing. The worker's docstring carries
    the full stack trace and the reproduction; **do not move the interp
    back in-process**.

    ``warnings``, when given, is extended with any diagnostics the
    worker reported alongside a successful read.

    Raises :class:`TclReadError` when the script does not evaluate
    (genuine Tcl syntax errors, undefined variables, a worker that
    crashed or wrote garbage) and :class:`TclResourceLimitError` when
    a budget was exceeded; :func:`parse` catches both and re-reads with
    the tokenizer.
    """
    response = _run_worker(text, TCL_COMMAND_LIMIT, TCL_TIME_LIMIT_SECONDS)
    if not response.get("ok"):
        message = str(response.get("error") or "unknown Tcl worker failure")
        if response.get("kind") == "resource_limit":
            raise TclResourceLimitError(message)
        raise TclReadError(message)
    try:
        if warnings is not None:
            warnings.extend(str(w) for w in response.get("partial_warnings") or ())
        commands = []
        for entry in response.get("commands") or ():
            line = entry.get("line")
            commands.append(
                _Command(
                    words=[str(w) for w in entry["words"]],
                    line=int(line) if isinstance(line, int) else None,
                )
            )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise TclReadError(
            f"the Tcl worker process wrote a malformed response ({exc})"
        ) from exc
    return commands


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
    # A collection loop is not evaluated (the reader resolves nothing
    # against the design, so it has no objects to iterate over): the
    # constraints in its body never reach the parser (rtl-buddy-cdc#382).
    "foreach_in_collection": (
        "collection loops are not evaluated, so the constraints in the loop "
        "body are ignored; write them on the collection directly"
    ),
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
    # Source line of the command, when the reader knows it (Tcl backend).
    # Handlers that record a constraint for reporting keep it (#328).
    line: int | None = None

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


# ---- unrecognised collection commands (rtl-buddy-cdc#382) ------------------

#: Bracketed command heads every handler can read: the ``get_*`` /
#: ``all_*`` collections, and ``expr`` — the tokenizer leaves a delay
#: value such as ``[expr {$p * 0.4}]`` unevaluated, and the handlers
#: already skip it as a number.
_RECOGNISED_HEADS: frozenset[str] = frozenset(
    {
        "get_clocks",
        "get_ports",
        "get_pins",
        "get_cells",
        "get_nets",
        "all_inputs",
        "all_outputs",
        "all_clocks",
        "all_registers",
        "expr",
    }
)

#: Commands whose *tail* may also hold the collection operators
#: (``remove_from_collection`` / ``add_to_collection``), which
#: :func:`_port_terms` evaluates. Anywhere else they are unrecognised.
_OPERATOR_TAIL_COMMANDS: frozenset[str] = frozenset(
    {"set_input_delay", "set_output_delay"}
)

# A bracket that is a bus index, not a command: ``d[3]``, ``d[*]``,
# ``d[7:0]``, ``d[$i]`` (an unevaluated variable, tokenizer only).
_INDEX_HEAD = re.compile(r"[0-9*?:,-]*|\$[A-Za-z0-9_:]+")


def _bracket_heads(text: str) -> list[str]:
    """The command name after every ``[`` in ``text`` (bus indices too)."""
    heads: list[str] = []
    for i, ch in enumerate(text):
        if ch != "[":
            continue
        j = i + 1
        while j < len(text) and text[j].isspace():
            j += 1
        k = j
        while k < len(text) and not text[k].isspace() and text[k] not in "[]{}":
            k += 1
        heads.append(text[j:k])
    return heads


def _unrecognised_head(item: str, operators: bool) -> str | None:
    """The first command in ``item`` no handler can evaluate, or ``None``.

    With ``operators``, an item that *is* a collection operator call is
    accepted and its operands are checked in turn; an operator nested
    anywhere else (inside a ``get_*``) is unrecognised, because the Tcl
    worker flattens the operands of a ``get_*`` call.
    """
    text = item.strip()
    if operators and text.startswith("[") and _closing_bracket(text) == len(text) - 1:
        parts = _endpoint_items(text[1:-1])
        if parts and parts[0] in COLLECTION_OPERATORS:
            for operand in parts[1:]:
                for sub in _endpoint_items(operand):
                    bad = _unrecognised_head(sub, operators)
                    if bad is not None:
                        return bad
            return None
    for head in _bracket_heads(text):
        if head in _RECOGNISED_HEADS or _INDEX_HEAD.fullmatch(head):
            continue
        return head
    return None


def _drop_unrecognised_operands(cmd: str, p: Parsed, warnings: list[str]) -> Parsed:
    """Replace every operand naming a command no handler evaluates.

    The Tcl worker hands an unrecognised command back as its own call
    (``[filter_collection [all_inputs] {…}]``) and the tokenizer keeps it
    as the same opaque word, so this one pass covers both backends. Each
    such item is dropped — an operand that was nothing but it becomes
    ``""``, which every handler reads as naming nothing — with one
    ``partial_warnings`` entry. A constraint is never applied to an
    argument of a command the reader does not understand (before
    rtl-buddy-cdc#382 the Tcl backend resolved such a call to its last
    argument, so ``[remove_from_collection [all_inputs] [get_ports clk]]``
    typed ``clk``).
    """
    where = f"{cmd}{_at_line(p.line)}"

    def clean(word: str, operators: bool) -> str:
        if "[" not in word:
            return word
        items = _endpoint_items(word)
        kept: list[str] = []
        for item in items:
            head = _unrecognised_head(item, operators)
            if head is None:
                kept.append(item)
                continue
            warnings.append(
                f"{where}: [{head} …] is not a collection command this reader "
                f"evaluates here; the operand {item} is ignored"
            )
        return word if len(kept) == len(items) else " ".join(kept)

    def clean_value(value: Any) -> Any:
        if isinstance(value, str):
            return clean(value, False)
        if isinstance(value, list):
            return [clean(w, False) for w in value]
        return value

    flags = {f: [clean_value(v) for v in vs] for f, vs in p.flags.items()}
    operators = cmd in _OPERATOR_TAIL_COMMANDS
    tail = [clean(w, operators) for w in p.tail]
    return dataclasses.replace(p, flags=flags, tail=tail)


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
    # ``set_case_analysis <value> <targets>`` — no flags; the value and
    # the target collections both land in the tail.
    "set_case_analysis": ArgSpec(flags={}),
    "set_max_delay": ArgSpec(
        flags={
            "-datapath_only": Arity.ZERO,
            "-from": Arity.GREEDY,
            "-to": Arity.GREEDY,
            "-rise_from": Arity.GREEDY,
            "-fall_from": Arity.GREEDY,
            "-rise_to": Arity.GREEDY,
            "-fall_to": Arity.GREEDY,
            "-through": Arity.GREEDY,
            "-rise_through": Arity.GREEDY,
            "-fall_through": Arity.GREEDY,
            "-rise": Arity.ZERO,
            "-fall": Arity.ZERO,
            "-reset_path": Arity.ZERO,
            "-ignore_clock_latency": Arity.ZERO,
            "-comment": Arity.ONE,
        },
    ),
}

# Commands in ``ARG_SPECS`` that are CDC-relevant only in some forms.
# The predicate sees the raw argument words; a False answer sends the
# command down the unrecognised-command path (DEBUG log, tokenizer-skip
# note), exactly as if it had no ``ARG_SPECS`` entry.
_COMMAND_GATES: dict[str, Callable[[list[str]], bool]] = {
    # Plain set_max_delay is STA noise and stays ignored; only the
    # -datapath_only form carries CDC intent (rtl-buddy-cdc#325).
    "set_max_delay": lambda args: "-datapath_only" in args,
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
    _claim_ports(spec, "create_clock", name, ports, add=p.present("-add"))
    spec.clocks[name] = Clock(name=name, period=period, ports=tuple(ports))


def _claim_ports(
    spec: ClockSpec, cmd: str, name: str, ports: list[str], *, add: bool
) -> None:
    """Apply SDC replace-vs-add semantics to a new clock's ports (#330).

    Without ``-add``, a clock defined on a source object that already
    carries a clock *replaces* it: the earlier clock loses that port,
    and is removed altogether once it has no port left. With ``-add``
    both clocks exist on the port (:attr:`ClockSpec.multi_clock_ports`).
    A replacement is almost always an accident in a constraints file —
    the earlier clock silently stops existing — so it warns; ``-add`` is
    a legitimate declaration and does not.
    """
    if add or not ports:
        return
    claimed = set(ports)
    for other in list(spec.clocks.values()):
        if other.name == name:
            continue
        lost = [p for p in other.ports if p in claimed]
        if not lost:
            continue
        kept = tuple(p for p in other.ports if p not in claimed)
        if kept:
            spec.clocks[other.name] = dataclasses.replace(other, ports=kept)
            fate = f"'{other.name}' keeps {', '.join(kept)}"
        else:
            del spec.clocks[other.name]
            fate = f"'{other.name}' is removed"
        spec.partial_warnings.append(
            f"{cmd} '{name}' without -add replaces clock '{other.name}' on "
            f"port {', '.join(repr(p) for p in lost)}; {fate} (use -add to "
            f"keep both clocks on the port)"
        )


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
    where = f"create_generated_clock{f' {name}' if name else ''}{_at_line(p.line)}"
    master = (
        _single_clock(master_word, f"{where} -master_clock", spec)
        if master_word
        else None
    )

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
        add = p.present("-add")
        for t in targets:
            prev = spec.pin_clocks.get(t)
            if prev is not None and prev != name:
                # rtl-buddy-cdc#330: replace-vs-add on an internal pin.
                # ``pin_clocks`` holds one clock per pin, so an added
                # clock is kept out of the domain model (the first one
                # stays) and said so; a replacement keeps today's
                # last-writer-wins and warns like a port replacement.
                if add:
                    spec.partial_warnings.append(
                        f"create_generated_clock '{name}' -add on pin '{t}', "
                        f"which already carries '{prev}': several generated "
                        f"clocks on one internal pin are not modelled — flops "
                        f"behind it stay in '{prev}'"
                    )
                    continue
                spec.partial_warnings.append(
                    f"create_generated_clock '{name}' without -add replaces "
                    f"clock '{prev}' on pin '{t}' (use -add to keep both)"
                )
            spec.pin_clocks[t] = name
    else:
        clock_ports = tuple(targets)
        _claim_ports(
            spec, "create_generated_clock", name, targets, add=p.present("-add")
        )

    if name in spec.clocks:
        # G-11 duplicate-name check, mirroring create_clock's surface.
        spec.partial_warnings.append(
            f"duplicate clock name '{name}': second create_generated_clock "
            f"silently overrides the first"
        )
    source_word = p.first("-source")
    source = tuple(_extract_names(source_word)[0]) if source_word is not None else ()
    spec.clocks[name] = Clock(
        name=name,
        period=period,
        ports=clock_ports,
        master=master,
        is_generated=True,
        source=source,
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

    where = f"set_clock_groups{_at_line(p.line)}"
    groups: list[set[str]] = []
    for slurped in p.all("-group"):
        # ``slurped`` is a list[str] of words after ``-group``, up to
        # the next ``-flag``. With a Tcl-aware tokenizer the common
        # case is a single word like ``{ck_a ck_b}`` or
        # ``[get_clocks ck_a]``; the GREEDY arity also tolerates the
        # un-collection form ``-group ck_a ck_b`` real SDC sometimes
        # uses. ``[all_clocks]`` and ``[get_clocks <glob>]`` expand
        # against the declared clocks (rtl-buddy-cdc#390).
        flags = _TermFlags()
        members = _read_clocks(slurped, where, spec, flags, ports_are_clocks=True)
        if flags.saw_filter:
            flags.warnings.append(
                f"{where}: ignored unsupported -filter clause in a -group"
            )
        spec.partial_warnings.extend(flags.warnings)
        if members:
            groups.append(set(members))
        elif any(w.strip() for w in slurped) and not flags.warnings:
            spec.partial_warnings.append(
                f"{where}: -group {' '.join(slurped)} selects no clock; the "
                f"group is ignored"
            )

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
    """Parse ``set_false_path``.

    A statement whose endpoints are all clocks
    (``-from [get_clocks A] -to [get_clocks B]``, or bare / braced
    names) is a pairwise async declaration: the pairs land in
    :attr:`ClockSpec.false_path_pairs`. That path is unchanged from
    before rtl-buddy-cdc#328, byte for byte.

    Anything narrower — a design-object endpoint (``[get_cells …]``,
    ``[get_pins …]``, ``[get_ports …]``, ``[get_nets …]``) on either
    side, or a ``-through`` — is recorded as a
    :class:`FalsePathException` on
    :attr:`ClockSpec.false_path_exceptions` instead, with any clock
    endpoints given alongside it. It never adds a clock pair: an
    object-scoped false path acknowledges specific paths, it does not
    make the clocks asynchronous. Resolving the objects against the
    netlist is :mod:`rtl_buddy_cdc.sdc_objects`'s job (the parser stays
    netlist-free).
    """
    from_clocks: list[str] = []
    to_clocks: list[str] = []
    from_objects: list[SdcObject] = []
    to_objects: list[SdcObject] = []
    qualifiers: list[str] = []
    where = f"set_false_path{_at_line(p.line)}"
    flags = _TermFlags()
    # A side whose flag carried operands, even ones that were dropped as
    # unreadable: it must select something, or the statement is ignored
    # rather than read as "any object" on that side.
    given: set[str] = set()

    for flag, side, clocks, objects in (
        ("-from", "-from", from_clocks, from_objects),
        ("-rise_from", "-from", from_clocks, from_objects),
        ("-fall_from", "-from", from_clocks, from_objects),
        ("-to", "-to", to_clocks, to_objects),
        ("-rise_to", "-to", to_clocks, to_objects),
        ("-fall_to", "-to", to_clocks, to_objects),
    ):
        for slurped in p.all(flag):
            if flag not in ("-from", "-to") and flag not in qualifiers:
                qualifiers.append(flag)
            if slurped:
                given.add(side)
            cs, objs = _read_endpoints(slurped, where, spec, flags)
            clocks.extend(cs)
            objects.extend(objs)
    saw_object = flags.saw_object
    saw_filter = flags.saw_filter
    spec.partial_warnings.extend(flags.warnings)

    through: list[tuple[SdcObject, ...]] = []
    for flag in ("-through", "-rise_through", "-fall_through"):
        for slurped in p.all(flag):
            if flag != "-through" and flag not in qualifiers:
                qualifiers.append(flag)
            group: list[SdcObject] = []
            for word in slurped:
                for item in _endpoint_items(word):
                    kind, names, sf, hier = _split_endpoint_item(item)
                    saw_filter = saw_filter or sf
                    if kind is None:
                        group.extend(SdcObject("any", n) for n in names)
                    elif kind in _OBJECT_KINDS:
                        group.extend(SdcObject(kind, n, hier) for n in names)
                    else:
                        spec.partial_warnings.append(
                            f"set_false_path{_at_line(p.line)}: -through "
                            f"{item} is not a pin, net or port — ignored"
                        )
            if group:
                through.append(tuple(dict.fromkeys(group)))
            else:
                # An empty -through group cannot be matched; dropping it
                # silently would widen the exception to every path.
                spec.partial_warnings.append(
                    f"set_false_path{_at_line(p.line)}: -through names no "
                    f"pin, net or port — statement ignored"
                )
                return

    if saw_object and not from_objects and not to_objects and not through:
        spec.partial_warnings.append(
            f"set_false_path{_at_line(p.line)}: object endpoint names no usable "
            f"pattern (unsupported -filter?) — statement ignored"
        )
        return
    if not from_objects and not to_objects and not through:
        # Pure clock-pair form — the pre-#328 path, unchanged.
        if not from_clocks or not to_clocks:
            spec.partial_warnings.append(
                "set_false_path: incomplete -from/-to clock list, ignored"
            )
            return
        for a in from_clocks:
            for b in to_clocks:
                if a != b:
                    spec.false_path_pairs.add(frozenset({a, b}))
        return

    if saw_filter:
        # A -filter clause drops the names it would have selected, so the
        # side it sat on would read as "any object" — a wider exception
        # than the author wrote. Drop the statement instead.
        spec.partial_warnings.append(
            f"set_false_path{_at_line(p.line)}: unsupported -filter clause in "
            f"an object-scoped false path — statement ignored"
        )
        return
    for side, found in (
        ("-from", from_clocks or from_objects),
        ("-to", to_clocks or to_objects),
    ):
        if side in given and not found:
            # Every operand on this side was unreadable (reported above);
            # an empty side would widen the exception to any object.
            spec.partial_warnings.append(
                f"{where}: {side} selects nothing — statement ignored"
            )
            return
    for flag in ("-setup", "-hold"):
        if p.present(flag):
            qualifiers.append(flag)
    spec.false_path_exceptions.append(
        FalsePathException(
            from_clocks=tuple(dict.fromkeys(from_clocks)),
            to_clocks=tuple(dict.fromkeys(to_clocks)),
            from_objects=tuple(dict.fromkeys(from_objects)),
            to_objects=tuple(dict.fromkeys(to_objects)),
            through=tuple(through),
            qualifiers=tuple(qualifiers),
            line=p.line,
        )
    )


#: Port-collection commands that select every top-level port of one
#: direction, keyed to the ``set_*_delay`` command that direction suits.
_ALL_PORTS_DIRECTION: dict[str, str] = {
    "all_inputs": "set_input_delay",
    "all_outputs": "set_output_delay",
}


def _handle_set_delay(spec: ClockSpec, p: Parsed, command: str) -> None:
    """Parse ``set_input_delay`` / ``set_output_delay``.

    The only CDC-relevant fields are ``-clock <name>`` and the
    trailing ``[get_ports <port>]``. The numeric delay value lands in
    ``p.tail`` alongside the port collection — we pick out anything
    that looks like a collection and ignore the rest.
    """
    clock_word = p.first("-clock")
    # An operand dropped as unrecognised reads as "" (see
    # ``_drop_unrecognised_operands``): no anchor at all.
    clock = (
        _single_clock(clock_word, f"{command}{_at_line(p.line)} -clock", spec)
        if clock_word
        else None
    )

    terms: list[tuple[str, tuple[str, ...]]] = []
    flags = _TermFlags()
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
            for item in _endpoint_items(word):
                got = _port_terms(_unbrace(item), command, flags, nested=False)
                if got is not None:
                    terms.extend(got)

    if flags.saw_filter:
        spec.partial_warnings.append("set_*_delay: ignored unsupported -filter clause")
    spec.partial_warnings.extend(flags.warnings)
    if not terms:
        # No port target — delay-only constraint or defaults-applies-
        # to-all-ports usage. Nothing actionable for CDC; stay silent.
        return
    ports = [target for target, _excl in terms]
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
    excludes = tuple(excl for _t, excl in terms)
    spec.port_delays.append(
        PortDelay(
            command=command,
            clock=clock,
            targets=tuple(ports),
            line=p.line,
            excludes=excludes if any(excludes) else (),
        )
    )
    # Exact names are typed now; a pattern (glob, bus index, ``*`` for
    # ``[all_inputs]``) keys ``port_clock`` as written until
    # ``sdc_objects.resolve_port_delays`` expands it against the netlist.
    # A removal is applied here only where it is decidable without the
    # netlist — the removed pattern matches the target's text.
    for target, excl in terms:
        if not any(_name_matches(pattern, target) for pattern in excl):
            spec.port_clock[target] = clock


@dataclass
class _TermFlags:
    """Diagnostics a collection evaluation accumulates for one statement.

    ``saw_object`` records a design-object collection in a ``-from`` /
    ``-to`` operand (:func:`_read_endpoints`), even one whose names were
    all dropped.
    """

    saw_filter: bool = False
    saw_object: bool = False
    warnings: list[str] = field(default_factory=list)


#: Port-set term: an included pattern and the patterns removed from it.
_Term = tuple[str, tuple[str, ...]]


def _port_terms(
    word: str, command: str, flags: _TermFlags, *, nested: bool
) -> list[_Term] | None:
    """Evaluate one ``set_*_delay`` target operand to port-set terms.

    Returns ``None`` when ``word`` is not a port target at all — the
    tokenizer's unevaluated ``[expr …]`` delay value, a pin / cell
    collection at the top level — or when it is one this reader cannot
    evaluate (then with a warning on ``flags``).

    Handles ``[get_ports …]``, bare / braced names, ``[all_inputs]`` /
    ``[all_outputs]`` and, recursively, ``remove_from_collection`` /
    ``add_to_collection`` (rtl-buddy-cdc#382). Both backends hand the
    operators over as the same word: the tokenizer keeps ``[…]`` opaque
    and the Tcl worker re-wraps the call with its operands intact.
    """
    text = word.strip()
    if text.startswith("[") and _closing_bracket(text) == len(text) - 1:
        parts = _endpoint_items(text[1:-1])
        if parts and parts[0] in COLLECTION_OPERATORS:
            return _operator_terms(parts[0], parts[1:], command, flags)
        if parts and parts[0] in _ALL_PORTS_DIRECTION:
            # ``[all_inputs]`` / ``[all_outputs]`` (rtl-buddy-cdc#378)
            # select every port of one direction: recorded as the glob
            # ``*``, which ``resolve_port_delays`` narrows to the
            # command's direction and to non-clock ports, as it does for
            # ``[get_ports *]``. The other direction's collection selects
            # nothing this command can type.
            head = parts[0]
            if _ALL_PORTS_DIRECTION[head] == command:
                return [("*", ())]
            flags.warnings.append(
                f"{command} [{head}]: {head} selects no port "
                f"{command} can constrain; the target is ignored"
            )
            return [] if nested else None
    # ``_split_collection`` keeps a bus index on its name (``{d[3]}`` →
    # ``d[3]``, where ``_extract_names`` split it into ``d`` and ``3``);
    # ``sdc_objects.resolve_port_delays`` resolves indices and globs
    # against the netlist (#357).
    kind, names, sf = _split_collection(text)
    if kind not in (None, "port"):
        if nested:
            flags.warnings.append(
                f"{command}: collection operand {text!r} is not a port "
                f"collection; the target is ignored"
            )
        # At the top level, ``[expr {$p * 0.4}]`` left unevaluated by the
        # tokenizer is the delay value, not a target (its ``*`` would glob
        # every port), and a pin / cell collection names no top-level
        # port.
        return None
    flags.saw_filter = flags.saw_filter or sf
    return [(n, ()) for n in names if not _looks_numeric(n)]


def _operator_terms(
    op: str, operands: list[str], command: str, flags: _TermFlags
) -> list[_Term] | None:
    """``remove_from_collection`` / ``add_to_collection`` over port sets.

    ``remove_from_collection <base> <removed>`` is every port of
    ``<base>`` not in ``<removed>``; ``add_to_collection <base> <added>``
    is their union (``-unique`` changes nothing for a set). The removed
    operand must itself be a plain collection — a removal inside it
    (``A − (B − C)``) is reported and ignored, as is ``-intersect``.
    """
    where = f"{command} [{op} …]"
    values: list[str] = []
    for operand in operands:
        if operand.startswith("-"):
            if op == "add_to_collection" and operand == "-unique":
                continue
            flags.warnings.append(
                f"{where}: unsupported flag {operand}; the target is ignored"
            )
            return None
        values.append(operand)
    if len(values) != 2:
        flags.warnings.append(
            f"{where}: expected two operands (a collection and the objects to "
            f"{'remove' if op == 'remove_from_collection' else 'add'}), got "
            f"{len(values)}; the target is ignored"
        )
        return None
    sides: list[list[_Term]] = []
    for value in values:
        terms: list[_Term] = []
        for item in _endpoint_items(value) or [value]:
            got = _port_terms(_unbrace(item), command, flags, nested=True)
            if got is None:
                return None
            terms.extend(got)
        sides.append(terms)
    base, other = sides
    if op == "add_to_collection":
        return base + other
    if any(excl for _t, excl in other):
        flags.warnings.append(
            f"{where}: a removal inside the removed collection is not "
            f"supported; the target is ignored"
        )
        return None
    removed = tuple(t for t, _excl in other)
    return [(t, excl + removed) for t, excl in base]


def _unbrace(item: str) -> str:
    """``{x}`` → ``x`` when one brace pair encloses the whole item."""
    if item.startswith("{") and _closing_brace(item) == len(item) - 1:
        return item[1:-1].strip()
    return item


def _closing_bracket(text: str) -> int:
    """Index of the bracket closing ``text[0] == '['``, or -1."""
    depth = 0
    for i, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _name_matches(pattern: str, name: str) -> bool:
    """Does the SDC name ``pattern`` (``*`` / ``?`` globs) select ``name``?

    Textual only — the parser has no netlist — so it decides just the
    cases spelled the same way; ``sdc_objects`` re-resolves everything.
    """
    if pattern == name:
        return True
    if not any(ch in "*?" for ch in pattern):
        return False
    body = "".join(
        ".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern
    )
    return re.fullmatch(body, name) is not None


def _handle_set_max_delay(spec: ClockSpec, p: Parsed) -> None:
    """Parse ``set_max_delay <v> -datapath_only -from … -to …`` (#325).

    Only reached for the ``-datapath_only`` form (see
    :data:`_COMMAND_GATES`). Every such constraint is recorded on
    :attr:`ClockSpec.datapath_only_delays`; a clock-to-clock one also
    adds its pairs to :attr:`ClockSpec.datapath_only_pairs`, which
    ``are_async`` reads as an intended asynchronous crossing.

    Endpoints are split per collection: ``[get_clocks …]`` and bare /
    braced names are clocks (as for ``set_false_path``); any other
    ``get_*`` collection is recorded as a ``(kind, name)`` object
    endpoint. Object endpoints never mark a clock pair async; they are
    resolved against the netlist later, by
    :mod:`rtl_buddy_cdc.sdc_objects` (rtl-buddy-cdc#328), which
    associates the record with the crossings it constrains (an
    annotation only — a bounded datapath still needs a synchroniser,
    so nothing is suppressed on its account). ``-through`` narrows the
    constraint to specific paths, so it is never a clock-pair hint.
    The delay value is the numeric operand wherever it sits — before
    the flags, or after them, where a GREEDY ``-to`` slurps it.
    """
    if (
        p.present("-through")
        or p.present("-rise_through")
        or p.present("-fall_through")
    ):
        spec.partial_warnings.append(
            "set_max_delay -datapath_only: -through clauses are path-specific "
            "and not interpreted as clock-pair async hints"
        )
        return

    value: float | None = None
    from_clocks: list[str] = []
    to_clocks: list[str] = []
    from_objects: list[tuple[str, str]] = []
    to_objects: list[tuple[str, str]] = []

    def take_value(word: str) -> bool:
        nonlocal value
        if not _looks_numeric(word):
            return False
        if value is None:
            value = float(word)
        return True

    for word in p.tail:
        take_value(word)

    where = f"set_max_delay -datapath_only{_at_line(p.line)}"
    flags = _TermFlags()
    for flag, clocks, objects in (
        ("-from", from_clocks, from_objects),
        ("-rise_from", from_clocks, from_objects),
        ("-fall_from", from_clocks, from_objects),
        ("-to", to_clocks, to_objects),
        ("-rise_to", to_clocks, to_objects),
        ("-fall_to", to_clocks, to_objects),
    ):
        for slurped in p.all(flag):
            words = [w for w in slurped if not take_value(w)]
            cs, objs = _read_endpoints(words, where, spec, flags)
            clocks.extend(cs)
            objects.extend((o.kind, o.name) for o in objs)
    saw_filter = flags.saw_filter
    spec.partial_warnings.extend(flags.warnings)

    if saw_filter:
        spec.partial_warnings.append(
            "set_max_delay -datapath_only: ignored unsupported -filter clause"
        )
    if not (from_clocks or from_objects) or not (to_clocks or to_objects):
        spec.partial_warnings.append(
            "set_max_delay -datapath_only: incomplete -from/-to endpoint list, ignored"
        )
        return

    record = DatapathOnlyDelay(
        value=value,
        from_clocks=tuple(dict.fromkeys(from_clocks)),
        to_clocks=tuple(dict.fromkeys(to_clocks)),
        from_objects=tuple(dict.fromkeys(from_objects)),
        to_objects=tuple(dict.fromkeys(to_objects)),
        line=p.line,
    )
    spec.datapath_only_delays.append(record)
    if not record.is_clock_pair:
        # Object endpoints: resolved against the netlist and associated
        # with crossings by ``sdc_objects`` (#328); never a clock pair.
        return
    for a in record.from_clocks:
        for b in record.to_clocks:
            if a != b:
                spec.datapath_only_pairs.add(frozenset({a, b}))


def _handle_set_case_analysis(spec: ClockSpec, p: Parsed) -> None:
    """Parse ``set_case_analysis <value> <targets>`` (rtl-buddy-cdc#325).

    Records each top-level port target on
    :attr:`ClockSpec.case_analysis` with the normalised value from
    :data:`CASE_ANALYSIS_VALUES` (last write wins, so a later statement
    on the same target replaces an earlier one). Targets may be
    ``[get_ports p]``, a braced / bare name list, and bus bits in the
    braced form ``{p[3]}``. Pins, cells and nets are out of scope: they
    are reported and ignored. Whether a port target exists in the
    design is checked later, against the netlist
    (:func:`rtl_buddy_cdc.domain.case_analysis_warnings`).
    """
    if not p.tail:
        spec.partial_warnings.append("set_case_analysis: missing value and target")
        return
    raw, targets = p.tail[0], p.tail[1:]
    value = CASE_ANALYSIS_VALUES.get(raw.lower())
    if value is None:
        spec.partial_warnings.append(
            f"set_case_analysis: unrecognised value {raw!r} (expected one of "
            f"0, 1, zero, one, rising, falling) — ignored"
        )
        return
    if not targets:
        spec.partial_warnings.append(f"set_case_analysis {raw}: missing target")
        return
    ports: list[str] = []
    for word in targets:
        kind, names, saw_filter = _split_collection(word)
        if saw_filter:
            spec.partial_warnings.append(
                "set_case_analysis: ignored unsupported -filter clause"
            )
        if kind is None or kind == "port":
            ports.extend(names)
        else:
            spec.partial_warnings.append(
                f"set_case_analysis {raw} on {kind} target(s) "
                f"{', '.join(names) or '(none)'}: only top-level ports are "
                f"supported — ignored"
            )
    for port in ports:
        spec.case_analysis.pop(port, None)  # re-insert: last write wins
        spec.case_analysis[port] = value


_DISPATCH: dict[str, Callable[[ClockSpec, Parsed], None]] = {
    "create_clock": _handle_create_clock,
    "create_generated_clock": _handle_create_generated_clock,
    "set_clock_groups": _handle_set_clock_groups,
    "set_false_path": _handle_set_false_path,
    "set_input_delay": functools.partial(_handle_set_delay, command="set_input_delay"),
    "set_output_delay": functools.partial(
        _handle_set_delay, command="set_output_delay"
    ),
    "set_max_delay": _handle_set_max_delay,
    "set_case_analysis": _handle_set_case_analysis,
}


# ---- collection-peeling helpers --------------------------------------------


# ---- clock collections (rtl-buddy-cdc#390) ---------------------------------
#
# A clock position (``-group``, a clock endpoint, ``-clock``,
# ``-master_clock``) reads clock *collections*, not just names: before
# #390 it peeled its operand with ``_extract_names``, which reads an
# unknown bracketed head as a name, so ``[all_clocks]`` was a clock
# called ``all_clocks``. A clock collection evaluates to set terms
# (:data:`_Term`: an included pattern and the patterns removed from it),
# exactly as a port target does; :func:`_resolve_clock_terms` turns them
# into clock names against the file's declared clocks.


def _clock_leaf(
    text: str, where: str, flags: _TermFlags, *, ports_are_clocks: bool
) -> list[_Term] | None:
    """One clock-position item → set terms, or ``None`` with a warning.

    ``[get_clocks …]`` and bare / braced names are clock patterns;
    ``[all_clocks]`` is the glob ``*`` (every declared clock). With
    ``ports_are_clocks`` a ``[get_ports p]`` / ``[get_pins p]`` names the
    clock ``p`` — the pre-#390 reading a ``-group`` / ``-clock`` keeps,
    since a ``create_clock`` without ``-name`` is named after its port.
    Any other collection (``all_registers``, ``all_inputs``,
    ``all_outputs``, ``get_cells``, ``get_nets``) names no clock.
    """
    kind, names, saw_filter = _split_collection(text)
    flags.saw_filter = flags.saw_filter or saw_filter
    if kind is None or kind == "clock":
        return [(n, ()) for n in names]
    if kind == "all_clocks":
        return [("*", ())]
    if ports_are_clocks and kind in ("port", "pin"):
        return [(n, ()) for n in names]
    flags.warnings.append(
        f"{where}: {text} is not a clock collection, so it selects no clock; "
        f"the operand is ignored"
    )
    return None


def _clock_universe(spec: ClockSpec) -> tuple[str, ...]:
    """The clock names a clock pattern resolves against (see
    :attr:`ClockSpec._clock_universe`)."""
    if spec._clock_universe is not None:
        return spec._clock_universe
    return tuple(spec.clocks)


def _resolve_clock_terms(
    terms: list[_Term], where: str, spec: ClockSpec, warnings: list[str]
) -> list[str]:
    """Set terms → clock names, in order, without duplicates.

    An exact name is kept as written whether or not it is declared, as
    every clock reference always was (an undeclared one is diagnosed
    elsewhere). A glob — ``*`` for ``[all_clocks]`` — expands against the
    file's declared clocks (:func:`_declared_clock_names`); one matching
    nothing is warned about. A removed pattern takes every name it
    matches out of its term; one matching no clock is warned about. A
    glob that picks up a clock declared *below* the statement is warned
    about too: SDC runs in file order, so a timer would leave it out.
    """
    universe = _clock_universe(spec)
    out: list[str] = []
    late: list[str] = []
    for pattern, excl in terms:
        is_glob = any(ch in "*?" for ch in pattern)
        if is_glob:
            hits = [c for c in universe if _name_matches(pattern, c)]
            if not hits:
                warnings.append(
                    f"{where}: clock pattern {pattern!r} matches no declared "
                    f"clock, so it selects nothing"
                )
                continue
        else:
            hits = [pattern]
        for removed in excl:
            if not any(_name_matches(removed, c) for c in (*universe, *hits)):
                warnings.append(
                    f"{where}: remove_from_collection pattern {removed!r} matches "
                    f"no declared clock, so it removes nothing — constraint typo?"
                )
        kept = [c for c in hits if not any(_name_matches(r, c) for r in excl)]
        if is_glob:
            late.extend(c for c in kept if c not in spec.clocks)
        out.extend(kept)
    late = list(dict.fromkeys(late))
    if late:
        warnings.append(
            f"{where}: the clock collection also selects {', '.join(late)}, "
            f"declared later in the file; clock references are resolved against "
            f"the whole file, but a timer evaluates it in order and would leave "
            f"{'it' if len(late) == 1 else 'them'} out — declare clocks first"
        )
    return list(dict.fromkeys(out))


def _read_clocks(
    words: list[str],
    where: str,
    spec: ClockSpec,
    flags: _TermFlags,
    *,
    ports_are_clocks: bool,
) -> list[str]:
    """The clock names a clock-only operand (``-group``, ``-clock``) selects."""
    terms: list[_Term] = []
    for word in words:
        for item in _endpoint_items(word):
            got = _clock_leaf(
                _unbrace(item), where, flags, ports_are_clocks=ports_are_clocks
            )
            if got:
                terms.extend(got)
    return _resolve_clock_terms(terms, where, spec, flags.warnings)


def _read_endpoints(
    words: list[str], where: str, spec: ClockSpec, flags: _TermFlags
) -> tuple[list[str], list[SdcObject]]:
    """A ``-from`` / ``-to`` operand → ``(clocks, objects)``.

    ``get_cells`` / ``get_pins`` / ``get_ports`` / ``get_nets`` items are
    design-object patterns (sets ``flags.saw_object``); every other item
    is read as a clock collection (:func:`_clock_leaf`), so
    ``[all_clocks]`` expands and ``[all_registers]`` selects nothing with
    a warning (rtl-buddy-cdc#390).
    """
    clocks: list[_Term] = []
    objects: list[SdcObject] = []
    for word in words:
        for item in _endpoint_items(word):
            text = _unbrace(item)
            kind, names, saw_filter, hier = _split_endpoint_item(text)
            if kind in _OBJECT_KINDS:
                assert kind is not None
                flags.saw_object = True
                flags.saw_filter = flags.saw_filter or saw_filter
                objects.extend(SdcObject(kind, n, hier) for n in names)
                continue
            got = _clock_leaf(text, where, flags, ports_are_clocks=False)
            if got:
                clocks.extend(got)
    return _resolve_clock_terms(clocks, where, spec, flags.warnings), objects


def _single_clock(word: str, where: str, spec: ClockSpec) -> str | None:
    """The one clock a single-clock option (``-clock``, ``-master_clock``)
    names, or ``None`` with a warning.

    A collection is accepted when it resolves to exactly one clock —
    ``[all_clocks]`` in a one-clock file is that clock. Several clocks
    (``{a b}``, ``[all_clocks]`` with more declared) are not silently cut
    to the first: the option is ignored and said so (rtl-buddy-cdc#390).
    """
    flags = _TermFlags()
    names = _read_clocks([word], where, spec, flags, ports_are_clocks=True)
    if flags.saw_filter:
        flags.warnings.append(f"{where}: ignored unsupported -filter clause")
    if len(names) > 1:
        flags.warnings.append(
            f"{where} {word} selects several clocks ({', '.join(names)}) where "
            f"one is expected; the option is ignored"
        )
    spec.partial_warnings.extend(flags.warnings)
    return names[0] if len(names) == 1 else None


#: ``get_*`` collection command → the object kind recorded for it.
_COLLECTION_KINDS: dict[str, str] = {
    "get_clocks": "clock",
    "get_ports": "port",
    "get_pins": "pin",
    "get_cells": "cell",
    "get_nets": "net",
}


def _split_collection(word: str) -> tuple[str | None, list[str], bool]:
    """Split one endpoint / target word into ``(kind, names, saw_filter)``.

    ``kind`` is ``None`` for a bare or braced name list (``clk``,
    ``{a b}`` — or ``a b`` once the Tcl backend has evaluated the
    braces away), else the collection's noun from
    :data:`_COLLECTION_KINDS` (an unlisted head such as
    ``all_registers`` is kept verbatim). Unlike
    :func:`~rtl_buddy_cdc.tcl_tokenizer._extract_names` this keeps a
    bus index on the name (``{sel[3]}`` → ``sel[3]``) and recognises
    every ``get_*`` head, which is what an object endpoint needs; the
    tokenizer module stays untouched because rtl-buddy vendors it.

    Works on both backends' word shapes: the tokenizer hands through
    ``[get_ports {sel[3]}]`` verbatim, the Tcl reader hands back
    ``[get_ports sel[3]]`` (braces evaluated, collection re-wrapped),
    and a nested ``[get_pins [get_cells u]/C]`` from the tokenizer is
    peeled to ``u/C`` the way the Tcl worker already does.
    """
    text = word.strip()
    kind: str | None = None
    if text.startswith("[") and text.endswith("]"):
        inner = _strip_collection_wrappers(text[1:-1])
        parts = inner.replace("{", " ").replace("}", " ").split()
        if parts:
            head, parts = parts[0], parts[1:]
            kind = _COLLECTION_KINDS.get(head, head)
    else:
        parts = text.replace("{", " ").replace("}", " ").split()
    names: list[str] = []
    saw_filter = False
    for tok in parts:
        if tok == "-filter":
            # The filter expression is everything after the flag in
            # practice; drop it (and say so via ``saw_filter``).
            saw_filter = True
            break
        if tok.startswith("-"):
            # Vendor flags inside a get_* expression (-hierarchical,
            # -quiet, -of_objects …) carry no name we can use.
            continue
        names.append(tok)
    return kind, names, saw_filter


#: Collection kinds that name design objects rather than clocks.
_OBJECT_KINDS: frozenset[str] = frozenset({"cell", "pin", "port", "net"})


def _endpoint_items(word: str) -> list[str]:
    """Split one endpoint word into its top-level items (#328).

    A word is usually one item — ``[get_cells u/q*]``, ``clk`` — but a
    braced or quoted list can mix collections:
    ``{[get_cells a] [get_clocks b]}`` (tokenizer) or
    ``[get_cells a] [get_clocks b]`` (the Tcl backend after
    substitution). Splits on whitespace outside ``[…]`` / ``{…}``; a
    bus index inside a bare name (``u/q[3]``) stays attached.
    """
    text = word.strip()
    if text.startswith("{") and _closing_brace(text) == len(text) - 1:
        text = text[1:-1]
    items: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in text:
        if ch in "[{":
            depth += 1
        elif ch in "]}" and depth:
            depth -= 1
        if ch.isspace() and depth == 0:
            if cur:
                items.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        items.append("".join(cur))
    return items


def _closing_brace(text: str) -> int:
    """Index of the brace closing ``text[0] == '{'``, or -1."""
    depth = 0
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_endpoint_item(item: str) -> tuple[str | None, list[str], bool, bool]:
    """:func:`_split_collection` plus the ``-hierarchical`` flag (#328)."""
    kind, names, saw_filter = _split_collection(item)
    hierarchical = False
    if kind is not None:
        words = item.replace("[", " ").replace("]", " ").split()
        hierarchical = any(w in ("-hierarchical", "-hier") for w in words)
    return kind, names, saw_filter, hierarchical


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
