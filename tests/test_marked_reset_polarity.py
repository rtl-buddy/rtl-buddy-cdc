"""Port-level ``(* reset_polarity *)`` declaration (issue #107).

Pairs ``bad_marked_reset_polarity`` (port declared "low", flop wired
posedge → fires RDC-002 port variant) with ``good_marked_reset_polarity``
(port declared "low", flop wired negedge → clean).

The flop→flop variant of RDC-002 is exercised by
``test_bad_rdc_002_polarity_mismatch.py``; this file only covers the
port-declared override path."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import (
    USER_RESET_POLARITY_ATTRS,
    run_all as run_all_rules,
    user_reset_polarity_overrides,
)

FIX_ROOT = Path(__file__).parent / "fixtures"
BAD_DIR = FIX_ROOT / "bad_marked_reset_polarity"
GOOD_DIR = FIX_ROOT / "good_marked_reset_polarity"


def _load(dir_: Path):
    name = dir_.name
    json_path = dir_ / f"{name}.json"
    sdc_path = dir_ / f"{name}.sdc"
    if not json_path.exists():
        pytest.skip(f"fixture not built: {json_path}")
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    crossings = find_crossings(module)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    return module, async_crossings, spec


def test_attribute_aliases() -> None:
    """The accepted-aliases set is single-valued today: only the
    ``reset_polarity`` name. Sanity-check the constant so any future
    change to add aliases is forced through a test update."""
    assert USER_RESET_POLARITY_ATTRS == frozenset({"reset_polarity"})


def test_overrides_extracted_from_port_attribute() -> None:
    """``user_reset_polarity_overrides`` should return the declared
    polarity keyed by the port name."""
    module, _crossings, _spec = _load(BAD_DIR)
    overrides = user_reset_polarity_overrides(module)
    assert overrides == {"rst_n": "low"}


def test_overrides_ignored_on_non_port_nets() -> None:
    """The attribute is a *port-level* declaration; internal nets
    with the same attribute must not appear in the override map.
    The bad fixture also carries a stray module-level attribute and
    that must not bleed in either."""
    module, _crossings, _spec = _load(BAD_DIR)
    overrides = user_reset_polarity_overrides(module)
    # Only the input port should appear; the module name itself and
    # any internal nets must not.
    assert set(overrides) <= {
        p.name for p in module.ports.values() if p.direction == "input"
    }


def test_bad_fires_rdc_002_port_variant() -> None:
    """The bad fixture's posedge-on-active-low-port wiring must fire
    RDC-002 once, with a message referencing the port declaration."""
    module, async_crossings, spec = _load(BAD_DIR)
    violations = run_all_rules(module, async_crossings, spec)
    rdc_002 = [v for v in violations if v.rule_id == "RDC-002"]
    assert len(rdc_002) == 1, (
        f"expected exactly one RDC-002 finding, got: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
    msg = rdc_002[0].message
    assert "port-level declaration" in msg
    assert "rst_n" in msg


def test_good_clean() -> None:
    """The good fixture matches the declared polarity; the analyzer
    must produce zero violations."""
    module, async_crossings, spec = _load(GOOD_DIR)
    violations = run_all_rules(module, async_crossings, spec)
    assert violations == [], (
        f"unexpected violations on matching port-polarity fixture: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
