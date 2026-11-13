"""slang frontend: plain ``always`` blocks and blackbox instances
(rtl-buddy-cdc#345).

Two gaps that made the pyslang path report nothing where the Yosys path
reports findings:

- A Verilog-2001 ``always`` block was dropped: only ``always_ff`` /
  ``always_comb`` / ``always_latch`` were lowered. An edge-triggered
  ``always`` now lowers like ``always_ff``, ``always @*`` (or a
  level-sensitive list) like ``always_comb``, a latch-inferring one like
  ``always_latch``, and anything still unlowerable logs a warning.
- A ``(* blackbox *)`` module elaborated as an empty body, so its
  instance and every crossing through it vanished. It is now a boundary
  cell plus a bodyless sibling module, the shape
  :func:`rtl_buddy_cdc.netlist.load_with_blackboxes` returns for the
  Yosys netlist; ``--blackbox`` / ``--greybox`` work on this path too.

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
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.netlist import Module

if importlib.util.find_spec("pyslang") is None:
    pytest.skip("pyslang not installed", allow_module_level=True)

from rtl_buddy_cdc.frontends import slang as slang_fe  # noqa: E402
from rtl_buddy_cdc.frontends.slang import SlangElaborationError  # noqa: E402

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
    return sorted(
        (c for c in module.cells.values() if c.type in _FLOPS),
        key=lambda c: c.connections["Q"],
    )


def _flop_shape(module: Module) -> list[tuple]:
    """Frontend-neutral flop summary: type, width, clock polarity and,
    for an async-reset flop, reset polarity and value."""
    out = []
    for c in _flops(module):
        p = c.parameters
        out.append(
            (
                c.type,
                len(c.connections["Q"]),
                p.get("CLK_POLARITY"),
                p.get("ARST_POLARITY"),
                p.get("ARST_VALUE"),
            )
        )
    return sorted(out)


def _driver_types(module: Module, bits: tuple) -> set[str]:
    return {
        c.type
        for c in module.cells.values()
        for pin, b in c.connections.items()
        if pin in ("Y", "Q") and set(b) & set(bits)
    }


def _assert_same_flops_as_yosys(tmp_path: Path, src: str) -> None:
    if not _HAVE_YOSYS:
        return
    s_mod, _ = _slang(tmp_path, src)
    y_mod, _ = _yosys(tmp_path, src)
    assert _flop_shape(s_mod) == _flop_shape(y_mod)


# --- plain ``always`` ---------------------------------------------------


def test_plain_always_posedge_lowers_to_dff(tmp_path: Path, caplog) -> None:
    src = """module m (input clk, input [3:0] d, output reg [3:0] q);
    always @(posedge clk) q <= d;
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module, siblings = _slang(tmp_path, src)
    (flop,) = _flops(module)
    assert flop.type == "$dff"
    assert flop.parameters["CLK_POLARITY"] == "1"
    assert flop.connections["CLK"] == module.ports["clk"].bits
    assert flop.connections["D"] == module.ports["d"].bits
    assert flop.connections["Q"] == module.ports["q"].bits
    assert siblings == {}
    assert not caplog.records
    _assert_same_flops_as_yosys(tmp_path, src)


def test_plain_always_negedge_clock_and_blocking_write(tmp_path: Path) -> None:
    """Verilog-2001 code also registers through a blocking ``=``; a plain
    ``always`` accepts it (``always_ff`` still does not)."""
    src = """module m (input clk, input d, output reg q);
    always @(negedge clk) q = d;
endmodule
"""
    module, _ = _slang(tmp_path, src)
    (flop,) = _flops(module)
    assert flop.type == "$dff"
    assert flop.parameters["CLK_POLARITY"] == "0"
    _assert_same_flops_as_yosys(tmp_path, src)


@pytest.mark.parametrize(
    "events",
    ["posedge clk or negedge rst_n", "negedge rst_n or posedge clk"],
    ids=["clock_first", "reset_first"],
)
def test_plain_always_async_reset_lowers_to_adff(tmp_path: Path, events: str) -> None:
    """The reset may be listed first; the body's ``if`` decides which event
    is the reset, as in synthesis. Applies to ``always_ff`` too."""
    src = f"""module m (input clk, input rst_n, input en, input [3:0] d,
          output reg [3:0] q);
    always @({events})
        if (!rst_n) q <= 4'd5;
        else if (en) q <= d;
endmodule
"""
    module, _ = _slang(tmp_path, src)
    (flop,) = _flops(module)
    assert flop.type == "$adff"
    assert flop.connections["CLK"] == module.ports["clk"].bits
    assert flop.connections["ARST"] == module.ports["rst_n"].bits
    assert flop.parameters["ARST_POLARITY"] == "0" * 32
    assert flop.parameters["ARST_VALUE"] == "0101"
    assert flop.parameters["CLK_POLARITY"] == "1"
    # The enable is a hold mux in front of D, not a direct wire.
    assert flop.connections["D"] != module.ports["d"].bits
    assert "$mux" in _driver_types(module, flop.connections["D"])
    _assert_same_flops_as_yosys(tmp_path, src)


def test_always_ff_reset_listed_first(tmp_path: Path) -> None:
    src = """module m (input logic clk, rst_n, d, output logic q);
    always_ff @(negedge rst_n or posedge clk)
        if (!rst_n) q <= 1'b0; else q <= d;
endmodule
"""
    module, _ = _slang(tmp_path, src)
    (flop,) = _flops(module)
    assert flop.type == "$adff"
    assert flop.connections["CLK"] == module.ports["clk"].bits
    assert flop.connections["ARST"] == module.ports["rst_n"].bits


@pytest.mark.parametrize(
    "sens",
    ["@*", "@(*)", "@(a or b)", "@(a, b)"],
    ids=["star", "paren_star", "or", "comma"],
)
def test_plain_always_level_sensitive_lowers_as_comb(tmp_path: Path, sens: str) -> None:
    src = f"""module m (input clk, input a, input b, output reg q);
    reg y;
    always {sens} y = a & b;
    always @(posedge clk) q <= y;
endmodule
"""
    module, _ = _slang(tmp_path, src)
    (flop,) = _flops(module)
    assert _driver_types(module, flop.connections["D"]) == {"$and"}
    assert "$dlatch" not in {c.type for c in module.cells.values()}
    _assert_same_flops_as_yosys(tmp_path, src)


def test_plain_always_star_with_default_assignment_is_comb(tmp_path: Path) -> None:
    """``y = 0; if (en) y = a;`` assigns ``y`` on every path: no latch."""
    src = """module m (input en, input a, input [1:0] s, output reg y, output reg z);
    always @* begin
        y = 1'b0;
        if (en) y = a;
        case (s)
            2'd0: z = a;
            default: z = en;
        endcase
    end
endmodule
"""
    module, _ = _slang(tmp_path, src)
    assert "$dlatch" not in {c.type for c in module.cells.values()}
    # Exactly what the same body produces as ``always_comb``.
    comb, _ = _slang(tmp_path, src.replace("always @*", "always_comb"))
    assert sorted(c.type for c in module.cells.values()) == sorted(
        c.type for c in comb.cells.values()
    )
    assert "$mux" in {c.type for c in module.cells.values()}


def test_plain_always_latch_lowers_to_dlatch(tmp_path: Path) -> None:
    src = """module m (input en, input [1:0] d, output reg [1:0] q);
    always @(en or d) if (en) q = d;
endmodule
"""
    module, _ = _slang(tmp_path, src)
    (latch,) = [c for c in module.cells.values() if c.type == "$dlatch"]
    assert latch.connections["EN"] == module.ports["en"].bits
    assert latch.connections["D"] == module.ports["d"].bits
    assert latch.connections["Q"] == module.ports["q"].bits
    if _HAVE_YOSYS:
        y_mod, _ = _yosys(tmp_path, src)
        assert [c.type for c in y_mod.cells.values()] == ["$dlatch"]


def test_plain_always_constant_condition_folds(tmp_path: Path) -> None:
    """A parameter-false ``if`` contributes no path: ``y`` is complete."""
    src = """module m #(parameter P = 0) (input a, input b, output reg y);
    always @* begin
        if (P) y = a; else y = b;
    end
endmodule
"""
    module, _ = _slang(tmp_path, src)
    assert "$dlatch" not in {c.type for c in module.cells.values()}


@pytest.mark.parametrize(
    "block, reason",
    [
        ("always @(posedge clk or b) q <= a;", "not edge-triggered on every event"),
        ("always @(edge clk) q <= a;", "not edge-triggered on every event"),
        ("always #5 q = ~q;", "not edge-triggered on every event"),
        ("always begin @(posedge clk); q <= a; end", "not edge-triggered"),
        (
            "always @* begin if (a) q = b; else if (b) q = a; end",
            "infers a latch in a shape other than",
        ),
        (
            "always @* case (a) 1'b0: q = b; endcase",
            "infers a latch in a shape other than",
        ),
    ],
    ids=["mixed", "edge", "delay", "inner_event", "latch_elsif", "latch_case"],
)
def test_unlowerable_always_warns(
    tmp_path: Path, caplog, block: str, reason: str
) -> None:
    src = f"""module m (input clk, input a, input b, output reg q);
    {block}
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module, _ = _slang(tmp_path, src)
    assert not _flops(module)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("procedural block not lowered" in m and reason in m for m in msgs), msgs
    # The location is the block's own source range.
    assert any("m.sv:2." in m for m in msgs), msgs


def test_always_ff_without_plain_clock_warns(tmp_path: Path, caplog) -> None:
    src = """module m (input logic [1:0] clk, input logic d, output logic q);
    always_ff @(posedge clk[0]) q <= d;
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module, _ = _slang(tmp_path, src)
    assert not _flops(module)
    assert any("clock is not a plain signal" in r.getMessage() for r in caplog.records)


def test_blocking_temporary_in_edge_always_warns(tmp_path: Path, caplog) -> None:
    src = """module m (input clk, input a, output reg q);
    reg t;
    always @(posedge clk) begin t = a; q = t; end
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module, _ = _slang(tmp_path, src)
    assert len(_flops(module)) == 2
    msgs = [r.getMessage() for r in caplog.records]
    assert any("blocking-assigned `t`" in m for m in msgs), msgs


@pytest.mark.parametrize(
    "body, warns",
    [
        ("n = n + 2'd1;", False),
        ("begin q = a; n = 2'd0; end", False),
        ("begin n = 2'd1; if (n[0]) q = a; end", True),
        ("begin n = 2'd1; case (n) 2'd1: q = a; default: q = 1'b0; endcase end", True),
        ("begin n = 2'd1; q <= n[1]; end", True),
        ("for (int i = 0; i < 2; i++) begin q = n[i]; n[i] = a; end", True),
        ("begin n = 2'd1; $display(n); end", True),
    ],
    ids=["read_before_write", "unrelated", "cond", "case", "rhs", "loop", "call"],
)
def test_blocking_temporary_warning_is_order_aware(
    tmp_path: Path, caplog, body: str, warns: bool
) -> None:
    src = f"""module m (input clk, input a, output reg q);
    reg [1:0] n;
    always @(posedge clk) {body}
endmodule
"""
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        _slang(tmp_path, src)
    msgs = [
        r.getMessage() for r in caplog.records if "blocking-assigned" in r.getMessage()
    ]
    assert bool(msgs) is warns, msgs


def test_cdc023_v2001_fires_on_both_frontends(tmp_path: Path) -> None:
    """End to end: the fuzz ``and_s1_v2001`` shape fires CDC-023 through
    ``lint --frontend slang`` (it came back clean before #345)."""
    sv = _write(
        tmp_path,
        """module m (input clk_a, input clk_b, input d_in, output q_out);
    wire gclk;
    assign gclk = clk_a & clk_b;
    reg q;
    always @(posedge gclk) q <= d_in;
    assign q_out = q;
endmodule
""",
    )
    sdc = tmp_path / "m.sdc"
    sdc.write_text(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 7 [get_ports clk_b]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}\n"
        "set_input_delay -clock clk_a 1 [get_ports d_in]\n"
    )
    frontends = ["slang"] + (["yosys"] if _HAVE_YOSYS else [])
    fired = {}
    for fe in frontends:
        out = tmp_path / f"{fe}.json"
        result = CliRunner().invoke(
            app,
            [
                "lint",
                "--top",
                "m",
                "--sdc",
                str(sdc),
                "--frontend",
                fe,
                "--format",
                "json",
                "--output",
                str(out),
                str(sv),
            ],  # fmt: skip
        )
        assert result.exit_code == 1, result.output
        fired[fe] = sorted(
            v["rule_id"] for v in json.loads(out.read_text())["violations"]
        )
    assert "CDC-023" in fired["slang"]
    if _HAVE_YOSYS:
        assert fired["slang"] == fired["yosys"]


# --- blackbox instances -------------------------------------------------

_XPM = """(* blackbox *)
module xpm_cdc_single #(
    parameter integer DEST_SYNC_FF = 4,
    parameter integer SRC_INPUT_REG = 1
) (
    input  wire src_clk,
    input  wire src_in,
    input  wire dest_clk,
    output wire dest_out
);
endmodule

module m (input clk_a, input clk_b, input d_in, output reg q_out);
    reg s;
    always @(posedge clk_a) s <= d_in;
    wire d;
    xpm_cdc_single #(.DEST_SYNC_FF(2), .SRC_INPUT_REG(0)) u_sync (
        .src_clk(clk_a), .src_in(s), .dest_clk(clk_b), .dest_out(d));
    always @(posedge clk_b) q_out <= d;
endmodule
"""


def test_blackbox_instance_is_a_boundary_cell(tmp_path: Path) -> None:
    module, siblings = _slang(tmp_path, _XPM)
    cell = module.cells["u_sync"]
    assert cell.type == "xpm_cdc_single"
    # Only the overridden parameters, 32-bit binary, as Yosys writes them.
    assert cell.parameters == {
        "DEST_SYNC_FF": format(2, "032b"),
        "SRC_INPUT_REG": "0" * 32,
    }
    assert cell.connections["src_clk"] == module.ports["clk_a"].bits
    assert cell.connections["dest_clk"] == module.ports["clk_b"].bits
    assert cell.attributes["src"].endswith(tuple("0123456789"))
    s_flop, q_flop = sorted(
        _flops(module), key=lambda c: c.connections["Q"] != module.netnames["s"].bits
    )
    assert cell.connections["src_in"] == s_flop.connections["Q"]
    assert cell.connections["dest_out"] == q_flop.connections["D"]
    sub = siblings["xpm_cdc_single"]
    assert sub.is_blackbox and not sub.cells
    assert {p.name: p.direction for p in sub.ports.values()} == {
        "src_clk": "input",
        "src_in": "input",
        "dest_clk": "input",
        "dest_out": "output",
    }
    if _HAVE_YOSYS:
        y_mod, y_sib = _yosys(tmp_path, _XPM)
        y_cell = y_mod.cells["u_sync"]
        assert y_cell.type == cell.type
        assert y_cell.parameters == cell.parameters
        assert {k: len(v) for k, v in y_cell.connections.items()} == {
            k: len(v) for k, v in cell.connections.items()
        }
        assert {
            p.name: p.direction for p in y_sib["xpm_cdc_single"].ports.values()
        } == {p.name: p.direction for p in sub.ports.values()}


@pytest.mark.parametrize("required, fires", [(2, False), (3, True)])
def test_cdc022_fires_through_lint_frontend_slang(
    tmp_path: Path, required: int, fires: bool
) -> None:
    sv = _write(tmp_path, _XPM)
    sdc = tmp_path / "m.sdc"
    sdc.write_text(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 7 [get_ports clk_b]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}\n"
        "set_input_delay -clock clk_a 1 [get_ports d_in]\n"
    )
    out = tmp_path / "o.json"
    result = CliRunner().invoke(
        app,
        [
            "lint",
            "--top",
            "m",
            "--sdc",
            str(sdc),
            "--frontend",
            "slang",
            "--sync-depth",
            str(required),
            "--format",
            "json",
            "--output",
            str(out),
            str(sv),
        ],  # fmt: skip
    )
    rules = [v["rule_id"] for v in json.loads(out.read_text())["violations"]]
    assert rules == (["CDC-022"] if fires else []), result.output


_PARAMS = """(* blackbox *)
module bb #(
    parameter integer N = 4,
    parameter W = 2,
    parameter real R = 1.0,
    parameter [3:0] X = 4'b0000,
    parameter [7:0] K = 8'h01
) (input wire clk, input wire [W-1:0] d, output wire [W-1:0] q, inout wire io);
endmodule

module child (input clk, input [3:0] d, output [3:0] q);
    bb #(.N(-2), .W(4), .R(2.5), .X(4'b1x0z)) u_bb (.clk(clk), .d(d + 4'd1), .q(q), .io());
endmodule

module m (input clk, input [3:0] d, output [3:0] q0, output [3:0] q1);
    child u_a (.clk(clk), .d(d), .q(q0));
    child u_b (.clk(clk), .d(d), .q(q1));
endmodule
"""


def test_blackbox_parameters_nesting_and_sibling_widths(tmp_path: Path) -> None:
    module, siblings = _slang(tmp_path, _PARAMS)
    # Nested instances are named by their flattened path, as Yosys does.
    assert {n for n, c in module.cells.items() if c.type == "bb"} == {
        "u_a.u_bb",
        "u_b.u_bb",
    }
    cell = module.cells["u_a.u_bb"]
    assert cell.parameters == {
        "N": format(-2 & 0xFFFFFFFF, "032b"),
        "W": format(4, "032b"),
        "R": "2.5",
        "X": "1x0z",
    }
    # ``.io()`` is a zero-width connection; ``d + 1`` lowers to a cell.
    assert cell.connections["io"] == ()
    assert "$add" in _driver_types(module, cell.connections["d"])
    assert cell.connections["q"] == module.ports["q0"].bits
    # One sibling per module, its ports sized by the instance.
    assert list(siblings) == ["bb"]
    assert {p.name: len(p.bits) for p in siblings["bb"].ports.values()} == {
        "clk": 1,
        "d": 4,
        "q": 4,
        "io": 1,
    }
    assert siblings["bb"].ports["io"].direction == "inout"


def test_blackbox_attribute_false_is_inlined(tmp_path: Path) -> None:
    src = """(* blackbox = 0 *)
module sub (input clk, input d, output reg q);
    always @(posedge clk) q <= d;
endmodule
module m (input clk, input d, output q);
    sub u_sub (.clk(clk), .d(d), .q(q));
endmodule
"""
    module, siblings = _slang(tmp_path, src)
    assert siblings == {}
    assert [c.type for c in _flops(module)] == ["$dff"]


_BODY = """module sub (input clk, input d, output reg q);
    reg s;
    always @(posedge clk) begin s <= d; q <= s; end
endmodule
module m (input clk_a, input clk_b, input d, output reg q);
    wire o;
    sub u_sub (.clk(clk_b), .d(d), .q(o));
    always @(posedge clk_b) q <= o;
endmodule
"""


def test_cli_blackbox_option_keeps_module_as_boundary(tmp_path: Path) -> None:
    module, siblings = _slang(tmp_path, _BODY, blackbox=["sub"])
    assert module.cells["u_sub"].type == "sub"
    assert len(_flops(module)) == 1  # sub's two flops are not inlined
    assert siblings["sub"].is_blackbox and not siblings["sub"].cells


def test_cli_greybox_option_keeps_the_body_in_the_sibling(tmp_path: Path) -> None:
    module, siblings = _slang(tmp_path, _BODY, greybox=["sub"])
    assert module.cells["u_sub"].type == "sub"
    assert len(_flops(module)) == 1
    sub = siblings["sub"]
    assert sub.is_blackbox and sub.name == "sub"
    assert len(_flops(sub)) == 2
    assert {p.name: p.direction for p in sub.ports.values()} == {
        "clk": "input",
        "d": "input",
        "q": "output",
    }


def test_cli_blackbox_and_greybox_overlap_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SlangElaborationError, match="same module"):
        _slang(tmp_path, _BODY, blackbox=["sub"], greybox=["sub"])


def test_cli_blackbox_unknown_module_warns(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=_SLANG_LOG):
        module, siblings = _slang(tmp_path, _BODY, blackbox=["nope"])
    assert siblings == {}
    assert any("'nope'" in r.getMessage() for r in caplog.records)


def test_undefined_module_fails_elaboration(tmp_path: Path) -> None:
    """Not a blackbox on either path: the Yosys ``hierarchy`` pass rejects
    an undefined module, and so does slang."""
    src = """module m (input a, output b);
    missing u_x (.a(a), .b(b));
endmodule
"""
    with pytest.raises(SlangElaborationError):
        _slang(tmp_path, src)


def test_single_return_elaborate_drops_siblings(tmp_path: Path) -> None:
    module = slang_fe.elaborate([_write(tmp_path, _XPM)], "m")
    assert module.cells["u_sync"].type == "xpm_cdc_single"


def test_encode_param_edge_cases() -> None:
    assert slang_fe._encode_param(None) is None
    assert slang_fe._encode_param(object()) is None
    assert slang_fe._encode_param(1.5) == "1.5"


# --- parity with the committed Yosys netlists of the boundary fixtures ---

_FIX = Path(__file__).parent / "fixtures"
_BOUNDARY_FIXTURES = [
    ("bbx_input_sync", ("json", "grey.json", "flat.json")),
    ("bbx_input_sync_broken", ("json", "grey.json", "flat.json")),
    ("bbx_residual_declines", ("json", "grey.json", "flat.json")),
    ("bbx_shared_internal_violation", ("json", "grey.json", "flat.json")),
    ("blackbox_leaf_crossing", ("json",)),
    ("clock_output_blackbox", ("json", "grey.json", "flat.json")),
    ("multi_clock_blackbox", ("json", "grey.json", "flat.json")),
    ("xpm_cdc_blackbox", ("json",)),
]
_OPTION = {"json": "--blackbox", "grey.json": "--greybox", "flat.json": None}


def _fired(args: list[str]) -> dict[str, int]:
    result = CliRunner().invoke(app, args)
    assert result.exit_code in (0, 1), result.output
    counts: dict[str, int] = {}
    for v in json.loads(result.output)["violations"]:
        counts[v["rule_id"]] = counts.get(v["rule_id"], 0) + 1
    return counts


@pytest.mark.parametrize(
    "fixture, variant, sync_depth",
    [
        (name, variant, depth)
        for name, variants in _BOUNDARY_FIXTURES
        for variant in variants
        for depth in ("2", "5")
    ],
)
def test_boundary_fixture_matches_committed_yosys_netlist(
    fixture: str, variant: str, sync_depth: str
) -> None:
    """``lint --frontend slang`` with the matching ``--blackbox`` /
    ``--greybox`` modules reports exactly what ``analyze`` reports on the
    fixture's committed Yosys netlist (``.json`` blackboxed, ``.grey.json``
    greyboxed, ``.flat.json`` flattened). ``--sync-depth 5`` makes CDC-022
    and the boundary depth checks bite."""
    d = _FIX / fixture
    netlist_path = d / f"{fixture}.{variant}"
    sdc = d / f"{fixture}.sdc"
    top, blackboxes = netlist.load_with_blackboxes(netlist_path)
    option = _OPTION[variant]
    extra = [a for name in sorted(blackboxes) for a in (option, name)] if option else []
    common = ["-s", str(sdc), "-f", "json", "--sync-depth", sync_depth]
    expected = _fired(["analyze", "-n", str(netlist_path), *common])
    got = _fired(
        [
            "lint",
            "--top",
            top.name,
            "--frontend",
            "slang",
            *common,
            *extra,
            str(d / f"{fixture}.sv"),
        ]  # fmt: skip
    )
    assert got == expected
