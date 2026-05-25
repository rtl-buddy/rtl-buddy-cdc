"""RDC-004 — async reset driven by combinational logic.

A flop's ARST is the output of a comb gate whose backward fanin
includes a flop. Glitch hazard on the reset pin.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic clk,
    input  logic rst_n,
    input  logic enable,
    input  logic d_in,
    output logic q_out
);
    logic enable_q;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) enable_q <= 1'b0;
        else        enable_q <= enable;

    // Combinational reset: AND of rst_n and ~enable_q. Output can
    // glitch when enable_q transitions.
    wire combined_rst_n = rst_n & ~enable_q;

    logic q;
    always_ff @(posedge clk or negedge combined_rst_n)
        if (!combined_rst_n) q <= 1'b0;
        else                 q <= d_in;
    assign q_out = q;
endmodule
"""

_SDC = """\
create_clock -name clk -period 10.0 [get_ports clk]
set_input_delay -clock clk 1.0 [get_ports enable]
set_input_delay -clock clk 1.0 [get_ports d_in]
"""


class CombDrivenReset:
    name = "rdc004_comb_driven_reset"

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
            expected=(ExpectedFinding("RDC-004", Op.GE, 1),),
        )
