"""CDC-004 — uncoded multi-bit bus crossing.

Source-domain multi-bit flop directly feeds a dst-domain multi-bit
register with no gating handshake or Gray encoding. CDC-004 must
fire (one finding per crossing).

Sweeps the bus width — the rule's behaviour should be invariant
above width 1.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic                  src_clk,
    input  logic                  dst_clk,
    input  logic                  rst_n,
    input  logic [{width_m1}:0]   d_in,
    output logic [{width_m1}:0]   q_out
);
    logic [{width_m1}:0] src_q;
    always_ff @(posedge src_clk or negedge rst_n)
        if (!rst_n) src_q <= '0;
        else        src_q <= d_in;

    logic [{width_m1}:0] dst_q;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) dst_q <= '0;
        else        dst_q <= src_q;

    assign q_out = dst_q;
endmodule
"""

_SDC_TEMPLATE = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""


class UncodedBus:
    """Plain multi-bit crossing — CDC-004 territory."""

    name = "cdc004_uncoded_bus"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        widths = [2, 4, 8]
        for width in widths:
            params = {"width": width}
            top = f"fuzz_{cls.name}_w{width}"
            sv = _TEMPLATE.format(top=top, width_m1=width - 1)
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=_SDC_TEMPLATE,
                top=top,
                params=params,
                expected=(ExpectedFinding("CDC-004", Op.GE, 1),),
                # CDC-001 also fires on every bit of an unsynced
                # crossing (the rule sees flop→flop crossings without
                # caring about width). That's expected and not a
                # claim we need to constrain here.
            )
