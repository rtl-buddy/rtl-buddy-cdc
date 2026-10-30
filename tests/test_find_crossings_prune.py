"""The ``find_crossings`` fanout prune must never change a result.

``_make_fanout_filter`` lets the flop-sourced walk skip bits that cannot
reach a foreign-domain flop ``D`` pin within the remaining hop budget. It
is a pure speed-up: every report must be byte-identical with the prune on
or off. The sweep below runs ``analyze`` over every committed fixture
netlist both ways and compares the full JSON reports; the unit tests pin
the filter's budget arithmetic directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import domain
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import _make_fanout_filter, assign_domains, find_crossings
from rtl_buddy_cdc.netlist import Cell, Module, Port

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _fixture_netlists() -> list[tuple[Path, Path]]:
    """Every fixture netlist JSON paired with an SDC from its directory."""
    out: list[tuple[Path, Path]] = []
    for path in sorted(FIXTURES.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "modules" not in data:
            continue
        sdcs = sorted(path.parent.glob("*.sdc"))
        if not sdcs:
            continue
        same_stem = [s for s in sdcs if s.stem == path.name.split(".")[0]]
        out.append((path, (same_stem or sdcs)[0]))
    return out


def _analyze(netlist: Path, sdc: Path) -> str:
    result = runner.invoke(
        app, ["analyze", "-n", str(netlist), "-s", str(sdc), "-f", "json"]
    )
    return f"exit={result.exit_code}\n{result.output}"


@pytest.mark.parametrize(
    ("netlist", "sdc"),
    _fixture_netlists(),
    ids=lambda p: p.relative_to(FIXTURES).as_posix(),
)
def test_prune_does_not_change_report(
    netlist: Path, sdc: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pruned = _analyze(netlist, sdc)
    monkeypatch.setattr(
        domain, "_make_fanout_filter", lambda *_a, **_k: lambda *_b: True
    )
    unpruned = _analyze(netlist, sdc)
    assert pruned == unpruned


def test_fixture_sweep_is_not_empty() -> None:
    assert len(_fixture_netlists()) > 100


def _ff(name: str, clk: int, d: tuple[int, ...], q: tuple[int, ...]) -> Cell:
    return Cell(name=name, type="$dff", connections={"CLK": (clk,), "D": d, "Q": q})


def _chain_module() -> Module:
    """``a0`` (clk_a) → ``$not`` → ``$not`` → ``b0`` (clk_b), plus a
    same-domain branch ``a0`` → ``$and`` → ``a1`` (clk_a).

    Bits: 2 = clk_a, 3 = clk_b, 4 = a0.Q, 5 = n1.Y, 6 = n2.Y, 7 = and.Y.
    """
    cells = {
        "a0": _ff("a0", 2, (10,), (4,)),
        "n1": Cell(name="n1", type="$not", connections={"A": (4,), "Y": (5,)}),
        "n2": Cell(name="n2", type="$not", connections={"A": (5,), "Y": (6,)}),
        "b0": _ff("b0", 3, (6,), (11,)),
        "g": Cell(name="g", type="$and", connections={"A": (4,), "B": (4,), "Y": (7,)}),
        "a1": _ff("a1", 2, (7,), (12,)),
    }
    ports = {
        "clk_a": Port(name="clk_a", direction="input", bits=(2,)),
        "clk_b": Port(name="clk_b", direction="input", bits=(3,)),
    }
    return Module(name="chain", ports=ports, cells=cells, netnames={})


def _filter(module: Module, max_hops: int):
    domains = {fd.flop.cell.name: fd for fd in assign_domains(module)}
    flop_by_d_bit: dict = {}
    for fd in domains.values():
        for b in fd.flop.d:
            flop_by_d_bit.setdefault(b, []).append(fd.flop)
    return _make_fanout_filter(module, flop_by_d_bit, domains, max_hops)


def test_filter_keeps_bits_within_budget_of_a_foreign_flop() -> None:
    useful = _filter(_chain_module(), max_hops=4)
    # a0.Q is 2 cells from b0.D (clk_b): useful from clk_a with 2+ hops left.
    assert useful(4, 0, "clk_a")
    assert useful(4, 2, "clk_a")
    assert not useful(4, 3, "clk_a")
    # n1.Y is 1 cell from b0.D.
    assert useful(5, 3, "clk_a")


def test_filter_drops_same_domain_only_fanout() -> None:
    useful = _filter(_chain_module(), max_hops=4)
    # and.Y only reaches a1.D, which is in the source's own domain.
    assert not useful(7, 0, "clk_a")
    # The same bit is useful to a walk from a clk_b source.
    assert useful(7, 0, "clk_b")


def test_filter_respects_max_hops() -> None:
    # With a 1-cell budget, a0.Q (2 cells from b0.D) is not useful.
    assert not _filter(_chain_module(), max_hops=1)(4, 0, "clk_a")
    crossings = find_crossings(_chain_module(), max_hops=1)
    assert crossings == []
    (c,) = find_crossings(_chain_module(), max_hops=2)
    assert (c.src_flop.name, c.dst_flop.name, c.min_hops) == ("a0", "b0", 2)
