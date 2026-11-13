"""slang frontend: arrays of instances (rtl-buddy-cdc#353).

``sync2 u_sync[1:0] (.clk(clk_b), .d(src), .q(dst));`` used to produce
no cells at all on the pyslang path: the instance array, its flops and
every crossing through it vanished without a diagnostic. Each element
is now lowered as its own instance, named ``u_sync[0]`` / ``u_sync[1]``
like the Yosys ``flatten`` output, for inlined modules and for blackbox
/ greybox boundary cells alike.

Connections follow IEEE 1800-2017 23.3.3.5: an expression of the port's
width goes to every element, one of ``port width * elements`` bits is
split with the leftmost index of the array range taking the leftmost
bits. Yosys ``read_verilog`` agrees for a descending range; for an
ascending one it hands the lowest index the least significant slice, so
there the two frontends attach the same bits to opposite element names
(verdicts are unaffected, and the set of instance paths is the same).

Where yosys is on PATH the same source is also run through the Yosys
frontend and compared; CI's pytest job has no yosys, so there the
slang-side assertions stand alone.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.netlist import Module

if importlib.util.find_spec("pyslang") is None:
    pytest.skip("pyslang not installed", allow_module_level=True)

from rtl_buddy_cdc.frontends import slang as slang_fe  # noqa: E402

_HAVE_YOSYS = shutil.which("yosys") is not None
_FLOPS = {"$dff", "$adff", "$sdff", "$dffe", "$adffe"}
_SLANG_LOG = "rtl_buddy_cdc.frontends.slang"


def _write(tmp_path: Path, src: str, name: str = "m.sv") -> Path:
    sv = tmp_path / name
    sv.write_text(src)
    return sv


def _slang(
    tmp_path: Path, src: str, top: str = "m", **kwargs
) -> tuple[Module, dict[str, Module]]:
    return slang_fe.elaborate_with_blackboxes([_write(tmp_path, src)], top, **kwargs)


def _yosys(
    tmp_path: Path, src: str, top: str = "m"
) -> tuple[Module, dict[str, Module]]:
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
    return netlist.load_with_blackboxes(out)


def _flops(module: Module) -> list:
    return [c for c in module.cells.values() if c.type in _FLOPS]


def _inst(cell_name: str) -> str:
    """Instance part of a flattened cell name on either frontend:
    ``u_sync[0].$slang$dff$1`` and ``$flatten\\u_sync[0].$procdff$2``
    both give ``u_sync[0]``."""
    name = cell_name.removeprefix("$flatten")
    parts = []
    for tok in name.split("."):
        tok = tok.lstrip("\\")
        if tok.startswith("$"):
            break
        parts.append(tok)
    return ".".join(parts)


def _port_bit(module: Module) -> dict:
    """Bit -> ``"port[i]"`` (LSB-first index) over the top ports."""
    return {
        b: f"{name}[{i}]"
        for name, port in module.ports.items()
        for i, b in enumerate(port.bits)
    }


def _first_stage_inputs(module: Module) -> dict[str, list[str]]:
    """Instance -> the top-port bits its flops read on ``D`` (only the
    flops fed straight from a top port: the first synchroniser stage)."""
    names = _port_bit(module)
    out: dict[str, list[str]] = {}
    for c in _flops(module):
        hits = [names[b] for b in c.connections["D"] if b in names]
        if hits:
            out.setdefault(_inst(c.name), []).extend(hits)
    return {k: sorted(v) for k, v in out.items()}


def _last_stage_outputs(module: Module) -> dict[str, list[str]]:
    names = _port_bit(module)
    out: dict[str, list[str]] = {}
    for c in _flops(module):
        hits = [names[b] for b in c.connections["Q"] if b in names]
        if hits:
            out.setdefault(_inst(c.name), []).extend(hits)
    return {k: sorted(v) for k, v in out.items()}


_SDC = """create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 7 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 1 [get_ports d_in]
"""


def _lint(tmp_path: Path, src: str, frontend: str, *extra: str) -> tuple[int, dict]:
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
            *extra,
            str(sv),
        ],  # fmt: skip
    )
    assert result.exit_code in (0, 1), result.output
    return result.exit_code, json.loads(out.read_text())


def _verdict(report: dict) -> tuple:
    """Frontend-neutral verdict: crossing count, per-rule counts, and the
    sorted (rule, instance path) of every kept and suppressed finding."""
    counts: dict[str, int] = {}
    for v in report["violations"]:
        counts[v["rule_id"]] = counts.get(v["rule_id"], 0) + 1
    kept = sorted(
        (v["rule_id"], tuple(v["instance_path"])) for v in report["violations"]
    )
    return report["summary"]["crossings"], counts, kept, report["summary"]["suppressed"]


# --- synchroniser arrays -------------------------------------------------


def _sync_src(rng: str, stages: int = 2) -> str:
    """An array of ``stages``-FF synchronisers: ``clk`` fanned out to both
    elements, ``d`` / ``q`` sliced from 2-bit nets."""
    regs = ", ".join(f"s{i}" for i in range(1, stages + 1))
    chain = " ".join(
        f"s{i} <= {'d' if i == 1 else f's{i - 1}'};" for i in range(1, stages + 1)
    )
    return f"""module sync_n (input clk, input d, output q);
    reg {regs};
    always @(posedge clk) begin {chain} end
    assign q = s{stages};
endmodule

module m (input clk_a, input clk_b, input [1:0] d_in, output reg [1:0] q_out);
    reg [1:0] src;
    always @(posedge clk_a) src <= d_in;
    wire [1:0] dst;
    sync_n u_s{rng} (.clk(clk_b), .d(src), .q(dst));
    always @(posedge clk_b) q_out <= dst;
endmodule
"""


def _element_slices(module: Module, elems: list[str]) -> dict[str, tuple[int, int]]:
    """Per element: which bit of the source register its ``d`` reads and
    which bit of the capture register its ``q`` feeds (LSB-first)."""
    flops = _flops(module)
    (src_flop,) = [c for c in flops if c.connections["D"] == module.ports["d_in"].bits]
    (cap,) = [c for c in flops if c.connections["Q"] == module.ports["q_out"].bits]
    out = {}
    for elem in elems:
        (d_bit,) = module.netnames[f"{elem}.d"].bits
        (q_bit,) = module.netnames[f"{elem}.q"].bits
        out[elem] = (
            src_flop.connections["Q"].index(d_bit),
            cap.connections["D"].index(q_bit),
        )
    return out


@pytest.mark.parametrize("rng", ["[1:0]", "[0:1]", "[4:3]"])
def test_sync_array_lowers_every_element(tmp_path: Path, caplog, rng: str) -> None:
    src = _sync_src(rng)
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module, siblings = _slang(tmp_path, src)
    assert not caplog.records
    assert siblings == {}
    left, right = (int(x) for x in rng.strip("[]").split(":"))
    elems = sorted([f"u_s[{left}]", f"u_s[{right}]"])
    flops = _flops(module)
    # 2-bit source + capture registers, and two 1-bit stages per element.
    assert sorted(_inst(c.name) for c in flops) == sorted(["", ""] + elems * 2)
    # The clock is fanned out unchanged to every element.
    for c in flops:
        if _inst(c.name) in elems:
            assert c.connections["CLK"] == module.ports["clk_b"].bits
    # ``d`` / ``q`` are sliced: the leftmost index of the range takes
    # the MSB of both nets.
    slices = _element_slices(module, elems)
    assert slices == {f"u_s[{left}]": (1, 1), f"u_s[{right}]": (0, 0)}
    # Each element's first stage reads its own slice.
    for elem in elems:
        (stage0,) = [
            c
            for c in flops
            if _inst(c.name) == elem
            and c.connections["D"] == module.netnames[f"{elem}.d"].bits
        ]
        assert stage0.connections["Q"] == module.netnames[f"{elem}.s1"].bits
    if _HAVE_YOSYS:
        y_mod, _ = _yosys(tmp_path, src)
        assert sorted(_inst(c.name) for c in _flops(y_mod)) == sorted(
            _inst(c.name) for c in flops
        )
        y_slices = _element_slices(y_mod, elems)
        if left > right:
            # Descending range: Yosys follows the LRM too.
            assert y_slices == slices
        else:
            # Ascending range: Yosys gives the lowest index the LSB slice.
            assert y_slices == {f"u_s[{left}]": (0, 0), f"u_s[{right}]": (1, 1)}


@pytest.mark.parametrize("rng", ["[1:0]", "[0:1]"])
@pytest.mark.parametrize("stages", [1, 2])
def test_sync_array_verdict_matches_yosys(
    tmp_path: Path, rng: str, stages: int
) -> None:
    """End to end through ``lint``: one CDC-001 per element for a 1-FF
    array, none for a 2-FF one, on both frontends. The 2-bit ``src``
    register synchronised lane by lane is a CDC-020 either way, and with
    a working 2-FF chain the lanes reconverge in ``q_out`` (CDC-005)."""
    src = _sync_src(rng, stages)
    code, report = _lint(tmp_path, src, "slang")
    crossings, counts, kept, _ = _verdict(report)
    assert code == 1
    assert crossings == 2
    lo, hi = sorted(int(x) for x in rng.strip("[]").split(":"))
    cdc001 = [k for k in kept if k[0] == "CDC-001"]
    if stages == 1:
        assert counts == {"CDC-001": 2, "CDC-020": 1}
        assert cdc001 == [("CDC-001", (f"u_s[{lo}]",)), ("CDC-001", (f"u_s[{hi}]",))]
    else:
        assert counts == {"CDC-005": 1, "CDC-020": 1}
    if _HAVE_YOSYS:
        y_code, y_report = _lint(tmp_path, src, "yosys")
        assert y_code == code
        assert _verdict(y_report) == _verdict(report)


def test_inst_waiver_scopes_one_array_element(tmp_path: Path) -> None:
    """An ``inst:`` waiver naming one element suppresses just that
    element's finding, the same way on both frontends."""
    waivers = tmp_path / "w.swl"
    waivers.write_text("waive CDC-001 inst:^u_s\\[1\\]$ element one is fine\n")
    src = _sync_src("[1:0]", 1)
    _code, report = _lint(tmp_path, src, "slang", "--waivers", str(waivers))
    _crossings, counts, kept, suppressed = _verdict(report)
    assert counts == {"CDC-001": 1, "CDC-020": 1}
    assert [k for k in kept if k[0] == "CDC-001"] == [("CDC-001", ("u_s[0]",))]
    assert suppressed == 1
    if _HAVE_YOSYS:
        _y_code, y_report = _lint(tmp_path, src, "yosys", "--waivers", str(waivers))
        assert _verdict(y_report) == _verdict(report)


def test_wide_element_port_takes_whole_slices(tmp_path: Path) -> None:
    """A 2-bit port on a 2-element array splits a 4-bit connection into
    2-bit slices, leftmost index first; a 2-bit connection fans out."""
    src = """module sync_w (input clk, input [1:0] d, output reg [1:0] q);
    reg [1:0] s;
    always @(posedge clk) begin s <= d; q <= s; end
endmodule
module m (input clk_b, input [3:0] d_wide, input [1:0] d_fan,
          output [3:0] q_wide, output [3:0] q_fan);
    sync_w u_w[1:0] (.clk(clk_b), .d(d_wide), .q(q_wide));
    sync_w u_f[1:0] (.clk(clk_b), .d(d_fan), .q(q_fan));
endmodule
"""
    module, _ = _slang(tmp_path, src)
    first = _first_stage_inputs(module)
    assert first == {
        "u_w[0]": ["d_wide[0]", "d_wide[1]"],
        "u_w[1]": ["d_wide[2]", "d_wide[3]"],
        "u_f[0]": ["d_fan[0]", "d_fan[1]"],
        "u_f[1]": ["d_fan[0]", "d_fan[1]"],
    }
    # Each element's last stage drives its slice of the top output port.
    assert _last_stage_outputs(module) == {
        "u_w[0]": ["q_wide[0]", "q_wide[1]"],
        "u_w[1]": ["q_wide[2]", "q_wide[3]"],
        "u_f[0]": ["q_fan[0]", "q_fan[1]"],
        "u_f[1]": ["q_fan[2]", "q_fan[3]"],
    }
    if _HAVE_YOSYS:
        y_mod, _ = _yosys(tmp_path, src)
        assert _first_stage_inputs(y_mod) == first
        assert _last_stage_outputs(y_mod) == _last_stage_outputs(module)


def test_multidimensional_array_is_lowered(tmp_path: Path, caplog) -> None:
    """pyslang exposes ``u[1:0][0:1]`` as an array of arrays; elements are
    named ``u[i][j]`` and sliced outer-leftmost first. (Yosys' parser and
    Icarus both reject a multi-dimensional instance array, so there is
    no second frontend to compare with.)"""
    src = """module ff (input clk, input d, output reg q);
    always @(posedge clk) q <= d;
endmodule
module m (input clk, input [3:0] d, output [3:0] q);
    ff u[1:0][0:1] (.clk(clk), .d(d), .q(q));
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module, _ = _slang(tmp_path, src)
    assert not caplog.records
    assert _first_stage_inputs(module) == {
        "u[1][0]": ["d[3]"],
        "u[1][1]": ["d[2]"],
        "u[0][0]": ["d[1]"],
        "u[0][1]": ["d[0]"],
    }
    assert _last_stage_outputs(module) == {
        "u[1][0]": ["q[3]"],
        "u[1][1]": ["q[2]"],
        "u[0][0]": ["q[1]"],
        "u[0][1]": ["q[0]"],
    }


def test_array_inside_a_child_module(tmp_path: Path) -> None:
    src = """module sync2 (input clk, input d, output q);
    reg s1, s2;
    always @(posedge clk) begin s1 <= d; s2 <= s1; end
    assign q = s2;
endmodule
module bank (input clk, input [1:0] d, output [1:0] q);
    sync2 u_s[1:0] (.clk(clk), .d(d), .q(q));
endmodule
module m (input clk_b, input [3:0] d_in, output [3:0] q_out);
    bank u_lo (.clk(clk_b), .d(d_in[1:0]), .q(q_out[1:0]));
    bank u_hi (.clk(clk_b), .d(d_in[3:2]), .q(q_out[3:2]));
endmodule
"""
    module, _ = _slang(tmp_path, src)
    first = _first_stage_inputs(module)
    assert first == {
        "u_lo.u_s[0]": ["d_in[0]"],
        "u_lo.u_s[1]": ["d_in[1]"],
        "u_hi.u_s[0]": ["d_in[2]"],
        "u_hi.u_s[1]": ["d_in[3]"],
    }
    assert _last_stage_outputs(module) == {
        k: [v[0].replace("d_in", "q_out")] for k, v in first.items()
    }
    assert {n for n in module.netnames if n.endswith(".s1")} == {
        f"{k}.s1" for k in first
    }
    if _HAVE_YOSYS:
        y_mod, _ = _yosys(tmp_path, src)
        assert _first_stage_inputs(y_mod) == first
        assert _last_stage_outputs(y_mod) == _last_stage_outputs(module)
        assert {n for n in y_mod.netnames if n.endswith(".s1")} == {
            n for n in module.netnames if n.endswith(".s1")
        }


def test_generate_loop_instances_not_double_counted(tmp_path: Path) -> None:
    """The pre-existing ``for``-generate path, and an instance array
    inside a generate entry: every flop is emitted exactly once."""
    src = """module sync2 (input clk, input d, output q);
    reg s1, s2;
    always @(posedge clk) begin s1 <= d; s2 <= s1; end
    assign q = s2;
endmodule
module m (input clk_b, input [5:0] d_in, output [5:0] q_out);
    for (genvar i = 0; i < 2; i++) begin : g
        sync2 u (.clk(clk_b), .d(d_in[i]), .q(q_out[i]));
        sync2 u_a[1:0] (.clk(clk_b), .d(d_in[2*i+3:2*i+2]), .q(q_out[2*i+3:2*i+2]));
    end
endmodule
"""
    module, _ = _slang(tmp_path, src)
    insts = sorted(_inst(c.name) for c in _flops(module))
    expected = sorted(
        2 * ["g[0].u", "g[1].u", "g[0].u_a[0]", "g[0].u_a[1]"]
        + 2 * ["g[1].u_a[0]", "g[1].u_a[1]"]
    )
    assert insts == expected
    first = _first_stage_inputs(module)
    assert first == {
        "g[0].u": ["d_in[0]"],
        "g[1].u": ["d_in[1]"],
        "g[0].u_a[0]": ["d_in[2]"],
        "g[0].u_a[1]": ["d_in[3]"],
        "g[1].u_a[0]": ["d_in[4]"],
        "g[1].u_a[1]": ["d_in[5]"],
    }
    if _HAVE_YOSYS:
        y_mod, _ = _yosys(tmp_path, src)
        assert sorted(_inst(c.name) for c in _flops(y_mod)) == insts
        assert _first_stage_inputs(y_mod) == first
        assert _last_stage_outputs(y_mod) == _last_stage_outputs(module)


# --- blackbox arrays ------------------------------------------------------

_BB_ARRAY = """(* blackbox *)
module bb_sync #(parameter integer STAGES = 2) (
    input  wire clk,
    input  wire d,
    output wire q
);
endmodule

module m (input clk_a, input clk_b, input [1:0] d_in, output reg [1:0] q_out);
    reg [1:0] src;
    always @(posedge clk_a) src <= d_in;
    wire [1:0] dst;
    bb_sync #(.STAGES(3)) u_bb[1:0] (.clk(clk_b), .d(src), .q(dst));
    always @(posedge clk_b) q_out <= dst;
endmodule
"""


def _boundary_view(module: Module, cell_type: str) -> dict:
    """Frontend-neutral view of the boundary cells of ``cell_type``: per
    cell, its parameters, pin directions and connections mapped through
    the flops that drive / read them."""
    src_q = {
        b: f"{_inst(c.name) or 'top'}.Q[{i}]"
        for c in _flops(module)
        for i, b in enumerate(c.connections["Q"])
    }
    readers = {
        b: f"{_inst(c.name) or 'top'}.D[{i}]"
        for c in _flops(module)
        for i, b in enumerate(c.connections["D"])
    }
    names = {**_port_bit(module), **src_q}
    view = {}
    for name, cell in module.cells.items():
        if cell.type != cell_type:
            continue
        view[name] = (
            cell.parameters,
            cell.port_directions,
            {
                pin: [readers.get(b) if pin == "q" else names.get(b) for b in bits]
                for pin, bits in cell.connections.items()
            },
        )
    return view


def test_blackbox_array_is_one_boundary_cell_per_element(tmp_path: Path) -> None:
    module, siblings = _slang(tmp_path, _BB_ARRAY)
    view = _boundary_view(module, "bb_sync")
    stages = format(3, "032b")
    pins = {"clk": "input", "d": "input", "q": "output"}
    assert view == {
        "u_bb[0]": (
            {"STAGES": stages},
            pins,
            {"clk": ["clk_b[0]"], "d": ["top.Q[0]"], "q": ["top.D[0]"]},
        ),
        "u_bb[1]": (
            {"STAGES": stages},
            pins,
            {"clk": ["clk_b[0]"], "d": ["top.Q[1]"], "q": ["top.D[1]"]},
        ),
    }
    assert module.cells["u_bb[0]"].attributes["src"]
    assert list(siblings) == ["bb_sync"]
    sub = siblings["bb_sync"]
    assert sub.is_blackbox and not sub.cells
    assert {p.name: (p.direction, len(p.bits)) for p in sub.ports.values()} == {
        "clk": ("input", 1),
        "d": ("input", 1),
        "q": ("output", 1),
    }
    if _HAVE_YOSYS:
        y_mod, y_sib = _yosys(tmp_path, _BB_ARRAY)
        assert _boundary_view(y_mod, "bb_sync") == view
        assert {
            p.name: (p.direction, len(p.bits)) for p in y_sib["bb_sync"].ports.values()
        } == {p.name: (p.direction, len(p.bits)) for p in sub.ports.values()}


def test_blackbox_array_verdict_matches_yosys(tmp_path: Path) -> None:
    """Each boundary cell is its own crossing point: a ``bb_sync`` is no
    recognised synchroniser, so both elements report CDC-001 under their
    own instance path (before rtl-buddy-cdc#353 the slang path reported
    nothing at all)."""
    code, report = _lint(tmp_path, _BB_ARRAY, "slang")
    assert code == 1
    assert _verdict(report) == (
        2,
        {"CDC-001": 2, "CDC-020": 1},
        [
            ("CDC-001", ("u_bb[0]",)),
            ("CDC-001", ("u_bb[1]",)),
            ("CDC-020", ()),
        ],
        0,
    )
    if _HAVE_YOSYS:
        y_code, y_report = _lint(tmp_path, _BB_ARRAY, "yosys")
        assert y_code == code
        assert _verdict(y_report) == _verdict(report)


_SUB_ARRAY = """module sub (input clk, input d, output reg q);
    reg s;
    always @(posedge clk) begin s <= d; q <= s; end
endmodule
module m (input clk_a, input clk_b, input [1:0] d_in, output reg [1:0] q_out);
    reg [1:0] src;
    always @(posedge clk_a) src <= d_in;
    wire [1:0] o;
    sub u_sub[1:0] (.clk(clk_b), .d(src), .q(o));
    always @(posedge clk_b) q_out <= o;
endmodule
"""


@pytest.mark.parametrize("option", ["blackbox", "greybox"])
def test_cli_boundary_option_applies_to_array_elements(
    tmp_path: Path, option: str
) -> None:
    module, siblings = _slang(tmp_path, _SUB_ARRAY, **{option: ["sub"]})
    assert {n for n, c in module.cells.items() if c.type == "sub"} == {
        "u_sub[0]",
        "u_sub[1]",
    }
    assert len(_flops(module)) == 2  # the elements' bodies stay out
    sub = siblings["sub"]
    assert sub.is_blackbox
    assert len(_flops(sub)) == (2 if option == "greybox" else 0)
    for name in ("u_sub[0]", "u_sub[1]"):
        assert module.cells[name].port_directions == {
            "clk": "input",
            "d": "input",
            "q": "output",
        }


# --- what is still not lowered warns -------------------------------------


def test_primitive_array_and_primitive_warn(tmp_path: Path, caplog) -> None:
    """Gate primitives are not lowered by this frontend; an array of them
    (or a lone one) is reported instead of vanishing."""
    src = """module m (input [1:0] a, input [1:0] b, output [1:0] y, output z);
    and g_and[1:0] (y, a, b);
    or g_or (z, a[0], b[0]);
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("instance array element not lowered" in m for m in msgs) == 2, msgs
    assert any("g_and[1] is a PrimitiveInstanceSymbol" in m for m in msgs), msgs
    assert any("primitive instance not lowered" in m and "g_or" in m for m in msgs)


def test_array_without_elements_warns(tmp_path: Path, caplog) -> None:
    class _Empty:
        range = None
        elements: list = []
        isUninstantiated = False
        syntax = None
        sourceRange = None
        location = None

    module, _ = _slang(tmp_path, _SUB_ARRAY)
    builder = slang_fe._ModuleBuilder.__new__(slang_fe._ModuleBuilder)
    builder.comp = types.SimpleNamespace(sourceManager=None)
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        builder._emit_instance_array(_Empty(), "", "u_x")
    assert any(
        "instance array not lowered (no elaborated elements)" in r.getMessage()
        for r in caplog.records
    )
    assert module.cells  # the real design is unaffected


# --- selects the per-element slices rely on -------------------------------


def test_offset_and_ascending_vector_selects_match_yosys(tmp_path: Path) -> None:
    """slang slices an array connection with the vector's own declared
    indices (``src[4]`` of ``[4:1]``, ``src[3:2][2]``), so selects of a
    non-zero-based or ascending vector must honour the declared range.
    Indexed part-selects too."""
    src = """module m (input clk, input [4:1] a, input [0:3] b, input [7:0] c,
          output reg q1, output reg [1:0] q2, output reg q3,
          output reg [1:0] q4, output reg [1:0] q5, output reg [1:0] q6);
    always @(posedge clk) begin
        q1 <= a[1]; q2 <= a[4:3]; q3 <= b[0]; q4 <= b[2:3];
        q5 <= c[4 +: 2]; q6 <= c[4 -: 2];
    end
endmodule
"""
    module, _ = _slang(tmp_path, src)

    def d_of(m: Module) -> dict[str, list[str]]:
        names = _port_bit(m)
        out = {}
        for c in _flops(m):
            (q,) = {names[b].split("[")[0] for b in c.connections["Q"]}
            out[q] = [names.get(b, "?") for b in c.connections["D"]]
        return out

    got = d_of(module)
    assert got == {
        "q1": ["a[0]"],
        "q2": ["a[2]", "a[3]"],
        "q3": ["b[3]"],
        "q4": ["b[0]", "b[1]"],
        "q5": ["c[4]", "c[5]"],
        "q6": ["c[3]", "c[4]"],
    }
    if _HAVE_YOSYS:
        y_mod, _ = _yosys(tmp_path, src)
        assert d_of(y_mod) == got


def test_array_with_offset_vector_connection(tmp_path: Path) -> None:
    src = """module ff (input clk, input d, output reg q);
    always @(posedge clk) q <= d;
endmodule
module m (input clk, input [4:1] d, input [0:1] e, output [4:1] q, output [0:1] r);
    ff u_d[3:0] (.clk(clk), .d(d), .q(q));
    ff u_e[1:0] (.clk(clk), .d(e), .q(r));
endmodule
"""
    module, _ = _slang(tmp_path, src)
    first = _first_stage_inputs(module)
    # ``d``'s LSB (d[1]) goes to the rightmost element; ``e``'s LSB is
    # e[1], stored as bit 0.
    assert first == {
        "u_d[0]": ["d[0]"],
        "u_d[1]": ["d[1]"],
        "u_d[2]": ["d[2]"],
        "u_d[3]": ["d[3]"],
        "u_e[0]": ["e[0]"],
        "u_e[1]": ["e[1]"],
    }
    assert _last_stage_outputs(module) == {
        "u_d[0]": ["q[0]"],
        "u_d[1]": ["q[1]"],
        "u_d[2]": ["q[2]"],
        "u_d[3]": ["q[3]"],
        "u_e[0]": ["r[0]"],
        "u_e[1]": ["r[1]"],
    }
    if _HAVE_YOSYS:
        y_mod, _ = _yosys(tmp_path, src)
        assert _first_stage_inputs(y_mod) == first
        assert _last_stage_outputs(y_mod) == _last_stage_outputs(module)
