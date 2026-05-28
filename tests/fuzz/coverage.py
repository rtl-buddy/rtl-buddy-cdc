"""Per-rule coverage reporter over the fuzz corpus.

Stage-3 (rtl-buddy-cdc#221) Layer-C extension: the report tracks
fires under **each frontend independently** so incomplete slang
parity is obvious at a glance. The historical single-column
(Yosys-only) shape is removed in favour of the per-frontend layout.

Layer B (xeno mutator) will add a third ``mutants`` column when it
lands; the layout below is designed to accept it without further
restructuring.

The Stage-2 plan in rtl-buddy-cdc#190 calls for "every shipping rule
fires ≥10 times across the corpus." With the differential oracle in
place the bar applies separately per frontend — a rule that fires
≥10× in Yosys but 0× in slang is still a coverage gap even though
the rule pack itself is exercised.

Invocation::

    uv run python -m tests.fuzz.coverage

Output: a table sorted by rule id with columns
``rule | yosys_fires | slang_fires | disagree | cases``. The
``disagree`` column counts cases where the per-rule fired counts
differ between the two frontends — high values point at the
slang-parity gaps tracked in rtl-buddy-cdc#224. Slang columns
show ``—`` when pyslang isn't importable in the current env.
"""

from __future__ import annotations

import sys
from collections import Counter

from rtl_buddy_cdc.rules import RULES

from .runner import collect_cases, run_case, run_case_slang
from .slang_cache import slang_available
from .yosys_cache import yosys_available


def main() -> int:
    if not yosys_available():
        print("yosys not on PATH; corpus coverage requires yosys")
        return 2

    run_slang = slang_available()

    yosys_rule_fires: Counter[str] = Counter()
    yosys_rule_cases: Counter[str] = Counter()
    slang_rule_fires: Counter[str] = Counter()
    slang_rule_cases: Counter[str] = Counter()
    per_rule_disagree: Counter[str] = Counter()
    case_count = 0
    failed_cases: list[str] = []
    slang_skip_count = 0

    for case in collect_cases():
        case_count += 1
        yosys_result = run_case(case)
        if yosys_result.failures:
            failed_cases.append(case.case_id)
        for rule_id, count in yosys_result.fired.items():
            yosys_rule_fires[rule_id] += count
            yosys_rule_cases[rule_id] += 1

        if not run_slang:
            continue

        try:
            slang_result = run_case_slang(case)
        except Exception:
            # Slang refuses illegal SV; the case still counts as a
            # Yosys datapoint but skips the slang side.
            slang_skip_count += 1
            continue

        for rule_id, count in slang_result.fired.items():
            slang_rule_fires[rule_id] += count
            slang_rule_cases[rule_id] += 1

        # Per-rule disagreement count: any rule whose fired count
        # differs between the frontends contributes one to the
        # disagreement column. This is the per-rule view of the
        # corpus-level agreement rate that rtl-buddy-cdc#221 done-when
        # tracks.
        all_rules = set(yosys_result.fired) | set(slang_result.fired)
        for rule_id in all_rules:
            if yosys_result.fired.get(rule_id, 0) != slang_result.fired.get(rule_id, 0):
                per_rule_disagree[rule_id] += 1

    print(f"Corpus: {case_count} cases (yosys)")
    if run_slang:
        print(
            f"        {case_count - slang_skip_count} cases (slang); "
            f"{slang_skip_count} skipped"
        )
    else:
        print("        slang frontend disabled (pyslang not importable)")
    if failed_cases:
        print(f"FAILED: {len(failed_cases)} case(s)")
        for cid in failed_cases:
            print(f"  - {cid}")

    print()
    if run_slang:
        header = (
            f"{'rule':<10} "
            f"{'yosys_fires':>11} {'yosys_cases':>11}  "
            f"{'slang_fires':>11} {'slang_cases':>11}  "
            f"{'disagree':>8}"
        )
        sep = f"{'-' * 10} {'-' * 11} {'-' * 11}  {'-' * 11} {'-' * 11}  {'-' * 8}"
    else:
        header = (
            f"{'rule':<10} {'yosys_fires':>11} {'yosys_cases':>11}  "
            f"{'slang_fires':>11} {'slang_cases':>11}  {'disagree':>8}"
        )
        sep = f"{'-' * 10} {'-' * 11} {'-' * 11}  {'-' * 11} {'-' * 11}  {'-' * 8}"

    print(header)
    print(sep)
    seen_rules = sorted(set(yosys_rule_fires) | set(slang_rule_fires))
    for rule_id in seen_rules:
        yf = yosys_rule_fires[rule_id]
        yc = yosys_rule_cases[rule_id]
        if run_slang:
            sf_disp: str = str(slang_rule_fires[rule_id])
            sc_disp: str = str(slang_rule_cases[rule_id])
            disagree_disp: str = str(per_rule_disagree[rule_id])
        else:
            sf_disp = sc_disp = disagree_disp = "—"
        print(
            f"{rule_id:<10} "
            f"{yf:>11} {yc:>11}  "
            f"{sf_disp:>11} {sc_disp:>11}  "
            f"{disagree_disp:>8}"
        )

    never_fired = [r for r in sorted(RULES) if r not in yosys_rule_fires]
    if never_fired:
        print()
        print(f"Never fired ({len(never_fired)}/{len(RULES)}):")
        for rule_id in never_fired:
            print(f"  - {rule_id}")

    return 0 if not failed_cases else 1


if __name__ == "__main__":
    sys.exit(main())
