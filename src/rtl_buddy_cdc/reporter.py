"""Output formatters for the analyzer.

The text formatter is what users see by default; ``json`` is the
machine-friendly equivalent for programmatic consumers (rtl-buddy
itself, downstream scripts); ``sarif`` emits SARIF 2.1.0 so the
results can be uploaded as GitHub code-scanning annotations.

Each formatter takes an :class:`AnalysisResult` and a writeable
text-mode file-like; nothing here imports Typer so the formatters
remain unit-testable in isolation.
"""

from __future__ import annotations

import json
import os
import re
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from typing import IO

from rtl_buddy_cdc.domain import Crossing, FlopDomain, InferredClockCandidate
from rtl_buddy_cdc.netlist import Module
from rtl_buddy_cdc.rules import Violation
from rtl_buddy_cdc.sdc import ClockSpec
from rtl_buddy_cdc.waivers import SuppressedViolation, Waiver

TOOL_NAME = "rtl-buddy-cdc"
TOOL_VERSION = "0.4.0"
TOOL_INFO_URI = "https://github.com/rtl-buddy/rtl-buddy-cdc"

# JSON output schema contract — these dotted keys are PUBLIC API.
# Downstream ``rtl_buddy`` (sibling repo) parses them out of the JSON
# report to populate its ``CdcResults`` summary. Renaming any key,
# changing its type, or removing it from the payload is a
# downstream-breaking change. Anything *else* in the JSON can evolve
# freely. See AGENTS.md § "Cross-repo coupling" and
# ``tests/test_reporter.py::test_json_contract_keys_are_stable``.
JSON_CONTRACT: dict[str, type] = {
    "summary.violations": int,
    "summary.suppressed": int,
    "summary.crossings": int,
    # Count of flops whose clock root could not be traced
    # (``FlopDomain.clock is None``). Such flops are excluded from CDC
    # analysis — a crossing into/out of them cannot be classified — so
    # a non-zero count means the run under-resolved the design. Pinned
    # here so downstream ``rtl_buddy`` can surface coverage degradation
    # rather than silently treating an under-resolved run as complete.
    # Issue #263.
    "summary.domain_unknown": int,
}

# Cap on how many unresolved-flop cell names land in
# ``domain_unknown_flops``. The list is a debugging aid (which subtrees
# under-resolved), not an exhaustive inventory; ``summary.domain_unknown``
# carries the full count. Kept bounded so a pathologically
# under-resolved netlist can't bloat the JSON report.
_DOMAIN_UNKNOWN_SAMPLE_CAP = 20

# Short-form descriptions per rule. Keep this terse — the long-form
# message is already attached to each result.
_RULE_DESCRIPTIONS: dict[str, str] = {
    "CDC-001": "Unsynchronized control crossing (no second-stage flop)",
    "CDC-002": "Insufficient synchronizer depth",
    "CDC-003": "Combinational logic before synchronizer first stage",
    "CDC-004": "Bus crossing without gating or gray-coding",
    "CDC-005": "Reconvergent synchronizers",
    "CDC-006": "Glitchy combinational source on a control crossing",
    "CDC-008": "Clock signal used as data",
    "RDC-001": "Async reset crossing without a reset synchronizer (was CDC-007)",
    "RDC-002": "Reset polarity mismatch on a direct flop→flop reset",
    "RDC-003": "Sync reset crossing without a reset synchroniser",
    "RDC-004": "Reset driven by combinational logic with no synchroniser",
    "RDC-005": "Multiple reset sources converging on a flop without muxing",
    "CDC-014": "Combinational logic between synchroniser stages",
    "CDC-015": "Sync chain asynchronously reset from a foreign clock domain",
    "CDC-016": "Opposite-edge synchroniser (halves MTBF)",
    "CDC-017": "Transparent latch in CDC path",
    "CDC-022": "CDC primitive with insufficient DEST_SYNC_FF depth",
    "CDC-023": "Clock net driven by a combine of two declared clocks",
}


@dataclass(frozen=True)
class AnalysisResult:
    """Everything a reporter needs to render. Stays pure data — no
    typer / no I/O — so the same struct flows through any formatter."""

    module: Module
    domains: list[FlopDomain]
    crossings: list[Crossing]
    async_crossings: list[Crossing]
    spec: ClockSpec | None
    violations: list[Violation]
    suppressed: list[SuppressedViolation] = field(default_factory=list)
    # Advisory hints (P3/#263): internal nets that fan out to many flop
    # CLK pins but are not declared as a clock — possible undeclared
    # ``create_generated_clock`` targets. Report-only; they never change a
    # domain, crossing, or violation, so the default-empty field keeps
    # every existing AnalysisResult construction unchanged.
    inferred_clock_candidates: list[InferredClockCandidate] = field(
        default_factory=list
    )
    # Findings filtered out by ``--baseline``: present in a baseline
    # JSON report so they're not re-flagged on this run. Visible in the
    # report (separate tally) but never drive the exit code — the
    # "carried over" tally is auto-derived waivers, not new findings.
    baseline_carryover: list[Violation] = field(default_factory=list)
    # Crossings the rule pack skipped because ``--ignore-scan-mode`` was
    # passed and their destination flop is clocked through a DFT scan
    # structure (#45). A tally, not a list: the crossings themselves are
    # still in ``crossings`` / ``async_crossings`` (tagged
    # ``scan_mode``), and the findings they would have produced were
    # never generated, so there is nothing richer to show. Zero when the
    # flag is off — which is what keeps the suppression visible rather
    # than silent.
    scan_mode_suppressed: int = 0


# --- text -------------------------------------------------------------------

# Visual width for the header banner. Picked to fit comfortably in 80-
# column terminals; longer module names overflow gracefully.
_HEADER_WIDTH = 80
# Width to wrap violation message bodies at. The 4-space body indent
# eats into 80, so we wrap at 76 - 6 = 70.
_BODY_WIDTH = 70


@dataclass(frozen=True)
class _Style:
    """ANSI escape sequences (or empty strings when color is disabled).

    Only used by ``render_text``; the JSON / SARIF outputs are color-
    free by definition. The toggle is a runtime check on the output
    stream's tty-ness, with a respect for the ``NO_COLOR`` convention
    (https://no-color.org/) and an explicit ``color`` argument that
    callers can use to override.
    """

    bold: str = ""
    dim: str = ""
    red: str = ""
    yellow: str = ""
    green: str = ""
    cyan: str = ""
    reset: str = ""

    @classmethod
    def for_stream(cls, stream: IO[str], force: bool | None = None) -> "_Style":
        if force is False:
            return cls()
        if force is None:
            if os.environ.get("NO_COLOR") is not None:
                return cls()
            if not getattr(stream, "isatty", lambda: False)():
                return cls()
        return cls(
            bold="\033[1m",
            dim="\033[2m",
            red="\033[31m",
            yellow="\033[33m",
            green="\033[32m",
            cyan="\033[36m",
            reset="\033[0m",
        )


def render_text(
    result: AnalysisResult,
    out: IO[str],
    *,
    verbose: bool = False,
    color: bool | None = None,
) -> None:
    """Render a human-readable report.

    ``verbose`` enables the per-crossing structural listing (off by
    default — clean runs print only the design-level summary).

    ``color`` is tri-state:
        - ``None`` (default): ANSI on a TTY, off otherwise / under
          ``NO_COLOR``.
        - ``True`` / ``False``: explicit override.
    """
    s = _Style.for_stream(out, force=color)
    _render_header(result, out, s)
    _render_design_summary(result, out, s, verbose=verbose)
    if result.spec is None:
        out.write(
            f"\n{s.yellow}No SDC supplied — every cross-clock crossing is "
            f"treated as synchronous and rule checks are skipped.{s.reset}\n"
        )
        return
    _render_violations(result, out, s)
    _render_suppressed(result, out, s)
    _render_baseline_carryover(result, out, s)


def _render_header(result: AnalysisResult, out: IO[str], s: _Style) -> None:
    has_violations = bool(result.violations)
    if result.spec is None:
        verdict = "SKIP"
        verdict_color = s.yellow
    elif has_violations:
        verdict = "FAIL"
        verdict_color = s.red
    else:
        verdict = "PASS"
        verdict_color = s.green
    left = f"{TOOL_NAME} {TOOL_VERSION} — {result.module.name}"
    pad = max(1, _HEADER_WIDTH - len(left) - len(verdict))
    out.write(
        f"{s.bold}{left}{s.reset}{' ' * pad}{verdict_color}{s.bold}{verdict}{s.reset}\n"
    )


def _render_design_summary(
    result: AnalysisResult, out: IO[str], s: _Style, *, verbose: bool
) -> None:
    out.write(f"\n{s.bold}Design{s.reset}\n")

    # ports / cells / flops on one compact line; per-domain breakdown
    # appended in parens.
    by_clock: dict[str | None, int] = {}
    for fd in result.domains:
        by_clock[fd.clock] = by_clock.get(fd.clock, 0) + 1
    domain_parts = [
        f"{n} {clk if clk is not None else '<unresolved>'}"
        for clk, n in sorted(by_clock.items(), key=lambda x: (x[0] is None, x[0] or ""))
    ]
    flop_str = f"{len(result.domains)} flops"
    if domain_parts:
        flop_str += f" ({', '.join(domain_parts)})"
    out.write(
        f"  {len(result.module.ports)} ports · "
        f"{len(result.module.cells)} cells · {flop_str}\n"
    )

    # Crossings line.
    cross_summary = f"{len(result.crossings)} cross-domain crossings detected"
    if result.spec is not None:
        cross_summary += f" ({len(result.async_crossings)} async per SDC)"
    if result.spec is not None:
        cross_summary += (
            f" · SDC: {len(result.spec.clocks)} clocks, "
            f"{len(result.spec.async_groups)} async group"
            f"{'s' if len(result.spec.async_groups) != 1 else ''}"
        )
    out.write(f"  {cross_summary}\n")

    # Coverage warning (issue #263): flops whose clock root we could not
    # trace are silently excluded from crossing detection. Surfacing the
    # count keeps an under-resolved run from reading like a clean,
    # complete analysis. Report-only — classification is unchanged.
    n_unknown = sum(1 for fd in result.domains if fd.clock is None)
    if n_unknown:
        out.write(
            f"  {s.yellow}{s.bold}⚠ {n_unknown} of {len(result.domains)} "
            f"flops have unresolved clock domain — excluded from CDC "
            f"analysis{s.reset}\n"
        )

    # Inferred-clock advisory (issue #263, P3): internal nets that fan
    # out to many flop CLK pins but carry no declared clock identity —
    # likely a forwarded/divided clock the user forgot to declare with
    # `create_generated_clock`. Advisory ONLY: these never change a
    # domain or a crossing; declaring them is what would let the
    # downstream flops resolve. The flops they clock are still counted in
    # the `domain_unknown` total above unless a real trace already
    # resolved them.
    if result.inferred_clock_candidates:
        n = len(result.inferred_clock_candidates)
        out.write(
            f"  {s.cyan}ⓘ {n} undeclared internal clock candidate"
            f"{'s' if n != 1 else ''} (net drives ≥4 flop CLK pins, not in "
            f"SDC) — add create_generated_clock to resolve:{s.reset}\n"
        )
        for cand in result.inferred_clock_candidates:
            sinks = ", ".join(cand.example_sinks)
            more = ", …" if cand.fanout > len(cand.example_sinks) else ""
            out.write(
                f"    {s.dim}{cand.driver} ({cand.driver_kind}) → "
                f"{cand.fanout} CLK pins [{sinks}{more}]{s.reset}\n"
            )

    # Scan-mode suppression tally (issue #45). Only non-zero when
    # ``--ignore-scan-mode`` was passed, and always printed when it is —
    # a suppression the reader cannot see is indistinguishable from a
    # clean design.
    if result.scan_mode_suppressed:
        n = result.scan_mode_suppressed
        out.write(
            f"  {s.cyan}ⓘ {n} async crossing{'s' if n != 1 else ''} "
            f"suppressed by --ignore-scan-mode (destination flop clocked "
            f"through a scan-mode structure){s.reset}\n"
        )

    if verbose and result.crossings:
        out.write(f"\n{s.dim}  Crossings:{s.reset}\n")
        for c in result.crossings:
            tag = ""
            if c.is_port_sourced:
                tag = f" {s.dim}(port-sourced){s.reset}"
            if c.scan_mode:
                tag += f" {s.dim}(scan-mode){s.reset}"
            out.write(
                f"    {c.src_clock} → {c.dst_clock}  "
                f"{c.src_name} → {c.dst_flop.name}  "
                f"{s.dim}width={c.width} hops={c.min_hops}{s.reset}{tag}\n"
            )


def _render_violations(result: AnalysisResult, out: IO[str], s: _Style) -> None:
    if not result.violations:
        out.write(f"\n{s.green}No rule violations.{s.reset}\n")
        return

    counts = _severity_counts(result.violations)
    summary_parts: list[str] = []
    if counts["error"]:
        summary_parts.append(f"{s.red}{counts['error']} error{s.reset}")
    if counts["warning"]:
        summary_parts.append(f"{s.yellow}{counts['warning']} warning{s.reset}")
    if counts["info"]:
        summary_parts.append(f"{s.cyan}{counts['info']} info{s.reset}")
    out.write(f"\n{s.bold}Violations{s.reset}  ({', '.join(summary_parts)})\n")

    by_rule: dict[str, list[Violation]] = defaultdict(list)
    for v in result.violations:
        by_rule[v.rule_id].append(v)
    for rule_id in sorted(by_rule):
        vs = by_rule[rule_id]
        desc = _RULE_DESCRIPTIONS.get(rule_id, "")
        out.write(f"\n  {s.bold}{rule_id}{s.reset} — {desc}\n")
        _render_rule_group(vs, result.module, out, s)


def _render_rule_group(
    violations: list[Violation], module: Module, out: IO[str], s: _Style
) -> None:
    """Render the violations within a single rule group.

    Per-instance bucketing is engaged iff *any* violation in the group
    carries a non-empty ``instance_path``. When every violation is at
    the top instance — the common case on flat IP-block fixtures — the
    headers collapse and the output is byte-identical to the pre-#46
    layout. Phase 4 of #46.
    """
    if not any(v.instance_path for v in violations):
        for v in violations:
            _render_one_violation(v, module, out, s, indent=4)
        return
    # Bucket by instance_path. Tuple comparison sorts ``()`` before any
    # populated path, then lexicographically — which is exactly the
    # display order we want: top first, then nested in path order.
    by_inst: dict[tuple[str, ...], list[Violation]] = defaultdict(list)
    for v in violations:
        by_inst[v.instance_path].append(v)
    for path in sorted(by_inst):
        header = "[top]" if not path else " / ".join(path)
        out.write(f"    {s.dim}{header}{s.reset}\n")
        for v in by_inst[path]:
            _render_one_violation(v, module, out, s, indent=6)


def _render_one_violation(
    v: Violation,
    module: Module,
    out: IO[str],
    s: _Style,
    *,
    indent: int = 4,
) -> None:
    severity_color = {
        "error": s.red,
        "warning": s.yellow,
        "info": s.cyan,
    }.get(v.severity, "")
    loc = _source_location(module, v.cell_name)
    loc_str = ""
    if loc is not None:
        line_part = f":{loc['start_line']}" if "start_line" in loc else ""
        loc_str = f"  {s.dim}{loc['file']}{line_part}{s.reset}"
    pad = " " * indent
    msg_pad = " " * (indent + 2)
    out.write(f"{pad}{severity_color}{v.severity}{s.reset}{loc_str}\n")
    for line in _wrap_message(v.message):
        out.write(f"{msg_pad}{line}\n")


def _wrap_message(text: str) -> list[str]:
    """Wrap a violation message at ``_BODY_WIDTH``, preserving the
    clear visual indent. Long single-line messages from the rule pack
    have semicolon- or colon-separated segments; we don't try to be
    clever about that — a generic textwrap.fill is good enough."""
    return textwrap.wrap(
        text,
        width=_BODY_WIDTH,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [""]


def _waiver_provenance(w: Waiver) -> str:
    """Where the waiver was written, for the human report.

    A waiver-file entry only has a line number (the file is the one the
    user passed to ``--waivers``). An in-RTL pragma has an ``origin``,
    so it renders as a source location — ``src.sv:42`` — which is both
    clickable and unambiguous against the waiver-file form. A pragma
    closed by an ``enable-rule`` renders its whole block as
    ``src.sv:42-58``, so a block-scoped suppression is visibly
    narrower than one that runs to the end of the file."""
    if w.origin is None:
        return f"waiver line {w.source_line}"
    if w.end_line is not None:
        return f"pragma {w.origin}:{w.source_line}-{w.end_line}"
    return f"pragma {w.origin}:{w.source_line}"


def _render_suppressed(result: AnalysisResult, out: IO[str], s: _Style) -> None:
    if not result.suppressed:
        return
    out.write(f"\n{s.bold}Suppressed by waivers{s.reset} ({len(result.suppressed)})\n")
    for sup in result.suppressed:
        rule_id = sup.violation.rule_id
        reason = sup.waiver.reason or "(no reason given)"
        out.write(
            f"  {s.dim}{rule_id}{s.reset}  {reason}  "
            f"{s.dim}({_waiver_provenance(sup.waiver)}){s.reset}\n"
        )


def _render_baseline_carryover(result: AnalysisResult, out: IO[str], s: _Style) -> None:
    if not result.baseline_carryover:
        return
    out.write(
        f"\n{s.bold}Carried over from baseline{s.reset} "
        f"({len(result.baseline_carryover)})\n"
    )
    for v in result.baseline_carryover:
        msg = v.message.splitlines()[0] if v.message else ""
        out.write(f"  {s.dim}{v.rule_id}{s.reset}  {msg}\n")


def _severity_counts(violations: list[Violation]) -> dict[str, int]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for v in violations:
        counts[v.severity] = counts.get(v.severity, 0) + 1
    return counts


# --- json -------------------------------------------------------------------


def _domain_unknown_flops(result: AnalysisResult) -> list[str]:
    """Cell names of flops whose clock root could not be traced.

    Order follows ``result.domains`` (deterministic across runs). The
    full count is ``len(...)``; callers cap the *sample* they emit but
    never the count.
    """
    return [fd.flop.cell.name for fd in result.domains if fd.clock is None]


def render_json(result: AnalysisResult, out: IO[str]) -> None:
    unknown_flops = _domain_unknown_flops(result)
    payload = {
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "module": result.module.name,
        "summary": {
            "ports": len(result.module.ports),
            "cells": len(result.module.cells),
            "flops": len(result.domains),
            "crossings": len(result.crossings),
            "async_crossings": len(result.async_crossings),
            "violations": len(result.violations),
            "suppressed": len(result.suppressed),
            "baseline_carryover": len(result.baseline_carryover),
            # Additive (#45): how many async crossings the rule pack
            # skipped under ``--ignore-scan-mode``. 0 without the flag.
            "scan_mode_suppressed": result.scan_mode_suppressed,
            # Coverage diagnostic (issue #263): how many flops the clock-
            # root tracer left unresolved. These are EXCLUDED from CDC
            # analysis, so a non-zero value means the report does not
            # cover the whole design. Report-only — never changes a
            # classification.
            "domain_unknown": len(unknown_flops),
        },
        # Bounded sample of unresolved-flop cell names to aid debugging
        # (which subtrees under-resolved). Full count lives in
        # ``summary.domain_unknown``; this list is capped.
        "domain_unknown_flops": unknown_flops[:_DOMAIN_UNKNOWN_SAMPLE_CAP],
        # Advisory (issue #263, P3): undeclared internal nets used as a
        # clock by many flops — likely a forgotten
        # ``create_generated_clock``. Report-only; never reflects a domain
        # or crossing change. Each entry carries the driver cell, whether
        # it is a flop-Q or a clock-gate output, the CLK-pin fanout, and a
        # bounded sample of sink flops. Empty list when none.
        "inferred_clock_candidates": [
            {
                "driver": c.driver,
                "driver_kind": c.driver_kind,
                "fanout": c.fanout,
                "example_sinks": list(c.example_sinks),
            }
            for c in result.inferred_clock_candidates
        ],
        "by_instance": _by_instance(result.violations),
        "domains": [
            {"flop": fd.flop.cell.name, "clock": fd.clock} for fd in result.domains
        ],
        "crossings": [_crossing_to_dict(c) for c in result.crossings],
        "violations": [_violation_to_dict(v, result.module) for v in result.violations],
        "suppressed": [
            {
                **_violation_to_dict(s.violation, result.module),
                "waiver": {
                    "rule_pattern": s.waiver.rule_pattern,
                    "regex": s.waiver.regex.pattern,
                    "reason": s.waiver.reason,
                    # ``source_line`` is a line in ``origin`` when that
                    # is set (an in-RTL pragma), else in the --waivers
                    # file. ``origin`` / ``end_line`` are added keys, so
                    # a consumer that ignores them keeps reading the old
                    # shape. ``end_line`` closes the pragma's half-open
                    # ``[source_line, end_line)`` block; null means the
                    # pragma runs to the end of its file (and is always
                    # null for a waiver-file entry, which has no line
                    # scope).
                    "source_line": s.waiver.source_line,
                    "origin": s.waiver.origin,
                    "end_line": s.waiver.end_line,
                },
            }
            for s in result.suppressed
        ],
        "baseline_carryover": [
            _violation_to_dict(v, result.module) for v in result.baseline_carryover
        ],
    }
    json.dump(payload, out, indent=2)
    out.write("\n")


def _by_instance(violations: list[Violation]) -> list[dict]:
    """Aggregate kept violations by ``instance_path``.

    Returns one entry per distinct path, sorted by ``instance_path``
    (empty tuple first, then lexicographic). Each entry has a
    ``violations`` count and a per-rule breakdown.

    Suppressed and baseline-carryover findings are intentionally
    excluded — they don't drive the exit code, and rolling them into
    the ``by_instance`` tally would mislead anyone reading the report
    as a "what's actually broken in each block" view. Consumers that
    want the suppressed view can iterate ``suppressed`` themselves
    (each entry already carries ``instance_path``).
    """
    buckets: dict[tuple[str, ...], dict[str, int]] = {}
    for v in violations:
        rule_counts = buckets.setdefault(v.instance_path, {})
        rule_counts[v.rule_id] = rule_counts.get(v.rule_id, 0) + 1
    out: list[dict] = []
    for path in sorted(buckets):
        rule_counts = buckets[path]
        out.append(
            {
                "instance_path": list(path),
                "violations": sum(rule_counts.values()),
                "rules": dict(sorted(rule_counts.items())),
            }
        )
    return out


def _crossing_to_dict(c: Crossing) -> dict:
    out: dict = {
        "src_clock": c.src_clock,
        "dst_clock": c.dst_clock,
        "dst_flop": c.dst_flop.cell.name,
        "width": c.width,
        "min_hops": c.min_hops,
    }
    # Additive (#45). Emitted unconditionally, flag or no flag, so the
    # tag is auditable even on a run that suppresses nothing.
    if c.scan_mode:
        out["scan_mode"] = True
    if c.src_flop is not None:
        out["src_flop"] = c.src_flop.cell.name
    if c.src_port is not None:
        out["src_port"] = c.src_port
    if c.src_boundary is not None:
        out["src_boundary"] = {
            "instance": c.src_boundary[0],
            "port": c.src_boundary[1],
        }
    if c.dst_boundary is not None:
        out["dst_boundary"] = {
            "instance": c.dst_boundary[0],
            "port": c.dst_boundary[1],
        }
    return out


def _violation_to_dict(v: Violation, module: Module) -> dict:
    out: dict[str, object] = {
        "rule_id": v.rule_id,
        "severity": v.severity,
        "message": v.message,
    }
    # ``cell_name`` is part of the stable JSON contract so ``--baseline``
    # has a unique key per violation (``rule_id`` alone collides for
    # designs with many same-rule findings). Emitted as ``null`` when
    # the rule didn't anchor on a single cell.
    out["cell_name"] = v.cell_name
    # Hierarchical instance path the offending cell lives in.
    # Always present (never null, never missing) — ``[]`` is the
    # top-instance result. Downstream consumers can treat the field
    # unconditionally. See phase 2 of #46.
    out["instance_path"] = list(v.instance_path)
    if v.crossing is not None:
        out["crossing"] = _crossing_to_dict(v.crossing)
    loc = _source_location(module, v.cell_name)
    if loc is not None:
        out["location"] = loc
    return out


# --- sarif ------------------------------------------------------------------

# Map our internal severity to SARIF's level enum.
_SARIF_LEVEL = {"error": "error", "warning": "warning", "info": "note"}


def render_sarif(result: AnalysisResult, out: IO[str]) -> None:
    rule_ids = sorted({v.rule_id for v in result.violations})
    rules = [
        {
            "id": rid,
            "name": rid.replace("-", "_"),
            "shortDescription": {
                "text": _RULE_DESCRIPTIONS.get(rid, rid),
            },
            "defaultConfiguration": {
                "level": _SARIF_LEVEL.get(
                    next(
                        (v.severity for v in result.violations if v.rule_id == rid),
                        "warning",
                    ),
                    "warning",
                ),
            },
        }
        for rid in rule_ids
    ]

    sarif_results = [_violation_to_sarif(v, result.module) for v in result.violations]
    # Suppressed findings are still reported, with a SARIF
    # ``suppressions`` field so consumers (e.g. GitHub Code Scanning)
    # know they were intentionally hushed.
    for s in result.suppressed:
        entry = _violation_to_sarif(s.violation, result.module)
        entry["suppressions"] = [
            {
                "kind": "external",
                "status": "accepted",
                "justification": s.waiver.reason or "waived",
            }
        ]
        sarif_results.append(entry)
    # Baseline-carried findings: same shape, distinct justification so
    # consumers can tell them apart from user-authored waivers.
    for v in result.baseline_carryover:
        entry = _violation_to_sarif(v, result.module)
        entry["suppressions"] = [
            {
                "kind": "external",
                "status": "accepted",
                "justification": "carried over from baseline",
            }
        ]
        sarif_results.append(entry)

    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "version": TOOL_VERSION,
                        "informationUri": TOOL_INFO_URI,
                        "rules": rules,
                    }
                },
                "results": sarif_results,
            }
        ],
    }
    json.dump(sarif, out, indent=2)
    out.write("\n")


def _violation_to_sarif(v: Violation, module: Module) -> dict:
    entry: dict = {
        "ruleId": v.rule_id,
        "level": _SARIF_LEVEL.get(v.severity, "warning"),
        "message": {"text": v.message},
    }
    location: dict = {}
    loc = _source_location(module, v.cell_name)
    if loc is not None:
        physical = {"artifactLocation": {"uri": loc["file"]}}
        if "start_line" in loc:
            region: dict = {"startLine": loc["start_line"]}
            if "start_column" in loc:
                region["startColumn"] = loc["start_column"]
            if "end_line" in loc:
                region["endLine"] = loc["end_line"]
            if "end_column" in loc:
                region["endColumn"] = loc["end_column"]
            physical["region"] = region
        location["physicalLocation"] = physical
    # ``logicalLocations`` is SARIF's slot for hierarchical / semantic
    # location info — what GitHub Code Scanning and the SARIF viewer use
    # to group results by component. Emitted only when the violation
    # actually lives inside a child instance; omitted (rather than an
    # empty list) at top-instance so the output diff stays minimal on
    # flat IP-block fixtures. See phase 3 of #46.
    if v.instance_path:
        location["logicalLocations"] = [
            {
                "name": v.instance_path[-1],
                "fullyQualifiedName": ".".join(v.instance_path),
                "kind": "module",
            }
        ]
    if location:
        entry["locations"] = [location]
    return entry


# --- shared helpers ---------------------------------------------------------


# Yosys writes ``src`` attributes as ``file:line.col-line.col`` (or any
# truncated prefix). Capture the longest prefix we can read.
_SRC_RE = re.compile(
    r"^(?P<file>.+?):"
    r"(?P<sl>\d+)"
    r"(?:\.(?P<sc>\d+))?"
    r"(?:-(?P<el>\d+)(?:\.(?P<ec>\d+))?)?$"
)


# Yosys ``flatten`` writes nested cells under a literal ``$flatten\``
# prefix followed by the instance name and a ``.`` separator. The
# constant captures one literal backslash — Python source needs the
# double-backslash escape, the on-disk string is one byte.
_FLATTEN_PREFIX = "$flatten\\"


def _instance_path(module: Module, cell_name: str | None) -> tuple[str, ...]:
    """Map a cell name to the hierarchical instance path it lives in.

    Returns an empty tuple for cells at the top instance (the common
    case on flat IP-block fixtures), the inferred path otherwise. The
    ``module`` argument is currently unused but mirrors the
    :func:`_source_location` signature and leaves room for a future
    hierarchy-aware lookup (e.g. resolving an attribute-tagged netname
    back through an alias chain).

    Cell-name shapes the analyzer sees today:

    - ``$flatten\\<inst>.<leaf>`` (single level) and
      ``$flatten\\<inst>.\\<inner>.<leaf>`` (any depth) — Yosys post-
      flatten. The flatten pass emits *exactly one* ``$flatten\\``
      prefix per cell regardless of nesting; deeper hierarchy is
      encoded as additional dot-separated, ``\\``-escaped instance
      identifiers in the same name. After the prefix, walk
      dot-separated tokens until one starts with ``$`` — that's the
      leaf cell. Everything before it is the instance path (with the
      Yosys identifier-escape ``\\`` stripped from each token).
    - ``$procdff$42`` / ``$add$<file>:<line>$N`` — top-level Yosys
      auto-name. No instance prefix; the ``$<kind>$<file>:<line>``
      shape embeds a source path so naïve ``split('.')`` would
      tokenize on the dot inside ``.sv``. Returns ``()``.
    - ``u_b0.q`` — slang frontend. Plain dotted path; the last
      component is the leaf symbol, everything before it is the
      instance path. The top instance is *not* part of the name (the
      slang frontend walks the top with ``hier_prefix=''``), so the
      resolver does not need to strip a top component.
    - Anything else (e.g. slang top-level ``$mux$23``) → ``()``.
    """
    if cell_name is None:
        return ()
    if cell_name.startswith(_FLATTEN_PREFIX):
        body = cell_name[len(_FLATTEN_PREFIX) :]
        # Leaf detection: the first ``$``-prefixed dot-separated token
        # is the leaf cell (``$procdff$N``, ``$add$<file>:<line>$N``,
        # ``$auto$proc_dff.cc:242:proc_dff$N``, etc.). Stop accumulating
        # instance components when we hit it; the leaf may itself
        # contain ``.`` inside an embedded source path which we
        # deliberately don't try to tokenize further.
        parts: list[str] = []
        for tok in body.split("."):
            if tok.startswith("$"):
                break
            parts.append(tok.lstrip("\\"))
        return tuple(parts)
    # No ``$flatten\`` prefix. Distinguish the slang dotted shape from
    # top-level Yosys auto-names. Yosys auto-names always begin with
    # ``$``; the ``$<...>:<line>`` shape may embed dots inside the
    # source path, so a name with ``$`` before its first ``.`` is
    # never a slang hierarchical path.
    first_dot = cell_name.find(".")
    if first_dot < 0:
        return ()
    if "$" in cell_name[:first_dot]:
        return ()
    components = cell_name.split(".")
    if len(components) < 2:
        return ()
    return tuple(components[:-1])


def _source_location(module: Module, cell_name: str | None) -> dict | None:
    """Translate a cell's ``attributes["src"]`` into a structured location.

    The structure is intentionally compatible with both the JSON
    reporter (which emits it verbatim) and the SARIF reporter (which
    repackages the fields into a ``physicalLocation``).
    """
    if cell_name is None:
        return None
    cell = module.cells.get(cell_name)
    if cell is None:
        return None
    src = cell.attributes.get("src")
    if not src:
        return None
    # ``src`` may contain multiple ``file:loc`` entries separated by
    # spaces or pipes when a cell aggregates locations from several
    # source statements; pick the first.
    first = re.split(r"[ |]+", src.strip())[0]
    m = _SRC_RE.match(first)
    if m is None:
        return {"file": first}
    out: dict = {"file": m.group("file")}
    if m.group("sl"):
        out["start_line"] = int(m.group("sl"))
    if m.group("sc"):
        out["start_column"] = int(m.group("sc"))
    if m.group("el"):
        out["end_line"] = int(m.group("el"))
    if m.group("ec"):
        out["end_column"] = int(m.group("ec"))
    return out
