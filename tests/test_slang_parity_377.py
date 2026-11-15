"""slang frontend: silent drops and Yosys-parity gaps (rtl-buddy-cdc#377).

Each section is one item of the issue: a minimal design the slang path
used to lower differently from Yosys (or not at all, with no
diagnostic), the netlist shape it lowers to now, and, where yosys is on
PATH, the same design through the Yosys frontend with the per-bit flop
connectivity and the ``lint`` verdict compared. CI's pytest job has no
yosys, so there the slang-side assertions stand alone.
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


def _cells(module: Module, cell_type: str) -> list:
    return [c for c in module.cells.values() if c.type == cell_type]


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
    """``(Q bit, D bit, clock port bit, flop width)`` for every flop lane."""
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
set_input_delay -clock clk_a 1 [get_ports d]
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


def _same_verdict(tmp_path: Path, src: str) -> tuple[int, dict[str, int]]:
    """The slang ``lint`` verdict, checked against Yosys' when available."""
    slang = _verdict(_lint(tmp_path, src, "slang"))
    if _HAVE_YOSYS:
        assert _verdict(_lint(tmp_path, src, "yosys")) == slang
    return slang


def _agree_with_yosys(tmp_path: Path, src: str, slang_mod: Module) -> None:
    if not _HAVE_YOSYS:
        return
    assert _flop_lanes(slang_mod) == _flop_lanes(_yosys(tmp_path, src))


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# --- 1. combinational results reach earlier readers -----------------------

_FORWARD = """module m (input clk_a, input clk_b, input d, output reg q);
    reg a_q, s1;
    {decl}
    always @(posedge clk_a) a_q <= d;
    always @(posedge clk_b) begin s1 <= x; q <= s1; end
    {comb}
endmodule
"""


@pytest.mark.parametrize(
    ("decl", "comb"),
    [
        ("reg x;", "always @* x = a_q;"),
        ("logic x;", "always_comb x = a_q;"),
        ("reg x;", "always @(a_q) x = a_q;"),
        ("logic [1:0] y; wire x = y[0];", "always_comb y = {1'b0, a_q};"),
        ("", "wire x = a_q;"),
    ],
    ids=["always_star", "always_comb", "sensitivity_list", "chain", "wire_init"],
)
def test_comb_result_reaches_earlier_reader(
    tmp_path: Path, caplog, decl: str, comb: str
) -> None:
    """A clocked block above the combinational block that drives what it
    reads: the first synchroniser stage used to read an undriven
    placeholder, so ``a_q`` was pruned and the crossing vanished."""
    src = _FORWARD.format(decl=decl, comb=comb)
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not _warnings(caplog)
    lanes = _flop_lanes(module)
    assert ("s1[0]", "a_q[0]", "clk_b[0]", 1) in lanes
    _agree_with_yosys(tmp_path, src, module)
    assert _same_verdict(tmp_path, src) == (1, {})


def test_partial_comb_write_reaches_earlier_reader(tmp_path: Path) -> None:
    """Only the written bits of a part-select target are re-pointed; the
    others keep their own driver, wherever the reader sits."""
    src = """module m (input clk_a, input clk_b, input d, input e,
          output reg [1:0] q);
    reg a_q;
    reg [1:0] x;
    always @(posedge clk_b) q <= x;
    always @(posedge clk_a) a_q <= d;
    always @* x[0] = a_q;
    always @* x[1] = e;
endmodule
"""
    module = _slang(tmp_path, src)
    assert ("q[0]", "a_q[0]", "clk_b[0]", 2) in _flop_lanes(module)
    assert ("q[1]", "e[0]", "clk_b[0]", 2) in _flop_lanes(module)
    _agree_with_yosys(tmp_path, src, module)


# --- 2. level-sensitive blocks: one incomplete variable is one latch ------


def _latches(module: Module) -> dict[str, frozenset[str]]:
    """Every ``$dlatch`` lane's Q bit -> the ports, registers and constants
    its D and EN cones read: what the latch holds and when, independent
    of how each frontend shapes the mux tree."""
    name = _namer(module)
    driver = {
        b: c
        for c in module.cells.values()
        for pin in ("Y",)
        for b in c.connections.get(pin, ())
    }

    def leaves(bits: tuple[Bit, ...]) -> set[str]:
        out: set[str] = set()
        seen: set[Bit] = set()
        stack = list(bits)
        while stack:
            b = stack.pop()
            if b in seen:
                continue
            seen.add(b)
            cell = driver.get(b)
            if cell is None or isinstance(b, str):
                out.add(name(b))
                continue
            for pin, pbits in cell.connections.items():
                if pin != "Y":
                    stack.extend(pbits)
        return out

    lanes: dict[str, frozenset[str]] = {}
    for c in _cells(module, "$dlatch"):
        cone = leaves(c.connections["D"]) | leaves(c.connections["EN"])
        for qb in c.connections["Q"]:
            lanes[name(qb)] = frozenset(cone - {"0", "1", "x"})
    return lanes


_INCOMPLETE = """module m (input clk_a, input clk_b, input d, input v, input w,
          output reg q, output reg [1:0] tq);
    reg a_q, s1, x;
    reg [1:0] t;
    always @(posedge clk_a) a_q <= d;
    always @* begin
        x = a_q;
        {body}
    end
    always @(posedge clk_b) begin s1 <= x; q <= s1; end
    {tail}
endmodule
"""


@pytest.mark.parametrize(
    ("body", "tail"),
    [
        ("if (v) t = {a_q, d};", ""),
        ("if (v) t = {a_q, d};", "always @(posedge clk_b) tq <= t;"),
        ("if (v) t = {a_q, d}; else if (w) t = {d, d};", "always @* tq = t;"),
        (
            "case ({v, w}) 2'b01: t = 2'b10; 2'b10: t = {d, a_q}; endcase",
            "assign tq = t;",
        ),
        ("if (v) t[0] = d;", "assign tq = t;"),
    ],
    ids=["unused", "registered", "elsif", "case", "part"],
)
def test_incomplete_variable_keeps_the_block(
    tmp_path: Path, caplog, body: str, tail: str
) -> None:
    """The issue's reproducer: a scratch variable assigned only under a
    condition used to drop the whole block with a warning, and with it
    ``x`` and the crossing it feeds. Now ``x`` is combinational and only
    ``t`` is a latch, as Yosys infers it."""
    src = _INCOMPLETE.format(body=body, tail=tail)
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not _warnings(caplog)
    assert ("s1[0]", "a_q[0]", "clk_b[0]", 1) in _flop_lanes(module)
    assert _cells(module, "$dlatch")
    if _HAVE_YOSYS:
        y_mod = _yosys(tmp_path, src)
        assert _flop_lanes(module) == _flop_lanes(y_mod)
        if tail:
            assert _latches(module) == _latches(y_mod)
    assert _same_verdict(tmp_path, src)[0] >= 1


def test_latch_enable_is_the_assigning_condition(tmp_path: Path) -> None:
    """``if (v) t = a; else if (w) t = b;`` loads ``v ? a : b`` while ``v |
    w``: the held path (neither) is the latch's closed state."""
    src = """module m (input v, input w, input a, input b, output reg t);
    always @* begin if (v) t = a; else if (w) t = b; end
endmodule
"""
    module = _slang(tmp_path, src)
    (latch,) = _cells(module, "$dlatch")
    assert latch.connections["Q"] == module.ports["t"].bits
    (mux,) = [
        c
        for c in _cells(module, "$mux")
        if c.connections["Y"] == latch.connections["D"]
    ]
    assert mux.connections["S"] == module.ports["v"].bits
    assert mux.connections["A"] == module.ports["b"].bits
    assert mux.connections["B"] == module.ports["a"].bits
    (en,) = latch.connections["EN"]
    (en_mux,) = [c for c in _cells(module, "$mux") if c.connections["Y"] == (en,)]
    assert en_mux.connections["S"] == module.ports["v"].bits
    assert en_mux.connections["A"] == module.ports["w"].bits
    assert en_mux.connections["B"] == ("1",)


def test_latch_leaves_no_dead_hold_mux(tmp_path: Path) -> None:
    """The hold-feedback merge a latch replaces is removed: left behind it
    read the latch output, and its select (here a clock) as data."""
    src = """module m (input en, input d, output reg q);
    always @* if (en) q = d;
endmodule
"""
    module = _slang(tmp_path, src)
    assert [c.type for c in module.cells.values()] == ["$dlatch"]


def test_unwritten_arm_keeps_the_prior_value(tmp_path: Path) -> None:
    """``y = b; if (sel) y = a;`` is ``sel ? a : b``. The single-arm write
    used to alias ``y`` to ``a``, dropping ``b`` and ``sel``, and (through
    the alias rewrite) re-pointed the input ``b`` itself at ``a``, so a
    register of ``b`` read ``a``."""
    src = """module m (input clk, input sel, input a, input b, output reg q,
          output reg r);
    reg y;
    always @* begin y = b; if (sel) y = a; end
    always @(posedge clk) q <= y;
    always @(posedge clk) r <= b;
endmodule
"""
    module = _slang(tmp_path, src)
    assert ("r[0]", "b[0]", "clk[0]", 1) in _flop_lanes(module)
    assert ("q[0]", "y[0]", "clk[0]", 1) in _flop_lanes(module)
    (mux,) = _cells(module, "$mux")
    assert mux.connections["A"] == module.ports["b"].bits
    assert mux.connections["B"] == module.ports["a"].bits
    assert not _cells(module, "$dlatch")
    _agree_with_yosys(tmp_path, src, module)


def test_full_case_without_default_is_not_a_latch(tmp_path: Path) -> None:
    """Labels covering every value of the case expression leave no held
    path: no latch, on either frontend."""
    src = """module m (input clk, input [1:0] s, input a, input b, output reg q);
    reg z;
    always @* begin
        case (s) 2'd0: z = a; 2'd1: z = b; 2'd2: z = 1'b1; 2'd3: z = 1'b0; endcase
    end
    always @(posedge clk) q <= z;
endmodule
"""
    module = _slang(tmp_path, src)
    assert not _cells(module, "$dlatch")
    if _HAVE_YOSYS:
        assert not _cells(_yosys(tmp_path, src), "$dlatch")


def test_always_comb_latch_warns(tmp_path: Path, caplog) -> None:
    """``always_comb`` forbids a latch (Yosys stops with an error); the
    slang path lowers it and says so."""
    src = """module m (input v, input a, output logic t);
    always_comb if (v) t = a;
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert len(_cells(module, "$dlatch")) == 1
    assert any("`always_comb` leaves `t` unassigned" in m for m in _warnings(caplog))


def test_unlowerable_condition_warns(tmp_path: Path, caplog) -> None:
    """A combinational ``if`` whose condition does not lower is reported
    and muxed on an opaque select (item 4), not walked as if both arms
    were unconditional."""
    src = """module m (input [3:0] a, input [3:0] b, output reg [3:0] y);
    always @* begin y = b; if (a ==? 4'b1z0x) y = a; end
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    assert any("`if` condition" in m for m in _warnings(caplog))


# --- 3. user-defined functions are inlined --------------------------------

_GRAY = """module m (input clk_a, input clk_b, output reg [3:0] q);
    function [3:0] bin2gray(input [3:0] x);
        bin2gray = x ^ (x >> 1);
    endfunction
    reg [3:0] b, g, s1;
    always @(posedge clk_a) begin
        b <= b + 1;
        g <= bin2gray(b);
    end
    always @(posedge clk_b) begin s1 <= g; q <= s1; end
endmodule
"""


def test_gray_encoder_function_is_seen(tmp_path: Path) -> None:
    """The corpus reproducer: a gray pointer written through
    ``bin2gray()`` used to be an opaque ``$_UNKNOWN_`` cell, so the
    gray-coded crossing reported CDC-004 on the slang path only."""
    module = _slang(tmp_path, _GRAY)
    assert not _cells(module, "$_UNKNOWN_")
    (xor,) = _cells(module, "$xor")
    (g_flop,) = [
        c for c in _flops(module) if c.connections["Q"] == module.netnames["g"].bits
    ]
    assert g_flop.connections["D"] == xor.connections["Y"]
    _agree_with_yosys(tmp_path, _GRAY, module)
    assert _same_verdict(tmp_path, _GRAY) == (1, {})


def test_function_body_with_locals_and_branches(tmp_path: Path) -> None:
    """Locals, an ``if`` and two calls of the same function with different
    arguments: each call gets its own values; the result matches the
    Yosys path lane for lane."""
    src = """module m (input clk, input [3:0] a, input [3:0] b, input [3:0] k,
          output reg [3:0] p, output reg [3:0] r);
    function [3:0] mix(input [3:0] x, input [3:0] y);
        reg [3:0] t;
        begin
            t = x & y;
            if (x[0]) t = t | 4'b1000;
            mix = t ^ y;
        end
    endfunction
    always @(posedge clk) begin p <= mix(a, k); r <= mix(b, mix(a, b)); end
endmodule
"""
    module = _slang(tmp_path, src)
    assert not _cells(module, "$_UNKNOWN_")
    assert len(_cells(module, "$and")) == 3
    _agree_with_yosys(tmp_path, src, module)


def test_sv_function_with_return(tmp_path: Path) -> None:
    """``return expr;`` ending the body sets the result."""
    src = """module m (input clk, input [3:0] a, output logic [3:0] q);
    function automatic logic [3:0] inv(input logic [3:0] x);
        logic [3:0] t;
        t = ~x;
        return t + 4'd1;
    endfunction
    always_ff @(posedge clk) q <= inv(a);
endmodule
"""
    module = _slang(tmp_path, src)
    (neg,) = _cells(module, "$not")
    (add,) = _cells(module, "$add")
    assert neg.connections["A"] == module.ports["a"].bits
    assert add.connections["A"][:4] == neg.connections["Y"]
    (flop,) = _flops(module)
    assert flop.connections["D"] == add.connections["Y"][:4]


def test_constant_call_folds(tmp_path: Path) -> None:
    """A call slang evaluates at elaboration time is its constant."""
    src = """module m #(parameter N = 16) (output [3:0] w, output [3:0] z);
    function automatic integer half(input integer v); return v / 2; endfunction
    assign w = $clog2(N);
    assign z = half(N);
endmodule
"""
    module = _slang(tmp_path, src)
    assert module.ports["w"].bits == ("0", "0", "1", "0")
    assert module.ports["z"].bits == ("0", "0", "0", "1")
    assert not module.cells


@pytest.mark.parametrize(
    "func",
    [
        # A ``return`` before the end of the body.
        "function automatic [3:0] f(input [3:0] x); if (x[0]) return x;"
        " return ~x; endfunction",
        # An output argument.
        "function automatic [3:0] f(input [3:0] x, output [3:0] o);"
        " o = x; f = x; endfunction",
        # A write to a module variable.
        "function automatic [3:0] f(input [3:0] x); side = x; f = x; endfunction",
        # A statement the walker does not lower.
        "function automatic [3:0] f(input [3:0] x); f = x;"
        " while (f[0]) f = f >> 1; endfunction",
    ],
    ids=["early_return", "output_arg", "side_effect", "while_loop"],
)
def test_function_not_inlined_stays_opaque(tmp_path: Path, func: str) -> None:
    """A function body outside the inlined shapes is not half-lowered:
    the call stays an opaque ``$_UNKNOWN_`` driver."""
    call = "f(a, o)" if "output" in func else "f(a)"
    src = f"""module m (input clk, input [3:0] a, output reg [3:0] q);
    reg [3:0] side, o;
    {func}
    always @(posedge clk) q <= {call};
endmodule
"""
    module = _slang(tmp_path, src)
    (flop,) = _flops(module)
    (unknown,) = _cells(module, "$_UNKNOWN_")
    assert flop.connections["D"] == unknown.connections["Y"]


# --- 4. no right-hand side is left silently undriven ----------------------


def test_signed_cast_in_condition(tmp_path: Path, caplog) -> None:
    """The corpus' ``slang-system-call`` reproducer: ``$signed(x) > 4``
    used to drop the ``if`` (and before that, walk it unconditionally).
    ``$signed`` / ``$unsigned`` have no logic of their own."""
    src = """module m (input clk, input [3:0] x, input a, input b, output reg q);
    reg y;
    always @* begin y = b; if ($signed(x) > 4 || $unsigned(x) == 4'd1) y = a; end
    always @(posedge clk) q <= y;
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not _warnings(caplog)
    assert not _cells(module, "$_UNKNOWN_")
    (gt,) = _cells(module, "$gt")
    assert gt.connections["A"][:4] == module.ports["x"].bits
    _agree_with_yosys(tmp_path, src, module)


def test_constants_read_as_values(tmp_path: Path) -> None:
    """A parameter, an enum value, ``'0`` / ``'1`` and an ``x`` / ``z``
    literal are constants. Parameters and enum values read as undriven
    nets; ``'0`` / ``'1`` and ``x`` / ``z`` literals became opaque
    ``$_UNKNOWN_`` drivers."""
    src = """typedef enum logic [1:0] {IDLE, RUN, DONE} st_t;
module m #(parameter [3:0] P = 4'd5) (input clk, input sel, input [3:0] a,
          output reg [3:0] q, output reg [3:0] r, output st_t s, output [3:0] w,
          output [5:0] z);
    localparam [3:0] L = P + 1;
    always @(posedge clk) q <= a & P;
    always @(posedge clk) r <= sel ? '1 : '0;
    always @(posedge clk) s <= sel ? RUN : DONE;
    assign w = L;
    assign z = 4'bx01z;
endmodule
"""
    module = _slang(tmp_path, src)
    assert not _cells(module, "$_UNKNOWN_")
    (and_,) = _cells(module, "$and")
    assert and_.connections["B"] == ("1", "0", "1", "0")
    muxes = {c.connections["Y"]: c for c in _cells(module, "$mux")}
    flops = {c.connections["Q"]: c for c in _flops(module)}
    r_mux = muxes[flops[module.ports["r"].bits].connections["D"]]
    assert (r_mux.connections["A"], r_mux.connections["B"]) == (("0",) * 4, ("1",) * 4)
    s_mux = muxes[flops[module.ports["s"].bits].connections["D"]]
    assert (s_mux.connections["A"], s_mux.connections["B"]) == (("0", "1"), ("1", "0"))
    assert module.ports["w"].bits == ("0", "1", "1", "0")
    assert module.ports["z"].bits == ("z", "1", "0", "x", "0", "0")


@pytest.mark.parametrize(
    "body",
    [
        "assign y = $countones(a);",
        "always_comb y = $countones(a);",
        "always @* begin y = 4'd0; y[1:0] = $countones(a); end",
        "wire [3:0] w = $countones(a); assign y = w;",
        "sub u (.i($countones(a)), .o(y));",
    ],
    ids=["assign", "always_comb", "part_select", "wire_init", "port"],
)
def test_unlowerable_rhs_is_opaque_and_reported(
    tmp_path: Path, caplog, body: str
) -> None:
    """A right-hand side with no lowering used to leave a combinational
    target (or a child's input port) silently undriven; it now gets an
    opaque ``$_UNKNOWN_`` driver and a warning, as a register input
    already did (with no warning)."""
    src = f"""module sub (input [3:0] i, output [3:0] o); assign o = i; endmodule
module m (input [3:0] a, output logic [3:0] y);
    {body}
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    unknown = {b for c in _cells(module, "$_UNKNOWN_") for b in c.connections["Y"]}
    assert unknown & set(module.ports["y"].bits)
    msgs = _warnings(caplog)
    assert any(
        "`$countones(a)` not lowered; modelled as an opaque driver" in m for m in msgs
    ), msgs


def test_unlowerable_register_input_is_reported(tmp_path: Path, caplog) -> None:
    src = """module m (input clk, input [3:0] a, output reg [3:0] q);
    always @(posedge clk) q <= $countones(a);
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    (flop,) = _flops(module)
    (unknown,) = _cells(module, "$_UNKNOWN_")
    assert flop.connections["D"] == unknown.connections["Y"]
    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize("kind", ["casez", "casex"])
def test_wildcard_case_labels(tmp_path: Path, kind: str) -> None:
    """``casez`` / ``casex`` labels compare only their non-wildcard bits;
    a wildcard label used to fail to lower and its arm was lost."""
    src = f"""module m (input clk, input [3:0] s, input a, input b, input c,
          output reg q);
    reg y;
    always @* begin
        {kind} (s)
            4'b1???: y = a;
            4'b01?{"x" if kind == "casex" else "?"}: y = b;
            default: y = c;
        endcase
    end
    always @(posedge clk) q <= y;
endmodule
"""
    module = _slang(tmp_path, src)
    eqs = sorted(
        (c.connections["A"], c.connections["B"]) for c in _cells(module, "$eq")
    )
    s_bits = module.ports["s"].bits
    assert eqs == sorted([((s_bits[3],), ("1",)), (s_bits[2:], ("1", "0"))])
    _agree_with_yosys(tmp_path, src, module)
