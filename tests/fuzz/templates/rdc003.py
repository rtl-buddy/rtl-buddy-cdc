"""RDC-003 — sync-reset crossing without a reset synchroniser.

A reset signal registered in ``src_clk`` directly drives the
synchronous reset pin (``SRST``) of a flop in ``dst_clk``. Sync
resets are sampled on the dst clock edge, so a foreign-domain
source can be metastable on the cycle the src flop changes. The
textbook fix is a 2FF reset synchroniser in ``dst_clk`` between the
foreign reset and the consuming ``$sdff``.

Requires Yosys ``opt_dff`` so the mux-on-D pattern folds into
``$sdff``; without it the consumer is a plain ``$dff`` + ``$mux``
and the rule (which keys on the ``SRST`` pin) can't see it. The
default fuzz pipeline applies ``proc; opt_dff; flatten`` so this
just works.
"""

from __future__ import annotations

from collections.abc import Iterator

from ._sweep import TWO_CLOCK_PERIODS, case_suffix
from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic global_rst_n,
    input  logic kill_req,
    input  logic d_in,
    output logic q_out
);
    logic src_rst;
    always_ff @(posedge src_clk or negedge global_rst_n)
        if (!global_rst_n) src_rst <= 1'b0;
        else               src_rst <= kill_req;

    logic q_dst;
    always_ff @(posedge dst_clk)
        if (src_rst) q_dst <= 1'b0;
        else         q_dst <= d_in;

    assign q_out = q_dst;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 1.0 [get_ports kill_req]
set_input_delay -clock dst_clk 1.0 [get_ports d_in]
"""


class SyncResetCrossing:
    """Foreign-domain src_rst driving dst_clk $sdff's SRST."""

    name = "rdc003_sync_reset_crossing"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for src_period, dst_period in TWO_CLOCK_PERIODS:
            top = f"fuzz_{cls.name}_{case_suffix(src_period, dst_period)}"
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=_TEMPLATE.format(top=top),
                sdc=_SDC.format(src_period=src_period, dst_period=dst_period),
                top=top,
                params={"src_period": src_period, "dst_period": dst_period},
                expected=(ExpectedFinding("RDC-003", Op.GE, 1),),
                # opt_dff folds the if/else into $sdff so SRST is exposed.
                extra_yosys_passes="opt_dff;",
            )
