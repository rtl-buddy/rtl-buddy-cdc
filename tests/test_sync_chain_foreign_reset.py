"""CDC-015 — sync chain reset from a foreign clock domain (issue #172).

The bad fixture wires a 2FF synchroniser in ``dst_clk`` whose ARST
is driven by a flop in ``src_clk``. The chain's resolving flops are
released asynchronously to ``dst_clk`` every time the foreign reset
deasserts, so the chain cannot reach steady state on its own clock.

CDC-001 / CDC-002 see a structurally valid 2FF chain (depth 2) and
stay silent — the failure is in the chain's reset path. CDC-015
fires with sync-chain-aware framing: "use dst_clk's reset for the
chain". RDC-001 *also* fires on the same physical structure with
reset-tree framing; the two findings are intentionally independent
so users can act on either.

The good fixture is structurally identical but sources the chain's
ARST from a dst_clk-domain reset flop (textbook async-assert /
sync-deassert distribution). CDC-015 stays silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_ROOT = Path(__file__).parent / "fixtures"
BAD_DIR = FIX_ROOT / "bad_sync_chain_foreign_reset"
GOOD_DIR = FIX_ROOT / "good_sync_chain_local_reset"


def _load(dir_: Path):
    name = dir_.name
    json_path = dir_ / f"{name}.json"
    sdc_path = dir_ / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    crossings = find_crossings(module, port_clock=spec.port_clock)
    return module, crossings, spec


def test_bad_fires_cdc_015() -> None:
    """One CDC-015 finding on the offending chain."""
    module, crossings, spec = _load(BAD_DIR)
    violations = run_all_rules(module, crossings, spec)
    cdc_015 = [v for v in violations if v.rule_id == "CDC-015"]
    assert len(cdc_015) == 1, (
        f"expected exactly one CDC-015 finding, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
    assert "src_clk" in cdc_015[0].message
    assert "dst_clk" in cdc_015[0].message


def test_bad_cdc_015_and_rdc_001_coexist() -> None:
    """The CDC-015 (chain-framing) and RDC-001 (reset-tree-framing)
    findings cover the same hazard from two angles; both should fire,
    not one or the other."""
    module, crossings, spec = _load(BAD_DIR)
    violations = run_all_rules(module, crossings, spec)
    rule_ids = sorted({v.rule_id for v in violations})
    assert "CDC-015" in rule_ids
    assert "RDC-001" in rule_ids
    # Structural sync rules must stay silent — the chain is depth 2
    # and valid; the bug is in the reset path.
    for cdc_only in ("CDC-001", "CDC-002", "CDC-003"):
        assert cdc_only not in rule_ids, (
            f"unexpected {cdc_only} finding on bad fixture: "
            f"{[(v.rule_id, v.message) for v in violations]}"
        )


def test_good_no_cdc_015() -> None:
    """Chain reset sourced from dst_clk's reset network: CDC-015
    silent. No other CDC rule fires either."""
    module, crossings, spec = _load(GOOD_DIR)
    violations = run_all_rules(module, crossings, spec)
    cdc_015 = [v for v in violations if v.rule_id == "CDC-015"]
    assert cdc_015 == [], (
        f"unexpected CDC-015 finding on good fixture: "
        f"{[(v.rule_id, v.message) for v in cdc_015]}"
    )
    assert violations == [], (
        f"unexpected findings on good fixture: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
