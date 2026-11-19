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


# --- items 5-9: the rule the flat run reports, at the block's pin -------------

_CLOCK_MUX_SELECT_PIN = """\
module blk (input clk, input clk2, input s, input a, output logic q);
    wire g = s ? clk2 : clk;
    always_ff @(posedge g) q <= a;
endmodule
module top (input fast, input slow, input e, input d, output q);
    logic x;
    always_ff @(posedge slow) x <= e;
    blk u (.clk(fast), .clk2(fast), .s(x), .a(d), .q(q));
endmodule
"""

_ICG_ENABLE_PIN = """\
module blk (input clk, input en, input a, output logic q);
    logic el;
    always_latch if (!clk) el = en;
    wire g = clk & el;
    always_ff @(posedge g) q <= a;
endmodule
module top (input fast, input slow, input e, input d, output q);
    logic x;
    always_ff @(posedge slow) x <= e;
    blk u (.clk(fast), .en(x), .a(d), .q(q));
endmodule
"""

_LATCH_BEFORE_SYNC = """\
module blk (input clk, input en, input a, output q);
    logic l, s1, s2;
    always_latch if (en) l = a;
    always_ff @(posedge clk) begin s1 <= l; s2 <= s1; end
    assign q = s2;
endmodule
module top (input fast, input slow, input d, input e, output q);
    logic x;
    always_ff @(posedge slow) x <= d;
    blk u (.clk(fast), .en(e), .a(x), .q(q));
endmodule
"""

_MEM_ASYNC_READ = """\
module blk (input clk, input [1:0] wa, input [7:0] wd, input we, input [1:0] ra,
            output logic [7:0] q);
    logic [7:0] mem [4];
    always_ff @(posedge clk) if (we) mem[wa] <= wd;
    assign q = mem[ra];
endmodule
module top (input fast, input slow, input [1:0] a, input [7:0] d, input w,
            input [1:0] b, output logic [7:0] q);
    logic [7:0] o;
    blk u (.clk(fast), .wa(a), .wd(d), .we(w), .ra(b), .q(o));
    always_ff @(posedge slow) q <= o;
endmodule
"""

_CLOCK_OUT_AS_DATA = """\
module blk (input clk, input d, output q);
    assign q = clk;
endmodule
module top (input fast, input slow, input d, output logic q);
    logic o;
    blk u (.clk(fast), .d(d), .q(o));
    always_ff @(posedge slow) q <= o;
endmodule
"""

_GATED_INSIDE = """\
module blk (input clk, input req, input [7:0] d, output logic [7:0] q);
    logic r1, r2;
    always_ff @(posedge clk) begin r1 <= req; r2 <= r1; if (r2) q <= d; end
endmodule
module top (input fast, input slow, input go, input [7:0] d, output [7:0] q);
    logic r;
    logic [7:0] x;
    always_ff @(posedge slow) begin r <= go; x <= d; end
    blk u (.clk(fast), .req(r), .d(x), .q(q));
endmodule
"""

# The same, with the acknowledge leaving the block and holding the payload.
_GATED_INSIDE_ACKED = """\
module blk (input clk, input req, input [7:0] d, output logic [7:0] q,
            output logic ack);
    logic r1, r2;
    always_ff @(posedge clk) begin r1 <= req; r2 <= r1; ack <= r2; if (r2) q <= d; end
endmodule
module top (input fast, input slow, input go, input [7:0] d, output [7:0] q);
    logic r, a1, a2, ack;
    logic [7:0] x;
    always_ff @(posedge slow) begin
        a1 <= ack; a2 <= a1;
        r <= go;
        if (a2) x <= d;
    end
    blk u (.clk(fast), .req(r), .d(x), .q(q), .ack(ack));
endmodule
"""


@needs_slang
@pytest.mark.parametrize(
    ("sv", "cell"),
    [(_CLOCK_MUX_SELECT_PIN, "$mux"), (_ICG_ENABLE_PIN, "$dlatch")],
    ids=["clock-mux-select", "latch-icg-enable"],
)
def test_clock_control_on_a_pin_is_cdc010(tmp_path: Path, sv: str, cell: str) -> None:
    """Item 5: the pin only steers a clock mux / gate inside the block.
    It seeds no data sink; CDC-010 is raised at the pin from the parent
    flop driving it."""
    sdc = _SDC + _typed("slow", "e") + _typed("fast", "d")
    reports = _views(tmp_path, sv, sdc)
    assert _rules(reports["flat"]) == Counter({"CDC-010": 1})
    assert _rules(reports["greybox"]) == Counter({"CDC-010": 1})
    (msg,) = _messages(reports["greybox"], "CDC-010")
    assert "inside abstracted block u" in msg and "in domain slow" in msg
    (flat_msg,) = _messages(reports["flat"], "CDC-010")
    assert f"({cell})" in flat_msg
    # A stub reads every non-clock pin as data (documented).
    assert _rules(reports["blackbox"]) == Counter({"CDC-001": 1})


@needs_slang
def test_latch_between_pin_and_sync_is_cdc017(tmp_path: Path) -> None:
    """Item 6: the crossing walk stops at a latch flat; at the boundary
    the pin seeds no sink and CDC-017 is raised from the latch fact."""
    sdc = _SDC + _typed("slow", "d") + _typed("fast", "e")
    reports = _views(tmp_path, _LATCH_BEFORE_SYNC, sdc)
    assert _rules(reports["flat"]) == Counter({"CDC-017": 1})
    assert _rules(reports["greybox"]) == Counter({"CDC-017": 1})
    (msg,) = _messages(reports["greybox"], "CDC-017")
    assert "inside abstracted block u, fed from its input u.a" in msg
    assert _rules(reports["blackbox"]) == Counter({"CDC-001": 1})


@needs_slang
def test_memory_async_read_through_a_block(tmp_path: Path) -> None:
    """Item 7. Documented limit here until rtl-buddy-cdc#411: the greybox
    now publishes the asynchronous read (a transit from the address pin
    to the data pin, and the memory's write clock), so the parent raises
    CDC-026 as flat; tests/test_abstraction_parity_411.py has the table.
    A stub cannot show the memory: CDC-004 at the address pin and the
    read data (documented)."""
    sdc = _SDC + _typed("fast", "a", "d", "w") + _typed("slow", "b")
    reports = _views(tmp_path, _MEM_ASYNC_READ, sdc)
    assert _rules(reports["flat"]) == Counter({"CDC-026": 1})
    assert _rules(reports["greybox"]) == Counter({"CDC-026": 1})
    assert _rules(reports["blackbox"]) == Counter({"CDC-004": 2})


@needs_slang
def test_clock_carried_out_of_a_block_used_as_data_is_cdc008(tmp_path: Path) -> None:
    """Item 8: the output carries the block's clock pin; it seeds no
    source, and the parent reads it as that clock. The unused data pin
    seeds no sink either (flat: nothing captures it)."""
    reports = _views(tmp_path, _CLOCK_OUT_AS_DATA, _SDC + _typed("slow", "d"))
    assert _rules(reports["flat"]) == Counter({"CDC-008": 1})
    assert _rules(reports["greybox"]) == Counter({"CDC-008": 1})
    (msg,) = _messages(reports["greybox"], "CDC-008")
    assert "u.q (clock pin u.clk)" in msg
    # A stub's output is data launched in its clock (documented).
    assert _rules(reports["blackbox"]) == Counter({"CDC-001": 2})


@needs_slang
def test_gating_inside_a_block_is_cdc012(tmp_path: Path) -> None:
    """Item 9: the bus into the block is captured under an enable
    synchronised inside it — gated, so CDC-012's question, not CDC-004's."""
    sdc = _SDC + _typed("slow", "go", "d")
    reports = _views(tmp_path, _GATED_INSIDE, sdc)
    assert _rules(reports["flat"]) == Counter({"CDC-012": 1})
    assert _rules(reports["greybox"]) == Counter({"CDC-012": 1})


@needs_slang
def test_gating_inside_a_block_with_an_acknowledge_is_clean(tmp_path: Path) -> None:
    sdc = _SDC + _typed("slow", "go", "d")
    reports = _views(tmp_path, _GATED_INSIDE_ACKED, sdc)
    assert _rules(reports["flat"])["CDC-012"] == 0
    assert _rules(reports["greybox"]) == _rules(reports["flat"])


# --- item 10: a greybox decline is per instance -------------------------------

_TWO_TAPS = """\
module tap (input smp_clk, input d, output q);
    logic a, b;
    always_ff @(posedge smp_clk) begin a <= d; b <= a; end
    assign q = b;
endmodule
module top (input fast, input ck, input d, output q0, output q1);
    tap u1 (.smp_clk(fast), .d(d), .q(q0));
    tap u2 (.smp_clk(ck), .d(d), .q(q1));
endmodule
"""


@needs_slang
def test_greybox_decline_is_per_instance(tmp_path: Path) -> None:
    """``u2`` is clocked from an undeclared port: its context declines
    (CDC-BBX, loud, as the flat run's CDC-021 is). ``u1`` is clocked from
    a declared clock: its context summarises, so it is abstracted and
    gets no CDC-BBX."""
    sdc = "create_clock -name fast -period 4 [get_ports fast]\n" + _typed("fast", "d")
    reports = _views(tmp_path, _TWO_TAPS, sdc, module="tap")
    assert _rules(reports["flat"]) == Counter({"CDC-021": 1})
    grey = reports["greybox"]
    assert _rules(grey) == Counter({"CDC-021": 1, "CDC-BBX": 1})
    (bbx,) = [v for v in grey["violations"] if v["rule_id"] == "CDC-BBX"]
    assert bbx["cell_name"] == "u2"


# --- clean counterparts at the pin --------------------------------------------


@needs_slang
def test_synchronised_clock_select_on_a_pin_is_clean(tmp_path: Path) -> None:
    """The select pin is driven from the cell's own clock domain."""
    sv = _CLOCK_MUX_SELECT_PIN.replace(
        "always_ff @(posedge slow) x <= e;",
        "logic x1;\n    always_ff @(posedge fast) begin x1 <= e; x <= x1; end",
    )
    sdc = _SDC + _typed("fast", "e", "d")
    reports = _views(tmp_path, sv, sdc)
    for view in ("flat", "greybox"):
        assert _rules(reports[view]) == Counter(), view


@needs_slang
def test_same_domain_latch_behind_a_pin_is_clean(tmp_path: Path) -> None:
    sv = _LATCH_BEFORE_SYNC.replace("posedge slow", "posedge fast")
    sdc = _SDC + _typed("fast", "d", "e")
    reports = _views(tmp_path, sv, sdc)
    for view in ("flat", "greybox"):
        assert _rules(reports[view])["CDC-017"] == 0, view


@needs_slang
def test_gray_tagged_payload_behind_a_block_output_is_exempt(tmp_path: Path) -> None:
    sv = _HOLD_BEHIND_OUTPUT.replace(
        "output logic [7:0] data);", "(* cdc_gray *) output logic [7:0] data);"
    ).replace(
        "always_ff @(posedge clk) begin req <= go; data <= d; end",
        "(* cdc_gray *) logic [7:0] g;\n"
        "    always_ff @(posedge clk) begin req <= go; g <= d; end\n"
        "    assign data = g;",
    )
    sdc = _SDC + _typed("slow", "go", "d")
    reports = _views(tmp_path, sv, sdc)
    for view in ("flat", "greybox"):
        assert _rules(reports[view])["CDC-012"] == 0, view


@needs_slang
def test_ungated_bus_from_a_block_output_stays_cdc004(tmp_path: Path) -> None:
    sv = """\
module blk (input clk, input [7:0] d, output logic [7:0] q);
    always_ff @(posedge clk) q <= d;
endmodule
module top (input fast, input slow, input [7:0] d, output logic [7:0] o);
    logic [7:0] x;
    blk u (.clk(slow), .d(d), .q(x));
    always_ff @(posedge fast) o <= x;
endmodule
"""
    reports = _views(tmp_path, sv, _SDC + _typed("slow", "d"))
    for view in ("flat", "greybox", "blackbox"):
        assert _rules(reports[view]) == Counter({"CDC-004": 1}), view


def test_clock_network_events_needs_an_sdc() -> None:
    from rtl_buddy_cdc.netlist import Module
    from rtl_buddy_cdc.rules import _build_context

    module = Module(name="m", ports={}, cells={}, netnames={})
    assert _build_context(module, None).clock_network_events == ([], {})
