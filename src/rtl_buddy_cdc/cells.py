"""Yosys internal cell library: what a cell *type name* says.

The analyzer reads two Yosys cell libraries (rtl-buddy-cdc#336):

- **Coarse cells** (``$dff`` / ``$adff`` / ``$mux`` / ``$and`` …) — what
  ``proc; flatten`` leaves. Polarities and reset values are
  *parameters* (``CLK_POLARITY``, ``ARST_VALUE`` …).
- **Gate-level cells** (``$_DFF_PN0_`` / ``$_MUX_`` / ``$_NAND_`` …) —
  what ``techmap`` / ``simplemap`` / ``abc`` produce. A gate-level
  storage cell has *no* parameters: its clock edge, reset kind, reset
  polarity, reset value and enable polarity are encoded in the
  cell-type name, one letter per pin.

This module is the one place that decodes the gate-level names, so a
rule never re-derives a letter position. The authority is the Yosys
internal cell library (``yosys -p 'help -cells'``, ``help <type>`` and
``help <type>+`` for the simulation model; the Yosys manual's
"Gate-level cells" chapter, generated from ``techlibs/common/simcells.v``).
Letter positions, per family (``X`` = ``P``/``N`` polarity, ``V`` =
``0``/``1`` reset value; pins in brackets):

=====================  ====================  ===============================
family                 letters               meaning
=====================  ====================  ===============================
``$_DFF_X_``           C                     clock edge [C]
``$_DFFE_XX_``         C E                   + clock enable [E]
``$_DFF_XXV_``         C R V                 + async reset [R] to V
``$_DFFE_XXVX_``       C R V E               async reset + enable
``$_SDFF_XXV_``        C R V                 sync reset [R] to V
``$_SDFFE_XXVX_``      C R V E               sync reset (priority) + enable
``$_SDFFCE_XXVX_``     C R V E               sync reset gated by enable
``$_DFFSR_XXX_``       C S R                 async set [S] + async reset [R]
``$_DFFSRE_XXXX_``     C S R E               + enable
``$_ALDFF_XX_``        C L                   async load [L] of [AD]
``$_ALDFFE_XXX_``      C L E                 + enable
``$_DLATCH_X_``        E                     latch gate [E]
``$_DLATCH_XXV_``      E R V                 + async reset [R] to V
``$_DLATCHSR_XXX_``    E S R                 + async set [S] / reset [R]
``$_SR_XX_``           S R                   set-reset latch
=====================  ====================  ===============================

``P`` means the pin is active high (posedge for ``C``), ``N`` active low
(negedge). ``$_DFFSR_*`` / ``$_DLATCHSR_*`` reset ``R`` always drives
``Q`` to 0 and has priority over ``S``.

The combinational families the analyzer walks are listed here too, so
"is this a mux / a clock gate?" has one answer across the rule pack.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Literal

ResetKind = Literal["async", "sync"]

# Family prefix -> accepted letter layouts. Layout letters:
#   C  clock edge (pin C)           E  enable / latch gate (pin E)
#   R  async reset (pin R)          r  sync reset (pin R)
#   V  reset value 0/1 for R / r    S  async set (pin S)
#   L  async load (pin L)
# Every prefix ends with ``_`` right after the family name, so no prefix
# is a prefix of another (``$_DFF_`` vs ``$_DFFE_``, ``$_DLATCH_`` vs
# ``$_DLATCHSR_``) and the lookup below is unambiguous.
_LAYOUTS: dict[str, tuple[str, ...]] = {
    "$_DFF_": ("C", "CRV"),
    "$_DFFE_": ("CE", "CRVE"),
    "$_SDFF_": ("CrV",),
    "$_SDFFE_": ("CrVE",),
    "$_SDFFCE_": ("CrVE",),
    "$_DFFSR_": ("CSR",),
    "$_DFFSRE_": ("CSRE",),
    "$_ALDFF_": ("CL",),
    "$_ALDFFE_": ("CLE",),
    "$_DLATCH_": ("E", "ERV"),
    "$_DLATCHSR_": ("ESR",),
    "$_SR_": ("SR",),
}

LATCH_FAMILIES: frozenset[str] = frozenset({"$_DLATCH_", "$_DLATCHSR_", "$_SR_"})


@dataclass(frozen=True)
class GateStorageCell:
    """A decoded Yosys gate-level flop / latch cell-type name.

    Every polarity is ``1`` (active high / posedge) or ``0`` (active low
    / negedge); ``None`` means the cell has no such pin.
    """

    family: str
    # Pin ``C``. ``None`` for latches (``$_DLATCH*``, ``$_SR_``).
    clk_polarity: int | None = None
    # Pin ``R``: async (``$_DFF_XXV_`` / ``$_DFFSR*`` / latches) or sync
    # (``$_SDFF*``), its polarity and the value ``Q`` takes when it
    # asserts.
    reset_kind: ResetKind | None = None
    reset_polarity: int | None = None
    reset_value: int | None = None
    # Pin ``S`` (async set, ``Q`` -> 1).
    set_polarity: int | None = None
    # Pin ``L`` (async load of ``AD``).
    aload_polarity: int | None = None
    # Pin ``E``: clock enable on a flop, the gate on a latch.
    enable_polarity: int | None = None
    # ``$_SDFFCE_*``: the sync reset only acts while ``E`` is active.
    reset_gated_by_enable: bool = False

    @property
    def is_flop(self) -> bool:
        return self.clk_polarity is not None

    @property
    def is_latch(self) -> bool:
        return self.family in LATCH_FAMILIES


def _pol(letter: str) -> int | None:
    return {"P": 1, "N": 0}.get(letter)


@cache
def parse_gate_storage_cell(cell_type: str) -> GateStorageCell | None:
    """Decode a gate-level flop / latch cell type, or ``None``.

    ``None`` for every coarse cell, every combinational gate, and any
    ``$_…_`` name whose suffix does not match its family's documented
    layout (wrong length, a polarity letter that is not ``P``/``N``, a
    reset value that is not ``0``/``1``) — an unknown shape is never
    guessed at.
    """
    for prefix, layouts in _LAYOUTS.items():
        if not cell_type.startswith(prefix):
            continue
        suffix = cell_type[len(prefix) :]
        if not suffix.endswith("_"):
            return None
        letters = suffix[:-1]
        for layout in layouts:
            if len(layout) == len(letters):
                return _decode(prefix, layout, letters)
        return None
    return None


def _decode(family: str, layout: str, letters: str) -> GateStorageCell | None:
    pols: dict[str, int] = {}
    value: int | None = None
    for role, ch in zip(layout, letters, strict=True):
        if role == "V":
            if ch not in "01":
                return None
            value = int(ch)
            continue
        pol = _pol(ch)
        if pol is None:
            return None
        pols[role] = pol
    reset_role = "R" if "R" in pols else "r" if "r" in pols else None
    reset_kind: ResetKind | None = None
    if reset_role is not None:
        reset_kind = "async" if reset_role == "R" else "sync"
        if value is None:
            # $_DFFSR* / $_DLATCHSR* / $_SR_: R clears Q.
            value = 0
    return GateStorageCell(
        family=family,
        clk_polarity=pols.get("C"),
        reset_kind=reset_kind,
        reset_polarity=pols.get(reset_role) if reset_role else None,
        reset_value=value,
        set_polarity=pols.get("S"),
        aload_polarity=pols.get("L"),
        enable_polarity=pols.get("E"),
        reset_gated_by_enable=family == "$_SDFFCE_",
    )


# --- Combinational cells ----------------------------------------------------

# Non-inverting 2:1 muxes: ``Y = S ? B : A`` on pins ``A`` / ``B`` / ``S``.
# Coarse ``$mux`` is W bits wide; gate ``$_MUX_`` is one bit. A rule
# that looks *through* a mux at its data legs (sync-reset mux, toggle
# hold mux, gating mux) accepts exactly these: ``$_NMUX_`` inverts, and
# ``$pmux`` is a priority structure, not one select.
MUX2_TYPES: frozenset[str] = frozenset({"$mux", "$_MUX_"})

# Every mux, as ``type -> (data pins, select pins)``. Gate-level wide
# muxes carry 4 / 8 / 16 data legs on ``A``.. and their selects on
# consecutive letters from ``S``. ``$_NMUX_`` is the inverting 2:1 mux;
# inversion is irrelevant to clock identity, so the clock tracer
# selects through it like any other mux.
MUX_PINS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "$mux": (("A", "B"), ("S",)),
    "$pmux": (("A", "B"), ("S",)),
    "$_MUX_": (("A", "B"), ("S",)),
    "$_NMUX_": (("A", "B"), ("S",)),
    "$_MUX4_": (tuple("ABCD"), ("S", "T")),
    "$_MUX8_": (tuple("ABCDEFGH"), ("S", "T", "U")),
    "$_MUX16_": (tuple("ABCDEFGHIJKLMNOP"), ("S", "T", "U", "V")),
}

# Gate-level cells whose output is a Boolean function of *all* of their
# inputs ``A`` / ``B`` (/ ``C`` / ``D``). On a clock net one input is the
# clock and the rest gate or combine it — the gate-level counterpart of
# coarse ``$and`` / ``$or`` / ``$xor`` …
GATE_LOGIC_TYPES: frozenset[str] = frozenset(
    {
        "$_AND_",
        "$_OR_",
        "$_XOR_",
        "$_XNOR_",
        "$_NAND_",
        "$_NOR_",
        "$_ANDNOT_",
        "$_ORNOT_",
        "$_AOI3_",
        "$_OAI3_",
        "$_AOI4_",
        "$_OAI4_",
    }
)
# The input pins of every cell above (2-input cells simply lack C / D).
GATE_LOGIC_INPUTS: tuple[str, ...] = ("A", "B", "C", "D")
