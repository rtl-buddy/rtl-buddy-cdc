"""The parent's bit-driver map is built once, not once per boundary instance.

Issue #320. ``abstract._instance_clocks`` used to call
``domain._bit_drivers(parent)``, a full scan of ``parent.cells``, on every
call. It runs once per blackbox instance from three places
(``blackbox_clock_pins_by_module``, ``compose_boundaries`` and the CLI's
CDC-008 clock-pin map), so on a flat SoC the cost was
O(cells x instances).

These tests count the builds instead of timing them, so they are exact
and do not flake. The fix is a pure refactor: the existing hierarchical,
blackbox and XPM suites pin the reports themselves.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import abstract, cli, domain, hierarchy, netlist, sdc as sdc_mod
from rtl_buddy_cdc.hierarchy import compose_boundaries
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.sdc import ClockSpec

FIX = Path(__file__).parent / "fixtures"
runner = CliRunner()


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Count ``_bit_drivers`` builds per module name at every call site
    that classifies boundary instances."""
    counts: Counter[str] = Counter()
    real = domain._bit_drivers

    def counting(module: Module):
        counts[module.name] += 1
        return real(module)

    for mod in (abstract, hierarchy, cli):
        monkeypatch.setattr(mod, "_bit_drivers", counting)
    return counts


def _many_instance_parent(n: int) -> tuple[Module, dict[str, Module]]:
    """``n`` instances of a single-clock ``pipe``, alternating between
    clk_a and clk_b, each driving a flop in the other domain.

    bits: clk_a=1, clk_b=2; instance i drives bit 10+i into flop dst<i>.
    """
    cells: dict[str, Cell] = {}
    for i in range(n):
        src, dst = (1, 2) if i % 2 == 0 else (2, 1)
        cells[f"u_pipe{i}"] = Cell(
            name=f"u_pipe{i}",
            type="pipe",
            connections={"clk": (src,), "d_out": (10 + i,)},
        )
        cells[f"dst{i}"] = Cell(
            name=f"dst{i}",
            type="$dff",
            connections={"CLK": (dst,), "D": (10 + i,), "Q": (1000 + i,)},
        )
    parent = Module(
        name="top",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(1,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(2,)),
        },
        cells=cells,
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


def _async_spec() -> ClockSpec:
    spec = ClockSpec()
    spec.async_groups.append([{"clk_a"}, {"clk_b"}])
    return spec


def test_compose_builds_parent_drivers_once(builds: Counter[str]) -> None:
    parent, blackboxes = _many_instance_parent(40)
    boundaries, stats = compose_boundaries(parent, blackboxes, _async_spec())
    # Both clock contexts summarised, every instance placed.
    assert len(boundaries) == 40
    assert stats.cache_hits == 38
    assert builds["top"] == 1


def test_compose_sync_primitive_path_builds_once(builds: Counter[str]) -> None:
    nl = FIX / "xpm_cdc_blackbox" / "xpm_cdc_blackbox.json"
    top, blackboxes = netlist.load_with_blackboxes(nl)
    spec = sdc_mod.parse_file(nl.with_suffix(".sdc"))
    sdc_mod.synthesize_unconstrained_inputs(spec, top)
    _boundaries, stats = compose_boundaries(top, blackboxes, spec)
    assert stats.primitive_modules
    assert builds[top.name] == 1


def test_analyze_builds_top_drivers_once_per_pass(builds: Counter[str]) -> None:
    """One build in ``compose_boundaries`` and one for the CLI's CDC-008
    clock-pin map, however many instances the top has."""
    d = FIX / "shared_subtree_compose"
    nl = d / "shared_subtree_compose.json"
    result = runner.invoke(
        cli.app,
        [
            "analyze",
            "-n",
            str(nl),
            "-s",
            str(d / "shared_subtree_compose.sdc"),
            "-f",
            "json",
        ],
    )
    assert result.exit_code in (0, 1), result.output
    top, _bb = netlist.load_with_blackboxes(nl)
    assert sum(1 for c in top.cells.values() if c.type == "pipe") >= 2
    assert builds[top.name] == 2
