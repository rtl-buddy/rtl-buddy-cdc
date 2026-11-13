"""CDC-001 and a single bit captured under a handshake enable
(rtl-buddy-cdc#368).

An 8-bit payload captured under a toggle handshake's synchronised enable
was accepted as a gated bus (CDC-004), while a 1-bit flag captured
under the very same enable reported CDC-001. A single-bit crossing is
now accepted when (1) the destination captures only under a dst-domain
enable with a real hold (``EN`` pin, or a mux whose other leg is the
flop's own ``Q``), (2) that enable is derived from a depth >= 2
synchroniser of a control from the crossing's own source domain, and
(3) the source is held by a synced-back acknowledge. Without (3) the
crossing still fires CDC-001, with a message naming the open loop: for
a bus that case is CDC-012's.

- ``good_handshake_single_bit``: the issue's reproducer plus a capture
  with a synchronous reset in front of its hold mux; clean on the
  ``proc`` (hold mux), ``opt_dff`` (``$dffe`` / ``$sdffe``) and
  gate-level (``$_DFFE_*`` / ``$_SDFFE_*``) netlists.
- ``bad_handshake_single_bit``: one capture per broken condition.

A constant enable and an enable straight from the source domain are
pinned by rewiring the good netlist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc import netlist, sdc as sdc_mod, sdc_objects
from rtl_buddy_cdc.cli import app
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.netlist import Cell, Module
from rtl_buddy_cdc.rules import _build_context, crossing_synchronizer, run_all

FIX = Path(__file__).parent / "fixtures"
GOOD = "good_handshake_single_bit"
BAD = "bad_handshake_single_bit"


def _analyze(name: str, suffix: str) -> tuple[Module, dict]:
    d = FIX / name
    net = d / f"{name}.{suffix}"
    r = CliRunner().invoke(
        app, ["analyze", "-n", str(net), "-s", str(d / f"{name}.sdc"), "-f", "json"]
    )
    assert r.exit_code in (0, 1), r.output
    return netlist.load(net), json.loads(r.stdout)


def _reg(module: Module, cell_name: str) -> str:
    q = tuple(module.cells[cell_name].connections.get("Q", ()))
    names = sorted(
        n.name
        for n in module.netnames.values()
        if q and tuple(n.bits) == q and not n.name.startswith("$")
    )
    return names[0] if names else cell_name


@pytest.mark.parametrize("suffix", ["json", "optdff.json", "synth.json"])
def test_handshake_captured_flag_is_clean(suffix: str) -> None:
    _module, report = _analyze(GOOD, suffix)
    assert report["summary"]["crossings"] == 5
    assert report["violations"] == []


def test_netlist_forms_carry_the_shapes_under_test() -> None:
    proc = netlist.load(FIX / GOOD / f"{GOOD}.json")
    assert not any(c.type in ("$dffe", "$sdffe") for c in proc.cells.values())
    optdff = netlist.load(FIX / GOOD / f"{GOOD}.optdff.json")
    assert {"$dffe", "$sdffe"} <= {c.type for c in optdff.cells.values()}
    synth = netlist.load(FIX / GOOD / f"{GOOD}.synth.json")
    assert any(c.type.startswith("$_DFFE_") for c in synth.cells.values())


def _spec_and_crossings(name: str, suffix: str):
    d = FIX / name
    module = netlist.load(d / f"{name}.{suffix}")
    spec = sdc_mod.parse_file(d / f"{name}.sdc")
    sdc_objects.resolve_port_delays(module, spec)
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = [
        c
        for c in find_crossings(module, port_clock=spec.port_clock)
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    return module, spec, crossings


@pytest.mark.parametrize("suffix", ["json", "optdff.json"])
def test_domain_map_names_the_flag_crossings_handshake(suffix: str) -> None:
    """The domain map's scheme agrees with lint: the two flag captures
    are ``"handshake"``, not unrecognised."""
    module, spec, crossings = _spec_and_crossings(GOOD, suffix)
    ctx = _build_context(module, spec, clock_for_port=spec.clock_for_port)
    schemes = {
        _reg(module, c.dst_flop.cell.name): crossing_synchronizer(module, c, ctx)
        for c in crossings
    }
    assert schemes["flag_out"] == "handshake"
    assert schemes["flag_r"] == "handshake"


@pytest.mark.parametrize("suffix", ["json", "optdff.json"])
def test_constant_enable_is_not_a_gate(suffix: str) -> None:
    """Control: tie the flag capture's enable (``EN`` pin, or the hold
    mux's select) to a constant and CDC-001 fires again."""
    module, spec, crossings = _spec_and_crossings(GOOD, suffix)
    flag = next(
        c for c in crossings if _reg(module, c.dst_flop.cell.name) == "flag_out"
    )
    cell = flag.dst_flop.cell
    cells = dict(module.cells)
    if "EN" in cell.connections:
        cells[cell.name] = Cell(
            name=cell.name,
            type=cell.type,
            connections={**cell.connections, "EN": ("1",)},
            parameters=cell.parameters,
            attributes=cell.attributes,
        )
    else:
        drv = next(
            c
            for c in module.cells.values()
            if c.connections.get("Y") == cell.connections["D"]
        )
        cells[drv.name] = Cell(
            name=drv.name,
            type=drv.type,
            connections={**drv.connections, "S": ("1",)},
            parameters=drv.parameters,
        )
    module = Module(
        name=module.name, ports=module.ports, cells=cells, netnames=module.netnames
    )
    crossings = [
        c
        for c in find_crossings(module, port_clock=spec.port_clock)
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    fired = [
        _reg(module, v.cell_name or "")
        for v in run_all(module, crossings, spec)
        if v.rule_id == "CDC-001"
    ]
    assert fired == ["flag_out"]


# Expected CDC-001 destinations, one finding per crossing (f_dsel is
# reached from both of its data legs).
_BAD_CDC001 = ["f_dsel", "f_dsel", "f_free", "f_open", "f_third", "f_unsync", "u1"]
_BAD_FORMS = ["json", "optdff.json"]


@pytest.mark.parametrize("suffix", _BAD_FORMS)
def test_unqualified_single_bit_captures_still_fire(suffix: str) -> None:
    module, report = _analyze(BAD, suffix)
    by_rule: dict[str, list[str]] = {}
    for v in report["violations"]:
        by_rule.setdefault(v["rule_id"], []).append(_reg(module, v["cell_name"]))
    assert sorted(by_rule.pop("CDC-001")) == _BAD_CDC001
    for v in report["violations"]:
        assert v["severity"] == "error" or v["rule_id"] != "CDC-001"
    # The open-loop 8-bit sibling is CDC-012's; u1 is a toggle captured
    # by a single flop, which CDC-013 also reads as event-loss prone.
    assert by_rule == {"CDC-012": ["data_o"], "CDC-013": ["tog_a"]}


@pytest.mark.parametrize("suffix", _BAD_FORMS)
def test_open_loop_capture_names_the_missing_acknowledge(suffix: str) -> None:
    module, report = _analyze(BAD, suffix)
    msgs = {
        _reg(module, v["cell_name"]): v["message"]
        for v in report["violations"]
        if v["rule_id"] == "CDC-001"
    }
    assert "no synchronised-back acknowledge" in msgs["f_open"]
    for reg in ("f_free", "f_unsync", "f_third", "f_dsel", "u1"):
        assert "has no second-stage synchronizer" in msgs[reg], reg


def test_source_domain_select_is_not_a_gate() -> None:
    """Control: drive the flag's hold-mux select from a source-domain
    register (the request toggle) instead of the synchronised edge."""
    module, spec, crossings = _spec_and_crossings(GOOD, "json")
    flag = next(
        c for c in crossings if _reg(module, c.dst_flop.cell.name) == "flag_out"
    )
    tog_q = next(
        tuple(c.connections["Q"])
        for c in module.cells.values()
        if "Q" in c.connections and _reg(module, c.name) == "tog_a"
    )
    drv = next(
        c
        for c in module.cells.values()
        if c.connections.get("Y") == flag.dst_flop.cell.connections["D"]
    )
    cells = dict(module.cells)
    cells[drv.name] = Cell(
        name=drv.name,
        type=drv.type,
        connections={**drv.connections, "S": tog_q},
        parameters=drv.parameters,
    )
    module = Module(
        name=module.name, ports=module.ports, cells=cells, netnames=module.netnames
    )
    crossings = [
        c
        for c in find_crossings(module, port_clock=spec.port_clock)
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    fired = sorted(
        _reg(module, v.cell_name or "")
        for v in run_all(module, crossings, spec)
        if v.rule_id == "CDC-001"
    )
    # One finding per crossing into flag_out: the data and the toggle.
    assert fired == ["flag_out", "flag_out"]


def test_port_source_cannot_show_a_hold() -> None:
    """Control: capture the typed input port ``flag_in`` itself under the
    synchronised edge. The enable qualifies, but a port has no register
    to hold it, so CDC-001 fires with the open-loop message."""
    module, spec, crossings = _spec_and_crossings(GOOD, "json")
    flag = next(
        c for c in crossings if _reg(module, c.dst_flop.cell.name) == "flag_out"
    )
    drv = next(
        c
        for c in module.cells.values()
        if c.connections.get("Y") == flag.dst_flop.cell.connections["D"]
    )
    hold = tuple(flag.dst_flop.q)
    data_pin = "A" if tuple(drv.connections["B"]) == hold else "B"
    cells = dict(module.cells)
    cells[drv.name] = Cell(
        name=drv.name,
        type=drv.type,
        connections={**drv.connections, data_pin: tuple(module.ports["flag_in"].bits)},
        parameters=drv.parameters,
    )
    module = Module(
        name=module.name, ports=module.ports, cells=cells, netnames=module.netnames
    )
    crossings = [
        c
        for c in find_crossings(module, port_clock=spec.port_clock)
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    fired = [v for v in run_all(module, crossings, spec) if v.rule_id == "CDC-001"]
    assert [_reg(module, v.cell_name or "") for v in fired] == ["flag_out"]
    assert fired[0].crossing is not None and fired[0].crossing.src_flop is None
    assert "no synchronised-back acknowledge" in fired[0].message


def test_enable_mixing_in_an_unsynchronised_term_is_not_qualified() -> None:
    """Control: ``edge | other`` with ``other`` any dst register that is
    not the synchronised control (here a payload output bit) may sample
    the source between transfers, so every flop in the enable's fanin
    must carry the synchronised control."""
    module, spec, crossings = _spec_and_crossings(GOOD, "json")
    flag = next(
        c for c in crossings if _reg(module, c.dst_flop.cell.name) == "flag_out"
    )
    drv = next(
        c
        for c in module.cells.values()
        if c.connections.get("Y") == flag.dst_flop.cell.connections["D"]
    )
    other = next(
        tuple(c.connections["Q"])[:1]
        for c in module.cells.values()
        if "Q" in c.connections and _reg(module, c.name) == "data_out"
    )
    new_bit = 1 + max(
        b
        for c in module.cells.values()
        for bs in c.connections.values()
        for b in bs
        if isinstance(b, int)
    )
    cells = dict(module.cells)
    cells["mix_or"] = Cell(
        name="mix_or",
        type="$or",
        connections={"A": tuple(drv.connections["S"]), "B": other, "Y": (new_bit,)},
    )
    cells[drv.name] = Cell(
        name=drv.name,
        type=drv.type,
        connections={**drv.connections, "S": (new_bit,)},
        parameters=drv.parameters,
    )
    module = Module(
        name=module.name, ports=module.ports, cells=cells, netnames=module.netnames
    )
    crossings = [
        c
        for c in find_crossings(module, port_clock=spec.port_clock)
        if spec.are_async(c.src_clock, c.dst_clock)
    ]
    fired = [
        _reg(module, v.cell_name or "")
        for v in run_all(module, crossings, spec)
        if v.rule_id == "CDC-001"
    ]
    assert fired == ["flag_out"]
