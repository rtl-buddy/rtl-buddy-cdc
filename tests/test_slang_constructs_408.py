"""slang frontend: constructs that were opaque or dropped until
rtl-buddy-cdc#408 (item 11), each checked by simulating the slang netlist
against the SystemVerilog semantics.

Lowered: unpacked-struct members; ``while`` / ``do … while`` / ``forever``
loops whose trip count folds at elaboration time; ``break`` / ``continue``
in a clocked loop; ``->`` / ``<->``; ``++`` / ``--`` and assignments
inside an expression; enum ``next()`` / ``prev()`` and array reduction
methods; ``inside`` over an unpacked array; function calls with ``output``
arguments inside an expression; run-time ``$clog2`` / ``$countbits``.
What stays unlowered is still reported, never dropped silently.

Yosys parses only a few of these shapes (unpacked structs); where it does
and is on PATH, its ``lint`` netlist is checked against the same model.
CI's pytest job has no yosys; there the slang-side assertions stand alone.
"""

from __future__ import annotations

import importlib.util
import logging
import random
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.netlist import Bit, Cell, Module

if importlib.util.find_spec("pyslang") is None:
    pytest.skip("pyslang not installed", allow_module_level=True)

from rtl_buddy_cdc.frontends import slang as slang_fe  # noqa: E402

_HAVE_YOSYS = shutil.which("yosys") is not None
_SLANG_LOG = "rtl_buddy_cdc.frontends.slang"
_M4 = 15


def _slang(tmp_path: Path, src: str) -> Module:
    sv = tmp_path / "s.sv"
    sv.write_text(src)
    module, _ = slang_fe.elaborate_with_blackboxes([sv], "m")
    return module


def _yosys(tmp_path: Path, src: str) -> Module:
    """The ``lint`` recipe, from a script file (CI's yosys 0.52 splits a
    ``-p`` script at commas)."""
    sv = tmp_path / "y.sv"
    sv.write_text(src)
    out = tmp_path / "y.json"
    script = tmp_path / "y.ys"
    script.write_text(
        f"read_verilog -sv {sv}\nhierarchy -top m\nproc\nflatten\n"
        f"opt_clean\nwrite_json {out}\n"
    )
    subprocess.run(["yosys", "-q", "-s", str(script)], check=True)
    return netlist.load(out)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _SLANG_LOG]


# --- a cycle simulator over the netlist ---------------------------------------


def _param(cell: Cell, name: str, default: int = 0) -> int:
    value = cell.parameters.get(name)
    if not value:
        return default
    return int(value.replace("x", "0").replace("z", "0"), 2)


def _sx(value: int, width: int, signed: bool = True) -> int:
    value &= (1 << width) - 1 if width else 0
    if signed and width and value >> (width - 1):
        value -= 1 << width
    return value


_FLOPS = frozenset({"$dff", "$adff", "$sdff", "$sdffe", "$sdffce", "$dffe", "$adffe"})


class _Sim:
    """Two-valued simulation: ``x`` reads as 0. Flops update on ``tick``;
    latches, ``$dffsr`` / ``$aldff`` controls and memories settle."""

    def __init__(self, module: Module) -> None:
        self.m = module
        self.comb: dict[Bit, tuple[Cell, int]] = {}
        self.flops: list[Cell] = []
        self.levels: list[Cell] = []
        self.memwr: list[Cell] = []
        for cell in module.cells.values():
            if cell.type in _FLOPS:
                self.flops.append(cell)
            elif cell.type in ("$dlatch", "$dffsr", "$aldff"):
                self.levels.append(cell)
                if cell.type != "$dlatch":
                    self.flops.append(cell)
            elif cell.type == "$memwr_v2":
                self.memwr.append(cell)
            else:
                pin = "DATA" if cell.type == "$memrd_v2" else "Y"
                for k, bit in enumerate(cell.connections[pin]):
                    self.comb[bit] = (cell, k)
        self.state: dict[Bit, int] = {}
        self.mem: dict[tuple[str, int], int] = {}
        self.env: dict[Bit, int] = {}
        self.memo: dict[str, int] = {}

    def num(self, bits: tuple[Bit, ...]) -> int:
        return sum(self.get(b) << k for k, b in enumerate(bits))

    def get(self, bit: Bit) -> int:
        if isinstance(bit, str):
            return int(bit == "1")
        if bit in self.env:
            return self.env[bit]
        if bit in self.state:
            return self.state[bit]
        if bit not in self.comb:
            return 0
        cell, k = self.comb[bit]
        if cell.name not in self.memo:
            self.memo[cell.name] = 0
            self.memo[cell.name] = self.cell(cell)
        return (self.memo[cell.name] >> k) & 1

    def cell(self, cell: Cell) -> int:
        c = cell.connections
        t = cell.type
        if t == "$_UNKNOWN_":
            return 0
        if t == "$memrd_v2":
            return self.mem.get((cell.parameters["MEMID"], self.num(c["ADDR"])), 0)
        width = len(c["Y"])
        mask = (1 << width) - 1
        if t == "$mux":
            return self.num(c["B"] if self.get(c["S"][0]) else c["A"])
        if t == "$pmux":
            for k, s in enumerate(c["S"]):
                if self.get(s):
                    return self.num(c["B"][k * width : (k + 1) * width])
            return self.num(c["A"])
        a_bits, b_bits = c.get("A", ()), c.get("B", ())
        ua, ub = self.num(a_bits), self.num(b_bits)
        a_signed = bool(_param(cell, "A_SIGNED"))
        b_signed = bool(_param(cell, "B_SIGNED"))
        a = _sx(ua, len(a_bits), a_signed)
        b = _sx(ub, len(b_bits), b_signed)
        if t in ("$lt", "$le", "$gt", "$ge", "$eq", "$ne") and not (
            a_signed and b_signed
        ):
            a, b = ua, ub
        full = max(len(a_bits), len(b_bits), width)
        if t in ("$shiftx", "$shift"):
            return (ua >> b if b >= 0 else ua << -b) & mask
        ops: dict[str, Callable[[], int]] = {
            "$not": lambda: ~a,
            "$neg": lambda: -a,
            "$logic_not": lambda: int(ua == 0),
            "$and": lambda: a & b,
            "$or": lambda: a | b,
            "$xor": lambda: a ^ b,
            "$xnor": lambda: ~(a ^ b),
            "$reduce_and": lambda: int(ua == (1 << len(a_bits)) - 1),
            "$reduce_or": lambda: int(ua != 0),
            "$reduce_bool": lambda: int(ua != 0),
            "$reduce_xor": lambda: bin(ua).count("1") & 1,
            "$logic_and": lambda: int(ua != 0 and ub != 0),
            "$logic_or": lambda: int(ua != 0 or ub != 0),
            "$eq": lambda: int(a == b),
            "$ne": lambda: int(a != b),
            "$lt": lambda: int(a < b),
            "$le": lambda: int(a <= b),
            "$gt": lambda: int(a > b),
            "$ge": lambda: int(a >= b),
            "$add": lambda: a + b,
            "$sub": lambda: a - b,
            "$mul": lambda: a * b,
            "$shl": lambda: a << ub,
            "$shr": lambda: (a & ((1 << full) - 1)) >> ub,
            "$sshr": lambda: a >> ub,
        }
        return ops[t]() & mask

    def settle(self, inputs: dict[str, int]) -> None:
        for _ in range(8):
            self.env = {}
            for name, value in inputs.items():
                for k, bit in enumerate(self.m.ports[name].bits):
                    if not isinstance(bit, str):
                        self.env[bit] = (value >> k) & 1
            self.memo = {}
            changed = False
            for cell in self.levels:
                c = cell.connections
                new = None
                if cell.type == "$dlatch":
                    if self.get(c["EN"][0]) == _param(cell, "EN_POLARITY", 1):
                        new = self.num(c["D"])
                elif cell.type == "$aldff":
                    if self.get(c["ALOAD"][0]) == _param(cell, "ALOAD_POLARITY", 1):
                        new = self.num(c["AD"])
                else:  # $dffsr
                    full = (1 << len(c["Q"])) - 1
                    s, r = self.num(c["SET"]), self.num(c["CLR"])
                    if not _param(cell, "SET_POLARITY", 1):
                        s = ~s & full
                    if not _param(cell, "CLR_POLARITY", 1):
                        r = ~r & full
                    if s or r:
                        new = (self.num(c["Q"]) | s) & ~r
                if new is not None:
                    changed |= self._load(c["Q"], new)
            for cell in self.flops:
                if cell.type in ("$adff", "$adffe"):
                    c = cell.connections
                    if self.get(c["ARST"][0]) == _param(cell, "ARST_POLARITY", 1):
                        changed |= self._load(c["Q"], _param(cell, "ARST_VALUE"))
            if not changed:
                return

    def _load(self, q: tuple[Bit, ...], value: int) -> bool:
        changed = False
        for k, bit in enumerate(q):
            if not isinstance(bit, str) and self.state.get(bit, 0) != (value >> k) & 1:
                self.state[bit] = (value >> k) & 1
                changed = True
        return changed

    def tick(self, inputs: dict[str, int]) -> None:
        self.settle(inputs)
        updates: list[tuple[tuple[Bit, ...], int]] = []
        for cell in self.flops:
            c = cell.connections
            if cell.type in ("$dffsr", "$aldff"):
                updates.append((c["Q"], self.num(c["D"])))
                continue
            value = self.num(c["Q"])
            enable = "EN" not in c or self.get(c["EN"][0]) == _param(
                cell, "EN_POLARITY", 1
            )
            srst = "SRST" in c and self.get(c["SRST"][0]) == _param(
                cell, "SRST_POLARITY", 1
            )
            if cell.type == "$sdffce":
                if enable:
                    value = _param(cell, "SRST_VALUE") if srst else self.num(c["D"])
            elif srst:
                value = _param(cell, "SRST_VALUE")
            elif enable:
                value = self.num(c["D"])
            updates.append((c["Q"], value))
        writes = []
        for cell in self.memwr:
            c = cell.connections
            en = self.num(c["EN"])
            if en:
                memid = cell.parameters["MEMID"]
                writes.append((memid, self.num(c["ADDR"]), self.num(c["DATA"]), en))
        for q, value in updates:
            self._load(q, value)
        for memid, addr, data, en in writes:
            old = self.mem.get((memid, addr), 0)
            self.mem[(memid, addr)] = (old & ~en) | (data & en)
        self.settle(inputs)

    def outputs(self, inputs: dict[str, int]) -> dict[str, int]:
        self.settle(inputs)
        return {
            name: self.num(port.bits)
            for name, port in self.m.ports.items()
            if port.direction == "output"
        }


# A sequential model: ``(step(state, inputs), outputs(state, inputs))``; a
# combinational one maps the inputs to the outputs.
_Model = Any


def _run(module: Module, model: _Model, cycles: int = 200) -> None:
    """Drive ``module`` with random inputs (``clk`` held low, one ``tick``
    per cycle) and compare every output with ``model``."""
    sim = _Sim(module)
    clocked = "clk" in module.ports
    inputs = [
        n for n, p in module.ports.items() if p.direction == "input" and n != "clk"
    ]
    rnd = random.Random(408)
    state: dict[str, Any] = {}
    for cycle in range(cycles):
        values = {n: rnd.getrandbits(len(module.ports[n].bits)) for n in inputs}
        drive = dict(values, clk=0) if clocked else values
        got = sim.outputs(drive)
        want = model[1](state, values) if isinstance(model, tuple) else model(**values)
        assert got == want, (cycle, values, got, want)
        if clocked:
            sim.tick(drive)
            model[0](state, values)


def _check(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    src: str,
    model: _Model,
    *,
    yosys: bool = False,
) -> Module:
    """The slang netlist (and, with ``yosys``, the Yosys one) computes
    ``model``, with no construct reported."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not _warnings(caplog)
    assert not [c for c in module.cells.values() if c.type == "$_UNKNOWN_"]
    _run(module, model)
    if yosys and _HAVE_YOSYS:
        _run(_yosys(tmp_path, src), model)
    return module


def _register(outs: tuple[str, ...], next_value: Callable[..., dict[str, int]]):
    """A model of registers ``outs`` loaded every cycle with
    ``next_value(**inputs)``."""
    return (
        lambda st, v: st.update(next_value(**v)),
        lambda st, v: {k: st.get(k, 0) for k in outs},
    )


def _first1(value: int, width: int) -> int | None:
    return next((i for i in range(width) if (value >> i) & 1), None)


def _reverse4(value: int) -> int:
    return int(f"{value:04b}"[::-1], 2)


# --- unpacked structs ----------------------------------------------------------

_US_BASIC = """module m (input clk, input [3:0] a, input [3:0] b, output logic [3:0] o,
          output logic [3:0] p, output logic [7:0] q);
    typedef struct { logic [3:0] x; logic [3:0] y; } s_t;
    s_t s, r;
    always_comb begin s.x = a; s.y = b ^ a; end
    assign o = s.x & s.y;
    always_ff @(posedge clk) begin r.x <= s.y; r.y <= r.x; end
    assign p = r.y;
    assign q = {r.x, s.x};
endmodule
"""


def _us_basic_step(st: dict, v: dict) -> None:
    st["ry"], st["rx"] = st.get("rx", 0), v["a"] ^ v["b"]


def _us_basic_out(st: dict, v: dict) -> dict[str, int]:
    return {
        "o": v["a"] & (v["a"] ^ v["b"]),
        "p": st.get("ry", 0),
        "q": (st.get("rx", 0) << 4) | v["a"],
    }


_US_NESTED = """module m (input [3:0] a, input [3:0] i, output [3:0] o, output [1:0] p,
          output [1:0] r);
    typedef struct { logic [1:0] u; logic [1:0] v; } in_t;
    typedef struct { logic [3:0] x; in_t n; logic [1:0] y [3]; } s_t;
    s_t s;
    always_comb begin
        s.x = a; s.n.u = i[1:0]; s.n.v = a[3:2] ^ i[1:0];
        s.y[0] = a[1:0]; s.y[1] = a[3:2]; s.y[2] = i[3:2];
    end
    assign o = s.x ^ {s.n.u, s.n.v};
    assign p = s.n.v;
    assign r = s.y[i[3:2] == 2'd3 ? 2 : i[3]];
endmodule
"""


def _us_nested(a: int, i: int) -> dict[str, int]:
    v = (a >> 2) ^ (i & 3)
    y = [a & 3, a >> 2, i >> 2]
    return {
        "o": a ^ (((i & 3) << 2) | v),
        "p": v,
        "r": y[2 if (i >> 2) == 3 else i >> 3],
    }


_US_AGGREGATES = """module m (input clk, input [1:0] i, input [1:0] j, input [3:0] a,
          output [3:0] o, output [3:0] p, output [3:0] q, output [1:0] r);
    typedef struct { logic [3:0] x; logic [3:0] y; } s_t;
    localparam s_t P = '{x: 4'h9, y: 4'h6};
    s_t arr [4];
    s_t pat;
    always_ff @(posedge clk) begin arr[i].x <= a; arr[i].y <= ~a; end
    assign o = arr[j].x ^ arr[j].y;
    assign pat = '{y: a + 1, x: P.y};
    assign p = pat.x ^ pat.y;
    assign q = P.x;
    function automatic s_t split(input logic [3:0] v);
        split.x = {2'b0, v[3:2]}; split.y = {2'b0, v[1:0]};
    endfunction
    assign r = split(a).y[1:0];
endmodule
"""


def _us_aggregates_step(st: dict, v: dict) -> None:
    st.setdefault("arr", [(0, 0)] * 4)[v["i"]] = (v["a"], ~v["a"] & _M4)


def _us_aggregates_out(st: dict, v: dict) -> dict[str, int]:
    x, y = st.get("arr", [(0, 0)] * 4)[v["j"]]
    return {"o": x ^ y, "p": 6 ^ ((v["a"] + 1) & _M4), "q": 9, "r": v["a"] & 3}


_US_RESET = """module m (input clk, input rst_n, input [3:0] a, input [1:0] k, input b,
          output [3:0] o, output [3:0] p);
    typedef struct { logic [3:0] x; logic [3:0] y; } s_t;
    s_t r;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) r <= '{x: 4'h5, y: 4'ha};
        else begin r.x <= a; r.y <= r.x; r.y[k] <= b; end
    assign o = r.x; assign p = r.y;
endmodule
"""


def _us_reset_step(st: dict, v: dict) -> None:
    if not v["rst_n"]:
        st["x"], st["y"] = 5, 10
        return
    y = st.get("x", 0)
    y = y & ~(1 << v["k"]) | (v["b"] << v["k"])
    st["x"], st["y"] = v["a"], y


def _us_reset_out(st: dict, v: dict) -> dict[str, int]:
    if not v["rst_n"]:
        return {"o": 5, "p": 10}
    return {"o": st.get("x", 0), "p": st.get("y", 0)}


_US_PORT = """typedef struct { logic [3:0] x; logic [1:0] y; } s_t;
module sub (input s_t i, output logic [5:0] o); assign o = {i.y, i.x}; endmodule
module m (input [3:0] a, input [1:0] b, output [5:0] o, output s_t t);
    s_t s; assign s.x = a ^ 4'h3; assign s.y = b;
    sub u (.i(s), .o(o));
    assign t = s;
endmodule
"""


def test_unpacked_struct_members(tmp_path: Path, caplog) -> None:
    """Members of an unpacked struct are read and written like a packed
    struct's (rtl-buddy-cdc#408: each read was an opaque driver and each
    write was dropped): combinational and clocked, nested, as member arrays.
    The struct is laid out first member most significant, as Yosys does."""
    _check(tmp_path, caplog, _US_BASIC, (_us_basic_step, _us_basic_out), yosys=True)


@pytest.mark.parametrize(
    ("src", "model"),
    [
        (_US_NESTED, _us_nested),
        (_US_AGGREGATES, (_us_aggregates_step, _us_aggregates_out)),
        (_US_RESET, (_us_reset_step, _us_reset_out)),
        (
            _US_PORT,
            lambda a, b: {
                "o": (b << 4) | (a ^ 3),
                "t": ((a ^ 3) << 2) | b,
            },
        ),
    ],
    ids=["nested_and_member_array", "arrays_patterns_params", "reset", "ports"],
)
def test_unpacked_struct_shapes(tmp_path: Path, caplog, src: str, model) -> None:
    """Nested structs and member arrays, an array of structs written and
    read at a run-time index, assignment patterns, a struct parameter, a
    function returning a struct, an async-reset pattern, a run-time bit of
    a member as a target, a struct port of a child and of the top."""
    _check(tmp_path, caplog, src, model)


def test_unpacked_union_member_stays_reported(tmp_path: Path, caplog) -> None:
    src = """module m (input [3:0] a, output [3:0] o);
    typedef union { logic [3:0] x; logic [3:0] y; } u_t;
    u_t s;
    always_comb s.x = a;
    assign o = s.y;
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    msgs = _warnings(caplog)
    assert any("member of an unpacked" in m for m in msgs), msgs
    assert any("`s.y` not lowered" in m for m in msgs), msgs


# --- while / do-while / forever ------------------------------------------------

_WHILE_COMB = """module m (input [7:0] a, output logic [3:0] o, output logic [3:0] p,
          output logic [3:0] q, output logic [3:0] r, output logic [3:0] s);
    function automatic logic [3:0] lead(input logic [7:0] x);
        int i; i = 7;
        while (i >= 0 && !x[i]) i--;
        return i[3:0];
    endfunction
    always_comb begin
        int i, j, k;
        i = 0; o = 0;
        while (i < 4) begin o[i] = a[3 - i]; i++; end
        j = 0;
        while (j < 8 && !a[j]) j++;
        p = j;
        k = 0;
        do begin if (a[k]) break; k++; end while (k < 8);
        q = k;
        r = 1; i = 9;
        do r = r + a[3:0]; while (i < 2);
    end
    assign s = lead(a);
endmodule
"""


def _while_comb(a: int) -> dict[str, int]:
    first = _first1(a, 8)
    lead = next((i for i in range(7, -1, -1) if (a >> i) & 1), -1)
    return {
        "o": _reverse4(a & _M4),
        "p": 8 if first is None else first,
        "q": 8 if first is None else first,
        "r": (1 + a) & _M4,
        "s": lead & _M4,
    }


_WHILE_FF = """module m (input clk, input rst_n, input [7:0] a, output logic [3:0] q,
          output logic [3:0] r, output logic [2:0] s, output logic [3:0] t);
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) q <= 0;
        else begin
            automatic int i = 7;
            while (i >= 0 && !a[i]) i--;
            q <= i[3:0];
        end
    always_ff @(posedge clk) begin
        automatic int i = 0; automatic logic [3:0] n = 0;
        while (i < 8) begin i++; if (!a[i-1]) continue; n++; end
        r <= n;
    end
    always_ff @(posedge clk) begin
        automatic int i = 0;
        forever begin if (i == 4 || a[i]) break; i++; end
        s <= i;
    end
    always_ff @(posedge clk) begin
        automatic logic [3:0] f = a[3:0];
        while (f[0]) f = f >> 1;
        t <= f;
    end
endmodule
"""


def _while_ff_step(st: dict, v: dict) -> None:
    a = v["a"]
    if not v["rst_n"]:
        st["q"] = 0
    else:
        st["q"] = next((i for i in range(7, -1, -1) if (a >> i) & 1), -1) & _M4
    st["r"] = bin(a).count("1")
    st["s"] = next((i for i in range(4) if (a >> i) & 1), 4)
    f = a & _M4
    while f & 1:
        f >>= 1
    st["t"] = f


def _while_ff_out(st: dict, v: dict) -> dict[str, int]:
    out = {k: st.get(k, 0) for k in ("q", "r", "s", "t")}
    if not v["rst_n"]:
        out["q"] = 0
    return out


def test_while_loops_unroll(tmp_path: Path, caplog) -> None:
    """``while`` / ``do … while`` / ``forever`` loops unroll while their
    state folds (rtl-buddy-cdc#408: reported and dropped): a counted loop,
    a run-time condition bounded by a counter (``i < 8 && !a[i]``, which
    never reads ``a[8]``), a ``break`` out of a ``do … while``, a body
    that runs once, a loop in an inlined function and in clocked blocks
    (with ``continue`` and a ``forever`` / ``break``), and a data loop
    whose value runs out of set bits."""
    _check(tmp_path, caplog, _WHILE_COMB, _while_comb)
    (tmp_path / "ff").mkdir()
    _check(tmp_path / "ff", caplog, _WHILE_FF, (_while_ff_step, _while_ff_out))


@pytest.mark.parametrize(
    "body",
    [
        "always_comb begin int i; i = 0; o = 0;"
        " while (i < n) begin o = o + a; i++; end end",
        "always_ff @(posedge clk) begin automatic int i = 0; r <= a;"
        " while (i < n) begin o <= o + a; i++; end end",
        # A write in the condition once the loop may have ended (after a
        # run-time ``break``) would still be made there.
        "always_comb begin int i; i = 0; o = 0; while (i++ < 8) begin"
        " if (n[0]) break; o = o + a; end end",
        # A counter that depends on the data: the state stops folding.
        "always_comb begin int i; i = 0; o = 0; while (i < 4) begin"
        " case (a[i[1:0]]) 1'b1: begin i = i + 1; continue; end"
        " default: o = o + 1; endcase i++; end end",
    ],
    ids=[
        "comb_runtime_bound",
        "clocked_runtime_bound",
        "write_in_condition",
        "data_dependent_counter",
    ],
)
def test_while_without_a_fixed_trip_count_is_undone_and_reported(
    tmp_path: Path, caplog, body: str
) -> None:
    """A loop that cannot end at elaboration time is reported, and what
    its iterations lowered is undone: ``o`` has no driver, ``r`` keeps its
    flop."""
    src = f"""module m (input clk, input [3:0] a, input [3:0] n, output logic [3:0] o,
          output logic [3:0] r);
    {body}
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    msgs = _warnings(caplog)
    assert len(msgs) == 1 and "`while` loop not lowered (its " in msgs[0], msgs
    driven = {
        b
        for c in module.cells.values()
        for p, bits in c.connections.items()
        if p in ("Y", "Q")
        for b in bits
    }
    assert not driven & set(module.ports["o"].bits)
    assert bool(driven & set(module.ports["r"].bits)) == ("r <= a" in body)


# --- break / continue in a clocked loop --------------------------------------

_CLOCKED_BREAK = """module m (input clk, input [7:0] a, input [3:0] d, output logic [3:0] q,
          output logic [3:0] n, output logic [4:0] w, output logic [15:0] r,
          output logic [3:0] c);
    logic [3:0] arr [4];
    always_ff @(posedge clk) begin
        q <= 4'hf;
        for (int i = 0; i < 8; i++) if (a[i]) begin q <= i; break; end
    end
    always_ff @(posedge clk) begin
        automatic logic [3:0] k = 0;
        for (int i = 0; i < 8; i++) begin if (!a[i]) continue; k = k + 1; end
        n <= k;
    end
    always_ff @(posedge clk) begin
        automatic logic [4:0] k = 0;
        for (int i = 0; i < 2; i++)
            for (int j = 0; j < 4; j++) begin if (a[i*4+j]) break; k = k + 1; end
        w <= k;
    end
    always_ff @(posedge clk)
        for (int i = 0; i < 4; i++) begin if (a[i]) break; arr[i] <= d + i; end
    assign r = {arr[3], arr[2], arr[1], arr[0]};
    always_ff @(posedge clk) begin
        c <= 4'hf;
        for (int i = 0; i < 4; i++)
            case (a[2*i +: 2]) 2'd3: begin c <= i; break; end default: ; endcase
    end
endmodule
"""


def _clocked_break_step(st: dict, v: dict) -> None:
    a, d = v["a"], v["d"]
    first = _first1(a, 8)
    st["q"] = 15 if first is None else first
    st["n"] = bin(a).count("1")
    w = 0
    for i in range(2):
        for j in range(4):
            if (a >> (i * 4 + j)) & 1:
                break
            w += 1
    st["w"] = w
    arr = st.setdefault("arr", [0] * 4)
    for i in range(4):
        if (a >> i) & 1:
            break
        arr[i] = (d + i) & _M4
    st["c"] = next((i for i in range(4) if (a >> 2 * i) & 3 == 3), 15)


def _clocked_break_out(st: dict, v: dict) -> dict[str, int]:
    arr = st.get("arr", [0] * 4)
    out = {k: st.get(k, 0) for k in ("q", "n", "w", "c")}
    out["r"] = sum(x << (4 * i) for i, x in enumerate(arr))
    return out


def test_break_and_continue_in_clocked_loops(tmp_path: Path, caplog) -> None:
    """``break`` / ``continue`` in a clocked loop gate what follows them
    through the block's decision tree (rtl-buddy-cdc#408: reported once per
    unrolled iteration and dropped, so every later write was
    unconditional): a priority encoder, a counter, nested loops, register
    array writes, a ``break`` inside a ``case``."""
    _check(tmp_path, caplog, _CLOCKED_BREAK, (_clocked_break_step, _clocked_break_out))


def test_unlowered_statement_in_an_unrolled_loop_warns_once(
    tmp_path: Path, caplog
) -> None:
    src = """module m (input clk, input [7:0] a, output logic [3:0] q);
    always_ff @(posedge clk)
        for (int i = 0; i < 8; i++) if (a[i]) begin q <= i; disable fork; end
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    msgs = _warnings(caplog)
    assert len(msgs) == 1 and "`DisableForkStatement`" in msgs[0], msgs


# --- operators -----------------------------------------------------------------

_OPERATORS = """module m (input clk, input [3:0] a, input [3:0] b, input c,
          output o1, output o2, output logic [3:0] o, output logic [3:0] p,
          output logic [3:0] t2, output logic [3:0] e, output logic [3:0] f,
          output logic [3:0] q, output logic [3:0] r, output logic [3:0] s,
          output logic g);
    assign o1 = a -> b;
    assign o2 = a <-> b;
    always_comb begin
        automatic logic [3:0] t = a;
        o = t++; p = ++t; t2 = t--; t2 = t2 + --t;
    end
    always_comb begin
        automatic logic [3:0] t = a;
        e = (t += b) + 1; f = t;
        if ((t = b) != 0) f = f ^ t;
    end
    always_ff @(posedge clk) begin
        automatic logic [3:0] t = a;
        q <= t++;
        r <= ++t + t--;
        s <= t;
        if (b -> c) g <= c;
    end
endmodule
"""


def _operators_comb(v: dict) -> dict[str, int]:
    a, b = v["a"], v["b"]
    t = a
    o = t
    t = (t + 2) & _M4
    p = t
    t2 = t
    t = (t - 2) & _M4
    t2 = (t2 + t) & _M4
    e_t = (a + b) & _M4
    f = e_t ^ b if b else e_t
    return {
        "o1": int(not a or bool(b)),
        "o2": int(bool(a) == bool(b)),
        "o": o,
        "p": p,
        "t2": t2,
        "e": (e_t + 1) & _M4,
        "f": f,
    }


def _operators_step(st: dict, v: dict) -> None:
    t = v["a"]
    st["q"] = t
    t = (t + 2) & _M4
    st["r"] = (2 * t) & _M4
    st["s"] = (t - 1) & _M4
    if not v["b"] or v["c"]:
        st["g"] = v["c"]


def _operators_out(st: dict, v: dict) -> dict[str, int]:
    return {
        **_operators_comb(v),
        **{k: st.get(k, 0) for k in ("q", "r", "s", "g")},
    }


def test_implication_increments_and_assignment_values(tmp_path: Path, caplog) -> None:
    """``->`` / ``<->`` (rtl-buddy-cdc#408: opaque) and the writes inside
    an expression: ``t++`` / ``++t`` / ``t--`` / ``--t`` and an assignment
    used as a value, in combinational and clocked blocks."""
    _check(tmp_path, caplog, _OPERATORS, (_operators_step, _operators_out))


@pytest.mark.parametrize(
    "stmt",
    [
        "p = c && (t++ != 0);",
        "p = c ? t++ : t--;",
        "p = c || f(a, t);",
    ],
    ids=["and", "ternary", "or_output_argument"],
)
def test_write_in_a_conditional_operand_is_reported(
    tmp_path: Path, caplog, stmt: str
) -> None:
    """A write inside an operand evaluated only on some condition would
    happen whatever the condition: it is reported, not lowered."""
    src = f"""module m (input [3:0] a, input c, output logic [3:0] o, output logic p);
    function automatic logic f(input logic [3:0] x, output logic [3:0] y);
        y = ~x; return x[0];
    endfunction
    always_comb begin automatic logic [3:0] t = a; {stmt} o = t; end
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    assert any("not lowered" in m for m in _warnings(caplog)), _warnings(caplog)


# --- enum and array methods ----------------------------------------------------

_METHODS = """module m (input clk, input rst_n, input go, input [1:0] a, input [2:0] b,
          input [3:0] x, input [3:0] y, output [1:0] o, output [1:0] p,
          output [2:0] r, output [1:0] f, output [1:0] st, output [3:0] sm,
          output [3:0] pr, output [3:0] an, output [3:0] orr, output [3:0] xr);
    typedef enum logic [1:0] { A = 2'd1, B = 2'd2, C = 2'd0 } e_t;
    typedef enum logic [2:0] { S0, S1, S2, S3, S4 } s_t;
    e_t e, fsm; s_t s;
    assign e = e_t'(a); assign s = s_t'(b);
    assign o = e.next();
    assign p = e.prev(2);
    assign r = s.prev(3);
    assign f = e.first() ^ e.last() ^ e.num();
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) fsm <= A; else if (go) fsm <= fsm.next();
    assign st = fsm;
    logic [3:0] arr [3];
    assign arr[0] = x; assign arr[1] = y; assign arr[2] = x ^ y;
    assign sm = arr.sum(); assign pr = arr.product(); assign an = arr.and();
    assign orr = arr.or(); assign xr = arr.xor();
endmodule
"""

_E = [1, 2, 0]


def _enum_step(value: int, n: int) -> int:
    return _E[(_E.index(value) + n) % 3] if value in _E else 0


def _methods_out(st: dict, v: dict) -> dict[str, int]:
    a, b, x, y = v["a"], v["b"], v["x"], v["y"]
    z = x ^ y
    return {
        "o": _enum_step(a, 1),
        "p": _enum_step(a, -2),
        "r": (b - 3) % 5 if b < 5 else 0,
        "f": 1 ^ 0 ^ 3,
        "st": 1 if not v["rst_n"] else st.get("fsm", 0),
        "sm": (x + y + z) & _M4,
        "pr": (x * y * z) & _M4,
        "an": x & y & z,
        "orr": x | y | z,
        "xr": x ^ y ^ z,
    }


def _methods_step(st: dict, v: dict) -> None:
    if not v["rst_n"]:
        st["fsm"] = 1
    elif v["go"]:
        st["fsm"] = _enum_step(st.get("fsm", 0), 1)


def test_enum_and_array_methods(tmp_path: Path, caplog) -> None:
    """``e.next(N)`` / ``e.prev(N)`` step through the members in
    declaration order, wrapping, ``x`` for a value that is no member
    (rtl-buddy-cdc#408: opaque); ``first`` / ``last`` / ``num`` fold;
    ``sum`` / ``product`` / ``and`` / ``or`` / ``xor`` reduce an unpacked
    array at its element width."""
    _check(tmp_path, caplog, _METHODS, (_methods_step, _methods_out))


@pytest.mark.parametrize(
    "expr",
    ['e.name() == "B"', "e.next(n)", "$countbits(a, n[0])"],
    ids=["enum_name", "runtime_step", "runtime_control_bit"],
)
def test_methods_and_functions_left_opaque_are_reported(
    tmp_path: Path, caplog, expr: str
) -> None:
    src = f"""module m (input [1:0] a, input [1:0] n, output [1:0] o);
    typedef enum logic [1:0] {{ A, B, C }} e_t;
    e_t e; assign e = e_t'(a);
    assign o = {expr};
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    assert any(f"`{expr}` not lowered" in m for m in _warnings(caplog))


# --- inside over an unpacked array ---------------------------------------------

_INSIDE = """module m (input clk, input [3:0] a, input [3:0] b, input [1:0] c,
          input signed [7:0] d, output o, output p, output w, output s,
          output logic [1:0] k, output logic [1:0] q);
    logic [3:0] arr [3];
    assign arr[0] = b; assign arr[1] = 4'd7; assign arr[2] = b + 1;
    assign o = a inside {arr};
    assign p = a inside {arr, 4'd12, [1:2]};
    localparam logic [3:0] P [2] = '{4'b1x0x, 4'd9};
    assign w = a inside {P};
    logic signed [3:0] sarr [2];
    assign sarr[0] = b; assign sarr[1] = -4'sd2;
    assign s = d inside {sarr};
    logic [3:0] lab [2];
    assign lab[0] = b; assign lab[1] = 4'd3;
    always_comb case (a) inside lab: k = 2'd1; [8:9]: k = 2'd2; default: k = 2'd3; endcase
    always_ff @(posedge clk)
        case (a) inside lab: q <= 2'd1; [8:9]: q <= 2'd2; default: q <= 2'd3; endcase
endmodule
"""


def _inside_case(a: int, b: int) -> int:
    return 1 if a in (b, 3) else 2 if a in (8, 9) else 3


def _inside_out(st: dict, v: dict) -> dict[str, int]:
    a, b, d = v["a"], v["b"], v["d"]
    elements = (b, 7, (b + 1) & _M4)
    return {
        "o": int(a in elements),
        "p": int(a in elements + (12, 1, 2)),
        "w": int((a & 0b1010) == 0b1000 or a == 9),
        "s": int(_sx(d, 8) in (_sx(b, 4), -2)),
        "k": _inside_case(a, b),
        "q": st.get("q", 0),
    }


def test_inside_an_unpacked_array(tmp_path: Path, caplog) -> None:
    """An unpacked array in an ``inside`` list stands for its elements,
    each compared with ``==?`` (rtl-buddy-cdc#408: an opaque driver), signed
    elements sign-extended; as a ``case … inside`` label it was compared as
    one value — a silently wrong match."""
    _check(
        tmp_path,
        caplog,
        _INSIDE,
        (lambda st, v: st.update(q=_inside_case(v["a"], v["b"])), _inside_out),
    )


# --- function output arguments inside an expression ----------------------------

_FN_OUT = """module m (input clk, input [3:0] a, input [3:0] b, output logic [3:0] o,
          output logic [3:0] p, output logic [3:0] g, output logic [3:0] h,
          output logic [3:0] q, output logic [3:0] r, output logic [1:0] u,
          output logic [1:0] v, output logic w);
    function automatic logic [3:0] f(input logic [3:0] x, output logic [3:0] hi);
        hi = x & 4'hc; return x & 4'h3;
    endfunction
    function automatic logic [3:0] acc(input logic [3:0] x, inout logic [3:0] s);
        s = s + x; return s ^ x;
    endfunction
    function automatic logic par(input logic [3:0] x, output logic [3:0] y);
        y = ~x; return ^x;
    endfunction
    always_comb o = f(a, p) + 1;
    always_comb begin h = b; g = acc(a, h); end
    always_ff @(posedge clk) begin
        automatic logic [3:0] t;
        q <= f(a, t) ^ f(b, r);
    end
    always_comb w = par(a, {u, v});
endmodule
"""


def _fn_out(st: dict, v: dict) -> dict[str, int]:
    a, b = v["a"], v["b"]
    h = (a + b) & _M4
    return {
        "o": ((a & 3) + 1) & _M4,
        "p": a & 12,
        "g": h ^ a,
        "h": h,
        "q": st.get("q", 0),
        "r": st.get("r", 0),
        "u": (~a & _M4) >> 2,
        "v": ~a & 3,
        "w": bin(a).count("1") & 1,
    }


def test_function_output_arguments_in_an_expression(tmp_path: Path, caplog) -> None:
    """A function with ``output`` / ``inout`` arguments called inside an
    expression is inlined and its arguments written back (rtl-buddy-cdc#408:
    an opaque driver), also two calls in one expression and a
    concatenation as the actual."""
    _check(
        tmp_path,
        caplog,
        _FN_OUT,
        (
            lambda st, v: st.update(q=(v["a"] & 3) ^ (v["b"] & 3), r=v["b"] & 12),
            _fn_out,
        ),
    )


# --- run-time $clog2 / $countbits ----------------------------------------------

_SYSF = """module m (input [7:0] a, input signed [3:0] s, output [3:0] o,
          output [3:0] p, output [3:0] q, output [3:0] r, output [3:0] z,
          output [3:0] c);
    assign o = $clog2(a);
    assign p = $countbits(a, 1'b1);
    assign q = $countbits(a, 1'b0);
    assign r = $countbits(a, 1'b0, 1'b1);
    assign z = $countbits(a, 1'bx);
    assign c = $clog2(s);
endmodule
"""


def _clog2(value: int) -> int:
    return 0 if value <= 1 else (value - 1).bit_length()


def test_runtime_clog2_and_countbits(tmp_path: Path, caplog) -> None:
    """A run-time ``$clog2`` / ``$countbits`` lowers to logic that computes
    it and keeps the argument's fan-in (rtl-buddy-cdc#408: an opaque
    driver)."""
    module = _check(
        tmp_path,
        caplog,
        _SYSF,
        lambda a, s: {
            "o": _clog2(a),
            "p": bin(a).count("1"),
            "q": 8 - bin(a).count("1"),
            "r": 8,
            "z": 0,
            "c": _clog2(s),
        },
    )
    # Every bit of ``a`` reaches ``o``.
    drivers = {b: c for c in module.cells.values() for b in c.connections.get("Y", ())}
    seen: set[Bit] = set()
    todo = list(module.ports["o"].bits)
    while todo:
        bit = todo.pop()
        if bit in seen:
            continue
        seen.add(bit)
        cell = drivers.get(bit)
        if cell is not None:
            todo.extend(
                b for p, bits in cell.connections.items() if p != "Y" for b in bits
            )
    assert set(module.ports["a"].bits) <= seen


# --- a run-time bit write after a whole-register write -------------------------

_REWRITE_REG = """module m (input clk, input [3:0] a, input [1:0] k, input b,
          output logic [3:0] o, output logic [3:0] p);
    always_ff @(posedge clk) begin o <= a; p <= o; p[k] <= b; end
endmodule
"""

_REWRITE_ARRAY = """module m (input clk, input [3:0] a, input [1:0] k, input b, input i,
          output logic [3:0] r0, output logic [3:0] r1);
    logic [3:0] arr [2];
    always_ff @(posedge clk) begin arr[i] <= a; arr[i][k] <= b; end
    assign r0 = arr[0]; assign r1 = arr[1];
endmodule
"""


def _with_bit(value: int, k: int, b: int) -> int:
    return value & ~(1 << k) | (b << k)


def _rewrite_reg_step(st: dict, v: dict) -> None:
    st["p"] = _with_bit(st.get("o", 0), v["k"], v["b"])
    st["o"] = v["a"]


def _rewrite_array_step(st: dict, v: dict) -> None:
    st["r%d" % v["i"]] = _with_bit(v["a"], v["k"], v["b"])


def test_runtime_bit_write_after_a_whole_write(tmp_path: Path, caplog) -> None:
    """``p <= o; p[k] <= b;`` loads ``o`` with bit ``k`` replaced: the
    run-time bit write writes only its bit (rtl-buddy-cdc#408: it loaded
    the register's own value into every other bit, a silently wrong
    netlist the construct sweep found); the same for a register array's
    element. Yosys keeps the array a memory, so only the register is
    checked against it."""
    _check(
        tmp_path,
        caplog,
        _REWRITE_REG,
        (_rewrite_reg_step, lambda st, v: {k: st.get(k, 0) for k in ("o", "p")}),
        yosys=True,
    )
    (tmp_path / "array").mkdir()
    _check(
        tmp_path / "array",
        caplog,
        _REWRITE_ARRAY,
        (_rewrite_array_step, lambda st, v: {k: st.get(k, 0) for k in ("r0", "r1")}),
    )


# --- the fold behind while-loop unrolling -------------------------------------

_FOLDS = """module m (input [3:0] a, output logic [3:0] o1, output logic [3:0] o2,
          output logic [7:0] o3, output logic [3:0] o4, output logic [3:0] o5,
          output logic [3:0] o6, output logic [3:0] o7, output logic o8,
          output logic [3:0] o9);
    always_comb begin
        int i, j, k, t, n;
        logic signed [7:0] sv;
        logic [7:0] k2;
        // Every operator the fold evaluates, on a constant counter.
        i = 0; o1 = 0;
        while (((i << 1) >> 1) < 4 && (i ^ 1) != 9 && (i | 0) == (i & 15)
               && -i > -4 && ~i != 0 && !(i == 9) && (i * 2 - i + 0) <= 3
               && (i ~^ i) != 0 && (|i || 1) && (i >= 0 && i != 9)
               && (a[0] ? i : i) < 4 && (^i || !(^i)) && (&i == 0 || i == 3)
               && i < 4) begin
            o1 = o1 + a; i++;
        end
        // A negative signed counter.
        j = -3; o2 = 0;
        while (j < 0) begin o2 = o2 + a[j + 3]; j++; end
        // Run-time shifts by a constant amount.
        t = 1; k = 1; k2 = 8'h80; sv = -8'sd128; o3 = 0;
        while (k < 16) begin k = k << t; k2 = k2 >> t; sv = sv >>> t; o3 = o3 + 1; end
        o3 = o3 ^ k2 ^ sv;
        // A multi-bit condition that runs out of set bits.
        n = 3; o4 = 0;
        while (n) begin o4 = o4 + a; n = n - 1; end
        // A break under an inverted condition, then a constant exit.
        i = 0; o5 = 0;
        while (1 && i < 4) begin if (!a[i]) o5 = o5 + 1; else break; i++; end
        // Run-time on both sides of an ``&&``, and on the left only.
        i = 0; o6 = 0;
        while (i < 4 && !a[i] && !a[3 - i]) begin o6++; i++; end
        i = 0; o7 = 0;
        while (!a[i] && i < 3) begin o7++; i++; end
        // A constant deciding ``&&`` whose other side does not lower.
        o8 = 1'b0 && ($urandom(a) != 0);
        // ``&&`` of two constants that are not loop state.
        t = 1; i = 0; o9 = 0;
        while (i < 2 && (t && t)) begin o9 = o9 + a; i++; end
    end
endmodule
"""


def _folds(a: int) -> dict[str, int]:
    o5 = 0
    for i in range(4):
        if (a >> i) & 1:
            break
        o5 += 1
    k2, sv = 0x80, -128
    for _ in range(4):
        k2 >>= 1
        sv >>= 1
    o6 = next((i for i in range(4) if (a >> i) & 1 or (a >> (3 - i)) & 1), 4)
    o7 = next((i for i in range(3) if (a >> i) & 1), 3)
    return {
        "o6": o6,
        "o7": o7,
        "o8": 0,
        "o9": (2 * a) & _M4,
        "o1": (4 * a) & _M4,
        "o2": bin(a & 7).count("1"),
        "o3": (4 ^ k2 ^ (sv & 0xFF)) & 0xFF,
        "o4": (3 * a) & _M4,
        "o5": o5,
    }


def test_while_loops_fold_every_operator(tmp_path: Path, caplog) -> None:
    """The fold that decides when an unrolled loop ends evaluates each
    cell kind its counter passes through with Yosys semantics: signed and
    unsigned compares, arithmetic, bitwise and reduction operators, shifts
    by a value, a select whose two legs agree."""
    _check(tmp_path, caplog, _FOLDS, _folds)


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        # The condition does not lower.
        ("while ($urandom(a) != 0) o = o + 1;", "its condition does not lower"),
        # A multi-bit run-time condition never folds.
        ("while (a) o = o + 1;", "its trip count"),
        ("forever begin if (a[0]) break; o = o + 1; end", "`forever` loop"),
        ("do o = o + 1; while (a != 0);", "`do … while` loop"),
        ("o = $clog2($urandom(a));", "`$clog2($urandom(a))` not lowered"),
        ("o = $countbits($urandom(a), 1'b1);", "not lowered"),
        ("o = $urandom(a) -> a[0];", "not lowered"),
    ],
    ids=["condition", "multibit", "forever", "do_while", "clog2", "countbits", "imply"],
)
def test_loops_without_a_fixed_trip_count_name_their_kind(
    tmp_path: Path, caplog, body: str, needle: str
) -> None:
    src = f"""module m (input [3:0] a, output logic [3:0] o);
    always_comb begin o = 0; {body} end
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    msgs = _warnings(caplog)
    assert any(needle in m for m in msgs), msgs


def test_clocked_break_known_at_elaboration(tmp_path: Path, caplog) -> None:
    """An unconditional ``break`` ends a clocked loop at elaboration time;
    one under a run-time condition followed by a constant one leaves the
    remaining iterations out."""
    src = """module m (input clk, input [3:0] a, output logic [3:0] q, output logic [3:0] r);
    always_ff @(posedge clk) begin
        q <= 0;
        for (int i = 0; i < 4; i++) begin q <= i; break; q <= 4'hf; end
        r <= 0;
        for (int i = 0; i < 4; i++) begin if (a[i]) break; r <= i + 1; if (i == 1) break; end
    end
endmodule
"""

    def step(st: dict, v: dict) -> None:
        r = 0
        for i in range(4):
            if (v["a"] >> i) & 1:
                break
            r = i + 1
            if i == 1:
                break
        st.update(q=0, r=r)

    _check(tmp_path, caplog, src, (step, lambda st, v: {k: st.get(k, 0) for k in "qr"}))


# --- guards on shapes that do not lower ----------------------------------------


def _bare_builder() -> Any:
    return slang_fe._ModuleBuilder(top_inst=None, compilation=None, pyslang=None)


class _Node:
    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


def test_member_span_guards() -> None:
    span = slang_fe._ModuleBuilder._member_span
    packed = _Node(isIntegral=True)
    assert span(_Node(value=_Node(type=packed), member=None)) is None
    assert span(_Node(value=None, member=_Node())) is None
    no_offset = _Node(bitOffset=None, type=_Node(bitWidth=4))
    assert span(_Node(value=_Node(type=packed), member=no_offset)) is None
    union = _Node(isIntegral=False, isUnpackedStruct=False)
    assert span(_Node(value=_Node(type=union), member=_Node(name="f"))) is None


def test_fold_guards() -> None:
    b = _bare_builder()
    # An x digit, an undriven net and a cell the fold has no rule for.
    assert b._fold_bits(("x",)) is None
    assert b._fold_bits((5,)) is None
    b._cells["c"] = Cell(
        name="c",
        type="$div",
        connections={"A": ("1",), "B": ("1",), "Y": (9,)},
        parameters={},
        attributes={},
    )
    assert b._fold_bits((9,)) is None
    # Only a loop's state variables fold.
    assert b._folded_var(_Node(name="v")) is None
    # Assumptions follow an OR at 0, an AND at 1 and an inverter.
    for n, (kind, value, inputs) in enumerate(
        (("$or", 0, ("A", "B")), ("$and", 1, ("A", "B")), ("$not", 1, ("A",)))
    ):
        bits = {pin: (100 + 10 * n + k,) for k, pin in enumerate(inputs)}
        b._cells[kind] = Cell(
            name=kind,
            type=kind,
            connections={**bits, "Y": (200 + n,)},
            parameters={},
            attributes={},
        )
        b._fold_assume = {}
        b._assume(200 + n, value)
        want = 1 - value if kind == "$not" else value
        assert all(b._fold_assume[bit[0]] == want for bit in bits.values())


def test_method_and_function_guards(tmp_path: Path) -> None:
    b = _bare_builder()
    not_enum = _Node(type=_Node(canonicalType=_Node(isEnum=False)))
    assert b._lower_enum_step(_Node(), [not_enum], 1) is None
    not_array = _Node(type=_Node(canonicalType=_Node(isUnpackedArray=False)))
    assert b._lower_array_reduction(_Node(), not_array, "sum") is None
    assert b._lower_step(_Node(), "Postincrement") is None  # no walk in progress
    assert b._lower_assignment_value(_Node()) is None
    b._expr_writer = lambda write: None
    assert b._lower_assignment_value(_Node(isNonBlocking=True)) is None


class FieldSymbol:  # noqa: D101 - named as pyslang names a struct member
    def __init__(self, name: str, width: int) -> None:
        self.name = name
        self.type = _Node(selectableWidth=width)


class EnumValueSymbol:  # noqa: D101 - named as pyslang names an enum member
    def __init__(self, value: Any) -> None:
        self.value = value


class _Scope(_Node):
    def __init__(self, members: list[Any], **attrs: Any) -> None:
        super().__init__(**attrs)
        self.members = members

    def __iter__(self):
        return iter(self.members)


def test_aggregate_guards() -> None:
    b = _bare_builder()
    span = slang_fe._ModuleBuilder._member_span
    empty = _Scope([FieldSymbol("f", 0)], isIntegral=False, isUnpackedStruct=True)
    assert span(_Node(value=_Node(type=empty), member=_Node(name="f"))) is None
    other = _Scope([FieldSymbol("f", 2)], isIntegral=False, isUnpackedStruct=True)
    assert span(_Node(value=_Node(type=other), member=_Node(name="g"))) is None
    # A struct value that does not match its type.
    assert b._unpacked_value_bits(_Node(value=1), other) is None
    assert b._unpacked_value_bits(_Node(value=[None]), other) is None
    # Enum members whose values do not lower, or none at all.
    for members in ([EnumValueSymbol(None)], [_Node()]):
        etype = _Scope(members, isEnum=True)
        arg = _Node(type=_Node(canonicalType=etype))
        b._bits_of_expression = lambda e: ("1",)
        assert b._lower_enum_step(_Node(type=_Node(bitWidth=1)), [arg], 1) is None
    # An array reduction over bits that do not match the array.
    atype = _Node(isUnpackedArray=True, range=_Node(left=0, right=1), elementType=None)
    arr = _Node(type=_Node(canonicalType=atype))
    assert b._lower_array_reduction(_Node(type=_Node(bitWidth=1)), arr, "or") is None


def test_driver_index_skips_a_deleted_cell() -> None:
    b = _bare_builder()
    b._cells["c"] = Cell(
        name="c",
        type="$not",
        connections={"A": ("1",), "Y": (9,)},
        parameters={},
        attributes={},
    )
    assert b._fold_bits((9,)) == 0
    del b._cells["c"]
    assert b._driver_of(9) is None
    # A select neither leg decides adds nothing past the select's output.
    b._cells["m"] = Cell(
        name="m",
        type="$mux",
        connections={"A": (20,), "B": (21,), "S": (22,), "Y": (23,)},
        parameters={},
        attributes={},
    )
    b._fold_assume = {}
    b._assume(23, 1)
    assert b._fold_assume == {23: 1}
