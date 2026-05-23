"""CDC-002 — sync chain shorter than required depth.

Two-flop chain in the destination domain, but the test runs with
``--sync-depth >= 3`` so CDC-002 fires. CDC-001 stays silent (chain
depth IS >= 2) — proves the rules are independently exercised.

The differential runner threads ``required_depth`` through
``run_all_rules``; templates can declare their needed depth via
``params["required_depth"]``.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import product

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);
    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n)
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;

    logic sync_meta;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= src_q;

    logic sync_q;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;

    assign q_out = sync_q;
endmodule
"""

_SDC_TEMPLATE = """\
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""


class ShortChain:
    """2-stage chain that fails CDC-002 once required depth ≥ 3."""

    name = "cdc002_short_chain"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        depths = [3, 4]
        period_pairs = [(10.0, 7.5)]
        for required_depth, (src_period, dst_period) in product(depths, period_pairs):
            params = {
                "required_depth": required_depth,
                "src_period": src_period,
                "dst_period": dst_period,
            }
            top = f"fuzz_{cls.name}_depth{required_depth}"
            sv = _TEMPLATE.format(top=top)
            sdc = _SDC_TEMPLATE.format(src_period=src_period, dst_period=dst_period)
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=sdc,
                top=top,
                params=params,
                expected=(ExpectedFinding("CDC-002", Op.EQ, 1),),
                forbidden=(
                    ExpectedFinding("CDC-001", Op.ZERO),
                    ExpectedFinding("CDC-003", Op.ZERO),
                ),
            )
