"""Gap-mining sentinel: G-12 — gate-level $_DFF_* flops invisible.

Reproduces the silent-failure mode from the rtl-buddy-cdc#188
coverage survey, gap G-12. Same RTL as the CDC-001 unsynced-bit
template; the case overrides ``extra_yosys_passes`` to push the
netlist through ``simplemap; abc -g cmos;`` so the inferred ``$dff``
cells are mapped to gate-level ``$_DFF_P_``.

Empirically observed today:

- Higher-level netlist: analyzer fires CDC-001 (one finding, correct).
- Gate-level netlist (this template): analyzer fires **zero
  CDC-001** because ``rtl_buddy_cdc.flops.FF_CELL_TYPES`` only lists
  ``$dff*`` cells. Additionally fires **bogus CDC-008** ("clock used
  as data") because the gate-level flops' CLK pins look like
  ordinary nets to the clock-tree walk.

This template pins that current behaviour. When the gap is fixed:

- CDC-001 will start firing → ``forbidden`` ZERO for CDC-001 will
  trip → this test will fail.
- The bogus CDC-008 should also disappear, but we don't pin its
  *absence* here — the rule itself is correct, only the gate-level
  flop visibility is the fix-target.

To convert this template from sentinel to passing case, move
``CDC-001`` from ``forbidden`` to ``expected`` and drop the
``gap_note``.

README at line 305 today claims "full coverage — Yosys primitives,
gate-level simplemap / abc output" for CDC-001 through CDC-010. That
claim is true for CDC-010 (which has its own gate-level cell map)
but false for CDC-001/-002/-003/-004/-005/-006 (which all key off
``find_flops``).
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

_GAP_NOTE = (
    "G-12 (rtl-buddy-cdc#188): gate-level $_DFF_* flops invisible to "
    "find_flops; analyzer silently produces zero CDC-001 findings on "
    "tech-mapped netlists. README at line 305 claims gate-level coverage "
    "for CDC-001..-006; that claim is false today."
)


class GapG12GateLevelSilent:
    """Pinned sentinel: gate-level netlist produces zero CDC-001."""

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
            # No ``expected`` claims — the bug is the *absence* of a
            # finding that should fire. Capture that with forbidden
            # CDC-001 (analyzer must not fire today; flips to expected
            # when fixed).
            expected=(),
            forbidden=(ExpectedFinding("CDC-001", Op.ZERO),),
            extra_yosys_passes="simplemap; abc -g cmos;",
            gap_note=_GAP_NOTE,
        )
