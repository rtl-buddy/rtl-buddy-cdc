"""CDC-012 — functional data-hold violation on a gated bus.

A 1-bit ``src_req`` is synchronised into ``dst_clk`` and gates the
destination's bus capture, so the crossing looks structurally
correct (CDC-004 stays silent — the bus is gated by a synced
control). The functional bug: ``src_payload`` keeps changing every
``src_clk`` cycle while the request travels through the sync chain,
so the dst sample can observe a payload from a *different*
transaction than the one that armed the request.

The textbook fix is a req/ack handshake — hold the payload stable
from req-assert through ack-return — captured in the
``good_functional_datahold_handshake`` fixture.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic        src_clk,
    input  logic        dst_clk,
    input  logic        rst_n,
    input  logic        src_req,
    input  logic [7:0]  src_data,
    output logic [7:0]  dst_data
);
    logic [7:0] src_payload;
    logic       src_req_q;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            src_payload <= 8'h00;
            src_req_q   <= 1'b0;
        end else begin
            src_payload <= src_data;
            src_req_q   <= src_req;
        end
    end

    logic req_meta, req_sync;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            req_meta <= 1'b0;
            req_sync <= 1'b0;
        end else begin
            req_meta <= src_req_q;
            req_sync <= req_meta;
        end
    end

    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n)        dst_data <= 8'h00;
        else if (req_sync) dst_data <= src_payload;
    end
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 4.0  [get_ports src_clk]
create_clock -name dst_clk -period 10.0 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 0.5 [get_ports src_req]
set_input_delay -clock src_clk 0.5 [get_ports src_data[*]]
"""


class FunctionalDataHoldEnable:
    """Synced gate + freely changing payload = data-hold violation."""

    name = "cdc012_functional_datahold_enable"

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
            expected=(ExpectedFinding("CDC-012", Op.GE, 1),),
        )
