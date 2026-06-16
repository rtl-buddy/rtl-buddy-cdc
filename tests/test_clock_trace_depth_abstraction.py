"""--clock-trace-depth must thread through the boundary-abstraction decision
(issue #263, fix for the P1 review's MAJOR).

The clock-trace hop budget surfaced as ``--clock-trace-depth`` has to reach
*every* clock-root trace on a run, not just the crossing walk. The
boundary-abstraction decision (``compose_boundaries`` ->
``summarise_subtree`` -> ``_instance_clocks``) traces each blackbox clock pin
to decide whether the subtree is single-clock. If that decision ran at a
*fixed* 16 while the crossing walk ran at a *raised* depth, the two would
disagree in the drop-a-crossing direction:

A dual-clock blackbox whose SECOND clock pin is fed through a clock chain
deeper than 16 hops would, at the fixed-16 abstraction depth, present only
its shallow root -> look single-clock -> be abstracted away, silently
collapsing its internal async crossing (the exact false-negative the brief
forbids). The crossing walk, at the raised depth, would have resolved that
deep root and declined.

These tests build such a hostile boundary in memory (no yosys needed) and
prove the abstraction decision is now depth-consistent:

  - at depth 16 the deep clock pin's root is unresolved, so the instance
    presents a single root (the abstraction would proceed);
  - at the raised depth BOTH clock roots resolve, the instance presents two
    async roots, and abstraction is correctly DECLINED -- the internal
    crossing is no longer dropped.

The contract under test: the depth the crossing walk uses and the depth the
abstraction decision uses are the SAME depth, so abstraction can never
collapse a boundary the walk would have kept multi-clock.
"""

from __future__ import annotations

from rtl_buddy_cdc import abstract, sdc as sdc_mod
from rtl_buddy_cdc.domain import trace_clock_root
from rtl_buddy_cdc.hierarchy import compose_boundaries
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.sdc import ClockSpec

# Stages in the divider chain feeding the blackbox's deep clock pin. Chosen
# > 16 so the chain's root is beyond the default budget but inside a raised
# one (we probe at 40). Each divider flop Q -> next flop CLK costs one hop.
_DEEP_STAGES = 20
_RAISED_DEPTH = 40


def _async_spec() -> ClockSpec:
    spec = ClockSpec()
    spec.clocks["clk_a"] = sdc_mod.Clock(name="clk_a", period=10.0, ports=("clk_a",))
    spec.clocks["clk_b"] = sdc_mod.Clock(name="clk_b", period=7.0, ports=("clk_b",))
    spec.async_groups.append([{"clk_a"}, {"clk_b"}])
    return spec


def _dual_clock_deep_boundary() -> tuple[Module, Cell, Module]:
    """A parent with a dual-clock blackbox ``u_sub`` whose two clock pins are:

      - ``clk`` (name-allow-listed) driven directly by ``clk_a``;
      - ``clk_i`` (name-allow-listed) driven through a ``_DEEP_STAGES``-stage
        ripple-divider chain off ``clk_b`` -- its root is > 16 hops up.

    Bit map: clk_a=1, clk_b=2, sub.d_out=3, the divider chain occupies
    bits 100.., and ``clk_i`` is wired to the last divider's Q.
    """
    cells: dict[str, Cell] = {}
    # Ripple-divider chain off clk_b: div0.CLK=clk_b, div_k.CLK=div_{k-1}.Q.
    prev_clk_bit: int = 2  # clk_b
    last_q: int = 2
    for k in range(_DEEP_STAGES):
        q_bit = 100 + k
        cells[f"div{k}"] = Cell(
            name=f"div{k}",
            type="$dff",
            connections={"CLK": (prev_clk_bit,), "D": (q_bit,), "Q": (q_bit,)},
        )
        prev_clk_bit = q_bit
        last_q = q_bit

    sub_inst = Cell(
        name="u_sub",
        type="sub",
        # clk <- clk_a (shallow); clk_i <- last divider Q (deep); d_out=3.
        connections={"clk": (1,), "clk_i": (last_q,), "d_out": (3,)},
    )
    cells["u_sub"] = sub_inst

    parent = Module(
        name="top",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(1,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(2,)),
        },
        cells=cells,
        netnames={},
    )
    sub = Module(
        name="sub",
        ports={
            "clk": Port(name="clk", direction="input", bits=()),
            "clk_i": Port(name="clk_i", direction="input", bits=()),
            "d_out": Port(name="d_out", direction="output", bits=()),
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )
    return parent, sub_inst, sub


def test_deep_clock_pin_is_genuinely_beyond_default_budget() -> None:
    """Sanity: the divider chain really is deeper than 16 hops -- the deep
    clock pin's root is None at depth 16 and clk_b at the raised depth."""
    parent, sub_inst, _ = _dual_clock_deep_boundary()
    from rtl_buddy_cdc.domain import _bit_drivers

    drivers = _bit_drivers(parent)
    deep_bit = sub_inst.connections["clk_i"][0]
    assert trace_clock_root(parent, deep_bit, drivers, max_depth=16) is None
    assert (
        trace_clock_root(parent, deep_bit, drivers, max_depth=_RAISED_DEPTH) == "clk_b"
    )


def test_instance_clocks_roots_grow_with_depth() -> None:
    """The traced clock-root SET of the dual-clock boundary depends on the
    budget: one root at depth 16 (deep pin unresolved), two at the raised
    depth. This is the value that feeds the single-clock decision."""
    parent, sub_inst, sub = _dual_clock_deep_boundary()
    spec = _async_spec()
    ic16 = abstract._instance_clocks(parent, sub_inst, sub, spec=spec, max_depth=16)
    ic40 = abstract._instance_clocks(
        parent, sub_inst, sub, spec=spec, max_depth=_RAISED_DEPTH
    )
    assert ic16.roots == frozenset({"clk_a"})
    assert ic40.roots == frozenset({"clk_a", "clk_b"})


def test_summarise_declines_dual_clock_only_at_matching_depth() -> None:
    """``summarise_subtree`` would WRONGLY abstract the dual-clock boundary
    at a fixed 16 (the deep clk_b pin is invisible there), but DECLINES it
    at the raised depth -- so the internal clk_a->clk_b crossing is not
    dropped. This is the soundness property the fix guarantees."""
    parent, sub_inst, sub = _dual_clock_deep_boundary()
    spec = _async_spec()
    # At 16 the second clock is invisible: the (unsound) abstraction proceeds.
    summary16 = abstract.summarise_subtree(parent, sub_inst, sub, spec, max_depth=16)
    assert summary16 is not None  # demonstrates the hazard the fix avoids
    # At the raised depth the two async roots are both seen: DECLINE.
    summary40 = abstract.summarise_subtree(
        parent, sub_inst, sub, spec, max_depth=_RAISED_DEPTH
    )
    assert summary40 is None


def test_compose_boundaries_declines_dual_clock_at_raised_depth() -> None:
    """End-to-end through the composition walk: at the raised depth the
    dual-clock blackbox is DECLINED (absent from ``boundaries`` and counted
    in ``declined_modules``), matching what the crossing walk at that depth
    resolves. At the fixed default it would be (unsoundly) summarised --
    the inconsistency the review flagged, now closed because both sides take
    the same ``max_depth``."""
    parent, _sub_inst, sub = _dual_clock_deep_boundary()
    spec = _async_spec()
    blackboxes = {"sub": sub}

    bnd40, stats40 = compose_boundaries(
        parent, blackboxes, spec, max_depth=_RAISED_DEPTH
    )
    assert "u_sub" not in bnd40
    assert "sub" in stats40.declined_modules

    # And the (deliberately) shallow default still abstracts it -- proving
    # the depth is what flips the decision, and that the main analyze path
    # (which now passes clock_trace_depth here) controls it.
    bnd16, stats16 = compose_boundaries(parent, blackboxes, spec, max_depth=16)
    assert "u_sub" in bnd16
    assert "sub" not in stats16.declined_modules
