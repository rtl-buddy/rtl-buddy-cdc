"""A register array that is a memory in all but coding style is judged as
one (rtl-buddy-cdc#414).

An async FIFO whose storage write shares the pointer's asynchronous-reset
block is ``DEPTH`` registers on both frontends (Yosys ``mem2reg``; the
slang frontend since rtl-buddy-cdc#401); a read side capturing the word
in several registers fired CDC-020 once per word. :mod:`rtl_buddy_cdc.regfile`
turns exactly one-write-decode / one-read-mux arrays back into a memory
before the analysis, so CDC-026 judges the storage path — and only those:
the negative cases below stay registers.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, regfile
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.liveness import prune_dead_cells

FIX = Path(__file__).parent / "fixtures" / "good_async_fifo_reset_block"
TOP = "good_async_fifo_reset_block"
NET = FIX / f"{TOP}.json"
SDC = FIX / f"{TOP}.sdc"
SV = FIX / f"{TOP}.sv"

PYSLANG = importlib.util.find_spec("pyslang") is not None
YOSYS = shutil.which("yosys") is not None


@pytest.fixture(autouse=True)
def _no_ambient_yosys_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--frontend yosys`` here means ``read_verilog``: an ambient
    ``RTL_BUDDY_SLANG_PLUGIN`` would switch the flow to ``read_slang``."""
    monkeypatch.delenv("RTL_BUDDY_SLANG_PLUGIN", raising=False)


def _run(tmp_path: Path, args: list[str]) -> tuple[dict, str]:
    out = tmp_path / "r.json"
    result = CliRunner().invoke(app, [*args, "--format", "json", "--output", str(out)])
    assert result.exit_code in (0, 1), result.output
    return json.loads(out.read_text()), result.output


def _rules(report: dict) -> list[str]:
    return sorted(v["rule_id"] for v in report["violations"])


def test_committed_netlist_is_recognised() -> None:
    mems = regfile.recognise(prune_dead_cells(netlist.load(NET)))
    assert len(mems) == 1
    (mem,) = mems
    assert mem.memid == "mem"
    assert len(mem.words) == 8
    assert len(mem.wdata) == 12
    assert [len(ctl) for _data, ctl in mem.reads] == [3]  # rbin[2:0]
    assert len(mem.reads[0][0]) == 12


def test_fifo_is_clean_and_reported_as_a_memory(tmp_path: Path) -> None:
    report, output = _run(tmp_path, ["analyze", "-n", str(NET), "-s", str(SDC)])
    assert _rules(report) == []
    # The two pointer synchronisers; the storage path is CDC-026's.
    assert report["summary"]["crossings"] == 2
    assert "register array mem (8 x 12 bits" in output


def test_without_the_pass_the_words_fire_cdc_020_with_the_array_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(regfile, "recognise", lambda module, notes=None: [])
    report, _ = _run(tmp_path, ["analyze", "-n", str(NET), "-s", str(SDC)])
    assert _rules(report) == ["CDC-020"] * 8
    assert all(
        "element of the register array `mem`" in v["message"]
        for v in report["violations"]
    )


@pytest.mark.skipif(not PYSLANG, reason="pyslang not installed")
def test_slang_frontend_reaches_the_same_verdict(tmp_path: Path) -> None:
    report, output = _run(
        tmp_path,
        ["lint", "--frontend", "slang", "--top", TOP, "-s", str(SDC), str(SV)],
    )
    assert _rules(report) == []
    assert report["summary"]["crossings"] == 2
    assert "register array mem (8 x 12 bits" in output


# --- soundness: an unsafe array is a memory CDC-026 rejects ------------------

_UNSAFE = """
module m (input logic wclk, wrst_n, we, input logic [7:0] wd, input logic [1:0] wa,
          input logic rclk, rrst_n, output logic [3:0] hi, lo);
  logic [7:0] mem [4];
  logic [1:0] ra;
  always_ff @(posedge wclk or negedge wrst_n)
    if (!wrst_n) ; else if (we) mem[wa] <= wd;
  // The read address is a free-running rclk counter: nothing tells the
  // reader which words are stable.
  always_ff @(posedge rclk or negedge rrst_n)
    if (!rrst_n) begin ra <= '0; hi <= '0; lo <= '0; end
    else begin ra <= ra + 2'd1; hi <= mem[ra][7:4]; lo <= mem[ra][3:0]; end
endmodule
"""
_UNSAFE_SDC = """
create_clock -name wclk -period 10 [get_ports wclk]
create_clock -name rclk -period 7 [get_ports rclk]
set_clock_groups -asynchronous -group wclk -group rclk
set_input_delay -clock wclk 1 [get_ports {we wd wa wrst_n}]
set_output_delay -clock rclk 1 [get_ports {hi lo}]
"""


def _frontends() -> list[str]:
    return [f for f, ok in (("slang", PYSLANG), ("yosys", YOSYS)) if ok]


@pytest.mark.parametrize("frontend", _frontends())
def test_an_unsynchronised_read_of_a_register_array_is_cdc_026(
    tmp_path: Path, frontend: str
) -> None:
    sv = tmp_path / "m.sv"
    sv.write_text(_UNSAFE)
    sdc = tmp_path / "m.sdc"
    sdc.write_text(_UNSAFE_SDC)
    report, output = _run(
        tmp_path,
        ["lint", "--frontend", frontend, "--top", "m", "-s", str(sdc), str(sv)],
    )
    assert "register array mem (4 x 8 bits" in output
    assert _rules(report) == ["CDC-026"]


# --- what stays registers ----------------------------------------------------

_SHAPES = {
    # A word also read outside the read mux.
    "extra_reader": """
module m (input logic clk, rst_n, we, input logic [7:0] wd, input logic [1:0] wa, ra,
          output logic [7:0] rd, output logic peek);
  logic [7:0] mem [4];
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) ; else if (we) mem[wa] <= wd;
  assign rd = mem[ra];
  assign peek = mem[2][0];
endmodule
""",
    # The array is reset: registers with a reset pin, not a memory.
    "array_reset": """
module m (input logic clk, rst_n, we, input logic [7:0] wd, input logic [1:0] wa, ra,
          output logic [7:0] rd);
  logic [7:0] mem [4];
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) for (int i = 0; i < 4; i++) mem[i] <= '0;
    else if (we) mem[wa] <= wd;
  assign rd = mem[ra];
endmodule
""",
    # Two words written in one cycle.
    "two_writes": """
module m (input logic clk, rst_n, we, input logic [7:0] wd, input logic [1:0] wa, ra,
          output logic [7:0] rd);
  logic [7:0] mem [4];
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) ; else if (we) begin mem[wa] <= wd; mem[~wa] <= wd; end
  assign rd = mem[ra];
endmodule
""",
    # One word loads something other than the shared write data.
    "other_data": """
module m (input logic clk, rst_n, we, clr, input logic [7:0] wd, input logic [1:0] wa, ra,
          output logic [7:0] rd);
  logic [7:0] mem [4];
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) ; else if (clr) mem[0] <= '0; else if (we) mem[wa] <= wd;
  assign rd = mem[ra];
endmodule
""",
    # The read mixes another value into the words' mux.
    "read_bypass": """
module m (input logic clk, rst_n, we, byp, input logic [7:0] wd, input logic [1:0] wa, ra,
          output logic [7:0] rd);
  logic [7:0] mem [4];
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) ; else if (we) mem[wa] <= wd;
  assign rd = byp ? wd : mem[ra];
endmodule
""",
}


@pytest.mark.parametrize("frontend", _frontends())
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_shapes_that_are_not_one_memory_stay_registers(
    tmp_path: Path, frontend: str, shape: str
) -> None:
    from rtl_buddy_cdc.frontend import Frontend, elaborate

    sv = tmp_path / "m.sv"
    sv.write_text(_SHAPES[shape])
    module = prune_dead_cells(elaborate([sv], "m", frontend=Frontend(frontend)))
    mems = regfile.recognise(module)
    if shape == "read_bypass":
        # The bypass mux reads the tree's output; the tree itself is one
        # memory read.
        assert [len(m.words) for m in mems] == [4]
    else:
        assert mems == [], shape


def test_enumeration_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the evaluation budget the array stays registers rather than
    being guessed at."""
    module = prune_dead_cells(netlist.load(NET))
    monkeypatch.setattr(regfile, "MAX_EVALUATIONS", 4)
    assert regfile.recognise(module) == []


# --- the evaluator ------------------------------------------------------------


def _eval(cell_type: str, conns: dict, params: dict | None = None, **env):
    """Evaluate ``Y`` of one cell whose input bits are named ints."""
    cell = netlist.Cell("c", cell_type, conns, params or {})
    y = conns["Y"]
    module = netlist.Module("m", {}, {"c": cell}, {})
    drivers = {b: ("c", "Y", i) for i, b in enumerate(y)}
    ev = regfile._Evaluator(module, drivers, {})
    ev.reset({int(k[1:]): v for k, v in env.items()})
    return ev.bits(tuple(y))


@pytest.mark.parametrize(
    ("cell_type", "conns", "params", "env", "want"),
    [
        ("$mux", {"A": (1,), "B": (2,), "S": (3,), "Y": (9,)}, {}, {"b1": 0, "b2": 1, "b3": 1}, [1]),
        ("$mux", {"A": (1,), "B": (2,), "S": (3,), "Y": (9,)}, {}, {"b1": 1, "b2": 1, "b3": "x"}, [1]),
        ("$mux", {"A": (1,), "B": (2,), "S": (3,), "Y": (9,)}, {}, {"b1": 0, "b2": 1, "b3": "x"}, ["x"]),
        ("$pmux", {"A": (1,), "B": (2, 3), "S": (4, 5), "Y": (9,)}, {}, {"b1": 0, "b2": 1, "b3": 0, "b4": 0, "b5": 1}, [0]),
        ("$pmux", {"A": (1,), "B": (2, 3), "S": (4, 5), "Y": (9,)}, {}, {"b1": 0, "b2": 1, "b3": 0, "b4": 0, "b5": 0}, [0]),
        ("$pmux", {"A": (1,), "B": (2, 3), "S": (4, 5), "Y": (9,)}, {}, {"b1": 0, "b2": 1, "b3": 0, "b4": 1, "b5": 1}, ["x"]),
        ("$pmux", {"A": (1,), "B": (2, 3), "S": (4, 5), "Y": (9,)}, {}, {"b1": 0, "b2": 1, "b3": 0, "b4": "x", "b5": 0}, ["x"]),
        ("$bmux", {"A": (1, 2), "S": (3,), "Y": (9,)}, {}, {"b1": 0, "b2": 1, "b3": 1}, [1]),
        ("$bmux", {"A": (1, 2), "S": (3,), "Y": (9,)}, {}, {"b1": 0, "b2": 1, "b3": "x"}, ["x"]),
        ("$shiftx", {"A": (1, 2), "B": (3,), "Y": (9,)}, {}, {"b1": 0, "b2": 1, "b3": 1}, [1]),
        ("$shiftx", {"A": (1, 2), "B": (3, 4), "Y": (9,)}, {"B_SIGNED": "1"}, {"b1": 0, "b2": 1, "b3": 1, "b4": 1}, ["x"]),
        ("$shiftx", {"A": (1, 2), "B": (3,), "Y": (9,)}, {}, {"b1": 0, "b2": 1, "b3": "x"}, ["x"]),
        ("$eq", {"A": (1, 2), "B": ("1", "0"), "Y": (9,)}, {}, {"b1": 1, "b2": 0}, [1]),
        ("$ne", {"A": (1, 2), "B": ("1", "0"), "Y": (9,)}, {}, {"b1": 1, "b2": 0}, [0]),
        ("$eq", {"A": (1,), "B": ("1",), "Y": (9,)}, {}, {"b1": "x"}, ["x"]),
        ("$and", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 1, "b2": 1}, [1]),
        ("$or", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 0, "b2": 0}, [0]),
        ("$xor", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 1, "b2": 1}, [0]),
        ("$xnor", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 1, "b2": "x"}, ["x"]),
        ("$_AND_", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 0, "b2": "x"}, [0]),
        ("$_OR_", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 1, "b2": "x"}, [1]),
        ("$_ANDNOT_", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 1, "b2": 0}, [1]),
        ("$_ORNOT_", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 0, "b2": 1}, [0]),
        ("$_NAND_", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 1, "b2": 1}, [0]),
        ("$_NOR_", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 0, "b2": 0}, [1]),
        ("$not", {"A": (1,), "Y": (9,)}, {}, {"b1": 0}, [1]),
        ("$_NOT_", {"A": (1,), "Y": (9,)}, {}, {"b1": "x"}, ["x"]),
        ("$pos", {"A": (1,), "Y": (9, 10)}, {"A_SIGNED": "1"}, {"b1": 1}, [1, 1]),
        ("$logic_not", {"A": (1, 2), "Y": (9,)}, {}, {"b1": 0, "b2": 0}, [1]),
        ("$reduce_and", {"A": (1, 2), "Y": (9,)}, {}, {"b1": 1, "b2": 0}, [0]),
        ("$reduce_or", {"A": (1, 2), "Y": (9,)}, {}, {"b1": 1, "b2": 0}, [1]),
        ("$reduce_bool", {"A": (1,), "Y": (9,)}, {}, {"b1": "x"}, ["x"]),
        ("$logic_and", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 1, "b2": 1}, [1]),
        ("$logic_or", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": 0, "b2": 0}, [0]),
        ("$logic_or", {"A": (1,), "B": (2,), "Y": (9,)}, {}, {"b1": "x", "b2": 0}, ["x"]),
    ],
)  # fmt: skip
def test_evaluator_cells(cell_type, conns, params, env, want) -> None:
    assert _eval(cell_type, conns, params, **env) == want


def test_evaluator_symbols() -> None:
    """Data passes through a mux leg; logic on data, data on a select and
    unknown cell types are not evaluated (the structure is rejected)."""
    sym = ("q", 0, 0)
    assert _eval(
        "$mux", {"A": (1,), "B": (2,), "S": (3,), "Y": (9,)}, b1=sym, b2=0, b3=0
    ) == [sym]
    assert _eval("$and", {"A": (1,), "B": (2,), "Y": (9,)}, b1=sym, b2=1) == [sym]
    assert _eval("$or", {"A": (1,), "B": (2,), "Y": (9,)}, b1=sym, b2=0) == [sym]
    for bad in (
        lambda: _eval("$xor", {"A": (1,), "B": (2,), "Y": (9,)}, b1=sym, b2=1),
        lambda: _eval("$not", {"A": (1,), "Y": (9,)}, b1=sym),
        lambda: _eval(
            "$mux", {"A": (1,), "B": (2,), "S": (3,), "Y": (9,)}, b1=0, b2=1, b3=sym
        ),
        lambda: _eval(
            "$pmux", {"A": (1,), "B": (2,), "S": (3,), "Y": (9,)}, b1=0, b2=1, b3=sym
        ),
        lambda: _eval("$eq", {"A": (1,), "B": (2,), "Y": (9,)}, b1=sym, b2=1),
        lambda: _eval("$add", {"A": (1,), "B": (2,), "Y": (9,)}, b1=0, b2=1),
        lambda: _eval("$and", {"A": (1,), "B": (5,), "Y": (9,)}, b1=1),
    ):
        with pytest.raises(regfile._Unsupported):
            bad()


# --- review regressions (rtl-buddy-cdc#414) ---------------------------------

_SHARED_ADDR = """
module m (input logic clk_a, clk_b, rst_n, we, input logic [7:0] wd,
          input logic [1:0] pin, q, output logic [7:0] rq);
  logic [1:0] p;
  always_ff @(posedge clk_b) p <= pin;
  wire [1:0] a = p ^ q;
  logic [7:0] mem [0:3];
  logic [7:0] cnt;
  always_ff @(posedge clk_a or negedge rst_n)
    if (!rst_n) cnt <= '0;
    else begin cnt <= cnt + 1'b1; if (we) mem[a] <= wd; end
  always_ff @(posedge clk_a) rq <= mem[a];
endmodule
"""
_CLK_SDC = """
create_clock -name clk_a -period 10 [get_ports clk_a]
create_clock -name clk_b -period 7 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 0 [get_ports {we wd q rst_n}]
set_input_delay -clock clk_b 0 [get_ports pin]
"""


def _lint(tmp_path: Path, frontend: str, src: str, sdc: str) -> tuple[dict, str]:
    sv = tmp_path / "m.sv"
    sv.write_text(src)
    sdc_path = tmp_path / "m.sdc"
    sdc_path.write_text(sdc)
    return _run(
        tmp_path,
        ["lint", "--frontend", frontend, "--top", "m", "-s", str(sdc_path), str(sv)],
    )


@pytest.mark.parametrize("frontend", _frontends())
def test_an_address_cell_both_sides_use_is_kept(tmp_path: Path, frontend: str) -> None:
    """The read decode's address cell also feeds the write decode: the
    rewrite keeps it, so the clk_b -> clk_a crossing into the write address
    is still reported (on the write port), and the read capture's too."""
    report, output = _lint(tmp_path, frontend, _SHARED_ADDR, _CLK_SDC)
    assert "register array mem (4 x 8 bits" in output
    dsts = sorted(v["cell_name"] for v in report["violations"])
    assert [v["rule_id"] for v in report["violations"]] == ["CDC-004", "CDC-004"]
    assert any("regfile$wr" in d for d in dsts), dsts


def _elab(tmp_path: Path, src: str, frontend: str = "slang") -> netlist.Module:
    from rtl_buddy_cdc.frontend import Frontend, elaborate

    sv = tmp_path / "m.sv"
    sv.write_text(src)
    return prune_dead_cells(elaborate([sv], "m", frontend=Frontend(frontend)))


_PLAIN = """
module m (input logic clk, rst_n, we, input logic [1:0] wa, ra,
          input logic [7:0] wd, output logic [7:0] rq);
  logic [7:0] mem [0:3];
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) ; else if (we) mem[wa] <= wd;
  assign rq = mem[ra];
endmodule
"""


def _with_peek(module: netlist.Module) -> netlist.Module:
    """``module`` plus a flop reading word 0's next-state net."""
    from dataclasses import replace

    (mem,) = regfile.recognise(module)
    word0 = module.cells[mem.words[0]]
    d = word0.connections["D"]
    peek = netlist.Cell(
        "peek_ff",
        "$dff",
        {
            "CLK": word0.connections["CLK"],
            "D": d,
            "Q": tuple(range(9000, 9000 + len(d))),
        },
        {"CLK_POLARITY": "1", "WIDTH": format(len(d), "b")},
    )
    out = netlist.Port("peek", "output", peek.connections["Q"])
    return replace(
        module,
        cells={**module.cells, "peek_ff": peek},
        ports={**module.ports, "peek": out},
    )


@pytest.mark.skipif(not PYSLANG, reason="pyslang not installed")
def test_a_reader_of_a_word_next_state_refuses_recognition(tmp_path: Path) -> None:
    base = _elab(tmp_path, _PLAIN)
    (mem,) = regfile.recognise(base)
    peeked = _with_peek(base)
    assert regfile.recognise(peeked) == []
    # And the rewrite's own invariant refuses it even when handed the
    # memory recognised without the reader: nothing read may lose its
    # driver.
    assert regfile._rewrite_one(peeked, mem) is None
    notes: list[str] = []
    out, done = regfile.rewrite(peeked, [mem], notes)
    assert out is peeked and done == []
    assert notes and "left as registers" in notes[0]


@pytest.mark.skipif(not PYSLANG, reason="pyslang not installed")
def test_a_deep_read_chain_is_evaluated_without_recursion(tmp_path: Path) -> None:
    """A 256-word array read through a 256-deep if-chain (a mux chain):
    the evaluator is iterative, so no RecursionError escapes."""
    module = _elab(
        tmp_path,
        """
module m (input logic clk, rst_n, we, input logic [7:0] wa, ra, wd,
          output logic [7:0] rq);
  logic [7:0] mem [0:255];
  logic [7:0] rd;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) ; else if (we) mem[wa] <= wd;
  always_comb begin
    rd = '0;
    for (int i = 0; i < 256; i++) if (ra == i) rd = mem[i];
  end
  assign rq = rd;
endmodule
""",
    )
    (mem,) = regfile.recognise(module)
    assert len(mem.words) == 256


def test_a_failing_check_leaves_registers_with_a_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object) -> None:
        raise RecursionError("too deep")

    monkeypatch.setattr(regfile, "_write_side", boom)
    notes: list[str] = []
    assert regfile.recognise(prune_dead_cells(netlist.load(NET)), notes) == []
    assert notes == [
        "register array mem left as registers: its memory check failed "
        "(RecursionError: too deep)"
    ]


@pytest.mark.skipif(not PYSLANG, reason="pyslang not installed")
@pytest.mark.parametrize(
    "decl",
    [
        "always_ff @(negedge clk or negedge rst_n)",  # falling edge
    ],
)
def test_a_falling_edge_array_stays_registers(tmp_path: Path, decl: str) -> None:
    src = _PLAIN.replace("always_ff @(posedge clk or negedge rst_n)", decl)
    assert regfile.recognise(_elab(tmp_path, src)) == []


@pytest.mark.skipif(not PYSLANG, reason="pyslang not installed")
def test_a_kept_array_stays_registers(tmp_path: Path) -> None:
    src = _PLAIN.replace("logic [7:0] mem [0:3];", "(* keep *) logic [7:0] mem [0:3];")
    assert regfile.recognise(_elab(tmp_path, src)) == []


def test_the_write_port_is_rising_edge() -> None:
    module = prune_dead_cells(netlist.load(NET))
    out, done = regfile.rewrite(module, regfile.recognise(module))
    assert len(done) == 1
    (wr,) = [c for c in out.cells.values() if c.type == "$memwr_v2"]
    assert wr.parameters["CLK_POLARITY"] == "1"


# --- what a recognised array is judged by ------------------------------------

_GRAY_READ = """
module m (input logic wclk, wrst_n, we, input logic [7:0] wd,
          input logic rclk, rrst_n, output logic [3:0] hi, lo);
  logic [7:0] mem [4];
  logic [2:0] wbin, wgray, wg1, wg2, wnext;
  assign wnext = wbin + 3'd1;
  always_ff @(posedge wclk or negedge wrst_n)
    if (!wrst_n) begin wbin <= '0; wgray <= '0; end
    else if (we) begin WRPORT wbin <= wnext; wgray <= wnext ^ (wnext >> 1); end
  STOREPORT
  always_ff @(posedge rclk or negedge rrst_n)
    if (!rrst_n) begin wg1 <= '0; wg2 <= '0; end
    else begin wg1 <= wgray; wg2 <= wg1; end
  logic [1:0] ra;
  assign ra = wg2[1:0];
  always_ff @(posedge rclk or negedge rrst_n)
    if (!rrst_n) begin hi <= '0; lo <= '0; end
    else begin hi <= mem[ra][7:4]; lo <= mem[ra][3:0]; end
endmodule
"""


@pytest.mark.parametrize("frontend", _frontends())
def test_a_recognised_array_is_judged_like_the_same_memory(
    tmp_path: Path, frontend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read address is the write pointer, gray-coded and synchronised
    into the read clock, with no empty check: the reader can sample the
    word being written. CDC-026 proves only that write-domain information
    reaches the read address through a synchroniser, so it is silent —
    for the register array (once recognised) exactly as for the same RTL
    with the write in a block of its own (a memory to both frontends).
    The register form reports the word crossings (CDC-004 / CDC-020):
    recognition trades that for the memory judgement."""
    as_regs = _GRAY_READ.replace("WRPORT", "mem[wbin[1:0]] <= wd;").replace(
        "STOREPORT", ""
    )
    as_mem = _GRAY_READ.replace("WRPORT", "").replace(
        "STOREPORT", "always_ff @(posedge wclk) if (we && wrst_n) mem[wbin[1:0]] <= wd;"
    )
    sdc = """
create_clock -name wclk -period 10 [get_ports wclk]
create_clock -name rclk -period 7 [get_ports rclk]
set_clock_groups -asynchronous -group wclk -group rclk
set_input_delay -clock wclk 1 [get_ports {we wd wrst_n}]
set_output_delay -clock rclk 1 [get_ports {hi lo}]
"""
    regs, output = _lint(tmp_path, frontend, as_regs, sdc)
    assert "register array mem" in output
    mem, _ = _lint(tmp_path, frontend, as_mem, sdc)
    assert _rules(regs) == _rules(mem) == []
    monkeypatch.setattr(regfile, "recognise", lambda module, notes=None: [])
    unrecognised, _ = _lint(tmp_path, frontend, as_regs, sdc)
    assert set(_rules(unrecognised)) == {"CDC-004", "CDC-020"}
