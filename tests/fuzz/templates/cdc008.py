"""CDC-008 — clock signal used as data.

A clock-network bit (here, a clock input that's also wired to a
flop's D pin) violates clock-network purity.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic main_clk,
    input  logic snoop_clk,
    output logic q_out
);
    // Flop in main_clk's domain that samples snoop_clk as data.
    // snoop_clk is part of the clock network (drives flops
    // elsewhere — make sure of that with the second flop below).
    logic snoop_q;
    always_ff @(posedge main_clk) snoop_q <= snoop_clk;

    // Force snoop_clk to also be a real clock so the analyzer
    // treats it as such.
    logic dummy;
    always_ff @(posedge snoop_clk) dummy <= 1'b1;

    assign q_out = snoop_q | dummy;
endmodule
"""

_SDC = """\
create_clock -name main_clk  -period 10.0 [get_ports main_clk]
create_clock -name snoop_clk -period 7.5  [get_ports snoop_clk]
set_clock_groups -asynchronous -group {main_clk} -group {snoop_clk}
"""


class ClockAsData:
    name = "cdc008_clock_as_data"

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
            expected=(ExpectedFinding("CDC-008", Op.GE, 1),),
        )
