"""Differential sim oracle: bad DUT must produce errors; good 2FF
must not. If both produce zero errors, the meta_flop injection
methodology has regressed and the oracle is no longer probing the
failure mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runner import iverilog_available, run

SIM_DIR = Path(__file__).parent


pytestmark = pytest.mark.sim


@pytest.mark.skipif(not iverilog_available(), reason="iverilog/vvp not on PATH")
def test_unsynced_crossing_produces_errors() -> None:
    """The bad DUT with no synchroniser must show a non-zero error
    rate under aggressive metastability injection. If this drops to
    zero, the oracle is producing false negatives — bug in either
    the meta_flop library or the testbench's golden model."""
    result = run(
        SIM_DIR / "dut_unsynced.sv",
        {"DUT_MODULE": "dut_unsynced", "LATENCY_CYCLES": 1},
    )
    assert result.errors > 0, (
        f"unsynced DUT produced zero sim errors over {result.total} "
        f"cycles — oracle has regressed"
    )


@pytest.mark.skipif(not iverilog_available(), reason="iverilog/vvp not on PATH")
def test_2ff_synchroniser_is_clean() -> None:
    """The good 2FF DUT must produce zero errors despite the first
    stage having 80% injection. If this becomes non-zero, the 2FF
    chain is no longer absorbing metastability — would indicate a
    bug in the meta_flop / safe_flop library."""
    result = run(
        SIM_DIR / "dut_2ff.sv",
        {"DUT_MODULE": "dut_2ff", "LATENCY_CYCLES": 3},
    )
    assert result.errors == 0, (
        f"2FF DUT produced {result.errors} errors over {result.total} "
        f"cycles — chain isn't absorbing metastability"
    )


@pytest.mark.skipif(not iverilog_available(), reason="iverilog/vvp not on PATH")
def test_differential_signal_is_clear() -> None:
    """End-to-end oracle sanity check: bad DUT's error rate must be
    materially higher than good DUT's. This is the assertion that
    the entire methodology hinges on — if it ever fails, the oracle
    is broken regardless of which side regressed."""
    bad = run(
        SIM_DIR / "dut_unsynced.sv",
        {"DUT_MODULE": "dut_unsynced", "LATENCY_CYCLES": 1},
    )
    good = run(
        SIM_DIR / "dut_2ff.sv",
        {"DUT_MODULE": "dut_2ff", "LATENCY_CYCLES": 3},
    )
    assert bad.error_rate > good.error_rate + 0.001, (
        f"bad/good differential too small: bad={bad.error_rate:.4f} "
        f"good={good.error_rate:.4f} (need bad > good + 0.001)"
    )


@pytest.mark.skipif(not iverilog_available(), reason="iverilog/vvp not on PATH")
def test_gap_g1_latch_sync_fails_like_unsynced() -> None:
    """Gap-mining payoff: a "synchroniser" built from a transparent
    latch + flop must fail simulation at roughly the same rate as a
    plain unsynced crossing. The analyzer cannot see this design
    (find_crossings returns 0); this sim case is the evidence that
    its silence is a false negative, not a correct exemption.

    Pair with ``tests/fuzz/templates/gap_g1.py``: the fuzz template
    pins the analyzer's silence; this sim test pins the functional
    failure. Together they meet the gap-mining proof bar: sim
    fails, analyzer silent."""
    latch = run(
        SIM_DIR / "dut_latch_sync.sv",
        {"DUT_MODULE": "dut_latch_sync", "LATENCY_CYCLES": 1},
    )
    good = run(
        SIM_DIR / "dut_2ff.sv",
        {"DUT_MODULE": "dut_2ff", "LATENCY_CYCLES": 3},
    )
    assert latch.error_rate > good.error_rate + 0.001, (
        f"G-1 latch-sync DUT must fail materially worse than the 2FF "
        f"reference: latch={latch.error_rate:.4f} "
        f"good={good.error_rate:.4f}"
    )
