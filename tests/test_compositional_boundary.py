"""Compositional per-module boundary analysis (rtl-buddy-cdc#261).

Boundary abstraction buys scale by collapsing a subtree to its port
boundary, and #259 closed the *silent* half of what that costs by
DECLINING the dangerous shapes — a multi-clock block, or a single-clock
block with >=2 incoming crossings. Sound, but it declined exactly the
dense-CDC integration blocks abstraction is most valuable on.

#261 analyses each boundary MODULE once, on its own internals, and lifts
the result into the summary instead of erasing it. These tests pin the
three things that has to mean:

1. **Proof, not assumption.** ``synchronised`` / ``sync_depth`` is the one
   lever that can make the analyzer UNDER-report, so a 2FF chain on a
   boundary input suppresses CDC-001 only when it is proven — and a 1FF
   chain or a comb bypass around a real chain must NOT be trusted.
2. **Coverage, not just scale.** A finding inside an abstracted block is
   reported, once per module type with its instances named, and an
   internal reconvergence is re-raised at the boundary.
3. **Nothing regresses.** Every #253/#259/#273 guard still holds: a stub
   blackbox (zero cells) behaves exactly as before, the multi-clock FIFO
   never silently drops its crossing, and a clock-forwarding module is
   still declined even with its internals in hand.

Each fixture ships three netlists built from ONE source:

- ``<name>.flat.json``  — fully flattened (the reference result),
- ``<name>.grey.json``  — the module kept as a boundary cell WITH its
  cells (``yosys ... proc; setattr -mod -set blackbox 1 <m>; flatten``,
  which the CLI surfaces as ``lint --greybox <m>``),
- ``<name>.json``       — the same boundary with its body stripped (what
  ``read_slang --blackboxed-module`` produces), i.e. the pre-#261 state.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.abstract import _instance_clocks
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.compositional import (
    INTERNAL_RULE_EXCLUSIONS,
    analyse_module,
    derive_sub_spec,
)
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.hierarchy import (
    compose_boundaries,
    reconvergence_unsafe_instances,
)

runner = CliRunner()

FIX = Path(__file__).parent / "fixtures"


def _paths(name: str) -> tuple[Path, Path, Path, Path]:
    """``(flat, grey, stub, sdc)`` for a three-netlist fixture."""
    d = FIX / name
    return (
        d / f"{name}.flat.json",
        d / f"{name}.grey.json",
        d / f"{name}.json",
        d / f"{name}.sdc",
    )


def _report(path: Path, sdc: Path, *extra: str) -> dict:
    run = runner.invoke(
        app, ["analyze", "-n", str(path), "-s", str(sdc), "-f", "json", *extra]
    )
    assert run.exit_code in (0, 1), run.output
    return json.loads(run.output)


def _rule_ids(report: dict) -> Counter:
    return Counter(v["rule_id"] for v in report["violations"])


def _load(path: Path, sdc: Path):
    top, blackboxes = netlist.load_with_blackboxes(path)
    spec = sdc_mod.parse_file(sdc)
    sdc_mod.synthesize_unconstrained_inputs(spec, top)
    return top, blackboxes, spec


def _boundaries(path: Path, sdc: Path, **kw):
    top, blackboxes, spec = _load(path, sdc)
    return (top, spec) + compose_boundaries(top, blackboxes, spec, **kw)


# --------------------------------------------------------------------------
# (i) a PROVEN input synchroniser suppresses the CDC-001 over-report
# --------------------------------------------------------------------------


def test_input_sync_greybox_matches_flat_exactly() -> None:
    """The headline: a block that really does synchronise its boundary
    input is reported as clean, exactly as the flattened design is —
    while the same block as a stub blackbox fires CDC-001, the
    over-report §4.9 documents as the price of the inert ``synchronised``
    field."""
    flat, grey, stub, sdc = _paths("bbx_input_sync")
    flat_r, grey_r, stub_r = (_report(p, sdc) for p in (flat, grey, stub))

    assert _rule_ids(flat_r) == Counter()
    assert _rule_ids(grey_r) == Counter()
    # Same crossing count too — the crossing is still REPORTED, it is only
    # the false "no second-stage synchronizer" finding that goes away.
    assert grey_r["summary"]["crossings"] == flat_r["summary"]["crossings"] == 1
    assert grey_r["summary"]["async_crossings"] == 1

    # The pre-#261 behaviour, kept visible: no internals, no proof.
    assert _rule_ids(stub_r) == Counter({"CDC-001": 1})

    # And the scaling win is real: the block's flops are not walked.
    assert grey_r["summary"]["flops"] < flat_r["summary"]["flops"]


def test_input_sync_depth_is_lifted_not_just_a_boolean() -> None:
    """``--sync-depth 4`` must reach INSIDE the block. The proven depth is
    3, so CDC-002 fires on the abstracted run exactly as it does flat —
    which a plain ``synchronised`` boolean could never express."""
    flat, grey, stub, sdc = _paths("bbx_input_sync")
    flat_r = _report(flat, sdc, "--sync-depth", "4")
    grey_r = _report(grey, sdc, "--sync-depth", "4")
    assert _rule_ids(flat_r) == _rule_ids(grey_r) == Counter({"CDC-002": 1})
    (v,) = grey_r["violations"]
    assert "found 3 flop(s), required >= 4" in v["message"]


def test_input_sync_summary_records_the_proof() -> None:
    """The proof lands on the data model: the input port carries
    ``synchronised`` + the measured depth, and the summary is marked as
    compositionally analysed."""
    _flat, grey, _stub, sdc = _paths("bbx_input_sync")
    _top, _spec, boundaries, stats = _boundaries(grey, sdc)
    summary = boundaries["u_sync"]
    assert summary.internal_analysed is True
    pb = summary.input_ports["req_in"]
    assert pb.synchronised is True
    assert pb.sync_depth == 3
    assert stats.analysed_modules == frozenset({"ctrl_sync"})
    assert stats.declined_modules == frozenset()


def test_stub_blackbox_records_no_proof() -> None:
    """The same fixture without internals: nothing is analysed, nothing is
    claimed. This is the invariant that keeps every pre-#261 fixture
    green — a zero-cell blackbox takes the untouched path."""
    _flat, _grey, stub, sdc = _paths("bbx_input_sync")
    _top, _spec, boundaries, stats = _boundaries(stub, sdc)
    summary = boundaries["u_sync"]
    assert summary.internal_analysed is False
    assert summary.input_ports["req_in"].synchronised is False
    assert summary.input_ports["req_in"].sync_depth is None
    assert stats.analysed_modules == frozenset()
    assert stats.lifted == ()


# --------------------------------------------------------------------------
# (i, negative) a chain that is not there, or is bypassed, is NOT trusted
# --------------------------------------------------------------------------


def test_broken_input_sync_is_never_marked_synchronised() -> None:
    """One flop is not a synchroniser, and a two-flop chain the input
    ALSO bypasses is not one either. Both must fail the proof: this is the
    soundness asymmetry — over-reporting is fine, trusting a synchroniser
    that is not there is not."""
    _flat, grey, _stub, sdc = _paths("bbx_input_sync_broken")
    _top, _spec, boundaries, stats = _boundaries(grey, sdc)
    assert stats.analysed_modules == frozenset({"oneff", "bypass"})

    one = boundaries["u_oneff"].input_ports["req_in"]
    assert one.synchronised is False
    assert one.sync_depth == 1  # measured, and honestly reported as short

    byp = boundaries["u_bypass"].input_ports["req_in"]
    assert byp.synchronised is False
    # Two readers of the port bit: the proof is refused outright rather
    # than measuring the chain that happens to exist next to the bypass.
    assert byp.sync_depth is None


def test_broken_input_sync_fires_cdc001_like_the_flat_run() -> None:
    _flat, grey, stub, sdc = _paths("bbx_input_sync_broken")
    flat_r, grey_r, stub_r = (_report(p, sdc) for p in (_flat, grey, stub))
    assert _rule_ids(flat_r) == Counter({"CDC-001": 2})
    assert _rule_ids(grey_r) == Counter({"CDC-001": 2})
    # Unchanged from the pre-#261 behaviour, which was already correct
    # here — the analysis only ever removes a finding it can disprove.
    assert _rule_ids(stub_r) == _rule_ids(grey_r)


# --------------------------------------------------------------------------
# (ii) reconvergence: lifted to the boundary, matching the flat run
# --------------------------------------------------------------------------


def test_reconvergence_is_raised_at_the_boundary_with_parity() -> None:
    """#259 FIX 3's decline case, now analysed. The flat run fires CDC-005;
    the abstracted run fires the SAME CDC-005 at the boundary and emits no
    CDC-BBX at all, because the reconvergence the star-collapse severs is
    recorded per module and re-raised here."""
    flat, grey, stub, sdc = _paths("reconvergence_two_inputs")
    flat_r, grey_r, stub_r = (_report(p, sdc) for p in (flat, grey, stub))

    assert _rule_ids(flat_r) == Counter({"CDC-005": 1})
    assert _rule_ids(grey_r) == Counter({"CDC-005": 1})
    assert grey_r["summary"]["crossings"] == flat_r["summary"]["crossings"] == 2
    (v,) = grey_r["violations"]
    assert v["severity"] == "warning"
    assert "INSIDE abstracted boundary `u_recon`" in v["message"]
    assert "a_in, b_in" in v["message"]

    # The pre-#261 result on the same design: declined, opaque, no CDC-005.
    assert _rule_ids(stub_r) == Counter({"CDC-BBX": 1})


def test_reconvergence_gate_stands_down_only_for_analysed_instances() -> None:
    """The gate is retired *per instance*, keyed on the summary carrying an
    analysis — not globally. A stub blackbox keeps the #259 decline."""
    _flat, grey, stub, sdc = _paths("reconvergence_two_inputs")
    for path, expect_unsafe in ((grey, set()), (stub, {"u_recon"})):
        top, _spec, boundaries, _stats = _boundaries(path, sdc)
        spec = sdc_mod.parse_file(sdc)
        sdc_mod.synthesize_unconstrained_inputs(spec, top)
        crossings = find_crossings(
            top,
            port_clock=spec.port_clock,
            pin_clocks=spec.pin_clocks,
            clock_for_port=spec.clock_for_port,
            boundaries=boundaries,
        )
        assert reconvergence_unsafe_instances(crossings, boundaries) == expect_unsafe
        # Without the boundary map the gate keeps its unconditional #259
        # behaviour, so legacy callers are unaffected.
        assert reconvergence_unsafe_instances(crossings) == {"u_recon"}


def test_reconvergent_input_pairs_are_recorded_on_the_summary() -> None:
    _flat, grey, _stub, sdc = _paths("reconvergence_two_inputs")
    _top, _spec, boundaries, _stats = _boundaries(grey, sdc)
    assert boundaries["u_recon"].reconvergent_inputs == frozenset(
        {frozenset({"a_in", "b_in"})}
    )


# --------------------------------------------------------------------------
# (iii) analyse ONCE: an internal finding is lifted once, naming instances
# --------------------------------------------------------------------------


def test_internal_violation_lifted_once_for_two_instances() -> None:
    """Two instances of one dual-clock IP with a 2-deep internal chain.
    Flat reports the short chain twice (once per inlined copy); the
    compositional run analyses the module ONCE and reports it ONCE,
    naming both instances. Same hazard, no per-instance repetition."""
    flat, grey, stub, sdc = _paths("bbx_shared_internal_violation")
    flat_r = _report(flat, sdc, "--sync-depth", "3")
    grey_r = _report(grey, sdc, "--sync-depth", "3")
    stub_r = _report(stub, sdc, "--sync-depth", "3")

    assert _rule_ids(flat_r) == Counter({"CDC-002": 2})
    assert _rule_ids(grey_r) == Counter({"CDC-002": 1})
    (v,) = grey_r["violations"]
    assert "[inside `xsync` — analysed once, 2 instances: u_a, u_b]" in v["message"]
    assert "found 2 flop(s), required >= 3" in v["message"]
    # ``cell_name`` re-anchors on a PARENT cell so the finding resolves to
    # a source location and is waivable by boundary instance.
    assert v["cell_name"] == "u_a"

    # Pre-#261: two CDC-BBX errors and zero coverage of the real hazard.
    assert _rule_ids(stub_r) == Counter({"CDC-BBX": 2})


def test_internal_violation_is_waivable_by_boundary_instance(tmp_path) -> None:
    _flat, grey, _stub, sdc = _paths("bbx_shared_internal_violation")
    waiver = tmp_path / "w.swl"
    waiver.write_text("waive CDC-002 u_a  reviewed IP\n")
    run = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(grey),
            "-s",
            str(sdc),
            "--sync-depth",
            "3",
            "--waivers",
            str(waiver),
            "-f",
            "json",
        ],
    )
    rep = json.loads(run.output)
    assert rep["summary"]["violations"] == 0
    assert rep["summary"]["suppressed"] == 1
    assert run.exit_code == 0


def test_shared_module_is_analysed_once_not_per_instance() -> None:
    """The analyse-once contract, proven structurally: one cache entry, one
    lifted record, both instances named, and a cache hit recorded."""
    _flat, grey, _stub, sdc = _paths("bbx_shared_internal_violation")
    _top, _spec, boundaries, stats = _boundaries(grey, sdc, required_depth=3)
    assert set(boundaries) == {"u_a", "u_b"}
    assert stats.cache_hits == 1
    assert len(stats.lifted) == 1
    (entry,) = stats.lifted
    assert entry.module == "xsync"
    assert entry.instances == ("u_a", "u_b")
    assert len(stats.lifted_violations()) == 1
    assert stats.internal_crossings() == 1


def test_lift_message_elides_a_long_instance_list() -> None:
    """A 50-instance mesh must not produce a 50-name message."""
    from rtl_buddy_cdc.compositional import ModuleAnalysis
    from rtl_buddy_cdc.hierarchy import LiftedAnalysis
    from rtl_buddy_cdc.rules import Violation

    entry = LiftedAnalysis(
        module="tile",
        instances=tuple(f"u{i}" for i in range(9)),
        analysis=ModuleAnalysis(
            module="tile",
            violations=(
                Violation(rule_id="CDC-001", severity="error", message="boom"),
            ),
        ),
    )
    (v,) = entry.lifted_violations()
    assert "u0, u1, u2, u3, +5 more" in v.message
    assert "9 instances" in v.message
    assert v.cell_name == "u0"
    # A record with no instances lifts nothing (defensive).
    assert LiftedAnalysis("tile", (), entry.analysis).lifted_violations() == []


# --------------------------------------------------------------------------
# (iv) the multi-clock decline is lifted — with coverage
# --------------------------------------------------------------------------


def test_multi_clock_block_abstracts_with_coverage() -> None:
    """The #259 FIX-1 fixture. Its internal wr_clk -> rd_clk bus crossing
    was the thing that must never vanish; #259 protected it with a
    decline. Now the module is analysed on its own internals and the SAME
    CDC-004 is reported — no decline, no CDC-BBX, no lost hazard."""
    flat, grey, stub, sdc = _paths("multi_clock_blackbox")
    flat_r, grey_r, stub_r = (_report(p, sdc) for p in (flat, grey, stub))

    assert _rule_ids(flat_r) == Counter({"CDC-004": 1})
    assert _rule_ids(grey_r) == Counter({"CDC-004": 1})
    (v,) = grey_r["violations"]
    assert v["severity"] == "error"
    assert "[inside `afifo` — analysed once, 1 instance: u_afifo]" in v["message"]
    assert "wr_clk → rd_clk" in v["message"]

    # Pre-#261: the crossing is not analysed, only its absence flagged.
    assert _rule_ids(stub_r) == Counter({"CDC-BBX": 1})


def test_multi_clock_summary_is_per_port() -> None:
    """The star-collapse becomes per-domain: the write-side data input is
    captured in ``wr_clk`` and the read-side output launched from
    ``rd_clk``, instead of one hub domain for the whole block (which is
    what made a multi-clock summary impossible before)."""
    _flat, grey, _stub, sdc = _paths("multi_clock_blackbox")
    _top, _spec, boundaries, stats = _boundaries(grey, sdc)
    summary = boundaries["u_afifo"]
    assert summary.internal_analysed is True
    assert summary.input_ports["wdata"].src_clock == "wr_clk"
    assert summary.ports["rdata"].src_clock == "rd_clk"
    # No single whole-block clock is claimed for a multi-clock summary.
    assert summary.clock is None
    assert stats.declined_modules == frozenset()


def test_multi_clock_fifo_regression_stays_green_on_the_stub() -> None:
    """The #253 silent-drop regression, re-pinned. With no internals the
    dual-clock IP is still DECLINED and still fails the run by default —
    a crossing through an abstracted block never vanishes quietly."""
    _flat, _grey, stub, sdc = _paths("multi_clock_blackbox")
    top, blackboxes, spec = _load(stub, sdc)
    boundaries, stats = compose_boundaries(top, blackboxes, spec)
    assert boundaries == {}
    assert "afifo" in stats.declined_modules
    assert stats.analysed_modules == frozenset()
    run = runner.invoke(app, ["analyze", "-n", str(stub), "-s", str(sdc)])
    assert run.exit_code == 1


# --------------------------------------------------------------------------
# (vi) #273 clock-output decline must survive compositional analysis
# --------------------------------------------------------------------------


def test_clock_output_decline_survives_with_internals() -> None:
    """Analysing a module's internals says nothing about the clock network
    it forwards to its PARENT. Blackboxing a clock-forwarding tile still
    elides that network, so the #273 decline stands — greybox or not, and
    ahead of any compositional work."""
    d = FIX / "clock_output_blackbox"
    sdc = d / "clock_output_blackbox.sdc"
    grey = d / "clock_output_blackbox.grey.json"
    stub = d / "clock_output_blackbox.json"

    for path in (grey, stub):
        top, blackboxes, spec = _load(path, sdc)
        boundaries, stats = compose_boundaries(top, blackboxes, spec)
        assert "clkfwd_tile" in stats.declined_modules
        assert "clkfwd_tile" not in stats.analysed_modules
        assert set(boundaries) == {"u_core"}
        report = _report(path, sdc)
        bbx = [v for v in report["violations"] if v["rule_id"] == "CDC-BBX"]
        assert len(bbx) == 2
        assert all("drives a clock output `clk_out`" in v["message"] for v in bbx)


# --------------------------------------------------------------------------
# no regressions on the already-abstracted single-clock shapes
# --------------------------------------------------------------------------


def test_single_clock_greybox_matches_the_stub_result() -> None:
    """A single-clock subtree already abstracted cleanly before #261. The
    greybox must produce the SAME summary shape (one whole-block domain on
    every port) so the compositional pass adds coverage without moving any
    existing result."""
    for name in ("single_clock_leaf_abstract", "safe_single_input"):
        _flat, grey, stub, sdc = _paths(name)
        grey_r, stub_r = _report(grey, sdc), _report(stub, sdc)
        assert _rule_ids(grey_r) == _rule_ids(stub_r), name
        for key in ("violations", "crossings", "async_crossings"):
            assert grey_r["summary"][key] == stub_r["summary"][key], (name, key)


def test_single_clock_summary_keeps_the_whole_block_domain() -> None:
    _flat, grey, _stub, sdc = _paths("safe_single_input")
    _top, _spec, boundaries, _stats = _boundaries(grey, sdc)
    summary = boundaries["u_oneport"]
    assert summary.clock == "clk_d"
    assert summary.input_ports["d_in"].src_clock == "clk_d"
    assert summary.ports["d_out"].src_clock == "clk_d"
    # A 4-bit bus is never granted the synchroniser proof: its correctness
    # pattern is gray coding / gating (CDC-004), not a per-bit 2FF chain,
    # and claiming it was handled would suppress the bus crossing.
    assert summary.input_ports["d_in"].synchronised is False
    assert summary.input_ports["d_in"].sync_depth is None


def test_shared_subtree_greybox_still_analyses_once() -> None:
    _flat, grey, stub, sdc = _paths("shared_subtree_compose")
    grey_r, stub_r = _report(grey, sdc), _report(stub, sdc)
    assert _rule_ids(grey_r) == _rule_ids(stub_r) == Counter({"CDC-004": 2})
    _top, _spec, boundaries, stats = _boundaries(grey, sdc)
    assert stats.cache_hits == 1
    assert stats.analysed_modules == frozenset({"pipe"})
    assert stats.lifted == ()  # nothing to report inside a clean pipeline


# --------------------------------------------------------------------------
# the pure per-module pass, directly
# --------------------------------------------------------------------------


def test_analyse_module_declines_a_bodyless_stub() -> None:
    _flat, _grey, stub, sdc = _paths("bbx_input_sync")
    top, blackboxes, spec = _load(stub, sdc)
    ic = _instance_clocks(top, top.cells["u_sync"], blackboxes["ctrl_sync"], spec=spec)
    assert analyse_module(blackboxes["ctrl_sync"], ic.pin_root_map(), spec) is None


def test_analyse_module_reports_ports_and_resolution() -> None:
    _flat, grey, _stub, sdc = _paths("bbx_shared_internal_violation")
    top, blackboxes, spec = _load(grey, sdc)
    ic = _instance_clocks(top, top.cells["u_a"], blackboxes["xsync"], spec=spec)
    assert ic.pin_root_map() == {"clk_s": "clk_s", "clk_c": "clk_c"}
    result = analyse_module(blackboxes["xsync"], ic.pin_root_map(), spec)
    assert result is not None
    assert result.resolved is True
    assert result.clock_roots == frozenset({"clk_s", "clk_c"})
    assert result.crossings == 1
    # Per-port attribution: captured on the source clock, launched on the
    # destination clock, and the output IS a proven synchroniser tail.
    assert result.ports["d_in"].clock == "clk_s"
    assert result.ports["d_out"].clock == "clk_c"
    assert result.ports["d_out"].synchronised is True
    # Clock pins are never described as data ports.
    assert "clk_s" not in result.ports


def test_derive_sub_spec_projects_the_parents_clock_partition() -> None:
    """The derived spec re-declares each parent clock ROOT on the subtree
    pin that carries it — so ``are_async`` gives the same answer on both
    sides of the boundary — while dropping the parent's own port lists, so
    a subtree data port sharing a name with a parent clock port is never
    mistaken for a clock."""
    _flat, grey, _stub, sdc = _paths("bbx_shared_internal_violation")
    _top, blackboxes, spec = _load(grey, sdc)
    sub_spec = derive_sub_spec(spec, {"clk_s": "clk_s", "clk_c": "clk_c"})
    assert sub_spec.are_async("clk_s", "clk_c") is True
    assert sub_spec.clock_for_port("clk_s") == "clk_s"
    # Port typings do not travel: a hierarchy pin is not a chip pin.
    assert sub_spec.port_clock == {}
    assert sub_spec.clock_for_port("d0") is None
    assert blackboxes["xsync"].cells  # sanity: this really is a greybox


def test_derive_sub_spec_declares_an_undeclared_traced_root() -> None:
    """A traced root the SDC never named still becomes a clock in the
    subtree's spec, so the internal flops land in a named domain instead
    of going domain-unknown (which would decline the whole module)."""
    spec = sdc_mod.ClockSpec()
    sub_spec = derive_sub_spec(spec, {"clk": "some_root", "unused": None})
    assert sub_spec.clock_for_port("clk") == "some_root"
    assert sub_spec.clock_for_port("unused") is None


def test_top_level_port_rules_are_excluded_from_the_internal_pass() -> None:
    """CDC-006 / CDC-011 / RDC-008 are defined on the DESIGN's top-level
    ports. At module scope a port is a hierarchy pin, so they fire on
    every correctly-built IP — CDC-006 in particular calls a textbook
    boundary input synchroniser a glitch hazard. They are excluded, and
    the hazard they are really about is reported by the parent as the
    boundary-sink crossing at that same port."""
    assert INTERNAL_RULE_EXCLUSIONS == frozenset({"CDC-006", "CDC-011", "RDC-008"})
    _flat, grey, _stub, sdc = _paths("bbx_input_sync")
    top, blackboxes, spec = _load(grey, sdc)
    ic = _instance_clocks(top, top.cells["u_sync"], blackboxes["ctrl_sync"], spec=spec)
    result = analyse_module(blackboxes["ctrl_sync"], ic.pin_root_map(), spec)
    assert result is not None
    assert [v.rule_id for v in result.violations] == []


# --------------------------------------------------------------------------
# what STILL declines, and why — a conservative decline with a diagnostic
# is acceptable; silence is not
# --------------------------------------------------------------------------


def test_unresolved_internal_clock_declines_and_was_a_silent_drop_before() -> None:
    """`oddclk` is the case pin inspection CANNOT see. Only ``clk_a`` is
    recognised as a clock pin, so the pre-#261 summariser called the block
    single-clock and abstracted it — and its real clk_a -> `strobe`
    crossing vanished with **no diagnostic at all**. With the internals in
    hand the second flop lands on no parent clock root, and the module is
    declined loudly instead."""
    _flat, grey, stub, sdc = _paths("bbx_residual_declines")

    # Pre-#261: silently abstracted, no finding of any kind for u_odd.
    top, blackboxes, spec = _load(stub, sdc)
    _b, stub_stats = compose_boundaries(top, blackboxes, spec)
    assert "oddclk" not in stub_stats.declined_modules
    stub_r = _report(stub, sdc)
    assert not any("u_odd" in v["message"] for v in stub_r["violations"])

    # #261: declined, with a message that names the real cause.
    top, blackboxes, spec = _load(grey, sdc)
    boundaries, stats = compose_boundaries(top, blackboxes, spec)
    assert "oddclk" in stats.declined_modules
    assert stats.unresolved_internal_modules == frozenset({"oddclk"})
    assert "u_odd" not in boundaries
    grey_r = _report(grey, sdc)
    (bbx,) = [
        v
        for v in grey_r["violations"]
        if v["rule_id"] == "CDC-BBX" and "u_odd" in v["message"]
    ]
    assert "could not resolve to a declared clock" in bbx["message"]
    assert bbx["severity"] == "error"


def test_ambiguous_input_capture_declines_with_its_own_reason() -> None:
    """`twocap` captures one input in TWO internal domains. A single
    virtual sink cannot represent both and picking either would drop the
    other's crossing, so the module keeps its decline — with a message
    that says exactly that rather than the generic multi-clock one."""
    _flat, grey, _stub, sdc = _paths("bbx_residual_declines")
    top, blackboxes, spec = _load(grey, sdc)
    boundaries, stats = compose_boundaries(top, blackboxes, spec)
    assert stats.ambiguous_input_modules == frozenset({"twocap"})
    assert "u_two" not in boundaries

    report = _report(grey, sdc)
    (bbx,) = [
        v
        for v in report["violations"]
        if v["rule_id"] == "CDC-BBX" and "u_two" in v["message"]
    ]
    assert "captured in more than one internal clock domain" in bbx["message"]

    # The residual declines stay waivable exactly like every other flavour.
    assert all(
        v["rule_id"] == "CDC-BBX"
        for v in report["violations"]
        if "u_two" in v["message"]
    )


def test_uncaptured_input_seeds_no_sink_but_the_block_is_summarised() -> None:
    """`feedthru` is the positive control: dual-clock, summarised per port,
    and its `sel` input — which nothing sequential captures — seeds no
    virtual sink. There is no crossing INTO the block there; what `sel`
    gates leaves through `y`, which is seeded as its own source."""
    _flat, grey, _stub, sdc = _paths("bbx_residual_declines")
    _top, _spec, boundaries, stats = _boundaries(grey, sdc)
    summary = boundaries["u_thru"]
    assert summary.internal_analysed is True
    assert set(summary.input_ports) == {"d_in"}
    assert summary.input_ports["d_in"].src_clock == "clk_a"
    assert summary.ports["y"].src_clock == "clk_b"
    assert "feedthru" in stats.analysed_modules

    # Its internal crossing is reported, matching the flat run's finding
    # on the same logic.
    flat_r, grey_r = _report(_flat, sdc), _report(grey, sdc)
    lifted = [v for v in grey_r["violations"] if "[inside `feedthru`" in v["message"]]
    assert [v["rule_id"] for v in lifted] == ["CDC-001"]
    assert any(
        v["rule_id"] == "CDC-001" and "u_thru" in v["message"]
        for v in flat_r["violations"]
    )


def test_single_domain_collapse_rules() -> None:
    """The pure collapse used for every port attribution: one known domain
    is the answer, nothing at all is 'no capture', and anything mixed is
    ambiguous (never a silent pick)."""
    from rtl_buddy_cdc.compositional import _single_domain

    assert _single_domain({"clk_a"}) == ("clk_a", False)
    assert _single_domain(set()) == (None, False)
    assert _single_domain({None}) == (None, False)
    assert _single_domain({"clk_a", "clk_b"}) == (None, True)
    assert _single_domain({"clk_a", None}) == (None, True)


# --------------------------------------------------------------------------
# the synchroniser proof, defeater by defeater
# --------------------------------------------------------------------------


def _dff(name: str, clk, d, q) -> netlist.Cell:
    return netlist.Cell(
        name=name, type="$dff", connections={"CLK": clk, "D": d, "Q": q}
    )


def _port(name, direction, bits) -> netlist.Port:
    return netlist.Port(name=name, direction=direction, bits=bits)


def _defeater_module() -> netlist.Module:
    """A greybox whose every data input defeats the synchroniser proof in
    a different, realistic way — and one output that carries the user's
    explicit promise instead of a structural proof.

    - ``tied``    : the port bit is a constant, so nothing captures it.
    - ``thru``    : the bit is ALSO an output-port bit, so the raw value
                    leaves the block unsynchronised.
    - ``wide_in`` : lands on lane 0 of a multi-bit flop — a bus register,
                    not a 1-bit synchroniser stage.
    - ``odd_in``  : its capture flop's CLK resolves to nothing, so its
                    domain is unknown and nothing about it is proven.
    - ``u_out``   : driven by a flop tagged ``(* async_reg *)`` — the
                    "explicitly promised" route §4.9 sanctions, honoured
                    exactly as the flat rules honour the same tag.
    - ``nc_out``  : a zero-width output; no bits, nothing to prove.
    """
    return netlist.Module(
        name="defeat",
        ports={
            "clk": _port("clk", "input", (1,)),
            "tied": _port("tied", "input", ("0",)),
            "thru": _port("thru", "input", (2,)),
            "thru_out": _port("thru_out", "output", (2,)),
            "wide_in": _port("wide_in", "input", (3,)),
            "wide_out": _port("wide_out", "output", (4, 5)),
            "odd_in": _port("odd_in", "input", (7,)),
            "odd_out": _port("odd_out", "output", (9,)),
            "u_out": _port("u_out", "output", (11,)),
            "nc_out": _port("nc_out", "output", ()),
        },
        cells={
            "f_wide": _dff("f_wide", (1,), (3, 6), (4, 5)),
            # CLK bit 8 is driven by nothing and is not a port: unknown.
            "f_odd": _dff("f_odd", (8,), (7,), (9,)),
            "f_user": _dff("f_user", (1,), (10,), (11,)),
        },
        netnames={
            "u_out": netlist.Netname(
                name="u_out",
                bits=(11,),
                attributes={"ASYNC_REG": "00000000000000000000000000000001"},
            )
        },
        is_blackbox=True,
    )


def test_every_defeater_refuses_the_input_synchroniser_proof() -> None:
    spec = sdc_mod.ClockSpec()
    spec.clocks["clk_a"] = sdc_mod.Clock(name="clk_a", period=10.0, ports=("clk_a",))
    result = analyse_module(_defeater_module(), {"clk": "clk_a"}, spec)
    assert result is not None
    for port in ("tied", "thru", "wide_in", "odd_in"):
        facts = result.ports[port]
        assert facts.sync_depth is None, port
        assert facts.synchronised is False, port
    # The unknown-domain flop also fails the resolution gate, so the
    # module as a whole would be declined — a decline, never a silent
    # abstraction.
    assert result.resolved is False
    assert "f_odd" in result.unresolved_flops


def test_user_tagged_output_tail_is_the_promised_route() -> None:
    """``(* async_reg *)`` on the last register before a port is the user
    asserting the synchroniser §4.9 calls route (b). It is accepted where
    the structural proof (a preceding same-domain stage) is absent — and a
    plain, untagged register with nothing behind it is not."""
    spec = sdc_mod.ClockSpec()
    spec.clocks["clk_a"] = sdc_mod.Clock(name="clk_a", period=10.0, ports=("clk_a",))
    result = analyse_module(_defeater_module(), {"clk": "clk_a"}, spec)
    assert result is not None
    assert result.ports["u_out"].user_synchronised is True
    assert result.ports["u_out"].synchronised is True
    # Same shape, no tag, no preceding stage -> not synchronised.
    assert result.ports["wide_out"].synchronised is False
    # A zero-width output proves nothing either.
    assert result.ports["nc_out"].synchronised is False


def test_multi_clock_module_with_no_internal_registers_declines() -> None:
    """Two clock pins but nothing clocked: the module has no internal
    domain to attribute any port to, so a per-port summary would be pure
    invention. Declined."""
    from rtl_buddy_cdc.abstract import summarise_subtree

    sub = netlist.Module(
        name="combo",
        ports={
            "clk_a": _port("clk_a", "input", (1,)),
            "clk_b": _port("clk_b", "input", (2,)),
            "d": _port("d", "input", (3,)),
            "y": _port("y", "output", (4,)),
        },
        cells={
            "g": netlist.Cell(name="g", type="$and", connections={"A": (3,), "Y": (4,)})
        },
        netnames={},
        is_blackbox=True,
    )
    parent = netlist.Module(
        name="top",
        ports={
            "clk_a": _port("clk_a", "input", (1,)),
            "clk_b": _port("clk_b", "input", (2,)),
        },
        cells={
            "u": netlist.Cell(
                name="u",
                type="combo",
                connections={"clk_a": (1,), "clk_b": (2,), "d": (3,), "y": (4,)},
            )
        },
        netnames={},
    )
    spec = sdc_mod.ClockSpec()
    spec.clocks["clk_a"] = sdc_mod.Clock(name="clk_a", period=10.0, ports=("clk_a",))
    spec.clocks["clk_b"] = sdc_mod.Clock(name="clk_b", period=7.0, ports=("clk_b",))
    spec.async_groups.append([{"clk_a"}, {"clk_b"}])
    ic = _instance_clocks(parent, parent.cells["u"], sub, spec=spec)
    assert ic.roots == frozenset({"clk_a", "clk_b"})
    analysis = analyse_module(sub, ic.pin_root_map(), spec)
    assert analysis is not None and analysis.resolved and not analysis.clock_roots
    assert (
        summarise_subtree(parent, parent.cells["u"], sub, spec, analysis=analysis)
        is None
    )
