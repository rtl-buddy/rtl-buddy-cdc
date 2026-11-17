"""Reset-domain analysis — parallel to :mod:`rtl_buddy_cdc.domain`.

Foundation for the broader RDC (Reset Domain Crossing) work tracked in
issue #107. This module exposes the structural facts the rule pack
needs to reason about resets without each rule re-walking the netlist:

* :class:`ResetSource` — describes the upstream origin of a flop's
  reset pin (top-level port vs. driven by another flop, polarity,
  sync vs. async).
* :class:`ResetDomain` — the per-flop assignment (flop name → reset
  source, or ``None`` when the flop has no reset pin at all).
* :func:`assign_reset_domains` — runs the per-flop walk once and
  returns the full map.

The classifier deliberately stays close to what the Yosys netlist
hands us. Polarity comes straight from the ``$adff*`` / ``$sdff*``
parameter (Yosys preserves it as a multi-bit binary string), or from
the gate-level type name (``$_DFF_PN0_``: active-low async reset).
Sync vs. async is derived from the cell type: ``$adff*`` / ``$aldff*``
/ ``$dffsr*`` and their ``$_DFF_XXV_`` / ``$_ALDFF*`` / ``$_DFFSR*``
gate forms are async, ``$sdff*`` / ``$_SDFF*`` are sync. The reset source is
classified by walking the reset bit back through the standard
backward-fanin: a top-level input port → ``"port"``; a flop's ``Q``
output → ``"inferred"`` (with the driving flop's cell name as the
identifier); a constant → ``"constant"``; anything else (combinational
expression, untracked driver) → ``"comb"``.

Out of scope here, by design:

* The :class:`ResetSource` ``clock`` field (the clock that samples a
  sync reset) is not populated yet — it requires the rule-side context
  to land cleanly. Stubbed for forward-compat; consumers should treat
  ``None`` as "not yet determined".
* Reset-synchronizer recognition lives in a follow-up PR alongside the
  first rule that needs it (RDC-001's recognizer wants the flop +
  clock-domain pair, not just the structural reset shape).
* No rules consume this module yet. The existing CDC-007 walk in
  :mod:`rtl_buddy_cdc.rules` is unchanged; rerooting it onto the new
  data model is a follow-up so the contract change (rule_id, message
  text) can be reviewed independently.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Literal

from rtl_buddy_cdc.cells import MUX2_TYPES
from rtl_buddy_cdc.flops import (
    Flop,
    ResetPin,
    find_flops,
    flop_async_controls,
    flop_reset,
)
from rtl_buddy_cdc.netlist import Bit, Cell, Module

# Which pin carries a flop's reset, its kind and its polarity come from
# :func:`rtl_buddy_cdc.flops.flop_reset`, which reads the coarse
# ``$adff*.ARST`` / ``$aldff*.ALOAD`` / ``$dffsr*.CLR`` / ``$sdff*.SRST``
# parameters and the gate-level ``$_DFF_XXV_`` / ``$_SDFF*`` /
# ``$_DFFSR*`` / ``$_ALDFF*`` type names alike (rtl-buddy-cdc#336).
# ``$dffsr`` / ``$_DFFSR_`` surface the clear pin as the reset; every
# asynchronous pin, the set pin included, is in ``ResetDomain.controls``
# (rtl-buddy-cdc#383).


ResetType = Literal["sync", "async"]
ResetPolarity = Literal["high", "low"]
ResetSourceKind = Literal["port", "inferred", "constant", "comb"]


@dataclass(frozen=True)
class ResetSource:
    """The upstream origin of a flop's reset pin.

    ``name`` identifies the source: a top-level port name for
    ``source="port"``, the driver flop's cell name for
    ``source="inferred"``, the literal constant for
    ``source="constant"``, or an empty string for ``source="comb"``
    (the analyzer doesn't currently summarise combinational reset
    expressions).
    """

    name: str
    polarity: ResetPolarity
    type: ResetType
    source: ResetSourceKind
    # The clock that samples a sync reset, or ``None`` for async
    # resets and for sync resets whose context isn't available yet.
    # Stubbed; populated by the rule-side context in a later PR.
    clock: str | None = None


@dataclass(frozen=True)
class AsyncControl:
    """One asynchronous control pin of a flop (rtl-buddy-cdc#383).

    ``pin`` is the pin itself (:func:`~rtl_buddy_cdc.flops.flop_async_controls`).
    ``bits`` is the signal that pin acts on, lane for lane, with the
    encoding Yosys wraps around it removed (:func:`effective_control_bit`):
    ``proc`` builds a set / clear flop's pins from muxes — ``CLR =
    clr ? 1 : 0``, ``SET = clr ? 0 : set`` — that are part of how the flop
    is represented, not logic in front of it, and that the gate-level
    flows simplify away. ``source`` classifies ``bits[0]`` the way
    :class:`ResetSource` classifies a reset, with the polarity that
    signal acts with: the pin's, flipped once per inverter stepped
    through (rtl-buddy-cdc#394). An active-low clear is ``CLR`` active
    high behind an inverter in the ``proc`` form and ``R`` active low in
    the gate-level form; both are an active-low reset.
    """

    pin: ResetPin
    bits: tuple[Bit, ...]
    source: ResetSource


@dataclass(frozen=True)
class ResetDomain:
    """A flop's reset assignment.

    ``reset`` is ``None`` for a flop with no reset: no reset pin and no
    synchronous reset mux on ``D`` that ``opt_dff`` would fold into one
    (rtl-buddy-cdc#394); consumers should treat that as "power-on
    initialisation only". It describes one pin per flop (``CLR`` on a set
    / clear flop; ``SRST`` for the ``proc`` form's reset mux).

    ``controls`` lists *every* asynchronous control pin — ``ARST``,
    ``ALOAD``, or ``CLR`` and ``SET`` — in priority order
    (rtl-buddy-cdc#383); empty for a flop with no asynchronous control.
    For an asynchronous reset, ``reset`` is ``controls[0].source``.
    """

    flop: str
    reset: ResetSource | None
    controls: tuple[AsyncControl, ...] = ()


def assign_reset_domains(
    module: Module,
    *,
    flops: Iterable[Flop] | None = None,
    drivers: dict[Bit, tuple[str, str]] | None = None,
    lane_drivers: dict[Bit, tuple[str, str, int]] | None = None,
    reader_counts: dict[Bit, int] | None = None,
) -> dict[str, ResetDomain]:
    """For each flop in ``module``, classify its reset (if any).

    Returns ``{cell_name: ResetDomain}`` covering every flop. Reset-
    bearing cells get a populated :class:`ResetSource` derived from the
    cell type, its polarity parameter, and a one-hop walk back through
    the netlist's bit-drivers table. A flop with no reset pin (``$dff``
    / ``$dffe`` …) gets the synchronous reset its ``D`` carries in the
    ``proc`` form, where ``opt_dff`` would fold it into ``SRST``
    (rtl-buddy-cdc#394, :func:`_folded_sync_reset`), so the two netlist
    forms of one design classify alike; ``None`` without one.

    ``polarity`` on the returned source is the polarity the reset
    *signal* acts with: the flop pin's, as inferred by Yosys, except
    where Yosys's encoding of the pin inverts it (an active-low clear of
    a set / clear flop, :class:`AsyncControl`; a reset mux whose
    constant is on the ``A`` leg). A user-level override (e.g. ``(*
    reset_polarity = "low" *)`` on a top-level port) is **not** applied
    here — the consumer rule pack reconciles port-declared vs.
    flop-inferred polarity itself; see
    :func:`rtl_buddy_cdc.rules.user_reset_polarity_overrides`.

    ``flops`` / ``drivers`` / ``lane_drivers`` / ``reader_counts``
    (optional, issue #324) let a caller that already holds
    ``find_flops(module)``, the bit-driver maps and the reader counts
    (the rule pack's ``_RuleContext``) skip rebuilding them. Each is
    built from ``module`` when omitted; passing them never changes the
    result.
    """
    if lane_drivers is None:
        lane_drivers = lane_bit_drivers(module)
    if drivers is None:
        drivers = {b: (cell, port) for b, (cell, port, _) in lane_drivers.items()}
    if flops is None:
        flops = find_flops(module)
    readers: dict[Bit, int] | None = reader_counts
    out: dict[str, ResetDomain] = {}
    for f in flops:
        controls = async_controls(module, f.cell, drivers)
        rst = flop_reset(f.cell)
        if rst is None:
            # No reset pin. A synchronous reset may still be on ``D`` as
            # the muxes ``opt_dff`` would fold into ``SRST``
            # (rtl-buddy-cdc#394).
            if readers is None:
                readers = bit_reader_count(module)
            reset = _folded_sync_reset(module, f, drivers, lane_drivers, readers)
            out[f.cell.name] = ResetDomain(
                flop=f.cell.name, reset=reset, controls=controls
            )
            continue
        if not rst.bits:
            # (Defensively) a flop typed as reset-bearing but missing the
            # pin connection: treat it as "no reset" rather than raising —
            # better to skip than crash on an exotic netlist.
            out[f.cell.name] = ResetDomain(
                flop=f.cell.name, reset=None, controls=controls
            )
            continue
        if controls and controls[0].pin.pin == rst.pin:
            reset = controls[0].source
        else:
            reset = _reset_source(module, rst.kind, rst.polarity, rst.bits[0], drivers)
        out[f.cell.name] = ResetDomain(flop=f.cell.name, reset=reset, controls=controls)
    return out


def _folded_sync_reset(
    module: Module,
    flop: Flop,
    drivers: dict[Bit, tuple[str, str]],
    lane_drivers: dict[Bit, tuple[str, str, int]],
    reader_counts: dict[Bit, int],
) -> ResetSource | None:
    """The synchronous reset ``opt_dff`` would fold into ``SRST`` on
    ``flop``, a flop with no reset pin, read off the reset muxes on its
    ``D`` (rtl-buddy-cdc#394); ``None`` when there is none.

    The muxes are the ones :func:`sync_reset_lanes` reads (with
    ``through_enable``, so a reset under an enable, ``$sdffce`` once
    folded, counts as well as one over it, ``$sdffe``), and only when
    every lane is reset by the same selects: ``opt_dff`` then keeps the
    register one cell. Otherwise it slices the register into cells
    with different resets, which one entry for the ``proc`` form's one
    cell cannot describe, and the flop keeps ``reset = None``.

    One select is the reset itself, active high when its constant is on
    the ``B`` leg (``s ? c : d``). Several are combined by a cell
    ``opt_dff`` adds (``$reduce_or``, active high), which classifies as
    ``"comb"``.
    """
    lanes = sync_reset_lanes(
        flop, module, lane_drivers, reader_counts, through_enable=True
    )
    if not lanes:
        return None
    resets = lanes[0][1]
    if not resets or any(r != resets for _, r in lanes):
        return None
    if len(resets) > 1:
        return ResetSource(name="", polarity="high", type="sync", source="comb")
    ((sel, active_high),) = resets
    return _reset_source(module, "sync", 1 if active_high else 0, sel, drivers)


def _reset_source(
    module: Module,
    kind: ResetType,
    polarity: int,
    bit: Bit,
    drivers: dict[Bit, tuple[str, str]],
) -> ResetSource:
    """Classify ``bit``, a reset of ``kind`` that acts with ``polarity``
    (1: active high)."""
    name, source = _classify_reset_source(module, bit, drivers)
    return ResetSource(
        name=name,
        polarity="high" if polarity == 1 else "low",
        type=kind,
        source=source,
    )


def async_controls(
    module: Module,
    cell: Cell,
    drivers: dict[Bit, tuple[str, str]],
) -> tuple[AsyncControl, ...]:
    """Every asynchronous control pin of flop ``cell``, each with the
    signal it acts on and that signal's classification (see
    :class:`AsyncControl`). Pins with no connection are skipped."""
    pins = tuple(p for p in flop_async_controls(cell) if p.bits)
    out: list[AsyncControl] = []
    for pin in pins:
        others = [o for o in pins if o is not pin]
        lanes = [
            effective_control(
                module,
                b,
                pin.polarity,
                frozenset(
                    control_root(module, o.bits[i], drivers)
                    for o in others
                    if i < len(o.bits)
                ),
                drivers,
                strip_inverters=bool(others),
            )
            for i, b in enumerate(pin.bits)
        ]
        bits = tuple(b for b, _ in lanes)
        # The source's polarity is the signal's, not the pin's: ``proc``
        # writes an active-low clear as an inverter in front of an
        # active-high ``CLR`` (rtl-buddy-cdc#394).
        out.append(
            AsyncControl(
                pin=pin,
                bits=bits,
                source=_reset_source(module, pin.kind, lanes[0][1], bits[0], drivers),
            )
        )
    return tuple(out)


# How many cells :func:`control_root` / :func:`effective_control_bit`
# step through. Yosys's encoding is at most two deep (``SET = clr ? 0 :
# (set_n ? 0 : 1)``); the margin covers a buffer or two in between.
_MAX_STRIP = 6
_PASS_THROUGH = frozenset({"$pos", "$buf", "$_BUF_", "$not", "$_NOT_"})
_MUX2 = frozenset({"$mux", "$_MUX_"})
# Lane-wise 2-input gates that, on an active-high pin, can only turn it
# *off* (``$_ANDNOT_`` = ``A & ~B``) and, on an active-low pin, only on.
_DEASSERT_ONLY_HIGH = frozenset({"$and", "$_AND_", "$_ANDNOT_"})
_DEASSERT_ONLY_LOW = frozenset({"$or", "$_OR_", "$_ORNOT_"})


def _lane_inputs(
    module: Module, bit: int, drivers: dict[Bit, tuple[str, str]]
) -> tuple[Cell, dict[str, Bit]] | None:
    """The cell driving ``bit`` and, per input pin, the bit that reaches
    lane ``bit`` of its ``Y`` (a 2:1 mux's ``S`` is shared by every
    lane)."""
    drv = drivers.get(bit)
    if drv is None or drv[1] != "Y":
        return None
    cell = module.cells.get(drv[0])
    if cell is None:
        return None
    y = cell.connections.get("Y", ())
    if bit not in y:
        return None
    i = y.index(bit)
    lane: dict[str, Bit] = {}
    for pin in ("A", "B"):
        bits = cell.connections.get(pin, ())
        if i < len(bits):
            lane[pin] = bits[i]
    s = cell.connections.get("S", ())
    if len(s) == 1:
        lane["S"] = s[0]
    return cell, lane


def control_root(module: Module, bit: Bit, drivers: dict[Bit, tuple[str, str]]) -> Bit:
    """The signal ``bit`` is a buffered or inverted copy of: step back
    through ``$not`` / ``$_NOT_`` / buffers and through 2:1 muxes whose
    legs are the two constants ``0`` and ``1`` (``s ? 1 : 0`` is ``s``,
    ``s ? 0 : 1`` is ``~s``) until something else drives it."""
    for _ in range(_MAX_STRIP):
        if not isinstance(bit, int):
            return bit
        found = _lane_inputs(module, bit, drivers)
        if found is None:
            return bit
        cell, lane = found
        if cell.type in _PASS_THROUGH and "A" in lane:
            bit = lane["A"]
        elif (
            cell.type in _MUX2
            and "S" in lane
            and {lane.get("A"), lane.get("B")} == {"0", "1"}
        ):
            bit = lane["S"]
        else:
            return bit
    return bit


def effective_control_bit(
    module: Module,
    bit: Bit,
    polarity: int,
    other_roots: frozenset[Bit],
    drivers: dict[Bit, tuple[str, str]],
    *,
    strip_inverters: bool = False,
) -> Bit:
    """The signal an asynchronous control pin lane ``bit`` acts on: the
    first half of :func:`effective_control`."""
    return effective_control(
        module, bit, polarity, other_roots, drivers, strip_inverters=strip_inverters
    )[0]


def effective_control(
    module: Module,
    bit: Bit,
    polarity: int,
    other_roots: frozenset[Bit],
    drivers: dict[Bit, tuple[str, str]],
    *,
    strip_inverters: bool = False,
) -> tuple[Bit, int]:
    """The signal an asynchronous control pin lane ``bit`` acts on, with
    the encoding Yosys builds around it removed (rtl-buddy-cdc#383), and
    the polarity it acts with (1: active high).

    ``polarity`` is the pin's. The returned one is the *signal's*: it
    differs from the pin's exactly when an inverter was stepped through
    (``strip_inverters``), as on the ``CLR`` of a ``$dffsr`` whose clear is
    active low (rtl-buddy-cdc#394).

    Two shapes are stepped through, both of which a gate-level flow
    simplifies away and neither of which is reset logic in the design:

    - a **buffer mux** ``s ? 1 : 0``, which is ``s`` (``proc`` builds one
      on every ``CLR`` / ``SET`` lane of a set / clear flop whose control
      is active high);
    - the **priority term** of a set / clear flop: ``proc`` writes
      ``if (clr) q <= 0; else if (set) q <= 1;`` as ``SET = clr ? 0 :
      set``, and after ``abc`` it is ``SET = ~clr & set``. The term
      ``clr`` is the flop's own other control (its
      :func:`control_root` is in ``other_roots``) and it can only
      *deassert* this pin (``polarity`` picks the deasserting constant
      and gate), so it changes nothing the flop does: its clear wins
      anyway.

    ``strip_inverters`` (set for a flop with more than one asynchronous
    control: a set / clear flop) also steps through an inverter — an
    inverter mux ``s ? 0 : 1``, ``$not`` / ``$_NOT_`` — flipping the
    polarity it tracks. Yosys has no polarity parameter it can use on
    those pins, so ``proc`` writes an active-low ``CLR`` / ``SET`` as an
    inverter mux in front of an active-high pin (the gate flows keep it
    as a ``$_NOT_``) where a single-reset flop gets ``ARST_POLARITY = 0``
    and no cell at all. On a single-reset flop an inverter is written by
    the designer and stays: it is a comb-driven reset, as before.
    """
    pol = polarity
    for _ in range(_MAX_STRIP):
        if not isinstance(bit, int):
            return bit, pol
        found = _lane_inputs(module, bit, drivers)
        if found is None:
            return bit, pol
        cell, lane = found
        a, b = lane.get("A"), lane.get("B")
        off = "0" if pol == 1 else "1"
        nxt: Bit | None = None
        if strip_inverters and cell.type in ("$not", "$_NOT_") and a is not None:
            nxt = a
            pol = 1 - pol
        elif cell.type in _MUX2 and "S" in lane:
            if a == "0" and b == "1":
                nxt = lane["S"]
            elif strip_inverters and a == "1" and b == "0":
                nxt = lane["S"]
                pol = 1 - pol
            elif control_root(module, lane["S"], drivers) in other_roots:
                if a == off and isinstance(b, int):
                    nxt = b
                elif b == off and isinstance(a, int):
                    nxt = a
        elif a is not None and b is not None and other_roots:
            deassert_only = _DEASSERT_ONLY_HIGH if pol == 1 else _DEASSERT_ONLY_LOW
            if cell.type in deassert_only:
                # ``$_ANDNOT_`` / ``$_ORNOT_`` invert ``B``: only that
                # input may be the stripped term, so the remaining
                # signal is the real net ``A``.
                inverts_b = cell.type in ("$_ANDNOT_", "$_ORNOT_")
                if control_root(module, b, drivers) in other_roots:
                    nxt = a
                elif not inverts_b and control_root(module, a, drivers) in other_roots:
                    nxt = b
        if nxt is None:
            return bit, pol
        bit = nxt
    return bit, pol


# --- helpers ----------------------------------------------------------------


def _bit_drivers(module: Module) -> dict[Bit, tuple[str, str]]:
    """Mirror of :func:`rtl_buddy_cdc.domain._bit_drivers`.

    Built locally so the reset pass doesn't depend on importing from
    :mod:`rtl_buddy_cdc.domain` (which would create a layering tangle
    once the rule pack also wires this in). The map is small and the
    walk is O(cells); the duplication is cheap.
    """
    out: dict[Bit, tuple[str, str]] = {}
    for cell in module.cells.values():
        for port_name in ("Y", "Q"):
            for b in cell.connections.get(port_name, ()):
                if isinstance(b, int):
                    out[b] = (cell.name, port_name)
    return out


def lane_bit_drivers(module: Module) -> dict[Bit, tuple[str, str, int]]:
    """Map every driven net bit to its ``(cell, output-port, index)``:
    :func:`_bit_drivers` with the bit's index in the output pin, which a
    lane-by-lane walk through a multi-bit cell needs. The rule pack's
    ``_RuleContext.bit_drivers`` is this map."""
    out: dict[Bit, tuple[str, str, int]] = {}
    for cell in module.cells.values():
        for port_name in ("Y", "Q"):
            for idx, b in enumerate(cell.connections.get(port_name, ())):
                if isinstance(b, int):
                    out[b] = (cell.name, port_name, idx)
    return out


def bit_reader_count(module: Module) -> dict[Bit, int]:
    """Count every site that reads each net bit.

    This is the one definition of "who reads this bit" behind every
    single-reader test in the rule pack (the synchroniser-chain walks,
    the reset-mux look-through, the compositional port proofs) and the
    reset classification here. A reader is:

    - a cell **input** pin bit — flop ``D``, combinational inputs, and
      the input pins of a blackbox / hierarchical instance. Flop ``CLK``
      is excluded because clock connections aren't part of the data
      fanout. A cell's *output* pins (``Y`` / ``Q``, or any pin
      ``port_directions`` marks ``output``, such as a blackbox's
      ``clk_out``) drive the bit and are not readers.
    - a module **output** (or ``inout``) port bit. The value leaves the
      module there and is used outside it, so it is a load like any
      other: a synchroniser stage exported straight to a port has a
      second reader exactly as it would through a buffer (rtl-buddy-cdc#344).

    Two reads of the same bit by the same cell on different ports count
    independently — both are real loads.
    """
    counts: dict[Bit, int] = {}
    for cell in module.cells.values():
        for port_name, bits in cell.connections.items():
            if port_name == "CLK" or cell.is_output_pin(port_name):
                continue
            for b in bits:
                if isinstance(b, int):
                    counts[b] = counts.get(b, 0) + 1
    for port in module.ports.values():
        if port.direction not in ("output", "inout"):
            continue
        for b in port.bits:
            if isinstance(b, int):
                counts[b] = counts.get(b, 0) + 1
    return counts


# How many reset muxes :func:`sync_reset_lanes` steps through on one lane.
# ``opt_dff`` has no limit; real designs chain two or three.
MAX_RESET_MUXES = 16
_RESET_CONSTANTS = ("0", "1")


def sync_reset_lanes(
    head: Flop,
    module: Module,
    bit_drivers: dict[Bit, tuple[str, str, int]],
    reader_counts: dict[Bit, int],
    *,
    through_enable: bool = False,
) -> list[tuple[Bit, frozenset[tuple[Bit, bool]]]]:
    """``head``'s sync reset read lane by lane the way Yosys ``opt_dff``
    folds it into ``SRST`` (rtl-buddy-cdc#390): per ``D`` lane, the bit
    left once the reset muxes are stepped through and the set of
    ``(select, active-high)`` resets collected on the way (empty: no
    reset on that lane).

    Per lane, from ``D``, a step is a 2:1 mux (``$mux`` / ``$_MUX_``) with
    a 1-bit select whose output lane has no reader but the step before,
    one leg the constant ``0`` or ``1`` and the other leg anything but
    the lane's own ``Q``. Every constant collected on a lane must equal
    the lane's first: ``if (r1) q <= 0; else if (r2) q <= 0;`` folds into
    ``SRST = r1 | r2``, ``... else if (r2) q <= 1;`` leaves the second mux
    on ``D``. The walk stops at anything else — two constant legs, an
    ``x`` / ``z`` leg, a leg that is the lane's own ``Q`` (an enable, which
    ``opt_dff`` folds into ``EN`` instead), a ``$pmux``, a mux output read
    twice. Observed on Yosys 0.64 by building the same RTL with and
    without ``opt_dff``; ``tests/test_srst_set_pins.py`` pins each case.

    ``through_enable`` (the reset classification, rtl-buddy-cdc#394) also
    steps through one enable mux (a leg that is the lane's own ``Q``)
    when no reset has been collected in front of it, and goes on
    collecting behind it: ``opt_dff`` folds that enable into ``EN`` and
    the reset under it into the ``SRST`` of an ``$sdffce``. A reset in
    front of the enable (``$sdffe``) ends the lane at the enable, as
    without the flag: a second reset under it is not folded. The bit
    returned for a lane walked through an enable is behind the enable,
    so it is not what the flop samples; callers asking that leave the
    flag off.
    """
    out: list[tuple[Bit, frozenset[tuple[Bit, bool]]]] = []
    for i, d_bit in enumerate(head.d):
        q_bit = head.q[i] if i < len(head.q) else None
        bit = d_bit
        value: str | None = None
        resets: set[tuple[Bit, bool]] = set()
        enable_ok = through_enable
        for _ in range(MAX_RESET_MUXES):
            drv = bit_drivers.get(bit) if isinstance(bit, int) else None
            if drv is None or drv[1] != "Y" or reader_counts.get(bit, 0) != 1:
                break
            cell = module.cells.get(drv[0])
            # ``$pmux`` (and every other cell type) is not walked;
            # gate-level ``$_MUX_`` is the 1-bit ``$mux`` (rtl-buddy-cdc#336).
            if cell is None or cell.type not in MUX2_TYPES:
                break
            sel = cell.connections.get("S", ())
            a_bits = cell.connections.get("A", ())
            b_bits = cell.connections.get("B", ())
            idx = drv[2]
            if len(sel) != 1 or idx >= len(a_bits) or idx >= len(b_bits):
                break
            a, b = a_bits[idx], b_bits[idx]
            if a in _RESET_CONSTANTS and b in _RESET_CONSTANTS:
                break
            if enable_ok and not resets and q_bit is not None and q_bit in (a, b):
                enable_ok = False
                bit = b if a == q_bit else a
            elif b in _RESET_CONSTANTS and value in (None, b) and a != q_bit:
                value, bit = b, a
                resets.add((sel[0], True))
            elif a in _RESET_CONSTANTS and value in (None, a) and b != q_bit:
                value, bit = a, b
                resets.add((sel[0], False))
            else:
                break
        out.append((bit, frozenset(resets)))
    return out


def _classify_reset_source(
    module: Module,
    bit: Bit,
    drivers: dict[Bit, tuple[str, str]],
) -> tuple[str, ResetSourceKind]:
    """One-hop classification of the bit driving a reset pin."""
    if not isinstance(bit, int):
        return (str(bit), "constant")
    port = module.port_of_bit(bit)
    if port is not None and port.direction == "input":
        return (port.name, "port")
    drv = drivers.get(bit)
    if drv is None:
        # Untracked driver (e.g. inout or a cell we don't recognise);
        # the safest classification is "comb" — the rule pack can refuse
        # to reason about such resets rather than misclassify them.
        return ("", "comb")
    cell_name, out_port = drv
    if out_port == "Q":
        return (cell_name, "inferred")
    return ("", "comb")


@dataclass(frozen=True)
class ResetSyncChain:
    """A recognised reset-synchroniser chain.

    Returned by :func:`iter_reset_sync_chains`. ``flops`` is the
    ordered list of cell names from tail (downstream-consumer-facing)
    to head (constant-fed). ``head_d_constant`` is the literal D-pin
    constant on the head flop — Yosys-encoded (``"0"`` / ``"1"`` /
    ``"x"`` / ``"z"`` or a multi-bit binary string). ``polarity`` is
    the chain's shared async-reset polarity.

    Consumers compare ``head_d_constant`` against the *deassertion*
    value implied by ``polarity`` (active-low → head D should be 1,
    active-high → head D should be 0). A mismatch is RDC-007's failure
    shape: the chain reloads the *asserted* value on the deassertion
    edge and never propagates "out of reset" to downstream consumers.
    """

    flops: tuple[str, ...]
    head_d_constant: str
    polarity: ResetPolarity


def find_reset_synchronizers(
    module: Module,
    clock_domains: dict[str, str | None],
    *,
    min_depth: int = 2,
    extra_synchronizers: AbstractSet[str] | None = None,
    reset_domains: dict[str, ResetDomain] | None = None,
    flops: Iterable[Flop] | None = None,
    bit_drivers: dict[Bit, tuple[str, str]] | None = None,
) -> set[str]:
    """Identify flop cells that participate in a reset-synchronizer chain.

    The canonical reset-synchronizer is the textbook async-assert /
    sync-deassert pattern: N≥2 flops in the destination clock domain
    sharing the same async reset, chained Q→D, with the chain's head
    flop's ``D`` tied to a constant (typically ``1'b1`` for an active-
    low reset, ``1'b0`` for an active-high reset). The synchronizer's
    last flop's ``Q`` is the synchronised reset that downstream
    consumers connect to their own ``ARST``.

    Returns the set of cell names of *every* flop participating in
    such a chain (head, tail, and anything in between) — RDC-002
    onward will skip these flops to avoid false-positive findings on
    legitimate synchronizers.

    A flop is recognised iff:

    * it is async-reset (has an ``ARST`` / ``ALOAD`` / ``CLR`` pin, or
      gate-level ``R`` / ``L``, per :func:`~rtl_buddy_cdc.flops.flop_reset`),
    * the chain walking backward through its ``D`` pin (following
      same-clock, same-reset-source flops only) reaches a flop whose
      ``D`` is a constant within ``min_depth - 1`` hops, and
    * the chain length (from head constant to tail) is at least
      ``min_depth`` flops.

    ``clock_domains`` is the per-flop clock-domain map that the rule
    pack already builds in ``_RuleContext.domains`` — passed in to
    avoid re-running :func:`rtl_buddy_cdc.domain.assign_domains`. The
    recogniser tolerates ``None`` entries (untraceable clock); those
    flops never match because their domain cannot be compared.

    ``extra_synchronizers`` (optional) is the set of flop cell names
    the caller wants treated as sync stages regardless of structural
    shape — typically the result of
    :func:`rtl_buddy_cdc.rules.user_reset_sync_flop_names` (flops the
    user has marked with ``(* reset_sync *)``). Useful when the chain
    head's D is fed by an upstream signal rather than a literal
    constant — the structural recogniser is deliberately conservative
    and would otherwise miss them.

    ``reset_domains`` / ``flops`` / ``bit_drivers`` (optional, issue
    #324) are the :func:`assign_reset_domains` result,
    ``find_flops(module)`` and the bit-driver map, for a caller that
    already holds them; each is built from ``module`` when omitted.
    """
    if min_depth < 1:
        raise ValueError("min_depth must be >= 1")
    if bit_drivers is None:
        bit_drivers = _bit_drivers(module)
    if flops is None:
        flops = find_flops(module)
    flops = tuple(flops)
    if reset_domains is None:
        reset_domains = assign_reset_domains(module, flops=flops, drivers=bit_drivers)
    domains = reset_domains
    flop_by_name = {f.cell.name: f for f in flops}
    out: set[str] = set(extra_synchronizers or ())

    for name, rd in domains.items():
        if rd.reset is None or rd.reset.type != "async":
            continue
        chain, _head_d = _trace_reset_sync_chain(
            name,
            flop_by_name,
            domains,
            clock_domains,
            bit_drivers,
            max_depth=min_depth + 4,
        )
        if len(chain) >= min_depth:
            out.update(chain)
    return out


def iter_reset_sync_chains(
    module: Module,
    clock_domains: dict[str, str | None],
    *,
    min_depth: int = 2,
    reset_domains: dict[str, ResetDomain] | None = None,
    flops: Iterable[Flop] | None = None,
    bit_drivers: dict[Bit, tuple[str, str]] | None = None,
) -> list[ResetSyncChain]:
    """Enumerate structurally-recognised reset-synchroniser chains.

    Companion to :func:`find_reset_synchronizers`: instead of
    flattening every chain into a set of cell names, returns one
    :class:`ResetSyncChain` per *distinct* chain head — so a rule that
    needs the head's D constant (e.g. RDC-007) can read it without
    walking the netlist twice.

    Two chains are considered the same when their head flop is the
    same cell; the canonical chain is the one whose tail (downstream-
    consumer-facing end) is the longest walk from the head.

    User-marked ``(* reset_sync *)`` flops are deliberately *not*
    included here: the recogniser would have no head D-constant to
    return for them (their head may be fed by an upstream signal
    rather than a literal). RDC-007 already documents that as the
    known limitation.

    ``reset_domains`` / ``flops`` / ``bit_drivers`` are optional
    precomputed inputs, as for :func:`find_reset_synchronizers`.
    """
    if bit_drivers is None:
        bit_drivers = _bit_drivers(module)
    if flops is None:
        flops = find_flops(module)
    flops = tuple(flops)
    if reset_domains is None:
        reset_domains = assign_reset_domains(module, flops=flops, drivers=bit_drivers)
    domains = reset_domains
    flop_by_name = {f.cell.name: f for f in flops}

    best_by_head: dict[str, ResetSyncChain] = {}
    for name, rd in domains.items():
        if rd.reset is None or rd.reset.type != "async":
            continue
        chain, head_d = _trace_reset_sync_chain(
            name,
            flop_by_name,
            domains,
            clock_domains,
            bit_drivers,
            max_depth=min_depth + 4,
        )
        if len(chain) < min_depth or head_d is None:
            continue
        head_name = chain[-1]
        prev = best_by_head.get(head_name)
        if prev is None or len(chain) > len(prev.flops):
            best_by_head[head_name] = ResetSyncChain(
                flops=tuple(chain),
                head_d_constant=head_d,
                polarity=rd.reset.polarity,
            )
    return sorted(best_by_head.values(), key=lambda c: c.flops[0])


def _trace_reset_sync_chain(
    start: str,
    flop_by_name: dict[str, Flop],
    reset_domains: dict[str, ResetDomain],
    clock_domains: dict[str, str | None],
    bit_drivers: dict[Bit, tuple[str, str]],
    *,
    max_depth: int,
) -> tuple[list[str], str | None]:
    """Walk Q→D back from ``start``; return the chain iff its head is constant-fed.

    Returns ``(chain, head_d_constant)``. The chain includes ``start``
    (as the tail) and every intermediate flop up to the constant-fed
    head. ``head_d_constant`` is the Yosys-encoded D-pin constant on
    the head flop (``"0"`` / ``"1"`` / ``"x"`` / ``"z"`` or a
    multi-bit binary string).

    If the walk terminates on anything other than a constant — a
    foreign-domain flop, a port, a multi-bit ``D``, a combinational
    signal, depth exhaustion — the chain is **not** a reset
    synchroniser and ``([], None)`` is returned. This is the
    load-bearing distinction: without it, any 2-flop ARST-sharing
    chain in the same clock domain (including data-path register
    chains that happen to share a reset) would be mis-identified as a
    synchroniser."""
    chain = [start]
    cur_name = start
    cur_rd = reset_domains[cur_name]
    cur_clk = clock_domains.get(cur_name)
    if cur_clk is None or cur_rd.reset is None:
        return ([], None)
    reset_signature = (cur_rd.reset.name, cur_rd.reset.source)

    for _ in range(max_depth):
        cur_flop = flop_by_name[cur_name]
        d_bits = cur_flop.d
        if len(d_bits) != 1:
            return ([], None)  # Multi-bit chains aren't the reset-sync shape.
        d_bit = d_bits[0]
        if not isinstance(d_bit, int):
            # D is a constant — chain head found. Return the chain as
            # collected so far plus the literal constant; the constant
            # itself isn't a flop.
            return (chain, str(d_bit))
        drv = bit_drivers.get(d_bit)
        if drv is None:
            return (
                [],
                None,
            )  # Port- or constant-net-fed D isn't the recogniser's shape.
        drv_name, drv_port = drv
        if drv_port != "Q" or drv_name not in flop_by_name:
            return ([], None)
        drv_rd = reset_domains.get(drv_name)
        drv_clk = clock_domains.get(drv_name)
        if drv_rd is None or drv_rd.reset is None or drv_clk is None:
            return ([], None)
        if drv_clk != cur_clk:
            return ([], None)
        if (drv_rd.reset.name, drv_rd.reset.source) != reset_signature:
            return ([], None)
        chain.append(drv_name)
        cur_name = drv_name
    return ([], None)


ResetCrossingKind = Literal[
    "async-deassert",
    "polarity-mismatch",
    "sync-crossing",
    "comb-driven",
]


@dataclass(frozen=True)
class ResetCrossing:
    """A single problematic reset arrival at a flop.

    Unified record consumed by external tooling (e.g. ``rtl-buddy-view``)
    that wants the reset-domain view without re-running each RDC rule
    individually. The rule pack still owns the *reporting* surface
    (rule-id, severity, grouped messages); :func:`find_reset_crossings`
    just emits the structural facts.

    Fields:

    * ``flop`` — destination flop cell name.
    * ``reset`` — the upstream reset source classification (from
      :func:`assign_reset_domains`).
    * ``flop_clock`` — the clock domain that samples ``flop``'s ``D``
      (i.e. the value passed in via ``clock_domains[flop]``). ``None``
      when the flop's clock isn't traceable.
    * ``kind`` — what about the crossing is worth flagging; see
      :data:`ResetCrossingKind`.
    * ``pin`` — the flop pin the reset arrives on, by its coarse name:
      ``"ARST"``, ``"ALOAD"``, ``"CLR"``, ``"SET"`` or ``"SRST"`` (gate
      level ``R`` / ``L`` / ``S`` read as the pin they stand for). A set /
      clear flop can have one record per pin (rtl-buddy-cdc#390).
      ``None`` only on a record built by hand without it.
    """

    flop: str
    reset: ResetSource
    flop_clock: str | None
    kind: ResetCrossingKind
    pin: str | None = None


#: Coarse name of each reset role, for :attr:`ResetCrossing.pin`.
PIN_LABELS: dict[str, str] = {
    "arst": "ARST",
    "aload": "ALOAD",
    "clr": "CLR",
    "set": "SET",
    "srst": "SRST",
}


def _reset_arrivals(rd: ResetDomain) -> list[tuple[str, ResetSource]]:
    """Every reset pin of ``rd``'s flop with the source on it: each
    asynchronous control (``ResetDomain.controls``, in priority order),
    then a synchronous reset (``ResetDomain.reset`` when it is ``SRST``)."""
    out = [(PIN_LABELS[c.pin.role], c.source) for c in rd.controls]
    if rd.reset is not None and rd.reset.type == "sync":
        out.append((PIN_LABELS["srst"], rd.reset))
    return out


def find_reset_crossings(
    module: Module,
    clock_domains: dict[str, str | None],
    *,
    recognised_syncs: set[str] | None = None,
    polarity_overrides: dict[str, ResetPolarity] | None = None,
    reset_domains: dict[str, ResetDomain] | None = None,
) -> list[ResetCrossing]:
    """Unified view of every reset arrival worth flagging in ``module``.

    Iterates :func:`assign_reset_domains` and emits one
    :class:`ResetCrossing` per reset pin whose source would draw a rule
    finding, naming the pin (:attr:`ResetCrossing.pin`). Each kind reads
    the pins its rule reads: ``async-deassert`` and ``comb-driven`` every
    asynchronous control (``ResetDomain.controls`` — a set / clear flop's
    ``SET`` as well as its ``CLR``, rtl-buddy-cdc#390), ``sync-crossing``
    the ``SRST`` pin, ``polarity-mismatch`` ``ResetDomain.reset`` (the one
    pin RDC-002 reads). The kinds produced parallel the RDC rule family:

    * ``"async-deassert"`` — an async reset whose source domain differs
      from the consumer flop's clock domain and no reset-synchroniser
      stage is present (RDC-001's structural shape).
    * ``"polarity-mismatch"`` — a port-level ``(* reset_polarity *)``
      override declares a polarity that disagrees with the flop's
      inferred reset-pin polarity (RDC-002's port-declared variant).
      Only emitted when ``polarity_overrides`` is supplied.
    * ``"sync-crossing"`` — a sync reset whose source flop is in a
      different clock domain from the consumer (RDC-003's shape).
    * ``"comb-driven"`` — the reset is driven by combinational logic
      (RDC-004's shape).

    ``recognised_syncs`` lists flop cell names that the analyzer has
    already classified as reset-synchroniser members (the union of
    :func:`find_reset_synchronizers`'s output and any user-marked
    flops); those flops are skipped — by construction they bridge the
    crossing safely.

    ``polarity_overrides`` maps top-level reset *port* names to the
    declared polarity (``"high"`` / ``"low"``). When a flop's reset
    source is one of these ports and the flop's inferred polarity
    disagrees with the declaration, a ``"polarity-mismatch"`` crossing
    is emitted in addition to any crossing-kind that would also fire.

    This function is intentionally additive: the existing RDC-001..-005
    rule checks keep their own walks. ``find_reset_crossings`` is the
    public surface for *consumers* of the analyzer (downstream tooling
    and tests that want to enumerate the reset-domain facts directly).

    ``reset_domains`` (optional) is a precomputed
    :func:`assign_reset_domains` result; built from ``module`` when
    omitted.
    """
    out: list[ResetCrossing] = []
    syncs = recognised_syncs or set()
    domains = (
        reset_domains if reset_domains is not None else assign_reset_domains(module)
    )

    for name, rd in domains.items():
        if name in syncs:
            continue
        flop_clock = clock_domains.get(name)
        for pin, rsrc in _reset_arrivals(rd):
            # comb-driven: structural reset shape is combinational; clock
            # domain comparison is moot here — the issue is upstream of
            # any crossing question.
            if rsrc.source == "comb":
                out.append(
                    ResetCrossing(
                        flop=name,
                        reset=rsrc,
                        flop_clock=flop_clock,
                        kind="comb-driven",
                        pin=pin,
                    )
                )
                continue

            # Domain-crossing kinds. A source flop in a different clock
            # domain (inferred reset source) is the shape that drives
            # RDC-001 (async) and RDC-003 (sync).
            if rsrc.source == "inferred":
                src_clock = clock_domains.get(rsrc.name)
                if (
                    flop_clock is not None
                    and src_clock is not None
                    and flop_clock != src_clock
                ):
                    kind: ResetCrossingKind = (
                        "async-deassert" if rsrc.type == "async" else "sync-crossing"
                    )
                    out.append(
                        ResetCrossing(
                            flop=name,
                            reset=rsrc,
                            flop_clock=flop_clock,
                            kind=kind,
                            pin=pin,
                        )
                    )

        # Port-declared polarity override disagrees with the flop's
        # inferred polarity. Emitted independently of any
        # crossing-kind above; a single flop can be both an
        # async-deassert and a polarity-mismatch crossing. RDC-002's
        # pin: ``ResetDomain.reset`` only.
        rsrc_main = rd.reset
        if (
            polarity_overrides
            and rsrc_main is not None
            and rsrc_main.source == "port"
            and rsrc_main.name in polarity_overrides
            and polarity_overrides[rsrc_main.name] != rsrc_main.polarity
        ):
            out.append(
                ResetCrossing(
                    flop=name,
                    reset=rsrc_main,
                    flop_clock=flop_clock,
                    kind="polarity-mismatch",
                    pin=_reset_arrivals(rd)[0][0],
                )
            )

    return out
