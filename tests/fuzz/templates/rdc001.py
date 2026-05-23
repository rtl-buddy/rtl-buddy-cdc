"""RDC-001 — async reset crossing without reset synchroniser.

A flop in the ``src_clk`` domain emits an async reset signal that
directly drives the ``ARST`` of flops in the ``dst_clk`` domain.
Reset assertion is fine (logic enters reset immediately), but the
*deassertion* edge is asynchronous to dst_clk — recovery/removal
timing is violated and the dst-domain flops can come out of reset
in different cycles, leaving state machines in illegal states.

RDC-001 must fire — one finding per (src_flop, src_clk, dst_clk)
group (the reset-tree grouping in ``rules.py``).
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic global_rst_n,
    input  logic local_rst_req,
    input  logic d_in,
    output logic q_out
);
    // Source-domain reset generator. Falls under src_clk's domain
    // (its only clock); the analyzer must see its Q as
    // src_clk-async-to-dst_clk.
    logic local_rst_n;
    always_ff @(posedge src_clk or negedge global_rst_n)
        if (!global_rst_n) local_rst_n <= 1'b0;
        else               local_rst_n <= ~local_rst_req;

    // Destination-domain flop whose async reset is driven by the
    // foreign-domain reset signal. RDC-001 territory.
    logic dst_q;
    always_ff @(posedge dst_clk or negedge local_rst_n)
        if (!local_rst_n) dst_q <= 1'b0;
        else              dst_q <= d_in;

    assign q_out = dst_q;
endmodule
"""

_SDC_TEMPLATE = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports local_rst_req]
set_input_delay -clock dst_clk 1.0 [get_ports d_in]
"""


class AsyncResetCrossing:
    """Foreign-domain async reset on a flop's ARST pin."""

    name = "rdc001_async_reset_crossing"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        top = f"fuzz_{cls.name}"
        sv = _TEMPLATE.format(top=top)
        yield RenderedCase(
            template_name=cls.name,
            case_id=top,
            sv=sv,
            sdc=_SDC_TEMPLATE,
            top=top,
            params={},
            expected=(ExpectedFinding("RDC-001", Op.GE, 1),),
        )
