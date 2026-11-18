"""Flat and abstracted runs agree on the data-path rules (rtl-buddy-cdc#405).

Each design puts the structure a rule reads inside a module that the
abstracted views keep as a boundary cell, then compares the flat run with
``--greybox`` (body kept, analysed once on its own) and ``--blackbox``
(bodyless stub). A greybox has the body in hand, so it must report what
the flat run reports; a stub legitimately knows less, and each test says
what it is expected to report.

The views are elaborated by the slang frontend from one source, so the
module skips without pyslang.
"""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app

needs_slang = pytest.mark.skipif(
    importlib.util.find_spec("pyslang") is None, reason="pyslang not installed"
)

_SDC = """\
create_clock -name fast -period 4 [get_ports fast]
create_clock -name slow -period 9 [get_ports slow]
set_clock_groups -asynchronous -group {fast} -group {slow}
"""


def _typed(clock: str, *ports: str) -> str:
    return "".join(f"set_input_delay -clock {clock} 1 [get_ports {p}]\n" for p in ports)


def _views(
    tmp_path: Path, sv: str, sdc: str, *extra: str, module: str = "blk"
) -> dict[str, dict]:
    src = tmp_path / "t.sv"
    src.write_text(sv)
    sdc_path = tmp_path / "t.sdc"
    sdc_path.write_text(sdc)
    reports = {}
    for view in ("flat", "greybox", "blackbox"):
        out = tmp_path / f"{view}.json"
        args = ["lint", "--top", "top", "--sdc", str(sdc_path), "--frontend", "slang"]
        if view != "flat":
            args += [f"--{view}", module]
        args += [*extra, "--format", "json", "--output", str(out), str(src)]
        result = CliRunner().invoke(app, args)
        assert result.exit_code in (0, 1), result.output
        reports[view] = json.loads(out.read_text())
    return reports


def _rules(report: dict) -> Counter:
    return Counter(v["rule_id"] for v in report["violations"])


def _messages(report: dict, rule_id: str) -> list[str]:
    return [v["message"] for v in report["violations"] if v["rule_id"] == rule_id]


# --- item 1: synchroniser depth across and behind the boundary ---------------

# A four-stage chain, wholly inside the abstracted block.
_DEEP_INSIDE = """\
module blk (input clk, input d, output q);
    logic s1, s2, s3, s4;
    always_ff @(posedge clk) begin s1 <= d; s2 <= s1; s3 <= s2; s4 <= s3; end
    assign q = s4;
endmodule
module top (input fast, input slow, input d, output q);
    logic x;
    always_ff @(posedge slow) x <= d;
    blk u (.clk(fast), .d(x), .q(q));
endmodule
"""

# Two stages in the parent, two in the block.
_SPAN_IN = """\
module blk (input clk, input d, output q);
    logic s1, s2;
    always_ff @(posedge clk) begin s1 <= d; s2 <= s1; end
    assign q = s2;
endmodule
module top (input fast, input slow, input d, output q);
    logic x, p1, p2;
    always_ff @(posedge slow) x <= d;
    always_ff @(posedge fast) begin p1 <= x; p2 <= p1; end
    blk u (.clk(fast), .d(p2), .q(q));
endmodule
"""

# Two stages in the block, two more in the parent behind its output.
_SPAN_OUT = """\
module blk (input clk, input d, output q);
    logic s1, s2;
    always_ff @(posedge clk) begin s1 <= d; s2 <= s1; end
    assign q = s2;
endmodule
module top (input fast, input slow, input d, output q);
    logic x, o, p1, p2;
    always_ff @(posedge slow) x <= d;
    blk u (.clk(fast), .d(x), .q(o));
    always_ff @(posedge fast) begin p1 <= o; p2 <= p1; end
    assign q = p2;
endmodule
"""

# The source flop is inside the block, the four-stage chain in the parent.
_DEEP_BEHIND_OUTPUT = """\
module blk (input clk, input d, output logic q);
    always_ff @(posedge clk) q <= d;
endmodule
module top (input fast, input slow, input d, output q);
    logic o, s1, s2, s3, s4;
    blk u (.clk(slow), .d(d), .q(o));
    always_ff @(posedge fast) begin s1 <= o; s2 <= s1; s3 <= s2; s4 <= s3; end
    assign q = s4;
endmodule
"""


@needs_slang
def test_deep_chain_inside_a_greybox_is_cdc018(tmp_path: Path) -> None:
    """The issue's reproducer: the per-module pass proves the depth-4
    chain, and CDC-018 now reads the proven depth."""
    reports = _views(tmp_path, _DEEP_INSIDE, _SDC + _typed("slow", "d"))
    assert _rules(reports["flat"]) == Counter({"CDC-018": 1})
    assert _rules(reports["greybox"]) == Counter({"CDC-018": 1})
    (msg,) = _messages(reports["greybox"], "CDC-018")
    assert "4-flop chain in fast starting at abstracted block input u.d" in msg
    # A stub has no body to prove a chain in: the crossing into it is
    # a depth-1 capture, as before.
    assert _rules(reports["blackbox"]) == Counter({"CDC-001": 1})


@needs_slang
def test_deep_chain_threshold_applies_to_the_proven_depth(tmp_path: Path) -> None:
    reports = _views(
        tmp_path,
        _DEEP_INSIDE,
        _SDC + _typed("slow", "d"),
        "--cdc-018-depth-threshold",
        "5",
    )
    assert _rules(reports["flat"]) == Counter()
    assert _rules(reports["greybox"]) == Counter()


@needs_slang
def test_chain_entering_a_greybox_is_measured_whole(tmp_path: Path) -> None:
    reports = _views(tmp_path, _SPAN_IN, _SDC + _typed("slow", "d"))
    assert _rules(reports["flat"]) == Counter({"CDC-018": 1})
    assert _rules(reports["greybox"]) == Counter({"CDC-018": 1})
    (msg,) = _messages(reports["greybox"], "CDC-018")
    assert "4-flop chain" in msg
    # The stub proves nothing behind its pin; the parent's two stages are
    # a complete synchroniser on their own.
    assert _rules(reports["blackbox"]) == Counter()


@needs_slang
def test_chain_leaving_a_greybox_is_measured_whole(tmp_path: Path) -> None:
    reports = _views(tmp_path, _SPAN_OUT, _SDC + _typed("slow", "d"))
    assert _rules(reports["flat"]) == Counter({"CDC-018": 1})
    assert _rules(reports["greybox"]) == Counter({"CDC-018": 1})
    assert _rules(reports["blackbox"]) == Counter({"CDC-001": 1})


@needs_slang
def test_one_stage_each_side_is_a_two_stage_synchroniser(tmp_path: Path) -> None:
    """A capture flop in the parent feeding a single stage in the block is
    a 2FF chain: the greybox used to report it as an unsynchronised
    CDC-001."""
    sv = """\
module blk (input clk, input d, output q);
    logic s1;
    always_ff @(posedge clk) s1 <= d;
    assign q = s1;
endmodule
module top (input fast, input slow, input d, output q);
    logic x, p1;
    always_ff @(posedge slow) x <= d;
    always_ff @(posedge fast) p1 <= x;
    blk u (.clk(fast), .d(p1), .q(q));
endmodule
"""
    reports = _views(tmp_path, sv, _SDC + _typed("slow", "d"))
    assert _rules(reports["flat"]) == Counter()
    assert _rules(reports["greybox"]) == Counter()
    # ...and ``--sync-depth 3`` still finds it two deep in both.
    deeper = _views(tmp_path, sv, _SDC + _typed("slow", "d"), "--sync-depth", "3")
    assert _rules(deeper["flat"]) == Counter({"CDC-002": 1})
    assert _rules(deeper["greybox"]) == Counter({"CDC-002": 1})


@needs_slang
def test_chain_into_another_clock_inside_is_not_joined(tmp_path: Path) -> None:
    """The join needs the block to capture in the parent stage's clock."""
    sv = """\
module blk (input clk, input d, output q);
    logic s1, s2, s3;
    always_ff @(posedge clk) begin s1 <= d; s2 <= s1; s3 <= s2; end
    assign q = s3;
endmodule
module top (input fast, input slow, input d, output q);
    logic x, p1, p2;
    always_ff @(posedge fast) x <= d;
    always_ff @(posedge slow) begin p1 <= x; p2 <= p1; end
    blk u (.clk(fast), .d(p2), .q(q));
endmodule
"""
    reports = _views(tmp_path, sv, _SDC + _typed("fast", "d"))
    for view in ("flat", "greybox"):
        assert "CDC-018" not in _rules(reports[view]), view


@needs_slang
def test_deep_chain_behind_a_block_output_is_cdc018(tmp_path: Path) -> None:
    """The source flop sits in the block: the crossing is launched from
    its output, and both abstracted views know the output's domain."""
    reports = _views(tmp_path, _DEEP_BEHIND_OUTPUT, _SDC + _typed("slow", "d"))
    for view in ("flat", "greybox", "blackbox"):
        assert _rules(reports[view]) == Counter({"CDC-018": 1}), view
    (msg,) = _messages(reports["greybox"], "CDC-018")
    assert "(source: boundary u.q)" in msg


# --- the audit: each rule with its structure inside the abstracted block ----

# (id, what is inside the block, source, SDC, expected rule counts per view).
# A greybox must agree with the flat run; a stub (--blackbox) has no
# body, so it knows only the pins: what it reports is pinned here too.
_AUDIT = [
    pytest.param(
        # CDC-009: a fast pulse register feeds a slow block's 2FF
        """\
module m (input clk, input p, output q);
  logic s1, s2; always_ff @(posedge clk) begin s1 <= p; s2 <= s1; end assign q = s2;
endmodule
module top (input fast, input slow, input go, output q);
  logic g1, g2, pulse; always_ff @(posedge fast) begin g1 <= go; g2 <= g1; pulse <= g1 & ~g2; end
  m u (.clk(slow), .p(pulse), .q(q));
endmodule
""",
        "create_clock -name fast -period 2 [get_ports fast]\ncreate_clock -name slow -period 20 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports go]\n",
        {"CDC-009": 1},
        {"CDC-009": 1},
        {"CDC-001": 1, "CDC-009": 1},
        id="in_009",
    ),
    pytest.param(
        # CDC-009: a fast pulse register inside the block drives its output
        """\
module m (input clk, input go, output logic pulse);
  logic g1, g2; always_ff @(posedge clk) begin g1 <= go; g2 <= g1; pulse <= g1 & ~g2; end
endmodule
module top (input fast, input slow, input go, output logic q);
  logic o, s1; m u (.clk(fast), .go(go), .pulse(o));
  always_ff @(posedge slow) begin s1 <= o; q <= s1; end
endmodule
""",
        "create_clock -name fast -period 2 [get_ports fast]\ncreate_clock -name slow -period 20 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports go]\n",
        {"CDC-009": 1},
        {"CDC-009": 1},
        {},
        id="out_009",
    ),
    pytest.param(
        # CDC-019: one-hot decoded registers feed four block 2FFs
        """\
module m (input clk, input a, input b, input c, input e, output [3:0] q);
  logic a1, a2, b1, b2, c1, c2, e1, e2;
  always_ff @(posedge clk) begin a1 <= a; a2 <= a1; b1 <= b; b2 <= b1; c1 <= c; c2 <= c1; e1 <= e; e2 <= e1; end
  assign q = {e2, c2, b2, a2};
endmodule
module top (input fast, input slow, input [1:0] sel, output [3:0] q);
  logic [3:0] one_hot; always_comb begin one_hot = '0; one_hot[sel] = 1'b1; end
  logic d0, d1, d2, d3; always_ff @(posedge fast) begin d0 <= one_hot[0]; d1 <= one_hot[1]; d2 <= one_hot[2]; d3 <= one_hot[3]; end
  m u (.clk(slow), .a(d0), .b(d1), .c(d2), .e(d3), .q(q));
endmodule
""",
        "create_clock -name fast -period 2 [get_ports fast]\ncreate_clock -name slow -period 20 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports sel]\n",
        {"CDC-019": 1},
        {"CDC-019": 1},
        {"CDC-BBX": 1},
        id="in_019b",
    ),
    pytest.param(
        # CDC-019: one-hot decoded registers inside the block drive four outputs
        """\
module m (input clk, input [1:0] sel, output logic d0, output logic d1, output logic d2, output logic d3);
  logic [3:0] one_hot; always_comb begin one_hot = '0; one_hot[sel] = 1'b1; end
  always_ff @(posedge clk) begin d0 <= one_hot[0]; d1 <= one_hot[1]; d2 <= one_hot[2]; d3 <= one_hot[3]; end
endmodule
module top (input fast, input slow, input [1:0] sel, output [3:0] q);
  logic d0, d1, d2, d3; m u (.clk(fast), .sel(sel), .d0(d0), .d1(d1), .d2(d2), .d3(d3));
  logic d0_m, d0_s, d1_m, d1_s, d2_m, d2_s, d3_m, d3_s;
  always_ff @(posedge slow) begin d0_m <= d0; d0_s <= d0_m; d1_m <= d1; d1_s <= d1_m; d2_m <= d2; d2_s <= d2_m; d3_m <= d3; d3_s <= d3_m; end
  assign q = {d3_s, d2_s, d1_s, d0_s};
endmodule
""",
        "create_clock -name fast -period 2 [get_ports fast]\ncreate_clock -name slow -period 20 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports sel]\n",
        {"CDC-019": 1},
        {"CDC-019": 1},
        {},
        id="out_019",
    ),
    pytest.param(
        # RDC-002: a parent register's reset value mismatches a block flop
        """\
module m (input clk, input rst_n, input a, output logic q);
  always_ff @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else q <= a;
endmodule
module top (input fast, input slow, input d, input r, input a, output q);
  logic rp; always_ff @(posedge fast or posedge r) if (r) rp <= 1; else rp <= d;
  m u (.clk(fast), .rst_n(rp), .a(a), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports {d r a}]",
        {"RDC-002": 1},
        {"RDC-002": 1},
        {},
        id="in_rdc002",
    ),
    pytest.param(
        # RDC-007: a backwards reset synchroniser wholly inside the block
        """\
module m (input clk, input rst_n, input a, output logic q);
  logic s1, s2;
  always_ff @(posedge clk or negedge rst_n) if (!rst_n) begin s1 <= 0; s2 <= 0; end else begin s1 <= 1'b0; s2 <= s1; end
  always_ff @(posedge clk or negedge s2) if (!s2) q <= 0; else q <= a;
endmodule
module top (input fast, input slow, input r, input a, output q);
  m u (.clk(fast), .rst_n(r), .a(a), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports {r a}]",
        {"RDC-007": 1},
        {"RDC-007": 1},
        {},
        id="int_rdc007",
    ),
    pytest.param(
        # CDC-006: a gate over two top-level ports in front of a block's 2FF
        """\
module m (input clk, input a, output q);
  logic s1, s2; always_ff @(posedge clk) begin s1 <= a; s2 <= s1; end assign q = s2;
endmodule
module top (input fast, input d, input e, output q);
  m u (.clk(fast), .a(d & e), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\nset_input_delay -clock slow 1 [get_ports e]\n",
        {"CDC-006": 1},
        {"CDC-006": 1},
        {"CDC-001": 2},
        id="in_006_glitchy",
    ),
    pytest.param(
        # CDC-006: the gate over the two port pins is inside the block
        """\
module m (input clk, input a, input b, output q);
  logic s1, s2; always_ff @(posedge clk) begin s1 <= a & b; s2 <= s1; end assign q = s2;
endmodule
module top (input fast, input d, input e, output q);
  m u (.clk(fast), .a(d), .b(e), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\nset_input_delay -clock slow 1 [get_ports e]\n",
        {"CDC-006": 1},
        {"CDC-006": 1},
        {"CDC-BBX": 1},
        id="in_006_glitchy_inside",
    ),
    pytest.param(
        # CDC-003: two foreign flops gated inside the block before its 2FF
        """\
module m (input clk, input a, input b, output q);
  logic s1, s2; always_ff @(posedge clk) begin s1 <= a & b; s2 <= s1; end assign q = s2;
endmodule
module top (input fast, input slow, input d, input e, output q);
  logic x, y; always_ff @(posedge slow) begin x <= d; y <= e; end
  m u (.clk(fast), .a(x), .b(y), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\nset_input_delay -clock slow 1 [get_ports e]\n",
        {"CDC-003": 2},
        {"CDC-003": 2},
        {"CDC-BBX": 1},
        id="in_003_comb",
    ),
    pytest.param(
        # CDC-003: an inverter inside the block before its 2FF
        """\
module m (input clk, input a, output q);
  logic s1, s2; always_ff @(posedge clk) begin s1 <= ~a; s2 <= s1; end assign q = s2;
endmodule
module top (input fast, input slow, input d, output q);
  logic x; always_ff @(posedge slow) x <= d;
  m u (.clk(fast), .a(x), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\n",
        {"CDC-003": 1},
        {"CDC-003": 1},
        {"CDC-001": 1},
        id="in_003_comb1",
    ),
    pytest.param(
        # CDC-014: a gate between the block's first and second stage
        """\
module m (input clk, input a, output q);
  logic s1, s2; always_ff @(posedge clk) begin s1 <= a; s2 <= ~s1; end assign q = s2;
endmodule
module top (input fast, input slow, input d, output q);
  logic x; always_ff @(posedge slow) x <= d;
  m u (.clk(fast), .a(x), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\n",
        {"CDC-014": 1},
        {"CDC-014": 1},
        {"CDC-001": 1},
        id="in_014_comb_between",
    ),
    pytest.param(
        # CDC-016: the block's 2FF samples on opposite edges
        """\
module m (input clk, input a, output q);
  logic s1, s2; always_ff @(posedge clk) s1 <= a; always_ff @(negedge clk) s2 <= s1; assign q = s2;
endmodule
module top (input fast, input slow, input d, output q);
  logic x; always_ff @(posedge slow) x <= d;
  m u (.clk(fast), .a(x), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\n",
        {"CDC-016": 1},
        {"CDC-016": 1},
        {"CDC-001": 1},
        id="in_016_opp_edge",
    ),
    pytest.param(
        # RDC-001: a foreign flop drives a block flop's async reset pin
        """\
module m (input clk, input rst_n, input a, output logic q);
  always_ff @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else q <= a;
endmodule
module top (input fast, input slow, input d, input r, output q);
  logic rr; always_ff @(posedge slow) rr <= r;
  m u (.clk(fast), .rst_n(rr), .a(d), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports r]\nset_input_delay -clock fast 1 [get_ports d]\n",
        {"RDC-001": 1},
        {"RDC-001": 1},
        {"CDC-001": 1},
        id="in_rdc001_arst",
    ),
    pytest.param(
        # RDC-003: a foreign flop drives a block flop's sync reset
        """\
module m (input clk, input rst, input a, output logic q);
  always_ff @(posedge clk) if (rst) q <= 0; else q <= a;
endmodule
module top (input fast, input slow, input d, input r, output q);
  logic rr; always_ff @(posedge slow) rr <= r;
  m u (.clk(fast), .rst(rr), .a(d), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports r]\nset_input_delay -clock fast 1 [get_ports d]\n",
        {"CDC-001": 1, "RDC-003": 1},
        {"CDC-001": 1, "RDC-003": 1},
        {"CDC-001": 1},
        id="in_rdc003_srst",
    ),
    pytest.param(
        # CDC-015 / RDC-001: the block's 2FF is reset from a foreign flop
        """\
module m (input clk, input rst_n, input a, output q);
  logic s1, s2; always_ff @(posedge clk or negedge rst_n) if (!rst_n) begin s1 <= 0; s2 <= 0; end else begin s1 <= a; s2 <= s1; end
  assign q = s2;
endmodule
module top (input fast, input slow, input d, input r, output q);
  logic x, rr; always_ff @(posedge slow) begin x <= d; rr <= r; end
  m u (.clk(fast), .rst_n(rr), .a(x), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\nset_input_delay -clock slow 1 [get_ports r]\n",
        {"CDC-015": 1, "RDC-001": 1},
        {"CDC-015": 1, "RDC-001": 1},
        {"CDC-BBX": 1},
        id="in_015_foreign_arst",
    ),
    pytest.param(
        # RDC-004: the block gates two registered resets
        """\
module m (input clk, input r1, input r2, input a, output logic q);
  wire rn = r1 & r2;
  always_ff @(posedge clk or negedge rn) if (!rn) q <= 0; else q <= a;
endmodule
module top (input fast, input a, input b, input d, output q);
  logic x, y; always_ff @(posedge fast) begin x <= a; y <= b; end
  m u (.clk(fast), .r1(x), .r2(y), .a(d), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports a]\nset_input_delay -clock fast 1 [get_ports b]\nset_input_delay -clock fast 1 [get_ports d]\n",
        {"RDC-004": 1},
        {"RDC-004": 1},
        {},
        id="in_rdc004_comb_rst",
    ),
    pytest.param(
        # RDC-005: the block gates two top-level reset ports
        """\
module m (input clk, input r1, input r2, input a, output logic q);
  wire rn = r1 & r2;
  always_ff @(posedge clk or negedge rn) if (!rn) q <= 0; else q <= a;
endmodule
module top (input fast, input ra, input rb, input d, output q);
  m u (.clk(fast), .r1(ra), .r2(rb), .a(d), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports ra]\nset_input_delay -clock fast 1 [get_ports rb]\nset_input_delay -clock fast 1 [get_ports d]\n",
        {"RDC-005": 1},
        {"RDC-005": 1},
        {},
        id="in_rdc005_multi",
    ),
    pytest.param(
        # RDC-006: the block muxes two reset ports
        """\
module m (input clk, input s, input r1, input r2, input a, output logic q);
  wire rn = s ? r1 : r2;
  always_ff @(posedge clk or negedge rn) if (!rn) q <= 0; else q <= a;
endmodule
module top (input fast, input sel, input ra, input rb, input d, output q);
  m u (.clk(fast), .s(sel), .r1(ra), .r2(rb), .a(d), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports sel]\nset_input_delay -clock fast 1 [get_ports ra]\nset_input_delay -clock fast 1 [get_ports rb]\nset_input_delay -clock fast 1 [get_ports d]\n",
        {"RDC-006": 1},
        {"RDC-006": 1},
        {},
        id="in_rdc006_mux",
    ),
    pytest.param(
        # CDC-003: a gate over two registers drives a block output
        """\
module m (input clk, input a, input b, output q);
  logic x, y; always_ff @(posedge clk) begin x <= a; y <= b; end assign q = x & y;
endmodule
module top (input fast, input slow, input d, input e, output logic q);
  logic o, s1; m u (.clk(slow), .a(d), .b(e), .q(o));
  always_ff @(posedge fast) begin s1 <= o; q <= s1; end
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\nset_input_delay -clock slow 1 [get_ports e]\n",
        {"CDC-003": 2},
        {"CDC-003": 2},
        {},
        id="out_003_comb",
    ),
    pytest.param(
        # CDC-004: a gray-coded register drives a block output (silent)
        """\
module m (input clk, input rst_n, input inc, output logic [3:0] g);
  logic [3:0] b, nb; assign nb = b + 4'd1;
  always_ff @(posedge clk or negedge rst_n) if (!rst_n) begin b <= 0; g <= 0; end else if (inc) begin b <= nb; g <= nb ^ (nb >> 1); end
endmodule
module top (input fast, input slow, input rst_n, input i, output logic [3:0] q);
  logic [3:0] o, s1; m u (.clk(slow), .rst_n(rst_n), .inc(i), .g(o));
  always_ff @(posedge fast or negedge rst_n) if (!rst_n) begin s1 <= 0; q <= 0; end else begin s1 <= o; q <= s1; end
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports i]\nset_input_delay -clock slow 1 [get_ports rst_n]\n",
        {},
        {},
        {"CDC-004": 1},
        id="out_004_gray2",
    ),
    pytest.param(
        # CDC-005: a block output feeds two parent synchronisers that recombine
        """\
module m (input clk, input d, output logic q);
  always_ff @(posedge clk) q <= d;
endmodule
module top (input fast, input slow, input d, output q);
  logic o, a1, a2, b1, b2; m u (.clk(slow), .d(d), .q(o));
  always_ff @(posedge fast) begin a1 <= o; a2 <= a1; b1 <= o; b2 <= b1; end
  assign q = a2 & b2;
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\n",
        {"CDC-005": 1},
        {"CDC-005": 1},
        {"CDC-005": 1},
        id="out_005_reconv",
    ),
    pytest.param(
        # CDC-013: a fast toggle register drives a block output
        """\
module m (input clk, input en, output logic t);
  always_ff @(posedge clk) t <= en ? ~t : t;
endmodule
module top (input fast, input slow, input e, output logic q);
  logic o, s1, s2; m u (.clk(fast), .en(e), .t(o));
  always_ff @(posedge slow) begin s1 <= o; s2 <= s1; q <= s2 ^ s1; end
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports e]\n",
        {"CDC-013": 1, "CDC-014": 1},
        {"CDC-013": 1, "CDC-014": 1},
        {"CDC-014": 1},
        id="out_013_toggle",
    ),
    pytest.param(
        # CDC-015 / RDC-001: a block output resets a parent 2FF
        """\
module m (input clk, input d, output logic q);
  always_ff @(posedge clk) q <= d;
endmodule
module top (input fast, input slow, input d, input r, input a, output q);
  logic rn, x, s1, s2; m u (.clk(slow), .d(r), .q(rn));
  always_ff @(posedge slow) x <= d;
  always_ff @(posedge fast or negedge rn) if (!rn) begin s1 <= 0; s2 <= 0; end else begin s1 <= x; s2 <= s1; end
  assign q = s2;
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\nset_input_delay -clock slow 1 [get_ports r]\n",
        {"CDC-015": 1, "RDC-001": 1},
        {"CDC-015": 1, "RDC-001": 1},
        {"CDC-015": 1, "RDC-001": 1},
        id="out_015",
    ),
    pytest.param(
        # CDC-017: a latch inside the block drives its output
        """\
module m (input clk, input en, input d, output q);
  logic x, l; always_ff @(posedge clk) x <= d; always_latch if (en) l = x; assign q = l;
endmodule
module top (input fast, input slow, input d, input e, output logic q);
  logic o, s1; m u (.clk(slow), .en(e), .d(d), .q(o));
  always_ff @(posedge fast) begin s1 <= o; q <= s1; end
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\nset_input_delay -clock slow 1 [get_ports e]\n",
        {"CDC-017": 1},
        {"CDC-017": 1},
        {},
        id="out_017_latch",
    ),
    pytest.param(
        # CDC-020: a 2-bit block register sliced into two parent syncs
        """\
module m (input clk, input [1:0] d, output logic [1:0] x);
  always_ff @(posedge clk) x <= d;
endmodule
module top (input fast, input slow, input [1:0] d, output q0, output q1);
  logic [1:0] o; logic a1, a2, b1, b2; m u (.clk(slow), .d(d), .x(o));
  always_ff @(posedge fast) begin a1 <= o[0]; a2 <= a1; b1 <= o[1]; b2 <= b1; end assign q0 = a2; assign q1 = b2;
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\n",
        {"CDC-020": 1},
        {"CDC-020": 1},
        {},
        id="out_020_sliced",
    ),
    pytest.param(
        # CDC-024: a block output drives a top output with an async contract
        """\
module m (input clk, input d, output logic q);
  always_ff @(posedge clk) q <= d;
endmodule
module top (input fast, input slow, input d, output q);
  m u (.clk(fast), .d(d), .q(q));
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports d]\nset_output_delay -clock slow 1 [get_ports q]\n",
        {"CDC-024": 1},
        {"CDC-024": 1},
        {"CDC-024": 1},
        id="out_024",
    ),
    pytest.param(
        # RDC-001: a block output drives a parent async reset
        """\
module m (input clk, input d, output logic q);
  always_ff @(posedge clk) q <= d;
endmodule
module top (input fast, input slow, input d, input a, output logic q);
  logic rn; m u (.clk(slow), .d(d), .q(rn));
  always_ff @(posedge fast or negedge rn) if (!rn) q <= 0; else q <= a;
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\nset_input_delay -clock fast 1 [get_ports a]\n",
        {"RDC-001": 1},
        {"RDC-001": 1},
        {"RDC-001": 1},
        id="out_rdc001",
    ),
    pytest.param(
        # RDC-002: a block register's reset value mismatches the consumer
        """\
module m (input clk, input d, input r, output logic q);
  always_ff @(posedge clk or posedge r) if (r) q <= 1; else q <= d;
endmodule
module top (input fast, input slow, input d, input r, input a, output logic q);
  logic rn; m u (.clk(fast), .d(d), .r(r), .q(rn));
  always_ff @(posedge fast or negedge rn) if (!rn) q <= 0; else q <= a;
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock fast 1 [get_ports d]\nset_input_delay -clock fast 1 [get_ports r]\nset_input_delay -clock fast 1 [get_ports a]\n",
        {"RDC-002": 1},
        {"RDC-002": 1},
        {},
        id="out_rdc002",
    ),
    pytest.param(
        # RDC-003: a block output drives a parent sync reset
        """\
module m (input clk, input d, output logic q);
  always_ff @(posedge clk) q <= d;
endmodule
module top (input fast, input slow, input d, input a, output logic q);
  logic rs; m u (.clk(slow), .d(d), .q(rs));
  always_ff @(posedge fast) if (rs) q <= 0; else q <= a;
endmodule
""",
        "create_clock -name fast -period 4 [get_ports fast]\ncreate_clock -name slow -period 9 [get_ports slow]\nset_clock_groups -asynchronous -group {fast} -group {slow}\nset_input_delay -clock slow 1 [get_ports d]\nset_input_delay -clock fast 1 [get_ports a]\n",
        {"CDC-001": 1, "RDC-003": 1},
        {"CDC-001": 1, "RDC-003": 1},
        {"CDC-001": 1, "RDC-003": 1},
        id="out_rdc003",
    ),
]


@needs_slang
@pytest.mark.parametrize(("sv", "sdc", "flat", "greybox", "blackbox"), _AUDIT)
def test_audit_views(
    tmp_path: Path,
    sv: str,
    sdc: str,
    flat: dict[str, int],
    greybox: dict[str, int],
    blackbox: dict[str, int],
) -> None:
    assert flat == greybox  # the table itself states the parity
    reports = _views(tmp_path, sv, sdc, module="m")
    assert _rules(reports["flat"]) == Counter(flat)
    assert _rules(reports["greybox"]) == Counter(greybox)
    assert _rules(reports["blackbox"]) == Counter(blackbox)


# --- item 3: a stub's clock pin ---------------------------------------------

_TAP = """\
module tap (input smp_clk, input d, output q);
    logic a, b;
    always_ff @(posedge smp_clk) begin a <= d; b <= a; end
    assign q = b;
endmodule
"""

_FAST_ONLY = (
    "create_clock -name fast -period 4 [get_ports fast]\n"
    "set_input_delay -clock fast 1 [get_ports d]\n"
)


@needs_slang
def test_stub_clock_pin_proven_by_a_sibling_instance(tmp_path: Path) -> None:
    """``u1.smp_clk`` traces to a declared clock, which proves ``smp_clk`` a
    clock pin of ``tap``; ``u2``'s, driven from the undeclared ``ck``, is
    then a clock sink under ``--blackbox`` too, and CDC-021 fires on it as
    it does on the flat flops."""
    sv = _TAP + (
        "module top (input fast, input ck, input d, output q0, output q1);\n"
        "    tap u1 (.smp_clk(fast), .d(d), .q(q0));\n"
        "    tap u2 (.smp_clk(ck), .d(d), .q(q1));\n"
        "endmodule\n"
    )
    reports = _views(tmp_path, sv, _FAST_ONLY, module="tap")
    for view in ("flat", "greybox", "blackbox"):
        assert _rules(reports[view])["CDC-021"] == 1, view
    (msg,) = _messages(reports["blackbox"], "CDC-021")
    assert "clock pin of abstracted instance u2.smp_clk" in msg


@needs_slang
def test_stub_clock_pin_nothing_proves_stays_data(tmp_path: Path) -> None:
    """The documented limit: no instance proves ``strobe`` a clock pin (no
    allow-listed name, no declared clock behind it), and a stub declares
    nothing about its pins, so ``--blackbox`` reads it as data and reports
    no CDC-021; the flat and greybox runs see the flops it clocks."""
    sv = (
        "module tap (input strobe, input d, output q);\n"
        "    logic a, b;\n"
        "    always_ff @(posedge strobe) begin a <= d; b <= a; end\n"
        "    assign q = b;\n"
        "endmodule\n"
        "module top (input fast, input ck, input d, output q);\n"
        "    tap u (.strobe(ck), .d(d), .q(q));\n"
        "endmodule\n"
    )
    reports = _views(tmp_path, sv, _FAST_ONLY, module="tap")
    assert _rules(reports["flat"])["CDC-021"] == 1
    assert _rules(reports["greybox"])["CDC-021"] == 1
    assert "CDC-021" not in _rules(reports["blackbox"])


# --- item 4: the clock views that counted flops -------------------------------

_BANK = """\
module bank (input clk, input d, output q);
    logic a, b, c, e;
    always_ff @(posedge clk) begin a <= d; b <= a; c <= b; e <= c; end
    assign q = e;
endmodule
module top (input fast, input fast2, input slow, input s, input d,
            output q0, output q1);
    logic div;
    always_ff @(posedge fast) div <= ~div;
    bank u_div (.clk(div), .d(d), .q(q0));
    logic sel;
    always_ff @(posedge slow) sel <= s;
    wire m = sel ? fast2 : fast;
    bank u_m (.clk(m), .d(d), .q(q1));
endmodule
"""

_BANK_SDC = (
    "create_clock -name fast -period 4 [get_ports {fast fast2}]\n"
    "create_clock -name slow -period 9 [get_ports slow]\n"
    "set_clock_groups -asynchronous -group {fast} -group {slow}\n"
    "set_input_delay -clock slow 1 [get_ports s]\n"
    "set_input_delay -clock fast 1 [get_ports d]\n"
)


def _maps(tmp_path: Path) -> dict[str, tuple[dict, dict]]:
    src = tmp_path / "t.sv"
    src.write_text(_BANK)
    sdc = tmp_path / "t.sdc"
    sdc.write_text(_BANK_SDC)
    out = {}
    for view in ("flat", "greybox", "blackbox"):
        report, dmap = tmp_path / f"{view}.json", tmp_path / f"{view}.map.json"
        args = ["lint", "--top", "top", "--sdc", str(sdc), "--frontend", "slang"]
        if view != "flat":
            args += [f"--{view}", "bank"]
        args += ["--format", "json", "--output", str(report)]
        args += ["--emit-domain-map", str(dmap), str(src)]
        result = CliRunner().invoke(app, args)
        assert result.exit_code in (0, 1), result.output
        out[view] = (json.loads(report.read_text()), json.loads(dmap.read_text()))
    return out


@needs_slang
def test_inferred_clock_counts_the_flops_behind_a_greybox_pin(tmp_path: Path) -> None:
    """``div`` clocks four flops, all inside ``u_div``: the flat run and the
    greybox (which weighs the pin by its body's four flops) both report
    it. A stub's pin counts one flop — its body is unknown — so the
    advisory stays under its threshold there."""
    maps = _maps(tmp_path)
    flat = maps["flat"][0]["inferred_clock_candidates"]
    grey = maps["greybox"][0]["inferred_clock_candidates"]
    assert [(c["driver"], c["fanout"]) for c in flat] == [
        (c["driver"], c["fanout"]) for c in grey
    ]
    assert len(grey) == 1 and grey[0]["fanout"] == 4
    assert grey[0]["example_sinks"] == ["u_div.clk"]
    assert maps["blackbox"][0]["inferred_clock_candidates"] == []


@needs_slang
def test_clock_network_edges_end_at_an_abstracted_clock_pin(tmp_path: Path) -> None:
    """The select of a clock mux in front of ``u_m`` comes from ``slow``:
    the flat domain map lists an edge per flop inside, the abstracted ones
    an edge to the pin, from the same source flop, marked
    ``dst_boundary``."""
    maps = _maps(tmp_path)
    flat = maps["flat"][1]["clock_network_crossings"]
    assert len(flat) == 4
    assert {e["dst_flop"].rsplit(".", 1)[0] for e in flat} == {"top.u_m"}
    assert all("dst_boundary" not in e for e in flat)
    for view in ("greybox", "blackbox"):
        (edge,) = maps[view][1]["clock_network_crossings"]
        assert edge["dst_flop"] == "top.u_m.clk", view
        assert edge["dst_boundary"] == {"instance": "u_m", "port": "clk"}, view
        assert edge["control_kind"] == "mux-select", view
        assert edge["src_clock"] == "slow" and edge["dst_clock"] == "fast", view
        assert "CDC-010" in edge["findings"], view
