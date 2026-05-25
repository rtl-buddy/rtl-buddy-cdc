"""RDC-002 — reset polarity mismatch on a direct flop→flop reset.

Producer flop ``gated_rst`` is ``$adff`` with active-low reset
(``ARST_POLARITY=0``, ``ARST_VALUE=0``) — so during system reset its
Q sits at 0. Consumer flop ``q_out`` takes that Q as its async reset
on a *posedge*, inferring ``$adff`` with ``ARST_POLARITY=1``. The
polarities disagree: during a system reset the producer drives 0
into the consumer's ARST, which the consumer reads as "reset
deasserted" — the consumer never enters reset on a system reset
event.

Single-clock design, so no CDC rules fire. RDC-002 owns the finding.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic clk,
    input  logic raw_rst_n,
    output logic q_out
);
    logic gated_rst;
    always_ff @(posedge clk or negedge raw_rst_n)
        if (!raw_rst_n) gated_rst <= 1'b0;
        else            gated_rst <= 1'b1;

    always_ff @(posedge clk or posedge gated_rst)
        if (gated_rst) q_out <= 1'b0;
        else           q_out <= 1'b1;
endmodule
"""

_SDC = """\
create_clock -name clk -period 10.0 [get_ports clk]
set_input_delay -clock clk 1.0 [get_ports raw_rst_n]
"""


class ResetPolarityMismatch:
    """Active-low producer → active-high consumer ARST."""

    name = "rdc002_polarity_mismatch"

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
            expected=(ExpectedFinding("RDC-002", Op.GE, 1),),
        )
