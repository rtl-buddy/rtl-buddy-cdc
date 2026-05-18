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

This is a hand-rolled tokenizer (shlex), **not** a Tcl interpreter.
That choice is deliberate: real Tcl interpreters execute user code,
add a non-Python dependency, and complicate deployment. The
documented unsupported constructs are command substitution beyond
``[get_clocks …]`` / ``[get_ports …]`` / ``[get_pins …]``, ``set``
variables, ``expr``, and ``-filter`` clauses. When the parser sees a
CDC-relevant command it can't fully understand it appends to
``ClockSpec.partial_warnings`` so the caller can surface a single
end-of-parse warning rather than spamming line-by-line.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

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


# ---- parser -----------------------------------------------------------------


def parse(text: str) -> ClockSpec:
    spec = ClockSpec()
    for raw_line in _logical_lines(text):
        try:
            tokens = shlex.split(raw_line, comments=False, posix=True)
        except ValueError:
            # Tokenizer choked (unbalanced braces in an unsupported
            # command, etc.); skip rather than fail the whole file.
            continue
        if not tokens:
            continue
        cmd, args = tokens[0], tokens[1:]
        if cmd == "create_clock":
            _handle_create_clock(spec, args)
        elif cmd == "create_generated_clock":
            _handle_create_generated_clock(spec, args)
        elif cmd == "set_clock_groups":
            _handle_set_clock_groups(spec, args)
        elif cmd == "set_false_path":
            _handle_set_false_path(spec, args)
        elif cmd in {"set_input_delay", "set_output_delay"}:
            _handle_set_delay(spec, args)
        else:
            # Drop noise (set_max_delay, set_load, set_drive, …) at
            # DEBUG level so users can see what was skipped via
            # ``--verbose`` without the report being flooded.
            logger.debug("sdc: ignoring unsupported command %r", cmd)
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


# ---- internals --------------------------------------------------------------


def _logical_lines(text: str):
    """Yield logical SDC lines: strip comments, join \\-continuations."""
    buf: list[str] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped:
            if buf:
                yield "".join(buf)
                buf = []
            continue
        if stripped.endswith("\\"):
            buf.append(stripped[:-1] + " ")
        else:
            buf.append(stripped)
            yield "".join(buf)
            buf = []
    if buf:
        yield "".join(buf)


def _handle_create_clock(spec: ClockSpec, args: list[str]) -> None:
    name: str | None = None
    period: float | None = None
    ports: list[str] = []
    saw_filter = False

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "-name" and i + 1 < len(args):
            name = args[i + 1]
            i += 2
        elif tok == "-period" and i + 1 < len(args):
            period = float(args[i + 1])
            i += 2
        elif tok == "-waveform" and i + 1 < len(args):
            i += 2  # ignore (CDC doesn't care about edge phase)
        elif tok in {"-add", "-comment"} or tok.startswith("-"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
        else:
            ports_chunk, saw_filter_chunk = _extract_ports_or_pins(args[i:])
            ports.extend(ports_chunk)
            saw_filter = saw_filter or saw_filter_chunk
            break

    if name is None and ports:
        name = ports[0]
    if name is None or period is None:
        return
    if saw_filter:
        spec.partial_warnings.append(
            f"create_clock {name}: ignored unsupported [get_ports -filter ...]"
        )
    spec.clocks[name] = Clock(name=name, period=period, ports=tuple(ports))


def _handle_create_generated_clock(spec: ClockSpec, args: list[str]) -> None:
    """Parse ``create_generated_clock``.

    The CDC-relevant fields are ``-name`` and either ``-master_clock``
    or ``-source`` (both indicate the upstream clock). ``-divide_by`` /
    ``-multiply_by`` only matter for the period; CDC treats the
    generated clock as synchronous to its master regardless of ratio.
    The pin/port target is captured for completeness but unused (the
    analyzer's clock-domain tracing already follows divider flops to
    their master clock).
    """
    name: str | None = None
    master: str | None = None
    divide_by = 1
    multiply_by = 1
    period: float | None = None
    targets: list[str] = []
    target_is_pin = False
    saw_filter = False

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "-name" and i + 1 < len(args):
            name = args[i + 1]
            i += 2
        elif tok == "-master_clock" and i + 1 < len(args):
            master = _strip_get_clocks(args[i + 1])
            i += 2
        elif tok == "-source":
            # The source expression is bracketed (``[get_ports ck_a]``
            # or ``[get_pins u_a/clk_out]``) and shlex splits the
            # opening bracket from the trailing ``]``-suffixed name —
            # so we consume forward until the next ``-`` flag rather
            # than a fixed token count. The source is currently used
            # only as documentation; the master identity comes from
            # ``-master_clock``.
            j = i + 1
            while j < len(args) and not args[j].startswith("-"):
                j += 1
            i = j
        elif tok == "-divide_by" and i + 1 < len(args):
            try:
                divide_by = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif tok == "-multiply_by" and i + 1 < len(args):
            try:
                multiply_by = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif tok == "-edges" and i + 1 < len(args):
            # Edge-list form: period derivable but we don't compute it
            # — CDC doesn't care about edge phase.
            i += 2
        elif tok == "-add" or tok == "-combinational":
            i += 1
        elif tok in {"-edge_shift", "-comment", "-duty_cycle", "-invert"}:
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
        elif tok.startswith("-"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
        else:
            target_blob = " ".join(args[i:])
            target_is_pin = "get_pins" in target_blob
            targets_chunk, saw_filter_chunk = _extract_ports_or_pins(args[i:])
            targets.extend(targets_chunk)
            saw_filter = saw_filter or saw_filter_chunk
            break

    if name is None and targets:
        name = targets[0]
    if name is None:
        spec.partial_warnings.append(
            "create_generated_clock: missing -name (and no fallback target)"
        )
        return

    # Derive a placeholder period so downstream code that reads
    # `clock.period` doesn't crash on generated clocks. If the master
    # is known and has a period, scale; otherwise default to 0.0 (CDC
    # ignores period anyway).
    if master is not None and master in spec.clocks and divide_by > 0:
        period = spec.clocks[master].period * divide_by / max(multiply_by, 1)
    if period is None:
        period = 0.0

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


def _extract_ports_or_pins(rest: list[str]) -> tuple[list[str], bool]:
    """Pull port/pin names out of a trailing ``[get_ports …]`` /
    ``[get_pins …]`` / ``[get_clocks …]`` argument.

    Returns ``(names, saw_unsupported_filter)``. The second tuple
    element is True if a ``-filter`` clause was present (we can't
    evaluate Tcl expressions, so the clause is silently dropped and
    the caller is expected to surface a partial-parse warning).
    """
    blob = " ".join(rest)
    saw_filter = "-filter" in blob
    for chunk in ("[", "]", "{", "}"):
        blob = blob.replace(chunk, " ")
    parts = blob.split()
    # Drop the get_* command head if present.
    if parts and parts[0] in {"get_ports", "get_pins", "get_clocks"}:
        parts = parts[1:]
    # If there was a -filter, throw away the filter expression — we
    # can't evaluate it. Names that came before -filter are still
    # valid, but in the common case nothing precedes it and we end up
    # with an empty list.
    if saw_filter:
        try:
            cut = parts.index("-filter")
            parts = parts[:cut]
        except ValueError:
            pass
    # Drop -include_generated_clocks and similar flags but keep their
    # operands as candidate names if they don't look like flags.
    cleaned: list[str] = []
    skip_next = False
    for tok in parts:
        if skip_next:
            skip_next = False
            continue
        if tok in {"-include_generated_clocks"}:
            continue
        if tok.startswith("-"):
            # Conservative: skip the flag. If it has an operand we
            # can't tell from here, but in practice the unsupported
            # ones we'd hit are -filter (handled above) and
            # -hierarchical (operand-less).
            continue
        cleaned.append(tok)
    return cleaned, saw_filter


def _strip_get_clocks(token: str) -> str:
    """Turn ``"[get_clocks foo]"`` or ``"{foo}"`` or ``"foo"`` into ``"foo"``."""
    cleaned = token
    for chunk in ("[", "]", "{", "}"):
        cleaned = cleaned.replace(chunk, " ")
    parts = [p for p in cleaned.split() if p]
    if parts and parts[0] == "get_clocks":
        parts = parts[1:]
    return parts[0] if parts else token


def _handle_set_clock_groups(spec: ClockSpec, args: list[str]) -> None:
    is_async = "-asynchronous" in args
    is_logical = "-logically_exclusive" in args
    is_physical = "-physically_exclusive" in args

    if not (is_async or is_logical or is_physical):
        spec.partial_warnings.append(
            "set_clock_groups: missing -asynchronous / "
            "-logically_exclusive / -physically_exclusive — ignored"
        )
        return

    # Slurp every non-flag token after each ``-group`` and let
    # ``_extract_clock_list`` strip braces wholesale. Matches the
    # pattern ``_handle_set_false_path`` already uses for its endpoint
    # flags. This is the form that survives ``shlex`` splitting
    # ``-group { ck0 ck1 }`` into five tokens — see issue #23 for the
    # earlier bug where the previous "re-glob the next token" fallback
    # only recovered the first clock.
    groups: list[set[str]] = []
    i = 0
    while i < len(args):
        if args[i] == "-group":
            j = i + 1
            while j < len(args) and not args[j].startswith("-"):
                j += 1
            members = _extract_clock_list(" ".join(args[i + 1 : j]))
            if members:
                groups.append(set(members))
            i = j
        else:
            i += 1

    if len(groups) < 2:
        spec.partial_warnings.append(
            "set_clock_groups: fewer than 2 -group clauses, ignored"
        )
        return

    if is_async:
        spec.async_groups.append(groups)
    if is_logical or is_physical:
        spec.exclusive_groups.append(groups)


def _handle_set_false_path(spec: ClockSpec, args: list[str]) -> None:
    """Parse ``set_false_path -from [get_clocks A] -to [get_clocks B]``.

    Treated as a pairwise async declaration when both endpoints are
    clock collections. ``-through`` makes this path-specific (not a
    clock-pair hint) — we drop those with a partial-parse warning.
    """
    if "-through" in args:
        spec.partial_warnings.append(
            "set_false_path: -through clauses are path-specific and "
            "not interpreted as clock-pair async hints"
        )
        return

    from_clocks: list[str] = []
    to_clocks: list[str] = []
    saw_non_clock_endpoint = False

    i = 0
    _ENDPOINT_FLAGS = {
        "-from",
        "-to",
        "-rise_from",
        "-fall_from",
        "-rise_to",
        "-fall_to",
    }
    while i < len(args):
        tok = args[i]
        if tok in _ENDPOINT_FLAGS:
            target = "from" if "from" in tok else "to"
            # Glob forward until the next -flag or end. shlex splits
            # ``[get_clocks a]`` into ``[get_clocks`` and ``a]`` so we
            # can't just look at args[i+1].
            j = i + 1
            blob_parts: list[str] = []
            while j < len(args) and not args[j].startswith("-"):
                blob_parts.append(args[j])
                j += 1
            blob = " ".join(blob_parts)
            if "get_pins" in blob or "get_cells" in blob or "get_ports" in blob:
                saw_non_clock_endpoint = True
                i = j
                continue
            names, _ = _extract_ports_or_pins(blob_parts)
            if target == "from":
                from_clocks.extend(names)
            else:
                to_clocks.extend(names)
            i = j
        elif tok.startswith("-"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
        else:
            i += 1

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


def _handle_set_delay(spec: ClockSpec, args: list[str]) -> None:
    """Parse ``set_input_delay`` / ``set_output_delay``.

    The only CDC-relevant fields are ``-clock <name>`` and the
    trailing ``[get_ports <port>]``. The numeric delay value, the
    ``-min``/``-max``/``-add_delay``/``-clock_fall``/``-source_latency_included``
    flags are all ignored.
    """
    clock: str | None = None
    ports: list[str] = []
    saw_filter = False

    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "-clock" and i + 1 < len(args):
            clock = _strip_get_clocks(args[i + 1])
            i += 2
        elif tok in {
            "-min",
            "-max",
            "-add_delay",
            "-network_latency_included",
            "-source_latency_included",
            "-clock_fall",
            "-rise",
            "-fall",
        }:
            i += 1
        elif tok in {"-reference_pin", "-level_sensitive"}:
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
        elif tok.startswith("-"):
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
        else:
            # First non-flag token: either the numeric delay (skip)
            # or the [get_ports …] target.
            if "get_ports" in tok or tok.startswith("[") or tok.startswith("{"):
                names, saw = _extract_ports_or_pins(args[i:])
                ports.extend(names)
                saw_filter = saw_filter or saw
                break
            else:
                # Numeric delay: skip and continue to consume the
                # trailing port spec.
                i += 1

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
    for p in ports:
        spec.port_clock[p] = clock


def _extract_clock_list(token: str) -> list[str]:
    """Turn ``"{src_clk dst_clk}"`` or ``"src_clk"`` into a list of names."""
    cleaned = token.replace("{", " ").replace("}", " ")
    cleaned = cleaned.replace("[", " ").replace("]", " ")
    parts = [p for p in cleaned.split() if p]
    if parts and parts[0] == "get_clocks":
        parts = parts[1:]
    return parts
