"""D-pin shape classifier for CDC-009 (pulse-width / fast-to-slow data-loss).

Given the D pin of a src-domain flop whose Q crosses to a slower dst
clock, classify the D pattern. The rule fires only on ``"pulse"`` —
the bias is deliberately false-negative over false-positive: a missed
pulse-width bug surfaces in silicon, where it can be investigated; a
noisy rule trains users to ignore it.

Note on the design proposal (#47 §2): the original proposal described
the detection target as a ``$dffe`` whose D feeds back from Q via a
``$mux`` keyed on EN. That shape actually produces a *latched* value on
Q (the flop holds whatever was captured when EN last pulsed), not a
single-cycle pulse on Q — so it wouldn't be a data-loss case in the
fast-to-slow sense. The textbook pulse-loss case (Cummings SNUG 2008
§4.5) is structurally a plain ``$dff`` (or ``$dffe`` with always-on
EN) whose D pin is an edge-detector output ``A & ~A_d`` — that's the
pattern this classifier matches.
"""

from __future__ import annotations

from typing import Literal

from rtl_buddy_cdc.flops import is_ff_cell
from rtl_buddy_cdc.netlist import Bit, Module

PulseShape = Literal["pulse", "other"]

# Cell types where ``Y = ~A``. After ``proc; flatten``, ``~x`` lands as
# one of these.
_NOT_TYPES: frozenset[str] = frozenset({"$not", "$logic_not", "$_NOT_"})

# Cell types where ``Y = A & B``. The edge-detector ``req & ~req_d``
# materialises as one of these with two single-bit inputs.
_AND_TYPES: frozenset[str] = frozenset({"$and", "$logic_and", "$_AND_"})


def classify_d_pin_shape(
    d_bit: Bit,
    src_clock: str,
    module: Module,
    bit_drivers: dict[Bit, tuple[str, str, int]],
    flop_domains: dict[str, str | None],
) -> PulseShape:
    """Classify the comb-cone driving a flop's D pin.

    Returns ``"pulse"`` iff ``d_bit`` is the ``Y`` of an ``$and`` cell
    whose two inputs are ``(F1.Q, ~F2.Q)`` where F1 and F2 are both
    src-domain flops and F2's D[0] equals F1.Q[0] (so F2 is F1's
    1-cycle delay). Anything else — including handshake / held-value
    patterns whose D is a priority-encoded ``$mux`` nest — returns
    ``"other"``.
    """
    if not isinstance(d_bit, int):
        return "other"
    if _matches_edge_detector(d_bit, src_clock, module, bit_drivers, flop_domains):
        return "pulse"
    return "other"


def _matches_edge_detector(
    d_bit: Bit,
    src_clock: str,
    module: Module,
    bit_drivers: dict[Bit, tuple[str, str, int]],
    flop_domains: dict[str, str | None],
) -> bool:
    drv = bit_drivers.get(d_bit)
    if drv is None:
        return False
    cell_name, port, _ = drv
    cell = module.cells.get(cell_name)
    if cell is None or cell.type not in _AND_TYPES or port != "Y":
        return False
    a_bits = cell.connections.get("A", ())
    b_bits = cell.connections.get("B", ())
    if len(a_bits) != 1 or len(b_bits) != 1:
        return False
    a_bit, b_bit = a_bits[0], b_bits[0]
    if not (isinstance(a_bit, int) and isinstance(b_bit, int)):
        return False
    # Try both orderings — Yosys may place the inverted operand on either pin.
    for direct, inverted in ((a_bit, b_bit), (b_bit, a_bit)):
        if _is_edge_detector_pair(
            direct, inverted, src_clock, module, bit_drivers, flop_domains
        ):
            return True
    return False


def _is_edge_detector_pair(
    direct_bit: Bit,
    inverted_bit: Bit,
    src_clock: str,
    module: Module,
    bit_drivers: dict[Bit, tuple[str, str, int]],
    flop_domains: dict[str, str | None],
) -> bool:
    direct_d = _src_flop_d_pin(direct_bit, src_clock, module, bit_drivers, flop_domains)
    if direct_d is None:
        return False
    drv = bit_drivers.get(inverted_bit)
    if drv is None:
        return False
    cell_name, port, _ = drv
    cell = module.cells.get(cell_name)
    if cell is None or cell.type not in _NOT_TYPES or port != "Y":
        return False
    not_a = cell.connections.get("A", ())
    if len(not_a) != 1 or not isinstance(not_a[0], int):
        return False
    delay_q = not_a[0]
    delay_d = _src_flop_d_pin(delay_q, src_clock, module, bit_drivers, flop_domains)
    if delay_d is None:
        return False
    # The delay flop's D must equal the direct flop's Q — that's what
    # makes it the 1-cycle delayed version of the same signal.
    return delay_d == direct_bit


ToggleShape = Literal["toggle", "other"]


def classify_toggle_d_pin(
    d_bit: Bit,
    src_q_bit: Bit,
    module: Module,
    bit_drivers: dict[Bit, tuple[str, str, int]],
) -> ToggleShape:
    """Classify whether ``d_bit`` is a toggle-with-enable pattern.

    Returns ``"toggle"`` iff ``d_bit`` is the ``Y`` of a ``$mux`` whose
    two data inputs (``A``, ``B``) are ``(src_q_bit, ~src_q_bit)`` in
    either order — where ``~src_q_bit`` is the ``Y`` of a cell in
    :data:`_NOT_TYPES` whose ``A`` is ``src_q_bit``. The mux's ``S``
    pin can be anything: the load-enable for the toggle.

    This is the structural shape of ``always_ff @(posedge clk)
    if (en) q <= ~q;`` after Yosys' ``proc`` pass — Yosys synthesises
    the conditional as ``D = en ? ~Q : Q`` (or the mirror), which
    materialises as the ``$mux(A=Q, B=~Q, S=en)`` cell shape.

    The classifier is shape-only; it does not look at the select pin
    and does not check the post-sync side of the crossing. CDC-013's
    rule body composes this with the fast-to-slow clock-ratio
    condition (parallel to how CDC-009 composes
    :func:`classify_d_pin_shape` with the same ratio check).

    Returns ``"other"`` for handshake / req-ack patterns whose ``D``
    is a priority-encoded ``$mux`` nest with non-``Q`` data inputs,
    for counter / pulse-stretcher patterns whose ``D`` is a reduction
    or adder, and for ``$dffe``-style enable flops whose ``D`` is just
    ``~Q`` (no mux — the enable is on a separate pin).
    """
    if not isinstance(d_bit, int) or not isinstance(src_q_bit, int):
        return "other"
    drv = bit_drivers.get(d_bit)
    if drv is None:
        return "other"
    cell_name, port, _ = drv
    cell = module.cells.get(cell_name)
    if cell is None or cell.type != "$mux" or port != "Y":
        return "other"
    a_bits = cell.connections.get("A", ())
    b_bits = cell.connections.get("B", ())
    if len(a_bits) != 1 or len(b_bits) != 1:
        return "other"
    a_bit, b_bit = a_bits[0], b_bits[0]
    if not (isinstance(a_bit, int) and isinstance(b_bit, int)):
        return "other"
    # Either ordering: A=Q,B=~Q or A=~Q,B=Q.
    for direct, inverted in ((a_bit, b_bit), (b_bit, a_bit)):
        if direct != src_q_bit:
            continue
        not_drv = bit_drivers.get(inverted)
        if not_drv is None:
            continue
        not_cell_name, not_port, _ = not_drv
        not_cell = module.cells.get(not_cell_name)
        if not_cell is None or not_cell.type not in _NOT_TYPES or not_port != "Y":
            continue
        not_a = not_cell.connections.get("A", ())
        if len(not_a) == 1 and not_a[0] == src_q_bit:
            return "toggle"
    return "other"


def _src_flop_d_pin(
    q_bit: Bit,
    src_clock: str,
    module: Module,
    bit_drivers: dict[Bit, tuple[str, str, int]],
    flop_domains: dict[str, str | None],
) -> Bit | None:
    """If ``q_bit`` is the Q output of a single-bit flop in
    ``src_clock``'s domain, return that flop's D[0]; otherwise None.
    """
    drv = bit_drivers.get(q_bit)
    if drv is None:
        return None
    cell_name, port, _ = drv
    if port != "Q":
        return None
    cell = module.cells.get(cell_name)
    if cell is None or not is_ff_cell(cell.type):
        return None
    if flop_domains.get(cell_name) != src_clock:
        return None
    d_bits = cell.connections.get("D", ())
    if len(d_bits) != 1:
        return None
    return d_bits[0]
