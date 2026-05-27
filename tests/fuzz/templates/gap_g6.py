"""Regression sentinel: CDC-020 sliced-bus reconvergence (rtl-buddy-cdc#210).

A genuinely-multi-bit src-domain flop (``logic [3:0]``) whose bus
is sliced into 1-bit lanes before crossing. After Yosys flattens
the slicing assignments, ``find_crossings`` emits one Crossing per
(src_flop, dst_flop) pair with ``width = number_of_bits_landing_at
_that_specific_dst`` — so each per-lane crossing has width=1 and
CDC-004's ``width <= 1`` skip drops them all.

Sibling of CDC-019: same per-lane-independent-sync hazard, but the
source is a true multi-bit register rather than a shared comb
decoder.

Stage-3 Layer A (rtl-buddy-cdc#221): the SDC clock periods sweep
across :data:`TWO_CLOCK_PERIODS` so CDC-020 crosses the ≥10× bar
without changing the structural shape — purely an SDC-only
multiplier per the issue's Layer-A guidance.

Asserts CDC-020 fires once per case on the (src_flop, dst_clock)
group. Before the rule existed, ``run_all`` produced zero findings —
CDC-004 silent because each lane is width=1, and CDC-019 silent
because the driver is a flop, not a comb cell.
"""

from __future__ import annotations

from collections.abc import Iterator

from ._sweep import TWO_CLOCK_PERIODS, case_suffix
from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic [3:0] d_in,
    output logic [3:0] data_dst
);
    // True 4-bit register — but the bus is split into 1-bit lanes
    // for the per-lane sync chains below.
    logic [3:0] data;
    always_ff @(posedge src_clk) data <= d_in;

    logic d0, d1, d2, d3;
    assign d0 = data[0];
    assign d1 = data[1];
    assign d2 = data[2];
    assign d3 = data[3];

    logic d0_m, d0_s, d1_m, d1_s, d2_m, d2_s, d3_m, d3_s;
    always_ff @(posedge dst_clk) begin
        d0_m <= d0; d0_s <= d0_m;
        d1_m <= d1; d1_s <= d1_m;
        d2_m <= d2; d2_s <= d2_m;
        d3_m <= d3; d3_s <= d3_m;
    end

    // Recombines at the destination — sees transient incoherent
    // values (e.g. 4'b0010 mid-transition 4'b0011 → 4'b0100).
    assign data_dst = {{d3_s, d2_s, d1_s, d0_s}};
endmodule
"""

_SDC_TEMPLATE = """\
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""


class GapG6SlicedBusReconvergence:
    """Regression sentinel: multi-bit src sliced into per-lane syncs fires CDC-020."""

    name = "gap_g6_sliced_bus_reconvergence"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for src_period, dst_period in TWO_CLOCK_PERIODS:
            top = f"fuzz_{cls.name}_{case_suffix(src_period, dst_period)}"
            sv = _TEMPLATE.format(top=top)
            sdc = _SDC_TEMPLATE.format(src_period=src_period, dst_period=dst_period)
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=sdc,
                top=top,
                params={"src_period": src_period, "dst_period": dst_period},
                expected=(ExpectedFinding("CDC-020", Op.EQ, 1),),
                # CDC-004 misses because every crossing is width=1 after
                # slicing; CDC-019 misses because the driver is a flop,
                # not a comb cell. Pin both stay silent.
                forbidden=(
                    ExpectedFinding("CDC-004", Op.ZERO),
                    ExpectedFinding("CDC-019", Op.ZERO),
                ),
            )
