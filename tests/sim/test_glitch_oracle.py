"""Stage 4 clock-glitch oracle (rtl-buddy-cdc#190).

Two sim probes that pair with the analyzer's CDC-010 and CDC-008
rules:

- ``test_clock_mux_emits_glitches`` — drives the combinational
  clock-mux DUT and asserts the muxed output emits *more* posedges
  than its baseline ck0_a would, evidence of runt pulses on sel
  transitions. Pairs with the analyzer's CDC-010 firing on the
  same shape: ``tests/fuzz/templates/cdc010.py`` and the
  ``bad_async_clock_mux`` hand-authored fixture.

- ``test_clock_as_data_fails_like_unsynced`` — a flop sampling a
  foreign clock as data must produce a non-trivial error rate
  under metastability injection. Pairs with CDC-008 firing on the
  same shape.

These don't replace the gap-mining oracle (``test_oracle.py``);
they add structural-failure coverage for two rules whose failure
modes aren't dominantly stochastic-flip but clock-network shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .runner import iverilog_available, run

SIM_DIR = Path(__file__).parent


pytestmark = pytest.mark.sim


@pytest.mark.skipif(not iverilog_available(), reason="iverilog/vvp not on PATH")
def test_clock_mux_emits_glitches() -> None:
    """CDC-010 oracle: a combinational mux on a foreign-domain
    select emits runt pulses on every sel transition that lands
    during a window where the two muxed clocks disagree. The TB
    counts posedges of the muxed clock vs. its ck0_a baseline; the
    bad DUT's excess is non-zero.

    The CI threshold of 50 excess edges across ~5000 cycles is well
    above noise (zero) but well below the typical observed value
    (~500–600); regressions that quietly stop producing glitches
    (e.g., Icarus's mux-output settle behaviour changing across
    versions) will trip this."""
    result = run(
        SIM_DIR / "dut_clock_mux_glitch.sv",
        {"DUT_MODULE": "dut_clock_mux_glitch"},
    )
    assert result.errors > 50, (
        f"clock-mux DUT produced only {result.errors} excess edges "
        f"over {result.total} baseline cycles — oracle has regressed "
        f"or Icarus's glitch behaviour has changed"
    )


@pytest.mark.skipif(not iverilog_available(), reason="iverilog/vvp not on PATH")
def test_clock_as_data_fails_like_unsynced() -> None:
    """CDC-008 oracle: a flop in main_clk's domain that samples a
    foreign clock as data must show a non-trivial divergence from
    a 1-cycle-delayed reference. Reuses ``tb_crossing.sv``'s golden
    model with LATENCY_CYCLES=1 — the same comparison shape as the
    unsynced data DUT in ``test_oracle.py``.

    A clock-as-data crossing is metastability-prone exactly like a
    plain unsynced data crossing — every transition of snoop_clk
    has a chance of landing in main_clk's setup/hold window."""
    result = run(
        SIM_DIR / "dut_clock_as_data.sv",
        {"DUT_MODULE": "dut_clock_as_data", "LATENCY_CYCLES": 1},
    )
    assert result.errors > 100, (
        f"clock-as-data DUT produced only {result.errors} errors "
        f"over {result.total} cycles — oracle should see hundreds "
        f"under 80% injection; injection setup may have regressed"
    )
