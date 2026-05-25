"""Regression sentinel: pulse-sync XOR-tail suppression (rtl-buddy-cdc#196).

Canonical pulse-synchroniser idiom (toggle in src + 2FF + XOR-tail
in dst). Before rtl-buddy-cdc#196 landed, CDC-013 false-fired here
because its classifier matched the source-side toggle pattern
``D = en ? ~Q : Q`` without recognising the dst-side XOR-tail.
The textbook correct idiom triggered a finding — users had to
waive or refactor.

Now asserts CDC-013 stays silent. The XOR-tail recognition
(``_has_xor_tail_pulse_recovery``) confirms the chain tail's Q is
both fed into a follow-on flop and one input of an XOR cell whose
other input is that follow-on flop's Q — the canonical structural
shape.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic en,
    output logic pulse_out
);
    // Source-side toggle flop. CDC-013's classifier matches the
    // D-pin shape `D = en ? ~Q : Q` here.
    logic toggle_q;
    always_ff @(posedge src_clk) toggle_q <= en ? ~toggle_q : toggle_q;

    // 2FF synchroniser on the toggle value into the dst domain.
    logic sync_meta, sync_q;
    always_ff @(posedge dst_clk) sync_meta <= toggle_q;
    always_ff @(posedge dst_clk) sync_q    <= sync_meta;

    // XOR-tail: reconstructs the pulse in dst domain. The
    // structural recogniser pairs (sync_q, sync_q_d) as inputs of
    // the same XOR and suppresses CDC-013.
    logic sync_q_d;
    always_ff @(posedge dst_clk) sync_q_d <= sync_q;
    assign pulse_out = sync_q ^ sync_q_d;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 5.0  [get_ports src_clk]
create_clock -name dst_clk -period 12.0 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports en]
"""


class GapG3PulseSyncFalsePositive:
    """Regression sentinel: canonical pulse-sync produces no CDC-013."""

    name = "gap_g3_pulse_sync_false_positive"

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
            expected=(),
            forbidden=(ExpectedFinding("CDC-013", Op.ZERO),),
        )
