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
    monkeypatch.setattr(regfile, "recognise", lambda module: [])
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


def test_too_many_control_bits_is_not_enumerated() -> None:
    """The enumeration is bounded: a cone past MAX_CONTROL_BITS leaves the
    array as registers rather than guessing."""
    assert regfile.MAX_CONTROL_BITS <= 16
