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

from rtl_buddy_cdc.domain import Crossing, FlopDomain
from rtl_buddy_cdc.netlist import Module
from rtl_buddy_cdc.rules import Violation
from rtl_buddy_cdc.sdc import ClockSpec
from rtl_buddy_cdc.waivers import SuppressedViolation

TOOL_NAME = "rtl-buddy-cdc"
TOOL_VERSION = "0.2.0"
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
}

# Short-form descriptions per rule. Keep this terse — the long-form
# message is already attached to each result.
_RULE_DESCRIPTIONS: dict[str, str] = {
    "CDC-001": "Unsynchronized control crossing (no second-stage flop)",
    "CDC-002": "Insufficient synchronizer depth",
    "CDC-003": "Combinational logic before synchronizer first stage",
    "CDC-004": "Bus crossing without gating or gray-coding",
    "CDC-005": "Reconvergent synchronizers",
    "CDC-006": "Glitchy combinational source on a control crossing",
    "CDC-007": "Async reset crossing without a reset synchronizer",
    "CDC-008": "Clock signal used as data",
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

    if verbose and result.crossings:
        out.write(f"\n{s.dim}  Crossings:{s.reset}\n")
        for c in result.crossings:
            tag = ""
            if c.is_port_sourced:
                tag = f" {s.dim}(port-sourced){s.reset}"
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
        for v in vs:
            _render_one_violation(v, result.module, out, s)


def _render_one_violation(
    v: Violation, module: Module, out: IO[str], s: _Style
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
    out.write(f"    {severity_color}{v.severity}{s.reset}{loc_str}\n")
    for line in _wrap_message(v.message):
        out.write(f"      {line}\n")


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


def _render_suppressed(result: AnalysisResult, out: IO[str], s: _Style) -> None:
    if not result.suppressed:
        return
    out.write(f"\n{s.bold}Suppressed by waivers{s.reset} ({len(result.suppressed)})\n")
    for sup in result.suppressed:
        rule_id = sup.violation.rule_id
        reason = sup.waiver.reason or "(no reason given)"
        out.write(
            f"  {s.dim}{rule_id}{s.reset}  {reason}  "
            f"{s.dim}(waiver line {sup.waiver.source_line}){s.reset}\n"
        )


def _severity_counts(violations: list[Violation]) -> dict[str, int]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for v in violations:
        counts[v.severity] = counts.get(v.severity, 0) + 1
    return counts


# --- json -------------------------------------------------------------------


def render_json(result: AnalysisResult, out: IO[str]) -> None:
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
        },
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
                    "source_line": s.waiver.source_line,
                },
            }
            for s in result.suppressed
        ],
    }
    json.dump(payload, out, indent=2)
    out.write("\n")


def _crossing_to_dict(c: Crossing) -> dict:
    out: dict = {
        "src_clock": c.src_clock,
        "dst_clock": c.dst_clock,
        "dst_flop": c.dst_flop.cell.name,
        "width": c.width,
        "min_hops": c.min_hops,
    }
    if c.src_flop is not None:
        out["src_flop"] = c.src_flop.cell.name
    if c.src_port is not None:
        out["src_port"] = c.src_port
    return out


def _violation_to_dict(v: Violation, module: Module) -> dict:
    out = {
        "rule_id": v.rule_id,
        "severity": v.severity,
        "message": v.message,
    }
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
        entry["locations"] = [{"physicalLocation": physical}]
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
