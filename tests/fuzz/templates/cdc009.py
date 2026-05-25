"""CDC-009 — fast-to-slow pulse-width loss.

A 1-cycle src pulse encoded as ``event_q & ~event_d`` (edge detector)
is captured by a much slower dst clock. The pulse can land entirely
between two dst rising edges and be dropped — data lost without
metastability ever entering the picture. CDC-001/-002 stay silent
because the dst-side capture is a textbook 2FF; the failure is
pulse-width, not synchroniser depth.

See rtl-buddy-cdc#47 for the rule's framing. The good counterpart
either width-stretches the strobe or replaces the edge-detector
source with a held request/ack handshake.
"""

from __future__ import annotations

from collections.abc import Iterator

from ._sweep import case_suffix
from .base import ExpectedFinding, Op, RenderedCase

# Aggressive src:dst ratios. The pulse-loss failure mode only
# manifests when the dst sampling rate is too coarse to catch every
# src strobe — sweep period pairs across the fast-to-slow regime.
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
    output logic captured
);
    logic event_q, event_d, event_strobe;
    always_ff @(posedge src_clk or negedge rst_n) begin
        if (!rst_n) begin
            event_q      <= 1'b0;
            event_d      <= 1'b0;
            event_strobe <= 1'b0;
        end else begin
            event_q      <= event_in;
            event_d      <= event_q;
            event_strobe <= event_q & ~event_d;
        end
    end

    logic captured_meta;
    always_ff @(posedge dst_clk or negedge rst_n) begin
        if (!rst_n) begin
            captured_meta <= 1'b0;
            captured      <= 1'b0;
        end else begin
            captured_meta <= event_strobe;
            captured      <= captured_meta;
        end
    end
endmodule
"""

_SDC = """\
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 0.5 [get_ports event_in]
"""


class PulseWidthFastToSlow:
    """10x clock ratio + edge-detector source = pulse-loss risk."""

    name = "cdc009_pulse_width_fast_to_slow"

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
                expected=(ExpectedFinding("CDC-009", Op.GE, 1),),
                forbidden=(
                    ExpectedFinding("CDC-001", Op.ZERO),
                    ExpectedFinding("CDC-002", Op.ZERO),
                ),
            )
