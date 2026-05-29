"""Coverage-focused tests for the upper half of ``rules.py``.

Targets the rule functions added after the original CDC-001..008 pack:
CDC-011..CDC-021 and RDC-001..RDC-008, plus the ``run_all``
post-processing layer (``_tag_handshake_related`` and the CDC-018
``depth_threshold`` plumbing / guard).

Every test drives the analyzer the way CI does — load a committed
netlist-JSON fixture with :func:`rtl_buddy_cdc.netlist.load` (pure
JSON read, no toolchain), parse the paired SDC, build crossings, run
the full rule
pack via :func:`rtl_buddy_cdc.rules.run_all` — then asserts on the
concrete :class:`~rtl_buddy_cdc.rules.Violation` content (rule_id,
severity, message anchors, cell_name). A handful of unit-level tests
construct ``Violation`` objects directly to pin the handshake-tagging
branches that need no netlist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import Crossing, find_crossings
from rtl_buddy_cdc.flops import Flop
from rtl_buddy_cdc.netlist import Cell
from rtl_buddy_cdc.rules import (
    _tag_handshake_related,
    check_cdc_018,
    run_all,
)

FIX = Path(__file__).parent / "fixtures"


def _analyze(name: str) -> tuple[netlist.Module, list[Crossing], sdc_mod.ClockSpec]:
    """Load ``<name>`` fixture, synthesize unconstrained inputs, and
    return ``(module, async_crossings, spec)`` ready for ``run_all``.

    Mirrors the per-fixture ``context`` fixtures used across the
    existing ``test_bad_*`` suite (e.g. ``test_bad_rdc_005...``): the
    async filter drops same-domain and unreachable crossings so the
    crossing list handed to ``run_all`` is the CDC-relevant subset.
    """
    fix_dir = FIX / name
    json_path = fix_dir / f"{name}.json"
    sdc_path = fix_dir / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    async_crossings: list[Crossing] = []
    for c in crossings:
        a = spec.clock_for_port(c.src_clock) or c.src_clock
        b = spec.clock_for_port(c.dst_clock) or c.dst_clock
        if spec.is_unreachable_crossing(a, b):
            continue
        if spec.are_async(a, b):
            async_crossings.append(c)
    return module, async_crossings, spec


def _run(name: str):
    module, async_crossings, spec = _analyze(name)
    return run_all(module, async_crossings, spec)


def _of(violations, rule_id: str):
    return [v for v in violations if v.rule_id == rule_id]


# --------------------------------------------------------------------------
# RDC family
# --------------------------------------------------------------------------


def test_rdc_001_reset_crossing_groups_by_source() -> None:
    """RDC-001 fires on an async ARST driven by a foreign-domain flop;
    the message names the destination clock and the foreign source."""
    violations = _run("bad_reset_crossing")
    rdc_001 = _of(violations, "RDC-001")
    assert len(rdc_001) >= 1
    v = rdc_001[0]
    assert v.severity == "error"
    assert "async reset crossing" in v.message
    assert "add a reset" in v.message
    assert v.cell_name is not None


def test_rdc_003_sync_reset_crossing() -> None:
    """RDC-003 walks the SRST pin backward into a foreign async domain."""
    violations = _run("bad_rdc_003_sync_reset_crossing")
    rdc_003 = _of(violations, "RDC-003")
    assert len(rdc_003) == 1
    v = rdc_003[0]
    assert v.severity == "error"
    assert "sync reset crossing" in v.message
    assert "SRST" in v.message
    assert "2FF reset synchroniser" in v.message
    # The source flop named in the message lives in src_clk.
    assert "$procdff$12" in v.message


def test_rdc_004_comb_driven_reset_lists_fanin_flops() -> None:
    """RDC-004 fires when an async reset pin is a comb-gate output whose
    fanin reaches flops; the message enumerates those flops."""
    violations = _run("bad_rdc_004_comb_driven_reset")
    rdc_004 = _of(violations, "RDC-004")
    assert len(rdc_004) == 1
    v = rdc_004[0]
    assert v.severity == "error"
    assert "reset driven by combinational logic" in v.message
    assert v.cell_name == "$procdff$12"
    # Both upstream flops are surfaced.
    assert "$procdff$17" in v.message
    assert "$procdff$22" in v.message


def test_rdc_005_multi_source_reset_is_warning() -> None:
    """RDC-005 warns on a comb-AND of two top-level reset ports with no
    mux selection; lists both ports and suggests muxing."""
    violations = _run("bad_rdc_005_multi_source_reset")
    rdc_005 = _of(violations, "RDC-005")
    assert len(rdc_005) == 1
    v = rdc_005[0]
    assert v.severity == "warning"
    assert "multiple reset sources converging" in v.message
    assert "global_rst_n" in v.message
    assert "block_rst_n" in v.message
    assert "$mux" in v.message


def test_rdc_002_polarity_mismatch_inferred_variant() -> None:
    """RDC-002 fires on a flop->flop reset where the producer's
    ARST_VALUE disagrees with the consumer's ARST_POLARITY."""
    violations = _run("bad_rdc_002_polarity_mismatch")
    rdc_002 = _of(violations, "RDC-002")
    assert len(rdc_002) == 1
    v = rdc_002[0]
    assert v.severity == "error"
    assert "reset polarity mismatch" in v.message
    assert "ARST_POLARITY" in v.message
    assert "ARST_VALUE" in v.message


def test_rdc_006_muxed_async_reset_is_warning() -> None:
    """RDC-006 fills the gap RDC-005 leaves: a $mux directly driving an
    async reset pin still has an unsynchronised deassertion edge."""
    violations = _run("bad_derived_async_reset_unsync")
    rdc_006 = _of(violations, "RDC-006")
    assert len(rdc_006) == 1
    v = rdc_006[0]
    assert v.severity == "warning"
    assert "muxed async reset without local synchroniser" in v.message
    assert "$mux" in v.message
    # The mux's data legs trace back to the two reset ports.
    assert "block_rst_n" in v.message
    assert "global_rst_n" in v.message
    assert "2FF reset synchroniser" in v.message
    assert v.cell_name == "$procdff$8"


def test_rdc_007_deassert_polarity_backwards() -> None:
    """RDC-007 fires on a reset-sync chain whose head D loads the
    *asserted* value, so it never deasserts."""
    violations = _run("bad_reset_sync_deassert_polarity")
    rdc_007 = _of(violations, "RDC-007")
    assert len(rdc_007) == 1
    v = rdc_007[0]
    assert v.severity == "error"
    assert "reset-synchroniser chain" in v.message
    assert "one-shot" in v.message
    # Active-low chain whose head was tied to the asserted ('0') value.
    assert "active-low" in v.message
    assert "deasserts on '1'" in v.message


def test_rdc_008_unsynced_primary_reset_port() -> None:
    """RDC-008 fires on >=2 consumers driven directly by a raw reset
    port in a domain that has a sync chain elsewhere (asymmetry gate)."""
    violations = _run("bad_primary_reset_unsynced")
    rdc_008 = _of(violations, "RDC-008")
    assert len(rdc_008) == 1
    v = rdc_008[0]
    assert v.severity == "error"
    assert "unsynced primary-reset port" in v.message
    assert "raw_rst_n" in v.message
    # Two consumer flops in the unsynced domain are named.
    assert "$procdff$13" in v.message
    assert "$procdff$18" in v.message
    assert "clk_b" in v.message


# --------------------------------------------------------------------------
# CDC-011: unconstrained primary input on a data pin
# --------------------------------------------------------------------------


def test_cdc_011_multi_domain_is_error() -> None:
    """A single unconstrained port captured in >=2 domains is an
    intrinsic error — it cannot be synchronous to two clocks."""
    violations = _run("bad_unconstrained_input_two_domains")
    cdc_011 = _of(violations, "CDC-011")
    assert len(cdc_011) == 1
    v = cdc_011[0]
    assert v.severity == "error"
    assert "captured in multiple clock domains" in v.message
    assert "'in'" in v.message
    # Both destination clocks are listed.
    assert "clk_a" in v.message
    assert "clk_b" in v.message


def test_cdc_011_single_domain_is_warning() -> None:
    """A single unconstrained port landing in one domain is a
    methodology warning suggesting set_input_delay -clock."""
    violations = _run("bad_unconstrained_input_derived_clock")
    cdc_011 = _of(violations, "CDC-011")
    assert len(cdc_011) == 1
    v = cdc_011[0]
    assert v.severity == "warning"
    assert "no set_input_delay -clock typing" in v.message
    assert "set_input_delay -clock" in v.message


# --------------------------------------------------------------------------
# CDC-012 / CDC-013 (handshake / toggle families)
# --------------------------------------------------------------------------


def test_cdc_012_functional_datahold_warning() -> None:
    """CDC-012 warns on a gated multi-bit crossing with no synced-back
    handshake (the functional data-hold risk)."""
    violations = _run("bad_functional_datahold_enable")
    cdc_012 = _of(violations, "CDC-012")
    assert len(cdc_012) >= 1
    v = cdc_012[0]
    assert v.severity == "warning"
    assert "functional data-hold risk" in v.message
    assert "gated bus crossing" in v.message
    assert "req/ack handshake" in v.message
    assert v.crossing is not None
    assert v.crossing.width > 1


def test_cdc_013_toggle_event_loss_warning() -> None:
    """CDC-013 warns on a fast-to-slow toggle synchroniser with no XOR
    edge-detector tail (event-loss risk)."""
    violations = _run("bad_toggle_no_xor_tail")
    cdc_013 = _of(violations, "CDC-013")
    assert len(cdc_013) == 1
    v = cdc_013[0]
    assert v.severity == "warning"
    assert "toggle-synchroniser event-loss risk" in v.message
    assert "D = en ? ~Q : Q" in v.message
    # Fast-to-slow: src period strictly less than dst period.
    assert v.crossing is not None


# --------------------------------------------------------------------------
# CDC-014..017 (synchroniser structural hazards)
# --------------------------------------------------------------------------


def test_cdc_014_comb_between_sync_stages() -> None:
    """CDC-014 fires on a gate sitting between two synchroniser
    stages — the gate samples a still-metastable first-stage output."""
    violations = _run("bad_comb_between_sync_stages")
    cdc_014 = _of(violations, "CDC-014")
    assert len(cdc_014) == 1
    v = cdc_014[0]
    assert v.severity == "error"
    assert "combinational logic between synchroniser stages" in v.message
    assert "feeds a comb cell" in v.message
    assert v.cell_name == "$procdff$19"


def test_cdc_016_opposite_edge_synchroniser() -> None:
    """CDC-016 fires when two adjacent sync-chain stages clock on
    opposite edges — halves MTBF."""
    violations = _run("bad_opposite_edge_sync")
    cdc_016 = _of(violations, "CDC-016")
    assert len(cdc_016) == 1
    v = cdc_016[0]
    assert v.severity == "error"
    assert "opposite-edge synchroniser" in v.message
    assert "halves MTBF" in v.message
    # The message names both edges; one posedge, one negedge.
    assert "posedge" in v.message
    assert "negedge" in v.message


def test_cdc_017_transparent_latch_in_cdc_path() -> None:
    """CDC-017 fires on a $dlatch between two flops in different
    domains — the latch is transparent and provides no resolution."""
    violations = _run("bad_latch_in_cdc_path")
    cdc_017 = _of(violations, "CDC-017")
    assert len(cdc_017) == 1
    v = cdc_017[0]
    assert v.severity == "error"
    assert "transparent latch in CDC path" in v.message
    # cell_name anchors on the offending latch cell.
    assert v.cell_name is not None
    assert "dlatch" in v.cell_name


# --------------------------------------------------------------------------
# CDC-018..021
# --------------------------------------------------------------------------


def test_cdc_018_cascaded_synchroniser_warning() -> None:
    """CDC-018 warns on a chain at/over the depth threshold (default 4)."""
    violations = _run("bad_cascaded_synchroniser")
    cdc_018 = _of(violations, "CDC-018")
    assert len(cdc_018) == 1
    v = cdc_018[0]
    assert v.severity == "warning"
    assert "cascaded synchroniser chain" in v.message
    assert "4-flop chain" in v.message
    assert "textbook 2FF sync is sufficient" in v.message


def test_cdc_018_threshold_raises_above_chain_silences() -> None:
    """Raising ``cdc_018_depth_threshold`` above the chain depth
    silences CDC-018 while leaving the rest of the pack untouched."""
    module, async_crossings, spec = _analyze("bad_cascaded_synchroniser")
    high = run_all(module, async_crossings, spec, cdc_018_depth_threshold=8)
    assert _of(high, "CDC-018") == []
    low = run_all(module, async_crossings, spec, cdc_018_depth_threshold=4)
    assert len(_of(low, "CDC-018")) == 1


def test_cdc_018_threshold_below_two_raises() -> None:
    """The standalone checker guards against a sub-2FF threshold."""
    module, async_crossings, spec = _analyze("bad_cascaded_synchroniser")
    with pytest.raises(ValueError, match="depth_threshold must be >= 2"):
        check_cdc_018(module, async_crossings, spec, 1)


def test_cdc_019_independent_onehot_sync_warning() -> None:
    """CDC-019 warns when a one-hot decode is split into independent
    per-bit syncs that can resolve on different cycles."""
    violations = _run("bad_onehot_decode_independent_sync")
    cdc_019 = _of(violations, "CDC-019")
    assert len(cdc_019) >= 1
    v = cdc_019[0]
    assert v.severity == "warning"
    assert "independently-synced one-hot decode" in v.message


def test_cdc_020_sliced_bus_reconvergence_warning() -> None:
    """CDC-020 warns on a multi-bit source flop sliced into per-lane
    crossings that reconverge in the destination domain."""
    violations = _run("bad_sliced_bus_reconvergence")
    cdc_020 = _of(violations, "CDC-020")
    assert len(cdc_020) >= 1
    v = cdc_020[0]
    assert v.severity == "warning"
    assert "sliced-bus reconvergence across CDC" in v.message
    assert "WIDTH=4" in v.message


def test_cdc_021_undeclared_clock_port_is_error() -> None:
    """CDC-021 fires when a flop's CLK traces to a port with no
    create_clock — every other rule silently skips that domain."""
    violations = _run("bad_flop_clk_undeclared_port")
    cdc_021 = _of(violations, "CDC-021")
    assert len(cdc_021) == 1
    v = cdc_021[0]
    assert v.severity == "error"
    assert "flop CLK driven by undeclared port" in v.message
    assert "clk_aux" in v.message
    assert "create_clock" in v.message
    assert v.cell_name == "$procdff$3"


# --------------------------------------------------------------------------
# run_all post-processing: _tag_handshake_related
# --------------------------------------------------------------------------


def test_handshake_tag_appended_on_paired_finding() -> None:
    """When a CDC-012 and a CDC-001/002 finding share an async domain
    pair, run_all appends a ``[handshake-related]`` cross-reference to
    the CDC-001/002 message naming the gated bus source flop."""
    violations = _run("bad_handshake_ack_missing")
    # Both families must fire for the tag to be meaningful.
    assert _of(violations, "CDC-012")
    cdc_001 = _of(violations, "CDC-001")
    assert cdc_001
    tagged = [v for v in cdc_001 if "[handshake-related]" in v.message]
    assert tagged, "expected the CDC-001 finding to carry the handshake tag"
    msg = tagged[0].message
    assert "same async domain pair" in msg
    assert "CDC-012-firing gated bus" in msg
    assert "req/ack" in msg


def _flop(name: str) -> Flop:
    """Minimal Flop standing in for a crossing endpoint in unit tests."""
    cell = Cell(name=name, type="$dff", connections={})
    return Flop(cell=cell, clk=1, d=(2,), q=(3,))


def _crossing(src: str, dst: str, src_flop: Flop, dst_flop: Flop) -> Crossing:
    return Crossing(
        src_clock=src,
        dst_flop=dst_flop,
        dst_clock=dst,
        min_hops=0,
        width=1,
        src_flop=src_flop,
    )


def test_tag_handshake_noop_without_cdc_012(monkeypatch) -> None:
    """``_tag_handshake_related`` is a pure pass-through when the list
    has no CDC-012 finding — the common case."""
    from rtl_buddy_cdc.rules import Violation

    only = [
        Violation(
            rule_id="CDC-001",
            severity="error",
            message="lone control crossing",
            crossing=_crossing("a", "b", _flop("src"), _flop("dst")),
        )
    ]
    out = _tag_handshake_related(only)
    assert out is only  # identity: early return, untouched


def test_tag_handshake_unpaired_domain_left_untouched() -> None:
    """A CDC-012 on one domain pair must not tag a CDC-001 on a
    *different* pair — the partner lookup misses, so the message is
    passed through verbatim."""
    from rtl_buddy_cdc.rules import Violation

    cdc_012 = Violation(
        rule_id="CDC-012",
        severity="warning",
        message="gated bus on a/b",
        crossing=_crossing("a", "b", _flop("gsrc"), _flop("gdst")),
    )
    # CDC-001 on an unrelated (c, d) pair.
    cdc_001 = Violation(
        rule_id="CDC-001",
        severity="error",
        message="unrelated control crossing",
        crossing=_crossing("c", "d", _flop("csrc"), _flop("cdst")),
    )
    out = _tag_handshake_related([cdc_012, cdc_001])
    tagged = [v for v in out if v.rule_id == "CDC-001"][0]
    assert "[handshake-related]" not in tagged.message
    assert tagged.message == "unrelated control crossing"


def test_tag_handshake_matches_either_direction() -> None:
    """The pairing is by *unordered* domain pair: a CDC-012 on (a,b)
    tags a CDC-001 whose crossing runs b->a."""
    from rtl_buddy_cdc.rules import Violation

    gated_src = _flop("gated_src_flop")
    gated_dst = _flop("gated_dst_flop")
    cdc_012 = Violation(
        rule_id="CDC-012",
        severity="warning",
        message="gated bus on a/b",
        crossing=_crossing("a", "b", gated_src, gated_dst),
    )
    cdc_001 = Violation(
        rule_id="CDC-001",
        severity="error",
        message="reverse-direction control crossing",
        crossing=_crossing("b", "a", _flop("csrc"), _flop("cdst")),
    )
    out = _tag_handshake_related([cdc_012, cdc_001])
    tagged = [v for v in out if v.rule_id == "CDC-001"][0]
    assert "[handshake-related]" in tagged.message
    # The tag names the gated bus endpoints, not the CDC-001 crossing.
    assert "gated_src_flop" in tagged.message
    assert "gated_dst_flop" in tagged.message
