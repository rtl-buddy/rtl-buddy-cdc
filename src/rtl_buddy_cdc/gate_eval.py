"""Truth tables of small combinational cones (rtl-buddy-cdc#337).

Synthesis (``abc``) re-derives logic freely, so a structural pattern the
RTL carried — the gray encoder's ``b ^ (b >> 1)`` ``$xor`` — may survive
a gate-level flow only as *some* network of ``$_AND_`` / ``$_XOR_`` /
``$_NOT_`` … computing the same function. When the cone is small, the
function itself can be compared: :func:`cone_tables` evaluates a set of
nets over the leaves of their combined backward cone (flop ``Q`` bits,
ports, undriven nets), all leaf assignments at once, one Python ``int``
bit per assignment.

Only cells with a known lane-wise Boolean function are walked: the
one-bit gate library and the coarse bitwise cells whose output bit ``i``
depends only on input bit ``i``. Any other cell in the cone — or a cone
over budget, or an ``x`` / ``z`` constant — makes the answer ``None``,
which callers treat as "not recognised".
"""

from __future__ import annotations

from functools import cache

from rtl_buddy_cdc.netlist import Bit, Module

Drivers = dict[Bit, tuple[str, str, int]]

# Lane-wise cells: output bit i is f(input bits i). Coarse cells qualify
# only when every pin is as wide as Y (no extension), checked below.
_LANE_INPUTS: dict[str, tuple[str, ...]] = {
    "$_BUF_": ("A",),
    "$_NOT_": ("A",),
    "$_AND_": ("A", "B"),
    "$_NAND_": ("A", "B"),
    "$_OR_": ("A", "B"),
    "$_NOR_": ("A", "B"),
    "$_XOR_": ("A", "B"),
    "$_XNOR_": ("A", "B"),
    "$_ANDNOT_": ("A", "B"),
    "$_ORNOT_": ("A", "B"),
    "$_MUX_": ("A", "B", "S"),
    "$_NMUX_": ("A", "B", "S"),
    "$_AOI3_": ("A", "B", "C"),
    "$_OAI3_": ("A", "B", "C"),
    "$_AOI4_": ("A", "B", "C", "D"),
    "$_OAI4_": ("A", "B", "C", "D"),
    "$pos": ("A",),
    "$not": ("A",),
    "$and": ("A", "B"),
    "$or": ("A", "B"),
    "$xor": ("A", "B"),
    "$xnor": ("A", "B"),
    "$mux": ("A", "B", "S"),
}


def _apply(ctype: str, v: list[int], mask: int) -> int:
    a = v[0]
    if ctype in ("$_BUF_", "$pos"):
        return a
    if ctype in ("$_NOT_", "$not"):
        return ~a & mask
    b = v[1]
    if ctype in ("$_AND_", "$and"):
        return a & b
    if ctype == "$_NAND_":
        return ~(a & b) & mask
    if ctype in ("$_OR_", "$or"):
        return a | b
    if ctype == "$_NOR_":
        return ~(a | b) & mask
    if ctype in ("$_XOR_", "$xor"):
        return a ^ b
    if ctype in ("$_XNOR_", "$xnor"):
        return ~(a ^ b) & mask
    if ctype == "$_ANDNOT_":
        return a & ~b & mask
    if ctype == "$_ORNOT_":
        return (a | ~b) & mask
    c = v[2]
    if ctype in ("$_MUX_", "$mux"):
        return (c & b) | (~c & a & mask)
    if ctype == "$_NMUX_":
        return ~((c & b) | (~c & a)) & mask
    if ctype == "$_AOI3_":
        return ~((a & b) | c) & mask
    if ctype == "$_OAI3_":
        return ~((a | b) & c) & mask
    d = v[3]
    if ctype == "$_AOI4_":
        return ~((a & b) | (c & d)) & mask
    # $_OAI4_
    return ~((a | b) & (c | d)) & mask


@cache
def _var_pattern(k: int, n: int) -> int:
    """Leaf ``k`` of ``n``: bit ``j`` set iff bit ``k`` of ``j`` is."""
    half = 1 << k
    block = ((1 << half) - 1) << half
    period = half * 2
    out = 0
    for p in range((1 << n) // period):
        out |= block << (p * period)
    return out


def cone_tables(
    module: Module,
    roots: list[tuple[Bit, ...]],
    drivers: Drivers,
    *,
    max_leaves: int = 10,
    max_cells: int = 256,
) -> list[tuple[int, ...]] | None:
    """Truth tables of every bit in ``roots``, over the leaves of their
    combined backward cone, or ``None`` (see the module docstring).

    Two nets get equal tables iff they compute the same function of the
    cone's leaves. Tables are only comparable within one call.
    """
    # 1. Collect the cone: cells to evaluate and leaves, iteratively.
    leaves: dict[int, int] = {}
    cone: dict[str, tuple[str, tuple[str, ...]]] = {}
    stack: list[Bit] = [b for vec in roots for b in vec]
    seen: set[int] = set()
    while stack:
        bit = stack.pop()
        if not isinstance(bit, int):
            if bit not in ("0", "1"):
                return None
            continue
        if bit in seen:
            continue
        seen.add(bit)
        drv = drivers.get(bit)
        if drv is None or drv[1] != "Y":
            # Flop Q, port, undriven net: a free variable.
            if len(leaves) >= max_leaves:
                return None
            leaves[bit] = len(leaves)
            continue
        cell = module.cells[drv[0]]
        ins = _LANE_INPUTS.get(cell.type)
        if ins is None:
            return None
        if cell.name not in cone:
            if len(cone) >= max_cells:
                return None
            width = len(cell.connections.get("Y", ()))
            if not cell.type.startswith("$_"):
                pins = ins if cell.type != "$mux" else ("A", "B")
                if any(len(cell.connections.get(p, ())) != width for p in pins):
                    return None
                if cell.type == "$mux" and len(cell.connections.get("S", ())) != 1:
                    return None
            cone[cell.name] = (cell.type, ins)
        idx = drv[2]
        for pin in ins:
            pbits = cell.connections.get(pin, ())
            stack.append(pbits[0] if pin == "S" else pbits[idx])

    # 2. Evaluate, memoised per net.
    n = len(leaves)
    mask = (1 << (1 << n)) - 1
    value: dict[Bit, int] = {"0": 0, "1": mask}
    for bit, k in leaves.items():
        value[bit] = _var_pattern(k, n)

    def inputs(bit: Bit) -> tuple[str, list[Bit]]:
        name, _port, idx = drivers[bit]
        ctype, ins = cone[name]
        conns = module.cells[name].connections
        return ctype, [conns[p][0] if p == "S" else conns[p][idx] for p in ins]

    # Post-order over the cone; a net met again while still open is a
    # combinational loop, which has no truth table.
    for root in (b for vec in roots for b in vec):
        work: list[tuple[Bit, bool]] = [(root, False)]
        open_: set[Bit] = set()
        while work:
            bit, expanded = work.pop()
            if bit in value:
                continue
            ctype, in_bits = inputs(bit)
            if expanded:
                value[bit] = _apply(ctype, [value[b] for b in in_bits], mask)
                open_.discard(bit)
                continue
            if bit in open_:
                return None
            open_.add(bit)
            work.append((bit, True))
            work.extend((b, False) for b in in_bits if b not in value)

    return [tuple(value[b] for b in vec) for vec in roots]
