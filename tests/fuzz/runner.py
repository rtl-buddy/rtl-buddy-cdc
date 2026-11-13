"""Analyzer-differential runner.

For each rendered case, drive it through Yosys (or the slang
frontend), load the netlist with :mod:`rtl_buddy_cdc.netlist`, run
``run_all_rules``, and diff the resulting findings against the
template's :class:`ExpectedFinding` and ``forbidden`` claims.

The cross-frontend (Yosys vs slang) differential oracle layered on
top of the per-frontend runner lives in
:mod:`tests.fuzz.test_corpus_diff` (Stage-3 Layer C of
rtl-buddy-cdc#221).

Analyzer options
~~~~~~~~~~~~~~~~

Templates pass analyzer options through ``case.params`` (the dict the
mutators copy verbatim onto every derived case, so a mutant of a
``--sync-depth 3`` parent is analysed at depth 3 too):

- ``required_depth`` — ``--sync-depth`` (CDC-002 / CDC-022).
- ``cdc_018_depth_threshold`` — ``--cdc-018-depth-threshold``.
- ``sync_primitives`` — ``--sync-primitive`` module names.
- ``analysis`` — ``"rules"`` (default) runs ``run_all`` directly on
  the top module, the historical recipe. ``"cli"`` routes the case
  through the CLI's own analysis path
  (:func:`rtl_buddy_cdc.cli._analyze_module_and_report`) instead:
  blackbox siblings are composed into boundaries, crossings go
  through ``_filter_async``, the CLI-emitted ``CDC-BBX`` coverage
  findings exist, and in-RTL ``// rbcdc:`` pragmas are honoured.
  Needed for anything with a ``(* blackbox *)`` stub (XPM macros,
  CDC-022, CDC-BBX) and for pragma-suppression variants
  (rtl-buddy-cdc#331). Only *kept* findings count as fired.
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from rtl_buddy_cdc import netlist, pragma as pragma_mod, sdc as sdc_mod
from rtl_buddy_cdc.domain import find_crossings
from rtl_buddy_cdc.netlist import Module
from rtl_buddy_cdc.rules import CDC_018_DEFAULT_THRESHOLD, run_all as run_all_rules

from .slang_cache import build as build_slang, slang_available
from .templates import ALL_TEMPLATES
from .templates.base import RenderedCase
from .yosys_cache import build, yosys_available


def collect_cases() -> list[RenderedCase]:
    """Enumerate every concrete case across every registered template.

    The order is deterministic: insertion-order of ``ALL_TEMPLATES``
    × insertion-order yielded by each template's ``cases``.
    """
    out: list[RenderedCase] = []
    for template in ALL_TEMPLATES:
        for case in template.cases():
            out.append(case)
    return out


@dataclass(frozen=True)
class CaseResult:
    case: RenderedCase
    fired: dict[str, int]
    cache_hit: bool
    failures: tuple[str, ...]


def _fired_via_cli(
    case: RenderedCase,
    module: Module,
    blackboxes: dict[str, Module],
    sdc_path: Path,
    sv_path: Path,
) -> Counter[str]:
    """Kept-finding counts from the CLI's analysis path (``analysis="cli"``).

    Calls the same function ``analyze`` / ``lint`` end in, with the
    case's options, and reads the JSON report back. ``sv_path`` is the
    cached source both frontends elaborated, so the ``src`` attributes
    the pragma scope matches against name the file scanned here.
    """
    from rtl_buddy_cdc import cli  # local: cli pulls in typer + reporters

    with tempfile.TemporaryDirectory(prefix="rbcdc-fuzz-") as tmp:
        report = Path(tmp) / "report.json"
        cli._analyze_module_and_report(
            module,
            sdc_path,
            None,
            cli.OutputFormat.json,
            report,
            int(case.params.get("required_depth", 2)),
            cdc_018_depth_threshold=int(
                case.params.get("cdc_018_depth_threshold", CDC_018_DEFAULT_THRESHOLD)
            ),
            blackboxes=blackboxes,
            sync_primitives=frozenset(case.params.get("sync_primitives", ())),
            extra_waivers=pragma_mod.scan([sv_path]),
        )
        data = json.loads(report.read_text())
    return Counter(v["rule_id"] for v in data["violations"])


def _analyze(
    case: RenderedCase,
    module: Module,
    blackboxes: dict[str, Module] | None = None,
) -> tuple[dict[str, int], tuple[str, ...]]:
    """Run the rule pack on a pre-elaborated ``module``; return the
    per-rule fired counts plus any expected/forbidden mismatches.

    Shared between the Yosys and slang runners so both frontends see
    the same SDC parse and the same ``required_depth`` resolution —
    the only thing that differs between them is the ``module`` they
    feed in. Findings disagreements between the two frontends
    therefore have exactly one explanation: the frontend itself.
    """
    # Write the SDC under the same digest as the SV / json / pkl
    # cache files so both frontend paths read the same Tcl bytes.
    from .yosys_cache import CACHE_ROOT  # local to avoid a top-of-file cycle

    bucket = CACHE_ROOT / case.content_hash[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    sdc_path = bucket / f"{case.content_hash}.sdc"
    if not sdc_path.exists():
        sdc_path.write_text(case.sdc)
    fired: Counter[str]
    if case.params.get("analysis", "rules") == "cli":
        fired = _fired_via_cli(
            case,
            module,
            blackboxes or {},
            sdc_path,
            bucket / f"{case.content_hash}.sv",
        )
    else:
        spec = sdc_mod.parse_file(sdc_path)
        # Mirrors what cli.py does between SDC parse and find_crossings:
        # mark unconstrained top-level inputs with UNCONSTRAINED_SENTINEL
        # so CDC-011 can see them.
        sdc_mod.synthesize_unconstrained_inputs(spec, module)
        crossings = find_crossings(module, port_clock=spec.port_clock)

        violations = run_all_rules(
            module,
            crossings,
            spec,
            required_depth=int(case.params.get("required_depth", 2)),
            cdc_018_depth_threshold=int(
                case.params.get("cdc_018_depth_threshold", CDC_018_DEFAULT_THRESHOLD)
            ),
            sync_primitives=frozenset(case.params.get("sync_primitives", ())),
        )
        fired = Counter(v.rule_id for v in violations)
    failures: list[str] = []

    for expected in case.expected:
        actual = fired.get(expected.rule_id, 0)
        if not expected.check(actual):
            failures.append(
                f"{expected.rule_id}: expected {expected.op.value} "
                f"{expected.count}, got {actual} "
                f"(fired={dict(fired)})"
            )
    for forbid in case.forbidden:
        actual = fired.get(forbid.rule_id, 0)
        if not forbid.check(actual):
            failures.append(
                f"{forbid.rule_id}: expected to stay silent, "
                f"got {actual} (fired={dict(fired)})"
            )

    return dict(fired), tuple(failures)


def run_case(case: RenderedCase) -> CaseResult:
    """Build, analyze, and diff a single case via the **Yosys** frontend.

    Never raises on analyzer findings — failures are recorded in the
    result.
    """
    if not yosys_available():
        raise RuntimeError("yosys not on PATH")
    cached = build(case)
    module, blackboxes = netlist.load_with_blackboxes(cached.json_path)
    fired, failures = _analyze(case, module, blackboxes)
    return CaseResult(
        case=case,
        fired=fired,
        cache_hit=cached.cache_hit,
        failures=failures,
    )


def run_case_slang(case: RenderedCase) -> CaseResult:
    """Build, analyze, and diff a single case via the **slang** frontend.

    Counterpart to :func:`run_case`. Same expected/forbidden contract;
    the only thing that differs is which frontend produced the
    :class:`Module`. Used by the cross-frontend differential oracle in
    :mod:`tests.fuzz.test_corpus_diff`.
    """
    if not slang_available():
        raise RuntimeError("pyslang not importable")
    cached = build_slang(case)
    fired, failures = _analyze(case, cached.module, cached.blackboxes)
    return CaseResult(
        case=case,
        fired=fired,
        cache_hit=cached.cache_hit,
        failures=failures,
    )


def iter_results() -> Iterator[CaseResult]:
    """Stream-run the full corpus. Used by the coverage reporter."""
    for case in collect_cases():
        yield run_case(case)
