"""Positive-counterpart fixtures.

For each "bad_*" fixture there's a "good_*" version implementing the
textbook fix. These tests pin the analyzer's *acceptance shape* —
they catch false positives if a rule gets tightened in a way that
flags a known-correct pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_ROOT = Path(__file__).parent / "fixtures"

# Each entry is (fixture_dir_name, expected_async_crossing_count). The
# count is asserted so we don't accidentally regress to "0 crossings,
# trivially passes" — the analyzer must actually be exercising the
# crossing path on each fixture.
GOOD_FIXTURES = [
    ("good_2ff_sync", 1),
    ("good_registered_before_sync", 1),
    ("good_registered_source", 1),
    ("good_reset_sync", 1),
    # Generated clock: divider Q is a flop, but its CLK traces back to
    # the master via trace_clock_root, so the analyzer assigns all
    # flops to the master domain — zero structural crossings.
    ("good_generated_clock_div2", 0),
    # Exclusive clocks: structural pass sees 1 ck0→ck1 flop→flop pair,
    # but _filter_async drops it via is_unreachable_crossing.
    ("good_exclusive_clock_mux", 0),
    # set_false_path is equivalent to set_clock_groups -asynchronous
    # for CDC; 1 async crossing landing in a 2FF synchronizer.
    ("good_false_path_pair", 1),
    # set_input_delay -clock dst_clk types the data ports as same-
    # domain as the destination — CDC-006 must not fire.
    ("good_input_delay_domain", 0),
    # Typed input port reaching a 2FF synchronizer in the destination
    # domain — port→flop crossing is recognised and silent because
    # chain depth ≥ 2.
    ("good_port_typed_sync", 1),
    # Source-synchronous chain (A→B0, A→B1, B0→C0, B1→C1): four raw
    # flop→flop crossings, but every clock pair shares a master via
    # create_generated_clock so are_async() returns False and the
    # analyzer drops them all.
    ("good_source_sync_chain", 0),
    # Same topology, but the forwarded clocks are wired internally
    # rather than re-exposed as top-level ports. The good SDC declares
    # each generated clock at the internal pin where it originates
    # (``[get_pins u_a/clk_out_b0]`` etc.); trace_clock_root must stop
    # at those pins to give each block a distinct clock name, then
    # resolve() collapses them back to ck_a so no crossings remain.
    ("good_source_sync_internal", 0),
    # Reconvergent-on-paper sync chains whose downstream cones are
    # disjoint (each sync chain feeds its own register + output port).
    # Phase-2 of CDC-005 (issue #33) classifies this as harmless and
    # must not fire. Two async crossings (src_q → each sync first
    # stage).
    ("good_disjoint_fanout_sync_chains", 2),
    # Single-source variant of the same shape: one src_q fans out to
    # two independent 2FF sync chains. Restores coverage of the
    # "shared src flop, multiple chains" topology after the two-source
    # rewrite landed in #150 (issue #149). Two async crossings, both
    # rooted in the same src flop.
    ("good_single_source_fanout_sync_chains", 2),
    # $dffe-style load-enable gating (issue #34). 8-bit data bus into
    # a $dffe whose EN is a dst-domain 2FF synchronizer's tail.
    # Yosys inference of $dffe is forced by ``opt_dff`` at fixture-
    # build time (see the fixture's SV header). The fixture also
    # carries a full req/ack handshake (synced-back ack into the src
    # domain) so CDC-012 stays silent — without that feedback the
    # bus would have the functional data-hold issue CDC-012 catches.
    # Three async crossings: the 1-bit synced load_req, the 1-bit
    # synced-back ack, and the 8-bit dffe-gated data path.
    ("good_dffe_gated_bus_crossing", 3),
    # Mux-on-D gating with a transparent buffer hop between mux and
    # the dst flop's D (issue #35). The fixture's JSON is post-
    # processed (see insert_buffer.py) to splice a single $_BUF_
    # per lane; the gating-shape detector must walk through it and
    # still recognise the originating mux. Carries the same req/ack
    # handshake as good_dffe_gated_bus_crossing so CDC-012 stays
    # silent. Three async crossings (synced load_req, synced-back
    # ack, gated data bus).
    ("good_buffered_gated_bus_crossing", 3),
    # CDC-011 (#97) positive shapes: SDC types the unconstrained
    # input via ``set_input_delay -clock``, with a 2FF synchronizer
    # on any cross-domain capture.
    #
    # _two_domains_typed: port→clk_a direct (same domain, dropped) +
    # port→clk_b through 2FF sync (1 async port-sourced crossing).
    ("good_unconstrained_input_two_domains_typed", 1),
    # _derived_clock_typed: port typed to clk_a and captured in clk_b
    # through a conventional 2FF synchronizer.
    ("good_unconstrained_input_derived_clock_typed", 1),
    # _bus_two_domains_typed: same shape as _two_domains_typed but
    # width 8; the dst capture is gated by a synchronized load bit.
    # Carries a full req/ack handshake (synced-back ack into clk_a)
    # so CDC-012 stays silent. Three async crossings: load control,
    # synced-back ack, and the gated data bus.
    ("good_unconstrained_input_bus_two_domains_typed", 3),
    # _muxed_clock_typed: port typed to the same clock that captures
    # it → same domain → 0 async crossings.
    ("good_unconstrained_input_muxed_clock_typed", 0),
    # RDC-002 positive shape: matched-polarity gated reset. Both
    # flops are active-low ($adff ARST_POLARITY=0), so when the
    # producer enters reset (Q=0) the consumer sees ARST=0 == its
    # polarity expectation. No async clock crossings — single-clock
    # design.
    ("good_rdc_002_polarity_match", 0),
    # RDC-003 positive shape: sync-reset crossing protected by a 2FF
    # synchroniser in the dst_clk domain between the foreign src_rst
    # and the consuming $sdff. Zero async data crossings — the src
    # flop's Q only reaches the dst domain through the synchroniser's
    # D-input path, which find_crossings *does* see, but it terminates
    # at the synchroniser's first stage (a normal CDC-001 same-shape
    # crossing the rule pack accepts because the chain depth is 2).
    ("good_rdc_003_sync_reset_synced", 1),
    # RDC-004 positive shape: the comb-AND of two flop outputs is
    # registered on the local clock before being used as a reset, so
    # the consumer's ARST sees a clean flop edge (no glitches). All
    # polarities matched (every flop active-low) — RDC-002 silent.
    # Single-clock design — zero async crossings.
    ("good_rdc_004_registered_reset", 0),
    # RDC-005 positive shape: $mux explicitly selects between two
    # reset ports on a control signal. The user's intent is
    # unambiguous — exactly one source is active at a time — so the
    # mux-on-reset exemption keeps RDC-005 silent. Single-clock.
    ("good_rdc_005_muxed_reset", 0),
    # RDC-006 (proposed, issue #151) positive shape: muxed reset
    # source first passes through a 2FF reset synchronizer in the
    # consumer clock domain, then drives the downstream flop's async
    # clear. Deassertion is aligned to clk. Single-clock — zero
    # async crossings. Stays clean today (no RDC-006 yet) and must
    # continue to stay clean once RDC-006 lands.
    ("good_derived_async_reset_synced", 0),
    # CDC-010 (#95/#134) positive shape: foreign-domain mux select
    # passed through a (* cdc_sync *) 2FF synchroniser into the
    # gated-clock (ck0) domain before reaching the mux. The mux's S
    # is then driven by a same-domain flop, so CDC-010 stays silent
    # and the 2FF chain keeps CDC-001 silent on the underlying
    # async crossing. 1 async crossing (ck1 sel_q → ck0 sel_meta).
    ("good_sync_clock_mux", 1),
    # CDC-012 (proposed, issue #151) positive shape: same gated-bus
    # topology as bad_functional_datahold_enable, but with a textbook
    # req/ack handshake — src_payload is loaded only when arming a
    # new request, held stable across the round-trip, and the request
    # clears only when a synced-back ack from dst confirms the
    # sample. Three async crossings: held request, synced-back ack,
    # and the gated 8-bit payload.
    ("good_functional_datahold_handshake", 3),
    # CDC-009 (#47/#101/#102) positive shapes: textbook fixes for the
    # fast-to-slow pulse-loss case.
    #
    # _stretched: a 4-bit countdown counter widens the src strobe to
    # ~16 src cycles before it crosses; the src flop's D is the
    # counter's "non-zero" reduction, not the edge-detector pattern,
    # so CDC-009 stays silent. 1 async crossing (stretched_strobe →
    # strobe_meta).
    ("good_pulse_width_stretched", 1),
    # _handshake: req/ack handshake — req_held is set on req_in and
    # cleared only by a synced-back ack from dst, so the src flop
    # holds its value across many cycles. D is a priority-encoded
    # $mux nest, not an $and edge-detector. 2 async crossings (the
    # synced-back ack and the held request).
    ("good_pulse_width_handshake", 2),
    # CDC-013 (proposed, issue #151) positive shape: same fast-to-slow
    # ratio as bad_toggle_no_xor_tail but the source uses a
    # req/ack handshake instead of a toggle. The src request flop's D
    # is a priority-encoded $mux nest (set on event_in, clear on
    # synced ack, hold otherwise), not the Q/~Q toggle pattern, so
    # CDC-013 stays silent. Two async crossings (synced-back ack and
    # held request).
    ("good_fast_to_slow_handshake", 2),
    # CDC-017 positive shape: a transparent latch is fine when source
    # and destination flops share a clock domain — no CDC at all.
    # Single-clock — zero async crossings. Pins that CDC-017 only
    # fires on cross-domain latch placement.
    ("good_latch_in_same_domain", 0),
]


@pytest.mark.parametrize("name, expected_async", GOOD_FIXTURES)
def test_good_fixture_has_no_violations(name: str, expected_async: int) -> None:
    fix_dir = FIX_ROOT / name
    json_path = fix_dir / f"{name}.json"
    sdc_path = fix_dir / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")

    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(
        module, port_clock=spec.port_clock, pin_clocks=spec.pin_clocks
    )
    async_crossings = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            async_crossings.append(c)
    assert len(async_crossings) == expected_async, (
        f"{name}: expected {expected_async} async crossing(s), "
        f"got {len(async_crossings)} — fixture may have regressed"
    )

    violations = run_all_rules(module, async_crossings, spec)
    assert violations == [], (
        f"{name}: expected zero violations, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )


def test_cdc_002_fires_when_required_depth_raised() -> None:
    """A 2-stage synchronizer is silent at the default required_depth=2
    but must fire CDC-002 when the project raises the bar to 3."""
    name = "good_2ff_sync"
    fix_dir = FIX_ROOT / name
    json_path = fix_dir / f"{name}.json"
    sdc_path = fix_dir / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")

    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]

    assert run_all_rules(module, async_crossings, spec, required_depth=2) == []
    raised = run_all_rules(module, async_crossings, spec, required_depth=3)
    assert len(raised) == 1 and raised[0].rule_id == "CDC-002"
