"""Identify flip-flops in a Yosys netlist and extract their key pins.

Yosys emits a small zoo of FF cells depending on which control pins the
inferred flop has (sync/async reset, enable, set/reset). The CDC analyzer
only cares about the clock pin (to find which domain the flop lives in)
and the data/output pins (to walk fanin/fanout). The reset pin is exposed
for future reset-CDC rules but unused in the initial implementation.
"""

from __future__ import annotations

from dataclasses import dataclass

from rtl_buddy_cdc.netlist import Bit, Cell, Module

# Yosys $-prefixed FF cell types. ``CLK``/``D``/``Q`` are present on every
# variant; reset / enable pin names vary, but the CLK pin is the only one
# we need for domain assignment.
FF_CELL_TYPES: frozenset[str] = frozenset(
    {
        "$dff",
        "$dffe",
        "$adff",
        "$adffe",
        "$aldff",
        "$aldffe",
        "$sdff",
        "$sdffe",
        "$sdffce",
        "$dffsr",
        "$dffsre",
    }
)


@dataclass(frozen=True)
class Flop:
    cell: Cell
    clk: Bit  # the single bit driving the CLK pin
    d: tuple[Bit, ...]
    q: tuple[Bit, ...]

    @property
    def name(self) -> str:
        return self.cell.name

    @property
    def width(self) -> int:
        return len(self.q)


def find_flops(module: Module) -> list[Flop]:
    """Return every flip-flop in ``module``.

    A flop's CLK connection is always a 1-bit vector in Yosys JSON; we
    flatten it to a single :class:`Bit` for convenience.
    """
    out: list[Flop] = []
    for cell in module.cells_of_type(FF_CELL_TYPES):
        clk_bits = cell.connections.get("CLK")
        if not clk_bits or len(clk_bits) != 1:
            # Defensive: skip malformed cells rather than raising — we'd
            # rather emit an analyzer warning than crash on an exotic FF.
            continue
        out.append(
            Flop(
                cell=cell,
                clk=clk_bits[0],
                d=cell.connections.get("D", ()),
                q=cell.connections.get("Q", ()),
            )
        )
    return out
