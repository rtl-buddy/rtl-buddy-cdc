"""Reset-aware sim oracle infrastructure.

Smoke-level test that the reset-aware DUTs (using
``meta_flop_arst`` / ``safe_flop_arst``) compile and run end-to-end
under the ``tb_reset_crossing.sv`` testbench, and that the runner
parses results correctly.

Discrimination between bad (foreign-domain ARST) and good
(synchronised ARST) is gated by the ``ARST_IS_ASYNC`` parameter on
``meta_flop_arst``. The bad DUT instantiates it with
``ARST_IS_ASYNC=1``; the good DUT with ``ARST_IS_ASYNC=0``. Both
DUTs go through the same testbench. The current behavioural
discrimination model is preliminary — both DUTs may produce similar
error counts due to testbench post-reset transients dominating the
injection-induced errors. A future revision will sharpen the
discrimination (e.g. by varying the post-reset settling window or
adding a reset-event-aligned golden trace).

For now, these tests pin the *infrastructure*: the DUTs compile,
the TB runs, the runner parses, the error count is well-defined.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runner import iverilog_available, run

SIM_DIR = Path(__file__).parent


pytestmark = pytest.mark.sim


@pytest.mark.skipif(not iverilog_available(), reason="iverilog/vvp not on PATH")
def test_rdc001_bad_runs_end_to_end() -> None:
    """The bad RDC-001 DUT compiles, runs, and produces a parseable
    SIM_RESULT line. Total cycles must be non-zero (TB ran)."""
    result = run(
        SIM_DIR / "dut_rdc001_bad.sv",
        {"DUT_MODULE": "dut_rdc001_bad", "LATENCY_CYCLES": 1},
    )
    assert result.total > 0


@pytest.mark.skipif(not iverilog_available(), reason="iverilog/vvp not on PATH")
def test_rdc001_good_runs_end_to_end() -> None:
    """The good RDC-001 DUT (reset-sync chain) compiles, runs, and
    produces a parseable SIM_RESULT line."""
    result = run(
        SIM_DIR / "dut_rdc001_good.sv",
        {"DUT_MODULE": "dut_rdc001_good", "LATENCY_CYCLES": 1},
    )
    assert result.total > 0
