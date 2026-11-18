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
