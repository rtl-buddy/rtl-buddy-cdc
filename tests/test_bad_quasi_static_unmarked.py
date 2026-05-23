"""Negative counterpart to ``marked_quasi_static`` — same RTL with
the ``(* cdc_static *)`` annotations stripped from both source flops.

Without the user assertion the analyzer cannot tell a held-after-boot
register apart from a routinely-toggling one, so the structural rules
must fire:

- 1-bit depth-1 crossing on cfg_mode_q → CDC-001
- 4-bit ungated crossing on cfg_data_q → CDC-004

This sentinel exists so that the marked fixture's clean result is not
trivial: removing the attribute on identical RTL surfaces both rules,
proving the attribute is doing exactly the work claimed.

See issue #173.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_DIR = Path(__file__).parent / "fixtures" / "bad_quasi_static_unmarked"
JSON = FIX_DIR / "bad_quasi_static_unmarked.json"
SDC = FIX_DIR / "bad_quasi_static_unmarked.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_cdc_001_and_cdc_004_fire(context) -> None:
    """Both expected rules fire on the unmarked variant."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    rule_ids = sorted(v.rule_id for v in violations)
    assert rule_ids == ["CDC-001", "CDC-004"], (
        f"expected CDC-001 + CDC-004 on the unmarked fixture, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
