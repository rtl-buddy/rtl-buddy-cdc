"""CDC-011 — unconstrained primary input.

Top-level port with no ``set_input_delay -clock`` typing in the SDC
that physically reaches a flop's D pin.
"""

from __future__ import annotations

from collections.abc import Iterator

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
create_clock -name clk -period 10.0 [get_ports clk]
"""


class UnconstrainedInput:
    name = "cdc011_unconstrained_input"

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
            expected=(ExpectedFinding("CDC-011", Op.GE, 1),),
        )
