"""Regression probes for signoff CDC/RDC coverage gaps.

These tests intentionally document feature classes that are not fully
implemented yet. Expected failures should become normal assertions as
rules are added.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_ROOT = Path(__file__).parent / "fixtures"


def _violations(name: str):
    fix_dir = FIX_ROOT / name
    module = netlist.load(fix_dir / f"{name}.json")
    spec = sdc_mod.parse_file(fix_dir / f"{name}.sdc")
    sdc_mod.synthesize_unconstrained_inputs(spec, module)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return run_all_rules(module, crossings, spec)


@pytest.mark.xfail(strict=True, reason="functional data-enable stability rule pending")
def test_bad_functional_datahold_enable_is_reported() -> None:
    rule_ids = {v.rule_id for v in _violations("bad_functional_datahold_enable")}
    assert "CDC-012" in rule_ids


@pytest.mark.xfail(strict=True, reason="fast-to-slow event-loss rule pending")
def test_bad_fast_to_slow_control_loss_is_reported() -> None:
    rule_ids = {v.rule_id for v in _violations("bad_fast_to_slow_control_loss")}
    assert "CDC-013" in rule_ids


@pytest.mark.xfail(strict=True, reason="derived async reset sync rule pending")
def test_bad_derived_async_reset_unsync_is_reported() -> None:
    rule_ids = {v.rule_id for v in _violations("bad_derived_async_reset_unsync")}
    assert "RDC-006" in rule_ids


def test_good_derived_async_reset_synced_stays_clean() -> None:
    violations = _violations("good_derived_async_reset_synced")
    assert violations == []
