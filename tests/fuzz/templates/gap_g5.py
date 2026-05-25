"""Regression sentinel: G-5 handshake-related message tag (rtl-buddy-cdc#214).

A gated multi-bit crossing (CDC-012) paired with a reverse-direction
crossing that misses its sync chain (CDC-001). The two findings
describe the same incomplete-handshake protocol — today the user
sees them as unrelated, G-5 adds a "[handshake-related]" tag to
the CDC-001 message linking it to its CDC-012 partner.

Asserts both findings fire (today), and that the CDC-001 message
carries the new tag once G-5's reporter refinement lands.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic       src_clk,
    input  logic       dst_clk,
    input  logic [7:0] d_in,
    input  logic       req_in,
    input  logic       status_in,
    output logic [7:0] data_dst,
    output logic       status_q
);
    logic [7:0] data_q;
    logic       req_q;
    logic       status_src_q;
    always_ff @(posedge src_clk) begin
        data_q       <= d_in;
        req_q        <= req_in;
        status_src_q <= status_in;
    end

    // Sync the req onto dst_clk (correct).
    logic req_sync_m, req_sync_q;
    always_ff @(posedge dst_clk) begin
        req_sync_m <= req_q;
        req_sync_q <= req_sync_m;
    end

    // Gated bus crossing — fires CDC-012 (no synced-back ack
    // anywhere between the two domains; both other crossings below
    // are src→dst).
    logic [7:0] data_dst_q;
    always_ff @(posedge dst_clk)
        if (req_sync_q) data_dst_q <= data_q;
    assign data_dst = data_dst_q;

    // Separate src→dst status signal that the user wired straight
    // into a dst-clock flop without a 2FF sync chain. Fires CDC-001
    // on the *same async domain pair* as the gated bus above.
    // G-5's reporter tag should link these findings.
    always_ff @(posedge dst_clk) status_q <= status_src_q;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports {d_in req_in}]
"""


class GapG5HandshakeAckMissing:
    """Regression sentinel: CDC-001 / CDC-012 pair on incomplete handshake."""

    name = "gap_g5_handshake_ack_missing"

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
            # Both rules fire today — the G-5 refinement doesn't change
            # the firing set, it only changes the CDC-001 message text.
            # Sentinel pins that both findings remain present after
            # G-5 lands (regression net against a future change that
            # accidentally suppresses one).
            expected=(
                ExpectedFinding("CDC-001", Op.GE, 1),
                ExpectedFinding("CDC-012", Op.EQ, 1),
            ),
            # opt_dff coerces Yosys to fold the if-gated assignment
            # into $dffe (matching the canonical CDC-004 shape-1
            # detector); without it CDC-012's gated-bus precondition
            # doesn't trigger.
            extra_yosys_passes="opt_dff;",
        )
