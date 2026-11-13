"""Multi-bit simulation oracle for CDC-004 and CDC-005 (rtl-buddy-cdc#331).

Each DUT is checked from both sides, on one source file:

- **Analyzer.** The committed netlist ``netlists/<dut>.json`` (the DUT
  elaborated by Yosys with the plain-flop stand-ins of
  ``meta_flop_synth.sv``; see :func:`runner.build_netlist`) goes through
  ``rtl-buddy-cdc analyze`` with ``netlists/multibit.sdc``. The per-rule
  finding counts must equal the case's ``fired``. Needs neither Yosys nor
  Icarus, so it runs in every CI job.
- **Simulation.** Icarus runs the DUT with the injecting
  ``meta_flop_bus*`` models under ``tb_bus_crossing.sv``, whose
  scoreboard counts destination samples that are not a value the source
  held within the last few destination periods (the property is stated
  in the testbench header). Every one of :data:`RUNS` — fixed seeds and
  clock relationships, so the result is deterministic — must fail for
  an unsafe DUT and must be clean for a safe one.
- **Agreement.** The analyzer reports a finding exactly when the
  simulation fails.

A disagreement is a finding about the analyzer (or the oracle); per
rtl-buddy-cdc#331 it is recorded as a strict ``xfail`` with a reproducer,
never tuned away. None is open today.

The testbench keeps src_clk slower than dst_clk in every run. The
injection model treats a lane that changed at any time since the
previous destination edge as possibly metastable — a full-period
aperture, where silicon has a few picoseconds — so a gray counter that
advances twice inside one destination period would show two uncertain
lanes, an artifact of the model rather than a silicon failure.
"""

from __future__ import annotations

import functools
import json
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app

from .runner import (
    NETLIST_DIR,
    SIM_DIR,
    build_netlist,
    iverilog_available,
    multibit_duts,
    run,
    yosys_available,
)

pytestmark = pytest.mark.sim

SDC = NETLIST_DIR / "multibit.sdc"


@dataclass(frozen=True)
class Case:
    dut: str
    #: Width of ``q_out`` / ``src_data`` (the testbench's ``BUS_W``).
    width: int
    #: Exact per-rule finding counts of ``analyze`` on the netlist.
    fired: dict[str, int]
    #: The simulation must show incoherent samples in every run.
    unsafe: bool


CASES = (
    # CDC-004: binary counter through an ungated 4-bit 2FF — lanes
    # resolve independently, a carry is sampled half old / half new.
    Case("dut_bus_binary", 4, {"CDC-004": 1}, unsafe=True),
    # CDC-004 protections: gray-coded source into a multi-bit sync
    # chain; payload captured under a four-phase req/ack handshake.
    Case("dut_bus_gray", 4, {}, unsafe=False),
    Case("dut_bus_handshake", 4, {}, unsafe=False),
    # CDC-005: one source bit through two 2FF chains, recombined.
    Case("dut_reconv_fanout", 2, {"CDC-005": 1}, unsafe=True),
    # The related-bits form: a one-hot 2-bit state, one 2FF per bit.
    # The state register is the one source flop of both chains
    # (CDC-005) and a multi-bit register crossing lane by lane
    # (CDC-020).
    Case("dut_reconv_onehot", 2, {"CDC-005": 1, "CDC-020": 1}, unsafe=True),
    # CDC-005 fixes: synchronise once and fan out in dst_clk; encode
    # the one-hot state as one bit, cross it, decode after the sync.
    Case("dut_reconv_single_sync", 2, {}, unsafe=False),
    Case("dut_reconv_onehot_encoded", 2, {}, unsafe=False),
)

#: Fixed run configurations: (dst_clk half period ps, src_clk start
#: phase ps, stimulus seed, injection seed, ``inc`` probability %).
#: src_clk's half period stays at the testbench's 10000 ps, so every
#: run keeps src_clk slower than dst_clk (see the module docstring)
#: while the ratio and phase move the source edges across the whole
#: destination period. Eight runs of 5000 destination cycles each.
RUNS = (
    (7500, 0, 1, 11, 50),
    (6100, 1234, 2, 22, 30),
    (8700, 4567, 3, 33, 70),
    (9300, 250, 4, 44, 50),
    (5300, 7777, 5, 55, 40),
    (9900, 3141, 6, 66, 60),
    (7100, 9001, 7, 77, 35),
    (8300, 555, 8, 88, 65),
)


def _case_id(case: Case) -> str:
    return case.dut


@functools.cache
def _analyzer_verdict(netlist: Path) -> dict[str, int]:
    """Per-rule finding counts of ``rtl-buddy-cdc analyze``."""
    with tempfile.TemporaryDirectory(prefix="rbcdc-sim-") as tmp:
        report = Path(tmp) / "report.json"
        result = CliRunner().invoke(
            app,
            [
                "analyze",
                "--netlist",
                str(netlist),
                "--sdc",
                str(SDC),
                "--format",
                "json",
                "--output",
                str(report),
            ],
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(report.read_text())
    return dict(Counter(v["rule_id"] for v in data["violations"]))


def _simulate(case: Case) -> list[tuple[int, int, int]]:
    """``(errors, total, updates)`` of every run in :data:`RUNS`."""
    out = []
    for dst_half, phase, stim_seed, meta_seed, inc_pct in RUNS:
        res = run(
            SIM_DIR / f"{case.dut}.sv",
            {"DUT_MODULE": case.dut, "BUS_W": case.width},
            {
                "DST_HALF_PS": dst_half,
                "SRC_PHASE_PS": phase,
                "STIM_SEED": stim_seed,
                "META_SEED": meta_seed,
                "INC_PCT": inc_pct,
            },
        )
        out.append((res.errors, res.total, res.stats["updates"]))
    return out


def test_every_multibit_dut_has_a_case() -> None:
    """A new ``dut_bus_*`` / ``dut_reconv_*`` DUT needs a verdict here,
    and every case needs its committed netlist."""
    assert {p.stem for p in multibit_duts()} == {c.dut for c in CASES}
    for case in CASES:
        assert (NETLIST_DIR / f"{case.dut}.json").is_file(), case.dut


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_analyzer_verdict(case: Case) -> None:
    """Analyzer half: exact per-rule counts on the committed netlist."""
    fired = _analyzer_verdict(NETLIST_DIR / f"{case.dut}.json")
    assert fired == case.fired


@pytest.mark.skipif(not yosys_available(), reason="yosys not on PATH")
@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_committed_netlist_is_current(case: Case, tmp_path: Path) -> None:
    """The committed netlist still matches its DUT source: rebuilding it
    gives the same analyzer verdict. Compared on verdicts, not bytes,
    so a Yosys version bump does not churn the check. Regenerate with
    ``uv run python -m tests.sim.runner``."""
    fresh = tmp_path / f"{case.dut}.json"
    build_netlist(SIM_DIR / f"{case.dut}.sv", fresh)
    assert _analyzer_verdict(fresh) == case.fired


@pytest.mark.skipif(not iverilog_available(), reason="iverilog/vvp not on PATH")
@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_simulation_agrees_with_analyzer(case: Case) -> None:
    """Both halves and their agreement, for one DUT."""
    fired = _analyzer_verdict(NETLIST_DIR / f"{case.dut}.json")
    assert fired == case.fired
    analyzer_unsafe = bool(fired)

    runs = _simulate(case)
    detail = ", ".join(f"{e}/{t} (updates {u})" for e, t, u in runs)
    # Liveness: a DUT whose output never moves would pass the coherence
    # check vacuously.
    assert all(u >= 100 for _, _, u in runs), f"q_out barely moves: {detail}"
    # The simulation verdict must be the same in every run: failing in
    # some runs only would make the outcome depend on the seed choice.
    sim_unsafe = all(e > 0 for e, _, _ in runs)
    sim_safe = all(e == 0 for e, _, _ in runs)
    assert sim_unsafe or sim_safe, f"{case.dut}: mixed outcome, errors/total {detail}"
    assert sim_unsafe == case.unsafe, (
        f"{case.dut}: expected "
        f"{'incoherent samples' if case.unsafe else 'no incoherent sample'}, "
        f"got errors/total {detail}"
    )
    assert analyzer_unsafe == sim_unsafe, (
        f"{case.dut}: analyzer fired {fired} but simulation "
        f"{'fails' if sim_unsafe else 'is clean'}"
    )
