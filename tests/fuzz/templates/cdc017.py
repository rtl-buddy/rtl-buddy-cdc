"""CDC-017 — transparent latch in CDC path.

A designer who builds a "synchroniser" out of a transparent latch
followed by a flop. The latch is gated by ``dst_clk``'s level so
it's transparent half the time — a metastable src_q passes straight
through to the dst-domain flop during the transparent window.

This is the coverage-steering counterpart to the ``gap_g1`` sentinel
(rtl-buddy-cdc#195). Sweeping the period pair gives the rule corpus
exposure across timing regimes; the sentinel pins exact-once firing
for its one canonical case.
"""

from __future__ import annotations

from collections.abc import Iterator

from ._sweep import TWO_CLOCK_PERIODS, case_suffix
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

    logic latch_q;
    always_latch
        if (latch_en) latch_q = src_q;

    logic sync_q;
    always_ff @(posedge dst_clk) sync_q <= latch_q;

    assign q_out = sync_q;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
set_input_delay -clock dst_clk 1.0 [get_ports latch_en]
"""


class LatchInCdcPath:
    """Transparent latch standing in for a synchroniser's first stage."""

    name = "cdc017_latch_in_cdc_path"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for src_period, dst_period in TWO_CLOCK_PERIODS:
            top = f"fuzz_{cls.name}_{case_suffix(src_period, dst_period)}"
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=_TEMPLATE.format(top=top),
                sdc=_SDC.format(src_period=src_period, dst_period=dst_period),
                top=top,
                params={"src_period": src_period, "dst_period": dst_period},
                expected=(ExpectedFinding("CDC-017", Op.GE, 1),),
                forbidden=(ExpectedFinding("CDC-001", Op.ZERO),),
            )
