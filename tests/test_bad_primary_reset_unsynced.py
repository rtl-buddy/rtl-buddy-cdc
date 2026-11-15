"""Negative-case fixture for RDC-008: port-sourced ARST missing sync in one of multiple domains."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_primary_reset_unsynced"
JSON = FIX_DIR / "bad_primary_reset_unsynced.json"
SDC = FIX_DIR / "bad_primary_reset_unsynced.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module)
    return module, crossings, spec


def test_rdc_008_fires_once(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    rdc_008 = [v for v in violations if v.rule_id == "RDC-008"]
    assert len(rdc_008) == 1, [v.rule_id for v in violations]


def test_rdc_008_names_the_unsynced_clock(context) -> None:
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    rdc_008 = next(v for v in violations if v.rule_id == "RDC-008")
    assert "clk_b" in rdc_008.message
    assert "raw_rst_n" in rdc_008.message


def test_rdc_001_stays_silent(context) -> None:
    """RDC-001 is the symmetric rule for foreign-domain flop-sourced
    ARSTs. The port-source path is RDC-008's territory; RDC-001 must
    stay silent on this shape."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    rdc_001 = [v for v in violations if v.rule_id == "RDC-001"]
    assert rdc_001 == []


@pytest.mark.parametrize(
    ("typed_clock", "fires"),
    [("clk_b", False), ("clk_a", True)],
    ids=["typed_to_consumer_clock", "typed_to_other_clock"],
)
def test_rdc_008_honours_a_reset_port_typed_to_the_consumer_clock(
    typed_clock: str, fires: bool
) -> None:
    """``set_input_delay -clock clk_b [get_ports raw_rst_n]``: the
    integrator guarantees the reset is synchronous to clk_b, so the clk_b
    consumers need no synchroniser (rtl-buddy-cdc#378). Typed to clk_a
    instead, the clk_b group still fires."""
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    text = SDC.read_text() + (
        f"set_input_delay -clock {typed_clock} 1.0 [get_ports raw_rst_n]\n"
    )
    spec = sdc_mod.parse(text, backend="tokenizer")
    crossings = find_crossings(
        module, port_clock=spec.port_clock, **spec.clock_root_inputs()
    )
    fired = [v.rule_id for v in run_all_rules(module, crossings, spec)]
    assert fired.count("RDC-008") == (1 if fires else 0), fired
