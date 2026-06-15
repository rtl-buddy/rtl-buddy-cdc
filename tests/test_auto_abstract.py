"""P2 auto-abstract single-clock subtrees (#256).

Covers the rtl-buddy-cdc side end to end without requiring a yosys
toolchain at test time:

- 2a — the single-clock-subtree *detector* (``abstract.is_single_clock_subtree``):
  empty / singleton / synchronous-multi / async-multi / unknown-clock cases.
- 2b — the *summariser* (``abstract.summarise_subtree``) and the boundary
  re-seeding in ``find_crossings`` (an abstracted subtree's output port
  becomes a virtual source).
- 2c — the committed ``single_clock_leaf_abstract`` fixture pair proving
  the safety property: the FLATTENED design (``pipe`` inlined) and the
  AUTO-ABSTRACTED design (``pipe`` blackboxed + summarised) produce
  IDENTICAL violations and identical ``summary.*`` — with strictly fewer
  flops walked in the abstracted run (the scaling win).
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy_cdc import abstract, netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.netlist import BoundarySummary, Cell, Module, Port
from rtl_buddy_cdc.sdc import ClockSpec

runner = CliRunner()

FIX_DIR = Path(__file__).parent / "fixtures" / "single_clock_leaf_abstract"
BB_JSON = FIX_DIR / "single_clock_leaf_abstract.json"
FLAT_JSON = FIX_DIR / "single_clock_leaf_abstract.flat.json"
SDC = FIX_DIR / "single_clock_leaf_abstract.sdc"


# --------------------------------------------------------------------------
# 2a — single-clock-subtree detector
# --------------------------------------------------------------------------


def _async_spec() -> ClockSpec:
    spec = ClockSpec()
    spec.async_groups.append([{"clk_a"}, {"clk_b"}])
    return spec


def test_detector_empty_set_is_single_clock() -> None:
    """A purely combinational subtree (no clocks) carries no crossing."""
    assert abstract.is_single_clock_subtree(set(), _async_spec()) is True


def test_detector_singleton_is_single_clock() -> None:
    assert abstract.is_single_clock_subtree({"clk_a"}, _async_spec()) is True


def test_detector_async_pair_is_not_single_clock() -> None:
    """Two clocks declared async to each other => the subtree DOES carry
    a crossing, so it must not be abstracted away."""
    assert abstract.is_single_clock_subtree({"clk_a", "clk_b"}, _async_spec()) is False


def test_detector_synchronous_generated_pair_is_single_clock() -> None:
    """A generated clock and its master resolve to one root => single
    domain, no internal crossing."""
    spec = ClockSpec()
    spec.clocks["clk"] = sdc_mod.Clock(name="clk", period=10.0, ports=("clk",))
    spec.clocks["clk_div2"] = sdc_mod.Clock(
        name="clk_div2", period=20.0, ports=(), master="clk", is_generated=True
    )
    assert abstract.is_single_clock_subtree({"clk", "clk_div2"}, spec) is True


def test_detector_unrelated_roots_not_single_clock() -> None:
    """Two clocks with different roots, not declared async: we can't
    prove they're the same domain, so be conservative and refuse."""
    spec = ClockSpec()
    spec.clocks["clk_x"] = sdc_mod.Clock(name="clk_x", period=10.0, ports=("clk_x",))
    spec.clocks["clk_y"] = sdc_mod.Clock(name="clk_y", period=10.0, ports=("clk_y",))
    assert abstract.is_single_clock_subtree({"clk_x", "clk_y"}, spec) is False


def test_detector_none_clock_is_not_single_clock() -> None:
    """An unknown (None / empty-string) clock in the set means a domain
    we couldn't pin down — never abstract, to avoid dropping a crossing."""
    assert abstract.is_single_clock_subtree({""}, _async_spec()) is False
    assert abstract.is_single_clock_subtree({"clk_a", ""}, _async_spec()) is False


def test_detector_exclusive_pair_not_single_clock() -> None:
    """Mutually-exclusive clocks are different domains; not abstractable."""
    spec = ClockSpec()
    spec.exclusive_groups.append([{"clk_a"}, {"clk_b"}])
    assert abstract.is_single_clock_subtree({"clk_a", "clk_b"}, spec) is False


# --------------------------------------------------------------------------
# 2b — summariser + boundary find_crossings seeding (hand-built)
# --------------------------------------------------------------------------


def _clocked_boundary_parent() -> tuple[Module, Cell, Module]:
    """A tiny parent with one clk_b flop fed by a blackbox ``sub``
    instance whose clock pin is driven by ``clk_a``.

    bits: clk_a=1, clk_b=2, sub.d_out=3, dst_q.D=3, dst_q.Q=4
    """
    sub_inst = Cell(
        name="u_sub",
        type="sub",
        connections={"clk": (1,), "d_out": (3,)},
    )
    dst = Cell(
        name="dst_q",
        type="$dff",
        connections={"CLK": (2,), "D": (3,), "Q": (4,)},
    )
    parent = Module(
        name="top",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(1,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(2,)),
        },
        cells={"u_sub": sub_inst, "dst_q": dst},
        netnames={},
    )
    sub = Module(
        name="sub",
        ports={
            "clk": Port(name="clk", direction="input", bits=()),
            "d_out": Port(name="d_out", direction="output", bits=()),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    return parent, sub_inst, sub


def test_summarise_subtree_single_clock() -> None:
    """A blackbox whose clock pin traces to clk_a is summarised: its
    output port carries src_clock=clk_a, synchronised=False."""
    parent, inst, sub = _clocked_boundary_parent()
    summary = abstract.summarise_subtree(parent, inst, sub, _async_spec())
    assert summary is not None
    assert summary.module == "sub"
    assert set(summary.ports) == {"d_out"}
    pb = summary.ports["d_out"]
    assert pb.src_clock == "clk_a"
    assert pb.synchronised is False


def test_summarise_subtree_data_only_unconstrained_source() -> None:
    """A data-only boundary (no clock pin) is trivially single-clock; its
    output is an unconstrained (None) source — conservative async."""
    inst = Cell(name="u_sub", type="sub", connections={"d_out": (3,)})
    parent = Module(
        name="top",
        ports={},
        cells={"u_sub": inst},
        netnames={},
    )
    sub = Module(
        name="sub",
        ports={"d_out": Port(name="d_out", direction="output", bits=())},
        cells={},
        netnames={},
        is_blackbox=True,
    )
    summary = abstract.summarise_subtree(parent, inst, sub, _async_spec())
    assert summary is not None
    assert summary.ports["d_out"].src_clock is None


def test_boundary_crossing_seeded_as_virtual_source() -> None:
    """find_crossings re-seeds an abstracted subtree's output port as a
    virtual source: clk_a (subtree) -> dst_q (clk_b) is a crossing with
    ``src_boundary`` set and no ``src_flop``."""
    parent, inst, sub = _clocked_boundary_parent()
    summary = BoundarySummary(
        module="sub",
        ports={
            "d_out": netlist.PortBoundary(
                port="d_out", src_clock="clk_a", synchronised=False, width=1
            )
        },
    )
    crossings = find_crossings(parent, boundaries={"sub": summary})
    assert len(crossings) == 1
    c = crossings[0]
    assert c.src_boundary == ("u_sub", "d_out")
    assert c.src_flop is None
    assert c.is_boundary_sourced is True
    assert c.src_clock == "clk_a"
    assert c.dst_clock == "clk_b"
    assert c.dst_flop.name == "dst_q"
    assert "boundary u_sub.d_out" in c.src_name


def test_boundary_crossing_through_comb_logic() -> None:
    """A boundary output reaching a foreign-domain flop THROUGH a
    combinational cell is still a crossing; the walk propagates hops and
    collapses the multi-bit bus to one Crossing record."""
    # bits: clk_a=1, clk_b=2, sub.d_out=[3,4], inv.Y=[5,6], dst.D=[5,6]
    sub_inst = Cell(
        name="u_sub", type="sub", connections={"clk": (1,), "d_out": (3, 4)}
    )
    inv = Cell(name="inv", type="$not", connections={"A": (3, 4), "Y": (5, 6)})
    dst = Cell(
        name="dst_q", type="$dff", connections={"CLK": (2,), "D": (5, 6), "Q": (7, 8)}
    )
    parent = Module(
        name="top",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(1,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(2,)),
        },
        cells={"u_sub": sub_inst, "inv": inv, "dst_q": dst},
        netnames={},
    )
    summary = BoundarySummary(
        module="sub",
        ports={
            "d_out": netlist.PortBoundary(
                port="d_out", src_clock="clk_a", synchronised=False, width=2
            )
        },
    )
    crossings = find_crossings(parent, boundaries={"sub": summary})
    assert len(crossings) == 1
    c = crossings[0]
    assert c.src_boundary == ("u_sub", "d_out")
    assert c.width == 2
    assert c.min_hops == 1  # one comb cell between boundary and the flop D


def test_boundary_walk_skips_scopeinfo_and_respects_max_hops() -> None:
    """The boundary walk skips ``$scopeinfo`` transit cells, drops a
    constant (non-int) output bit, and stops at ``max_hops`` so a sink
    deeper than the budget is not reported."""
    # boundary out=10 -> $scopeinfo (skipped) ; out=10 -> chain of
    # buffers longer than max_hops to a clk_b flop.
    sub_inst = Cell(name="u_sub", type="sub", connections={"clk": (1,), "d_out": (10,)})
    scope = Cell(name="dbg", type="$scopeinfo", connections={"A": (10,)})
    # buffer chain: 10 -> b1.Y=11 -> b2.Y=12 ; flop captures bit 12.
    b1 = Cell(name="b1", type="$not", connections={"A": (10,), "Y": (11, "0")})
    b2 = Cell(name="b2", type="$not", connections={"A": (11,), "Y": (12,)})
    dst = Cell(
        name="dst_q", type="$dff", connections={"CLK": (2,), "D": (12,), "Q": (13,)}
    )
    parent = Module(
        name="top",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(1,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(2,)),
        },
        cells={"u_sub": sub_inst, "dbg": scope, "b1": b1, "b2": b2, "dst_q": dst},
        netnames={},
    )
    summary = BoundarySummary(
        module="sub",
        ports={
            "d_out": netlist.PortBoundary(
                port="d_out", src_clock="clk_a", synchronised=False, width=1
            )
        },
    )
    # max_hops=1 can't reach a flop 2 buffers deep → no crossing.
    assert find_crossings(parent, max_hops=1, boundaries={"sub": summary}) == []
    # max_hops=2 reaches it.
    (c,) = find_crossings(parent, max_hops=2, boundaries={"sub": summary})
    assert c.dst_flop.name == "dst_q"
    assert c.min_hops == 2


def test_boundary_walk_skips_flop_transit() -> None:
    """A flop on the boundary's fanout is a sink check, never a transit
    node — the walk doesn't tunnel through it to a deeper flop."""
    sub_inst = Cell(name="u_sub", type="sub", connections={"clk": (1,), "d_out": (10,)})
    # clk_a flop captures bit 10 (SAME domain as boundary -> not a
    # crossing) and re-emits bit 11; a clk_b flop captures 11.
    mid = Cell(
        name="mid_q", type="$dff", connections={"CLK": (1,), "D": (10,), "Q": (11,)}
    )
    dst = Cell(
        name="dst_q", type="$dff", connections={"CLK": (2,), "D": (11,), "Q": (12,)}
    )
    parent = Module(
        name="top",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(1,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(2,)),
        },
        cells={"u_sub": sub_inst, "mid_q": mid, "dst_q": dst},
        netnames={},
    )
    summary = BoundarySummary(
        module="sub",
        ports={
            "d_out": netlist.PortBoundary(
                port="d_out", src_clock="clk_a", synchronised=False, width=1
            )
        },
    )
    # The boundary feeds mid_q (clk_a, same domain → no crossing). The
    # walk must NOT tunnel through mid_q to dst_q, so no *boundary*-sourced
    # crossing is reported (the mid_q→dst_q flop crossing the normal walk
    # finds is unrelated to the boundary seeding).
    crossings = find_crossings(parent, boundaries={"sub": summary})
    assert [c for c in crossings if c.is_boundary_sourced] == []


def test_boundary_unconstrained_clock_seeded_against_any_sink() -> None:
    """A boundary whose src_clock is None seeds an unconstrained source
    that crosses to any known-domain sink."""
    sub_inst = Cell(name="u_sub", type="sub", connections={"d_out": (3,)})
    dst = Cell(
        name="dst_q", type="$dff", connections={"CLK": (2,), "D": (3,), "Q": (4,)}
    )
    parent = Module(
        name="top",
        ports={"clk_b": Port(name="clk_b", direction="input", bits=(2,))},
        cells={"u_sub": sub_inst, "dst_q": dst},
        netnames={},
    )
    summary = BoundarySummary(
        module="sub",
        ports={
            "d_out": netlist.PortBoundary(
                port="d_out", src_clock=None, synchronised=False, width=1
            )
        },
    )
    (c,) = find_crossings(parent, boundaries={"sub": summary})
    assert c.src_clock == "<unconstrained>"
    assert c.dst_clock == "clk_b"


def test_boundary_same_domain_sink_is_not_a_crossing() -> None:
    """A boundary output feeding a sink in the SAME domain is not a
    crossing — nothing to seed."""
    parent, inst, sub = _clocked_boundary_parent()
    # Summarise the subtree at clk_b (same as the dst flop).
    summary = BoundarySummary(
        module="sub",
        ports={
            "d_out": netlist.PortBoundary(
                port="d_out", src_clock="clk_b", synchronised=False, width=1
            )
        },
    )
    assert find_crossings(parent, boundaries={"sub": summary}) == []


def _foreign_input_parent() -> tuple[Module, Cell, Module]:
    """A parent where a clk_b flop drives the data input of a clk_a
    single-clock blackbox ``sub`` (the blocker's exact repro).

    bits: clk_a=1, clk_b=2, src_q.D=3 (port d_in), src_q.Q=4,
          sub.d_in=4, sub.d_out=5, dst_q.D=5, dst_q.Q=6
    The async crossing (clk_b src_q -> clk_a subtree input) lands on
    the subtree's first internal flop, INSIDE the input boundary, so the
    output-only boundary summary would drop it. summarise_subtree must
    therefore refuse to abstract this subtree.
    """
    src = Cell(
        name="src_q", type="$dff", connections={"CLK": (2,), "D": (3,), "Q": (4,)}
    )
    sub_inst = Cell(
        name="u_sub", type="sub", connections={"clk": (1,), "d_in": (4,), "d_out": (5,)}
    )
    dst = Cell(
        name="dst_q", type="$dff", connections={"CLK": (1,), "D": (5,), "Q": (6,)}
    )
    parent = Module(
        name="top",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(1,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(2,)),
            "d_in": Port(name="d_in", direction="input", bits=(3,)),
        },
        cells={"src_q": src, "u_sub": sub_inst, "dst_q": dst},
        netnames={},
    )
    sub = Module(
        name="sub",
        ports={
            "clk": Port(name="clk", direction="input", bits=()),
            "d_in": Port(name="d_in", direction="input", bits=()),
            "d_out": Port(name="d_out", direction="output", bits=()),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    return parent, sub_inst, sub


def _foreign_input_spec() -> ClockSpec:
    spec = _async_spec()
    # src_q is a real clk_b flop; assign_domains derives its domain from
    # the CLK pin, so no port typing is required for the foreign source.
    return spec


def test_summarise_seeds_foreign_domain_data_input() -> None:
    """Result-preserving by construction (P3/#257): a clk_b signal driven
    into a clk_a single-clock subtree's DATA INPUT is a crossing the
    flattened design reports at the subtree's first internal flop. The
    summary now carries an INPUT-side virtual sink (``input_ports`` in the
    boundary's ``clock`` domain) so abstracting it preserves the crossing
    rather than dropping it — the subtree IS abstracted."""
    parent, inst, sub = _foreign_input_parent()
    summary = abstract.summarise_subtree(parent, inst, sub, _foreign_input_spec())
    assert summary is not None
    assert summary.clock == "clk_a"
    assert set(summary.ports) == {"d_out"}
    assert set(summary.input_ports) == {"d_in"}


def test_summarise_foreign_input_crossing_seeded_via_find_crossings() -> None:
    """End to end at the find_crossings layer: with the subtree abstracted
    and the input sink seeded, the clk_b -> clk_a crossing INTO the
    boundary is reported (``dst_boundary`` set), restoring parity with the
    flattened design."""
    parent, inst, sub = _foreign_input_parent()
    summary = abstract.summarise_subtree(parent, inst, sub, _foreign_input_spec())
    assert summary is not None
    crossings = find_crossings(parent, boundaries={"u_sub": summary})
    sink = [c for c in crossings if c.dst_boundary is not None]
    assert len(sink) == 1
    c = sink[0]
    assert c.src_clock == "clk_b"
    assert c.dst_clock == "clk_a"
    assert c.dst_boundary == ("u_sub", "d_in")


def test_foreign_input_crossing_present_when_not_abstracted() -> None:
    """The crossing the refusal preserves: with the clk_a flop standing
    in for the subtree's first internal stage (the flattened view), the
    normal flat walk reports the clk_b -> clk_a crossing. This is the
    crossing abstraction would have dropped."""
    parent, _inst, _sub = _foreign_input_parent()
    # Replace the blackbox instance with its first internal clk_a flop to
    # model the flattened subtree: src_q(clk_b).Q=4 -> stage0(clk_a).D=4.
    stage0 = Cell(
        name="u_sub.s0", type="$dff", connections={"CLK": (1,), "D": (4,), "Q": (5,)}
    )
    flat = Module(
        name="top",
        ports=parent.ports,
        cells={
            "src_q": parent.cells["src_q"],
            "u_sub.s0": stage0,
            "dst_q": parent.cells["dst_q"],
        },
        netnames={},
    )
    crossings = find_crossings(flat)
    assert len(crossings) == 1
    c = crossings[0]
    assert c.src_clock == "clk_b"
    assert c.dst_clock == "clk_a"


def test_summarise_unconstrained_data_input_still_abstracts() -> None:
    """An unconstrained (untyped) top-level input feeding a clk_a
    subtree's data input no longer blocks abstraction: the subtree is
    summarised and the input sink is seeded so the unconstrained-domain
    crossing INTO the boundary is preserved by find_crossings."""
    parent, inst, sub = _foreign_input_parent()
    # Drive sub.d_in directly from a top input port typed <unconstrained>.
    inst = Cell(
        name="u_sub", type="sub", connections={"clk": (1,), "d_in": (3,), "d_out": (5,)}
    )
    p2 = Module(
        name="top",
        ports=parent.ports,
        cells={"u_sub": inst, "dst_q": parent.cells["dst_q"]},
        netnames={},
    )
    spec = _async_spec()
    spec.port_clock["d_in"] = sdc_mod.UNCONSTRAINED_SENTINEL
    summary = abstract.summarise_subtree(p2, inst, sub, spec)
    assert summary is not None
    assert set(summary.input_ports) == {"d_in"}
    # The unconstrained-domain input crosses into the clk_a boundary.
    crossings = find_crossings(
        p2, port_clock=spec.port_clock, boundaries={"u_sub": summary}
    )
    sink = [c for c in crossings if c.dst_boundary == ("u_sub", "d_in")]
    assert len(sink) == 1
    assert sink[0].dst_clock == "clk_a"


def test_summarise_allows_same_domain_data_input() -> None:
    """The control case: a clk_a (same-domain) data input does NOT block
    abstraction — the subtree is summarised as before."""
    parent, inst, sub = _foreign_input_parent()
    # Re-clock the source flop to clk_a (same domain as the subtree).
    src = Cell(
        name="src_q", type="$dff", connections={"CLK": (1,), "D": (3,), "Q": (4,)}
    )
    p2 = Module(
        name="top",
        ports=parent.ports,
        cells={"src_q": src, "u_sub": inst, "dst_q": parent.cells["dst_q"]},
        netnames={},
    )
    summary = abstract.summarise_subtree(p2, inst, sub, _async_spec())
    assert summary is not None
    assert set(summary.ports) == {"d_out"}


def test_summarise_subtree_refuses_multiclock(monkeypatch) -> None:
    """When the instance clock resolves to a clock that the detector
    rejects (multi-domain), the summariser returns None — the subtree is
    left to the normal walk, never abstracted."""
    parent, inst, sub = _clocked_boundary_parent()
    # Force the resolved instance clock to look like an async-partitioned
    # set by patching the detector to reject anything.
    monkeypatch.setattr(abstract, "is_single_clock_subtree", lambda *a, **k: False)
    assert abstract.summarise_subtree(parent, inst, sub, _async_spec()) is None


# --------------------------------------------------------------------------
# 2c — fixture safety property: flat vs auto-abstracted identical output
# --------------------------------------------------------------------------


def _analyze_json_with(path: Path, sdc: Path) -> dict:
    result = runner.invoke(
        app,
        ["analyze", "-n", str(path), "-s", str(sdc), "-f", "json"],
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


def _analyze_json(path: Path) -> dict:
    return _analyze_json_with(path, SDC)


def _violation_keys(report: dict) -> list[tuple[str, str, object]]:
    return sorted(
        (v["rule_id"], v["severity"], v.get("cell_name")) for v in report["violations"]
    )


def test_abstracted_matches_flattened_violations() -> None:
    """The safety property: identical violations (rule, severity, anchor
    cell) and identical contract counts between the flattened design and
    the auto-abstracted one."""
    bb = _analyze_json(BB_JSON)
    flat = _analyze_json(FLAT_JSON)

    assert _violation_keys(bb) == _violation_keys(flat)
    for key in ("violations", "suppressed", "crossings", "async_crossings"):
        assert bb["summary"][key] == flat["summary"][key], key

    # The real async crossing is reported in both; abstracted reports it
    # as a boundary-sourced crossing.
    assert bb["summary"]["async_crossings"] == 1
    (bb_cross,) = bb["crossings"]
    assert bb_cross["src_clock"] == "clk_a"
    assert bb_cross["dst_clock"] == "clk_b"
    assert bb_cross["src_boundary"] == {"instance": "u_pipe", "port": "d_out"}


def test_abstraction_reduces_flop_count() -> None:
    """The scaling win: the auto-abstracted run walks strictly fewer
    flops (pipe's internal stages are summarised away)."""
    bb = _analyze_json(BB_JSON)
    flat = _analyze_json(FLAT_JSON)
    assert bb["summary"]["flops"] < flat["summary"]["flops"]
    # pipe's two clk_a stages drop out; only the two top flops remain.
    assert bb["summary"]["flops"] == 2
    assert flat["summary"]["flops"] == 4


def test_fixture_blackbox_summarised_and_clean_of_spurious_cdc008() -> None:
    """The boundary cell's clock pin must NOT trip CDC-008 (clock used as
    data) — that would be a divergence the abstraction introduces."""
    bb = _analyze_json(BB_JSON)
    assert all(v["rule_id"] != "CDC-008" for v in bb["violations"])


def test_fixture_loads_pipe_as_blackbox_sibling() -> None:
    top, blackboxes = netlist.load_with_blackboxes(BB_JSON)
    assert top.name == "top"
    assert set(blackboxes) == {"pipe"}
    assert blackboxes["pipe"].is_blackbox is True
    assert blackboxes["pipe"].cells == {}
    assert top.cells["u_pipe"].type == "pipe"


# --------------------------------------------------------------------------
# 2c — foreign-domain-input fixture: abstraction must be declined
# --------------------------------------------------------------------------

FI_DIR = Path(__file__).parent / "fixtures" / "foreign_input_no_abstract"
FI_BB_JSON = FI_DIR / "foreign_input_no_abstract.json"
FI_FLAT_JSON = FI_DIR / "foreign_input_no_abstract.flat.json"
FI_SDC = FI_DIR / "foreign_input_no_abstract.sdc"


def test_foreign_input_fixture_abstracts_and_seeds_input_sink() -> None:
    """End-to-end: the SDC drives clk_b ``src_q`` into the clk_a ``pipe``
    subtree's ``d_in``. The summariser now abstracts ``pipe`` (per-instance
    boundary keyed by cell name) AND records its input ports, so the
    input-side crossing is re-seeded rather than dropped (P3 dst_boundary,
    #257)."""
    from rtl_buddy_cdc import sdc as _sdc
    from rtl_buddy_cdc.cli import _summarise_blackboxes

    top, blackboxes = netlist.load_with_blackboxes(FI_BB_JSON)
    assert set(blackboxes) == {"pipe"}
    spec = _sdc.parse_file(FI_SDC)
    _sdc.synthesize_unconstrained_inputs(spec, top)
    boundaries = _summarise_blackboxes(top, blackboxes, spec)
    # Keyed per instance; the single ``u_pipe`` instance is abstracted.
    assert set(boundaries) == {"u_pipe"}
    summary = boundaries["u_pipe"]
    assert summary.clock == "clk_a"
    assert set(summary.input_ports) == {"d_in"}


def test_foreign_input_flat_reports_async_crossing() -> None:
    """The flattened fixture (``pipe`` inlined) reports the real clk_b ->
    clk_a crossing at the subtree's first internal flop — the crossing
    the refusal protects from being silently dropped."""
    flat = _analyze_json_with(FI_FLAT_JSON, FI_SDC)
    assert flat["summary"]["async_crossings"] == 1
    (c,) = flat["crossings"]
    assert c["src_clock"] == "clk_b"
    assert c["dst_clock"] == "clk_a"


def test_foreign_input_blackbox_run_matches_flat() -> None:
    """CLI-level LITERAL PARITY (rtl-buddy-cdc#257): the abstracted
    blackbox run on this fixture now produces the SAME violation set and
    the same ``summary.crossings`` as the flattened companion.

    ``pipe`` is abstracted to its port boundary; the clk_b -> clk_a
    crossing INTO the boundary is re-seeded as a ``dst_boundary`` virtual
    sink, so the CDC-004 the flattened design reports at pipe's first
    internal flop reappears (anchored on the boundary input pin). The
    boundary clock pin still does not trip CDC-008 (clock-as-data).
    """
    bb = _analyze_json_with(FI_BB_JSON, FI_SDC)
    flat = _analyze_json_with(FI_FLAT_JSON, FI_SDC)

    # Same contract counts on both runs (the whole point of the parity).
    for key in ("violations", "suppressed", "crossings", "async_crossings"):
        assert bb["summary"][key] == flat["summary"][key], key
    # Same rule set fires; the real crossing is a CDC-004 in both.
    assert sorted(v["rule_id"] for v in bb["violations"]) == sorted(
        v["rule_id"] for v in flat["violations"]
    )
    assert any(v["rule_id"] == "CDC-004" for v in bb["violations"])
    assert all(v["rule_id"] != "CDC-008" for v in bb["violations"])
    # The abstracted run anchors the crossing on the boundary input pin.
    (c,) = bb["crossings"]
    assert c["dst_boundary"] == {"instance": "u_pipe", "port": "d_in"}


def test_boundary_instance_clocks_helper() -> None:
    """The diagnostic helper reports the clk_a domain the parent feeds
    into the boundary instance's clock pin."""
    parent, _inst, _sub = _clocked_boundary_parent()
    assert abstract.boundary_instance_clocks(parent) == {"clk_a"}


def test_boundary_instance_clocks_skips_flops_and_unclocked() -> None:
    """The diagnostic helper skips real flops (their domain is handled by
    assign_domains) and instances whose clock pin doesn't trace anywhere
    / instances with no clock pin at all."""
    flop = Cell(name="f", type="$dff", connections={"CLK": (1,), "D": (2,), "Q": (3,)})
    # Instance whose clock pin (bit 9) has no driver and no port → root None.
    floating = Cell(name="u_float", type="blk", connections={"clk": (9,)})
    # Data-only instance with no clock pin at all.
    dataonly = Cell(name="u_data", type="blk2", connections={"d_out": (3,)})
    parent = Module(
        name="top",
        ports={"clk_a": Port(name="clk_a", direction="input", bits=(1,))},
        cells={"f": flop, "u_float": floating, "u_data": dataonly},
        netnames={},
    )
    # The real flop is skipped (its domain is handled by assign_domains);
    # the floating instance's clk doesn't trace and the data-only one has
    # no clock pin — so nothing is reported.
    assert abstract.boundary_instance_clocks(parent) == set()


def test_instance_clock_unresolved_pin_returns_none() -> None:
    """_instance_clock returns None when a clock-named pin exists but its
    net doesn't trace to any clock root."""
    inst = Cell(name="u_sub", type="sub", connections={"clk": (9,)})
    parent = Module(name="top", ports={}, cells={"u_sub": inst}, netnames={})
    assert abstract._instance_clock(parent, inst) is None
