"""Gap-mining sentinel: G-1 — latch as sync stage invisible.

Reproduces a false-negative the prototype's sim oracle confirmed:
a designer who builds a "synchroniser" out of a transparent latch
followed by a flop. The latch is gated by ``dst_clk``'s level so
it's transparent half the time — a metastable src_q passes straight
through to the dst-domain flop during the transparent window.

Empirical findings from the prototype:

- ``find_crossings`` returns **zero crossings** on this design.
  The walker keys off Flop objects (``$dff*`` cells only); the
  $dlatch in between hides the dst-flop's src-flop fanin.
- ``run_all`` therefore produces **zero findings**. No CDC rule
  fires.
- The corresponding sim DUT (``tests/sim/dut_latch_sync.sv``)
  produces 83 errors / 4995 cycles at injection rate 80% —
  functionally equivalent to the textbook bad unsynced crossing
  (``dut_unsynced.sv`` at the same settings).

This template pins the current (broken) behaviour: empty
``expected``, ``forbidden`` requires CDC-001 to NOT fire today.
When the gap is fixed (e.g. by extending ``find_crossings`` to
trace through ``$dlatch``, or adding a CDC-017 latch-in-CDC-path
rule), CDC-001 or CDC-017 should fire and this test will trip —
update expected/forbidden at that point.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic latch_en,
    input  logic d_in,
    output logic q_out
);
    logic src_q;
    always_ff @(posedge src_clk) src_q <= d_in;

    // The G-1 shape: a transparent latch where a synchroniser flop
    // should be. Gated by latch_en (declared in dst_clk's domain
    // via the SDC) so the analyzer's crossing walker would also
    // need to traverse through the latch to find the src→dst path.
    logic latch_q;
    always_latch
        if (latch_en) latch_q = src_q;

    logic sync_q;
    always_ff @(posedge dst_clk) sync_q <= latch_q;

    assign q_out = sync_q;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 10.0 [get_ports src_clk]
create_clock -name dst_clk -period 7.5  [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports d_in]
set_input_delay -clock dst_clk 1.0 [get_ports latch_en]
"""

_GAP_NOTE = (
    "G-1 (rtl-buddy-cdc#188): $dlatch between source and destination "
    "flop hides the crossing from find_crossings, which keys off "
    "Flop objects ($dff* cells only). Zero crossings, zero findings; "
    "sim oracle (tests/sim/dut_latch_sync.sv) confirms the design "
    "functionally fails at 83 errors / 4995 cycles."
)


class GapG1LatchInSyncChain:
    """Pinned sentinel: $dlatch-as-sync-stage produces no findings."""

    name = "gap_g1_latch_in_sync_chain"

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
            forbidden=(
                # No CDC rule must fire today; the gap is precisely
                # this silence. When the analyzer learns to see
                # through the latch, one of these forbidden claims
                # will trip and the template's expected/forbidden
                # gets updated.
                ExpectedFinding("CDC-001", Op.ZERO),
                ExpectedFinding("CDC-014", Op.ZERO),
            ),
            gap_note=_GAP_NOTE,
        )
