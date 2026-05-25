"""CDC-001 — unsynchronised single-bit control crossing.

Source-domain flop's Q feeds a destination-domain flop's D with no
2FF synchroniser in between. CDC-001 must fire. By construction the
chain depth is exactly 1, so CDC-002 fires too.

Parameter sweep:

- ``rst_polarity``: ``"async_low"`` / ``"async_high"`` / ``"none"``
- ``src_period`` / ``dst_period``: numeric pairs (always async)
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import product

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
{reset_port}
    input  logic d_in,
    output logic q_out
);
    logic src_q;
{src_block}

    logic dst_q;
{dst_block}

    assign q_out = dst_q;
endmodule
"""

_SDC_TEMPLATE = """\
create_clock -name src_clk -period {src_period} [get_ports src_clk]
create_clock -name dst_clk -period {dst_period} [get_ports dst_clk]
set_clock_groups -asynchronous -group {{src_clk}} -group {{dst_clk}}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""


def _reset_decls(polarity: str) -> tuple[str, str, str]:
    """Return (port-line, src always-ff body, dst always-ff body)."""
    if polarity == "none":
        return (
            "",
            "    always_ff @(posedge src_clk) src_q <= d_in;",
            "    always_ff @(posedge dst_clk) dst_q <= src_q;",
        )
    if polarity == "async_low":
        return (
            "    input  logic rst_n,",
            (
                "    always_ff @(posedge src_clk or negedge rst_n)\n"
                "        if (!rst_n) src_q <= 1'b0;\n"
                "        else        src_q <= d_in;"
            ),
            (
                "    always_ff @(posedge dst_clk or negedge rst_n)\n"
                "        if (!rst_n) dst_q <= 1'b0;\n"
                "        else        dst_q <= src_q;"
            ),
        )
    if polarity == "async_high":
        return (
            "    input  logic rst,",
            (
                "    always_ff @(posedge src_clk or posedge rst)\n"
                "        if (rst) src_q <= 1'b0;\n"
                "        else     src_q <= d_in;"
            ),
            (
                "    always_ff @(posedge dst_clk or posedge rst)\n"
                "        if (rst) dst_q <= 1'b0;\n"
                "        else     dst_q <= src_q;"
            ),
        )
    raise AssertionError(f"unknown polarity {polarity}")  # pragma: no cover


class UnsyncedSingleBit:
    """Single bit, no synchroniser between source and destination."""

    name = "cdc001_unsynced_single_bit"

    @classmethod
    def cases(cls) -> Iterator[RenderedCase]:
        polarities = ["async_low", "async_high", "none"]
        period_pairs = [(10.0, 7.5), (10.0, 13.0)]
        for polarity, (src_period, dst_period) in product(polarities, period_pairs):
            params = {
                "rst_polarity": polarity,
                "src_period": src_period,
                "dst_period": dst_period,
            }
            top = f"fuzz_{cls.name}_{polarity}_{int(src_period * 10)}_{int(dst_period * 10)}"
            reset_port, src_block, dst_block = _reset_decls(polarity)
            sv = _TEMPLATE.format(
                top=top,
                reset_port=reset_port + "\n" if reset_port else "",
                src_block=src_block,
                dst_block=dst_block,
            )
            sdc = _SDC_TEMPLATE.format(src_period=src_period, dst_period=dst_period)
            yield RenderedCase(
                template_name=cls.name,
                case_id=top,
                sv=sv,
                sdc=sdc,
                top=top,
                params=params,
                expected=(ExpectedFinding("CDC-001", Op.EQ, 1),),
                # CDC-002 is partitioned with CDC-001: it fires only
                # when a chain exists but is shorter than required.
                # Depth = 0 here, so CDC-001 owns the finding.
                forbidden=(
                    ExpectedFinding("CDC-002", Op.ZERO),
                    ExpectedFinding("CDC-003", Op.ZERO),
                ),
            )
