"""Boundary verdicts after the rtl-buddy-cdc#405 round (rtl-buddy-cdc#408).

Item 1 is a flat-run verdict: a clock gate whose enable is a flop in a
foreign domain is an async control on the clock network (CDC-010), not
a combine of two clocks (CDC-023). The other items compare the flat run
with ``--greybox`` (body kept, analysed once on its own) and
``--blackbox`` (bodyless stub) on designs that put the structure a rule
reads inside, or behind, an abstracted block.

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


def _run(tmp_path: Path, sv: str, sdc: str, *args: str, tag: str = "flat") -> dict:
    src = tmp_path / "t.sv"
    src.write_text(sv)
    sdc_path = tmp_path / "t.sdc"
    sdc_path.write_text(sdc)
    out = tmp_path / f"{tag}.json"
    cli = ["lint", "--top", "top", "--sdc", str(sdc_path), "--frontend", "slang"]
    cli += [*args, "--format", "json", "--output", str(out), str(src)]
    result = CliRunner().invoke(app, cli)
    assert result.exit_code in (0, 1), result.output
    return json.loads(out.read_text())


def _views(
    tmp_path: Path, sv: str, sdc: str, *extra: str, module: str = "blk"
) -> dict[str, dict]:
    reports = {"flat": _run(tmp_path, sv, sdc, *extra, tag="flat")}
    for view in ("greybox", "blackbox"):
        reports[view] = _run(tmp_path, sv, sdc, f"--{view}", module, *extra, tag=view)
    return reports


def _rules(report: dict) -> Counter:
    return Counter(v["rule_id"] for v in report["violations"])


def _messages(report: dict, rule_id: str) -> list[str]:
    return [v["message"] for v in report["violations"] if v["rule_id"] == rule_id]


# --- item 1: a foreign-domain enable on a clock gate is CDC-010 ---------------

_AND_GATE_ASYNC_EN = """\
module top (input fast, input slow, input e, input d, output logic q);
    logic x;
    always_ff @(posedge slow) x <= e;
    wire g = fast & x;
    always_ff @(posedge g) q <= d;
endmodule
"""

_AND_GATE_SYNCED_EN = """\
module top (input fast, input slow, input e, input d, output logic q);
    logic x, s1, s2;
    always_ff @(posedge slow) x <= e;
    always_ff @(posedge fast) begin s1 <= x; s2 <= s1; end
    wire g = fast & s2;
    always_ff @(posedge g) q <= d;
endmodule
"""

_LATCH_ICG_ASYNC_EN = """\
module top (input fast, input slow, input e, input d, output logic q);
    logic x, el;
    always_ff @(posedge slow) x <= e;
    always_latch if (!fast) el = x;
    wire g = fast & el;
    always_ff @(posedge g) q <= d;
endmodule
"""

# Two undeclared dividers of two clocks, combined: no direct clock leg
# is left to take the flop, so the gate still declines as a combine.
_TWO_DIVIDERS = """\
module top (input fast, input slow, input d, output logic q);
    logic da, db;
    always_ff @(posedge fast) da <= ~da;
    always_ff @(posedge slow) db <= ~db;
    wire g = da & db;
    always_ff @(posedge g) q <= d;
endmodule
"""


@needs_slang
def test_and_gate_with_foreign_enable_is_cdc010(tmp_path: Path) -> None:
    report = _run(
        tmp_path, _AND_GATE_ASYNC_EN, _SDC + _typed("slow", "e") + _typed("fast", "d")
    )
    assert _rules(report) == Counter({"CDC-010": 1})
    (msg,) = _messages(report, "CDC-010")
    assert "in domain slow" in msg and "['fast']" in msg
    # The gated flop resolves to the gate's clock.
    assert report["summary"]["domain_unknown"] == 0


@needs_slang
def test_and_gate_with_synchronised_enable_is_clean(tmp_path: Path) -> None:
    report = _run(
        tmp_path, _AND_GATE_SYNCED_EN, _SDC + _typed("slow", "e") + _typed("fast", "d")
    )
    assert _rules(report) == Counter()


@needs_slang
def test_latch_icg_with_foreign_enable_is_cdc010(tmp_path: Path) -> None:
    """The latch takes the clock on EN and the enable on D: CDC-010
    checks the leg the clock walk reads as the control."""
    report = _run(
        tmp_path, _LATCH_ICG_ASYNC_EN, _SDC + _typed("slow", "e") + _typed("fast", "d")
    )
    assert _rules(report) == Counter({"CDC-010": 1})
    (msg,) = _messages(report, "CDC-010")
    assert "($dlatch) control pin D" in msg


@needs_slang
def test_declared_generated_clock_on_the_leg_still_combines(tmp_path: Path) -> None:
    """A divided clock declared with create_generated_clock is a declared
    clock on its own pin: gating it with another clock is a combine."""
    sdc = (
        _SDC
        + "create_generated_clock -name sdiv -source [get_ports slow] "
        + "-divide_by 2 [get_pins x]\n"
        + "set_clock_groups -asynchronous -group {fast} -group {sdiv}\n"
    )
    sv = _AND_GATE_ASYNC_EN.replace("x <= e", "x <= ~x")
    report = _run(tmp_path, sv, sdc + _typed("slow", "e"))
    assert _rules(report)["CDC-023"] == 1
    assert _rules(report)["CDC-010"] == 0


@needs_slang
def test_two_undeclared_dividers_still_combine(tmp_path: Path) -> None:
    report = _run(tmp_path, _TWO_DIVIDERS, _SDC)
    assert _rules(report) == Counter({"CDC-023": 1})


# --- item 2: CDC-012 behind an abstracted block's output ----------------------

# The payload register and the request flop are inside the block; the
# parent synchronises the request and loads the payload on it. Nothing
# holds the payload.
_HOLD_BEHIND_OUTPUT = """\
module blk (input clk, input go, input [7:0] d, output logic req, output logic [7:0] data);
    always_ff @(posedge clk) begin req <= go; data <= d; end
endmodule
module top (input fast, input slow, input go, input [7:0] d, output logic [7:0] q);
    logic r, r1, r2;
    logic [7:0] x;
    blk u (.clk(slow), .go(go), .d(d), .req(r), .data(x));
    always_ff @(posedge fast) begin r1 <= r; r2 <= r1; if (r2) q <= x; end
endmodule
"""

# The same, with a req/ack handshake: the parent returns an acknowledge
# from the destination, the block synchronises it and holds the payload
# until it arrives.
_HELD_BEHIND_OUTPUT = """\
module blk (input clk, input go, input ack, input [7:0] d,
            output logic req, output logic [7:0] data);
    logic a1, a2;
    always_ff @(posedge clk) begin
        a1 <= ack; a2 <= a1;
        req <= go;
        if (a2) data <= d;
    end
endmodule
module top (input fast, input slow, input go, input [7:0] d, output logic [7:0] q);
    logic r, r1, r2, ack;
    logic [7:0] x;
    blk u (.clk(slow), .go(go), .ack(ack), .d(d), .req(r), .data(x));
    always_ff @(posedge fast) begin r1 <= r; r2 <= r1; ack <= r2; if (r2) q <= x; end
endmodule
"""


@needs_slang
def test_hold_behind_a_block_output_is_cdc012(tmp_path: Path) -> None:
    sdc = _SDC + _typed("slow", "go", "d")
    reports = _views(tmp_path, _HOLD_BEHIND_OUTPUT, sdc)
    for view in ("flat", "greybox", "blackbox"):
        assert _rules(reports[view]) == Counter({"CDC-012": 1}), view
    (msg,) = _messages(reports["greybox"], "CDC-012")
    assert "boundary u.data" in msg


@needs_slang
def test_held_behind_a_block_output_is_clean(tmp_path: Path) -> None:
    """The acknowledge leaves the parent through the block's ``ack`` pin:
    the greybox says the payload's hold control reads it, the stub is
    given the benefit of an acknowledge entering it."""
    sdc = _SDC + _typed("slow", "go", "d")
    reports = _views(tmp_path, _HELD_BEHIND_OUTPUT, sdc)
    for view in ("flat", "greybox", "blackbox"):
        assert _rules(reports[view])["CDC-012"] == 0, view


# --- item 3: CDC-025 through a block's combinational feed-through -------------

# The block's output XORs its own register with its input, which the
# parent drives from another clock: the top-level output merges two
# asynchronous launches through the block.
_MERGE_THROUGH_BLOCK = """\
module blk (input clk, input d, output q);
    logic s1;
    always_ff @(posedge clk) s1 <= d;
    assign q = s1 ^ d;
endmodule
module top (input fast, input slow, input d, output q);
    logic x;
    always_ff @(posedge slow) x <= d;
    blk u (.clk(fast), .d(x), .q(q));
endmodule
"""

# Registered output: no feed-through, nothing merges.
_REGISTERED_THROUGH_BLOCK = _MERGE_THROUGH_BLOCK.replace(
    "assign q = s1 ^ d;",
    "logic s2;\n    always_ff @(posedge clk) s2 <= s1;\n    assign q = s2;",
)


@needs_slang
def test_async_merge_through_a_greybox_feedthrough_is_cdc025(tmp_path: Path) -> None:
    reports = _views(tmp_path, _MERGE_THROUGH_BLOCK, _SDC + _typed("slow", "d"))
    assert _rules(reports["flat"])["CDC-025"] == 1
    assert _rules(reports["greybox"]) == _rules(reports["flat"])
    (msg,) = _messages(reports["greybox"], "CDC-025")
    assert "abstracted block output `u.q`" in msg and "slow" in msg
    (v,) = [x for x in reports["greybox"]["violations"] if x["rule_id"] == "CDC-025"]
    assert v["cell_name"] == "u"
    # A stub has no body to show the feed-through: documented limit
    # (architecture spec section 4.9), its output is one launch in the
    # block's clock.
    assert _rules(reports["blackbox"])["CDC-025"] == 0


@needs_slang
def test_registered_block_output_merges_nothing(tmp_path: Path) -> None:
    reports = _views(tmp_path, _REGISTERED_THROUGH_BLOCK, _SDC + _typed("slow", "d"))
    for view in ("flat", "greybox", "blackbox"):
        assert _rules(reports[view])["CDC-025"] == 0, view


@needs_slang
def test_feedthrough_reaches_a_contracted_output(tmp_path: Path) -> None:
    """CDC-024 reads the same cone: a contracted output fed through the
    block from a register in the contract's foreign clock."""
    sdc = _SDC + _typed("slow", "d") + "set_output_delay -clock fast 1 [get_ports q]\n"
    reports = _views(tmp_path, _MERGE_THROUGH_BLOCK, sdc)
    assert _rules(reports["flat"])["CDC-024"] == 1
    assert _rules(reports["greybox"])["CDC-024"] == 1
    (msg,) = _messages(reports["greybox"], "CDC-024")
    assert "slow (flop" in msg


# --- item 4: a block output port literally named Q or Y -----------------------


def _q_reset(name: str) -> str:
    return f"""\
module blk (input clk, input d, output logic {name});
    always_ff @(posedge clk) {name} <= d;
endmodule
module top (input fast, input slow, input d, input e, output logic o);
    logic w;
    blk u (.clk(slow), .d(d), .{name}(w));
    always_ff @(posedge fast or posedge w) if (w) o <= 1'b0; else o <= e;
endmodule
"""


def _y_gate(name: str) -> str:
    return f"""\
module blk (input clk, input d, input e, output logic {name});
    logic a, b;
    always_ff @(posedge clk) begin a <= d; b <= e; end
    assign {name} = a & b;
endmodule
module top (input fast, input slow, input d, input e,
            output o1, output logic o2, output o3);
    logic w, s1, s2;
    blk u (.clk(slow), .d(d), .e(e), .{name}(w));
    always_ff @(posedge fast) begin s1 <= w; s2 <= s1; end
    assign o2 = s2;
    assign o1 = w;
    assign o3 = w ^ s2;
endmodule
"""


_Y_SDC = (
    _SDC
    + _typed("slow", "d", "e")
    + "set_output_delay -clock fast 1 [get_ports o1]\n"
    + "set_output_delay -clock fast 1 [get_ports o2]\n"
)


@needs_slang
@pytest.mark.parametrize("name", ["rst_q", "Q"])
def test_block_output_named_q_is_a_pin_not_a_register(
    tmp_path: Path, name: str
) -> None:
    """A pin named ``Q`` read as a flop's ``Q``: the reset walk stopped at
    a nameless register with no domain and RDC-001 was lost."""
    sdc = _SDC + _typed("slow", "d") + _typed("fast", "e")
    reports = _views(tmp_path, _q_reset(name), sdc)
    for view in ("flat", "greybox", "blackbox"):
        assert _rules(reports[view]) == Counter({"RDC-001": 1}), view


@needs_slang
@pytest.mark.parametrize("name", ["z", "Y"])
def test_block_output_named_y_is_a_pin_not_a_gate(tmp_path: Path, name: str) -> None:
    """A pin named ``Y`` read as a gate's ``Y``: backward walks went on
    through the block's inputs — a spurious CDC-006 on the ports behind
    it, and CDC-025 lost to the block's clock pin in the cone."""
    reports = _views(tmp_path, _y_gate(name), _Y_SDC)
    flat = _rules(reports["flat"])
    assert flat == Counter({"CDC-003": 2, "CDC-024": 1, "CDC-025": 1})
    assert _rules(reports["greybox"]) == flat
    # A stub cannot show the gate behind its pin (no CDC-003).
    assert _rules(reports["blackbox"]) == Counter({"CDC-024": 1, "CDC-025": 1})
