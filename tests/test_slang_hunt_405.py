"""slang frontend: wrong-netlist gaps found by a systematic cross-frontend
simulation sweep (rtl-buddy-cdc#405).

Small designs were elaborated on both frontends and the two netlists
simulated on the same random stimulus. Each section here is one gap that
lost or invented fan-in (or computed a different function) on slang,
checked on the slang netlist alone by simulation against the
SystemVerilog semantics and, where yosys is on PATH, against the
``lint`` recipe's netlist. CI's pytest job has no yosys; there the
slang-side assertions stand alone.
"""

from __future__ import annotations

import importlib.util
import itertools
import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.netlist import Bit, Cell, Module

if importlib.util.find_spec("pyslang") is None:
    pytest.skip("pyslang not installed", allow_module_level=True)

from rtl_buddy_cdc.frontends import slang as slang_fe  # noqa: E402

_HAVE_YOSYS = shutil.which("yosys") is not None
_SLANG_LOG = "rtl_buddy_cdc.frontends.slang"


def _write(tmp_path: Path, src: str, name: str) -> Path:
    sv = tmp_path / name
    sv.write_text(src)
    return sv


def _slang(tmp_path: Path, src: str) -> Module:
    module, _ = slang_fe.elaborate_with_blackboxes([_write(tmp_path, src, "s.sv")], "m")
    return module


def _yosys(tmp_path: Path, src: str) -> Module:
    """The ``lint`` recipe, from a script file: CI's yosys 0.52 splits a
    ``-p`` script at commas."""
    sv = _write(tmp_path, src, "y.sv")
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


# --- a combinational netlist evaluator, signed operands included -------------


def _flag(cell: Cell, name: str) -> bool:
    return "1" in (cell.parameters.get(name) or "0")


def _signed(value: int, width: int, signed: bool) -> int:
    value &= (1 << width) - 1 if width else 0
    if signed and width and value >> (width - 1):
        value -= 1 << width
    return value


def _cell_value(cell: Cell, num: Callable[[tuple[Bit, ...]], int]) -> int:
    """``Y`` of a combinational cell by the Yosys cell semantics."""
    c = cell.connections
    width = len(c["Y"])
    mask = (1 << width) - 1
    if cell.type == "$mux":
        return num(c["B"] if num(c["S"]) else c["A"])
    if cell.type == "$pmux":
        for k, s in enumerate(c["S"]):
            if num((s,)):
                return num(c["B"][k * width : (k + 1) * width])
        return num(c["A"])
    a_bits, b_bits = c.get("A", ()), c.get("B", ())
    ua, ub = num(a_bits), num(b_bits)
    a_signed, b_signed = _flag(cell, "A_SIGNED"), _flag(cell, "B_SIGNED")
    a = _signed(ua, len(a_bits), a_signed)
    b = _signed(ub, len(b_bits), b_signed)
    if cell.type in ("$lt", "$le", "$gt", "$ge", "$eq", "$ne", "$div", "$mod"):
        if not (a_signed and b_signed):
            a, b = ua, ub
    full = max(len(a_bits), len(b_bits), width)
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
        "$reduce_xnor": lambda: 1 - (bin(ua).count("1") & 1),
        "$logic_and": lambda: int(ua != 0 and ub != 0),
        "$logic_or": lambda: int(ua != 0 or ub != 0),
        "$eq": lambda: int(a == b),
        "$ne": lambda: int(a != b),
        "$eqx": lambda: int(a == b),
        "$nex": lambda: int(a != b),
        "$lt": lambda: int(a < b),
        "$le": lambda: int(a <= b),
        "$gt": lambda: int(a > b),
        "$ge": lambda: int(a >= b),
        "$add": lambda: a + b,
        "$sub": lambda: a - b,
        "$mul": lambda: a * b,
        "$div": lambda: (
            0 if b == 0 else int(abs(a) // abs(b)) * (1 if (a < 0) == (b < 0) else -1)
        ),
        "$mod": lambda: 0 if b == 0 else (abs(a) % abs(b)) * (-1 if a < 0 else 1),
        "$shl": lambda: a << ub,
        "$sshl": lambda: a << ub,
        "$shr": lambda: (a & ((1 << full) - 1)) >> ub,
        "$sshr": lambda: a >> ub,
    }
    return ops[cell.type]() & mask


def _outputs(module: Module, values: dict[str, int], names: tuple[str, ...]) -> dict:
    env: dict[Bit, int] = {}
    for name, value in values.items():
        for k, bit in enumerate(module.ports[name].bits):
            env[bit] = (value >> k) & 1
    comb = {
        bit: (c, k)
        for c in module.cells.values()
        if "Y" in c.connections
        for k, bit in enumerate(c.connections["Y"])
    }
    memo: dict[str, int] = {}

    def get(bit: Bit) -> int:
        if isinstance(bit, str):
            return int(bit == "1")
        if bit in env:
            return env[bit]
        if bit not in comb:
            return 0
        cell, lane = comb[bit]
        if cell.name not in memo:
            memo[cell.name] = _cell_value(cell, num)
        return (memo[cell.name] >> lane) & 1

    def num(bits: tuple[Bit, ...]) -> int:
        return sum(get(b) << k for k, b in enumerate(bits))

    return {n: num(module.ports[n].bits) for n in names}


def _check(
    tmp_path: Path,
    src: str,
    inputs: tuple[str, ...],
    outs: tuple[str, ...],
    model: Callable[..., dict[str, int]],
) -> Module:
    """Every output equals ``model`` for every input combination, on the
    slang netlist and (when available) the Yosys one."""
    module = _slang(tmp_path, src)
    y_mod = _yosys(tmp_path, src) if _HAVE_YOSYS else None
    widths = [len(module.ports[n].bits) for n in inputs]
    for combo in itertools.product(*(range(1 << w) for w in widths)):
        values = dict(zip(inputs, combo))
        want = model(**values)
        assert _outputs(module, values, outs) == want, values
        if y_mod is not None:
            assert _outputs(y_mod, values, outs) == want, values
    return module


def _sx(value: int, width: int) -> int:
    return _signed(value, width, True)


# --- signed operands ---------------------------------------------------------

_SIGNED = """module m (input signed [3:0] a, input signed [3:0] b, input [2:0] s,
          output [7:0] o, output [7:0] p, output [7:0] q, output lt,
          output ltu, output [7:0] sh, output [7:0] r, output [3:0] dv,
          output [7:0] t);
    assign o = a * b;
    assign p = a + $unsigned(b);
    assign q = s[0] ? a : 4'sd1;
    assign lt = a < b;
    assign ltu = a < $unsigned(b);
    assign sh = (a + 8'sd0) >>> s;
    assign r = a;
    assign dv = b == 0 ? 4'sd0 : a / b;
    assign t = {a, a} + a;
endmodule
"""


def _signed_model(a: int, b: int, s: int) -> dict[str, int]:
    sa, sb = _sx(a, 4), _sx(b, 4)
    dv = 0
    if sb:
        dv = abs(sa) // abs(sb) * (1 if (sa < 0) == (sb < 0) else -1)
    return {
        "o": (sa * sb) & 0xFF,
        "p": (a + b) & 0xFF,
        "q": (sa if s & 1 else 1) & 0xFF,
        "lt": int(sa < sb),
        "ltu": int(a < b),
        "sh": (sa >> s) & 0xFF,
        "r": sa & 0xFF,
        "dv": dv & 0xF,
        "t": (((a << 4) | a) + a) & 0xFF,
    }


def test_signed_operands_extend_and_compare_signed(tmp_path: Path) -> None:
    """A signed operand widened inside an expression (``a * b`` and
    ``s ? a : 4'sd1`` into 8 bits) is sign-extended, so its MSB fans out
    to the upper result bits (it was zero-extended); a signed operand of an
    unsigned expression (``a + $unsigned(b)``, ``{a, a} + a``) is
    zero-extended; ``<``, ``>>>`` and ``/`` of signed operands are signed
    cells. Every output matches the SystemVerilog value for every input,
    and the Yosys netlist's."""
    module = _check(
        tmp_path,
        _SIGNED,
        ("a", "b", "s"),
        ("o", "p", "q", "lt", "ltu", "sh", "r", "dv", "t"),
        _signed_model,
    )
    # The MSB of ``a`` reaches every upper bit of ``q`` through the mux.
    (mux,) = [
        c
        for c in module.cells.values()
        if c.type == "$mux" and c.connections["Y"] == module.ports["q"].bits
    ]
    a3 = module.ports["a"].bits[3]
    assert mux.connections["B"][4:] == (a3,) * 4


# --- reduction operators ----------------------------------------------------

_REDUCE = """module m (input [3:0] a, output o1, output o2, output o3, output o4,
          output o5, output o6);
    assign o1 = &a; assign o2 = ~&a; assign o3 = |a;
    assign o4 = ~|a; assign o5 = ^a; assign o6 = ~^a;
endmodule
"""


def test_inverted_reductions_invert(tmp_path: Path) -> None:
    """``~&a``, ``~|a`` and ``~^a`` are the inverted reductions; they
    computed ``&a``, ``|a`` and ``^a``."""

    def model(a: int) -> dict[str, int]:
        par = bin(a).count("1") & 1
        return {
            "o1": int(a == 15),
            "o2": int(a != 15),
            "o3": int(a != 0),
            "o4": int(a == 0),
            "o5": par,
            "o6": 1 - par,
        }

    _check(tmp_path, _REDUCE, ("a",), ("o1", "o2", "o3", "o4", "o5", "o6"), model)


# --- a vector condition --------------------------------------------------------

_VEC_COND = """module m (input [3:0] c, input [1:0] a, input [1:0] b, output [1:0] o);
    assign o = c ? a : b;
endmodule
"""


def test_vector_ternary_condition_reads_every_bit(tmp_path: Path) -> None:
    """``c ? a : b`` with a 4-bit ``c`` selects on ``c != 0`` (a
    ``$reduce_bool``, as Yosys builds it); it selected on ``c[0]``, so
    ``c[3:1]`` had no fan-in to ``o``."""
    module = _check(
        tmp_path,
        _VEC_COND,
        ("c", "a", "b"),
        ("o",),
        lambda c, a, b: {"o": a if c else b},
    )
    (rb,) = [x for x in module.cells.values() if x.type == "$reduce_bool"]
    assert rb.connections["A"] == module.ports["c"].bits


# --- case labels that are not constants ------------------------------------------

_CASE_TRUE = """module m (input clk, input [3:0] a, input [3:0] d, output logic [1:0] o,
          output logic [3:0] q);
    always_comb case (1'b1)
        a[3]: o = 2'd3;
        a[2]: o = 2'd2;
        a[1]: o = 2'd1;
        default: o = 2'd0;
    endcase
    always_ff @(posedge clk) case (1'b1)
        a[0]: q <= d;
        a[1]: q <= ~d;
    endcase
endmodule
"""


def test_constant_case_expression_with_signal_labels(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``case (1'b1)`` with signal labels (a one-hot priority select) is
    decided at run time, first matching label first. The constant case
    expression made it look decided at elaboration: the non-constant
    labels were skipped and the ``default`` walked alone, so ``o`` was
    the constant 0 and ``q`` had no driver from ``d`` at all."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _check(
            tmp_path,
            _CASE_TRUE,
            ("a",),
            ("o",),
            lambda a: {"o": 3 if a & 8 else 2 if a & 4 else 1 if a & 2 else 0},
        )
    assert _warnings(caplog) == []
    (ff,) = [c for c in module.cells.values() if c.type == "$dff"]
    for a in range(16):
        for d in (0x5, 0xA):
            values = {"a": a, "d": d}
            # ``q`` holds unless a[0] or a[1]: the hold reads Q = 0 here.
            want = d if a & 1 else (~d & 0xF) if a & 2 else 0
            assert _next_q(module, ff, values) == want, values


def _next_q(module: Module, ff: Cell, values: dict[str, int]) -> int:
    """``ff``'s ``D`` with the inputs ``values`` and ``Q`` = 0."""
    probe = Module(
        name=module.name,
        ports={
            **module.ports,
            "__d": netlist.Port(
                name="__d", direction="output", bits=ff.connections["D"]
            ),
        },
        cells=module.cells,
        netnames=module.netnames,
    )
    return _outputs(probe, values, ("__d",))["__d"]


# --- nets with more than one continuous driver -----------------------------------

_WIRED = """module m (input a, input b, input c, input d, input e, input f,
          output o, output p, output q, output r);
    wor wo;
    wand wa;
    wire wi;
    wire [1:0] wp;
    assign wo = a;
    assign wo = b;
    assign wa = c;
    assign wa = d;
    assign wi = e;
    assign wi = f;
    assign wp[0] = a;
    assign wp = {c, d};
    assign o = wo;
    assign p = wa;
    assign q = wi;
    assign r = wp[0];
endmodule
"""


def test_second_continuous_driver_keeps_both(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A second ``assign`` to a net renamed the first driver's source net
    to its own: with ``assign w = a; assign w = b;`` the inputs ``a`` and
    ``b`` became one net, inventing fan-in from ``b`` to every reader of
    ``a``. Now a ``wor`` / ``wand`` net resolves its drivers by ``$or`` /
    ``$and``; any other net is their ``$or``, reported, so neither driver's
    fan-in is lost. The inputs stay distinct nets. (Yosys shorts the two
    drivers of a plain ``wire``; slang alone is checked.)"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _WIRED)
    names = ("a", "b", "c", "d", "e", "f")
    bits = [module.ports[n].bits for n in names]
    assert len(set(bits)) == len(names)
    msgs = _warnings(caplog)
    assert len(msgs) == 2, msgs
    assert all("more than one continuous driver" in m for m in msgs)
    assert any("`wi`" in m for m in msgs) and any("`wp`" in m for m in msgs)
    for combo in itertools.product((0, 1), repeat=6):
        v = dict(zip(names, combo))
        assert _outputs(module, v, ("o", "p", "q", "r")) == {
            "o": v["a"] | v["b"],
            "p": v["c"] & v["d"],
            "q": v["e"] | v["f"],
            "r": v["a"] | v["d"],
        }, v


# --- inside, wildcard equality, streaming (were opaque; Yosys rejects them) ------

_SET_OPS = """module m (input [3:0] a, input [3:0] b, output o, output p, output q,
          output logic [1:0] c);
    assign o = a inside {1, 3, [8:10], b};
    assign p = a ==? 4'b1?0?;
    assign q = a !=? b;
    always_comb case (a) inside
        [0:3]: c = 0;
        4, 6, [12:$]: c = 1;
        4'b1?11: c = 2;
        default: c = 3;
    endcase
endmodule
"""


def test_inside_and_wildcard_equality(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``inside`` (values, ranges, an open range, a signal), ``==?`` /
    ``!=?`` and ``case … inside`` ranges are lowered (they were opaque
    drivers, so ``a`` and ``b`` had no fan-in to the outputs). Every output
    matches the SystemVerilog value for every input. Yosys' Verilog
    frontend does not parse these operators."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _SET_OPS)
    assert _warnings(caplog) == []
    assert not [c for c in module.cells.values() if c.type == "$_UNKNOWN_"]
    for a in range(16):
        for b in range(16):
            if a <= 3:
                c = 0
            elif a in (4, 6) or a >= 12:
                c = 1
            elif a & 0b1011 == 0b1011:
                c = 2
            else:
                c = 3
            assert _outputs(module, {"a": a, "b": b}, ("o", "p", "q", "c")) == {
                "o": int(a in (1, 3, 8, 9, 10, b)),
                "p": int(a & 0b1010 == 0b1000),
                "q": int(a != b),
                "c": c,
            }, (a, b)


_STREAM = """module m (input [3:0] a, input [3:0] b, output [7:0] r, output [7:0] s,
          output [7:0] t, output logic [7:0] u, output [3:0] v, output [3:0] w);
    assign r = {<<{a, b}};
    assign s = {<<2{a, b}};
    assign t = {>>4{b, a}};
    always_comb {<<{u}} = {a, b};
    assign {<<2{v, w}} = {b, a};
endmodule
"""


def _rev(value: int, width: int, size: int) -> int:
    chunks = [(value >> k) & ((1 << size) - 1) for k in range(0, width, size)]
    out = 0
    for chunk in chunks:
        out = (out << size) | chunk
    return out


def test_streaming_concatenation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``{<<{..}}`` / ``{<<N{..}}`` / ``{>>N{..}}`` read and written (in a
    combinational block and a continuous assignment) are bit permutations
    (IEEE 1800-2017 11.4.14); they were opaque drivers and dropped writes."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _STREAM)
    assert _warnings(caplog) == []
    for a in range(16):
        for b in range(16):
            ab, ba = (a << 4) | b, (b << 4) | a
            vw = _rev(ba, 8, 2)
            assert _outputs(
                module, {"a": a, "b": b}, ("r", "s", "t", "u", "v", "w")
            ) == {
                "r": _rev(ab, 8, 1),
                "s": _rev(ab, 8, 2),
                "t": ba,
                "u": _rev(ab, 8, 1),
                "v": vw >> 4,
                "w": vw & 0xF,
            }, (a, b)


# --- assignment patterns (were opaque unless constant; Yosys rejects them) --------

_PATTERNS = """module m (input s, input [3:0] d, input [1:0] i, output logic [7:0] o,
          output [3:0] p, output [3:0] q, output [7:0] r);
    typedef struct packed { logic [3:0] x; logic [3:0] y; } s_t;
    s_t t;
    logic [3:0] a [4];
    logic [3:0] b [3:0];
    always_comb begin t = '{default: '0}; if (s) t.y = d; o = t; end
    assign a = '{d, ~d, d + 1, 4'h5};
    assign b = '{2{d, ~d}};
    assign p = a[i];
    assign q = b[i];
    assign r = s_t'{x: d, default: 4'h3};
endmodule
"""


def test_assignment_patterns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """``'{default: '0}``, a positional pattern of an ascending unpacked
    array, a replicated one of a descending array (its first element is
    the left-bound index, ``b[3]``) and a keyed struct pattern carry their
    elements' fan-in (they were opaque drivers unless constant)."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _PATTERNS)
    assert _warnings(caplog) == []
    for s in (0, 1):
        for d in range(16):
            for i in range(4):
                nd = ~d & 0xF
                assert _outputs(
                    module, {"s": s, "d": d, "i": i}, ("o", "p", "q", "r")
                ) == {
                    "o": d if s else 0,
                    "p": [d, nd, (d + 1) & 0xF, 5][i],
                    "q": [nd, d, nd, d][i],
                    "r": (d << 4) | 3,
                }, (s, d, i)


# --- repeat and foreach loops (were dropped) ---------------------------------------

_LOOPS = """module m (input [3:0] a, output logic [3:0] o, output logic [3:0] p,
          output [5:0] r);
    logic [1:0] arr [2][3:1];
    always_comb foreach (o[i]) o[i] = a[i] ^ a[(i + 1) % 4];
    always_comb begin
        p = a;
        repeat (3) p = {p[2:0], p[3]};
    end
    always_comb foreach (arr[i, j]) arr[i][j] = a[1:0] + i + j;
    assign r = {arr[1][3], arr[0][2], arr[0][1]};
endmodule
"""


def test_repeat_and_foreach_unroll(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``repeat (N)`` and ``foreach`` (one and two dimensions, a
    descending one included) are unrolled like ``for`` (each was a
    reported statement whose writes were dropped). Every output matches
    the SystemVerilog value; a ``repeat`` matches Yosys too (Yosys' Verilog
    frontend does not parse ``foreach``)."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _LOOPS)
    assert _warnings(caplog) == []
    for a in range(16):
        o = sum((((a >> i) ^ (a >> ((i + 1) % 4))) & 1) << i for i in range(4))
        p = ((a << 3) | (a >> 1)) & 0xF
        arr = {(i, j): ((a & 3) + i + j) & 3 for i in (0, 1) for j in (1, 2, 3)}
        r = (arr[1, 3] << 4) | (arr[0, 2] << 2) | arr[0, 1]
        assert _outputs(module, {"a": a}, ("o", "p", "r")) == {
            "o": o,
            "p": p,
            "r": r,
        }, a


# --- early return, break, continue, tasks (were opaque / dropped) -------------------

_CONTROL = """module m (input [7:0] a, input [3:0] b, input [3:0] c, output [3:0] f,
          output [3:0] g, output logic [3:0] h, output logic [3:0] k,
          output logic [3:0] t, output logic [3:0] u, output logic [3:0] v);
    function automatic logic [3:0] early(input logic [3:0] x, input logic [3:0] y);
        if (x == 0) return y;
        if (x[3]) return ~y;
        return x ^ y;
    endfunction
    function automatic logic [3:0] first1(input logic [7:0] w);
        for (int i = 0; i < 8; i++) if (w[i]) return i;
        return 4'hf;
    endfunction
    task automatic addt(input logic [3:0] x, input logic [3:0] y, output logic [3:0] z);
        z = x + y;
    endtask
    function automatic void split(input logic [3:0] x, output logic [3:0] lo,
                                  inout logic [3:0] io);
        lo = x & 4'h3;
        if (x[0]) return;
        io = io ^ x;
    endfunction
    assign f = early(b, c);
    assign g = first1(a);
    always_comb begin
        h = 4'hf;
        for (int i = 0; i < 8; i++) if (a[i]) begin h = i; break; end
    end
    always_comb begin
        k = 0;
        for (int i = 0; i < 8; i++) begin if (!a[i]) continue; k = k + 1; end
    end
    always_comb addt(b, c, t);
    always_comb begin v = c; split(b, u, v); end
endmodule
"""


def _first1(a: int) -> int:
    return next((i for i in range(8) if a >> i & 1), 15)


def test_return_break_continue_and_task_calls(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An early ``return`` (also inside a loop), ``break`` and
    ``continue`` in an unrolled loop, a task and a ``void`` function with
    ``output`` / ``inout`` arguments called as statements: each was an
    opaque driver or a reported, dropped statement. Every output matches
    the SystemVerilog value for every input. (Yosys' Verilog frontend
    rejects each of these shapes.)"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _CONTROL)
    assert _warnings(caplog) == []
    assert not [c for c in module.cells.values() if c.type == "$_UNKNOWN_"]
    outs = ("f", "g", "h", "k", "t", "u", "v")
    for a in range(256):
        for b, c in ((a & 0xF, a >> 4), (0, a & 0xF), (a >> 4 | 8, a & 0xF)):
            early = c if b == 0 else (~c & 0xF) if b & 8 else b ^ c
            assert _outputs(module, {"a": a, "b": b, "c": c}, outs) == {
                "f": early,
                "g": _first1(a),
                "h": _first1(a),
                "k": bin(a).count("1") & 0xF,
                "t": (b + c) & 0xF,
                "u": b & 3,
                "v": c if b & 1 else c ^ b,
            }, (a, b, c)


# --- bit-count system functions (were opaque) --------------------------------------

_BITCOUNT = """module m (input [4:0] a, output [2:0] o, output p, output q, output r);
    assign o = $countones(a);
    assign p = $onehot(a);
    assign q = $onehot0(a);
    assign r = $isunknown(a);
endmodule
"""


def test_bit_count_functions(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """``$countones`` / ``$onehot`` / ``$onehot0`` are an adder chain over
    the operand's bits and a compare of the count, as Yosys lowers them
    (each was an opaque driver); ``$isunknown`` of a netlist value is 0.
    Every output matches the SystemVerilog value, and Yosys' (``$isunknown``
    aside: Yosys builds it from ``$eqx`` against ``x``)."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _check(
            tmp_path,
            _BITCOUNT.replace("    assign r = $isunknown(a);\n", ""),
            ("a",),
            ("o", "p", "q"),
            lambda a: {
                "o": bin(a).count("1"),
                "p": int(bin(a).count("1") == 1),
                "q": int(bin(a).count("1") <= 1),
            },
        )
        # A new directory: pyslang caches a source file by its path.
        (tmp_path / "full").mkdir()
        full = _slang(tmp_path / "full", _BITCOUNT)
    assert _warnings(caplog) == []
    assert full.ports["r"].bits == ("0",)
    assert module.ports["o"].bits != ("0", "0", "0")
