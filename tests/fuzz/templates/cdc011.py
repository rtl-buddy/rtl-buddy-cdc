"""CDC-011 — unconstrained primary input.

Top-level port with no ``set_input_delay -clock`` typing in the SDC
that physically reaches a flop's D pin.
"""

from __future__ import annotations

from collections.abc import Iterator

from ._sweep import ONE_CLOCK_PERIODS, case_suffix
from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic clk,
    input  logic untyped_in,
    output logic q_out
);
    logic q;
    always_ff @(posedge clk) q <= untyped_in;
    assign q_out = q;
endmodule
"""

# Deliberately no set_input_delay on untyped_in.
_SDC = """\
create_clock -name clk -period {clk_period} [get_ports clk]
"""


class UnconstrainedInput:
    name = "cdc011_unconstrained_input"

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
                expected=(ExpectedFinding("CDC-011", Op.GE, 1),),
            )
