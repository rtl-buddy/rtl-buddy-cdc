"""Register-array memories (rtl-buddy-cdc#414).

A memory written in a block with an asynchronous reset is not a memory to
Yosys: ``mem2reg`` turns ``mem[waddr] <= wdata`` into one register per
word, each loaded through a decode of the write address, and the read
``mem[raddr]`` into a mux tree; the slang frontend does the same
(rtl-buddy-cdc#401). The same RTL with the write in a block of its own is
a memory cell, which the crossing walk does not cross (the storage path
is CDC-026's to judge, see :mod:`rtl_buddy_cdc.memory`). In the register
form every word is a crossing source of its own, and a read-side capture
of the word in several registers fires CDC-020 once per word: the
textbook async FIFO reported as sliced buses because of how its reset is
coded.

:func:`recognise` finds the register form and :func:`rewrite` turns it
back into a memory — one ``$memwr_v2`` write port, one asynchronous
``$memrd_v2`` read port — before the analysis, so both frontends' netlists
are judged as the memory they describe. It is deliberately narrow; a
register array is a memory here only when **all** of this holds, checked
on the netlist, not on names:

- ``DEPTH >= 2`` registers of one width, with the same cell type, clock
  and edge and no reset, set or load pin (an array reset in its reset
  branch is registers, not a memory), named as elements of one unpacked
  array (``mem[0]`` …, the netnames both frontends give them);
- each register's next state is either its own value (hold) or one write
  data bus shared by all of them, chosen by multiplexers (or an enable
  pin) whose selects depend on a small set of control nets; evaluated
  exhaustively over every value of those nets, **at most one** register
  loads in any cycle and every register loads in some — a decode of one
  write address;
- the registers' outputs are read by nothing but their own hold logic and
  one tree of multiplexers (``$mux`` / ``$pmux`` / ``$bmux`` /
  ``$shiftx`` / ``$_MUX_``) whose selects again depend on a small set of
  control nets; evaluated over every value of those, the tree's output is
  always all of one register's bits (or undefined), each bit in a fixed
  position — one mux on one read address.

Anything else — a register read elsewhere, a write of anything but the
shared bus, two words loaded at once, a select cone too large to
enumerate — is left as registers, and CDC-020 keeps judging it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from itertools import product

from rtl_buddy_cdc.netlist import Bit, Cell, Module

# Word register types: plain and enable flops (coarse and gate level).
_WORD_TYPES = frozenset({"$dff", "$dffe", "$_DFF_P_", "$_DFF_N_"}) | frozenset(
    f"$_DFFE_{c}{e}_" for c in "PN" for e in "PN"
)
# Multiplexer cells: data legs and select pins.
_MUX_LEGS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "$mux": (("A", "B"), ("S",)),
    "$_MUX_": (("A", "B"), ("S",)),
    "$pmux": (("A", "B"), ("S",)),
    "$bmux": (("A",), ("S",)),
    "$shiftx": (("A",), ("B",)),
}
# Most control nets enumerated (2**N assignments).
MAX_CONTROL_BITS = 12
_ELEMENT = re.compile(r"^(?P<base>.+)\[(?P<index>-?\d+)\]$")

# A symbolic value: a register's bit ``("q", word, j)`` or a write-data bit
# ``("w", j)``; concrete values are 0 / 1 / "x".
Value = int | str | tuple


class _Unsupported(Exception):
    """A cell the evaluator cannot evaluate on these values."""


@dataclass(frozen=True)
class RegisterMemory:
    """One register array recognised as a memory."""

    memid: str
    words: tuple[str, ...]  # register cell names, in element order
    clk: Bit
    wdata: tuple[Bit, ...]
    write_control: tuple[Bit, ...]
    # Read ports: (read data in column order, control leaves).
    reads: tuple[tuple[tuple[Bit, ...], tuple[Bit, ...]], ...]
    read_cells: tuple[str, ...]  # the read mux trees and their decode
    # The hold / load multiplexers and the write decode only it reads.
    write_cells: tuple[str, ...]
    src: str | None


# --- evaluation --------------------------------------------------------------


def _drivers(module: Module) -> dict[Bit, tuple[str, str, int]]:
    out: dict[Bit, tuple[str, str, int]] = {}
    for cell in module.cells.values():
        for pin, bits in cell.connections.items():
            if cell.is_output_pin(pin):
                for i, b in enumerate(bits):
                    if isinstance(b, int):
                        out[b] = (cell.name, pin, i)
    return out


def _consumers(module: Module) -> dict[Bit, list[tuple[str, str]]]:
    out: dict[Bit, list[tuple[str, str]]] = {}
    for cell in module.cells.values():
        for pin, bits in cell.connections.items():
            if cell.is_output_pin(pin):
                continue
            for b in bits:
                if isinstance(b, int):
                    out.setdefault(b, []).append((cell.name, pin))
    return out


def _signed(cell: Cell, pin: str) -> bool:
    raw = str(cell.parameters.get(f"{pin}_SIGNED", "0"))
    return raw.strip("0") != "" and raw[-1] == "1"


def _ext(vals: list[Value], width: int, signed: bool) -> list[Value]:
    if len(vals) >= width:
        return vals[:width]
    fill: Value = vals[-1] if signed and vals else 0
    return vals + [fill] * (width - len(vals))


def _concrete(vals: list[Value]) -> int | None:
    """``vals`` (LSB first) as an int; ``None`` if any is ``x``; raises on
    a symbol (a control function of data is not a decode)."""
    n = 0
    for i, v in enumerate(vals):
        if isinstance(v, tuple):
            raise _Unsupported("symbol in a control position")
        if v == "x":
            return None
        n |= int(v) << i
    return n


def _bitwise(op: str, a: Value, b: Value) -> Value:
    if op == "and":
        if a == 0 or b == 0:
            return 0
        if a == 1:
            return b
        if b == 1:
            return a
    if op == "or":
        if a == 1 or b == 1:
            return 1
        if a == 0:
            return b
        if b == 0:
            return a
    if isinstance(a, tuple) or isinstance(b, tuple):
        raise _Unsupported("bitwise logic on data")
    if a == "x" or b == "x":
        return "x"
    return {"and": a & b, "or": a | b, "xor": a ^ b, "xnor": 1 - (a ^ b)}[op]  # type: ignore[operator]


def _not(a: Value) -> Value:
    if isinstance(a, tuple):
        raise _Unsupported("inverted data")
    return "x" if a == "x" else 1 - int(a)


def _pick(sel: Value, a: Value, b: Value) -> Value:
    if sel == 0:
        return a
    if sel == 1:
        return b
    if isinstance(sel, tuple):
        raise _Unsupported("data on a select")
    return a if a == b else "x"


class _Evaluator:
    """Evaluate nets of ``module`` under one assignment of leaf bits."""

    def __init__(
        self,
        module: Module,
        drivers: dict[Bit, tuple[str, str, int]],
        symbols: dict[Bit, Value],
    ):
        self.module = module
        self.drivers = drivers
        # Data bits, the same in every assignment.
        self.symbols = symbols
        self.env: dict[Bit, Value] = {}
        self.memo: dict[str, dict[str, list[Value]]] = {}
        self._deps: dict[str, tuple[str, ...]] = {}

    def reset(self, env: dict[Bit, Value]) -> None:
        """Start a new assignment. Its keys (the leaves) must be the same
        in every assignment of one evaluator."""
        self.env = env
        self.memo = {}

    def bit(self, b: Bit) -> Value:
        if not isinstance(b, int):
            return int(b) if b in ("0", "1") else "x"
        if b in self.env:
            return self.env[b]
        if b in self.symbols:
            return self.symbols[b]
        drv = self.drivers.get(b)
        if drv is None:
            raise _Unsupported(f"leaf {b} not assigned")
        name, pin, idx = drv
        outs = self.memo.get(name)
        if outs is None:
            self._settle(name)
            outs = self.memo[name]
        return outs[pin][idx]

    def _settle(self, root: str) -> None:
        """Evaluate cell ``root`` and, first, every unevaluated cell it
        depends on, without recursion (rtl-buddy-cdc#414: a 256-deep mux
        chain overflowed the Python stack): a cell is evaluated once all
        its inputs are leaves, symbols or evaluated cells."""
        expanded: set[str] = set()
        stack = [root]
        memo = self.memo
        while stack:
            name = stack[-1]
            if name in memo:
                stack.pop()
                continue
            if name in expanded:
                # Every input is settled now.
                memo[name] = self._cell(self.module.cells[name])
                stack.pop()
                continue
            expanded.add(name)
            for dep in self._deps_of(name):
                if dep in memo:
                    continue
                if dep in expanded:
                    raise _Unsupported("combinational loop")
                stack.append(dep)

    def _deps_of(self, name: str) -> tuple[str, ...]:
        """The cells driving ``name``'s inputs that are neither leaves nor
        symbols. The leaves are the same in every assignment, so this is
        computed once per cell."""
        deps = self._deps.get(name)
        if deps is None:
            cell = self.module.cells[name]
            found: dict[str, None] = {}
            for pin, bits in cell.connections.items():
                if cell.is_output_pin(pin):
                    continue
                for b in bits:
                    if not isinstance(b, int) or b in self.env or b in self.symbols:
                        continue
                    drv = self.drivers.get(b)
                    if drv is None:
                        raise _Unsupported(f"leaf {b} not assigned")
                    found[drv[0]] = None
            deps = tuple(found)
            self._deps[name] = deps
        return deps

    def bits(self, bits: tuple[Bit, ...]) -> list[Value]:
        return [self.bit(b) for b in bits]

    def _cell(self, cell: Cell) -> dict[str, list[Value]]:
        t = cell.type
        c = cell.connections
        y_width = len(c.get("Y", ()))
        if t in _MUX_LEGS:
            return {"Y": self._mux(cell, y_width)}
        if t in ("$eq", "$ne", "$eqx", "$nex"):
            w = max(len(c["A"]), len(c["B"]))
            signed = _signed(cell, "A") and _signed(cell, "B")
            ia = _concrete(_ext(self.bits(c["A"]), w, signed))
            ib = _concrete(_ext(self.bits(c["B"]), w, signed))
            v: Value = "x"
            if ia is not None and ib is not None:
                v = int(ia == ib) if t in ("$eq", "$eqx") else int(ia != ib)
            return {"Y": [v, *([0] * (y_width - 1))]}
        if t in ("$and", "$or", "$xor", "$xnor"):
            signed = _signed(cell, "A") and _signed(cell, "B")
            va = _ext(self.bits(c["A"]), y_width, signed)
            vb = _ext(self.bits(c["B"]), y_width, signed)
            return {"Y": [_bitwise(t[1:], x, y) for x, y in zip(va, vb)]}
        if t in ("$_AND_", "$_OR_", "$_XOR_", "$_XNOR_"):
            x, y = self.bit(c["A"][0]), self.bit(c["B"][0])
            return {"Y": [_bitwise(t[2:-1].lower(), x, y)]}
        if t in ("$_ANDNOT_", "$_ORNOT_"):
            op = "and" if t == "$_ANDNOT_" else "or"
            x, y = self.bit(c["A"][0]), _not(self.bit(c["B"][0]))
            return {"Y": [_bitwise(op, x, y)]}
        if t in ("$_NAND_", "$_NOR_"):
            op = "and" if t == "$_NAND_" else "or"
            x, y = self.bit(c["A"][0]), self.bit(c["B"][0])
            return {"Y": [_not(_bitwise(op, x, y))]}
        if t in ("$not", "$_NOT_"):
            va = _ext(self.bits(c["A"]), y_width or 1, _signed(cell, "A"))
            return {"Y": [_not(x) for x in va]}
        if t in ("$pos", "$buf", "$_BUF_"):
            return {"Y": _ext(self.bits(c["A"]), y_width or 1, _signed(cell, "A"))}
        if t in ("$logic_not", "$reduce_bool", "$reduce_or", "$reduce_and"):
            ia = _concrete(self.bits(c["A"]))
            n = len(c["A"])
            v = "x"
            if ia is not None:
                if t == "$logic_not":
                    v = int(ia == 0)
                elif t == "$reduce_and":
                    v = int(ia == (1 << n) - 1)
                else:
                    v = int(ia != 0)
            return {"Y": [v, *([0] * (y_width - 1))]}
        if t in ("$logic_and", "$logic_or"):
            ia = _concrete(self.bits(c["A"]))
            ib = _concrete(self.bits(c["B"]))
            v = "x"
            if ia is not None and ib is not None:
                both = bool(ia) and bool(ib)
                v = int(both if t == "$logic_and" else bool(ia) or bool(ib))
            return {"Y": [v, *([0] * (y_width - 1))]}
        raise _Unsupported(f"cell type {t}")

    def _mux(self, cell: Cell, y_width: int) -> list[Value]:
        """Output of a multiplexer cell (:data:`_MUX_LEGS`)."""
        t = cell.type
        c = cell.connections
        va = self.bits(c["A"])
        if t in ("$mux", "$_MUX_"):
            sel = self.bit(c["S"][0])
            return [_pick(sel, x, y) for x, y in zip(va, self.bits(c["B"]))]
        if t == "$pmux":
            vb = self.bits(c["B"])
            hot = _concrete_list(self.bits(c["S"]))
            w = len(va)
            if hot is None:
                return ["x"] * w
            ones = [i for i, h in enumerate(hot) if h]
            if not ones:
                return va
            if len(ones) > 1:
                return ["x"] * w
            return vb[ones[0] * w : (ones[0] + 1) * w]
        if t == "$bmux":
            index = _concrete(self.bits(c["S"]))
            if index is None:
                return ["x"] * y_width
            return va[index * y_width : (index + 1) * y_width]
        # $shiftx
        vb = self.bits(c["B"])
        amt = _concrete(vb)
        if amt is None:
            return ["x"] * y_width
        if _signed(cell, "B") and vb and vb[-1] == 1:
            amt -= 1 << len(vb)
        return [va[i + amt] if 0 <= i + amt < len(va) else "x" for i in range(y_width)]


def _concrete_list(vals: list[Value]) -> list[int] | None:
    out: list[int] = []
    for v in vals:
        if isinstance(v, tuple):
            raise _Unsupported("data on a select")
        if v == "x":
            return None
        out.append(int(v))
    return out


# --- recognition -------------------------------------------------------------


def element_netnames(module: Module) -> dict[tuple[Bit, ...], tuple[str, int]]:
    """Q bits -> (array base name, element index) for every netname
    spelled as an element of an unpacked array."""
    out: dict[tuple[Bit, ...], tuple[str, int]] = {}
    for name, nn in module.netnames.items():
        m = _ELEMENT.match(name)
        if m is None:
            continue
        out.setdefault(tuple(nn.bits), (m["base"], int(m["index"])))
    return out


def _has_only(cell: Cell, pins: set[str]) -> bool:
    return set(cell.connections) <= pins


def _word_pins(cell: Cell) -> tuple[str | None, bool]:
    """``(enable pin, polarity)`` of a word register, or raise when it has
    a pin other than clock / data / enable (a reset, set or load)."""
    if cell.type == "$dff":
        if not _has_only(cell, {"CLK", "D", "Q"}):
            raise _Unsupported("extra pin")
        return None, True
    if cell.type == "$dffe":
        if not _has_only(cell, {"CLK", "D", "Q", "EN"}):
            raise _Unsupported("extra pin")
        raw = str(cell.parameters.get("EN_POLARITY", "1"))
        return "EN", raw[-1:] != "0"
    if cell.type.startswith("$_DFFE_"):
        return "E", cell.type[-2] == "P"
    return None, True


def _clk_key(cell: Cell) -> tuple[Bit, str, str]:
    clk = (cell.connections.get("CLK") or cell.connections.get("C") or ("x",))[0]
    return clk, cell.type, str(cell.parameters.get("CLK_POLARITY", ""))


# Combinational cells the evaluator models: a control cone walks through
# them; any other driver is a leaf.
_EVALUABLE = frozenset(
    {
        "$mux",
        "$_MUX_",
        "$pmux",
        "$bmux",
        "$shiftx",
        "$eq",
        "$ne",
        "$eqx",
        "$nex",
        "$and",
        "$or",
        "$xor",
        "$xnor",
        "$_AND_",
        "$_OR_",
        "$_XOR_",
        "$_XNOR_",
        "$_ANDNOT_",
        "$_ORNOT_",
        "$_NAND_",
        "$_NOR_",
        "$not",
        "$_NOT_",
        "$pos",
        "$buf",
        "$_BUF_",
        "$logic_not",
        "$reduce_bool",
        "$reduce_or",
        "$reduce_and",
        "$logic_and",
        "$logic_or",
    }
)
# Most cells a control cone may span before the structure is given up.
_MAX_CONE_CELLS = 4096
# Most (assignment x register) evaluations one array may cost.
MAX_EVALUATIONS = 1 << 18


def _budget(leaves: int, registers: int) -> None:
    if (1 << leaves) * registers > MAX_EVALUATIONS:
        raise _Unsupported("enumeration over budget")


def _control_leaves(
    module: Module,
    roots: set[Bit],
    owned: set[str],
    drivers: dict[Bit, tuple[str, str, int]],
    consumers: dict[Bit, list[tuple[str, str]]],
    ports: frozenset[Bit],
    stop: set[Bit],
) -> tuple[list[Bit], set[str]]:
    """Leaves of the control cone behind ``roots`` and the cells it owns.

    The cone is walked back through the combinational cells used only by
    the structure (``owned``, the cells already attributed to it, grows
    with them): a cell any other logic reads is shared, so its output is a
    leaf (a free variable of the enumeration — every value of it is
    tried, a sound over-approximation), and so is a register, a port, an
    undriven net or a cell the evaluator does not model. Raises when the
    cone reaches a ``stop`` bit (a word or the write data: control that
    depends on data is not a decode) or when it has more than
    :data:`MAX_CONTROL_BITS` leaves."""
    cone: set[str] = set()
    stack = [b for b in roots if isinstance(b, int)]
    seen: set[Bit] = set()
    while stack:
        b = stack.pop()
        if b in seen:
            continue
        seen.add(b)
        if b in stop:
            raise _Unsupported("data in a control cone")
        drv = drivers.get(b)
        if drv is None or b in ports:
            continue
        cell = module.cells[drv[0]]
        if cell.type not in _EVALUABLE or cell.name in owned or cell.name in cone:
            continue
        cone.add(cell.name)
        if len(cone) > _MAX_CONE_CELLS:
            raise _Unsupported("control cone too large")
        for pin, bits in cell.connections.items():
            if not cell.is_output_pin(pin):
                stack.extend(x for x in bits if isinstance(x, int))
    # Keep only the cells nothing outside the structure reads.
    mine = set(cone)
    changed = True
    while changed:
        changed = False
        for name in sorted(mine):
            cell = module.cells[name]
            if any(
                o in ports
                or any(
                    c not in mine and c not in owned for c, _p in consumers.get(o, ())
                )
                for o in _cell_out_bits(cell)
            ):
                mine.discard(name)
                changed = True
    leaves: set[Bit] = set()
    stack = [b for b in roots if isinstance(b, int)]
    seen = set()
    while stack:
        b = stack.pop()
        if b in seen:
            continue
        seen.add(b)
        drv = drivers.get(b)
        drv_name = drv[0] if drv is not None else None
        if drv_name is not None and drv_name in owned and drv_name not in mine:
            # A structure cell (a hold mux): its select cone was given as
            # a root already.
            continue
        if drv_name is None or drv_name not in mine or b in ports:
            leaves.add(b)
            continue
        cell = module.cells[drv_name]
        for pin, bits in cell.connections.items():
            if not cell.is_output_pin(pin):
                stack.extend(x for x in bits if isinstance(x, int))
    if len(leaves) > MAX_CONTROL_BITS:
        raise _Unsupported("too many control bits")
    owned |= mine
    return sorted(leaves, key=str), owned


def _cell_out_bits(cell: Cell) -> list[Bit]:
    return [
        b
        for pin, bits in cell.connections.items()
        if cell.is_output_pin(pin)
        for b in bits
        if isinstance(b, int)
    ]


def _const_value(b: Bit) -> Value:
    return int(b) if b in ("0", "1") else "x"


def _write_side(
    module: Module,
    words: list[Cell],
    drivers: dict[Bit, tuple[str, str, int]],
    consumers: dict[Bit, list[tuple[str, str]]],
    ports: frozenset[Bit],
) -> tuple[tuple[Bit, ...], tuple[Bit, ...], set[str]]:
    """``(wdata, control leaves, write-side cells)`` when every word holds
    or loads one shared bus, at most one loads at a time and each loads
    sometimes; raises otherwise."""
    width = len(words[0].connections["Q"])
    qbits = [tuple(w.connections["Q"]) for w in words]
    q_owner = {b: (k, j) for k, q in enumerate(qbits) for j, b in enumerate(q)}
    wdata: list[Bit | None] = [None] * width
    selects: set[Bit] = set()
    # The structure's own cells: a net only they read is not shared.
    owned: set[str] = {w.name for w in words}
    for k, w in enumerate(words):
        if len(w.connections.get("D", ())) != width:
            raise _Unsupported("D and Q widths differ")
        en_pin, _pol = _word_pins(w)
        if en_pin is not None:
            selects.update(b for b in w.connections[en_pin] if isinstance(b, int))
        for j, d in enumerate(w.connections["D"]):
            stack = [d]
            while stack:
                b = stack.pop()
                if b == qbits[k][j]:
                    continue
                drv = drivers.get(b) if isinstance(b, int) else None
                cell = module.cells[drv[0]] if drv is not None else None
                if (
                    drv is not None
                    and cell is not None
                    and cell.type in ("$mux", "$_MUX_")
                ):
                    # A hold / load multiplexer: follow this lane's legs.
                    owned.add(cell.name)
                    selects.update(
                        x for x in cell.connections["S"] if isinstance(x, int)
                    )
                    stack.extend(cell.connections[p][drv[2]] for p in ("A", "B"))
                    continue
                if wdata[j] is None:
                    wdata[j] = b
                elif wdata[j] != b:
                    raise _Unsupported("two write data sources")
    if any(b is None or b in q_owner for b in wdata):
        raise _Unsupported("no shared write data")
    # A hold / load multiplexer is the register's next state and nothing
    # else's: logic reading it would read a word the rewrite removes.
    for name in owned:
        cell = module.cells[name]
        if cell.type in _WORD_TYPES:
            continue
        for b in _cell_out_bits(cell):
            if b in ports or any(c not in owned for c, _p in consumers.get(b, ())):
                raise _Unsupported("a register's next state is read elsewhere")
    data: tuple[Bit, ...] = tuple(b for b in wdata if b is not None)
    if not any(isinstance(b, int) for b in data):
        raise _Unsupported("constant write data")
    stop = set(q_owner) | {b for b in data if isinstance(b, int)}
    order, owned = _control_leaves(
        module, selects, owned, drivers, consumers, ports, stop
    )
    expected = [
        ("w", j) if isinstance(b, int) else _const_value(b) for j, b in enumerate(data)
    ]
    _budget(len(order), len(words))
    symbols: dict[Bit, Value] = {b: ("q", k, j) for b, (k, j) in q_owner.items()}
    symbols.update({b: ("w", j) for j, b in enumerate(data) if isinstance(b, int)})
    ev = _Evaluator(module, drivers, symbols)
    loaded = [False] * len(words)
    plan = []
    for k, w in enumerate(words):
        en_pin, pol = _word_pins(w)
        en_bit = w.connections[en_pin][0] if en_pin is not None else None
        hold = [("q", k, j) for j in range(width)]
        plan.append((k, en_bit, 1 if pol else 0, tuple(w.connections["D"]), hold))
    for values in product((0, 1), repeat=len(order)):
        ev.reset(dict(zip(order, values)))
        writes = 0
        for k, en_bit, active, d_bits, hold in plan:
            if en_bit is not None:
                v = ev.bit(en_bit)
                if v not in (0, 1):
                    raise _Unsupported("undefined enable")
                if v != active:
                    continue
            dvals = ev.bits(d_bits)
            if dvals == hold:
                continue
            if dvals != expected:
                raise _Unsupported("a word loads something else")
            writes += 1
            if writes > 1:
                raise _Unsupported("two words loaded at once")
            loaded[k] = True
    if not all(loaded):
        raise _Unsupported("a word is never written")
    return data, tuple(order), owned


def _read_side(
    module: Module,
    words: list[Cell],
    write_cells: set[str],
    drivers: dict[Bit, tuple[str, str, int]],
    consumers: dict[Bit, list[tuple[str, str]]],
    ports: frozenset[Bit],
) -> tuple[tuple[tuple[tuple[Bit, ...], tuple[Bit, ...]], ...], set[str]]:
    """``(read ports, read-side cells)`` when the words are read by
    multiplexer trees only, each of which always yields one whole word
    (a port is ``(read data, control leaves)``); raises otherwise."""
    qbits = [tuple(w.connections["Q"]) for w in words]
    q_owner = {b: (k, j) for k, q in enumerate(qbits) for j, b in enumerate(q)}
    word_names = {w.name for w in words}
    if any(b in ports for b in q_owner):
        raise _Unsupported("a word is a port")
    # Grow the tree: a mux whose every data input is a word bit, a tree
    # output or a constant joins it (so a user mux mixing the read data
    # with anything else stays outside, reading the tree's output).
    tree: set[str] = set()
    tree_out: set[Bit] = set()
    frontier = set(q_owner)
    while frontier:
        candidates = {
            name
            for b in frontier
            for name, pin in consumers.get(b, ())
            if name not in tree
            and (legs := _MUX_LEGS.get(module.cells[name].type)) is not None
            and pin in legs[0]
        }
        frontier = set()
        for name in sorted(candidates):
            cell = module.cells[name]
            data_pins = _MUX_LEGS[cell.type][0]
            if all(
                not isinstance(b, int) or b in q_owner or b in tree_out
                for p in data_pins
                for b in cell.connections[p]
            ):
                tree.add(name)
                outs = _cell_out_bits(cell)
                tree_out.update(outs)
                frontier.update(outs)
    if not tree:
        raise _Unsupported("no read tree")
    for b in q_owner:
        for name, _pin in consumers.get(b, ()):
            if name not in tree and name not in write_cells and name not in word_names:
                raise _Unsupported("a word is read by other logic")
    # One read port per connected part of the tree (``hi <= mem[ra][7:4];
    # lo <= mem[ra][3:0]`` is two), each checked on its own.
    parts = _components(module, tree)
    read_ports: list[tuple[tuple[Bit, ...], tuple[Bit, ...]]] = []
    owned = set(tree) | word_names | write_cells
    for part in parts:
        rdata, control, owned = _read_port(
            module, part, q_owner, len(qbits[0]), owned, drivers, consumers, ports
        )
        read_ports.append((rdata, control))
    return tuple(read_ports), owned - word_names - write_cells


def _components(module: Module, tree: set[str]) -> list[set[str]]:
    """The tree's cells grouped by connection (an output of one read by
    another), in a stable order."""
    parent = {name: name for name in tree}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    out_of: dict[Bit, str] = {}
    for name in tree:
        for b in _cell_out_bits(module.cells[name]):
            out_of[b] = name
    for name in tree:
        cell = module.cells[name]
        for pin, bits in cell.connections.items():
            if cell.is_output_pin(pin):
                continue
            for b in bits:
                src = out_of.get(b)
                if src is not None:
                    parent[find(src)] = find(name)
    groups: dict[str, set[str]] = {}
    for name in tree:
        groups.setdefault(find(name), set()).add(name)
    return sorted(groups.values(), key=lambda g: sorted(g))


def _read_port(
    module: Module,
    part: set[str],
    q_owner: dict[Bit, tuple[int, int]],
    width: int,
    owned: set[str],
    drivers: dict[Bit, tuple[str, str, int]],
    consumers: dict[Bit, list[tuple[str, str]]],
    ports: frozenset[Bit],
) -> tuple[tuple[Bit, ...], tuple[Bit, ...], set[str]]:
    """``(read data in column order, control leaves, owned cells)`` of one
    read port, the tree cells ``part``: over every value of its control,
    its outputs are all of one word (or undefined)."""
    part_out = {b for name in part for b in _cell_out_bits(module.cells[name])}
    # The final outputs: outputs no cell of the part reads. An inner one
    # read outside the part is a partial read.
    rdata: list[Bit] = []
    for b in sorted(part_out, key=str):
        users = consumers.get(b, ())
        inner = [n for n, _p in users if n in part]
        if not inner:
            rdata.append(b)
        elif len(inner) != len(users) or b in ports:
            raise _Unsupported("a partial read")
    selects: set[Bit] = set()
    for name in part:
        cell = module.cells[name]
        for p in _MUX_LEGS[cell.type][1]:
            selects.update(x for x in cell.connections[p] if isinstance(x, int))
    order, owned = _control_leaves(
        module, selects, owned, drivers, consumers, ports, set(q_owner) | part_out
    )
    _budget(len(order), 1)
    symbols: dict[Bit, Value] = {b: ("q", k, j) for b, (k, j) in q_owner.items()}
    ev = _Evaluator(module, drivers, symbols)
    column: dict[Bit, int] = {}
    for values in product((0, 1), repeat=len(order)):
        ev.reset(dict(zip(order, values)))
        word: int | None = None
        for b in rdata:
            v = ev.bit(b)
            if not isinstance(v, tuple):
                continue  # an address past the last word: undefined
            _q, k, j = v
            if word is None:
                word = k
            elif word != k:
                raise _Unsupported("a read mixes two words")
            if column.setdefault(b, j) != j:
                raise _Unsupported("a read bit moves between columns")
    if len(column) != len(rdata) or sorted(column.values()) != list(range(width)):
        raise _Unsupported("the read is not one whole word")
    return tuple(sorted(rdata, key=lambda b: column[b])), tuple(order), owned


def _truthy(raw: object) -> bool:
    return raw is not None and any(ch not in "0" for ch in str(raw))


def _posedge(cell: Cell) -> bool:
    """A rising-edge word register (rtl-buddy-cdc#414: the write port the
    rewrite builds is rising-edge, so a falling-edge array is not
    rewritten)."""
    if cell.type in ("$dff", "$dffe"):
        raw = str(cell.parameters.get("CLK_POLARITY", "1"))
        return raw[-1:] != "0"
    return cell.type in ("$_DFF_P_",) or cell.type.startswith("$_DFFE_P")


def recognise(module: Module, notes: list[str] | None = None) -> list[RegisterMemory]:
    """Every register array of ``module`` that is a memory (module docstring).

    Never raises: an array whose analysis fails for any reason stays
    registers, and when ``notes`` is given a line saying so is appended to
    it."""
    elements = element_netnames(module)
    keep_nets = {
        tuple(nn.bits)
        for nn in module.netnames.values()
        if _truthy(nn.attributes.get("keep"))
    }
    groups: dict[tuple, list[tuple[int, Cell]]] = {}
    for cell in module.cells.values():
        if cell.type not in _WORD_TYPES:
            continue
        q = tuple(cell.connections.get("Q", ()))
        elem = elements.get(q)
        if elem is None or not q:
            continue
        base, index = elem
        key = (base, len(q), *_clk_key(cell))
        groups.setdefault(key, []).append((index, cell))
    if not groups:
        return []
    drivers = _drivers(module)
    consumers = _consumers(module)
    ports = frozenset(b for p in module.ports.values() for b in p.bits)
    out: list[RegisterMemory] = []
    for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if len(members) < 2:
            continue
        words = [c for _i, c in sorted(members, key=lambda t: t[0])]
        try:
            for w in words:
                _word_pins(w)
                if not _posedge(w):
                    raise _Unsupported("falling-edge registers")
                if (
                    _truthy(w.attributes.get("keep"))
                    or tuple(w.connections["Q"]) in keep_nets
                ):
                    raise _Unsupported("a register is marked keep")
            wdata, wctl, wcells = _write_side(module, words, drivers, consumers, ports)
            reads, rcells = _read_side(module, words, wcells, drivers, consumers, ports)
        except _Unsupported:
            continue
        except Exception as exc:  # noqa: BLE001 - never fail the run over this
            if notes is not None:
                notes.append(
                    f"register array {key[0]} left as registers: its memory "
                    f"check failed ({type(exc).__name__}: {exc})"
                )
            continue
        out.append(
            RegisterMemory(
                memid=str(key[0]),
                words=tuple(w.name for w in words),
                clk=key[2],
                wdata=wdata,
                write_control=wctl,
                reads=reads,
                read_cells=tuple(sorted(rcells)),
                write_cells=tuple(sorted(wcells - {w.name for w in words})),
                src=words[0].attributes.get("src"),
            )
        )
    return out


def _driven(module: Module) -> set[Bit]:
    """Every bit something drives: a cell output or a module input."""
    out: set[Bit] = {
        b
        for p in module.ports.values()
        if p.direction != "output"
        for b in p.bits
        if isinstance(b, int)
    }
    for cell in module.cells.values():
        out.update(_cell_out_bits(cell))
    return out


def _read_bits(module: Module) -> set[Bit]:
    """Every bit something reads: a cell input or a module output."""
    out: set[Bit] = {
        b
        for p in module.ports.values()
        if p.direction != "input"
        for b in p.bits
        if isinstance(b, int)
    }
    for cell in module.cells.values():
        for pin, bits in cell.connections.items():
            if not cell.is_output_pin(pin):
                out.update(b for b in bits if isinstance(b, int))
    return out


def _rewrite_one(module: Module, mem: RegisterMemory) -> Module | None:
    """``module`` with ``mem`` as a memory, or ``None`` when that would
    leave something reading a net nothing drives any more.

    A cell is deleted only if every reader of every one of its outputs is
    deleted too (or the output is the new read port's data): a read-side
    decode the write side also uses stays (rtl-buddy-cdc#414). The result
    is then checked as a whole — no bit that was driven and is read
    afterwards may be undriven — so a reader the recognition missed (of a
    word, of a hold mux) refuses the rewrite instead of silently cutting a
    crossing."""
    consumers = _consumers(module)
    redriven = {b for rdata, _ctl in mem.reads for b in rdata if isinstance(b, int)}
    # What the new ports read must stay driven.
    needed = set(mem.write_control) | set(mem.wdata)
    for _rdata, ctl in mem.reads:
        needed.update(ctl)
    words = set(mem.words)
    doomed = words | set(mem.read_cells) | set(mem.write_cells)
    changed = True
    while changed:
        changed = False
        for name in sorted(doomed - words):
            cell = module.cells[name]
            for b in _cell_out_bits(cell):
                if b in redriven:
                    continue
                if b in needed or any(
                    c not in doomed for c, _p in consumers.get(b, ())
                ):
                    doomed.discard(name)
                    changed = True
                    break
    cells = {n: c for n, c in module.cells.items() if n not in doomed}
    attrs = {"src": mem.src} if mem.src else {}
    params = {
        "MEMID": f"\\{mem.memid}",
        "WIDTH": format(len(mem.wdata), "b"),
        "ABITS": format(max(len(mem.write_control), 1), "b"),
    }
    wr = f"{mem.memid}$regfile$wr"
    cells[wr] = Cell(
        name=wr,
        type="$memwr_v2",
        connections={
            "CLK": (mem.clk,),
            "ADDR": mem.write_control,
            "DATA": mem.wdata,
            "EN": (),
        },
        parameters={**params, "CLK_ENABLE": "1", "CLK_POLARITY": "1", "PORTID": "0"},
        attributes=dict(attrs),
        port_directions={
            "CLK": "input",
            "ADDR": "input",
            "DATA": "input",
            "EN": "input",
        },
    )
    for i, (rdata, rctl) in enumerate(mem.reads):
        rd = f"{mem.memid}$regfile$rd{i}"
        cells[rd] = Cell(
            name=rd,
            type="$memrd_v2",
            connections={"CLK": ("x",), "ADDR": rctl, "DATA": rdata, "EN": ("1",)},
            parameters={**params, "CLK_ENABLE": "0"},
            attributes=dict(attrs),
            port_directions={
                "CLK": "input",
                "ADDR": "input",
                "DATA": "output",
                "EN": "input",
            },
        )
    after = replace(module, cells=cells)
    lost = (_read_bits(after) & _driven(module)) - _driven(after)
    if lost:
        return None
    return after


def rewrite(
    module: Module,
    memories: list[RegisterMemory],
    notes: list[str] | None = None,
) -> tuple[Module, list[RegisterMemory]]:
    """``module`` with each of ``memories`` as one ``$memwr_v2`` write port
    and an asynchronous ``$memrd_v2`` per read tree (``MEMID`` the array
    name), and the memories actually rewritten. The word registers and
    the read trees go, and so do the hold multiplexers and the decode only
    they read (whatever else still reads a cell keeps it). A
    memory whose rewrite would cut a net something still reads
    (:func:`_rewrite_one`) is left as registers, with a line in ``notes``."""
    done: list[RegisterMemory] = []
    for mem in memories:
        try:
            after = _rewrite_one(module, mem)
        except Exception as exc:  # noqa: BLE001 - never fail the run over this
            after = None
            reason = f"{type(exc).__name__}: {exc}"
        else:
            reason = "other logic reads its registers or their next state"
        if after is None:
            if notes is not None:
                notes.append(f"register array {mem.memid} left as registers: {reason}")
            continue
        module = after
        done.append(mem)
    return module, done
