"""Minimal SDC parser — only the subset CDC analysis cares about.

Supported commands:

    create_clock -name <name> -period <p> [get_ports <port>]
    set_clock_groups -asynchronous -group {<clk> ...} -group {<clk> ...} ...

Everything else (set_input_delay, set_max_delay, set_load, etc.) is
silently accepted and discarded — the analyzer doesn't need timing
numbers, only the clock topology and async-group partitioning. This is
intentional so users can point the tool at their existing constraints
file without curating a CDC-only subset.

This is a hand-rolled tokenizer rather than a real Tcl parser. SDC is
Tcl, but the subset we need does not require command substitution,
variables, or `expr`. If a real-world file ever needs more, swap in a
Tcl-aware front end.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Clock:
    name: str
    period: float
    ports: tuple[str, ...]  # top-level port names this clock is associated with


@dataclass
class ClockSpec:
    """The fully parsed CDC-relevant view of an SDC file."""

    clocks: dict[str, Clock] = field(default_factory=dict)
    # Each entry is one ``set_clock_groups -asynchronous`` invocation,
    # holding the list of *groups*; clocks within the same group are
    # synchronous, clocks in different groups are asynchronous.
    async_groups: list[list[set[str]]] = field(default_factory=list)

    def clock_for_port(self, port: str) -> str | None:
        for clk in self.clocks.values():
            if port in clk.ports:
                return clk.name
        return None

    def are_async(self, a: str, b: str) -> bool:
        """Return True iff clocks ``a`` and ``b`` are declared asynchronous.

        Two clocks are async if some ``set_clock_groups -asynchronous``
        statement places them in *different* groups. Clocks not
        mentioned in any group are conservatively treated as
        synchronous to everything (no false positive); the caller can
        layer stricter policy on top.
        """
        if a == b:
            return False
        for groups in self.async_groups:
            ga = next((g for g in groups if a in g), None)
            gb = next((g for g in groups if b in g), None)
            if ga is not None and gb is not None and ga is not gb:
                return True
        return False


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
        elif cmd == "set_clock_groups":
            _handle_set_clock_groups(spec, args)
        # other SDC commands are intentionally ignored
    return spec


def parse_file(path: str | Path) -> ClockSpec:
    return parse(Path(path).read_text())


# --- internals --------------------------------------------------------------


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
            i += 2  # ignore
        elif tok in {"-add", "-comment"} or tok.startswith("-"):
            # Skip unknown flags; -comment takes an argument, others
            # may or may not. Conservative: peek and skip the next
            # token if it doesn't start with `-`.
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
        else:
            ports.extend(_extract_ports(args[i:]))
            break

    if name is None and ports:
        name = ports[0]
    if name is None or period is None:
        return
    spec.clocks[name] = Clock(name=name, period=period, ports=tuple(ports))


def _extract_ports(rest: list[str]) -> list[str]:
    """Pull port names out of a trailing ``[get_ports …]`` argument.

    SDC writes the port-spec as a Tcl command in square brackets;
    ``shlex`` keeps the brackets as part of the surrounding token. We
    accept either a single bracketed token or a free-form list and
    strip ``get_ports`` / brace pairs / brackets uniformly.
    """
    blob = " ".join(rest)
    for chunk in ("[", "]", "{", "}"):
        blob = blob.replace(chunk, " ")
    parts = blob.split()
    if parts and parts[0] == "get_ports":
        parts = parts[1:]
    return parts


def _handle_set_clock_groups(spec: ClockSpec, args: list[str]) -> None:
    if "-asynchronous" not in args:
        # logically_exclusive / physically_exclusive are not yet
        # interpreted; the conservative fallback (sync until proven
        # otherwise) is correct for those.
        return

    groups: list[set[str]] = []
    i = 0
    while i < len(args):
        if args[i] == "-group" and i + 1 < len(args):
            members = _extract_clock_list(args[i + 1])
            if members:
                groups.append(set(members))
            i += 2
        else:
            i += 1

    if len(groups) >= 2:
        spec.async_groups.append(groups)


def _extract_clock_list(token: str) -> list[str]:
    """Turn ``"{src_clk dst_clk}"`` or ``"src_clk"`` into a list of names."""
    cleaned = token.replace("{", " ").replace("}", " ")
    cleaned = cleaned.replace("[", " ").replace("]", " ")
    parts = [p for p in cleaned.split() if p]
    if parts and parts[0] == "get_clocks":
        parts = parts[1:]
    return parts
