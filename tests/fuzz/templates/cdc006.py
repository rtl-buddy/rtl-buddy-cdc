"""CDC-006 — glitchy combinational source.

Unregistered top-level port feeds directly into a sync stage in the
dst domain. The port can carry combinational glitches that the sync
stage samples — even with a 2FF chain downstream, the source-side
glitch is the failure.
"""

from __future__ import annotations

from collections.abc import Iterator

from ._sweep import TWO_CLOCK_PERIODS, case_suffix
from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic a,
    input  logic b,
    output logic q_out
);
    // Source: combinational expression on top-level inputs — no
    // registering flop, port goes straight into the sync chain.
    wire comb_in = a & b;

    logic sync_meta, sync_q;
    always_ff @(posedge dst_clk) sync_meta <= comb_in;
    always_ff @(posedge dst_clk) sync_q    <= sync_meta;

    // src_clk needs at least one consumer so the analyzer treats
    // it as a real clock domain.
    logic src_dummy;
    always_ff @(posedge src_clk) src_dummy <= 1'b0;

    assign q_out = sync_q | src_dummy;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 1.0 [get_ports a]
set_input_delay -clock src_clk 1.0 [get_ports b]
"""


class CombSource:
    name = "cdc006_comb_source"

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
                expected=(ExpectedFinding("CDC-006", Op.GE, 1),),
            )
