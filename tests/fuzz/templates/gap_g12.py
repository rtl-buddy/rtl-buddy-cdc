"""Regression sentinel: gate-level $_DFF_* flop coverage (rtl-buddy-cdc#194).

Same RTL as the CDC-001 unsynced-bit template; ``extra_yosys_passes``
overrides push the netlist through ``simplemap; abc -g cmos;`` so the
inferred ``$dff`` cells are mapped to gate-level ``$_DFF_P_``.

Asserts that the analyzer sees through the gate-level cells:

- ``find_crossings`` finds the src→dst crossing across ``$_DFF_*``
  cells.
- ``CDC-001`` fires once on the unsynced crossing — same finding as
  on the equivalent higher-level netlist.
- ``CDC-008`` stays silent — the rule exempts the ``C`` pin on
  gate-level FF cells so no spurious "clock used as data" findings
  surface.

Before rtl-buddy-cdc#194 landed, this fixture produced zero crossings,
zero CDC-001, and spurious CDC-008 on every gate-level flop. The gap
was silent-when-broken: zero findings on a real CDC bug. If any of
the assertions above regress, this sentinel is the first signal.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic d_in,
    output logic q_out
);
    logic src_q;
    always_ff @(posedge src_clk) src_q <= d_in;

    logic dst_q;
    always_ff @(posedge dst_clk) dst_q <= src_q;

    assign q_out = dst_q;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
"""


class GapG12GateLevelSilent:
    """Regression sentinel: gate-level netlist fires CDC-001 cleanly."""

    name = "gap_g12_gate_level_silent"

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
            expected=(ExpectedFinding("CDC-001", Op.EQ, 1),),
            forbidden=(ExpectedFinding("CDC-008", Op.ZERO),),
            extra_yosys_passes="simplemap; abc -g cmos;",
        )
