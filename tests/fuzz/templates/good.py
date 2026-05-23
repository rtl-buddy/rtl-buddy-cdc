"""Clean 2FF synchroniser — every rule must stay silent.

This is the "no false positives" sentinel. If any rule fires on a
textbook 2FF chain, the prototype has surfaced a real bug.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import product

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic rst_n,
    input  logic d_in,
    output logic q_out
);
    logic src_q;
    always_ff @(posedge src_clk or negedge rst_n)
        if (!rst_n) src_q <= 1'b0;
        else        src_q <= d_in;

    logic sync_meta;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) sync_meta <= 1'b0;
        else        sync_meta <= src_q;

    logic sync_q;
    always_ff @(posedge dst_clk or negedge rst_n)
        if (!rst_n) sync_q <= 1'b0;
        else        sync_q <= sync_meta;

    assign q_out = sync_q;
endmodule
"""

_SDC_TEMPLATE = """\
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""

# Rules that the differential runner must verify stay silent on a
# clean 2FF chain. Anything not in this list is checked by the
# "no unexpected findings" sweep on good cases.
_MUST_STAY_SILENT = (
    "CDC-001",
    "CDC-002",
    "CDC-003",
    "CDC-004",
    "CDC-005",
    "CDC-006",
    "CDC-014",
    "CDC-016",
    "RDC-001",
)


class GoodTwoFF:
    """Textbook 2FF synchroniser — no rule should fire."""

    name = "good_2ff"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        period_pairs = [(10.0, 7.5), (5.0, 11.3)]
        for src_period, dst_period in period_pairs:
            params = {
                "src_period": src_period,
                "dst_period": dst_period,
            }
            top = f"fuzz_{cls.name}_{int(src_period * 10)}_{int(dst_period * 10)}"
            sv = _TEMPLATE.format(top=top)
            sdc = _SDC_TEMPLATE.format(src_period=src_period, dst_period=dst_period)
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=sdc,
                top=top,
                params=params,
                expected=(),
                forbidden=tuple(
                    ExpectedFinding(rid, Op.ZERO) for rid in _MUST_STAY_SILENT
                ),
            )


# 'product' kept imported for symmetry with the bad templates; quiet
# the linter without removing the convention.
_ = product
