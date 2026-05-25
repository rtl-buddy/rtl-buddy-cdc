"""RDC-005 — multiple reset sources converging on a flop without mux.

Two top-level reset ports ORed together drive a flop's ARST. Both
resets are simultaneously active and the user has no explicit
control over which one dominates.
"""

from __future__ import annotations

from collections.abc import Iterator

from ._sweep import ONE_CLOCK_PERIODS, case_suffix
from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic clk,
    input  logic global_rst_n,
    input  logic local_rst_n,
    input  logic d_in,
    output logic q_out
);
    // AND of two reset ports — both must be high to deassert.
    wire combined_rst_n = global_rst_n & local_rst_n;

    logic q;
    always_ff @(posedge clk or negedge combined_rst_n)
        if (!combined_rst_n) q <= 1'b0;
        else                 q <= d_in;
    assign q_out = q;
endmodule
"""

_SDC = """\
create_clock -name clk -period {clk_period} [get_ports clk]
set_input_delay -clock clk 1.0 [get_ports d_in]
"""


class MultiSourceReset:
    name = "rdc005_multi_source_reset"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for clk_period in ONE_CLOCK_PERIODS:
            top = f"fuzz_{cls.name}_{case_suffix(clk_period)}"
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=_TEMPLATE.format(top=top),
                sdc=_SDC.format(clk_period=clk_period),
                top=top,
                params={"clk_period": clk_period},
                expected=(ExpectedFinding("RDC-005", Op.GE, 1),),
            )
