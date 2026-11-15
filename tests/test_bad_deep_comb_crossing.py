"""Crossings behind more than four combinational cells (rtl-buddy-cdc#347).

``find_crossings`` used to stop after ``max_hops=4`` cells, which the CLI
never changed: a deeper path from one domain into a flop of another was
not a crossing — no finding, no count, no warning. The walk now follows
the whole cone by default; ``--crossing-depth N`` caps it for run time,
and a cap that stops a path still reaching a flop in an asynchronous
domain is reported (``summary.crossing_depth_cut``, the sampled
``crossing_depth_cuts`` and a text warning), never silent.

- ``bad_deep_comb_crossing``: a port-sourced and a flop-sourced
  clk_a -> clk_b path, each six cells deep, committed as RTL (``.json``),
  gate-level (``.gates.json``) and with the flop's module kept as an
  analysed boundary cell (``.grey.json``).
- Hand-built modules pin the cut records of the port and boundary walks,
  the ``-through`` walker's cap and the async filter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod, sdc_objects
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import DepthCut, filter_async_cuts, find_crossings
from rtl_buddy_cdc.netlist import BoundarySummary, Cell, Module, Port, PortBoundary

NAME = "bad_deep_comb_crossing"
FIX = Path(__file__).parent / "fixtures" / NAME
SDC = FIX / f"{NAME}.sdc"
runner = CliRunner()


def _invoke(suffix: str, *extra: str, sdc: bool = True) -> tuple[int, str]:
    args = ["analyze", "-n", str(FIX / f"{NAME}.{suffix}"), *extra]
    if sdc:
        args += ["-s", str(SDC)]
    r = runner.invoke(app, args)
    assert r.exit_code in (0, 1), r.output
    return r.exit_code, r.stdout


def _json(suffix: str, *extra: str, sdc: bool = True) -> dict:
    return json.loads(_invoke(suffix, "-f", "json", *extra, sdc=sdc)[1])


@pytest.mark.parametrize("suffix", ["json", "gates.json"])
def test_deep_crossings_are_found_by_default(suffix: str) -> None:
    report = _json(suffix)
    summary = report["summary"]
    assert summary["crossings"] == 2
    assert (summary["crossing_depth"], summary["crossing_depth_cut"]) == (None, 0)
    assert report["crossing_depth_cuts"] == []
    assert sorted(c["min_hops"] for c in report["crossings"]) == [6, 6]
    assert {c.get("src_port") for c in report["crossings"]} == {"req_a", None}
    assert [v["rule_id"] for v in report["violations"]] == ["CDC-001", "CDC-001"]


@pytest.mark.parametrize("suffix", ["json", "gates.json"])
def test_cap_drops_them_and_reports_the_cut(suffix: str) -> None:
    report = _json(suffix, "--crossing-depth", "4")
    summary = report["summary"]
    assert (summary["crossings"], summary["violations"]) == (0, 0)
    assert (summary["crossing_depth"], summary["crossing_depth_cut"]) == (4, 2)
    cuts = report["crossing_depth_cuts"]
    assert {c["src_clock"] for c in cuts} == {"clk_a"}
    assert {c["dst_clock"] for c in cuts} == {"clk_b"}
    assert "port req_a" in {c["src"] for c in cuts}


def test_cap_at_the_path_depth_finds_them_without_a_cut() -> None:
    summary = _json("json", "--crossing-depth", "6")["summary"]
    assert (summary["crossings"], summary["crossing_depth_cut"]) == (2, 0)


def test_text_report_warns_about_the_cut() -> None:
    code, out = _invoke("json", "--crossing-depth", "4", "--no-color")
    assert code == 0
    assert "--crossing-depth 4 cut 2 paths into an asynchronous domain" in out
    assert "port req_a (clk_a) → clk_b" in out
    _code, clean = _invoke("json", "--no-color")
    assert "--crossing-depth" not in clean


def test_text_report_elides_a_long_cut_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from rtl_buddy_cdc import reporter

    monkeypatch.setattr(reporter, "_DEPTH_CUT_SAMPLE_CAP", 1)
    _code, out = _invoke("json", "--crossing-depth", "4", "--no-color")
    assert "\n    …\n" in out


def test_without_an_sdc_every_cut_is_reported() -> None:
    """No SDC: the flop walk still runs (ports are untyped, so not
    walked), and with no clock relation to filter by, its cut is
    reported."""
    report = _json("json", "--crossing-depth", "4", sdc=False)
    assert report["summary"]["crossing_depth_cut"] == 1
    (cut,) = report["crossing_depth_cuts"]
    assert (cut["src_clock"], cut["dst_clock"]) == ("clk_a", "clk_b")


def test_cut_inside_an_analysed_boundary_module_is_lifted() -> None:
    """``deep_ip`` kept as a boundary cell with its body: its internal
    crossing is found by the compositional pass (and lifted as CDC-001);
    under a cap, its cut is lifted too, named by module."""
    report = _json("grey.json")
    assert any("[inside `deep_ip`" in v["message"] for v in report["violations"])
    capped = _json("grey.json", "--crossing-depth", "4")
    assert capped["summary"]["violations"] == 0
    srcs = sorted(c["src"] for c in capped["crossing_depth_cuts"])
    assert srcs[0].startswith("deep_ip: ") and srcs[1] == "port req_a"


def test_lint_accepts_the_option() -> None:
    r = runner.invoke(app, ["lint", "--help"])
    assert "--crossing-depth" in r.output


def test_option_rejects_zero() -> None:
    r = runner.invoke(
        app,
        ["analyze", "-n", str(FIX / f"{NAME}.json"), "--crossing-depth", "0"],
    )
    assert r.exit_code == 2


# --- hand-built walks -------------------------------------------------------

# Bits: 2 = clk_a, 3 = clk_b, 4 = source, 5..7 = buffer chain, 8 = dst Q.


def _chain(src: Cell, *extra: Cell, ports: tuple[Port, ...] = ()) -> Module:
    cells = {
        src.name: src,
        "b0": Cell(name="b0", type="$not", connections={"A": (4,), "Y": (5,)}),
        "b1": Cell(name="b1", type="$not", connections={"A": (5,), "Y": (6,)}),
        "b2": Cell(name="b2", type="$not", connections={"A": (6,), "Y": (7,)}),
        "dst": Cell(
            name="dst", type="$dff", connections={"CLK": (3,), "D": (7,), "Q": (8,)}
        ),
        **{c.name: c for c in extra},
    }
    all_ports = {
        "clk_a": Port(name="clk_a", direction="input", bits=(2,)),
        "clk_b": Port(name="clk_b", direction="input", bits=(3,)),
        **{p.name: p for p in ports},
    }
    return Module(name="m", ports=all_ports, cells=cells, netnames={})


def _flop_src() -> Cell:
    return Cell(name="a", type="$dff", connections={"CLK": (2,), "D": (9,), "Q": (4,)})


def test_port_walk_reports_its_cut() -> None:
    port = Port(name="p", direction="input", bits=(4,))
    stub = Cell(name="a", type="$scopeinfo", connections={})
    module = _chain(stub, ports=(port,))
    cuts: list[DepthCut] = []
    xs = find_crossings(module, 2, port_clock={"p": "clk_a"}, on_depth_cut=cuts.append)
    assert xs == []
    assert cuts == [DepthCut("port p", "clk_a", "clk_b")]
    assert len(find_crossings(module, 3, port_clock={"p": "clk_a"})) == 1


@pytest.mark.parametrize(("src_clock", "named"), [("clk_a", "clk_a"), (None, None)])
def test_boundary_walk_reports_its_cut(
    src_clock: str | None, named: str | None
) -> None:
    inst = Cell(name="u", type="sub", connections={"o": (4,)})
    summary = BoundarySummary(
        module="sub",
        clock=src_clock,
        ports={
            "o": PortBoundary(
                port="o", src_clock=src_clock, synchronised=False, width=1
            )
        },
    )
    cuts: list[DepthCut] = []
    xs = find_crossings(
        _chain(inst), 2, boundaries={"u": summary}, on_depth_cut=cuts.append
    )
    assert xs == []
    (cut,) = cuts
    assert cut.src == "boundary u.o" and cut.dst_clock == "clk_b"
    assert cut.src_clock == (named or "<unconstrained>")


def test_cut_into_the_source_domain_is_not_reported() -> None:
    """Only a capture pin of ANOTHER domain beyond the cap is a cut."""
    module = _chain(_flop_src())
    module.cells["dst"] = Cell(
        name="dst", type="$dff", connections={"CLK": (2,), "D": (7,), "Q": (8,)}
    )
    cuts: list[DepthCut] = []
    assert find_crossings(module, 1, on_depth_cut=cuts.append) == []
    assert cuts == []


def test_uncapped_walk_never_reports_a_cut() -> None:
    cuts: list[DepthCut] = []
    (c,) = find_crossings(_chain(_flop_src()), on_depth_cut=cuts.append)
    assert c.min_hops == 3 and cuts == []


def test_filter_async_cuts_keeps_only_asynchronous_pairs() -> None:
    spec = sdc_mod.parse(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 7 [get_ports clk_b]\n"
        "create_clock -name clk_c -period 5 [get_ports clk_c]\n"
        "set_clock_groups -asynchronous -group {clk_a} -group {clk_b}\n"
    )
    cuts = [DepthCut("a", "clk_a", "clk_b"), DepthCut("a", "clk_a", "clk_c")]
    assert filter_async_cuts(cuts, spec) == [cuts[0]]


def test_through_walker_takes_the_cap() -> None:
    """``-through`` coverage walks the crossing walk's paths with its cap:
    a path the cap stops cannot defeat the cover, an uncapped one can."""
    module = _chain(_flop_src())
    module.netnames["mid"] = netlist.Netname(name="mid", bits=(9,))
    spec = sdc_mod.parse(
        "create_clock -name clk_a -period 10 [get_ports clk_a]\n"
        "create_clock -name clk_b -period 7 [get_ports clk_b]\n"
        "set_false_path -through [get_nets mid]\n"
    )
    (rc,) = sdc_objects.resolve(module, spec).constraints
    (c,) = find_crossings(module)
    assert not sdc_objects._ThroughWalker(module).all_paths_through(c, rc)
    assert sdc_objects._ThroughWalker(module, 2).all_paths_through(c, rc)


def test_control_pin_beyond_the_cap_is_a_cut() -> None:
    """``a`` reaches ``d1``'s D directly (kept) and ``d2``'s ``$sdffe``
    enable, two folded muxes from D (rtl-buddy-cdc#375): a cap of 1 drops
    the second, and says so even though the walk went on from ``a.Q``."""
    a = _flop_src()
    d1 = Cell(name="d1", type="$dff", connections={"CLK": (3,), "D": (4,), "Q": (10,)})
    d2 = Cell(
        name="d2",
        type="$sdffe",
        connections={"CLK": (3,), "D": (11,), "Q": (12,), "SRST": (13,), "EN": (4,)},
    )
    module = Module(
        name="m",
        ports={
            "clk_a": Port(name="clk_a", direction="input", bits=(2,)),
            "clk_b": Port(name="clk_b", direction="input", bits=(3,)),
        },
        cells={"a": a, "d1": d1, "d2": d2},
        netnames={},
    )
    cuts: list[DepthCut] = []
    (c,) = find_crossings(module, 1, on_depth_cut=cuts.append)
    assert c.dst_flop.name == "d1"
    assert cuts == [DepthCut("a", "clk_a", "clk_b")]
    assert len(find_crossings(module, 2)) == 2
