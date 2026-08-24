"""Clock-output blackbox decline (rtl-buddy-cdc#273).

The boundary-soundness check declines a candidate it cannot prove
single-clock, but used to *accept* a single-clock module that has a
**clock output port** — one that generates or forwards a clock consumed
elsewhere. Blackboxing such a module silently elides the clock
generation / forwarding network.

``clock_output_blackbox`` is the fixture: ``clkfwd_tile`` forwards
``clk_in`` to ``clk_out`` and the next tile is clocked from it, while
``datapath_core`` is the data-only positive control that must stay
abstracted.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rtl_buddy_cdc import abstract, netlist, sdc as sdc_mod
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.hierarchy import compose_boundaries

runner = CliRunner()

FIX = Path(__file__).parent / "fixtures" / "clock_output_blackbox"
FLAT = FIX / "clock_output_blackbox.flat.json"
BB = FIX / "clock_output_blackbox.json"
SDC = FIX / "clock_output_blackbox.sdc"

EXPECTED = (
    "blackbox `u0` (`clkfwd_tile`) drives a clock output `clk_out` — clock "
    "generation/forwarding would be elided; flatten it or analyse standalone "
    "(waive CDC-BBX if intentionally out of scope here)."
)


def _report(path: Path) -> dict:
    run = runner.invoke(app, ["analyze", "-n", str(path), "-s", str(SDC), "-f", "json"])
    assert run.exit_code in (0, 1), run.output
    return json.loads(run.output)


def _load() -> tuple[netlist.Module, dict[str, netlist.Module], sdc_mod.ClockSpec]:
    top, blackboxes = netlist.load_with_blackboxes(BB)
    spec = sdc_mod.parse_file(SDC)
    sdc_mod.synthesize_unconstrained_inputs(spec, top)
    return top, blackboxes, spec


# --------------------------------------------------------------------------
# The walk itself
# --------------------------------------------------------------------------


def test_clock_output_port_is_detected() -> None:
    """``u0.clk_out`` reaches ``u1``'s clock pin, so it drives a clock.

    ``u1``'s own ``clk_out`` is unconnected, so that instance on its own
    shows nothing — the decline has to be a MODULE-level verdict.
    """
    top, blackboxes, spec = _load()
    sub = blackboxes["clkfwd_tile"]
    assert abstract.clock_driving_output_ports(
        top,
        top.cells["u0"],
        sub,
        blackboxes=blackboxes,
        spec=spec,
        pin_clocks=spec.pin_clocks,
    ) == ("clk_out",)
    assert (
        abstract.clock_driving_output_ports(
            top,
            top.cells["u1"],
            sub,
            blackboxes=blackboxes,
            spec=spec,
            pin_clocks=spec.pin_clocks,
        )
        == ()
    )


def test_module_level_clock_pin_union_sees_the_forwarded_pin() -> None:
    """``u1.clk_in`` cannot be traced to a declared clock (its driver is an
    opaque boundary output), but ``u0`` proves ``clk_in`` is a clock pin OF
    THE MODULE — which is how the forwarded clock's sink is recognised
    structurally rather than by pin-name guessing."""
    top, blackboxes, spec = _load()
    per_instance = abstract.instance_clock_pins(
        top, top.cells["u1"], blackboxes["clkfwd_tile"], spec=spec
    )
    assert per_instance == frozenset()
    by_module = abstract.blackbox_clock_pins_by_module(
        top, blackboxes, spec=spec, pin_clocks=spec.pin_clocks
    )
    assert by_module["clkfwd_tile"] == frozenset({"clk_in"})
    assert by_module["datapath_core"] == frozenset({"clk"})


def test_data_only_output_is_not_detected() -> None:
    """Positive control at the walk level: ``datapath_core.q`` carries data
    only, so nothing is reported — no false declines on the common case."""
    top, blackboxes, spec = _load()
    assert (
        abstract.clock_driving_output_ports(
            top,
            top.cells["u_core"],
            blackboxes["datapath_core"],
            blackboxes=blackboxes,
            spec=spec,
            pin_clocks=spec.pin_clocks,
        )
        == ()
    )


# --------------------------------------------------------------------------
# The decline, through compose_boundaries
# --------------------------------------------------------------------------


def test_clock_forwarding_tile_is_declined_data_core_is_abstracted() -> None:
    """Both ``clkfwd_tile`` instances are declined (module-level verdict),
    while the data-only ``datapath_core`` is still summarised."""
    top, blackboxes, spec = _load()
    boundaries, stats = compose_boundaries(top, blackboxes, spec)
    assert set(boundaries) == {"u_core"}
    assert stats.declined_modules == frozenset({"clkfwd_tile"})
    assert stats.clock_output_ports == frozenset({("clkfwd_tile", "clk_out")})
    assert stats.clock_output_ports_of("clkfwd_tile") == ("clk_out",)
    assert stats.clock_output_ports_of("datapath_core") == ()
    assert stats.boundary_modules == frozenset({"datapath_core"})


def test_single_clock_alone_would_have_accepted_the_tile() -> None:
    """The tile really is single-clock — the pre-#273 check accepted it.
    Without the new flavour it would have been abstracted away."""
    top, blackboxes, spec = _load()
    ic = abstract._instance_clocks(
        top, top.cells["u0"], blackboxes["clkfwd_tile"], spec=spec
    )
    assert ic.roots == frozenset({"clk"})
    assert abstract.is_single_clock_subtree(set(ic.roots), spec)
    assert (
        abstract.summarise_subtree(
            top, top.cells["u0"], blackboxes["clkfwd_tile"], spec
        )
        is not None
    )


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_flat_run_is_clean() -> None:
    """FLAT the design is clean: ``clk_out`` is a buffered copy of ``clk``
    and both tiles sit in the one domain. The blackboxed run's findings are
    therefore purely the coverage gap, not a real CDC bug."""
    report = _report(FLAT)
    assert report["violations"] == []
    assert report["summary"]["flops"] == 3


def test_decline_fires_with_the_clock_output_message() -> None:
    report = _report(BB)
    bbx = [v for v in report["violations"] if v["rule_id"] == "CDC-BBX"]
    assert [v["message"] for v in bbx] == [
        EXPECTED,
        EXPECTED.replace("`u0`", "`u1`"),
    ]
    assert all(v["severity"] == "error" for v in bbx)
    assert sorted(v["cell_name"] for v in bbx) == ["u0", "u1"]
    run = runner.invoke(app, ["analyze", "-n", str(BB), "-s", str(SDC)])
    assert run.exit_code == 1


def test_no_clock_as_data_false_positive_on_the_declined_boundary() -> None:
    """The declined run reports the coverage gap and NOTHING else.

    In particular no CDC-008: the issue's second failure mode is the
    clock-network exemption losing the forwarded clock once the
    clock-output module is abstracted away. With the module declined, the
    blackboxed run's rule findings match the flat run's exactly (both
    empty) — only the CDC-BBX coverage errors are added."""
    flat = _report(FLAT)
    bb = _report(BB)
    assert [v["rule_id"] for v in flat["violations"]] == []
    assert {v["rule_id"] for v in bb["violations"]} == {"CDC-BBX"}
    assert not any(v["rule_id"] == "CDC-008" for v in bb["violations"])


def test_decline_is_waivable_exactly_like_the_other_flavour(tmp_path) -> None:
    """``waive CDC-BBX <instance-regex>`` suppresses the clock-output
    flavour the same way it suppresses the not-provably-single-clock one:
    same rule id, same report path, run back to exit 0."""
    waiver = tmp_path / "bbx.swl"
    waiver.write_text("waive CDC-BBX u[01]$  clock mesh analysed standalone\n")
    run = runner.invoke(
        app,
        [
            "analyze",
            "-n",
            str(BB),
            "-s",
            str(SDC),
            "--waivers",
            str(waiver),
            "-f",
            "json",
        ],
    )
    rep = json.loads(run.output)
    assert rep["summary"]["violations"] == 0
    assert rep["summary"]["suppressed"] == 2
    assert run.exit_code == 0


# --------------------------------------------------------------------------
# Walk mechanics, on hand-built netlists (the fixture lands its clock output
# straight on the sink; these pin the comb hops, the depth bound and the
# no-false-decline guards)
# --------------------------------------------------------------------------


def _bb(name: str, ports: dict[str, tuple[str, int]]) -> netlist.Module:
    return netlist.Module(
        name=name,
        ports={
            p: netlist.Port(name=p, direction=d, bits=(b,))
            for p, (d, b) in ports.items()
        },
        cells={},
        netnames={},
        is_blackbox=True,
    )


def _parent(cells: list[netlist.Cell], ports: dict[str, tuple[str, int]]):
    return netlist.Module(
        name="top",
        ports={
            p: netlist.Port(name=p, direction=d, bits=(b,))
            for p, (d, b) in ports.items()
        },
        cells={c.name: c for c in cells},
        netnames={},
    )


CLK_SDC = "create_clock -name clk -period 10.0 [get_ports clk]"


def test_clock_output_through_a_visible_gate_is_detected() -> None:
    """The forward walk crosses ordinary comb cells: a boundary output that
    reaches a flop CLK through a visible clock gate still declines."""
    gen = netlist.Cell(
        name="u_gen", type="clkgen", connections={"clk_in": (2,), "clk_out": (4,)}
    )
    gate = netlist.Cell(
        name="g0", type="$and", connections={"A": (4,), "B": (3,), "Y": (5,)}
    )
    ff = netlist.Cell(
        name="ff", type="$dff", connections={"CLK": (5,), "D": (3,), "Q": (9,)}
    )
    top = _parent(
        [gen, gate, ff],
        {"clk": ("input", 2), "en": ("input", 3), "q": ("output", 9)},
    )
    blackboxes = {
        "clkgen": _bb("clkgen", {"clk_in": ("input", 1), "clk_out": ("output", 2)})
    }
    spec = sdc_mod.parse(CLK_SDC)
    assert abstract.clock_driving_output_ports(
        top, gen, blackboxes["clkgen"], blackboxes=blackboxes, spec=spec
    ) == ("clk_out",)


def test_data_output_through_a_visible_gate_is_not_detected() -> None:
    """Same shape, but the gate output lands on the flop's ``D`` — data, not
    clock. No decline (the no-false-decline guard for the common case)."""
    gen = netlist.Cell(name="u_gen", type="dpath", connections={"clk": (2,), "o": (4,)})
    gate = netlist.Cell(
        name="g0", type="$and", connections={"A": (4,), "B": (3,), "Y": (5,)}
    )
    ff = netlist.Cell(
        name="ff", type="$dff", connections={"CLK": (2,), "D": (5,), "Q": (9,)}
    )
    top = _parent(
        [gen, gate, ff],
        {"clk": ("input", 2), "en": ("input", 3), "q": ("output", 9)},
    )
    blackboxes = {"dpath": _bb("dpath", {"clk": ("input", 1), "o": ("output", 2)})}
    spec = sdc_mod.parse(CLK_SDC)
    assert (
        abstract.clock_driving_output_ports(
            top, gen, blackboxes["dpath"], blackboxes=blackboxes, spec=spec
        )
        == ()
    )


def test_forward_walk_is_depth_bounded() -> None:
    """The walk is bounded by ``max_depth`` — the same clock-trace budget the
    rest of the boundary machinery threads from ``--clock-trace-depth``."""
    gen = netlist.Cell(
        name="u_gen", type="clkgen", connections={"clk_in": (2,), "clk_out": (4,)}
    )
    buf0 = netlist.Cell(name="b0", type="$_BUF_", connections={"A": (4,), "Y": (5,)})
    buf1 = netlist.Cell(name="b1", type="$_BUF_", connections={"A": (5,), "Y": (6,)})
    buf2 = netlist.Cell(name="b2", type="$_BUF_", connections={"A": (6,), "Y": (7,)})
    ff = netlist.Cell(
        name="ff", type="$dff", connections={"CLK": (7,), "D": (3,), "Q": (9,)}
    )
    top = _parent(
        [gen, buf0, buf1, buf2, ff],
        {"clk": ("input", 2), "d": ("input", 3), "q": ("output", 9)},
    )
    blackboxes = {
        "clkgen": _bb("clkgen", {"clk_in": ("input", 1), "clk_out": ("output", 2)})
    }
    spec = sdc_mod.parse(CLK_SDC)
    kw = dict(blackboxes=blackboxes, spec=spec)
    assert abstract.clock_driving_output_ports(
        top, gen, blackboxes["clkgen"], max_depth=16, **kw
    ) == ("clk_out",)
    assert (
        abstract.clock_driving_output_ports(
            top, gen, blackboxes["clkgen"], max_depth=1, **kw
        )
        == ()
    )


def test_no_clock_sinks_at_all_declines_nothing() -> None:
    """A design with no flop CLK, no boundary clock pin and no SDC has no
    clock to drive — the walk short-circuits."""
    gen = netlist.Cell(name="u_gen", type="dpath", connections={"i": (2,), "o": (4,)})
    gate = netlist.Cell(
        name="g0", type="$and", connections={"A": (4,), "B": (3,), "Y": (5,)}
    )
    top = _parent(
        [gen, gate],
        {"a": ("input", 2), "b": ("input", 3), "y": ("output", 5)},
    )
    blackboxes = {"dpath": _bb("dpath", {"i": ("input", 1), "o": ("output", 2)})}
    assert (
        abstract.clock_driving_output_ports(
            top, gen, blackboxes["dpath"], blackboxes=blackboxes
        )
        == ()
    )


def test_inout_clock_pin_is_not_a_clock_output() -> None:
    """An ``inout`` the instance is *driven* on is a clock pin, not a clock
    the subtree generates — it must not decline."""
    gen = netlist.Cell(name="u_gen", type="bidir", connections={"clk": (2,), "o": (4,)})
    ff = netlist.Cell(
        name="ff", type="$dff", connections={"CLK": (2,), "D": (4,), "Q": (9,)}
    )
    top = _parent([gen, ff], {"clk": ("input", 2), "q": ("output", 9)})
    blackboxes = {"bidir": _bb("bidir", {"clk": ("inout", 1), "o": ("output", 2)})}
    spec = sdc_mod.parse(CLK_SDC)
    assert (
        abstract.clock_driving_output_ports(
            top, gen, blackboxes["bidir"], blackboxes=blackboxes, spec=spec
        )
        == ()
    )


def test_declared_clock_on_a_port_absent_from_the_netlist_is_ignored() -> None:
    """A ``create_clock`` naming a port the netlist doesn't have contributes
    no sink bits (and doesn't crash the sink scan)."""
    gen = netlist.Cell(name="u_gen", type="dpath", connections={"clk": (2,), "o": (4,)})
    ff = netlist.Cell(
        name="ff", type="$dff", connections={"CLK": (2,), "D": (4,), "Q": (9,)}
    )
    top = _parent([gen, ff], {"clk": ("input", 2), "q": ("output", 9)})
    blackboxes = {"dpath": _bb("dpath", {"clk": ("input", 1), "o": ("output", 2)})}
    spec = sdc_mod.parse(
        CLK_SDC + "\ncreate_clock -name ghost -period 5.0 [get_ports not_in_netlist]"
    )
    assert (
        abstract.clock_driving_output_ports(
            top, gen, blackboxes["dpath"], blackboxes=blackboxes, spec=spec
        )
        == ()
    )


def test_reconvergent_fanout_visits_each_bit_once() -> None:
    """A boundary output that fans out and reconverges is walked once per
    bit — the visited set keeps the walk linear instead of exponential."""
    gen = netlist.Cell(name="u_gen", type="dpath", connections={"clk": (2,), "o": (4,)})
    g0 = netlist.Cell(
        name="g0", type="$and", connections={"A": (4,), "B": (4,), "Y": (5,)}
    )
    g1 = netlist.Cell(
        name="g1", type="$xor", connections={"A": (5,), "B": (5,), "Y": (6,)}
    )
    ff = netlist.Cell(
        name="ff", type="$dff", connections={"CLK": (2,), "D": (6,), "Q": (9,)}
    )
    top = _parent([gen, g0, g1, ff], {"clk": ("input", 2), "q": ("output", 9)})
    blackboxes = {"dpath": _bb("dpath", {"clk": ("input", 1), "o": ("output", 2)})}
    spec = sdc_mod.parse(CLK_SDC)
    assert (
        abstract.clock_driving_output_ports(
            top, gen, blackboxes["dpath"], blackboxes=blackboxes, spec=spec
        )
        == ()
    )
