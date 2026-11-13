"""Regroup bit-blasted gate-level flops into their source registers.

After ``techmap`` / ``abc`` a W-bit register is W one-bit ``$_DFF_*``
cells (rtl-buddy-cdc#337). Every rule that reasons about a *bus* — CDC-004
/ 012 / 020, the gated- and gray-bus acceptances, CDC-014's width guard —
keys on one flop cell carrying the whole bus, as ``proc`` leaves it. On a
gate-level netlist those rules either go silent (no flop is wider than one
bit) or report one finding per bit.

The bus is not lost, though: the register's name survives as a multi-bit
*netname* over the flops' ``Q`` bits. :func:`regroup_flops` uses it to
rebuild one wide coarse flop cell (``$dff`` / ``$adff`` / ``$dffe`` / …,
exactly what ``proc`` would have produced) per register, so the whole
analyzer — crossing enumeration, abstraction, the rule pack — sees the
same shape on a gate-level netlist as on an RTL one. The loader runs it
on every module (:func:`rtl_buddy_cdc.netlist.load_with_blackboxes`).

What is grouped
~~~~~~~~~~~~~~~

A *candidate* is a gate-level flop cell (decoded by
:func:`~rtl_buddy_cdc.cells.parse_gate_storage_cell`) with a one-bit
``D`` / ``Q`` and a net on ``Q``. Each candidate gets at most one
*owner* netname, chosen among the public (not ``$``-prefixed) netnames
of at least two bits that contain its ``Q`` bit:

- A **port** netname is passed over when the bit has any other public
  name, one-bit names included. Ports are usually wiring: in
  ``assign sync_out = {d3_s, d2_s, d1_s, d0_s};`` the four bits are four
  one-bit registers, and in ``assign dst_data = sync_q;`` the register
  is ``sync_q``. A register that *is* the port (``output logic [7:0] q``
  assigned in an ``always_ff``) has no other name and keeps it.
- Among the rest, the **narrowest** wins (then the name, for
  determinism), so a wire that concatenates two registers
  (``assign bus = {a_q, b_q};``) does not merge them: ``a_q`` and
  ``b_q`` own their own bits.
- One-bit names never own (a one-bit group is no group), so a register
  whose bits are also aliased one by one (``assign d0 = data[0];``)
  still groups as ``data``.

Candidates with the same owner are then split by *control key* —
family, clock net and edge, reset kind / net / polarity, set / load
net and polarity, enable net and polarity (all decoded from the cell
type and pins, so they mean the same as :mod:`rtl_buddy_cdc.flops`
reads them) — and by the cell's ``src`` attribute, which is the
``always`` block the register was assigned in: bits of one register
share it. The reset *value* is not part of the key: a register reset
to ``8'hA5`` is a mix of ``$_DFF_PN0_`` and ``$_DFF_PN1_`` bits and is
still one register. A split with at least two lanes becomes one group;
its lanes are ordered by their position in the owner netname (LSB
first, the order ``proc`` gives ``Q``).

Combinational lanes
~~~~~~~~~~~~~~~~~~~

The logic in front of the register is bit-blasted too: an enable or
sync-reset ``$mux`` becomes W ``$_MUX_`` cells, the gray encoder's
``$xor`` W ``$_XOR_`` cells. Starting from each regrouped register's
``D`` vector, a vector whose every bit is the ``Y`` of a distinct
one-bit gate of the same type (``$_MUX_`` with one shared ``S``,
``$_AND_`` / ``$_OR_`` / ``$_XOR_`` / ``$_XNOR_`` / ``$_NOT_`` /
``$_BUF_``) is rebuilt as the one wide coarse cell it is equivalent to
(``$mux`` / ``$and`` / … / ``$pos``), and the walk continues into that
cell's input vectors. The rebuilt cell computes exactly what its lanes
did, on the same nets, so this only changes which cell a rule sees.
Anything else ends the walk: mixed gate types (``abc`` output), a
select that differs between lanes, a lane that is not a gate output.

What is not
~~~~~~~~~~~

- A flop whose ``Q`` is in no qualifying netname — synthesis renamed or
  merged the register, or it was one bit wide — stays a one-bit cell,
  and the rules see W one-bit crossings. One that is not a declared
  scalar register either (no one-bit public name of its own) is tagged
  :data:`ORPHAN_ATTR`: CDC-004 then reports orphans of one ``always``
  block that cross together as a possible unprotected bus, so a bus
  whose bits are each properly synchronised does not go silent just
  because its name was lost.
- An owner netname that names the same flop at two positions (bits
  merged by ``opt_merge``) is not grouped at all.
- A multi-bit slice with its own name (``assign lo = q[3:0];``) wins
  ownership of its bits, so the register is split in two groups. That
  is the conservative failure: a split bus loses a bus-level acceptance
  (gray / gated) before it loses a finding.

The one shape the netlist cannot tell apart is a non-port vector wire
that concatenates one-bit registers assigned in the same ``always``
block with the same controls (``wire [3:0] w = {d3, d2, d1, d0};``):
it reads exactly like a register whose bits are aliased one by one, and
is grouped.

Coarse cells are never touched, so an RTL-level netlist goes through
unchanged.

Naming
~~~~~~

A group is named after its owner netname (``sync_q``) when it covers
every flop lane of that netname, else ``sync_q[3:0]`` / ``sync_q[0,2,5]``
(netname positions, LSB = 0). A clash with an existing cell name gets a
``$regroup`` suffix. The cell keeps the ``src`` attribute of its lanes
(one entry per distinct value, ``|``-joined as Yosys does) and records
the original cell names, space-separated, in :data:`REGROUPED_ATTR`.

Cost is linear in the size of the netlist: one pass over the cells and
one over the netnames' bits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from rtl_buddy_cdc.cells import GateStorageCell, parse_gate_storage_cell
from rtl_buddy_cdc.netlist import Bit, Cell, Module, Netname

# Attribute on a regrouped cell listing the gate-level cells it replaces.
REGROUPED_ATTR = "rtl_buddy_cdc_regrouped"
# Attribute on a gate-level flop that could not be regrouped and has no
# one-bit name of its own: a bit of a register whose name synthesis lost.
ORPHAN_ATTR = "rtl_buddy_cdc_unregrouped"

# Gate family + "has async reset R" -> coarse type. ``$_DFF_`` /
# ``$_DFFE_`` have a reset-less and an async-reset layout.
_COARSE_TYPE: dict[tuple[str, bool], str] = {
    ("$_DFF_", False): "$dff",
    ("$_DFF_", True): "$adff",
    ("$_DFFE_", False): "$dffe",
    ("$_DFFE_", True): "$adffe",
    ("$_SDFF_", True): "$sdff",
    ("$_SDFFE_", True): "$sdffe",
    ("$_SDFFCE_", True): "$sdffce",
    ("$_DFFSR_", True): "$dffsr",
    ("$_DFFSRE_", True): "$dffsre",
    ("$_ALDFF_", False): "$aldff",
    ("$_ALDFFE_", False): "$aldffe",
}


def _int32(value: int) -> str:
    return format(value, "032b")


@dataclass(frozen=True)
class _Lane:
    cell: Cell
    gate: GateStorageCell
    pos: int  # position of the Q bit in the owner netname


def _single(cell: Cell, pin: str) -> Bit | None:
    bits = cell.connections.get(pin, ())
    return bits[0] if len(bits) == 1 else None


def _control_key(cell: Cell, gate: GateStorageCell) -> tuple | None:
    """Everything two lanes of one register must share, or ``None`` when a
    control pin is not a single bit (malformed cell: never grouped)."""
    pins: list[Bit | None] = [_single(cell, "C")]
    if gate.reset_kind is not None:
        pins.append(_single(cell, "R"))
    if gate.set_polarity is not None:
        pins.append(_single(cell, "S"))
    if gate.aload_polarity is not None:
        pins.append(_single(cell, "L"))
    if gate.enable_polarity is not None:
        pins.append(_single(cell, "E"))
    if any(p is None for p in pins):
        return None
    return (
        cell.attributes.get("src"),
        gate.family,
        gate.clk_polarity,
        gate.reset_kind,
        gate.reset_polarity,
        gate.set_polarity,
        gate.aload_polarity,
        gate.enable_polarity,
        tuple(pins),
    )


def _candidates(module: Module) -> dict[int, tuple[Cell, GateStorageCell]]:
    """``Q`` net -> (gate flop cell, decoded type) for every groupable flop."""
    out: dict[int, tuple[Cell, GateStorageCell]] = {}
    contested: set[int] = set()
    for cell in module.cells.values():
        if not cell.type.startswith("$_"):
            continue
        gate = parse_gate_storage_cell(cell.type)
        if gate is None or not gate.is_flop:
            continue
        if (gate.family, gate.reset_kind is not None) not in _COARSE_TYPE:
            continue
        q = cell.connections.get("Q", ())
        d = cell.connections.get("D", ())
        if len(q) != 1 or len(d) != 1 or not isinstance(q[0], int):
            continue
        if q[0] in out:
            contested.add(q[0])  # two drivers on one net: malformed
            continue
        out[q[0]] = (cell, gate)
    for q_bit in contested:
        del out[q_bit]  # leave both alone
    return out


def _is_public(nn: Netname) -> bool:
    # Yosys hides exactly the ``$``-prefixed names (``hide_name``).
    return not nn.name.startswith("$")


# A one-bit netname that is one bit of a split vector: ``q[3]``.
_SPLIT_BIT = re.compile(r"^(.+)\[(\d+)\]$")


def _split_vectors(
    public: list[Netname],
    netnames: dict[str, Netname],
    port_names: frozenset[str],
) -> tuple[list[Netname], set[str]]:
    """Vectors rebuilt from one-bit netnames ``base[i]`` — what
    ``splitnets`` leaves of a register's name — when no netname ``base``
    exists. Returns them (bits ordered by index, missing indices as
    ``"x"``) and the names of those whose parts were ports."""
    parts: dict[str, dict[int, Bit]] = {}
    ports: set[str] = set()
    for nn in public:
        if len(nn.bits) != 1:
            continue
        m = _SPLIT_BIT.match(nn.name)
        if m is None or m.group(1) in netnames:
            continue
        base, idx = m.group(1), int(m.group(2))
        parts.setdefault(base, {}).setdefault(idx, nn.bits[0])
        if nn.name in port_names:
            ports.add(base)
    out: list[Netname] = []
    for base, lanes in sorted(parts.items()):
        lo, hi = min(lanes), max(lanes)
        # Sparse index sets (``x[0]`` and ``x[100000]``) are not a vector.
        if len(lanes) < 2 or hi - lo + 1 > 2 * len(lanes):
            continue
        bits = tuple(lanes.get(i, "x") for i in range(lo, hi + 1))
        out.append(Netname(name=base, bits=bits))
    return out, ports


def _src_of(lanes: list[_Lane]) -> str | None:
    return _src_of_cells([lane.cell for lane in lanes])


def _src_of_cells(cells: list[Cell]) -> str | None:
    seen: list[str] = []
    for cell in cells:
        src = cell.attributes.get("src")
        if src and src not in seen:
            seen.append(src)
    return "|".join(seen) if seen else None


def _coarse_cell(name: str, lanes: list[_Lane]) -> Cell:
    """The coarse flop cell ``proc`` would have produced for ``lanes``."""
    first, gate = lanes[0].cell, lanes[0].gate
    width = len(lanes)
    ctype = _COARSE_TYPE[(gate.family, gate.reset_kind is not None)]

    def bus(pin: str) -> tuple[Bit, ...]:
        return tuple(lane.cell.connections[pin][0] for lane in lanes)

    def shared(pin: str) -> tuple[Bit, ...]:
        return tuple(first.connections[pin])

    conns: dict[str, tuple[Bit, ...]] = {
        "CLK": shared("C"),
        "D": bus("D"),
        "Q": bus("Q"),
    }
    params: dict[str, str] = {
        "CLK_POLARITY": _int32(gate.clk_polarity or 0),
        "WIDTH": _int32(width),
    }
    # Coarse value parameters are MSB first.
    values = "".join(str(lane.gate.reset_value) for lane in reversed(lanes))
    if gate.family in ("$_DFFSR_", "$_DFFSRE_"):
        conns["SET"] = shared("S") * width
        conns["CLR"] = shared("R") * width
        params["SET_POLARITY"] = _int32(gate.set_polarity or 0)
        params["CLR_POLARITY"] = _int32(gate.reset_polarity or 0)
    elif gate.reset_kind == "async":
        conns["ARST"] = shared("R")
        params["ARST_POLARITY"] = _int32(gate.reset_polarity or 0)
        params["ARST_VALUE"] = values
    elif gate.reset_kind == "sync":
        conns["SRST"] = shared("R")
        params["SRST_POLARITY"] = _int32(gate.reset_polarity or 0)
        params["SRST_VALUE"] = values
    if gate.aload_polarity is not None:
        conns["ALOAD"] = shared("L")
        conns["AD"] = (
            bus("AD") if all("AD" in lane.cell.connections for lane in lanes) else ()
        )
        params["ALOAD_POLARITY"] = _int32(gate.aload_polarity)
    if gate.enable_polarity is not None:
        conns["EN"] = shared("E")
        params["EN_POLARITY"] = _int32(gate.enable_polarity)

    attrs: dict[str, str] = {}
    src = _src_of(lanes)
    if src is not None:
        attrs["src"] = src
    attrs[REGROUPED_ATTR] = " ".join(lane.cell.name for lane in lanes)
    return Cell(
        name=name, type=ctype, connections=conns, parameters=params, attributes=attrs
    )


def _group_name(net: str, positions: list[int], whole: bool) -> str:
    if whole:
        return net
    lo, hi = positions[0], positions[-1]
    if hi - lo + 1 == len(positions):
        return f"{net}[{hi}:{lo}]"
    return f"{net}[{','.join(str(p) for p in positions)}]"


def regroup_flops(module: Module) -> Module:
    """Return ``module`` with its bit-blasted gate flops regrouped into
    wide coarse flop cells (see the module docstring); ``module`` itself
    when there is nothing to regroup."""
    cands = _candidates(module)
    if not cands:
        return module
    port_names = frozenset(module.ports)

    # 1. Owner netname per candidate Q bit: two passes over netname bits.
    public = [nn for nn in module.netnames.values() if _is_public(nn)]
    has_nonport_name: set[int] = set()
    for nn in public:
        if nn.name not in port_names:
            has_nonport_name.update(b for b in nn.bits if isinstance(b, int))
    split, split_ports = _split_vectors(public, module.netnames, port_names)
    vectors = {nn.name: nn for nn in public if len(nn.bits) >= 2}
    vectors.update((nn.name, nn) for nn in split)
    owner: dict[int, Netname] = {}
    for nn in vectors.values():
        is_port = nn.name in port_names or nn.name in split_ports
        rank = (len(nn.bits), nn.name)
        for b in nn.bits:
            if not isinstance(b, int) or b not in cands:
                continue
            if is_port and b in has_nonport_name:
                continue
            cur = owner.get(b)
            if cur is None or rank < (len(cur.bits), cur.name):
                owner[b] = nn

    # 2. Lanes per owner, refusing an owner that names one net twice.
    by_owner: dict[str, list[_Lane]] = {}
    for nn_name in sorted({nn.name for nn in owner.values()}):
        nn = vectors[nn_name]
        lanes: list[_Lane] = []
        seen: set[int] = set()
        dup = False
        for pos, b in enumerate(nn.bits):
            if not isinstance(b, int) or owner.get(b) is not nn:
                continue
            if b in seen:
                dup = True
                break
            seen.add(b)
            cell, gate = cands[b]
            lanes.append(_Lane(cell, gate, pos))
        if not dup and len(lanes) >= 2:
            by_owner[nn_name] = lanes

    # 3. Split each owner's lanes by control key; build the wide cells.
    new_cells: dict[str, Cell] = {}
    replaced: set[str] = set()
    for net, lanes in by_owner.items():
        splits: dict[tuple, list[_Lane]] = {}
        for lane in lanes:
            key = _control_key(lane.cell, lane.gate)
            if key is not None:
                splits.setdefault(key, []).append(lane)
        groups = [g for g in splits.values() if len(g) >= 2]
        whole = len(groups) == 1 and len(groups[0]) == len(lanes)
        for group in groups:
            name = _group_name(net, [lane.pos for lane in group], whole)
            while name in module.cells or name in new_cells:
                name += "$regroup"
            new_cells[name] = _coarse_cell(name, group)
            replaced.update(lane.cell.name for lane in group)

    # 4. Orphans: candidates left one bit wide that are not a declared
    # scalar register either (no one-bit name of their own) — synthesis
    # dropped or merged the register's name. Tagged so CDC-004 can stay
    # conservative about them (see ``rules._orphan_bus_violations``).
    scalar: set[int] = set()
    for nn in public:
        if len(nn.bits) == 1 and isinstance(nn.bits[0], int):
            if _SPLIT_BIT.match(nn.name) is None:
                scalar.add(nn.bits[0])
    orphans = {
        cell.name: replace(cell, attributes={**cell.attributes, ORPHAN_ATTR: "1"})
        for q, (cell, _gate) in cands.items()
        if cell.name not in replaced and q not in scalar
    }
    if not new_cells and not orphans:
        return module
    cells = {n: c for n, c in module.cells.items() if n not in replaced}
    cells.update(orphans)
    cells.update(new_cells)
    if new_cells:
        _merge_comb_lanes(cells, [c.connections["D"] for c in new_cells.values()])
    return replace(module, cells=cells)


# One-bit gate -> (coarse type, input pins). ``$_MUX_`` additionally needs
# one shared ``S``; the coarse cell is lane-for-lane the same function.
_COMB_COARSE: dict[str, tuple[str, tuple[str, ...]]] = {
    "$_MUX_": ("$mux", ("A", "B")),
    "$_AND_": ("$and", ("A", "B")),
    "$_OR_": ("$or", ("A", "B")),
    "$_XOR_": ("$xor", ("A", "B")),
    "$_XNOR_": ("$xnor", ("A", "B")),
    "$_NOT_": ("$not", ("A",)),
    "$_BUF_": ("$pos", ("A",)),
}


def _merge_comb_lanes(cells: dict[str, Cell], roots: list[tuple[Bit, ...]]) -> None:
    """Rebuild bit-blasted combinational lanes in front of the regrouped
    registers as wide coarse cells, in place (see the module docstring).

    ``roots`` are the regrouped registers' ``D`` vectors. Each one-bit
    gate is merged at most once, so the walk is linear in the cone it
    visits.
    """
    gate_by_y: dict[int, Cell] = {}
    for cell in cells.values():
        if cell.type in _COMB_COARSE:
            y = cell.connections.get("Y", ())
            if len(y) == 1 and isinstance(y[0], int):
                gate_by_y[y[0]] = cell
    if not gate_by_y:
        return
    merged: set[str] = set()
    work = list(roots)
    while work:
        vec = work.pop()
        if len(vec) < 2:
            continue
        lanes: list[Cell] = []
        for b in vec:
            lane_cell = gate_by_y.get(b) if isinstance(b, int) else None
            if lane_cell is None or lane_cell.name in merged:
                break
            lanes.append(lane_cell)
        else:
            new = _coarse_comb(lanes)
            if new is None:
                continue
            for lane in lanes:
                merged.add(lane.name)
                del cells[lane.name]
            name = new.name
            while name in cells:
                name += "$regroup"
            cells[name] = replace(new, name=name)
            _ctype, ins = _COMB_COARSE[lanes[0].type]
            work.extend(new.connections[pin] for pin in ins)


def _coarse_comb(lanes: list[Cell]) -> Cell | None:
    """The one wide coarse cell equivalent to one-bit gate ``lanes``, or
    ``None`` when they are not one bit-blasted cell (mixed types, a
    repeated cell, differing ``$_MUX_`` selects, malformed pins)."""
    gtype = lanes[0].type
    if any(lane.type != gtype for lane in lanes):
        return None
    if len({lane.name for lane in lanes}) != len(lanes):
        return None
    ctype, ins = _COMB_COARSE[gtype]
    conns: dict[str, tuple[Bit, ...]] = {}
    for pin in (*ins, "Y"):
        col: list[Bit] = []
        for lane in lanes:
            bits = lane.connections.get(pin, ())
            if len(bits) != 1:
                return None
            col.append(bits[0])
        conns[pin] = tuple(col)
    width = len(lanes)
    params: dict[str, str] = {}
    if gtype == "$_MUX_":
        selects = {lane.connections.get("S", ()) for lane in lanes}
        if len(selects) != 1:
            return None
        s_bits = next(iter(selects))
        if len(s_bits) != 1:
            return None
        conns["S"] = tuple(s_bits)
        params["WIDTH"] = _int32(width)
    else:
        for pin in ins:
            params[f"{pin}_SIGNED"] = _int32(0)
            params[f"{pin}_WIDTH"] = _int32(width)
        params["Y_WIDTH"] = _int32(width)
    attrs: dict[str, str] = {}
    src = _src_of_cells(lanes)
    if src is not None:
        attrs["src"] = src
    attrs[REGROUPED_ATTR] = " ".join(lane.name for lane in lanes)
    return Cell(
        name=f"{lanes[0].name}$regroup",
        type=ctype,
        connections=conns,
        parameters=params,
        attributes=attrs,
    )
