"""Identify flip-flops in a Yosys netlist and extract their key pins.

Yosys emits a small zoo of FF cells. The CDC analyzer enumerates two
families:

- **Higher-level cells** (``$dff`` / ``$dffe`` / ``$adff`` / etc.) —
  what ``proc; flatten;`` produces. CLK pin is ``CLK``; data on ``D``;
  output on ``Q``. ``CLK_POLARITY`` is a parameter.
- **Gate-level cells** (``$_DFF_P_`` / ``$_DFF_N_`` / ``$_DFFE_*`` /
  ``$_SDFF*`` / ``$_DFFSR*`` / ``$_ALDFF*`` and their PP0P-style
  variant explosions) — what ``simplemap`` / ``abc`` produce when
  flops are mapped to standard primitives. CLK pin is ``C``; data on
  ``D``; output on ``Q``. Polarity encoded in the cell-type suffix
  (decoded by ``rules._clk_polarity``).

Reset / enable pins vary by family but aren't load-bearing here —
CDC rule helpers extract those names directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from rtl_buddy_cdc.netlist import Bit, Cell, Module

# Higher-level Yosys FF cell types. Pin convention: ``CLK`` / ``D`` /
# ``Q``. Listed explicitly because Yosys's set of higher-level FF
# families is closed and small.
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

# Gate-level FF cell-type prefixes (Yosys's ``$_DFF*`` family produced
# by ``simplemap`` / ``abc``). The variant explosion ($_DFFE_PP0P_,
# $_SDFFE_NN1N_, …) is absorbed by prefix matching: any cell whose
# type starts with one of these and is followed by a polarity-encoding
# suffix is a flop. Pin convention for the entire family: ``C`` /
# ``D`` / ``Q``. Note: the order matters for prefix matching — longer
# prefixes (e.g. ``$_DFFSRE_``) must come before their shorter
# substrings would; but Python ``str.startswith`` checks each entry
# independently so the order in the frozenset is irrelevant.
GATE_LEVEL_FF_PREFIXES: frozenset[str] = frozenset(
    {
        "$_DFF_",
        "$_DFFE_",
        "$_SDFF_",
        "$_SDFFE_",
        "$_SDFFCE_",
        "$_DFFSR_",
        "$_DFFSRE_",
        "$_ALDFF_",
        "$_ALDFFE_",
    }
)


def is_ff_cell(cell_type: str) -> bool:
    """True iff ``cell_type`` is a Yosys flip-flop cell type the
    analyzer enumerates — either a higher-level family (``$dff`` /
    ``$dffe`` / …) or a gate-level family (``$_DFF_P_``, ``$_DFFE_PP0P_``,
    …).

    Used by every rule helper that needs to ask "is this cell a
    flop?". The two families have different pin name conventions
    (``CLK`` vs ``C``); use :func:`flop_clk_pin` to extract the
    clock connection portably.
    """
    if cell_type in FF_CELL_TYPES:
        return True
    return any(cell_type.startswith(p) for p in GATE_LEVEL_FF_PREFIXES)


def is_latch_cell(cell_type: str) -> bool:
    """True iff ``cell_type`` is a Yosys transparent-latch cell type.

    Covers both higher-level (``$dlatch``) and gate-level
    (``$_DLATCH_P_`` / ``$_DLATCH_N_`` / ``$_DLATCH_PP0_`` …)
    families. The gate-level family uses the polarity-encoding suffix
    convention. Used by CDC-017 (latch-in-CDC-path) and elsewhere
    that needs to ask "is this a transparent latch?" without
    enumerating every variant.
    """
    return cell_type == "$dlatch" or cell_type.startswith("$_DLATCH_")


def flop_clk_pin(cell: Cell) -> tuple[Bit, ...] | None:
    """Return the CLK connection for a flop cell, portably across
    families. Returns ``None`` if no recognised CLK pin is connected.

    Higher-level Yosys cells use ``CLK``; gate-level ``$_DFF*`` cells
    use ``C``. The caller is expected to have already checked
    :func:`is_ff_cell`.
    """
    bits = cell.connections.get("CLK") or cell.connections.get("C")
    return bits if bits else None


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

    Enumerates both higher-level (``$dff*``) and gate-level
    (``$_DFF*``) FF cells. A flop's CLK connection is a 1-bit vector
    in Yosys JSON; we flatten it to a single :class:`Bit`.
    """
    out: list[Flop] = []
    for cell in module.cells.values():
        if not is_ff_cell(cell.type):
            continue
        clk_bits = flop_clk_pin(cell)
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
