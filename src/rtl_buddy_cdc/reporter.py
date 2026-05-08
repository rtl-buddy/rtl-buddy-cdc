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
import re
from dataclasses import dataclass, field
from typing import IO

from rtl_buddy_cdc.domain import Crossing, FlopDomain
from rtl_buddy_cdc.netlist import Module
from rtl_buddy_cdc.rules import Violation
from rtl_buddy_cdc.sdc import ClockSpec
from rtl_buddy_cdc.waivers import SuppressedViolation

TOOL_NAME = "rtl-buddy-cdc"
TOOL_VERSION = "0.1.0"
TOOL_INFO_URI = "https://github.com/rtl-buddy/rtl-buddy-cdc"


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


def render_text(result: AnalysisResult, out: IO[str]) -> None:
    out.write(f"module: {result.module.name}\n")
    out.write(f"  ports: {len(result.module.ports)}\n")
    out.write(f"  cells: {len(result.module.cells)}\n")
    out.write(f"  flops: {len(result.domains)}\n")

    by_clock: dict[str | None, int] = {}
    for fd in result.domains:
        by_clock[fd.clock] = by_clock.get(fd.clock, 0) + 1
    for clk, n in sorted(by_clock.items(), key=lambda x: (x[0] is None, x[0] or "")):
        label = clk if clk is not None else "<unresolved>"
        out.write(f"    domain {label}: {n} flop(s)\n")

    out.write(f"  crossings (flop→flop, different clock): {len(result.crossings)}\n")
    for c in result.crossings:
        out.write(
            f"    {c.src_clock} → {c.dst_clock}  "
            f"({c.src_flop.name} → {c.dst_flop.name}, "
            f"width={c.width}, min_hops={c.min_hops})\n"
        )

    if result.spec is None:
        out.write(
            "  no SDC supplied — skipping rule checks "
            "(every cross-clock crossing is treated as synchronous)\n"
        )
        return

    out.write(
        f"  sdc: {len(result.spec.clocks)} clock(s), "
        f"{len(result.spec.async_groups)} async-group statement(s)\n"
    )
    out.write(
        f"  async crossings (per SDC clock groups): {len(result.async_crossings)}\n"
    )
    if result.violations:
        out.write(f"  {len(result.violations)} violation(s):\n")
        for v in result.violations:
            out.write(f"    [{v.rule_id}] {v.severity}: {v.message}\n")
    else:
        out.write("  no rule violations.\n")

    if result.suppressed:
        out.write(f"  {len(result.suppressed)} suppressed by waivers:\n")
        for s in result.suppressed:
            reason = s.waiver.reason or "(no reason given)"
            out.write(
                f"    [{s.violation.rule_id}] suppressed: {reason} "
                f"(waiver line {s.waiver.source_line})\n"
            )


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
    return {
        "src_clock": c.src_clock,
        "dst_clock": c.dst_clock,
        "src_flop": c.src_flop.cell.name,
        "dst_flop": c.dst_flop.cell.name,
        "width": c.width,
        "min_hops": c.min_hops,
    }


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
