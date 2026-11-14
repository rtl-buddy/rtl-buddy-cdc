"""slang frontend: concatenation and partial assignment targets
(rtl-buddy-cdc#370).

``always @(posedge clk_b) {q, meta} <= {meta, a_q};`` used to lower to
nothing on the pyslang path — no flops, no crossing, a clean report and
no diagnostic — because ``_lvalue_bits`` had no case for a
concatenation. The combinational ``{carry, sum} = acc + inc`` was lost
the same way, and with it (through dead-logic pruning) the register
feeding it.

A concatenation target is now split: the right-hand side is sized to the
total width of the parts (zero / sign extension, truncation; operator
results take the context width, so ``acc + inc`` into five bits is a
five-bit ``$add`` whose top bit is the carry) and the rightmost part
takes the least significant bits. Each register written that way is one
flop, as Yosys ``proc`` makes it. Parts may be whole variables, constant
bit / part selects over any declared range, packed-struct members and
nested concatenations; continuous ``assign`` and combinational writes to
a part select or member are lowered by the same splice. Targets the
frontend cannot lower (streaming concatenation, a run-time index outside
the memory / decoder shapes, …) are reported with a WARNING instead of
vanishing.

Where yosys is on PATH every design is also run through the Yosys
frontend and the two netlists (per-bit flop connectivity) and verdicts
are compared; CI's pytest job has no yosys, so there the slang-side
assertions stand alone.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.netlist import Bit, Module

if importlib.util.find_spec("pyslang") is None:
    pytest.skip("pyslang not installed", allow_module_level=True)

from rtl_buddy_cdc.frontends import slang as slang_fe  # noqa: E402

_HAVE_YOSYS = shutil.which("yosys") is not None
_FLOPS = {"$dff", "$adff", "$sdff", "$dffe", "$adffe"}
_SLANG_LOG = "rtl_buddy_cdc.frontends.slang"


def _write(tmp_path: Path, src: str, name: str) -> Path:
    sv = tmp_path / name
    sv.write_text(src)
    return sv


def _slang(tmp_path: Path, src: str, top: str = "m") -> Module:
    module, _ = slang_fe.elaborate_with_blackboxes([_write(tmp_path, src, "s.sv")], top)
    return module


def _yosys(tmp_path: Path, src: str, top: str = "m") -> Module:
    """The ``lint`` recipe, run from a script file (CI's yosys 0.52 splits
    a ``-p`` script at commas)."""
    sv = _write(tmp_path, src, "y.sv")
    out = tmp_path / "y.json"
    script = tmp_path / "y.ys"
    script.write_text(
        f"read_verilog -sv {sv}\nhierarchy -top {top}\nproc\nflatten\n"
        f"opt_clean\nwrite_json {out}\n"
    )
    subprocess.run(["yosys", "-q", "-s", str(script)], check=True)
    return netlist.load(out)


def _flops(module: Module) -> list:
    return [c for c in module.cells.values() if c.type in _FLOPS]


def _namer(module: Module):
    """Bit -> frontend-neutral name: a top port bit, else the smallest
    ``netname[i]`` holding it, else ``<cell type>.<pin>[i]`` of its
    driver; constants stay as they are."""
    names: dict[Bit, str] = {}
    for pname, port in sorted(module.ports.items()):
        for i, b in enumerate(port.bits):
            names.setdefault(b, f"{pname}[{i}]")
    nets: dict[Bit, str] = {}
    for nname, nn in module.netnames.items():
        if "$" in nname:
            continue
        for i, b in enumerate(nn.bits):
            cand = f"{nname}[{i}]"
            if b not in nets or cand < nets[b]:
                nets[b] = cand
    drivers: dict[Bit, str] = {}
    for c in module.cells.values():
        for pin in ("Y", "Q"):
            for i, b in enumerate(c.connections.get(pin, ())):
                drivers.setdefault(b, f"{c.type}.{pin}[{i}]")

    def name(b: Bit) -> str:
        if isinstance(b, str):
            return b
        return names.get(b) or nets.get(b) or drivers.get(b) or "?"

    return name


def _flop_lanes(module: Module) -> set[tuple[str, str, str, int]]:
    """``(Q bit, D bit, clock port bit, flop width)`` for every flop lane:
    which register bit is loaded from what, on which clock, and how the
    register is grouped into flop cells."""
    name = _namer(module)
    out = set()
    for c in _flops(module):
        q, d = c.connections["Q"], c.connections["D"]
        assert len(q) == len(d), c
        (clk,) = c.connections["CLK"]
        for qb, db in zip(q, d):
            out.add((name(qb), name(db), name(clk), len(q)))
    return out


_SDC = """create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 7 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 1 [get_ports {d acc_in inc}]
"""


def _lint(tmp_path: Path, src: str, frontend: str) -> dict:
    sv = _write(tmp_path, src, f"{frontend}.sv")
    sdc = tmp_path / "m.sdc"
    sdc.write_text(_SDC)
    out = tmp_path / f"{frontend}.json"
    result = CliRunner().invoke(
        app,
        [
            "lint",
            "--top",
            "m",
            "--sdc",
            str(sdc),
            "--frontend",
            frontend,
            "--format",
            "json",
            "--output",
            str(out),
            str(sv),
        ],  # fmt: skip
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(out.read_text())


def _verdict(report: dict) -> tuple[int, dict[str, int]]:
    counts: dict[str, int] = {}
    for v in report["violations"]:
        counts[v["rule_id"]] = counts.get(v["rule_id"], 0) + 1
    return report["summary"]["crossings"], counts


def _agree_with_yosys(tmp_path: Path, src: str, slang_mod: Module) -> None:
    if not _HAVE_YOSYS:
        return
    assert _flop_lanes(slang_mod) == _flop_lanes(_yosys(tmp_path, src))


# --- the issue's two shapes ----------------------------------------------

_SHIFT_SYNC = """module m (input clk_a, input clk_b, input d, output reg q);
    reg a_q, meta;
    always @(posedge clk_a) a_q <= d;
    always @(posedge clk_b) {{q, meta}} {op} {{meta, a_q}};
endmodule
"""


@pytest.mark.parametrize("op", ["<=", "="])
def test_packed_shift_synchroniser(tmp_path: Path, caplog, op: str) -> None:
    src = _SHIFT_SYNC.format(op=op)
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not caplog.records
    lanes = _flop_lanes(module)
    assert lanes == {
        ("a_q[0]", "d[0]", "clk_a[0]", 1),
        ("meta[0]", "a_q[0]", "clk_b[0]", 1),
        ("q[0]", "meta[0]", "clk_b[0]", 1),
    }
    _agree_with_yosys(tmp_path, src, module)
    # 1 crossing into a two-flop synchroniser: clean, on both paths.
    slang = _verdict(_lint(tmp_path, src, "slang"))
    assert slang == (1, {})
    if _HAVE_YOSYS:
        assert _verdict(_lint(tmp_path, src, "yosys")) == slang


_CARRY = """module m (input clk_a, input clk_b, input [3:0] acc_in, input [3:0] inc,
          output reg c_q, output reg [3:0] s_q);
    reg [3:0] acc, sum;
    reg carry;
    always @(posedge clk_a) acc <= acc_in;
    {comb}
    always @(posedge clk_b) begin c_q <= carry; s_q <= sum; end
endmodule
"""


@pytest.mark.parametrize(
    "comb",
    [
        "always @* {carry, sum} = acc + inc;",
        "always_comb {carry, sum} = acc + inc;",
        "wire [4:0] cs; assign {carry, sum} = acc + inc;",
    ],
    ids=["always_star", "always_comb", "assign"],
)
def test_carry_sum_split(tmp_path: Path, caplog, comb: str) -> None:
    src = _CARRY.format(comb=comb)
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not caplog.records
    (add,) = [c for c in module.cells.values() if c.type == "$add"]
    # Context-determined width: a 5-bit adder, carry is its MSB.
    y = add.connections["Y"]
    assert len(y) == 5
    (c_flop,) = [
        c for c in _flops(module) if c.connections["Q"] == module.ports["c_q"].bits
    ]
    (s_flop,) = [
        c for c in _flops(module) if c.connections["Q"] == module.ports["s_q"].bits
    ]
    assert c_flop.connections["D"] == (y[4],)
    assert s_flop.connections["D"] == y[:4]
    # The acc register survives (it feeds the adder).
    assert any(c.connections["Q"] == add.connections["A"][:4] for c in _flops(module))
    _agree_with_yosys(tmp_path, src, module)
    slang = _verdict(_lint(tmp_path, src, "slang"))
    assert slang[0] >= 1 and slang[1]
    if _HAVE_YOSYS:
        assert _verdict(_lint(tmp_path, src, "yosys")) == slang


# --- part shapes ------------------------------------------------------------

_PARTS = """module m (input clk_b, input [7:0] x, output reg [5:2] lo, output reg [0:3] hi,
          output reg b);
    always @(posedge clk_b) {{hi[1:2], {{lo[4:3], b}}, hi[0], lo[2 +: 1], {tail}}} <= {rhs};
    always @(posedge clk_b) lo[5] <= x[0];
endmodule
"""


@pytest.mark.parametrize(
    ("tail", "rhs"),
    [
        ("hi[3]", "x"),
        ("hi[3]", "x[6:0]"),  # zero-extended to 8 bits
        ("hi[3]", "{x, x}"),  # truncated: the low 8 bits
    ],
    ids=["exact", "narrow", "wide"],
)
def test_nested_parts_and_ranges(tmp_path: Path, caplog, tail: str, rhs: str) -> None:
    """Nested concatenation, part selects over a non-zero-based
    descending and an ascending range, an indexed part select, single
    bits — LSB-first slices land where Yosys puts them."""
    src = _PARTS.format(tail=tail, rhs=rhs)
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not caplog.records
    lanes = {(q, dr) for q, dr, _clk, _w in _flop_lanes(module)}
    # hi is [0:3]: hi[3] is its LSB (hi[0] in Yosys' LSB-first numbering).
    x = [f"x[{i}]" for i in range(8)]
    if rhs == "x[6:0]":
        x[7] = "0"
    assert {
        ("hi[0]", x[0]),
        ("lo[0]", x[1]),
        ("hi[3]", x[2]),
        ("b[0]", x[3]),
        ("lo[1]", x[4]),
        ("lo[2]", x[5]),
        ("hi[1]", x[6]),
        ("hi[2]", x[7]),
        ("lo[3]", "x[0]"),
    } == lanes
    _agree_with_yosys(tmp_path, src, module)


_SIGNED = """module m (input clk_b, input signed [1:0] sd, input [1:0] x,
          output reg [2:0] p, output reg [1:0] r);
    always @(posedge clk_b) {{p, r}} <= {rhs};
endmodule
"""


@pytest.mark.parametrize("rhs", ["sd", "x"])
def test_extension_follows_signedness(tmp_path: Path, rhs: str) -> None:
    src = _SIGNED.format(rhs=rhs)
    module = _slang(tmp_path, src)
    lanes = {q: dr for q, dr, _clk, _w in _flop_lanes(module)}
    src_name = rhs
    pad = "sd[1]" if rhs == "sd" else "0"
    assert lanes == {
        "r[0]": f"{src_name}[0]",
        "r[1]": f"{src_name}[1]",
        "p[0]": pad,
        "p[1]": pad,
        "p[2]": pad,
    }
    _agree_with_yosys(tmp_path, src, module)


def test_contiguous_parts_of_one_register_are_one_flop(tmp_path: Path) -> None:
    """Parts of one variable that cover a contiguous range are one flop,
    in whatever order the concatenation lists them; a gap splits it."""
    src = """module m (input clk_b, input [7:0] x, output reg [3:0] q, output reg [3:0] g,
          output reg b);
    always @(posedge clk_b) {q[1:0], q[3], q[2]} <= x[3:0];
    always @(posedge clk_b) {g[3], b, g[1:0]} <= x[7:4];
endmodule
"""
    module = _slang(tmp_path, src)
    by_q = {
        tuple(_namer(module)(b) for b in c.connections["Q"]): tuple(
            _namer(module)(b) for b in c.connections["D"]
        )
        for c in _flops(module)
    }
    assert by_q == {
        ("q[0]", "q[1]", "q[2]", "q[3]"): ("x[2]", "x[3]", "x[0]", "x[1]"),
        ("g[0]", "g[1]"): ("x[4]", "x[5]"),
        ("b[0]",): ("x[6]",),
        ("g[3]",): ("x[7]",),
    }
    _agree_with_yosys(tmp_path, src, module)


def test_packed_struct_member_target(tmp_path: Path, caplog) -> None:
    src = """typedef struct packed { logic [2:0] hi; logic lo; } st_t;
module m (input clk_b, input [3:0] x, output st_t s, output st_t t);
    always @(posedge clk_b) s.hi <= x[2:0];
    always @(posedge clk_b) s.lo <= x[3];
    always @(posedge clk_b) {t.lo, t.hi} <= x;
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not caplog.records
    lanes = {(q, dr) for q, dr, _clk, _w in _flop_lanes(module)}
    assert {
        ("s[1]", "x[0]"),
        ("s[2]", "x[1]"),
        ("s[3]", "x[2]"),
        ("s[0]", "x[3]"),
        ("t[1]", "x[0]"),
        ("t[2]", "x[1]"),
        ("t[3]", "x[2]"),
        ("t[0]", "x[3]"),
    } == lanes
    _agree_with_yosys(tmp_path, src, module)


# --- combinational and continuous splices --------------------------------


def test_comb_and_continuous_partial_targets(tmp_path: Path, caplog) -> None:
    """``assign bus[3:0] = …`` / ``v[1] = …`` in ``always_comb`` / a
    concatenation in ``assign`` — each written range is re-pointed, the
    other bits keep their drivers. The flop reading ``bus`` comes first
    in source order, so the continuous splice must reach it too."""
    src = """module m (input clk_b, input [3:0] x, input [1:0] y,
          output reg [7:0] q, output reg [2:0] r, output reg [1:0] z);
    wire [7:0] bus;
    always @(posedge clk_b) q <= bus;
    assign bus[3:0] = x;
    assign bus[7:4] = {y, y};
    reg [2:0] v;
    always_comb begin v[2] = y[0]; v[1:0] = x[3:2]; end
    always @(posedge clk_b) r <= v;
    wire a, b;
    assign {a, b} = y;
    always @(posedge clk_b) z <= {b, a};
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not caplog.records
    lanes = {(q, dr) for q, dr, _clk, _w in _flop_lanes(module)}
    assert {
        *((f"q[{i}]", f"x[{i}]") for i in range(4)),
        ("q[4]", "y[0]"),
        ("q[5]", "y[1]"),
        ("q[6]", "y[0]"),
        ("q[7]", "y[1]"),
        ("r[0]", "x[2]"),
        ("r[1]", "x[3]"),
        ("r[2]", "y[0]"),
        ("z[0]", "y[1]"),
        ("z[1]", "y[0]"),
    } == lanes
    _agree_with_yosys(tmp_path, src, module)


def test_latch_concat_target(tmp_path: Path) -> None:
    src = """module m (input en, input [2:0] x, output reg a, output reg [1:0] b);
    always_latch if (en) {a, b} = x;
endmodule
"""
    module = _slang(tmp_path, src)
    latches = sorted(
        (c.connections["Q"], c.connections["D"])
        for c in module.cells.values()
        if c.type == "$dlatch"
    )
    x = module.ports["x"].bits
    assert latches == sorted(
        [(module.ports["a"].bits, x[2:]), (module.ports["b"].bits, x[:2])]
    )


def test_reset_values_split_across_parts(tmp_path: Path) -> None:
    src = """module m (input clk_b, input rst_n, input [2:0] x,
          output reg a, output reg [1:0] b);
    always @(posedge clk_b or negedge rst_n)
        if (!rst_n) {a, b} <= 3'b110;
        else {a, b} <= x;
endmodule
"""
    module = _slang(tmp_path, src)
    by_q = {
        c.connections["Q"]: c.parameters["ARST_VALUE"]
        for c in module.cells.values()
        if c.type == "$adff"
    }
    assert by_q == {module.ports["a"].bits: "1", module.ports["b"].bits: "10"}


def test_output_port_bound_to_concatenation(tmp_path: Path) -> None:
    src = """module sync2 (input clk, input d, output reg [1:0] q);
    always @(posedge clk) q <= {q[0], d};
endmodule
module m (input clk_a, input clk_b, input d, output reg o);
    reg a_q, s1, s2;
    always @(posedge clk_a) a_q <= d;
    sync2 u (.clk(clk_b), .d(a_q), .q({s2, s1}));
    always @(posedge clk_b) o <= s2;
endmodule
"""
    module = _slang(tmp_path, src)
    lanes = _flop_lanes(module)
    assert ("o[0]", "s2[0]", "clk_b[0]", 1) in lanes
    assert ("s1[0]", "a_q[0]", "clk_b[0]", 2) in lanes
    _agree_with_yosys(tmp_path, src, module)


# --- what is still not lowered warns -----------------------------------


@pytest.mark.parametrize(
    ("decl", "stmt", "reason"),
    [
        ("reg [7:0] y;", "always @(posedge clk_b) {>>{y}} <= x;", "streaming"),
        ("reg [7:0] y;", "always_comb {>>{y}} = x;", "streaming"),
        ("wire [7:0] y;", "assign {>>{y}} = x;", "streaming"),
        ("reg [7:0] y;", "always @(posedge clk_b) y[i] <= x[0];", "run-time index"),
        (
            "reg [7:0] y; reg c;",
            "always @(posedge clk_b) {c, y[i]} <= x[1:0];",
            "run-time index",
        ),
        (
            "reg [1:0] y [0:1][0:1];",
            "always_comb y[i][1] = x[1:0];",
            "run-time index",
        ),
    ],
    ids=[
        "ff_stream",
        "comb_stream",
        "assign_stream",
        "ff_index",
        "concat_index",
        "nested_index",
    ],
)
def test_unlowered_target_warns(
    tmp_path: Path, caplog, decl: str, stmt: str, reason: str
) -> None:
    src = f"""module m (input clk_b, input [7:0] x, input [2:0] i);
    {decl}
    {stmt}
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1, msgs
    assert "assignment not lowered" in msgs[0] and reason in msgs[0]


def test_unlowered_target_in_unrolled_loop_warns_once(tmp_path: Path, caplog) -> None:
    src = """module m (input clk_b, input [7:0] x);
    reg [7:0] y [0:3];
    always @(posedge clk_b) for (int k = 0; k < 4; k++) {>>{y[k]}} <= x;
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    assert len(caplog.records) == 1


# --- edge paths -----------------------------------------------------------

_UNPACKED = "typedef struct { logic [1:0] f; } us_t;\n"


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        # Output port bound to an unpacked-struct member: no bits here.
        (
            "us_t us; sub u (.q(us.f));",
            "output port connection `q` not lowered (target is a member",
        ),
        ("us_t us; always_comb us.f = x[1:0];", "member of an unpacked"),
        ("reg [7:0] y; always_comb {y[i +: 2], y[0]} = x[2:0];", "run-time index"),
        ("reg [0:0] y; always_comb y[i] = x[0];", "run-time index"),
        (
            "reg [1:0] y; reg c; always_latch if (x[0]) {c, y[i]} = x[2:0];",
            "run-time index",
        ),
        ("always @(posedge clk_b) u.r <= x[0];", "hierarchical reference"),
    ],
    ids=[
        "port_unpacked",
        "comb_unpacked",
        "comb_indexed",
        "comb_1bit_index",
        "latch_concat",
        "hierarchical",
    ],
)
def test_more_unlowered_targets_warn(tmp_path: Path, caplog, body: str, needle: str):
    src = (
        _UNPACKED
        + f"""module sub (output logic [1:0] q);
    logic r;
    assign q = 2'b0;
endmodule
module m (input clk_b, input [7:0] x, input [2:0] i);
    {body}
    {"" if "sub u" in body else "sub u ();"}
endmodule
"""
    )
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    msgs = [r.getMessage() for r in caplog.records]
    assert any(needle in m for m in msgs), msgs


def test_unlowerable_rhs_into_concatenation(tmp_path: Path) -> None:
    """An RHS with no lowering (a function call) still gives the
    registers an opaque ``$_UNKNOWN_`` driver, as for a plain target; a
    combinational splice leaves the bits undriven, as a whole-variable
    combinational target does."""
    src = """module m (input clk_b, input rst_n, input [2:0] x, output reg a,
          output reg [1:0] b, output reg [2:0] c);
    function automatic [2:0] f(input [2:0] v); f = v ^ 3'b101; endfunction
    always @(posedge clk_b or negedge rst_n)
        if (!rst_n) {a, b} <= x;
        else {a, b} <= f(x);
    reg [2:0] w;
    always_comb {w[2], w[1:0]} = f(x);
    always @(posedge clk_b) c <= w;
endmodule
"""
    module = _slang(tmp_path, src)
    unknown = {
        b
        for c in module.cells.values()
        if c.type == "$_UNKNOWN_"
        for b in c.connections["Y"]
    }
    flops = {c.connections["Q"]: c for c in _flops(module)}
    assert set(flops[module.ports["a"].bits].connections["D"]) <= unknown
    assert set(flops[module.ports["b"].bits].connections["D"]) <= unknown
    # The reset arm's RHS is not a constant: reset value left at 0.
    assert flops[module.ports["a"].bits].parameters["ARST_VALUE"] == "0"
