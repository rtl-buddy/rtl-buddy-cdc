"""Calibrate the Stage-4 grammar's topology-emission rate.

Closes rtl-buddy-cdc#222 done-when criterion 1: "Grammar emits ≥N
novel topologies / minute on a single core (calibrate N once Stage
3 perf is settled)."

Two figures are reported:

- **emit_only** — pure :func:`tests.fuzz.grammar.generate` rate, no
  Yosys elaboration. This is the upper bound; the grammar driver
  is pure-Python and dominated by ``random.Random`` draws + string
  formatting, so the number sits in the thousands-per-minute range.
- **elaborated** — :func:`generate` + Yosys elaboration (the full
  pipeline a mining run sees). This is the operational cost per
  case; the Yosys cache is *bypassed* per case by appending the
  seed to ``case.top``, so the measurement reflects cold elaboration
  cost rather than cache-hit cost.

Invocation::

    uv run python -m scripts.bench_grammar_rate
    uv run python -m scripts.bench_grammar_rate --duration 30 --skip-yosys

Bypasses the bench tooling pattern from rtl-buddy-cdc#221 Layer A
(the Yosys-cache numbers came from a one-off script that lived in
the PR body, not the tree) by committing the script — the
calibrated number is durable, and re-running on different hardware
takes one command instead of a copy-paste from the PR.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow ``python -m scripts.bench_grammar_rate`` when ``tests`` isn't
# already on the path; the test tree imports rtl_buddy_cdc fine, but
# scripts under ``scripts/`` need an explicit insert.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fuzz.grammar import generate  # noqa: E402
from tests.fuzz.runner import run_case  # noqa: E402
from tests.fuzz.yosys_cache import build, yosys_available  # noqa: E402


def _bench_emit_only(duration_s: float) -> tuple[int, float]:
    """Count :func:`generate` calls that fit in ``duration_s``.

    Uses an ever-increasing seed so each call follows a fresh
    composition path (no inadvertent in-process memoisation).
    """
    start = time.perf_counter()
    seed = 0
    while True:
        generate(seed=seed)
        seed += 1
        elapsed = time.perf_counter() - start
        if elapsed >= duration_s:
            break
    return seed, elapsed


def _bench_elaborated(duration_s: float) -> tuple[int, float, int]:
    """Count generate+Yosys-elaborate+analyze cycles inside ``duration_s``.

    Each case gets a unique ``top`` (the seed is already in there
    via :func:`generate`'s default) and a unique SV body (seed
    drives composition), so the Yosys content-hash cache misses on
    every call. The returned tuple is ``(cases, elapsed_s, failures)``
    — failures count cases whose analyzer findings didn't match
    the production verdicts.
    """
    if not yosys_available():
        raise RuntimeError("yosys not on PATH; --skip-yosys to bench emit-only")
    start = time.perf_counter()
    seed = 0
    failures = 0
    while True:
        case = generate(seed=seed)
        # ``build`` produces the JSON; ``run_case`` reads + analyses
        # it. Using both, not just ``build``, so the measurement
        # reflects the full per-case cost a mining run pays.
        result = run_case(case)
        if result.failures:
            failures += 1
        seed += 1
        elapsed = time.perf_counter() - start
        if elapsed >= duration_s:
            break
    return seed, elapsed, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate the Stage-4 grammar's topology-emission rate."
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="wall-clock seconds per phase (default: 10)",
    )
    parser.add_argument(
        "--skip-yosys",
        action="store_true",
        help="skip the elaborated phase (useful when yosys isn't on PATH)",
    )
    args = parser.parse_args(argv)

    print(f"Grammar rate calibration (duration={args.duration:.1f} s/phase)")
    print(f"  python: {sys.version.split()[0]}")
    print()

    # Warm-up the import path + any lazily-resolved registries so the
    # first measured call isn't paying for import cost.
    generate(seed=0)
    if not args.skip_yosys and yosys_available():
        build(generate(seed=0))

    print("Phase 1: emit-only (generate() rate)")
    cases, elapsed = _bench_emit_only(args.duration)
    rate = cases / elapsed * 60.0
    print(f"  {cases} cases in {elapsed:.2f} s → {rate:,.0f} topologies/min")
    print()

    if args.skip_yosys:
        print("Phase 2: elaborated (skipped — --skip-yosys)")
        return 0
    if not yosys_available():
        print("Phase 2: elaborated (skipped — yosys not on PATH)")
        return 0
    print("Phase 2: elaborated (generate + yosys + analyzer rate)")
    cases, elapsed, failures = _bench_elaborated(args.duration)
    rate = cases / elapsed * 60.0
    print(
        f"  {cases} cases in {elapsed:.2f} s → {rate:,.0f} topologies/min "
        f"({failures} analyzer-mismatch case(s))"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
