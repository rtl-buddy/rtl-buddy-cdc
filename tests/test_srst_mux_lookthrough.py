"""A flop's own synchronous-reset ``$mux`` is part of the flop
(rtl-buddy-cdc#376).

``if (rst) q <= '0; else q <= x;`` lowers, after ``proc``, to a
constant-leg ``$mux`` in front of ``D``; ``opt_dff`` folds the same
source into an ``$sdff`` whose ``D`` is ``x``. Two structural tests read
the ``proc`` form differently from the ``$sdff`` form:

- CDC-004's gated-bus test took a reset mux selected by a destination-
  domain register for a load-enable gate, so a binary bus into a
  synchroniser with a local sync reset passed as gated.
- CDC-013 neither classified a sync-reset toggle source as a toggle nor
  found the XOR tail's delayed copy behind its reset mux.

Both now look through the mux with the shared helper
``_own_srst_data_leg``. Each fixture is checked in both forms, which
must agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import Crossing, find_crossings
from rtl_buddy_cdc.flops import Flop
from rtl_buddy_cdc.netlist import Bit, Cell, Module, Port
from rtl_buddy_cdc.rules import (
    _is_gated_bus_crossing,  # noqa: PLC2701
    _own_srst_data_leg,  # noqa: PLC2701
    run_all,
)

FIX_ROOT = Path(__file__).parent / "fixtures"
BUS = "bad_bus_sync_local_srst"
PULSE = "good_pulse_synchronizer_srst"
FORMS = ["json", "optdff.json"]


def _load(name: str, suffix: str, module: Module | None = None):
    if module is None:
        module = netlist.load(FIX_ROOT / name / f"{name}.{suffix}")
    spec = sdc_mod.parse_file(FIX_ROOT / name / f"{name}.sdc")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = [
        c
        for c in find_crossings(
            module, port_clock=spec.port_clock, **spec.clock_root_inputs()
        )
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    return module, crossings, spec


def test_fixture_forms_differ_only_in_reset_folding() -> None:
    for name in (BUS, PULSE):
        proc = netlist.load(FIX_ROOT / name / f"{name}.json")
        optdff = netlist.load(FIX_ROOT / name / f"{name}.optdff.json")
        assert any(c.type == "$mux" for c in proc.cells.values())
        assert not any(c.type.startswith("$sdff") for c in proc.cells.values())
        assert any(c.type.startswith("$sdff") for c in optdff.cells.values())


# --- CDC-004: a reset is not a gate ----------------------------------------


@pytest.mark.parametrize("suffix", FORMS)
def test_binary_bus_into_local_srst_synchroniser_fires_cdc_004(suffix: str) -> None:
    module, crossings, spec = _load(BUS, suffix)
    assert [c.width for c in crossings] == [4]
    fired = [v.rule_id for v in run_all(module, crossings, spec)]
    assert fired == ["CDC-004"]


def _flop(name: str, d: tuple[Bit, ...], q: tuple[Bit, ...], clk: Bit) -> Flop:
    cell = Cell(name=name, type="$dff", connections={"CLK": (clk,), "D": d, "Q": q})
    return Flop(cell=cell, clk=clk, d=d, q=q)


def _reset_then(
    data_leg_driver: str, *, rst_domain: str = "dst"
) -> tuple[Module, Crossing, dict[str, str | None]]:
    """2-bit destination ``q`` behind its own reset mux (select = flop
    ``rstf`` of ``rst_domain``). ``data_leg_driver``: ``"src"`` = the
    leg is the source bus itself, ``"hold"`` = an enable mux (``en ?
    bus : q``, select = dst flop ``enf``) sits behind the reset."""
    src = _flop("src", (1, 2), (10, 11), clk=90)
    rstf = _flop("rstf", (3,), (20,), clk=91 if rst_domain == "dst" else 90)
    enf = _flop("enf", (4,), (21,), clk=91)
    q = _flop("q", (40, 41), (50, 51), clk=91)
    cells = [src.cell, rstf.cell, enf.cell, q.cell]
    if data_leg_driver == "src":
        leg: tuple[Bit, ...] = (10, 11)
    else:
        leg = (30, 31)
        cells.append(
            Cell(
                name="enmux",
                type="$mux",
                connections={"A": (50, 51), "B": (10, 11), "S": (21,), "Y": leg},
            )
        )
    cells.append(
        Cell(
            name="rmux",
            type="$mux",
            connections={"A": leg, "B": ("0", "0"), "S": (20,), "Y": (40, 41)},
        )
    )
    module = Module(
        name="m",
        ports={"o": Port(name="o", direction="output", bits=(50, 51))},
        cells={c.name: c for c in cells},
        netnames={},
    )
    domains: dict[str, str | None] = {
        "src": "a",
        "rstf": rst_domain if rst_domain != "src" else "a",
        "enf": "dst",
        "q": "dst",
    }
    c = Crossing(
        src_clock="a",
        src_flop=src,
        dst_flop=q,
        dst_clock="dst",
        width=2,
        min_hops=1,
    )
    return module, c, domains


def _drivers(module: Module) -> dict[Bit, tuple[str, str, int]]:
    out: dict[Bit, tuple[str, str, int]] = {}
    for cell in module.cells.values():
        for pin in ("Y", "Q"):
            for i, b in enumerate(cell.connections.get(pin, ())):
                if isinstance(b, int):
                    out[b] = (cell.name, pin, i)
    return out


def test_local_reset_mux_alone_is_not_a_gate() -> None:
    module, c, domains = _reset_then("src")
    assert not _is_gated_bus_crossing(module, c, domains, _drivers(module))


def test_enable_mux_behind_local_reset_is_still_a_gate() -> None:
    """``if (rst) q <= 0; else if (en) q <= bus;`` after ``proc``: the
    reset is stepped over and the hold mux behind it is the gate."""
    module, c, domains = _reset_then("hold")
    assert _is_gated_bus_crossing(module, c, domains, _drivers(module))


def test_own_srst_data_leg_rejects_foreign_select() -> None:
    module, c, domains = _reset_then("src", rst_domain="src")
    drivers = _drivers(module)
    counts = {b: 1 for b in (10, 11, 40, 41, 20)}
    assert (
        _own_srst_data_leg(module, c.dst_flop, "dst", domains, drivers, counts) is None
    )
    assert (
        _own_srst_data_leg(module, c.dst_flop, None, domains, drivers, counts) is None
    )
    local, c2, local_domains = _reset_then("src")
    assert _own_srst_data_leg(
        local, c2.dst_flop, "dst", local_domains, _drivers(local), counts
    ) == (10, 11)


# --- CDC-013: toggle synchroniser with synchronous resets -------------------


@pytest.mark.parametrize("suffix", FORMS)
def test_pulse_synchroniser_with_sync_resets_is_clean(suffix: str) -> None:
    module, crossings, spec = _load(PULSE, suffix)
    assert len(crossings) == 1
    assert run_all(module, crossings, spec) == []


def _without_xor_tail(module: Module) -> Module:
    """The same netlist with the XOR tail replaced by an AND: the delayed
    copy no longer recovers a pulse, so the toggle source is CDC-013's
    failure mode."""
    cells = dict(module.cells)
    for name, cell in module.cells.items():
        if cell.type == "$xor":
            cells[name] = Cell(
                name=name,
                type="$and",
                connections=dict(cell.connections),
                parameters=cell.parameters,
                attributes=cell.attributes,
            )
    return Module(
        name=module.name, ports=module.ports, cells=cells, netnames=module.netnames
    )


@pytest.mark.parametrize("suffix", FORMS)
def test_sync_reset_toggle_without_xor_tail_fires_cdc_013(suffix: str) -> None:
    """The source toggle's own reset mux is looked through: the ``proc``
    form classifies it as a toggle, as the ``$sdffe`` form does."""
    base = netlist.load(FIX_ROOT / PULSE / f"{PULSE}.{suffix}")
    module, crossings, spec = _load(PULSE, suffix, _without_xor_tail(base))
    fired = [v.rule_id for v in run_all(module, crossings, spec)]
    assert fired == ["CDC-013"]
