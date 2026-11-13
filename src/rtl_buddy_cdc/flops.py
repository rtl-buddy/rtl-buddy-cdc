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
  (decoded by :func:`rtl_buddy_cdc.cells.parse_gate_storage_cell`).

Reset and enable pins differ between the families (``ARST`` / ``SRST`` /
``CLR`` / ``ALOAD`` / ``EN`` coarse, ``R`` / ``L`` / ``E`` gate-level,
with the gate-level polarity in the type name). Rules read them through
:func:`flop_reset`, :func:`flop_enable_pin` and :func:`flop_clk_polarity`
so both families answer the same way (rtl-buddy-cdc#336).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Literal

from rtl_buddy_cdc.cells import ResetKind, parse_gate_storage_cell
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

# Gate-level FF cell-type families (Yosys's ``$_DFF*`` family produced
# by ``simplemap`` / ``abc``). The variant explosion ($_DFFE_PP0P_,
# $_SDFFE_NN1N_, …) is decoded by :func:`cells.parse_gate_storage_cell`;
# this set is the list of clocked families it accepts. Pin convention
# for the entire family: ``C`` / ``D`` / ``Q``.
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


@cache
def is_ff_cell(cell_type: str) -> bool:
    """True iff ``cell_type`` is a Yosys flip-flop cell type the
    analyzer enumerates — either a higher-level family (``$dff`` /
    ``$dffe`` / …) or a gate-level family (``$_DFF_P_``, ``$_DFFE_PP0P_``,
    …).

    Used by every rule helper that needs to ask "is this cell a
    flop?". The two families have different pin name conventions
    (``CLK`` vs ``C``); use :func:`flop_clk_pin` to extract the
    clock connection portably.

    Cached: it is called once per cell visit in every graph walk, and a
    design has only a handful of distinct cell types.
    """
    if cell_type in FF_CELL_TYPES:
        return True
    gate = parse_gate_storage_cell(cell_type)
    return gate is not None and gate.is_flop


def is_latch_cell(cell_type: str) -> bool:
    """True iff ``cell_type`` is a Yosys transparent-latch cell type.

    Covers both higher-level (``$dlatch``) and gate-level
    (``$_DLATCH_P_`` / ``$_DLATCH_N_`` / ``$_DLATCH_PP0_`` /
    ``$_DLATCHSR_PNN_`` …) families, decoded by
    :func:`cells.parse_gate_storage_cell`. ``$_SR_*`` has no data pin,
    so it is not a transparent latch. Used by CDC-017
    (latch-in-CDC-path) and elsewhere that needs to ask "is this a
    transparent latch?" without enumerating every variant.
    """
    if cell_type == "$dlatch":
        return True
    gate = parse_gate_storage_cell(cell_type)
    return gate is not None and gate.family in ("$_DLATCH_", "$_DLATCHSR_")


def flop_clk_pin(cell: Cell) -> tuple[Bit, ...] | None:
    """Return the CLK connection for a flop cell, portably across
    families. Returns ``None`` if no recognised CLK pin is connected.

    Higher-level Yosys cells use ``CLK``; gate-level ``$_DFF*`` cells
    use ``C``. The caller is expected to have already checked
    :func:`is_ff_cell`.
    """
    bits = cell.connections.get("CLK") or cell.connections.get("C")
    return bits if bits else None


def _trailing(raw: str | None, default: str) -> str:
    """Trailing bit of a Yosys binary-string parameter (``"1"``,
    ``"0"`` or the 32-bit padded form). ``default`` when the parameter
    is absent; an empty string reads as ``"0"`` (the active-low /
    reset-to-0 convention)."""
    if raw is None:
        return default
    return raw[-1] if raw else "0"


def flop_clk_polarity(cell: Cell) -> int:
    """``1`` for a posedge flop, ``0`` for negedge.

    Coarse cells carry ``CLK_POLARITY``; gate-level cells encode the
    edge as the first letter of the type suffix (``$_DFF_NN0_`` is
    negedge). Anything unclassifiable is treated as posedge — the
    false-negative-biased default for unknown library cells.
    """
    p = cell.parameters.get("CLK_POLARITY") if cell.parameters else None
    if p is not None:
        return 1 if _trailing(p, "1") == "1" else 0
    gate = parse_gate_storage_cell(cell.type)
    if gate is not None and gate.clk_polarity is not None:
        return gate.clk_polarity
    return 1


ResetRole = Literal["arst", "srst", "clr", "aload"]


@dataclass(frozen=True)
class ResetPin:
    """A flop's reset-like pin, the same shape for both cell libraries.

    ``role`` names the coarse pin it corresponds to, so a rule scoped to
    one of them (RDC-001 reads ``ARST`` only) stays scoped on a
    gate-level netlist:

    ========  =========================  ==================================
    role      coarse                     gate-level
    ========  =========================  ==================================
    arst      ``$adff*.ARST``            ``$_DFF_XXV_`` / ``$_DFFE_XXVX_`` R
    srst      ``$sdff*.SRST``            ``$_SDFF*_`` R
    clr       ``$dffsr*.CLR``            ``$_DFFSR*_`` R
    aload     ``$aldff*.ALOAD``          ``$_ALDFF*_`` L
    ========  =========================  ==================================

    ``polarity`` is 1 for active high. ``value`` is the bit ``Q`` takes
    while the pin is asserted (``"0"`` / ``"1"``), or ``None`` for an
    async load, whose value is the ``AD`` input. ``bits`` is the pin's
    connection (may be empty on a malformed cell).
    """

    role: ResetRole
    pin: str
    kind: ResetKind
    polarity: int
    value: str | None
    bits: tuple[Bit, ...]


# Coarse reset pins: type -> (role, pin, polarity param, value param or
# a literal value). ``$dffsr`` surfaces ``CLR`` as its reset (``SET`` is
# a separate concern, like gate-level ``S``).
_COARSE_RESETS: dict[str, tuple[ResetRole, str, str, str | None]] = {
    "$adff": ("arst", "ARST", "ARST_POLARITY", "ARST_VALUE"),
    "$adffe": ("arst", "ARST", "ARST_POLARITY", "ARST_VALUE"),
    "$aldff": ("aload", "ALOAD", "ALOAD_POLARITY", None),
    "$aldffe": ("aload", "ALOAD", "ALOAD_POLARITY", None),
    "$dffsr": ("clr", "CLR", "CLR_POLARITY", "0"),
    "$dffsre": ("clr", "CLR", "CLR_POLARITY", "0"),
    "$sdff": ("srst", "SRST", "SRST_POLARITY", "SRST_VALUE"),
    "$sdffe": ("srst", "SRST", "SRST_POLARITY", "SRST_VALUE"),
    "$sdffce": ("srst", "SRST", "SRST_POLARITY", "SRST_VALUE"),
}


def flop_reset(cell: Cell) -> ResetPin | None:
    """The reset pin of flop ``cell`` (see :class:`ResetPin`), or
    ``None`` for a flop without one (``$dff`` / ``$_DFF_P_`` / …)."""
    coarse = _COARSE_RESETS.get(cell.type)
    if coarse is not None:
        role, pin, pol_param, value_src = coarse
        params = cell.parameters or {}
        polarity = 1 if _trailing(params.get(pol_param), "1") == "1" else 0
        value: str | None = None
        if value_src is not None:
            if value_src.endswith("_VALUE"):
                bit = _trailing(params.get(value_src), "0")
                value = bit if bit in "01" else "0"
            else:
                value = value_src
        kind: ResetKind = "sync" if role == "srst" else "async"
        return ResetPin(role, pin, kind, polarity, value, cell.connections.get(pin, ()))
    gate = parse_gate_storage_cell(cell.type)
    if gate is None or not gate.is_flop:
        return None
    if gate.reset_kind is not None and gate.reset_polarity is not None:
        if gate.reset_kind == "sync":
            g_role: ResetRole = "srst"
        elif gate.set_polarity is not None:
            g_role = "clr"  # $_DFFSR*: R clears, S sets
        else:
            g_role = "arst"
        return ResetPin(
            g_role,
            "R",
            gate.reset_kind,
            gate.reset_polarity,
            str(gate.reset_value),
            cell.connections.get("R", ()),
        )
    if gate.aload_polarity is not None:
        return ResetPin(
            "aload",
            "L",
            "async",
            gate.aload_polarity,
            None,
            cell.connections.get("L", ()),
        )
    return None


def flop_reset_bits(cell: Cell, *roles: ResetRole) -> tuple[Bit, ...]:
    """Connection of ``cell``'s reset pin if its role is one of
    ``roles``, else ``()`` — the drop-in for
    ``cell.connections.get("ARST", ())`` that also reads gate-level
    pin ``R`` / ``L``."""
    rst = flop_reset(cell)
    if rst is None or rst.role not in roles:
        return ()
    return rst.bits


def flop_enable_pin(cell: Cell) -> str | None:
    """Name of a flop's clock-enable pin: ``EN`` on the coarse enable
    families, ``E`` on gate-level ``$_DFFE_`` / ``$_SDFFE_`` /
    ``$_SDFFCE_`` / ``$_DFFSRE_`` / ``$_ALDFFE_``; ``None`` without
    one."""
    if cell.type in FF_CELL_TYPES:
        return "EN" if "EN" in cell.connections else None
    gate = parse_gate_storage_cell(cell.type)
    if gate is not None and gate.is_flop and gate.enable_polarity is not None:
        return "E"
    return None


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
