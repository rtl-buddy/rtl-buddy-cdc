"""Per-rule coverage reporter over the fuzz corpus.

Stage-3 (rtl-buddy-cdc#221) full extension: the report tracks fires
under **each surface independently** so incomplete parity / coverage
is obvious at a glance. The historical single-column (Yosys-only)
shape is removed in favour of three independent surfaces:

- ``yosys`` — the canonical analyzer path; the historical surface.
- ``slang`` — the in-process slang frontend (Layer C); empty when
  pyslang isn't importable.
- ``mutants`` — xeno-generated mutated SV (Layer B); empty when
  rtl-buddy-xeno isn't importable. Per-mutant elaboration runs
  through the Yosys pipeline (mutants are SV-only perturbations
  of the parent template's source), so the column tracks how
  many mutant cases fired each rule under the analyzer.

A ``disagree`` column counts per-rule Yosys-vs-slang mismatches —
high values point at the slang-parity gaps tracked in
rtl-buddy-cdc#224.

The Stage-2 plan in rtl-buddy-cdc#190 calls for "every shipping rule
fires ≥10 times across the corpus." With the three-surface report
each surface is its own ratchet:

- A rule that fires ≥10× in Yosys but 0× in slang is a slang
  parity gap (track in #224).
- A rule that fires ≥10× in parent cases but 0× in mutants is a
  mutator-side coverage gap (track in rtl-buddy-xeno#2).
- A rule whose ``mutants`` column shows fewer fires than the
  rtl-buddy-cdc#221 done-when target (≥10 per template) flags
  insufficient xeno operator coverage for that rule's structural
  shape.

Invocation::

    uv run python -m tests.fuzz.coverage

Output: a table sorted by rule id with the columns above.
Surfaces show ``—`` when their backing dependency isn't installed.
"""

from __future__ import annotations

import sys
from collections import Counter

from rtl_buddy_cdc.rules import RULES

from ._mutator import iter_mutants, xeno_available
from .runner import collect_cases, run_case, run_case_slang
from .slang_cache import slang_available
from .yosys_cache import yosys_available


def main() -> int:
    if not yosys_available():
        print("yosys not on PATH; corpus coverage requires yosys")
        return 2

    run_slang = slang_available()
    run_mutants = xeno_available()

    yosys_rule_fires: Counter[str] = Counter()
    yosys_rule_cases: Counter[str] = Counter()
    slang_rule_fires: Counter[str] = Counter()
    slang_rule_cases: Counter[str] = Counter()
    per_rule_disagree: Counter[str] = Counter()
    mutant_rule_fires: Counter[str] = Counter()
    mutant_rule_cases: Counter[str] = Counter()
    case_count = 0
    failed_cases: list[str] = []
    slang_skip_count = 0
    mutant_total = 0
    mutant_skip_count = 0

    # ``canonical`` here mirrors tests/fuzz/test_mutants.py's
    # canonical-parent strategy: one parent per template family,
    # so the mutants column doesn't get dominated by sweep-induced
    # redundancy.
    canonical: dict[str, object] = {}
    cases = collect_cases()

    for case in cases:
        case_count += 1
        canonical.setdefault(case.template_name, case)
        yosys_result = run_case(case)
        if yosys_result.failures:
            failed_cases.append(case.case_id)
        for rule_id, count in yosys_result.fired.items():
            yosys_rule_fires[rule_id] += count
            yosys_rule_cases[rule_id] += 1

        if run_slang:
            try:
                slang_result = run_case_slang(case)
            except Exception:
                slang_skip_count += 1
            else:
                for rule_id, count in slang_result.fired.items():
                    slang_rule_fires[rule_id] += count
                    slang_rule_cases[rule_id] += 1
                # Per-rule disagreement count: any rule whose fired
                # count differs between the frontends contributes
                # one to the disagreement column. Per-rule view of
                # the corpus-level agreement rate from
                # rtl-buddy-cdc#221 done-when criterion 3.
                all_rules = set(yosys_result.fired) | set(slang_result.fired)
                for rule_id in all_rules:
                    if yosys_result.fired.get(rule_id, 0) != slang_result.fired.get(
                        rule_id, 0
                    ):
                        per_rule_disagree[rule_id] += 1

    if run_mutants:
        for parent in canonical.values():
            for mc in iter_mutants(parent, count=64, seed=0):  # type: ignore[arg-type]
                mutant_total += 1
                try:
                    mutant_result = run_case(mc.case)
                except Exception:
                    # Mutant produced invalid SV (xeno v0.0.1 bug; see
                    # tests/fuzz/test_mutants.py docstring).
                    mutant_skip_count += 1
                    continue
                for rule_id, count in mutant_result.fired.items():
                    mutant_rule_fires[rule_id] += count
                    mutant_rule_cases[rule_id] += 1

    print(f"Corpus: {case_count} cases (yosys)")
    if run_slang:
        print(
            f"        {case_count - slang_skip_count} cases (slang); "
            f"{slang_skip_count} skipped"
        )
    else:
        print("        slang frontend disabled (pyslang not importable)")
    if run_mutants:
        print(
            f"        {mutant_total - mutant_skip_count} cases (mutants); "
            f"{mutant_skip_count} skipped, "
            f"{len(canonical)} canonical parents"
        )
    else:
        print("        mutants disabled (rtl-buddy-xeno not importable)")
    if failed_cases:
        print(f"FAILED: {len(failed_cases)} case(s)")
        for cid in failed_cases:
            print(f"  - {cid}")

    print()
    header = (
        f"{'rule':<10} "
        f"{'yosys_fires':>11} {'yosys_cases':>11}  "
        f"{'slang_fires':>11} {'slang_cases':>11}  "
        f"{'mut_fires':>10} {'mut_cases':>10}  "
        f"{'disagree':>8}"
    )
    sep = (
        f"{'-' * 10} "
        f"{'-' * 11} {'-' * 11}  "
        f"{'-' * 11} {'-' * 11}  "
        f"{'-' * 10} {'-' * 10}  "
        f"{'-' * 8}"
    )
    print(header)
    print(sep)
    seen_rules = sorted(
        set(yosys_rule_fires) | set(slang_rule_fires) | set(mutant_rule_fires)
    )
    for rule_id in seen_rules:
        yf = yosys_rule_fires[rule_id]
        yc = yosys_rule_cases[rule_id]
        if run_slang:
            sf_disp: str = str(slang_rule_fires[rule_id])
            sc_disp: str = str(slang_rule_cases[rule_id])
            disagree_disp: str = str(per_rule_disagree[rule_id])
        else:
            sf_disp = sc_disp = disagree_disp = "—"
        if run_mutants:
            mf_disp: str = str(mutant_rule_fires[rule_id])
            mc_disp: str = str(mutant_rule_cases[rule_id])
        else:
            mf_disp = mc_disp = "—"
        print(
            f"{rule_id:<10} "
            f"{yf:>11} {yc:>11}  "
            f"{sf_disp:>11} {sc_disp:>11}  "
            f"{mf_disp:>10} {mc_disp:>10}  "
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
