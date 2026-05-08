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
class Module:
    name: str
    ports: dict[str, Port]
    cells: dict[str, Cell]
    netnames: dict[str, Netname]

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


def load(path: str | Path) -> Module:
    """Load a single-module Yosys JSON dump.

    The CDC analyzer always works on a fully-flattened design, so we
    expect exactly one user module (``$scopeinfo`` and similar pseudo
    cells inside it are tolerated).
    """
    data = json.loads(Path(path).read_text())
    mods = data.get("modules", {})
    if not mods:
        raise ValueError(f"{path}: no modules in JSON")
    if len(mods) > 1:
        # Pick the one without a leading "$" (Yosys uses $-prefixed names
        # for paramod / blackbox stubs after flatten).
        candidates = [n for n in mods if not n.startswith("$")]
        if len(candidates) != 1:
            raise ValueError(
                f"{path}: expected exactly one user module after flatten, "
                f"got {list(mods)}"
            )
        name = candidates[0]
    else:
        name = next(iter(mods))

    raw = mods[name]
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
    return Module(name=name, ports=ports, cells=cells, netnames=netnames)
