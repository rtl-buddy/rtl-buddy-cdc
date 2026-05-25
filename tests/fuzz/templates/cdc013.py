"""CDC-013 — toggle synchroniser without XOR-tail pulse recovery.

Source-side toggle flop encodes events as level changes
(``D = en ? ~Q : Q``); the destination 2FF chain hands its tail's Q
to the consumer directly, without reconstructing the pulse via
``Q ^ Q_delayed``. The output therefore tracks the toggle level
instead of emitting one pulse per source event — closely-spaced
events that toggle twice between two destination samples are lost.

This template pins the *genuine* failure mode. The companion
``gap_g3`` sentinel (added in rtl-buddy-cdc#196 and now a positive
regression for the XOR-tail recogniser) covers the textbook-correct
shape that must stay silent. Together they bracket CDC-013's
acceptance shape from both sides.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic event_in,
    output logic event_seen
);
    logic src_toggle;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n)        src_toggle <= 1'b0;
        else if (event_in) src_toggle <= ~src_toggle;
    end

    logic toggle_meta, toggle_sync;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            toggle_meta <= 1'b0;
            toggle_sync <= 1'b0;
        end else begin
            toggle_meta <= src_toggle;
            toggle_sync <= toggle_meta;
        end
    end

    assign event_seen = toggle_sync;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 2.0  [get_ports src_clk]
create_clock -name dst_clk -period 20.0 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 0.5 [get_ports event_in]
"""


class ToggleNoXorTail:
    """Toggle src + 2FF dst with no XOR-tail = pulse-rate loss."""

    name = "cdc013_toggle_no_xor_tail"

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
            expected=(ExpectedFinding("CDC-013", Op.GE, 1),),
        )
