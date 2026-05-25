"""Regression sentinel: RDC-008 unsynced primary-reset-port (rtl-buddy-cdc#216).

A top-level reset port directly drives a flop's ARST in a clock
domain with no reset-synchroniser chain. RDC-001 stays silent
because the reset source is a port (not a foreign-domain flop's Q),
but the recovery/removal hazard is the same: deassertion edge is
unsynchronised to the consumer clock.

Asserts RDC-008 fires once. Before the rule existed, ``run_all``
produced zero findings on this shape.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic clk_a,
    input  logic clk_b,
    input  logic raw_rst_n,
    input  logic d_in_a,
    input  logic d_in_b,
    output logic q_a,
    output logic q_b
);
    // 2FF reset-sync chain in clk_a — the user clearly knows the
    // raw_rst_n port needs synchronisation.
    logic rst_a_meta, rst_a_n;
    always_ff @(posedge clk_a or negedge raw_rst_n)
        if (!raw_rst_n) rst_a_meta <= 1'b0;
        else            rst_a_meta <= 1'b1;
    always_ff @(posedge clk_a or negedge raw_rst_n)
        if (!raw_rst_n) rst_a_n    <= 1'b0;
        else            rst_a_n    <= rst_a_meta;

    // clk_a consumers route through the chain — silent.
    logic qa_q;
    always_ff @(posedge clk_a or negedge rst_a_n)
        if (!rst_a_n) qa_q <= 1'b0;
        else          qa_q <= d_in_a;
    assign q_a = qa_q;

    // BAD: clk_b flops use raw_rst_n directly, no chain in clk_b.
    // The user built infrastructure in clk_a (intent is clear) but
    // missed it in clk_b. ≥2 unsynced consumers triggers RDC-008
    // (the rule's heuristic distinguishes "real distribution
    // pattern missing its sync" from "single-flop shortcut").
    logic qb_q0, qb_q1;
    always_ff @(posedge clk_b or negedge raw_rst_n) begin
        if (!raw_rst_n) begin
            qb_q0 <= 1'b0;
            qb_q1 <= 1'b0;
        end else begin
            qb_q0 <= d_in_b;
            qb_q1 <= qb_q0;
        end
    end
    assign q_b = qb_q1;
endmodule
"""

_SDC = """\
create_clock -name clk_a -period 10.0 [get_ports clk_a]
create_clock -name clk_b -period 7.5  [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_input_delay -clock clk_a 1.0 [get_ports d_in_a]
set_input_delay -clock clk_b 1.0 [get_ports d_in_b]
"""


class GapG8PrimaryResetUnsynced:
    """Regression sentinel: port-sourced ARST without sync chain fires RDC-008."""

    name = "gap_g8_primary_reset_unsynced"

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
            # Port has a chain in clk_a, missing it in clk_b — one
            # (port, clk_b) finding under the asymmetric-intent gate.
            expected=(ExpectedFinding("RDC-008", Op.EQ, 1),),
            # RDC-001 deliberately stays silent on port-sourced
            # resets (the source isn't a foreign-domain flop). Pin
            # that explicitly so a future broadening of RDC-001
            # doesn't silently double-report.
            forbidden=(ExpectedFinding("RDC-001", Op.ZERO),),
        )
