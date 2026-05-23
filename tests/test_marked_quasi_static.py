"""SV-attribute coverage: ``(* cdc_static *)`` user escape hatch.

The fixture wires a 1-bit and a 4-bit configuration register in
``cfg_clk`` and reads them directly (depth 1, no synchroniser, no
gating) on ``dst_clk``. Without the attribute this would fire
CDC-001 (1-bit) and CDC-004 (4-bit). The annotation on the source
flops asserts the values are runtime-constant during operation —
metastability cannot occur on a non-transitioning value — so the
rule pack must skip both crossings.

Paired sentinel: :file:`test_bad_quasi_static_unmarked.py` runs the
same RTL with the annotations stripped and asserts both rules fire.
Together they pin the contract that the attribute is doing exactly
the work claimed.

See issue #173.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import (
    USER_STATIC_ATTRS,
    run_all as run_all_rules,
    user_static_flop_names,
)

FIX_DIR = Path(__file__).parent / "fixtures" / "marked_quasi_static"
JSON = FIX_DIR / "marked_quasi_static.json"
SDC = FIX_DIR / "marked_quasi_static.sdc"


@pytest.fixture(scope="module")
def context():
    if not JSON.exists():
        pytest.skip(f"fixture not built: {JSON}")
    module = netlist.load(JSON)
    spec = sdc_mod.parse_file(SDC)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_attribute_aliases() -> None:
    """The two aliases users may write are public surface."""
    assert "cdc_static" in USER_STATIC_ATTRS
    assert "quasi_static" in USER_STATIC_ATTRS


def test_marked_flops_detected(context) -> None:
    """Both source registers (1-bit cfg_mode_q and 4-bit cfg_data_q)
    end up in :func:`user_static_flop_names`."""
    module, _crossings, _spec = context
    statics = user_static_flop_names(module)
    assert len(statics) == 2, (
        f"expected exactly two (* cdc_static *)-marked flops, got {sorted(statics)}"
    )


def test_no_violations_with_attribute(context) -> None:
    """The annotation suppresses CDC-001 and CDC-004 on both crossings."""
    module, crossings, spec = context
    violations = run_all_rules(module, crossings, spec)
    assert violations == [], (
        f"expected no findings with (* cdc_static *) suppression, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
