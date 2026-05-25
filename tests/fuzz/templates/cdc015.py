"""CDC-015 — sync chain asynchronously reset from a foreign clock domain.

A 2FF synchroniser in ``dst_clk`` whose resolving flops have their
``ARST`` driven by a reset *registered in ``src_clk``*. The chain
cannot reach steady state on its own clock — every foreign-domain
reset deassertion races the dst-clk edge, restoring the flops at an
arbitrary point in pointer-value space.

The data-path CDC rules (CDC-001 / CDC-002) see a structurally valid
depth-2 chain and stay silent. The bug is in the reset path. RDC-001
also fires by construction on the same physical structure with
reset-tree framing — the two findings coexist, so we don't forbid
RDC-001 here.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic global_rst_n,
    input  logic async_signal,
    output logic dst_q
);
    logic src_rst_q;
    always_ff @(posedge src_clk or negedge global_rst_n)
        if (!global_rst_n) src_rst_q <= 1'b0;
        else               src_rst_q <= 1'b1;

    logic src_q;
    always_ff @(posedge src_clk or negedge global_rst_n)
        if (!global_rst_n) src_q <= 1'b0;
        else               src_q <= async_signal;

    logic sync_1;
    always_ff @(posedge dst_clk or negedge src_rst_q)
        if (!src_rst_q) sync_1 <= 1'b0;
        else            sync_1 <= src_q;

    logic sync_2;
    always_ff @(posedge dst_clk or negedge src_rst_q)
        if (!src_rst_q) sync_2 <= 1'b0;
        else            sync_2 <= sync_1;

    assign dst_q = sync_2;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports async_signal]
"""


class SyncChainForeignReset:
    """2FF sync chain whose ARST comes from a foreign-domain flop."""

    name = "cdc015_sync_chain_foreign_reset"

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
            expected=(ExpectedFinding("CDC-015", Op.GE, 1),),
            forbidden=(
                ExpectedFinding("CDC-001", Op.ZERO),
                ExpectedFinding("CDC-002", Op.ZERO),
            ),
        )
