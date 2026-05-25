"""Per-rule coverage reporter over the fuzz corpus.

Runs every template variant through the analyzer and reports how
often each rule fires. The stage-2 plan in
``docs/proposals/fuzzer-and-simulation-oracle.md`` calls for
"every shipping rule fires ≥10 times across the corpus"; this
script is the measurement instrument.

Invocation::

    uv run python -m tests.fuzz.coverage

Output: a table sorted by rule id, columns ``rule | fires |
cases``, plus a "never fires" summary listing rules in
``rtl_buddy_cdc.rules.RULES`` that this corpus did not exercise.
"""

from __future__ import annotations

import sys
from collections import Counter

from rtl_buddy_cdc.rules import RULES

from .runner import iter_results


def main() -> int:
    rule_fires: Counter[str] = Counter()
    rule_cases: Counter[str] = Counter()
    case_count = 0
    failed_cases: list[str] = []

    for result in iter_results():
        case_count += 1
        if result.failures:
            failed_cases.append(result.case.case_id)
        for rule_id, count in result.fired.items():
            rule_fires[rule_id] += count
            rule_cases[rule_id] += 1

    print(f"Corpus: {case_count} cases")
    if failed_cases:
        print(f"FAILED: {len(failed_cases)} case(s)")
        for cid in failed_cases:
            print(f"  - {cid}")

    print()
    print(f"{'rule':<10} {'fires':>6} {'cases':>6}")
    print(f"{'-' * 10} {'-' * 6} {'-' * 6}")
    for rule_id in sorted(rule_fires):
        print(f"{rule_id:<10} {rule_fires[rule_id]:>6} {rule_cases[rule_id]:>6}")

    never_fired = [r for r in sorted(RULES) if r not in rule_fires]
    if never_fired:
        print()
        print(f"Never fired ({len(never_fired)}/{len(RULES)}):")
        for rule_id in never_fired:
            print(f"  - {rule_id}")

    return 0 if not failed_cases else 1


if __name__ == "__main__":
    sys.exit(main())
