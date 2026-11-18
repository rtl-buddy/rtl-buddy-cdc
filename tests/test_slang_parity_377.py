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
from rtl_buddy_cdc.liveness import prune_dead_cells
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
    ``netname[i]`` holding it, else ``<cell type>.<pin>`` of its driver;
    constants stay as they are."""
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
                # No bit index: the frontends split a mux into chunks
                # differently (a whole-register mux vs one per written part).
                drivers.setdefault(b, f"{c.type}.{pin}")

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
    """Same flop lanes as the Yosys path, after the dead-logic pruning
    ``lint`` applies to the slang netlist (Yosys' ``opt_clean`` removes a
    register nothing reads)."""
    if not _HAVE_YOSYS:
        return
    pruned = prune_dead_cells(slang_mod, {})
    assert _flop_lanes(pruned) == _flop_lanes(_yosys(tmp_path, src))


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
    always @* begin y = b; if (a[0] -> b[0]) y = a; end
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


# --- 5. blocking writes in clocked blocks ----------------------------------


@pytest.mark.parametrize(
    ("kw", "body"),
    [
        ("always_ff", "q = d;"),
        ("always_ff", "begin t = d + 2'd1; q <= t; end"),
        ("always", "begin t = d; if (en) t = ~d; q <= t ^ d; end"),
        ("always", "begin q <= t; t = d; end"),
    ],
    ids=["ff_blocking", "ff_temp", "cond_temp", "read_first"],
)
def test_blocking_write_value_reaches_later_reads(
    tmp_path: Path, caplog, kw: str, body: str
) -> None:
    """A blocking write in an ``always_ff`` used to be skipped without a
    word; in a plain ``always`` it was registered, but a later read in
    the same block saw the register (with a warning), where synthesis
    forwards the value just written. Both are lowered as Yosys does:
    each written variable is a register, and reads after the write see
    the written value."""
    src = f"""module m (input clk, input en, input [1:0] d, output reg [1:0] q);
    reg [1:0] t;
    {kw} @(posedge clk) {body}
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not _warnings(caplog)
    assert any(c.connections["Q"] == module.ports["q"].bits for c in _flops(module))
    _agree_with_yosys(tmp_path, src, module)


def test_blocking_temporary_does_not_add_a_sync_stage(tmp_path: Path) -> None:
    """``s1 = x; q = s1;`` in one clocked block is one stage, not two: ``q``
    loads ``x`` directly. The slang path counted a two-flop
    synchroniser here; Yosys sees a single flop stage."""
    src = """module m (input clk_a, input clk_b, input d, output reg q);
    reg a_q, s1;
    always @(posedge clk_a) a_q <= d;
    always @(posedge clk_b) begin s1 = a_q; q = s1; end
endmodule
"""
    module = _slang(tmp_path, src)
    assert ("q[0]", "a_q[0]", "clk_b[0]", 1) in _flop_lanes(module)
    _agree_with_yosys(tmp_path, src, module)
    crossings, rules = _same_verdict(tmp_path, src)
    assert crossings >= 1 and rules


def test_nonblocking_write_in_combinational_block(tmp_path: Path) -> None:
    """``y <= a`` in ``always_comb`` / ``always @*`` was skipped (``y``
    undriven); Yosys lowers it like ``y = a``."""
    src = """module m (input clk, input a, input b, output reg q, output reg r);
    logic y, z;
    always_comb y <= a;
    always @* z <= b;
    always @(posedge clk) begin q <= y; r <= z; end
endmodule
"""
    module = _slang(tmp_path, src)
    lanes = _flop_lanes(module)
    assert ("q[0]", "a[0]", "clk[0]", 1) in lanes
    assert ("r[0]", "b[0]", "clk[0]", 1) in lanes


# --- 6. loops and other statements ------------------------------------------


@pytest.mark.parametrize(
    "block",
    [
        # gray -> binary, Verilog-2001 loop variable and step.
        "integer i; always @* for (i = 0; i <= 3; i = i + 1) y[i] = ^(g >> i);",
        "always_comb for (int i = 0; i < 4; i++) y[i] = g[3 - i];",
        "always_comb begin y = 4'd0; for (int i = 3; i >= 0; i -= 1)"
        " y = y ^ (g >> i); end",
    ],
    ids=["v2001_comb", "sv_comb", "compound_step"],
)
def test_loops_unroll(tmp_path: Path, caplog, block: str) -> None:
    """A ``for`` loop in a combinational block, and a clocked one whose
    variable is declared outside the loop or stepped as ``i = i - 1``,
    used to be skipped without a word."""
    src = f"""module m (input clk, input [3:0] g, output reg [3:0] q);
    reg [3:0] y;
    {block}
    always @(posedge clk) q <= y;
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not _warnings(caplog)
    assert not _cells(module, "$_UNKNOWN_")
    (q_flop,) = _flops(module)
    driven = {b for c in module.cells.values() for b in c.connections.get("Y", ())}
    assert set(q_flop.connections["D"]) <= driven | set(module.ports["g"].bits)
    _agree_with_yosys(tmp_path, src, module)


def test_loop_variable_read_as_a_value(tmp_path: Path) -> None:
    """The loop variable read as a value is its iteration's constant (it
    read as an undriven variable)."""
    src = """module m (output logic [3:0] y);
    always_comb begin y = 4'd0; for (int i = 1; i < 3; i++) y = y + i; end
endmodule
"""
    module = _slang(tmp_path, src)
    adds = _cells(module, "$add")
    assert [c.connections["B"][:2] for c in adds] == [("1", "0"), ("0", "1")]


def test_function_with_loop_inlines(tmp_path: Path) -> None:
    """The corpus' ``gray2bin()``: a function whose body is a loop."""
    src = """module m (input clk, input [3:0] g, output reg [3:0] q);
    function [3:0] gray2bin(input [3:0] x);
        integer i;
        for (i = 0; i <= 3; i = i + 1) gray2bin[i] = ^(x >> i);
    endfunction
    always @(posedge clk) q <= gray2bin(g);
endmodule
"""
    module = _slang(tmp_path, src)
    assert not _cells(module, "$_UNKNOWN_")
    assert len(_cells(module, "$reduce_xor")) == 4
    _agree_with_yosys(tmp_path, src, module)


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("for (int i = 0; i < n; i++) q[i] = a;", "`for` loop not lowered"),
        ("while (n[0]) q = a;", "`WhileLoopStatement` is not modelled"),
        # A constant ``repeat`` is unrolled (rtl-buddy-cdc#405).
        ("repeat (n) q = a;", "`repeat` loop not lowered"),
        ("t(q);", "`ExpressionStatement` is not modelled"),
    ],
    ids=["runtime_bound", "while", "repeat", "task_call"],
)
@pytest.mark.parametrize("kw", ["always_comb", "always_ff @(posedge clk)"])
def test_unmodelled_statements_warn(
    tmp_path: Path, caplog, body: str, needle: str, kw: str
) -> None:
    src = f"""module m (input clk, input [1:0] n, input a, output logic [3:0] q);
    task automatic t(output logic [3:0] o); o = 4'd1; endtask
    {kw} begin {body} end
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    assert any(needle in m for m in _warnings(caplog)), _warnings(caplog)


def test_system_tasks_and_assertions_are_silent(tmp_path: Path, caplog) -> None:
    src = """module m (input clk, input a, output logic q);
    always_ff @(posedge clk) begin
        $display("a=%b", a);
        assert (a == a);
        q <= a;
    end
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not _warnings(caplog)
    assert ("q[0]", "a[0]", "clk[0]", 1) in _flop_lanes(module)


@pytest.mark.parametrize(
    "body",
    [
        "begin n++; q <= n; end",
        "begin n += 2'd1; q <= n; end",
        "begin n <= n - 2'd1; --n; q <= n; end",
    ],
    ids=["incr", "compound", "decr"],
)
def test_increments_and_compound_assignments(tmp_path: Path, caplog, body: str) -> None:
    """``n++`` / ``--n`` statements were skipped; ``n += 1`` read an
    opaque value for its own target."""
    src = f"""module m (input clk, output reg [1:0] q);
    reg [1:0] n;
    always @(posedge clk) {body}
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not _warnings(caplog)
    assert not _cells(module, "$_UNKNOWN_")
    (n_flop,) = [
        c for c in _flops(module) if c.connections["Q"] == module.netnames["n"].bits
    ]
    assert n_flop.connections["D"] != n_flop.connections["Q"]
    _agree_with_yosys(tmp_path, src, module)


@pytest.mark.parametrize(
    ("body", "latched"),
    [
        ("if (en) q = a; else q = b;", set()),
        ("if (en) begin q = a; r = b; end", {"q[0]", "r[0]"}),
        ("case (s) 2'd0: q = a; 2'd1: r = b; endcase", {"q[0]", "r[0]"}),
    ],
    ids=["else", "two_writes", "case"],
)
def test_always_latch_shapes(tmp_path: Path, body: str, latched: set[str]) -> None:
    """``always_latch`` bodies beyond the single ``if (en) q = d;`` were
    skipped silently (or, with an ``else``, half-lowered)."""
    src = f"""module m (input en, input [1:0] s, input a, input b, output logic q,
          output logic r);
    always_latch begin {body} end
endmodule
"""
    module = _slang(tmp_path, src)
    assert set(_latches(module)) == latched
    if _HAVE_YOSYS and latched:
        # (Yosys rejects an ``always_latch`` that infers no latch.)
        assert _latches(module) == _latches(_yosys(tmp_path, src))


# --- 7. one register, one flop per contiguous run ---------------------------


@pytest.mark.parametrize(
    "block",
    [
        "always @(posedge clk) begin p[0] <= a[0]; p[1] <= b[1]; end",
        "always @(posedge clk) begin p <= a; if (c) p[1] <= b[1]; end",
        "always @(posedge clk) begin p[3] <= a[3]; p[1:0] <= b[1:0]; end",
        "always @(posedge clk) begin if (c) p <= a; else p[2:1] <= b[2:1]; end",
        "always @(posedge clk or negedge rst_n) if (!rst_n) p <= 4'b1010;"
        " else begin p[0] <= a[0]; if (c) p[3:1] <= b[3:1]; end",
        "integer j; always @(posedge clk) begin p[0] <= a[0];"
        " for (j = 3; j > 0; j = j - 1) p[j] <= p[j - 1]; end",
        "always_ff @(posedge clk) begin t = a; t[1] = c; p <= t; end",
        "always @(posedge clk) for (int i = 0; i < 4; i++)"
        " begin p[i] = t[i]; t[i] = b[i]; end",
    ],
    ids=[
        "disjoint",
        "whole_then_part",
        "two_runs",
        "branches",
        "reset_value",
        "pipeline_loop",
        "blocking_partial",
        "blocking_loop",
    ],
)
def test_register_parts_merge_like_proc(tmp_path: Path, block: str) -> None:
    """Each write used to be its own flop: ``p <= a; if (c) p[1] <= b;``
    gave two flops driving ``p[1]``, and per-bit writes (an unrolled loop
    among them) one 1-bit flop each. Yosys ``proc`` makes one flop per
    contiguous run of the bits a block writes."""
    src = f"""module m (input clk, input rst_n, input c, input [3:0] a, input [3:0] b,
          output reg [3:0] p);
    reg [3:0] t;
    {block}
endmodule
"""
    module = _slang(tmp_path, src)
    q_bits = [b for c in _flops(module) for b in c.connections["Q"]]
    assert len(q_bits) == len(set(q_bits)), "a register bit driven by two flops"
    _agree_with_yosys(tmp_path, src, module)


def test_reset_value_assembled_per_bit(tmp_path: Path) -> None:
    src = """module m (input clk, input rst_n, input [3:0] a, output reg [3:0] p);
    always @(posedge clk or negedge rst_n)
        if (!rst_n) begin p[3:2] <= 2'b10; p[1:0] <= 2'b01; end
        else begin p[0] <= a[0]; p[3:1] <= a[3:1]; end
endmodule
"""
    (flop,) = _flops(_slang(tmp_path, src))
    assert flop.parameters["ARST_VALUE"] == "1001"


def test_unpacked_array_elements_stay_separate(tmp_path: Path) -> None:
    """The stages of an unpacked-array synchroniser are separate registers
    (Yosys ``mem2reg``), so they are not merged into one flop."""
    src = """module m (input clk, input d, output q);
    logic chain [3];
    always_ff @(posedge clk) begin
        chain[0] <= d;
        for (int i = 1; i < 3; i++) chain[i] <= chain[i - 1];
    end
    assign q = chain[2];
endmodule
"""
    module = _slang(tmp_path, src)
    assert sorted(len(c.connections["Q"]) for c in _flops(module)) == [1, 1, 1]


# --- 8. gate primitives -------------------------------------------------------

_GATES = """module m (input clk_a, input clk_b, input a, input b, input c,
          output y1, output y2, output y3, output y4, output y5, output y6,
          output reg q);
    and g1 (y1, a, b, c);
    nand g2 (y2, a, b);
    not n1 (y3, y4, a);
    buf (y5, b);
    xnor #1 (y6, a, b);
    reg a_q, s1;
    wire x;
    always @(posedge clk_a) a_q <= a;
    or g3 (x, a_q, b);
    always @(posedge clk_b) begin s1 <= x; q <= s1; end
endmodule
"""


def test_gate_primitives_lower_like_read_verilog(tmp_path: Path, caplog) -> None:
    """Gates were reported (rtl-buddy-cdc#353) but not lowered, so their
    logic was missing: here the crossing through the ``or`` gate."""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, _GATES)
    assert not _warnings(caplog)
    types = sorted(
        c.type for c in module.cells.values() if not c.type.startswith("$dff")
    )
    assert types == ["$and", "$and", "$and", "$not", "$not", "$not", "$or", "$xor"]
    assert module.ports["y5"].bits == module.ports["b"].bits
    assert ("s1[0]", "x[0]", "clk_b[0]", 1) in _flop_lanes(module)
    # The wire ``x`` has a netname on both paths since rtl-buddy-cdc#384.
    _agree_with_yosys(tmp_path, _GATES, module)
    assert _same_verdict(tmp_path, _GATES)[0] >= 1


def test_gate_primitive_array(tmp_path: Path) -> None:
    """An array of gates is one gate per element on its slices (Yosys
    rejects primitive arrays, so there is nothing to compare with)."""
    src = """module m (input [1:0] v, input [1:0] u, output [1:0] y);
    or ga[1:0] (y, v, u);
endmodule
"""
    module = _slang(tmp_path, src)
    ors = sorted(
        (c.connections["A"], c.connections["B"], c.connections["Y"])
        for c in _cells(module, "$or")
    )
    v, u, y = (module.ports[n].bits for n in ("v", "u", "y"))
    assert ors == sorted(((v[i],), (u[i],), (y[i],)) for i in range(2))


@pytest.mark.parametrize(
    "inst",
    ["bufif1 t (y, a, b);", "pullup (y);", "tran s (y, a);"],
    ids=["tristate", "pull", "switch"],
)
def test_unmodelled_primitives_warn(tmp_path: Path, caplog, inst: str) -> None:
    src = f"""module m (input a, input b, output y);
    {inst}
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    assert any("primitive instance not lowered" in m for m in _warnings(caplog))


# --- 9. run-time index writes on any declared range ------------------------


@pytest.mark.parametrize(
    "decl",
    ["logic [4:1] v", "logic [0:3] v", "logic [3:0] v", "logic [1:0][1:0] v"],
    ids=["offset", "ascending", "zero_based", "packed_2d"],
)
def test_runtime_index_decoder_selects_the_declared_bit(
    tmp_path: Path, decl: str
) -> None:
    """``v[i] = 1`` used ``i`` as the bit position whatever ``v``'s range:
    ``logic [4:1] v`` set the wrong bit and an ascending range came out
    reversed. Simulated here on the netlist for every index."""
    elem = "2'b01" if "[1:0][1:0]" in decl else "1'b1"
    src = f"""module m (input [1:0] i, output {decl});
    always_comb begin v = '0; v[i] = {elem}; end
endmodule
"""
    module = _slang(tmp_path, src)

    def evaluate(index: int) -> tuple[str, ...]:
        values: dict[Bit, int] = {
            b: (index >> k) & 1 for k, b in enumerate(module.ports["i"].bits)
        }
        values.update({"0": 0, "1": 1})
        cells = list(module.cells.values())
        for _ in range(len(cells)):
            for c in cells:
                ins = {
                    p: [values.get(b) for b in bits]
                    for p, bits in c.connections.items()
                    if p != "Y"
                }
                if any(x is None for bits in ins.values() for x in bits):
                    continue
                num = {
                    p: sum(x << k for k, x in enumerate(bits))
                    for p, bits in ins.items()
                }
                y = c.connections["Y"]
                mask = (1 << len(y)) - 1
                if c.type == "$sub":
                    r = num["A"] - num["B"]
                elif c.type == "$mul":
                    r = num["A"] * num["B"]
                elif c.type == "$shl":
                    r = num["A"] << num["B"]
                elif c.type == "$neg":
                    r = -num["A"]
                elif c.type == "$shift":
                    # ``B`` signed: a negative amount shifts left.
                    b_bits = len(c.connections["B"])
                    amount = num["B"] - ((num["B"] >> (b_bits - 1)) << b_bits)
                    r = num["A"] >> amount if amount >= 0 else num["A"] << -amount
                else:
                    raise AssertionError(c.type)
                for k, b in enumerate(y):
                    values[b] = ((r & mask) >> k) & 1
        return tuple(str(values[b]) for b in module.ports["v"].bits)

    width = len(module.ports["v"].bits)
    for index in range(4):
        # Expected LSB-first bits of v after v[index] = elem, by the LRM.
        if "[4:1]" in decl:
            pos = [index - 1] if 1 <= index <= 4 else []
        elif "[0:3]" in decl:
            pos = [3 - index]
        elif "[1:0][1:0]" in decl:
            pos = [2 * index] if index < 2 else []
        else:
            pos = [index]
        want = tuple("1" if k in pos else "0" for k in range(width))
        assert evaluate(index) == want, index


# --- the corpus' remaining opaque right-hand sides --------------------------


def test_constant_ternary_and_power_fold(tmp_path: Path, caplog) -> None:
    """``EN ? bus[OFF +: W] : '0`` with the field disabled (the dead arm is
    not even a valid select) and ``x * 2**N`` were opaque drivers on the
    slang path (axis_async_fifo, ptp_clock_cdc); Yosys folds both."""
    src = """module m #(parameter EN = 0, parameter OFF = 4) (input [3:0] a,
          input signed [3:0] x, output [1:0] y, output signed [7:0] z);
    assign y = EN ? a[OFF +: 2] : 2'b00;
    assign z = x * 2**3;
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module = _slang(tmp_path, src)
    assert not _warnings(caplog)
    assert module.ports["y"].bits == ("0", "0")
    (mul,) = _cells(module, "$mul")
    assert mul.connections["B"][:4] == ("0", "0", "0", "1")
    assert module.ports["z"].bits == mul.connections["Y"][:8]


@pytest.mark.parametrize(
    "body",
    [
        "if (en) begin t = d + 2'd1; q <= t; end",
        "case (d) 2'd0: begin t = ~d; q <= t; end default: q <= d; endcase",
        "begin if (en) t = d; else t = ~d; q <= t; end",
    ],
    ids=["if_arm", "case_arm", "both_arms"],
)
def test_blocking_value_is_arm_local(tmp_path: Path, body: str) -> None:
    """Inside the ``if`` / ``case`` arm that writes it, a blocking value is
    the written one, as ``proc`` evaluates the arm; only after the
    statement is it muxed with the prior value. Muxing at the write kept
    the temporary's register alive (the corpus' ``rd_ptr_temp`` /
    ``wr_ptr_temp`` in axis_async_fifo, 26 flop bits Yosys does not
    have)."""
    src = f"""module m (input clk, input en, input [1:0] d, output reg [1:0] q);
    reg [1:0] t;
    always @(posedge clk) {body}
endmodule
"""
    module = prune_dead_cells(_slang(tmp_path, src), {})
    if "else" not in body:
        # ``t`` is never read before it is written: no register survives.
        assert all(
            c.connections["Q"] != module.netnames["t"].bits for c in _flops(module)
        )
    _agree_with_yosys(tmp_path, src, module)
