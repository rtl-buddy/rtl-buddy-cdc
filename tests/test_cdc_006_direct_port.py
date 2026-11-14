"""CDC-006 needs a combinational cell between the source and the
synchroniser (rtl-buddy-cdc#360).

A typed input port wired straight into a 2FF synchroniser is the
textbook way to bring in an asynchronous input. CDC-006 fired on it
anyway ("driven by combinational logic") because it flagged any foreign
port in the first stage's ``D`` fanin. The typed-port good fixtures
escaped only through ``(* cdc_sync *)`` on the first stage; with the
attribute removed they are clean too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc
from rtl_buddy_cdc.netlist import Cell, Module, Port
from rtl_buddy_cdc.rules import check_cdc_006, run_all

FIX = Path(__file__).parent / "fixtures"

_SDC = """
create_clock -name clk -period 10.0 [get_ports clk]
create_clock -name vclk -period 12.0
set_clock_groups -asynchronous -group {clk} -group {vclk}
set_input_delay -clock vclk 1.0 [get_ports {a b}]
"""


def _sync(first_stage_d: int, extra_cells: dict[str, Cell]) -> Module:
    # Bits: clk=2, a=3, b=4, s1=5, s2=6; comb outputs from 10.
    return Module(
        name="top",
        ports={
            "clk": Port("clk", "input", (2,)),
            "a": Port("a", "input", (3,)),
            "b": Port("b", "input", (4,)),
            "q": Port("q", "output", (6,)),
        },
        cells={
            **extra_cells,
            "s1": Cell("s1", "$_DFF_P_", {"C": (2,), "D": (first_stage_d,), "Q": (5,)}),
            "s2": Cell("s2", "$_DFF_P_", {"C": (2,), "D": (5,), "Q": (6,)}),
        },
        netnames={},
    )


def _cdc_006(module: Module) -> list[str]:
    spec = sdc.parse(_SDC, backend="tokenizer")
    return [v.cell_name for v in check_cdc_006(module, [], spec)]


def test_port_straight_into_a_2ff_is_not_cdc_006() -> None:
    assert _cdc_006(_sync(3, {})) == []


@pytest.mark.parametrize("cell_type", ["$_NOT_", "$_BUF_", "$not"])
def test_port_through_a_buffer_or_inverter_is_not_cdc_006(cell_type: str) -> None:
    inv = Cell("inv", cell_type, {"A": (3,), "Y": (10,)})
    assert _cdc_006(_sync(10, {"inv": inv})) == []


def test_comb_of_two_ports_still_fires() -> None:
    gate = Cell("g", "$_AND_", {"A": (3,), "B": (4,), "Y": (10,)})
    assert _cdc_006(_sync(10, {"g": gate})) == ["s1"]


def test_comb_behind_an_inverter_still_fires() -> None:
    gate = Cell("g", "$_AND_", {"A": (3,), "B": (4,), "Y": (10,)})
    inv = Cell("inv", "$_NOT_", {"A": (10,), "Y": (11,)})
    assert _cdc_006(_sync(11, {"g": gate, "inv": inv})) == ["s1"]


def _strip_cdc_sync(name: str, tmp_path: Path) -> Path:
    raw = json.loads((FIX / name / f"{name}.json").read_text())
    stripped = 0
    for mod in raw["modules"].values():
        for nn in mod["netnames"].values():
            if nn.get("attributes", {}).pop("cdc_sync", None) is not None:
                stripped += 1
    assert stripped, f"{name} carries no (* cdc_sync *) to remove"
    out = tmp_path / f"{name}.json"
    out.write_text(json.dumps(raw))
    return out


@pytest.mark.parametrize(
    "name",
    [
        "good_port_typed_sync",
        "good_unconstrained_input_two_domains_typed",
        "good_unconstrained_input_derived_clock_typed",
    ],
)
def test_typed_port_fixtures_are_clean_without_cdc_sync(
    name: str, tmp_path: Path
) -> None:
    module = netlist.load(_strip_cdc_sync(name, tmp_path))
    spec = sdc.parse_file(FIX / name / f"{name}.sdc", backend="tokenizer")
    sdc.synthesize_unconstrained_inputs(spec, module)
    from rtl_buddy_cdc.domain import filter_async, find_crossings

    crossings = filter_async(
        find_crossings(module, port_clock=spec.port_clock, **spec.clock_root_inputs()),
        spec,
    )
    assert run_all(module, crossings, spec) == []


@pytest.mark.parametrize("name", ["bad_comb_source", "bad_input_delay_cross_domain"])
def test_comb_before_a_synchroniser_still_fires(name: str) -> None:
    module = netlist.load(FIX / name / f"{name}.json")
    spec = sdc.parse_file(FIX / name / f"{name}.sdc", backend="tokenizer")
    sdc.synthesize_unconstrained_inputs(spec, module)
    assert check_cdc_006(module, [], spec)
