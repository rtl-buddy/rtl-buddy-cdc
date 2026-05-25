"""RDC-006 — derived async reset feeding a flop's ARST unsynchronised.

A mux selects between two reset ports; the selected reset goes
directly to a flop's async clear pin. The consumer clock has no
local reset synchroniser, so the deassertion edge of whichever
reset is currently selected is asynchronous to ``clk`` — recovery/
removal timing on ``q_out`` is violated and the flop can come out
of reset partway through a clock cycle.

Single-clock design — no CDC findings. RDC-006 owns it.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic clk,
    input  logic global_rst_n,
    input  logic block_rst_n,
    input  logic use_block_rst,
    input  logic d_in,
    output logic q_out
);
    logic selected_rst_n;
    assign selected_rst_n = use_block_rst ? block_rst_n : global_rst_n;

    always_ff @(posedge clk or negedge selected_rst_n)
        if (!selected_rst_n) q_out <= 1'b0;
        else                 q_out <= d_in;
endmodule
"""

_SDC = """\
create_clock -name clk -period 10.0 [get_ports clk]
set_input_delay -clock clk 0.5 [get_ports d_in]
"""


class DerivedAsyncResetUnsync:
    """Muxed reset directly driving ARST with no dst-side sync."""

    name = "rdc006_derived_async_reset_unsync"

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
            expected=(ExpectedFinding("RDC-006", Op.GE, 1),),
        )
