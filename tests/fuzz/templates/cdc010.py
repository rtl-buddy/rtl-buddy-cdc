"""CDC-010 — clock mux with foreign-domain select.

A 2-to-1 mux selects between two legitimate ck0-domain clocks; its
select pin is driven by a flop in the async ck1 domain. Every ck1
edge that toggles the select can chop the muxed output mid-period,
producing a sub-period runt the downstream flop will sample as a
real edge. The fix is to synchronise the select into ck0 before it
reaches the mux.

The two mux inputs are declared as the *same* clock in the SDC so
the mux output's clock-input-domain set collapses to ``{ck0}``; the
foreign-domain select is what trips the rule structurally.
"""

from __future__ import annotations

from collections.abc import Iterator

from ._sweep import TWO_CLOCK_PERIODS, case_suffix
from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic ck0_a,
    input  logic ck0_b,
    input  logic ck1,
    input  logic rst_n,
    input  logic sel_d,
    input  logic d_in,
    output logic q_out
);
    logic sel_q;
    always_ff @(posedge ck1 or negedge rst_n)
        if (!rst_n) sel_q <= 1'b0;
        else        sel_q <= sel_d;

    logic ck_out;
    assign ck_out = sel_q ? ck0_a : ck0_b;

    always_ff @(posedge ck_out or negedge rst_n)
        if (!rst_n) q_out <= 1'b0;
        else        q_out <= d_in;
endmodule
"""

_SDC = """\
create_clock -name ck0 -period {src_period}  [get_ports {{ck0_a ck0_b}}]
create_clock -name ck1 -period {dst_period}  [get_ports ck1]
set_clock_groups -asynchronous -group {{ck0}} -group {{ck1}}
set_input_delay -clock ck1 0.0 [get_ports sel_d]
set_input_delay -clock ck0 0.0 [get_ports d_in]
"""


class AsyncClockMux:
    """Async select on a 2-to-1 clock mux."""

    name = "cdc010_async_clock_mux"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        for ck0_period, ck1_period in TWO_CLOCK_PERIODS:
            top = f"fuzz_{cls.name}_{case_suffix(ck0_period, ck1_period)}"
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=_TEMPLATE.format(top=top),
                sdc=_SDC.format(src_period=ck0_period, dst_period=ck1_period),
                top=top,
                params={"ck0_period": ck0_period, "ck1_period": ck1_period},
                expected=(ExpectedFinding("CDC-010", Op.GE, 1),),
            )
