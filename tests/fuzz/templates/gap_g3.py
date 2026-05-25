"""Gap-mining sentinel: G-3 — pulse synchroniser false positive on CDC-013.

The canonical fast-to-slow event-passing idiom (Cummings SNUG 2008,
§6) uses a toggle flop in the source domain, a 2FF synchroniser
into the destination, and an XOR of the synced output with its
1-cycle-delayed copy to reconstruct a 1-cycle pulse in the dst
domain. This is the *correct* solution — events are encoded as
level changes that survive sub-sampling, and the XOR-tail recovers
the pulse without losing edges.

CDC-013's detector keys off the source-side toggle pattern
(``D = en ? ~Q : Q``) without recognising the dst-side XOR-tail.
Result: a textbook-correct design fires the rule. Users today must
either waive the finding or refactor away from the recommended
idiom — both wrong outcomes.

This template pins the false positive. When the rule grows
XOR-tail recognition (positive-suppression phase), CDC-013 will
stop firing on this shape and the expected/forbidden flip:
``expected=()`` + ``forbidden=(ExpectedFinding("CDC-013", ZERO),)``.
"""

from __future__ import annotations

from collections.abc import Iterator

from .base import ExpectedFinding, Op, RenderedCase

_TEMPLATE = """\
module {top} (
    input  logic src_clk,
    input  logic dst_clk,
    input  logic en,
    output logic pulse_out
);
    // Source-side toggle flop. CDC-013's classifier matches the
    // D-pin shape `D = en ? ~Q : Q` here and fires.
    logic toggle_q;
    always_ff @(posedge src_clk) toggle_q <= en ? ~toggle_q : toggle_q;

    // 2FF synchroniser on the toggle value into the dst domain.
    logic sync_meta, sync_q;
    always_ff @(posedge dst_clk) sync_meta <= toggle_q;
    always_ff @(posedge dst_clk) sync_q    <= sync_meta;

    // XOR-tail: reconstructs the pulse in dst domain. THIS is the
    // bit CDC-013 should recognise as the "good" tail and suppress
    // on. Today it doesn't.
    logic sync_q_d;
    always_ff @(posedge dst_clk) sync_q_d <= sync_q;
    assign pulse_out = sync_q ^ sync_q_d;
endmodule
"""

_SDC = """\
create_clock -name src_clk -period 5.0  [get_ports src_clk]
create_clock -name dst_clk -period 12.0 [get_ports dst_clk]
set_clock_groups -asynchronous -group {src_clk} -group {dst_clk}
set_input_delay -clock src_clk 1.0 [get_ports en]
"""

_GAP_NOTE = (
    "G-3 (rtl-buddy-cdc#188): CDC-013 fires on the canonical "
    "pulse-synchroniser idiom (Cummings SNUG 2008 §6) — toggle "
    "source + 2FF + XOR tail. Fix: extend CDC-013 to recognise the "
    "XOR-tail and suppress when present."
)


class GapG3PulseSyncFalsePositive:
    """Pinned sentinel: canonical pulse-sync fires CDC-013 today."""

    name = "gap_g3_pulse_sync_false_positive"

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
            # CDC-013 fires today (the false positive). Encode that
            # as the current expected behaviour — when the rule
            # learns the XOR-tail, this assertion trips and the gap
            # is closed.
            expected=(ExpectedFinding("CDC-013", Op.GE, 1),),
            gap_note=_GAP_NOTE,
        )
