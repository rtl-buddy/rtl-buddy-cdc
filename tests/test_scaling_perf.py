"""Hierarchical/abstraction scaling perf demo (rtl-buddy-cdc#253).

This module demonstrates the scaling win that motivates the #253 epic:
auto-abstracting single-clock subtrees to their port boundary lets the
analyzer walk far fewer flops without changing the result.

The ``scaling_perf`` fixture is one design built two ways:

- ``scaling_perf.flat.json`` — fully flattened. Every flop of all 50
  single-clock ``pipe`` instances (20 flops each) is present, so the
  crossing walk visits ~1100 flops. This is the "before".
- ``scaling_perf.json`` — the same design with each single-clock ``pipe``
  blackboxed (``read_slang --blackboxed-module pipe``). Auto-abstract
  summarises each pipe to its port boundary, so the walk visits only the
  ~100 fabric flops. This is the "after".

Both variants report the SAME single async crossing (clk_a -> clk_b) and
the SAME violations — the parity the abstraction must preserve — while the
abstracted run is measurably faster.

This test is EXCLUDED FROM CI by an env gate: it only runs when
``RTL_BUDDY_CDC_PERF`` is set in the environment (e.g.
``RTL_BUDDY_CDC_PERF=1 uv run pytest -q tests/test_scaling_perf.py``). A
plain ``uv run pytest`` skips it, so the wall-time assertions never make
CI flaky.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app

runner = CliRunner()

FIX_DIR = Path(__file__).parent / "fixtures" / "scaling_perf"
FLAT_JSON = FIX_DIR / "scaling_perf.flat.json"
BB_JSON = FIX_DIR / "scaling_perf.json"
SDC = FIX_DIR / "scaling_perf.sdc"

# Generous overall budget: the flat run is sub-second, so the whole test
# finishes in well under this. The bound is a guard, not a tight target.
TOTAL_BUDGET_S = 30.0


def _analyze(path: Path) -> tuple[dict, float]:
    """Run ``analyze`` on a netlist and return (json report, wall-time)."""
    start = time.perf_counter()
    result = runner.invoke(
        app, ["analyze", "-n", str(path), "-s", str(SDC), "-f", "json"]
    )
    elapsed = time.perf_counter() - start
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output), elapsed


def _violation_keys(report: dict) -> list[tuple[str, str, object]]:
    return sorted(
        (v["rule_id"], v["severity"], v.get("cell_name")) for v in report["violations"]
    )


@pytest.mark.skipif(
    not os.environ.get("RTL_BUDDY_CDC_PERF"),
    reason="perf fixture; set RTL_BUDDY_CDC_PERF=1 to run",
)
def test_abstraction_scaling_win() -> None:
    """The flat run walks every pipe flop; the abstracted run walks only
    the fabric flops. Assert identical results (parity) and that the
    abstracted run is faster than the flat run."""
    test_start = time.perf_counter()

    flat, t_flat = _analyze(FLAT_JSON)
    bb, t_bb = _analyze(BB_JSON)

    # Parity: identical violations and identical contract crossings.
    assert _violation_keys(flat) == _violation_keys(bb)
    assert flat["summary"]["crossings"] == bb["summary"]["crossings"]
    for key in ("violations", "suppressed", "crossings", "async_crossings"):
        assert flat["summary"][key] == bb["summary"][key], key

    # The known, identical violation: exactly one real async crossing.
    assert flat["summary"]["async_crossings"] == 1
    assert any(v["rule_id"] == "CDC-004" for v in flat["violations"])

    # The scaling win: abstraction summarises each single-clock pipe away,
    # so the abstracted run walks strictly fewer flops...
    assert bb["summary"]["flops"] < flat["summary"]["flops"]
    # ...and completes faster in wall-time.
    assert t_bb < t_flat, f"abstracted {t_bb:.3f}s not faster than flat {t_flat:.3f}s"

    total = time.perf_counter() - test_start
    assert total < TOTAL_BUDGET_S, (
        f"perf test took {total:.2f}s (budget {TOTAL_BUDGET_S}s)"
    )

    # Human-readable evidence when run with -s.
    print(
        f"\n[#253 scaling] flat: {flat['summary']['flops']} flops, {t_flat:.3f}s | "
        f"abstracted: {bb['summary']['flops']} flops, {t_bb:.3f}s | "
        f"speedup {t_flat / t_bb:.2f}x | total {total:.3f}s"
    )
