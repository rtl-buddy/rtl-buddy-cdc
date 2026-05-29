"""Bounded gap-mining run over Stage-4 grammar topologies.

Operationalises rtl-buddy-cdc#222 done-when criterion 2:

> Generated corpus surfaces ≥1 new gap candidate beyond what Stage
> 3's mutation surfaces. The "novel topology found a new gap" event
> is the success signal; "no new gaps" after a bounded mining run
> is the failure signal and Stage 4 stops being prioritised.

What counts as a gap candidate
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For each grammar seed:

1. Compose the case and read its declared prediction
   (``ExpectedFinding.GE`` rules in ``case.expected``).
2. Run the case through Yosys + the analyzer; collect the set of
   rules that actually fired.
3. Compute ``surprise = actual_fires - predicted_added``. Rules
   in ``surprise`` fired in spite of no production declaring them
   — either a *known co-fire* (e.g. CDC-001 typically pairs with
   CDC-002 on a depth=0 crossing) or a *real gap* the productions
   don't yet model.

The script groups surprise patterns by frequency. A persistent,
high-frequency surprise pattern that doesn't correspond to a
known co-fire is the actionable signal — file a gap candidate
against rtl-buddy-cdc (the issue body becomes the design record;
see AGENTS.md "Design proposals live on GitHub").

Known co-fires (suppressed)
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Some rule pairs always fire together by analyzer construction —
suppressing them keeps the surprise report focused on novel
patterns. The :data:`_KNOWN_COFIRES` mapping documents each pair
and where the partition is defined in the rule pack.

Invocation
~~~~~~~~~~

::

    uv run python -m scripts.gap_mining
    uv run python -m scripts.gap_mining --seeds 5000
    uv run python -m scripts.gap_mining --seeds 200 --quiet
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fuzz.grammar import generate  # noqa: E402
from tests.fuzz.runner import run_case  # noqa: E402
from tests.fuzz.yosys_cache import yosys_available  # noqa: E402

# Rule pairs that the analyzer is structured to always fire
# together. Suppressing the co-rule from the surprise set focuses
# the report on patterns the grammar's productions don't yet model.
# Format: ``predicted_rule -> {co-rules that legitimately fire alongside}``.
_KNOWN_COFIRES: dict[str, frozenset[str]] = {
    # CDC-012 (gated bus, no handshake) is detected on the same
    # crossing as the underlying multi-bit CDC-001 — gap_g5's
    # template documents this pairing.
    "CDC-012": frozenset({"CDC-001"}),
}


def _predicted_added(case) -> set[str]:
    """Collect the rules the case's composed prediction declares
    will fire (``Op.GE``-style entries in ``case.expected``)."""
    return {ef.rule_id for ef in case.expected}


def _suppress_known_cofires(surprise: set[str], predicted: set[str]) -> set[str]:
    """Remove rules from ``surprise`` that are documented co-fires
    of any rule in ``predicted``."""
    out = set(surprise)
    for rule in predicted:
        out -= _KNOWN_COFIRES.get(rule, frozenset())
    return out


def _mine_seeds(
    seeds: range, *, verbose: bool
) -> tuple[
    Counter,
    list[tuple[int, frozenset[str], frozenset[str]]],
    Counter,
    list[tuple[int, frozenset[str], frozenset[str]]],
]:
    """Run the mining loop. Returns four pieces:

    - ``surprise_freq`` — Counter of (frozenset of surprise rules)
      → frequency. Each entry is a *rule fired without prediction*.
    - ``surprise_candidates`` — sample (seed, predicted, surprise)
      tuples for the surprise patterns.
    - ``missing_freq`` — Counter of (frozenset of missing rules) →
      frequency. Each entry is a *rule predicted but didn't fire*.
      That's the false-negative signal — either the production is
      lying about its verdict, or the analyzer missed a finding.
    - ``missing_candidates`` — sample (seed, predicted, missing)
      tuples for the missing patterns.
    """
    surprise_freq: Counter[frozenset[str]] = Counter()
    surprise_candidates: list[tuple[int, frozenset[str], frozenset[str]]] = []
    missing_freq: Counter[frozenset[str]] = Counter()
    missing_candidates: list[tuple[int, frozenset[str], frozenset[str]]] = []

    for seed in seeds:
        case = generate(seed=seed)
        predicted = _predicted_added(case)
        try:
            result = run_case(case)
        except Exception as exc:
            if verbose:
                print(f"  seed={seed}: yosys failed — {exc.__class__.__name__}")
            continue

        fired = {r for r, n in result.fired.items() if n > 0}
        surprise = _suppress_known_cofires(fired - predicted, predicted)
        if surprise:
            key = frozenset(surprise)
            surprise_freq[key] += 1
            surprise_candidates.append((seed, frozenset(predicted), key))

        missing = predicted - fired
        if missing:
            key = frozenset(missing)
            missing_freq[key] += 1
            missing_candidates.append((seed, frozenset(predicted), key))

    return surprise_freq, surprise_candidates, missing_freq, missing_candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded Stage-4 grammar gap-mining run."
    )
    parser.add_argument(
        "--seeds", type=int, default=1000, help="number of grammar seeds (default 1000)"
    )
    parser.add_argument(
        "--start", type=int, default=0, help="first seed value (default 0)"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="don't print per-candidate diagnostic rows"
    )
    args = parser.parse_args(argv)

    if not yosys_available():
        print("yosys not on PATH; gap-mining requires yosys")
        return 2

    seeds = range(args.start, args.start + args.seeds)
    print(f"Gap mining: {args.seeds} grammar seeds ({seeds.start}..{seeds.stop - 1})")
    print()

    surprise_freq, surprise_candidates, missing_freq, missing_candidates = _mine_seeds(
        seeds, verbose=not args.quiet
    )

    print(
        f"Surprise (rule fired without prediction): "
        f"{len(surprise_candidates)} / {args.seeds} cases"
    )
    print(
        f"Missing  (rule predicted but didn't fire): "
        f"{len(missing_candidates)} / {args.seeds} cases"
    )
    print()

    if not surprise_freq and not missing_freq:
        print("No surprise or missing patterns observed.")
        print("Bounded-run failure-signal per #222 done-when 2 — Stage 4 stops")
        print("being prioritised unless productions expand.")
        return 0

    if surprise_freq:
        print(f"Surprise patterns (by frequency, {len(surprise_freq)} unique):")
        for rules, freq in surprise_freq.most_common():
            share = freq / args.seeds * 100.0
            print(f"  {freq:>4} × ({share:5.1f}%)  {sorted(rules)}")
        if not args.quiet:
            print()
            print("Sample surprise candidates (first 5):")
            for seed, predicted, surprise in surprise_candidates[:5]:
                print(
                    f"  seed={seed:>4}  predicted={sorted(predicted)}  "
                    f"surprise={sorted(surprise)}"
                )
        print()

    if missing_freq:
        print(f"Missing patterns (by frequency, {len(missing_freq)} unique):")
        for rules, freq in missing_freq.most_common():
            share = freq / args.seeds * 100.0
            print(f"  {freq:>4} × ({share:5.1f}%)  {sorted(rules)}")
        if not args.quiet:
            print()
            print("Sample missing candidates (first 5):")
            for seed, predicted, missing in missing_candidates[:5]:
                print(
                    f"  seed={seed:>4}  predicted={sorted(predicted)}  "
                    f"missing={sorted(missing)}"
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
