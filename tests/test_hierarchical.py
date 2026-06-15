"""P3 hierarchical / compositional analysis (#257).

Covers the rtl-buddy-cdc side end to end without a yosys toolchain:

- 3a/3b — the compositional boundary walk
  (``hierarchy.compose_boundaries``): each distinct blackbox module is
  summarised **once** (cached by module identity) and re-applied to
  every instance, with a ``CompositionStats`` record that proves the
  sharing (a block instantiated N times costs one summarise call).
- 3c — boundary-summary consumption in ``find_crossings`` is exercised
  by the existing P2 suite; here we assert it composes across multiple
  instances of one shared module.
- 3d — the committed ``shared_subtree_compose`` fixture pair: a
  single-clock ``pipe`` instantiated twice, each feeding a foreign-domain
  flop. The FLATTENED design (both copies inlined) and the
  AUTO-ABSTRACTED design (``pipe`` blackboxed + summarised once) produce
  IDENTICAL violations and identical ``summary.*`` — with strictly fewer
  flops walked in the abstracted run (the scaling win), and the shared
  subtree demonstrably analysed once.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import _summarise_blackboxes, app
from rtl_buddy_cdc.hierarchy import CompositionStats, compose_boundaries
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.sdc import ClockSpec

runner = CliRunner()

FIX_DIR = Path(__file__).parent / "fixtures" / "shared_subtree_compose"
BB_JSON = FIX_DIR / "shared_subtree_compose.json"
FLAT_JSON = FIX_DIR / "shared_subtree_compose.flat.json"
SDC = FIX_DIR / "shared_subtree_compose.sdc"


# --------------------------------------------------------------------------
# 3a/3b — compositional walk + module-identity caching (hand-built)
# --------------------------------------------------------------------------


def _async_spec() -> ClockSpec:
    spec = ClockSpec()
    spec.async_groups.append([{"clk_a"}, {"clk_b"}])
    return spec


def _two_instance_parent() -> tuple[Module, dict[str, Module]]:
    """A parent with TWO instances of the same single-clock ``pipe``
    module, both clocked from clk_a, each driving a clk_b flop.

    bits: clk_a=1, clk_b=2,
          u_pipe0.d_out=3 -> dst0.D=3 ; u_pipe1.d_out=4 -> dst1.D=4
    """
    p0 = Cell(name="u_pipe0", type="pipe", connections={"clk": (1,), "d_out": (3,)})
    p1 = Cell(name="u_pipe1", type="pipe", connections={"clk": (1,), "d_out": (4,)})
    dst0 = Cell(
        name="dst0", type="$dff", connections={"CLK": (2,), "D": (3,), "Q": (5,)}
    )
    dst1 = Cell(
        name="dst1", type="$dff", connections={"CLK": (2,), "D": (4,), "Q": (6,)}
    )
    parent = Module(
        name="top",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(1,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(2,)),
        },
        cells={"u_pipe0": p0, "u_pipe1": p1, "dst0": dst0, "dst1": dst1},
        netnames={},
    )
    pipe = Module(
        name="pipe",
        ports={
            "clk": Port(name="clk", direction="input", bits=()),
            "d_out": Port(name="d_out", direction="output", bits=()),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    return parent, {"pipe": pipe}


def test_compose_summarises_shared_module_once() -> None:
    """Two instances of one module ⇒ one BoundarySummary keyed by module
    name, and CompositionStats proves the second instance was a cache
    hit (analysed once, re-applied)."""
    parent, blackboxes = _two_instance_parent()
    boundaries, stats = compose_boundaries(parent, blackboxes, _async_spec())
    assert set(boundaries) == {"pipe"}
    assert stats.instances == 2
    assert stats.summarised == 1
    assert stats.cache_hits == 1
    assert stats.declined == 0
    assert stats.shared_subtree_reused is True


def test_compose_seeds_one_crossing_per_instance() -> None:
    """Although ``pipe`` is summarised once, find_crossings re-seeds the
    boundary per instance, so both clk_a→clk_b crossings surface."""
    from rtl_buddy_cdc.domain import find_crossings

    parent, blackboxes = _two_instance_parent()
    boundaries, _ = compose_boundaries(parent, blackboxes, _async_spec())
    crossings = find_crossings(parent, boundaries=boundaries)
    bnd = sorted(
        (c.src_boundary, c.dst_flop.name) for c in crossings if c.is_boundary_sourced
    )
    assert bnd == [(("u_pipe0", "d_out"), "dst0"), (("u_pipe1", "d_out"), "dst1")]


def test_compose_no_blackboxes_is_empty() -> None:
    """No blackbox siblings ⇒ empty map and zeroed stats."""
    parent = Module(name="top", ports={}, cells={}, netnames={})
    boundaries, stats = compose_boundaries(parent, None, _async_spec())
    assert boundaries == {}
    assert stats == CompositionStats()
    assert stats.shared_subtree_reused is False
    # Empty-dict (falsy) blackboxes take the same early-out path.
    boundaries2, stats2 = compose_boundaries(parent, {}, _async_spec())
    assert boundaries2 == {}
    assert stats2 == CompositionStats()


def test_compose_records_declined_module() -> None:
    """A multi-clock subtree the summariser declines is recorded in
    ``declined`` / ``declined_modules`` and absent from the boundary
    map; a second instance of the declined module is still a cache hit
    (declined once, not re-summarised)."""
    # ``mix``'s clock pin traces to clk_a, but its data input is driven
    # by a clk_b flop — a foreign-domain input the output-only summary
    # can't seed, so summarise_subtree declines (P2 safety, preserved).
    src = Cell(
        name="src_b", type="$dff", connections={"CLK": (2,), "D": (7,), "Q": (8,)}
    )
    m0 = Cell(
        name="u_mix0",
        type="mix",
        connections={"clk": (1,), "d_in": (8,), "d_out": (3,)},
    )
    m1 = Cell(
        name="u_mix1",
        type="mix",
        connections={"clk": (1,), "d_in": (8,), "d_out": (4,)},
    )
    parent = Module(
        name="top",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(1,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(2,)),
        },
        cells={"src_b": src, "u_mix0": m0, "u_mix1": m1},
        netnames={},
    )
    mix = Module(
        name="mix",
        ports={
            "clk": Port(name="clk", direction="input", bits=()),
            "d_in": Port(name="d_in", direction="input", bits=()),
            "d_out": Port(name="d_out", direction="output", bits=()),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    boundaries, stats = compose_boundaries(parent, {"mix": mix}, _async_spec())
    assert boundaries == {}
    assert stats.instances == 2
    assert stats.summarised == 0
    assert stats.declined == 1
    assert stats.declined_modules == frozenset({"mix"})
    # The second instance of the declined module was served from cache.
    assert stats.cache_hits == 1


def test_summarise_blackboxes_wrapper_delegates() -> None:
    """The CLI wrapper returns just the boundary map (drops stats) and
    matches ``compose_boundaries``' first element."""
    parent, blackboxes = _two_instance_parent()
    spec = _async_spec()
    boundaries = _summarise_blackboxes(parent, blackboxes, spec)
    expected, _ = compose_boundaries(parent, blackboxes, spec)
    assert boundaries == expected


# --------------------------------------------------------------------------
# 3d — fixture parity: flat vs auto-abstracted identical output
# --------------------------------------------------------------------------


def _analyze_json(path: Path) -> dict:
    result = runner.invoke(
        app, ["analyze", "-n", str(path), "-s", str(SDC), "-f", "json"]
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


def _violation_keys(report: dict) -> list[tuple[str, str, object]]:
    return sorted(
        (v["rule_id"], v["severity"], v.get("cell_name")) for v in report["violations"]
    )


def test_shared_subtree_abstracted_matches_flattened() -> None:
    """Parity (the whole point of P3): identical violations (rule,
    severity, anchor cell) and identical contract counts between the
    flattened design (both pipe copies inlined) and the auto-abstracted
    one (pipe summarised once, applied twice)."""
    bb = _analyze_json(BB_JSON)
    flat = _analyze_json(FLAT_JSON)

    assert _violation_keys(bb) == _violation_keys(flat)
    for key in ("violations", "suppressed", "crossings", "async_crossings"):
        assert bb["summary"][key] == flat["summary"][key], key

    # Both report two async clk_a→clk_b crossings, anchored on the two
    # real top-level destination flops present in both views.
    assert bb["summary"]["async_crossings"] == 2
    assert _violation_keys(bb) == [
        ("CDC-004", "error", "dst_q0"),
        ("CDC-004", "error", "dst_q1"),
    ]
    # The abstracted crossings are boundary-sourced, one per instance.
    bnd = sorted(
        c["src_boundary"]["instance"] for c in bb["crossings"] if "src_boundary" in c
    )
    assert bnd == ["u_pipe0", "u_pipe1"]


def test_shared_subtree_abstraction_reduces_flop_count() -> None:
    """The scaling win: the auto-abstracted run walks strictly fewer
    flops — both pipe internal stages are summarised away, leaving only
    the three top flops (src_q + two dst flops)."""
    bb = _analyze_json(BB_JSON)
    flat = _analyze_json(FLAT_JSON)
    assert bb["summary"]["flops"] < flat["summary"]["flops"]
    assert bb["summary"]["flops"] == 3
    assert flat["summary"]["flops"] == 5


def test_shared_subtree_loads_pipe_once_as_blackbox_sibling() -> None:
    """The abstracted dump loads one top + one ``pipe`` blackbox sibling
    even though the parent instantiates it twice."""
    top, blackboxes = netlist.load_with_blackboxes(BB_JSON)
    assert top.name == "shared_subtree_compose"
    assert set(blackboxes) == {"pipe"}
    assert blackboxes["pipe"].is_blackbox is True
    assert blackboxes["pipe"].cells == {}
    assert {c.type for c in top.cells.values() if c.type == "pipe"} == {"pipe"}
    assert sum(1 for c in top.cells.values() if c.type == "pipe") == 2


def test_shared_subtree_compose_stats_on_fixture() -> None:
    """End-to-end on the committed fixture: the compositional walk
    summarises ``pipe`` once and serves the second instance from cache."""
    top, blackboxes = netlist.load_with_blackboxes(BB_JSON)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, top)
    boundaries, stats = compose_boundaries(top, blackboxes, spec)
    assert set(boundaries) == {"pipe"}
    assert stats.instances == 2
    assert stats.summarised == 1
    assert stats.cache_hits == 1
    assert stats.shared_subtree_reused is True
