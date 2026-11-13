"""Negative-case fixture: a one-flop synchroniser whose first stage is
exported straight to an output port and also pipelined → CDC-001
(rtl-buddy-cdc#344).

The port tap is a second reader of the first stage, so the pipeline
register behind it is not a second synchroniser stage. Before the fix
``_bit_reader_count`` counted cell input pins only and the design
passed clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rtl_buddy_cdc import netlist, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.rules import _bit_reader_count, _build_context, _sync_chain_depth
from rtl_buddy_cdc.rules import run_all as run_all_rules

FIX = Path(__file__).parent / "fixtures"
JSON = FIX / "bad_exported_sync_stage" / "bad_exported_sync_stage.json"
SDC = FIX / "bad_exported_sync_stage" / "bad_exported_sync_stage.sdc"
GOOD_JSON = FIX / "good_exported_sync_stage" / "good_exported_sync_stage.json"
GOOD_SDC = FIX / "good_exported_sync_stage" / "good_exported_sync_stage.sdc"


def _load(json_path: Path, sdc_path: Path):
    module = netlist.load(json_path)
    spec = sdc_mod.parse_file(sdc_path)
    crossings = [
        c
        for c in find_crossings(module)
        if spec.are_async(
            spec.clock_for_port(c.src_clock) or c.src_clock,
            spec.clock_for_port(c.dst_clock) or c.dst_clock,
        )
    ]
    return module, spec, crossings


@pytest.fixture(scope="module")
def bad():
    return _load(JSON, SDC)


@pytest.fixture(scope="module")
def good():
    return _load(GOOD_JSON, GOOD_SDC)


def test_one_async_crossing(bad) -> None:
    _module, _spec, crossings = bad
    assert len(crossings) == 1


def test_cdc_001_fires_and_nothing_else(bad) -> None:
    module, spec, crossings = bad
    violations = run_all_rules(module, crossings, spec)
    assert [v.rule_id for v in violations] == ["CDC-001"]
    assert "chain depth = 1" in violations[0].message
    assert violations[0].severity == "error"


def test_exported_first_stage_has_two_readers(bad) -> None:
    """The first stage's ``Q`` is read by the pipeline flop and by the
    ``q_tap`` output port: two readers, so the chain stops at depth 1."""
    module, spec, crossings = bad
    (crossing,) = crossings
    q = crossing.dst_flop.q[0]
    assert q in module.ports["q_tap"].bits
    assert _bit_reader_count(module)[q] == 2
    ctx = _build_context(module, spec)
    assert ctx.reader_counts[q] == 2
    depth = _sync_chain_depth(
        module, crossing.dst_flop, crossing.dst_clock, ctx.domains
    )
    assert depth == 1


def test_good_twin_last_stage_tap_ends_the_chain_at_two(good) -> None:
    """Twin: the tap is on the *last* stage of a 2FF synchroniser. The
    chain is two deep (CDC-001 / CDC-002 silent), and the tap stops it
    there, so the two pipeline registers behind it are not counted as
    a 4-flop cascade (CDC-018 silent)."""
    module, spec, crossings = good
    (crossing,) = crossings
    ctx = _build_context(module, spec)
    depth = _sync_chain_depth(
        module, crossing.dst_flop, crossing.dst_clock, ctx.domains
    )
    assert depth == 2
    assert run_all_rules(module, crossings, spec) == []
