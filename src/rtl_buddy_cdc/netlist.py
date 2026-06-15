"""Yosys ``write_json`` netlist loader.

Wraps the subset of the Yosys JSON schema we need: ports, cells, and the
bit-level net identity that connects them. Bits in the schema are integers
(net IDs) or one of the strings ``"0"``, ``"1"``, ``"x"``, ``"z"`` for
constants. We preserve them as-is and treat anything non-int as a constant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# A "bit" in Yosys JSON is either an int (net ID) or a constant char.
Bit = int | str


@dataclass(frozen=True)
class Port:
    name: str
    direction: str  # "input" | "output" | "inout"
    bits: tuple[Bit, ...]


@dataclass(frozen=True)
class Cell:
    name: str
    type: str
    connections: dict[str, tuple[Bit, ...]]
    parameters: dict[str, str] = field(default_factory=dict)
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Netname:
    """A named wire / reg from the source. Carries any
    ``(* foo = "bar" *)`` SV attributes the user attached to the
    declaration — Yosys preserves them on the netname rather than the
    cell that drives the bits."""

    name: str
    bits: tuple[Bit, ...]
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PortBoundary:
    """CDC summary of one output/inout port of a blackboxed subtree.

    Produced by the P2 summariser (not by ``load``). ``synchronised``
    True means every register→port path inside the subtree passes a
    recognised synchroniser, so a downstream sink in ``src_clock`` is
    *not* a crossing. ``src_clock`` None mirrors today's
    ``<unconstrained>`` async-against-any-sink source.
    """

    port: str  # output/inout port name (matches Module.ports key)
    src_clock: str | None  # clock domain driving this port; None = unconstrained
    synchronised: bool  # True iff every register->port path is synchronised
    width: int  # number of bits on the port (len(Port.bits))


@dataclass(frozen=True)
class BoundarySummary:
    """Port-direction summary of a blackboxed subtree.

    Only outputs/inouts get a :class:`PortBoundary` (inputs are driven
    by the parent, which already knows their domain). Attached to a
    :class:`Module` (``Module.boundary``) by the P2 summariser; P1 only
    sets :attr:`Module.is_blackbox` from the Yosys ``blackbox``
    attribute and leaves ``boundary`` ``None``.
    """

    module: str  # the summarised module's real (non-$) name
    ports: dict[str, PortBoundary]  # keyed by output/inout port name


@dataclass(frozen=True)
class Module:
    name: str
    ports: dict[str, Port]
    cells: dict[str, Cell]
    netnames: dict[str, Netname]
    # P1/P0 blackbox support. A flattened, summarised subtree arrives as
    # an ordinary Yosys-JSON module carrying ``attributes.blackbox`` with
    # zero cells; ``load`` sets ``is_blackbox`` from that attribute.
    # ``boundary`` is attached later by the P2 summariser, never by
    # ``load``. Defaults keep every existing ``Module`` consumer
    # (find_flops / assign_domains / find_crossings / rules) unchanged.
    is_blackbox: bool = False
    boundary: BoundarySummary | None = None

    def port_of_bit(self, bit: Bit) -> Port | None:
        """Return the top-level port that owns ``bit`` if any."""
        if not isinstance(bit, int):
            return None
        for p in self.ports.values():
            if bit in p.bits:
                return p
        return None

    def cells_of_type(self, types: Iterable[str]) -> list[Cell]:
        wanted = set(types)
        return [c for c in self.cells.values() if c.type in wanted]


def _bits(raw: list[Bit]) -> tuple[Bit, ...]:
    return tuple(raw)


def _is_blackbox(raw: dict) -> bool:
    """True iff a raw Yosys-JSON module carries a truthy ``blackbox``
    attribute.

    Yosys serialises the attribute as a bit-string (``"00…01"`` when
    set). A blackboxed subtree survives ``flatten`` with its real name
    and this attribute (verified by the P1 prep probe), so the loader
    keys on the attribute rather than a ``$``-prefix rename pass.
    """
    val = raw.get("attributes", {}).get("blackbox")
    if val is None:
        return False
    # Any non-zero bit (or a non-bit-string truthy value) counts. The
    # canonical set form is "00…01"; tolerate "1"/"true" defensively.
    return any(ch not in ("0", "") for ch in str(val))


def _parse_module(name: str, raw: dict, *, is_blackbox: bool) -> Module:
    ports = {
        pn: Port(name=pn, direction=pd["direction"], bits=_bits(pd["bits"]))
        for pn, pd in raw.get("ports", {}).items()
    }
    cells = {
        cn: Cell(
            name=cn,
            type=cd["type"],
            connections={pn: _bits(pb) for pn, pb in cd.get("connections", {}).items()},
            parameters=dict(cd.get("parameters", {})),
            attributes=dict(cd.get("attributes", {})),
        )
        for cn, cd in raw.get("cells", {}).items()
    }
    netnames = {
        nn: Netname(
            name=nn,
            bits=_bits(nd["bits"]),
            attributes=dict(nd.get("attributes", {})),
        )
        for nn, nd in raw.get("netnames", {}).items()
    }
    return Module(
        name=name,
        ports=ports,
        cells=cells,
        netnames=netnames,
        is_blackbox=is_blackbox,
    )


def load_with_blackboxes(path: str | Path) -> tuple[Module, dict[str, Module]]:
    """Load a flattened Yosys JSON dump that may carry blackbox siblings.

    Returns ``(top, blackboxes)`` where ``top`` is the single non-``$``
    non-blackbox user module and ``blackboxes`` is a (possibly empty)
    ``dict`` of blackboxed boundary modules keyed by module name. A
    blackboxed subtree survives ``flatten`` with its real name and a
    truthy ``attributes.blackbox`` (P1 prep probe); the parent keeps
    each instance as an ordinary cell whose ``type`` is the blackbox
    module name, so no parent rewrite is needed.

    The single-module invariant is relaxed from "exactly one non-``$``
    module" to "exactly one non-``$`` non-blackbox module (the top) +
    zero-or-more blackbox sibling modules". Two non-``$`` non-blackbox
    modules after flatten is still ambiguous and raises.
    """
    data = json.loads(Path(path).read_text())
    mods = data.get("modules", {})
    if not mods:
        raise ValueError(f"{path}: no modules in JSON")

    blackboxes: dict[str, Module] = {}
    top_candidates: list[str] = []
    for n, raw in mods.items():
        if _is_blackbox(raw):
            blackboxes[n] = _parse_module(n, raw, is_blackbox=True)
        elif not n.startswith("$"):
            # $-prefixed paramod / blackbox stubs (the legacy convention)
            # are still discarded; only real user modules are top
            # candidates.
            top_candidates.append(n)

    if len(top_candidates) != 1:
        raise ValueError(
            f"{path}: expected exactly one user module after flatten, got {list(mods)}"
        )
    name = top_candidates[0]
    top = _parse_module(name, mods[name], is_blackbox=False)
    return top, blackboxes


def load(path: str | Path) -> Module:
    """Load a flattened Yosys JSON dump, returning the top module.

    The CDC analyzer always works on a fully-flattened design, so we
    expect exactly one user (non-blackbox) module; ``$scopeinfo`` and
    similar pseudo cells inside it are tolerated, and ``$``-prefixed
    paramod / blackbox stubs are discarded. Blackbox boundary siblings
    (``attributes.blackbox``) are accepted and dropped on the floor by
    this back-compat entry point — use :func:`load_with_blackboxes` to
    receive them.
    """
    top, _ = load_with_blackboxes(path)
    return top
