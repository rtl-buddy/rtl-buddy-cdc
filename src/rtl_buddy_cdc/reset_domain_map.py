"""Serialize the analyzer's reset-domain model to a stable JSON artifact.

The output is the v1.0 ``reset-domain-map`` schema, parallel to the
clock-domain map in :mod:`rtl_buddy_cdc.domain_map` (#106). Designed
for downstream consumers — chiefly `rtl-buddy-view
<https://github.com/rtl-buddy/rtl-buddy-view>`_ (reset-overlay on the
hierarchy view) — that want the reset-tree truth without
re-implementing reset inference, polarity classification, or RDC
detection.

Schema reference: rtl-buddy-cdc issue #108.

The format is stable: renaming or retyping any documented field
requires a ``schema_version`` bump (currently ``"1.0"``). Additions
are backward-compatible. Collections are sorted by a documented key
so a golden-file diff against the same inputs stays empty.
"""

from __future__ import annotations

import re
from typing import Any

from rtl_buddy_cdc.domain_map import _hier_path
from rtl_buddy_cdc.netlist import Module
from rtl_buddy_cdc.reporter import (
    TOOL_NAME,
    TOOL_VERSION,
    _SRC_RE,
    _source_location,
)
from rtl_buddy_cdc.reset_domain import (
    ResetCrossing,
    ResetDomain,
    ResetPolarity,
)

SCHEMA_VERSION = "1.0"
# The reset analysis runs on the same flattened ``Module`` as the
# clock-domain map; both frontends (Yosys, slang) lower to that shape.
_FRONTEND = "yosys"


def build_reset_domain_map(
    module: Module,
    reset_domains: dict[str, ResetDomain],
    clock_domains: dict[str, str | None],
    recognised_syncs: set[str],
    polarity_overrides: dict[str, ResetPolarity],
    reset_crossings: list[ResetCrossing],
) -> dict[str, Any]:
    """Build the v1.0 reset-domain-map payload.

    Arguments mirror the structural facts the analyzer already
    computes — no rule evaluation is required. ``reset_domains`` is
    the output of :func:`rtl_buddy_cdc.reset_domain.assign_reset_domains`;
    ``clock_domains`` is the per-flop clock map from
    :func:`rtl_buddy_cdc.domain.assign_domains` (flattened to
    ``{cell_name: clock_or_None}``); ``recognised_syncs`` is the union
    of :func:`rtl_buddy_cdc.reset_domain.find_reset_synchronizers` and
    user-marked flops; ``polarity_overrides`` is the
    ``(* reset_polarity *)`` port map from
    :func:`rtl_buddy_cdc.rules.user_reset_polarity_overrides`;
    ``reset_crossings`` is the output of
    :func:`rtl_buddy_cdc.reset_domain.find_reset_crossings` given the
    other inputs.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "design": {"top": module.name, "frontend": _FRONTEND},
        "reset_sources": _serialize_reset_sources(
            module, reset_domains, recognised_syncs, polarity_overrides
        ),
        "reset_synchronizers": _serialize_reset_synchronizers(
            module, reset_domains, clock_domains, recognised_syncs
        ),
        "flop_resets": _serialize_flop_resets(module, reset_domains, clock_domains),
        "reset_crossings": _serialize_reset_crossings(module, reset_crossings),
    }


# --- helpers ----------------------------------------------------------------


def _serialize_reset_sources(
    module: Module,
    reset_domains: dict[str, ResetDomain],
    recognised_syncs: set[str],
    polarity_overrides: dict[str, ResetPolarity],
) -> list[dict[str, Any]]:
    """Distinct upstream reset sources observed across every flop.

    Deduplicated by ``(name, source)``. Combinational and untracked
    sources (``source == "comb"``) are skipped — they're surfaced in
    ``reset_crossings`` instead, where the per-flop context makes the
    finding actionable.

    For a ``source == "port"`` entry with a ``(* reset_polarity *)``
    declaration on that port, the declared polarity is the
    authoritative value emitted under ``polarity``; the original
    consumer-inferred polarity drops out (the disagreement, if any,
    surfaces as a ``polarity-mismatch`` crossing).
    """
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for rd in reset_domains.values():
        rsrc = rd.reset
        if rsrc is None or rsrc.source == "comb":
            continue
        key = (rsrc.name, rsrc.source)
        if key in seen:
            continue
        polarity: ResetPolarity = rsrc.polarity
        if rsrc.source == "port" and rsrc.name in polarity_overrides:
            polarity = polarity_overrides[rsrc.name]
        entry: dict[str, Any] = {
            "name": rsrc.name,
            "source": rsrc.source,
            "polarity": polarity,
            "type": rsrc.type,
            "clock": rsrc.clock,
            "via_synchronizer": rsrc.source == "inferred"
            and rsrc.name in recognised_syncs,
        }
        if rsrc.source == "port" and rsrc.name in polarity_overrides:
            entry["declared_polarity"] = polarity_overrides[rsrc.name]
        loc = _reset_source_location(module, rsrc.name, rsrc.source)
        if loc is not None:
            entry["location"] = loc
        seen[key] = entry
    out = list(seen.values())
    out.sort(key=lambda e: (str(e["source"]), str(e["name"])))
    return out


def _serialize_reset_synchronizers(
    module: Module,
    reset_domains: dict[str, ResetDomain],
    clock_domains: dict[str, str | None],
    recognised_syncs: set[str],
) -> list[dict[str, Any]]:
    """One entry per flop in the recognised reset-synchroniser set.

    A "chain" view (head/tail/depth) would be ideal but isn't directly
    exposed by :func:`rtl_buddy_cdc.reset_domain.find_reset_synchronizers`
    today — it returns a flat set of member cells. v1.0 emits per-flop
    members; promoting to chain records is a v1.x extension.
    """
    out: list[dict[str, Any]] = []
    for name in recognised_syncs:
        rd = reset_domains.get(name)
        if rd is None:
            continue
        entry: dict[str, Any] = {
            "instance_path": _hier_path(module, name),
            "dest_clock": clock_domains.get(name),
        }
        if rd.reset is not None:
            entry["async_in"] = rd.reset.name
            entry["async_in_kind"] = rd.reset.source
        loc = _source_location(module, name)
        if loc is not None:
            entry["location"] = loc
        out.append(entry)
    out.sort(key=lambda e: str(e["instance_path"]))
    return out


def _serialize_flop_resets(
    module: Module,
    reset_domains: dict[str, ResetDomain],
    clock_domains: dict[str, str | None],
) -> list[dict[str, Any]]:
    """Per-flop reset assignment.

    Plain ``$dff`` / ``$dffe`` cells (no reset pin) are omitted — the
    section is the *reset* inventory, not a flop inventory; consumers
    that need every flop can use the clock-domain map.
    """
    out: list[dict[str, Any]] = []
    for name, rd in reset_domains.items():
        if rd.reset is None:
            continue
        entry: dict[str, Any] = {
            "instance_path": _hier_path(module, name),
            "clock": clock_domains.get(name),
            "reset": rd.reset.name,
            "reset_kind": rd.reset.source,
            "polarity": rd.reset.polarity,
            "type": rd.reset.type,
        }
        loc = _source_location(module, name)
        if loc is not None:
            entry["location"] = loc
        out.append(entry)
    out.sort(key=lambda e: str(e["instance_path"]))
    return out


def _serialize_reset_crossings(
    module: Module, crossings: list[ResetCrossing]
) -> list[dict[str, Any]]:
    """Structural reset crossings (the RDC analyzer's unified view).

    Each entry maps onto an RDC rule by ``kind`` — consumers that need
    the rule_id can derive it (``async-deassert`` → RDC-001,
    ``polarity-mismatch`` → RDC-002, ``sync-crossing`` → RDC-003,
    ``comb-driven`` → RDC-004). Rule severity and waiver status are
    intentionally not emitted here; they live in the analyzer's normal
    findings report.
    """
    out: list[dict[str, Any]] = []
    for c in crossings:
        entry: dict[str, Any] = {
            "instance_path": _hier_path(module, c.flop),
            "kind": c.kind,
            "flop_clock": c.flop_clock,
            "reset": c.reset.name,
            "reset_kind": c.reset.source,
            "polarity": c.reset.polarity,
            "type": c.reset.type,
        }
        loc = _source_location(module, c.flop)
        if loc is not None:
            entry["location"] = loc
        out.append(entry)
    out.sort(key=lambda e: (str(e["instance_path"]), str(e["kind"])))
    return out


def _reset_source_location(
    module: Module, name: str, source: str
) -> dict[str, Any] | None:
    """Best-effort source location for a reset source.

    Ports have no cell location — fall back to the netname's
    ``src`` attribute when available. Inferred sources resolve via
    the producer cell. Constant sources have no location.
    """
    if source == "port":
        nn = module.netnames.get(name)
        if nn is not None:
            raw = nn.attributes.get("src")
            if raw:
                return _parse_src_attr(raw)
        return None
    if source == "inferred":
        return _source_location(module, name)
    return None


def _parse_src_attr(raw: str) -> dict[str, Any] | None:
    """Decode a Yosys ``src`` attribute string via the reporter's regex.

    Output shape matches :func:`rtl_buddy_cdc.reporter._source_location`
    so port-sourced and cell-sourced locations are key-compatible.
    """
    first = re.split(r"[ |]+", raw.strip())[0]
    m = _SRC_RE.match(first)
    if m is None:
        return {"file": first} if first else None
    out: dict[str, Any] = {"file": m.group("file")}
    if m.group("sl"):
        out["start_line"] = int(m.group("sl"))
    if m.group("sc"):
        out["start_column"] = int(m.group("sc"))
    if m.group("el"):
        out["end_line"] = int(m.group("el"))
    if m.group("ec"):
        out["end_column"] = int(m.group("ec"))
    return out
