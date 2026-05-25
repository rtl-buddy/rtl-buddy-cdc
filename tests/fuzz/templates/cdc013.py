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

from ._sweep import case_suffix
from .base import ExpectedFinding, Op, RenderedCase

# CDC-013's failure mode is rate-mismatch driven, same as CDC-009:
# fast src toggles faster than dst can sample, so events get lost.
# Reuse a fast-src / slow-dst period spread.
_FAST_TO_SLOW_PERIODS: list[tuple[float, float]] = [
    (2.0, 20.0),
    (1.5, 18.0),
    (3.0, 24.0),
    (2.5, 30.0),
    (1.0, 17.0),
    (2.0, 15.0),
    (4.0, 28.0),
    (1.2, 22.0),
    (3.5, 19.0),
    (2.0, 25.0),
    (1.8, 13.0),
    (2.5, 21.0),
]

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
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 0.5 [get_ports event_in]
"""


class ToggleNoXorTail:
    """Toggle src + 2FF dst with no XOR-tail = pulse-rate loss."""

    name = "cdc013_toggle_no_xor_tail"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for src_period, dst_period in _FAST_TO_SLOW_PERIODS:
            top = f"fuzz_{cls.name}_{case_suffix(src_period, dst_period)}"
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=_TEMPLATE.format(top=top),
                sdc=_SDC.format(src_period=src_period, dst_period=dst_period),
                top=top,
                params={"src_period": src_period, "dst_period": dst_period},
                expected=(ExpectedFinding("CDC-013", Op.GE, 1),),
            )
