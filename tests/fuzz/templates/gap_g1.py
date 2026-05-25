"""Regression sentinel: CDC-017 latch-in-CDC-path (rtl-buddy-cdc#195).

A designer who builds a "synchroniser" out of a transparent latch
followed by a flop. The latch is gated by ``dst_clk``'s level so
it's transparent half the time — a metastable src_q passes straight
through to the dst-domain flop during the transparent window.

Asserts CDC-017 (added in rtl-buddy-cdc#195) fires on this shape.
Before the rule existed, ``find_crossings`` returned zero crossings
and ``run_all`` produced zero findings. The companion sim DUT
(``tests/sim/dut_latch_sync.sv``) still produces 83 errors / 4995
cycles at 80% injection — confirming the design genuinely fails
functionally.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic latch_en,
    input  logic d_in,
    output logic q_out
);
    logic src_q;
    always_ff @(posedge src_clk) src_q <= d_in;

    // The shape CDC-017 catches: transparent latch where a sync
    // first stage should be.
    logic latch_q;
    always_latch
        if (latch_en) latch_q = src_q;

    logic sync_q;
    always_ff @(posedge dst_clk) sync_q <= latch_q;

    assign q_out = sync_q;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
set_input_delay -clock dst_clk 1.0 [get_ports latch_en]
"""


class GapG1LatchInSyncChain:
    """Regression sentinel: latch-as-sync fires CDC-017 exactly once."""

    name = "gap_g1_latch_in_sync_chain"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        top = f"fuzz_{cls.name}"
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=_TEMPLATE.format(top=top),
            sdc=_SDC,
            top=top,
            params={},
            expected=(ExpectedFinding("CDC-017", Op.EQ, 1),),
            # CDC-001 doesn't see the crossing (it goes through the
            # latch and find_crossings doesn't traverse latches);
            # pin that for explicitness so a future rewiring of
            # find_crossings to walk through latches doesn't
            # silently double-report.
            forbidden=(ExpectedFinding("CDC-001", Op.ZERO),),
        )
