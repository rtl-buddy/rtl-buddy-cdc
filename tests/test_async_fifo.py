"""Async FIFO pointer-pair fixtures (issue #170).

The most common multi-bit CDC idiom in production silicon: a pair of
pointers crossing in opposite directions, gated by Gray encoding so
each individual lane sample is a coherent point in pointer-value
space. Paired bad/good:

- :data:`GOOD_DIR` — Gray-coded pointers + 2FF sync at the
  destination, full / empty derived from local-vs-synced comparison.
  CDC-004 must accept both crossings via the structural Gray detector
  (no ``(* cdc_gray *)`` annotation needed).

- :data:`BAD_DIR` — same structure with the Gray encoding dropped.
  Both crossings carry plain binary multi-bit pointers; CDC-004 must
  fire on both.

The pair also exercises CDC-005's reconvergence filter: each synced
foreign pointer fans out to a single downstream comparator, so the
phase-2 disjoint-cone filter must let it pass. If CDC-005 fires on
the good fixture, the filter has regressed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX_ROOT = Path(__file__).parent / "fixtures"
GOOD_DIR = FIX_ROOT / "good_async_fifo_gray_ptrs"
BAD_DIR = FIX_ROOT / "bad_async_fifo_binary_ptrs"


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


def test_good_no_violations() -> None:
    """Gray-coded pointer pair: both crossings pass via the structural
    Gray detector. No rule fires."""
    module, crossings, spec = _load(GOOD_DIR)
    violations = run_all_rules(module, crossings, spec)
    assert violations == [], (
        f"unexpected violations on good fixture: "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )


def test_good_has_two_async_crossings() -> None:
    """Exactly two async crossings detected (wptr and rptr); pins
    that the fixture is structurally exercising both directions."""
    _, crossings, spec = _load(GOOD_DIR)
    async_crossings = [
        c
        for c in crossings
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    assert len(async_crossings) == 2, (
        f"expected 2 async pointer crossings, got {len(async_crossings)}"
    )


def test_bad_fires_cdc_004_on_both_crossings() -> None:
    """Binary pointers: CDC-004 fires twice (one per pointer
    direction), no other rule should partially-suppress."""
    module, crossings, spec = _load(BAD_DIR)
    violations = run_all_rules(module, crossings, spec)
    cdc_004 = [v for v in violations if v.rule_id == "CDC-004"]
    assert len(cdc_004) == 2, (
        f"expected exactly two CDC-004 findings, got "
        f"{[(v.rule_id, v.message) for v in violations]}"
    )
    other = [v for v in violations if v.rule_id != "CDC-004"]
    assert other == [], (
        f"unexpected non-CDC-004 findings on bad fixture: "
        f"{[(v.rule_id, v.message) for v in other]}"
    )


def test_bad_covers_both_directions() -> None:
    """The two CDC-004 findings cover *both* pointer crossings —
    src→dst (wptr) and dst→src (rptr)."""
    module, crossings, spec = _load(BAD_DIR)
    violations = run_all_rules(module, crossings, spec)
    cdc_004 = [v for v in violations if v.rule_id == "CDC-004"]
    directions = sorted(
        f"{v.crossing.src_clock}→{v.crossing.dst_clock}"
        for v in cdc_004
        if v.crossing is not None
    )
    assert directions == ["dst_clk→src_clk", "src_clk→dst_clk"], (
        f"CDC-004 findings should cover both pointer directions, got {directions}"
    )
