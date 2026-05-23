"""Serialize the analyzer's domain model to a stable JSON artifact.

The output is the v1.0 ``domain-map`` schema designed for downstream
consumers — chiefly `rtl-buddy-view
<https://github.com/rtl-buddy/rtl-buddy-view>`_ (clock-domain overlay on
the hierarchy view), but generally useful for any tool that wants the
analyzer's clock/flop/crossing inventory without re-implementing SDC
parsing.

Schema reference: rtl-buddy-cdc issue #106.

The format is stable: rename or retype any documented field requires a
``schema_version`` bump (currently ``"1.0"``). Additions are
backward-compatible. Collections are sorted by a documented key so a
golden-file diff against the same inputs stays empty.
"""

from __future__ import annotations

from typing import Any

from rtl_buddy_cdc.clock_network import ClockNetworkCrossing
from rtl_buddy_cdc.domain import Crossing, FlopDomain
from rtl_buddy_cdc.netlist import Module
from rtl_buddy_cdc.reporter import (
    TOOL_NAME,
    TOOL_VERSION,
    _instance_path,
    _source_location,
)
from rtl_buddy_cdc.sdc import UNCONSTRAINED_SENTINEL, ClockSpec

# v1.1 adds the additive optional ``clock_network_crossings`` list
# (#168). v1.0 consumers ignore unknown keys, so the bump is
# backward-compatible — old maps without the field still validate.
SCHEMA_VERSION = "1.1"
# Yosys is the only frontend whose ``Module`` we serialize today (the
# slang frontend lowers to the same shape, so it lands here unchanged).
_FRONTEND = "yosys"


def build_domain_map(
    module: Module,
    domains: list[FlopDomain],
    crossings: list[Crossing],
    spec: ClockSpec | None,
    *,
    async_crossings: list[Crossing] | None = None,
    clock_network_crossings: list[ClockNetworkCrossing] | None = None,
) -> dict[str, Any]:
    """Build the v1.1 domain-map payload.

    ``async_crossings`` is the SDC-aware subset of ``crossings`` that
    the rule pack would receive. Each emitted crossing is tagged with
    ``async_per_sdc`` accordingly. Pass ``None`` when no SDC was
    supplied — every entry then carries ``async_per_sdc: false``,
    matching the convention that no-SDC runs have nothing to compare
    against.

    ``clock_network_crossings`` is the parallel surface for flop→flop
    relationships that travel through the clock network (a foreign-
    domain flop driving a clock-mux select or ICG enable). Empty list
    when omitted — consumers always see the key. See issue #168.
    """
    async_keys = _crossing_keys(async_crossings or [])
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "design": {"top": module.name, "frontend": _FRONTEND},
        "clocks": _serialize_clocks(spec, generated=False),
        "generated_clocks": _serialize_clocks(spec, generated=True),
        "clock_groups": _serialize_clock_groups(spec),
        "false_path_pairs": _serialize_false_paths(spec),
        "flop_domains": _serialize_flop_domains(module, domains),
        "port_domains": _serialize_port_domains(module, spec),
        "crossings": _serialize_crossings(module, crossings, async_keys),
        "clock_network_crossings": _serialize_clock_network_crossings(
            module, clock_network_crossings or []
        ),
    }


# --- helpers ----------------------------------------------------------------


def _serialize_clocks(
    spec: ClockSpec | None, *, generated: bool
) -> list[dict[str, Any]]:
    if spec is None:
        return []
    out: list[dict[str, Any]] = []
    for clk in spec.clocks.values():
        if clk.is_generated != generated:
            continue
        if generated:
            entry: dict[str, Any] = {
                "name": clk.name,
                "master": clk.master,
                "period": clk.period,
            }
            # Port-targeted generated clocks (rare but legal) carry the
            # port list; pin-targeted ones (the common case) carry an
            # empty tuple and the ``pin_clocks`` map on the spec.
            if clk.ports:
                entry["ports"] = list(clk.ports)
            out.append(entry)
        else:
            out.append(
                {
                    "name": clk.name,
                    "period": clk.period,
                    "source": "create_clock",
                    "ports": list(clk.ports),
                }
            )
    out.sort(key=lambda e: str(e["name"]))
    return out


def _serialize_clock_groups(spec: ClockSpec | None) -> list[dict[str, Any]]:
    if spec is None:
        return []
    groups: list[dict[str, Any]] = []
    for grp in spec.async_groups:
        members = [sorted(s) for s in grp]
        groups.append({"kind": "asynchronous", "members": members})
    for grp in spec.exclusive_groups:
        members = [sorted(s) for s in grp]
        groups.append({"kind": "exclusive", "members": members})
    groups.sort(
        key=lambda g: (
            g["kind"],
            g["members"][0][0] if g["members"] and g["members"][0] else "",
        )
    )
    return groups


def _serialize_false_paths(spec: ClockSpec | None) -> list[list[str]]:
    if spec is None:
        return []
    pairs = [sorted(fp) for fp in spec.false_path_pairs]
    pairs.sort(key=lambda p: (p[0], p[1] if len(p) > 1 else ""))
    return pairs


def _serialize_flop_domains(
    module: Module, domains: list[FlopDomain]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fd in domains:
        cell_name = fd.flop.cell.name
        entry: dict[str, Any] = {
            "instance_path": _hier_path(module, cell_name),
            "source_instance_path": _source_instance_path(module, cell_name),
            "clock": fd.clock,
        }
        loc = _source_location(module, cell_name)
        if loc is not None:
            entry["location"] = loc
        out.append(entry)
    out.sort(key=lambda e: str(e["instance_path"]))
    return out


def _serialize_port_domains(
    module: Module, spec: ClockSpec | None
) -> list[dict[str, Any]]:
    """Top-level ports the SDC has typed to a clock domain.

    Selection rule: every port name present in
    :attr:`ClockSpec.port_clock` (populated by ``set_input_delay
    -clock`` and ``set_output_delay -clock``) whose value is a real
    clock — the unconstrained-input sentinel from
    :mod:`rtl_buddy_cdc.sdc` is filtered out.

    Clock-source ports (the ports that ``create_clock`` targets) are
    intentionally excluded: they're already listed under
    ``clocks[].ports``. Outputs are included with ``kind: "output"``
    when they carry a ``set_output_delay -clock`` typing.
    """
    if spec is None:
        return []
    out: list[dict[str, Any]] = []
    for port_name, clk in spec.port_clock.items():
        if clk == UNCONSTRAINED_SENTINEL:
            continue
        port = module.ports.get(port_name)
        if port is None:
            continue
        out.append(
            {
                "module": module.name,
                "port": port_name,
                "clock": clk,
                "kind": port.direction,
            }
        )
    out.sort(key=lambda e: (str(e["module"]), str(e["port"])))
    return out


def _serialize_crossings(
    module: Module,
    crossings: list[Crossing],
    async_keys: set[tuple[str, str, str, str]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in crossings:
        dst_path = _hier_path(module, c.dst_flop.cell.name)
        src_path = (
            _hier_path(module, c.src_flop.cell.name)
            if c.src_flop is not None
            else (c.src_port or "")
        )
        # async_keys was built from raw cell names — match on those, not
        # the hier-path strings the consumer sees.
        src_key = c.src_flop.cell.name if c.src_flop is not None else (c.src_port or "")
        key = (c.src_clock, c.dst_clock, c.dst_flop.cell.name, src_key)
        entry: dict[str, Any] = {
            "src_clock": c.src_clock,
            "dst_clock": c.dst_clock,
            "dst_flop": dst_path,
            "dst_source_instance_path": _source_instance_path(
                module, c.dst_flop.cell.name
            ),
            "min_hops": c.min_hops,
            "width": c.width,
            "async_per_sdc": key in async_keys,
        }
        if c.src_flop is not None:
            entry["src_flop"] = src_path
            entry["src_source_instance_path"] = _source_instance_path(
                module, c.src_flop.cell.name
            )
        if c.src_port is not None:
            entry["src_port"] = c.src_port
        out.append(entry)
    out.sort(
        key=lambda e: (
            str(e["src_clock"]),
            str(e["dst_clock"]),
            str(e["dst_flop"]),
            str(e.get("src_flop") or e.get("src_port") or ""),
        )
    )
    return out


def _serialize_clock_network_crossings(
    module: Module,
    crossings: list[ClockNetworkCrossing],
) -> list[dict[str, Any]]:
    """Per-cell flop→flop relationships routed via the clock network.

    Each entry carries the source flop (control-pin driver), the
    destination flop (whose CLK pin the controlled cell feeds), the
    two clock domains, and metadata describing the gating cell
    (``control_cell``, ``control_cell_type``, ``control_pin``,
    ``control_kind`` ∈ {``"mux-select"``, ``"gate-enable"``}). Always
    ``async_per_sdc: true`` here — the upstream walker only emits
    pairs where the source domain is async to every clock-input
    domain of the controlled cell, mirroring CDC-010's firing
    condition.
    """
    out: list[dict[str, Any]] = []
    for c in crossings:
        out.append(
            {
                "src_clock": c.src_clock,
                "dst_clock": c.dst_clock,
                "src_flop": _hier_path(module, c.src_flop.cell.name),
                "src_source_instance_path": _source_instance_path(
                    module, c.src_flop.cell.name
                ),
                "dst_flop": _hier_path(module, c.dst_flop.cell.name),
                "dst_source_instance_path": _source_instance_path(
                    module, c.dst_flop.cell.name
                ),
                "control_cell": c.control_cell,
                "control_cell_type": c.control_cell_type,
                "control_pin": c.control_pin,
                "control_kind": c.control_kind,
                "async_per_sdc": True,
            }
        )
    out.sort(
        key=lambda e: (
            str(e["src_flop"]),
            str(e["dst_flop"]),
            str(e["control_cell"]),
            str(e["control_pin"]),
        )
    )
    return out


def _crossing_keys(
    cs: list[Crossing],
) -> set[tuple[str, str, str, str]]:
    """Build the key set used by ``async_per_sdc`` lookup.

    Keys are constructed from raw cell names — the serializer rewrites
    them to ``_hier_path`` form before comparing, so this helper has to
    use the same representation. Done lazily inside the serializer to
    avoid duplicating the ``Module`` plumbing here."""
    out: set[tuple[str, str, str, str]] = set()
    for c in cs:
        src = c.src_flop.cell.name if c.src_flop is not None else (c.src_port or "")
        out.add((c.src_clock, c.dst_clock, c.dst_flop.cell.name, src))
    return out


def _hier_path(module: Module, cell_name: str) -> str:
    """``<top>.<parent-chain>.<leaf>`` for a cell.

    Parent chain reuses the reporter's flatten-aware path inferer
    (handles Yosys' ``$flatten\\`` prefix, slang's plain dotted shape,
    and top-level auto-names). The leaf is the trailing identifier of
    the original cell name with the Yosys identifier-escape stripped.
    For top-level auto-named cells (``$procdff$42``) the result is
    ``<top>.<leaf>``.
    """
    parents = _instance_path(module, cell_name)
    leaf = _cell_leaf_name(cell_name)
    parts = [module.name, *parents, leaf]
    return ".".join(parts)


def _source_instance_path(module: Module, cell_name: str | None) -> str | None:
    """Dotted path of the deepest source-level module instance owning a cell.

    The analyzer already walks the elaboration tree; the source-level
    enclosing instance is the upward chain reachable by stripping the
    synth-generated leaf handle (``$slang$sdff$N``, ``$procdff$N``,
    etc.) from the cell's flattened name. Returns the chain rooted at
    the design top — never includes the leaf cell itself. For a cell
    instantiated at top, the result is just the top module name.

    Returns ``None`` when the analyzer can't resolve the chain (kept
    distinct from a missing field so consumers can tell "unresolvable"
    from "older producer that didn't emit this field"). With today's
    Yosys and slang frontends the chain is always resolvable for cells
    the analyzer holds a :class:`Module` reference to, so this branch
    is reserved for future frontends whose flop-naming convention is
    less amenable to a name-only path-walk (e.g. Yosys runs that skip
    ``proc``/``flatten``).
    """
    if cell_name is None:
        return None
    parents = _instance_path(module, cell_name)
    return ".".join([module.name, *parents])


def _cell_leaf_name(cell_name: str) -> str:
    flatten_prefix = "$flatten\\"
    if cell_name.startswith(flatten_prefix):
        body = cell_name[len(flatten_prefix) :]
        # The leaf is whatever follows the last non-leaf dot. Reuse the
        # same convention as _instance_path: walk dot-separated tokens
        # until we hit one starting with ``$``; that token (and the
        # remainder of the body) is the leaf.
        tail: list[str] = []
        capturing = False
        for tok in body.split("."):
            if capturing or tok.startswith("$"):
                tail.append(tok)
                capturing = True
        if tail:
            return ".".join(tail)
        return body.rsplit(".", 1)[-1].lstrip("\\")
    if "." in cell_name and not cell_name.startswith("$"):
        return cell_name.rsplit(".", 1)[-1]
    return cell_name
