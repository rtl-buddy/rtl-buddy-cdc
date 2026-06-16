"""Structural surface for clock-network-driven flop→flop relationships.

CDC-010 detects a hazard the data-fanout walk in
:func:`rtl_buddy_cdc.domain.find_crossings` is blind to: a flop in one
clock domain drives the *control* pin of a clock-network cell (a mux's
``S``, an ICG's ``EN``) whose output clocks flops in another domain.
The async control transition chops the downstream clock into runt
pulses; no synchroniser at the sink can recover.

The rule pack already detects this and emits a :class:`Violation` per
``(cell, control_pin, src_flop)`` triple. This module exposes the same
facts as a parallel structural surface — one record per ``(src_flop,
dst_flop)`` pair where the relationship goes through the clock network —
so consumers like the domain-map emitter and the mermaid renderer can
draw the edge that ``find_crossings`` couldn't.

See issue #168 for the design discussion.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from rtl_buddy_cdc.domain import assign_domains
from rtl_buddy_cdc.flops import Flop, find_flops
from rtl_buddy_cdc.netlist import Bit, Module
from rtl_buddy_cdc.rules import (
    _backward_flop_fanin,
    _clock_network_cells,
    _control_pins_for,
    _OUTPUT_PINS,
)
from rtl_buddy_cdc.sdc import ClockSpec

ControlKind = Literal["mux-select", "gate-enable"]


@dataclass(frozen=True)
class ClockNetworkCrossing:
    """A flop drives a control pin whose host cell's output reaches another
    flop's CLK pin, and the two flops sit in async clock domains.

    Mirrors :class:`rtl_buddy_cdc.domain.Crossing` for the clock-network
    axis. ``src_flop`` is the control-pin driver; ``dst_flop`` is one of
    the flops whose CLK pin traces back through ``control_cell``.
    ``dst_clock`` is the clock-input domain of the controlled cell that
    ``src_clock`` is async to (a cell can have multiple clock-input
    domains; we record the first one we encounter — the rule that the
    source must be async to *every* one of them has already fired by
    the time the record is emitted, so any choice is correct).
    """

    src_flop: Flop
    src_clock: str
    dst_flop: Flop
    dst_clock: str
    control_cell: str
    control_cell_type: str
    control_pin: str
    control_kind: ControlKind


def find_clock_network_crossings(
    module: Module,
    clock_spec: ClockSpec | None = None,
    *,
    clock_for_port: Callable[[str], str | None] | None = None,
    use_heuristic: bool = True,
    max_depth: int = 16,
) -> list[ClockNetworkCrossing]:
    """Enumerate every clock-network-driven async flop→flop pair.

    The walk is the same structural shape as
    :func:`rtl_buddy_cdc.rules.check_cdc_010`: for each cell on the
    clock-distribution network, pull the control pins via
    :func:`_control_pins_for`, walk the control bits back to source
    flops via :func:`_backward_flop_fanin`, and compare each source
    flop's clock domain against the cell's own clock-input domains.
    The new bit — the part the rule pack doesn't need — is the
    forward mapping from controlled cell to the downstream flops it
    clocks, computed once up front via
    :func:`_cell_to_downstream_flops`. Records are emitted per
    ``(src_flop, dst_flop)`` pair rather than per cell so consumers
    can draw the relationship as an edge.

    Determinism: every emitted list (per (cell, ctrl_port) source
    flops, per cell downstream flops, the final crossings list) is
    sorted by name so two runs on the same input produce the same
    sequence.

    ``max_depth`` is the clock-trace hop budget forwarded to
    :func:`~rtl_buddy_cdc.domain.assign_domains` (default 16, surfaced
    as ``--clock-trace-depth``). It must match the budget the main
    crossing walk uses so this clock-network view of per-flop domains
    agrees with the crossings list at any depth. See issue #263.
    """
    pin_clocks = clock_spec.pin_clocks if clock_spec is not None else None
    flop_domains = assign_domains(
        module,
        pin_clocks=pin_clocks,
        clock_for_port=clock_for_port,
        max_depth=max_depth,
    )
    flop_by_name = {fd.flop.cell.name: fd.flop for fd in flop_domains}
    domain_by_name = {fd.flop.cell.name: fd.clock for fd in flop_domains}

    drivers = _bit_drivers_with_idx(module)
    clock_net_cells = _clock_network_cells(module, drivers)
    if not clock_net_cells:
        return []
    cell_to_dst_flops = _cell_to_downstream_flops(module, drivers)

    def _resolve(name: str) -> str:
        if clock_for_port is None:
            return name
        return clock_for_port(name) or name

    def _async(a: str, b: str) -> bool:
        if clock_spec is None:
            return a != b
        return clock_spec.are_async(_resolve(a), _resolve(b))

    crossings: list[ClockNetworkCrossing] = []
    seen: set[tuple[str, str, str]] = set()

    for cell_name in sorted(clock_net_cells):
        cell = module.cells[cell_name]
        control_ports = _control_pins_for(cell, use_heuristic=use_heuristic)
        if not control_ports:
            continue
        dst_flop_names = sorted(cell_to_dst_flops.get(cell_name, ()))
        if not dst_flop_names:
            continue
        cell_clock_domains: set[str] | None = None
        for ctrl_port in sorted(control_ports):
            ctrl_bits = cell.connections.get(ctrl_port, ())
            if not ctrl_bits:
                continue
            ctrl_fanin = _backward_flop_fanin(module, ctrl_bits, drivers)
            if not ctrl_fanin:
                continue
            if cell_clock_domains is None:
                cell_clock_domains = _cell_clock_input_domains(
                    module,
                    cell,
                    drivers,
                    domain_by_name,
                    clock_spec,
                    control_ports,
                )
            if not cell_clock_domains:
                continue
            kind = _control_kind_for(ctrl_port, cell.type)
            for src_flop_name in sorted(ctrl_fanin):
                src_clk = domain_by_name.get(src_flop_name)
                if src_clk is None:
                    continue
                if src_clk in cell_clock_domains:
                    continue
                if not all(_async(src_clk, d) for d in cell_clock_domains):
                    continue
                # Pick a deterministic representative for the cell's
                # gated-clock domain — sorted so the choice is stable
                # across runs. CDC-010 already enforced that src_clk
                # is async to every member of the set.
                dst_clk = sorted(cell_clock_domains)[0]
                src_flop = flop_by_name.get(src_flop_name)
                if src_flop is None:
                    continue
                for dst_flop_name in dst_flop_names:
                    dst_flop = flop_by_name.get(dst_flop_name)
                    if dst_flop is None or dst_flop is src_flop:
                        continue
                    key = (src_flop_name, dst_flop_name, cell_name)
                    if key in seen:
                        continue
                    seen.add(key)
                    crossings.append(
                        ClockNetworkCrossing(
                            src_flop=src_flop,
                            src_clock=src_clk,
                            dst_flop=dst_flop,
                            dst_clock=dst_clk,
                            control_cell=cell_name,
                            control_cell_type=cell.type,
                            control_pin=ctrl_port,
                            control_kind=kind,
                        )
                    )

    crossings.sort(
        key=lambda c: (
            c.src_flop.cell.name,
            c.dst_flop.cell.name,
            c.control_cell,
            c.control_pin,
        )
    )
    return crossings


# --- helpers ----------------------------------------------------------------


def _bit_drivers_with_idx(
    module: Module,
) -> dict[Bit, tuple[str, str, int]]:
    """Like :func:`rtl_buddy_cdc.domain._bit_drivers` but with the bit
    index — the schema the rules.py helpers consume."""
    out: dict[Bit, tuple[str, str, int]] = {}
    for cell in module.cells.values():
        for port_name in ("Y", "Q"):
            for idx, b in enumerate(cell.connections.get(port_name, ())):
                if isinstance(b, int):
                    out[b] = (cell.name, port_name, idx)
    return out


def _cell_to_downstream_flops(
    module: Module,
    drivers: dict[Bit, tuple[str, str, int]],
) -> dict[str, set[str]]:
    """Inverse of :func:`_clock_network_cells`: for each clock-network
    cell, the set of flop cell-names whose CLK pin is reached when
    walking back from the flop through that cell."""
    out: dict[str, set[str]] = defaultdict(set)
    for f in find_flops(module):
        if not isinstance(f.clk, int):
            continue
        seen_bits: set[Bit] = set()
        frontier: list[Bit] = [f.clk]
        while frontier:
            nxt: list[Bit] = []
            for bit in frontier:
                if bit in seen_bits:
                    continue
                seen_bits.add(bit)
                drv = drivers.get(bit)
                if drv is None:
                    continue
                cell_name = drv[0]
                out[cell_name].add(f.cell.name)
                cell = module.cells[cell_name]
                for in_port, in_bits in cell.connections.items():
                    if in_port in _OUTPUT_PINS:
                        continue
                    for b in in_bits:
                        if isinstance(b, int):
                            nxt.append(b)
            frontier = nxt
    return dict(out)


def _cell_clock_input_domains(
    module: Module,
    cell,
    drivers: dict[Bit, tuple[str, str, int]],
    domain_by_name: dict[str, str | None],
    clock_spec: ClockSpec | None,
    control_ports: frozenset[str],
    max_depth: int = 12,
) -> set[str]:
    """Set of clock-domain names driving ``cell``'s non-control inputs.

    Self-contained mirror of :func:`rtl_buddy_cdc.rules._clock_input_domains_for`
    — takes the precomputed drivers + domain map directly rather than a
    :class:`_RuleContext`, so this module doesn't need to instantiate
    rule-pack-specific state.
    """
    domains: set[str] = set()
    seen: set[Bit] = set()
    frontier: list[tuple[Bit, int]] = []
    for port_name, bits in cell.connections.items():
        if port_name in _OUTPUT_PINS or port_name in control_ports:
            continue
        for b in bits:
            if isinstance(b, int):
                frontier.append((b, 0))

    while frontier:
        nxt: list[tuple[Bit, int]] = []
        for bit, depth in frontier:
            if bit in seen:
                continue
            seen.add(bit)
            drv = drivers.get(bit)
            if drv is None:
                if clock_spec is None:
                    continue
                port = module.port_of_bit(bit)
                if port is None:
                    continue
                clk = clock_spec.clock_for_port(port.name)
                if clk is not None:
                    domains.add(clk)
                continue
            src_cell_name, out_port, _idx = drv
            if out_port == "Q":
                src_clk = domain_by_name.get(src_cell_name)
                if src_clk is not None:
                    domains.add(src_clk)
                continue
            if depth >= max_depth:
                continue
            src_cell = module.cells[src_cell_name]
            for in_port, in_bits in src_cell.connections.items():
                if in_port in _OUTPUT_PINS:
                    continue
                for b in in_bits:
                    if isinstance(b, int):
                        nxt.append((b, depth + 1))
        frontier = nxt
    return domains


_GATE_ENABLE_PINS: frozenset[str] = frozenset({"E", "EN", "CE", "GATE", "SE"})


def _control_kind_for(control_pin: str, cell_type: str) -> ControlKind:
    """Classify a control pin as mux-select or gate-enable.

    Cell-type-driven first (mux families have ``S``/``T``/``U``/``V``
    selects; everything else with a control pin is treated as a gate
    enable). The pin-name list is the same heuristic the rule pack uses
    in :data:`rtl_buddy_cdc.rules._CDC_010_HEURISTIC_PINS`.
    """
    if cell_type in {"$mux", "$pmux", "$_MUX_", "$_MUX4_", "$_MUX8_", "$_MUX16_"}:
        return "mux-select"
    if control_pin.upper() in _GATE_ENABLE_PINS:
        return "gate-enable"
    # Fallback — anything else with a select-shaped name (rare in
    # practice). Treat as mux-select for visual differentiation.
    return "mux-select"
