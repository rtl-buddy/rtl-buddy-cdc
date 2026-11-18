"""Per-module standalone CDC analysis for boundary subtrees (#261).

Boundary abstraction (#253 / #256 / #257) buys scale by collapsing a
subtree to its port boundary, and §4.9 of the architecture spec records
what that costs: every rule that needs the block's *internals* —
reconvergence (CDC-005), internal synchroniser depth (CDC-002), internal
gray coding, clock-as-data on internal nets — is traded away. PR #259
closed the *silent* part of that trade conservatively by **declining**
the dangerous cases (multi-clock, or single-clock with ≥2 incoming
boundary crossings), which is sound but costs exactly the dense-CDC
integration blocks abstraction is most valuable on.

This module is the principled fix §4.9 names as "route (a)": analyse each
boundary **module once, on its own internals**, where the ordinary
pipeline (``assign_domains`` → ``find_crossings`` → the rule pack) runs
normally, and lift the result into the :class:`BoundarySummary` instead
of erasing it.

**The internals have to be there.** A module blackboxed through
``read_slang --blackboxed-module`` arrives with *zero cells*, so there is
nothing to analyse and every function here declines (returns ``None``) —
that path behaves exactly as it did before this change. The compositional
pass needs a **greybox** netlist: a module that carries the Yosys
``blackbox`` attribute (so ``flatten`` leaves it standing and the parent
keeps an ordinary instance cell) **and keeps its cells**. Yosys produces
one with ``setattr -mod -set blackbox 1 <module>`` between ``proc`` and
``flatten``; the CLI surfaces it as ``lint --greybox MODULE``.

Everything here is pure: it reads a :class:`Module` and a
:class:`ClockSpec` and returns a frozen record. The orchestration that
decides *which* modules to analyse, caches the result per ``(module type,
clock context)`` and folds it into the boundary summaries lives in
:mod:`rtl_buddy_cdc.hierarchy`.

Soundness posture (§4.9's asymmetry). Every fact this module publishes is
either over-reporting or *proven*:

- a port's capture / launch domain is published only when it resolves to
  exactly one domain; anything ambiguous is reported as such and the
  caller declines or falls back to the unconstrained sentinel;
- ``synchronised`` — the one lever that can make the tool **under**
  report — is set only when a ≥2-stage chain is proven structurally (or
  the first stage carries a ``USER_SYNC_ATTRS`` tag, the user's explicit
  promise). A tie-off, a partial-bus path, a comb bypass around the
  chain, a multi-bit port, or a port that also feeds an output
  combinationally all defeat the proof.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

from rtl_buddy_cdc.domain import (
    Crossing,
    DepthCut,
    clock_network_events,
    filter_async,
    filter_async_cuts,
    find_crossings,
)
from rtl_buddy_cdc.flops import (
    Flop,
    flop_async_controls,
    flop_clk_polarity,
    flop_reset_bits,
    is_ff_cell,
    is_latch_cell,
)
from rtl_buddy_cdc.netlist import Bit, DriverFacts, Module, PinFacts, ResetPinFact
from rtl_buddy_cdc.pulse import classify_d_pin_shape, classify_toggle_d_pin
from rtl_buddy_cdc.rules import (
    _ack_walk,
    _clock_control_cells,
    _clock_input_domains_for,
    _gated_capture_fanin,
    _ack_walk_excluded,
    _hold_control_bits,
    Violation,
    _backward_flop_fanin,
    _ASYNC_PIN_LABEL,
    _backward_fanin,
    _build_context,
    _chain_has_inter_stage_comb,
    _decoder_group,
    _deasserting_reset_sync_members,
    _folded_srst_selects,
    _live_mux_driver,
    _forward_reachable_cells,
    _is_gray_encoded_source,
    _own_srst_data_leg,
    _producer_async_reset_value,
    _RuleContext,
    _sync_chain_depth,
    _sync_chain_flops,
    run_all,
)
from rtl_buddy_cdc.sdc import Clock, ClockSpec

# Rules whose predicate is literally "a TOP-LEVEL port of the design".
# A blackboxed subtree's ports are hierarchy pins driven by the parent,
# not chip pins, so at module scope these fire on the boundary itself and
# call every correctly-built IP a bug:
#
# - **CDC-006** flags a synchroniser first stage whose ``D`` traces back
#   to an unregistered top-level port with no source flop between. That
#   is the *definition* of a boundary input synchroniser — the textbook
#   2FF chain on an IP's input port trips it every time.
# - **CDC-011** flags an input port the SDC never typed with
#   ``set_input_delay``. No subtree pin is ever typed (see
#   :func:`derive_sub_spec`), so every data input would fire.
# - **RDC-008** flags an async reset driven *directly by a top-level
#   port*. A reset arriving on a module pin is by construction exactly
#   that; the flattened design sees the same structure as a flop-sourced
#   reset and reports it under RDC-001 instead.
# - **RDC-005** flags an async reset that is a gate over two or more
#   *top-level reset ports*. Two reset pins of a module are hierarchy pins;
#   whether they are two top-level ports, one, or registers is the
#   parent's to know. The parent re-raises it (and RDC-004, the
#   registered shape) from the reset facts the pass publishes
#   (rtl-buddy-cdc#405).
# - **CDC-025** flags a top-level *output* with no ``set_output_delay``
#   contract. No subtree pin is ever typed, so its single-domain shape is
#   already gated off here; its asynchronous-merge shape would still fire
#   on a subtree output pin, which is an internal net of the parent — the
#   parent's crossing walk reports whatever captures it. (A merge driving
#   a real top-level output *through* an abstracted boundary pin is not
#   seen by the parent either — the same scope limit CDC-024 has.)
#
# None of the input-side three goes silent overall: what they are really
# about — data (or a reset) entering the block from a foreign domain — is
# reported by the PARENT as the boundary-sink crossing at that very port,
# where CDC-001 / CDC-003 / CDC-004 apply. And comb logic between the port
# and the first internal flop *defeats* the synchroniser proof
# (:func:`_prove_input_synchroniser`), so the boundary falls back to
# firing CDC-001 rather than staying quiet. The residual is a rule-id
# difference against a flat run, never a dropped hazard.
#
# Everything else in the pack runs. The derived sub-spec (see
# :func:`derive_sub_spec`) declares the module's traced clock pins as real
# clocks, so the rules that key off declared-clock identity (CDC-008 /
# CDC-010 / CDC-021 / CDC-023) see a coherent SDC rather than an
# undeclared-everything one.
INTERNAL_RULE_EXCLUSIONS: frozenset[str] = frozenset(
    {"CDC-006", "CDC-011", "CDC-025", "RDC-005", "RDC-008"}
)


@dataclass(frozen=True)
class PortFacts:
    """What the standalone pass proved about one data port of a subtree.

    ``clock`` is the port's single internal domain — the domain that
    *captures* an input (the first flop the port reaches) or *launches*
    an output (the flops in its backward cone). ``None`` means it did not
    resolve to exactly one domain: either nothing sequential touches the
    port (a pure combinational feed-through) or several domains do. The
    two are distinguished by ``ambiguous``, because they call for
    opposite conservative answers — no capture at all means no crossing
    *into* the block at that port, whereas capture in two domains cannot
    be expressed by a single virtual sink and must decline.

    ``sync_depth`` is the proven length of the synchroniser chain the
    port's first capturing stage starts, and is set only when the proof
    holds end to end (see the module docstring). ``synchronised`` is the
    boundary-facing verdict: depth ≥ 2, or a user-tagged first stage.
    """

    port: str
    direction: str
    width: int
    clock: str | None = None
    ambiguous: bool = False
    sync_depth: int | None = None
    user_synchronised: bool = False
    # The output port, and the bit of it, the proven input chain's last
    # stage drives and nothing else reads (rtl-buddy-cdc#405). A flat run
    # sees the chain carry on into whatever the parent hangs on that
    # output; the boundary hides that, so the parent rejoins the two
    # halves through this fact. ``None`` when the chain ends inside.
    chain_exit: tuple[str, int] | None = None
    # The proven chain's stages, first to last, each with its clock edge
    # (1 posedge, 0 negedge), so CDC-016 can check a chain the boundary
    # hides (rtl-buddy-cdc#405). Empty when not proven stage by stage.
    chain_stages: tuple[tuple[str, int], ...] = ()
    # For an output, single-bit ports only (rtl-buddy-cdc#405): the fewest
    # combinational cells between a launching register and the pin
    # (``0`` for a register driving it directly; ``None`` when no
    # register launches it, or a latch is on the way) — the part of a
    # crossing's ``min_hops`` a flat walk counts inside the block — and,
    # for a pin a transparent latch drives directly, the latch's name and
    # the one clock its ``D`` is launched from. For a proven input, the
    # comb cells between the pin and the chain's first stage.
    comb_hops: int | None = None
    latch: tuple[str, str] | None = None
    # Outputs with comb cells inside (``comb_hops`` > 0): the registers
    # launching them, which a flat run names one CDC-003 each.
    launchers: tuple[str, ...] = ()
    # Inputs only (rtl-buddy-cdc#405): the flops whose reset this pin
    # drives (:class:`~rtl_buddy_cdc.netlist.ResetPinFact`), so the
    # parent's RDC-001 / RDC-003 / RDC-004 / CDC-015 can judge what drives
    # the pin. ``async_only``:
    # the pin reaches flops only through asynchronous controls, so it is
    # no data capture (a flat crossing walk finds none there).
    resets: tuple[ResetPinFact, ...] = ()
    async_only: bool = False
    # Inputs only: the stage a comb cell after a depth-1 first stage
    # feeds — CDC-014's shape, which CDC-001 defers to (rtl-buddy-cdc#405).
    inter_stage: str | None = None
    # Outputs only: what the register driving the pin directly looks like
    # (rtl-buddy-cdc#405) — see :class:`~rtl_buddy_cdc.netlist.DriverFacts`.
    driver: DriverFacts | None = None
    # Inputs only: what the body does with the pin besides a flop capture
    # (rtl-buddy-cdc#408) — see :class:`~rtl_buddy_cdc.netlist.PinFacts`.
    pin: PinFacts | None = None
    # Outputs only: the input pins with a combinational path to the pin
    # (rtl-buddy-cdc#408), so the parent's output-cone walk carries on
    # through the block as a flat walk does.
    feedthrough: tuple[str, ...] = ()

    @property
    def synchronised(self) -> bool:
        """True iff the port is *proven* to pass a synchroniser."""
        return self.user_synchronised or (
            self.sync_depth is not None and self.sync_depth >= 2
        )


@dataclass(frozen=True)
class ModuleAnalysis:
    """The result of analysing one boundary module on its own internals.

    ``clock_roots`` is the set of domains actually assigned to the
    module's internal flops. ``unresolved_flops`` names the internal
    flops that did **not** land on one of the parent's clock roots —
    either no domain at all, or a domain the parent never handed in.

    That second case is the one worth naming. Pin inspection alone can
    only see the clocks arriving on pins it recognises; a flop clocked
    from a port no classifier accepts (``strobe``, ``go``, a bare enable
    name) resolves *inside* the module to that port's own name, which is
    not a declared clock anywhere. ``are_async`` then answers False for
    every pair involving it, so its crossings are filtered out and
    disappear — the exact silent drop #253/#259 spent two rounds closing,
    reached from a direction pin inspection cannot see. Detecting it is
    only possible with the internals in hand, and the answer is to
    decline the module loudly.

    ``violations`` are the module-internal findings, ready to be lifted
    into the parent's report once per module type. ``crossings`` is the
    internal async-crossing count, kept for stats / diagnostics.
    ``depth_cuts`` are the asynchronous paths a ``--crossing-depth`` cap
    stopped inside the module (rtl-buddy-cdc#347), lifted with the
    findings so a cap is reported wherever it bites.
    """

    module: str
    clock_roots: frozenset[str] = frozenset()
    unresolved_flops: tuple[str, ...] = ()
    ports: dict[str, PortFacts] = field(default_factory=dict)
    reconvergent_inputs: frozenset[frozenset[str]] = frozenset()
    violations: tuple[Violation, ...] = ()
    crossings: int = 0
    depth_cuts: tuple[DepthCut, ...] = ()

    @property
    def resolved(self) -> bool:
        """True iff every internal flop landed on a parent clock root.

        The gate on summarising anything: a flop the pass cannot place in
        a domain the parent knows means the crossings that flop takes part
        in are invisible to BOTH sides, so claiming coverage for the
        module would be the silent drop, not the fix.
        """
        return not self.unresolved_flops


def derive_sub_spec(
    spec: ClockSpec,
    clock_pin_roots: dict[str, str | None],
) -> ClockSpec:
    """Project the parent's SDC onto a subtree's own port namespace.

    Inside the subtree, ``trace_clock_root`` stops at the module's clock
    *pin* (``clk`` / ``wr_clk`` / …), not at the parent's clock port. The
    derived spec re-declares each parent clock **root** as a clock whose
    port list is the subtree pin(s) it arrives on, so ``clock_for_port``
    normalises the internal trace back to the parent-canonical clock name
    and every downstream consumer — ``assign_domains``, the crossing
    walk, and the rules that key off declared-clock identity — agrees
    with the parent's view of what the domains are.

    Two deliberate omissions:

    - **Port typings** (``port_clock``) are dropped. A subtree input is a
      hierarchy pin driven by the parent; the parent's boundary-sink
      crossing is what reports data entering it. Re-typing it here would
      double-report, and synthesising ``<unconstrained>`` sentinels for
      the untyped rest would flood CDC-011 (which is excluded for the
      same reason — see :data:`INTERNAL_RULE_EXCLUSIONS`).
    - **Generated-clock pin targets** (``pin_clocks``) are dropped: their
      paths are parent-relative (``u_inst/u_sub/pin``) and do not name a
      net inside the subtree. A ``create_generated_clock`` declared on a
      pin *inside* the subtree therefore leaves its consumers
      domain-unknown, which :attr:`ModuleAnalysis.resolved` turns into a
      decline rather than a silent coverage hole.

    Clock *definitions* (periods, generated→master chains) and the async
    / exclusive / false-path partitions are copied verbatim, so
    ``are_async`` gives identical answers on both sides of the boundary.
    """
    ports_by_root: dict[str, list[str]] = {}
    for pin, root in clock_pin_roots.items():
        if root:
            ports_by_root.setdefault(root, []).append(pin)

    sub = ClockSpec()
    for name, clk in spec.clocks.items():
        # Strip the parent's port list: only the subtree pins we traced
        # may resolve through ``clock_for_port`` here, so a subtree data
        # port that happens to share a name with a parent clock port is
        # never mistaken for a clock.
        sub.clocks[name] = Clock(
            name=clk.name,
            period=clk.period,
            ports=tuple(sorted(ports_by_root.get(name, ()))),
            master=clk.master,
            is_generated=clk.is_generated,
        )
    for root, pins in ports_by_root.items():
        if root not in sub.clocks:
            # A traced root the SDC never named as a clock (e.g. a clock
            # port the parent resolved by name only). Declare it so the
            # subtree's flops still land in a named domain.
            sub.clocks[root] = Clock(name=root, period=0.0, ports=tuple(sorted(pins)))
    sub.async_groups = [[set(g) for g in stmt] for stmt in spec.async_groups]
    sub.exclusive_groups = [[set(g) for g in stmt] for stmt in spec.exclusive_groups]
    sub.false_path_pairs = set(spec.false_path_pairs)
    # A clock-pair ``set_max_delay -datapath_only`` is an async relation
    # like the three above (#325); without it a subtree crossing between
    # such a pair would read as synchronous inside the per-module pass.
    sub.datapath_only_pairs = set(spec.datapath_only_pairs)
    # Multi-clock ports (#330): only the values matter here — a primary
    # clock name and the clocks co-located with it — so ``are_async``
    # expands a subtree domain exactly as it does the parent's.
    sub.multi_clock_ports = dict(spec.multi_clock_ports)
    return sub


def analyse_module(
    sub: Module,
    clock_pin_roots: dict[str, str | None],
    spec: ClockSpec,
    *,
    max_depth: int = 16,
    required_depth: int = 2,
    sync_primitives: frozenset[str] = frozenset(),
    crossing_depth: int | None = None,
) -> ModuleAnalysis | None:
    """Run the ordinary CDC pipeline over ``sub`` in isolation.

    ``clock_pin_roots`` maps the subtree's traced clock **pin** names to
    the parent clock roots driving them — the instance clock context
    :func:`~rtl_buddy_cdc.hierarchy.compose_boundaries` already computes
    and caches on. That mapping is the only thing the parent contributes:
    everything else is a property of the module, which is what makes the
    result cacheable per ``(module type, clock context)`` and analysed
    **once** no matter how many times the module is instantiated.

    Returns ``None`` for a module with no cells — a stub blackbox, where
    there is nothing to analyse and the pre-#261 behaviour must stand.
    """
    if not sub.cells and not _wired_through(sub):
        return None

    sub_spec = derive_sub_spec(spec, clock_pin_roots)
    # The derived spec's clock inputs are threaded into every domain view:
    # inside the subtree a flop's clock traces back to the module's *pin*
    # (``clk``), and only the derived spec knows that pin carries the
    # parent's ``clk_d``. The rule context (here and the one ``run_all``
    # builds below) takes them from ``sub_spec`` itself, the crossing walk
    # from the same ``clock_root_inputs()`` bundle, so the two cannot
    # disagree about a domain name at module scope (rtl-buddy-cdc#358).
    ctx = _build_context(sub, sub_spec, max_depth=max_depth)
    domains: dict[str, str | None] = dict(ctx.domains)

    # A flop is resolved only when its domain is one of the roots the
    # PARENT handed in. ``None`` (untraceable clock) and a domain that is
    # merely the name of an unrecognised module port both fail: neither is
    # a clock any ``are_async`` answer can be trusted about.
    known_roots = {r for r in clock_pin_roots.values() if r is not None}
    unresolved = tuple(
        sorted(n for n, c in domains.items() if c is None or c not in known_roots)
    )
    clock_roots = frozenset(c for c in domains.values() if c is not None)

    cuts: list[DepthCut] = []
    crossings = find_crossings(
        sub,
        crossing_depth,
        max_depth=max_depth,
        on_depth_cut=cuts.append,
        **sub_spec.clock_root_inputs(),
    )
    async_crossings = filter_async(crossings, sub_spec)
    violations = [
        v
        for v in run_all(
            sub,
            async_crossings,
            sub_spec,
            required_depth=required_depth,
            max_depth=max_depth,
            sync_primitives=sync_primitives,
        )
        if v.rule_id not in INTERNAL_RULE_EXCLUSIONS
    ]

    flop_by_name = {f.cell.name: f for f in ctx.flops}
    ports: dict[str, PortFacts] = {}
    for port in sub.ports.values():
        if port.name in clock_pin_roots:
            continue
        if port.direction in ("input", "inout"):
            ports[port.name] = _input_facts(sub, port.name, ctx, domains, flop_by_name)
        else:
            ports[port.name] = _output_facts(
                sub, port.name, ctx, domains, flop_by_name, clock_pin_roots
            )

    _attach_resets(sub, ports, ctx, domains)
    _attach_pin_facts(sub, ports, ctx, sub_spec, domains, max_depth)
    reconvergent = _reconvergent_input_ports(sub, ports, ctx, domains, flop_by_name)

    return ModuleAnalysis(
        module=sub.name,
        clock_roots=clock_roots,
        unresolved_flops=unresolved,
        ports=ports,
        reconvergent_inputs=reconvergent,
        violations=tuple(violations),
        crossings=len(async_crossings),
        depth_cuts=tuple(filter_async_cuts(cuts, sub_spec)),
    )


def _wired_through(sub: Module) -> bool:
    """A body with no cells whose output ports share a net with an input
    (``assign q = clk;``): a body to analyse, unlike a stub's, whose
    ports are all distinct nets (rtl-buddy-cdc#408)."""
    ins = {
        b
        for p in sub.ports.values()
        if p.direction in ("input", "inout")
        for b in p.bits
        if isinstance(b, int)
    }
    return any(
        b in ins
        for p in sub.ports.values()
        if p.direction == "output"
        for b in p.bits
        if isinstance(b, int)
    )


# --- port facts -------------------------------------------------------------


def _attach_resets(
    sub: Module,
    ports: dict[str, PortFacts],
    ctx: _RuleContext,
    domains: dict[str, str | None],
) -> None:
    """Fill :attr:`PortFacts.resets` / ``async_only`` on the input ports.

    Walks back from every flop's reset pins — each asynchronous control,
    and the synchronous reset as ``opt_dff`` folds it (the pins RDC-001 /
    RDC-003 read) — through comb cells to the module's input ports.
    """
    exempt = _deasserting_reset_sync_members(ctx)
    found: dict[str, list[ResetPinFact]] = {}
    async_pins: dict[str, set[Bit]] = {}
    for f in ctx.flops:
        clock = domains.get(f.cell.name)
        if clock is None:
            continue
        rd = ctx.reset_domains.get(f.cell.name)
        reached: list[tuple[str, tuple[Bit, ...], bool, bool]] = [
            (
                _ASYNC_PIN_LABEL[ctl.pin.role],
                ctl.bits,
                ctl.source.source == "comb",
                _live_mux_driver(sub, ctl.bits[0], ctx.bit_drivers) is not None,
            )
            for ctl in (rd.controls if rd is not None else ())
            if ctl.bits
        ]
        async_bits = {b for pin in flop_async_controls(f.cell) for b in pin.bits}
        srst = flop_reset_bits(f.cell, "srst") + _folded_srst_selects(
            f, sub, ctx.bit_drivers, ctx.reader_counts
        )
        if srst:
            reached.append(("SRST", srst, False, False))
        # RDC-002's subject: the main async reset, wired from a pin.
        direct = (
            rd.reset.name
            if rd is not None
            and rd.reset is not None
            and rd.reset.type == "async"
            and rd.reset.source == "port"
            else None
        )
        polarity = (
            ("1" if rd.reset.polarity == "high" else "0")
            if direct is not None and rd is not None and rd.reset is not None
            else None
        )
        for label, bits, comb, mux in reached:
            _flops, in_ports = _backward_fanin(sub, bits, ctx.bit_drivers)
            for name in in_ports:
                if name not in ports:
                    continue
                entry = ResetPinFact(
                    label,
                    f.cell.name,
                    clock,
                    exempt=f.cell.name in exempt,
                    comb=comb,
                    reset_sync=f.cell.name in ctx.reset_synchronizers,
                    mux=mux,
                    polarity=polarity if name == direct and label != "SRST" else None,
                )
                if entry not in found.setdefault(name, []):
                    found[name].append(entry)
        for b in async_bits:
            if isinstance(b, int):
                async_pins.setdefault(f.cell.name, set()).add(b)
    for name, entries in found.items():
        facts = ports[name]
        only = facts.direction == "input" and not _reaches_capture(
            sub, sub.ports[name].bits, ctx, async_pins
        )
        ports[name] = replace(
            facts,
            resets=tuple(sorted(entries, key=lambda e: (e.flop, e.pin))),
            async_only=only,
        )


def _attach_pin_facts(
    sub: Module,
    ports: dict[str, PortFacts],
    ctx: _RuleContext,
    spec: ClockSpec,
    domains: dict[str, str | None],
    max_depth: int,
) -> None:
    """Fill :attr:`PortFacts.pin` on the data input ports
    (rtl-buddy-cdc#408): see :class:`~rtl_buddy_cdc.netlist.PinFacts`.

    The clock controls are CDC-010's own reading of the body — the
    clock-network cells and their control pins
    (:func:`~rtl_buddy_cdc.rules._clock_control_cells`, with the clock
    walk's controls taken from one declared clock up, since the body's
    one clock pin is all it has), each control traced back through comb
    cells to the input pins, and the cell's clock-input domains.
    """
    _combines, traced = clock_network_events(
        sub, max_depth=max_depth, min_clocks=1, **spec.clock_root_inputs()
    )
    controls: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for cell, ctrl_ports in _clock_control_cells(sub, ctx, traced):
        bits = tuple(
            b
            for p in sorted(ctrl_ports)
            for b in cell.connections.get(p, ())
            if isinstance(b, int)
        )
        _flops, in_ports = _backward_fanin(sub, bits, ctx.bit_drivers)
        if not in_ports:
            continue
        doms = tuple(sorted(_clock_input_domains_for(sub, cell, ctx, spec, ctrl_ports)))
        for name in in_ports:
            if name in ports:
                controls.setdefault(name, []).append((cell.name, doms))
    out_bits: set[Bit] = {
        b
        for p in sub.ports.values()
        if p.direction in ("output", "inout")
        for b in p.bits
        if isinstance(b, int)
    }
    for name, facts in list(ports.items()):
        if facts.direction not in ("input", "inout"):
            continue
        pin_bits = sub.ports[name].bits
        captured, latch_feeds, heads = _pin_reach(sub, pin_bits, ctx, domains, out_bits)
        gated = (
            len(pin_bits) > 1
            and bool(heads)
            and all(_gated_head(sub, ctx, domains, h, len(pin_bits)) for h in heads)
        )
        pin = PinFacts(
            no_capture=not captured and not facts.resets,
            clock_controls=tuple(sorted(set(controls.get(name, ())))),
            latch_feeds=latch_feeds,
            gated=gated,
        )
        if pin != PinFacts():
            ports[name] = replace(facts, pin=pin)


def _pin_reach(
    sub: Module,
    bits: tuple[Bit, ...],
    ctx: _RuleContext,
    domains: dict[str, str | None],
    out_bits: set[Bit],
) -> tuple[bool, tuple[tuple[str, str, str], ...], set[str]]:
    """Forward from an input pin, as the flat crossing walk goes:
    through comb cells, stopping at flops (the captures), memories and
    output ports (counted as captures too), and transparent latches (the
    walk ends there; CDC-017 owns the shape). A clock pin is not a
    consumer, so a clock mux select or gate enable reaches nothing.

    Returns ``(captured, latch_feeds, heads)``: whether anything was
    reached, the ``(latch, flop, clock)`` of each latch reached on its
    ``D`` whose ``Q`` a single-bit flop reads, and the flops reached.
    """
    captured = False
    feeds: set[tuple[str, str, str]] = set()
    heads: set[str] = set()
    seen: set[Bit] = set()
    expanded: set[str] = set()
    frontier: list[Bit] = [b for b in bits if isinstance(b, int)]
    while frontier:
        nxt: list[Bit] = []
        for b in frontier:
            if b in seen:
                continue
            seen.add(b)
            if b in out_bits:
                captured = True
            for cell_name, pin, _idx in ctx.bit_consumers.get(b, ()):
                cell = sub.cells[cell_name]
                if is_ff_cell(cell.type):
                    captured = True
                    heads.add(cell_name)
                    continue
                if is_latch_cell(cell.type):
                    if pin != "D":
                        continue
                    for q in cell.connections.get("Q", ()):
                        if not isinstance(q, int):
                            continue
                        dst = ctx.d_bit_to_single_bit_flop.get(q)
                        clock = domains.get(dst.cell.name) if dst is not None else None
                        if dst is not None and clock is not None:
                            feeds.add((cell_name, dst.cell.name, clock))
                    continue
                if cell.type.startswith("$mem"):
                    captured = True
                    continue
                if cell_name in expanded:
                    continue
                expanded.add(cell_name)
                for p, obits in cell.connections.items():
                    if cell.is_output_pin(p):
                        nxt.extend(x for x in obits if isinstance(x, int))
        frontier = nxt
    return captured, tuple(sorted(feeds)), heads


def _gated_head(
    sub: Module,
    ctx: _RuleContext,
    domains: dict[str, str | None],
    head: str,
    width: int,
) -> bool:
    """Does flop ``head`` load under an enable / hold mux driven only from
    its own domain — the destination half of CDC-004's gated-bus test?"""
    flop = next((f for f in ctx.flops if f.cell.name == head), None)
    clock = domains.get(head)
    if flop is None or clock is None:
        return False
    probe = Crossing(
        src_clock="", dst_flop=flop, dst_clock=clock, min_hops=0, width=width
    )
    return (
        _gated_capture_fanin(
            sub, probe, domains, ctx.bit_drivers, reader_counts=ctx.reader_counts
        )
        is not None
    )


def _reaches_capture(
    sub: Module,
    bits: tuple[Bit, ...],
    ctx: _RuleContext,
    async_pins: dict[str, set[Bit]],
) -> bool:
    """True iff ``bits`` reach a flop on a pin other than an async control
    (``D``, an enable, a synchronous reset): a data capture."""
    seen: set[Bit] = set()
    frontier: list[Bit] = [b for b in bits if isinstance(b, int)]
    while frontier:
        nxt: list[Bit] = []
        for b in frontier:
            if b in seen:
                continue
            seen.add(b)
            for cell_name, pin, _idx in ctx.bit_consumers.get(b, ()):
                cell = sub.cells[cell_name]
                if is_ff_cell(cell.type):
                    if b not in async_pins.get(cell_name, ()) or pin == "D":
                        return True
                    continue
                for p, out in cell.connections.items():
                    if cell.is_output_pin(p):
                        nxt.extend(x for x in out if isinstance(x, int))
        frontier = nxt
    return False


def _capture_flops(sub: Module, bits: tuple[Bit, ...], ctx: _RuleContext) -> set[str]:
    """Names of the flops a port's bits reach *first* (no flop crossed)."""
    reached = _forward_reachable_cells(sub, bits, ctx.bit_consumers)
    return {n for n in reached if is_ff_cell(sub.cells[n].type)}


def _single_domain(domains_seen: set[str | None]) -> tuple[str | None, bool]:
    """Collapse an observed domain set to ``(clock, ambiguous)``."""
    known = {d for d in domains_seen if d is not None}
    if len(known) == 1 and len(domains_seen) == 1:
        return next(iter(known)), False
    if len(domains_seen) <= 1:
        # Empty (nothing sequential) or a lone unresolved domain.
        return None, False
    return None, True


def _input_facts(
    sub: Module,
    name: str,
    ctx: _RuleContext,
    domains: dict[str, str | None],
    flop_by_name: dict[str, Flop],
) -> PortFacts:
    """Capture domain + synchroniser proof for one data input port."""
    port = sub.ports[name]
    heads = _capture_flops(sub, port.bits, ctx)
    clock, ambiguous = _single_domain({domains.get(h) for h in heads})
    proof = _prove_input_synchroniser(sub, name, ctx, domains, flop_by_name)
    return PortFacts(
        port=name,
        direction=port.direction,
        width=len(port.bits),
        clock=clock,
        ambiguous=ambiguous,
        sync_depth=proof.depth,
        user_synchronised=proof.user,
        chain_exit=proof.exit,
        chain_stages=proof.stages,
        comb_hops=proof.comb_hops,
        inter_stage=proof.inter_stage,
    )


@dataclass(frozen=True)
class _InputProof:
    """What :func:`_prove_input_synchroniser` established about one input.

    ``depth`` ``None`` means the proof failed and everything else is
    empty. ``comb_hops`` is the comb cells between the pin and the first
    stage; ``inter_stage`` the stage a comb cell after a depth-1 first
    stage feeds (CDC-014's shape). See :class:`PortFacts` for the rest.
    """

    depth: int | None = None
    user: bool = False
    exit: tuple[str, int] | None = None
    stages: tuple[tuple[str, int], ...] = ()
    comb_hops: int | None = None
    inter_stage: str | None = None


_NO_PROOF = _InputProof()


def _prove_input_synchroniser(
    sub: Module,
    name: str,
    ctx: _RuleContext,
    domains: dict[str, str | None],
    flop_by_name: dict[str, Flop],
) -> _InputProof:
    """Prove (or refuse to prove) that ``name`` lands on a synchroniser.

    Returns the :class:`_InputProof`; an empty one whenever any step of
    the proof fails. The refusals are the ones §4.9 demands —
    each is a way a real design defeats the "the IP synchronises it"
    claim, and every one of them falls back to the conservative
    over-reporting default:

    - **width > 1** — a bus through per-bit 2FF chains is a data-coherency
      bug in its own right (CDC-004's territory: gray coding or gating).
      Claiming the boundary handles it would suppress the crossing the
      bus rules must see, so multi-bit ports are never proven.
    - **tie-off / unconnected** — no reader, nothing to prove.
    - **comb bypass or extra load** — more than one reader of the port
      bit means the raw, unsynchronised value is used somewhere else too.
      The pin may reach the first stage through single-output comb cells
      (rtl-buddy-cdc#405), each read only on the way: the chain is still
      proven, and the cells are published as ``comb_hops`` so CDC-003 /
      CDC-006 report them as a flat run does. The first stage's own
      synchronous-reset mux is part of the flop, not a hop.
    - **not a flop ``D`` pin** — landing on an enable / reset pin is not
      a synchroniser first stage.
    - **feed-through** — the port bit is also an output-port bit, so the
      unsynchronised value leaves the block directly.
    - **domain-unknown first stage** — nothing is proven about a flop we
      could not place in a domain.
    """
    port = sub.ports[name]
    if len(port.bits) != 1:
        return _NO_PROOF
    bit = port.bits[0]
    if not isinstance(bit, int):
        return _NO_PROOF
    for other in sub.ports.values():
        if other.direction in ("output", "inout") and bit in other.bits:
            return _NO_PROOF
    found = _first_stage(sub, bit, ctx)
    if found is None:
        return _NO_PROOF
    cell_name, hops = found
    head = flop_by_name[cell_name]
    if len(head.d) != 1:
        return _NO_PROOF
    clock = domains.get(cell_name)
    if clock is None:
        return _NO_PROOF
    depth = _sync_chain_depth(
        sub,
        head,
        clock,
        domains,
        ctx.reader_counts,
        d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        bit_drivers=ctx.bit_drivers,
        srst_mux_data_bit_to_single_bit_flop=ctx.srst_mux_data_bit_to_single_bit_flop,
    )
    exit_, stages = _chain_shape(sub, head, clock, depth, ctx)
    nxt = _chain_has_inter_stage_comb(head, ctx) if depth < 2 else None
    return _InputProof(
        depth=depth,
        user=cell_name in ctx.user_syncs,
        exit=exit_,
        stages=stages,
        comb_hops=hops,
        inter_stage=nxt.cell.name if nxt is not None else None,
    )


def _first_stage(sub: Module, bit: Bit, ctx: _RuleContext) -> tuple[str, int] | None:
    """The flop whose ``D`` the pin bit reaches, and the comb cells on the way.

    Each bit on the way has exactly one reader (a module output port
    counts as one), each cell one output bit, and no latch; the walk
    stops at the first flop, which must take the bit on ``D`` (directly
    or through its own synchronous-reset mux).
    """
    hops = 0
    while hops <= _COMB_HOP_LIMIT:
        if ctx.reader_counts.get(bit, 0) != 1:
            return None
        behind = ctx.srst_mux_data_bit_to_single_bit_flop.get(bit)
        if behind is not None:
            return behind.cell.name, hops
        readers = ctx.bit_consumers.get(bit, ())
        if len(readers) != 1:
            return None
        cell_name, pin, _idx = readers[0]
        cell = sub.cells[cell_name]
        if is_ff_cell(cell.type):
            return (cell_name, hops) if pin == "D" else None
        if is_latch_cell(cell.type):
            return None
        outs = [
            b
            for p, bits in cell.connections.items()
            if cell.is_output_pin(p)
            for b in bits
        ]
        if len(outs) != 1 or not isinstance(outs[0], int):
            return None
        bit = outs[0]
        hops += 1
    return None


# ``(chain_exit, chain_stages)`` of a proven input chain.
_ChainShape = tuple[tuple[str, int] | None, tuple[tuple[str, int], ...]]
_NO_SHAPE: _ChainShape = (None, ())


def _chain_shape(
    sub: Module, head: Flop, clock: str, depth: int, ctx: _RuleContext
) -> _ChainShape:
    """The chain's stages and the output-port bit it ends on, if it does.

    The chain's last stage must drive the port directly and have no other
    reader, the condition :func:`_sync_chain_depth` puts on every link, so
    the parent extends the chain exactly as a flat walk would. A packed
    shift-register head (the whole chain in one cell) has no stages to
    list and no exit.
    """
    chain = _sync_chain_flops(
        sub,
        head,
        clock,
        domains=ctx.domains,
        reader_counts=ctx.reader_counts,
        d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
        srst_mux_data_bit_to_single_bit_flop=ctx.srst_mux_data_bit_to_single_bit_flop,
    )
    if len(chain) != depth:
        return _NO_SHAPE
    stages = tuple((f.cell.name, flop_clk_polarity(f.cell)) for f in chain)
    tail = chain[-1]
    if len(tail.q) != 1 or not isinstance(tail.q[0], int):
        return None, stages
    bit = tail.q[0]
    if ctx.reader_counts.get(bit, 0) != 1 or ctx.bit_consumers.get(bit):
        return None, stages
    for port in sub.ports.values():
        if port.direction == "output" and bit in port.bits:
            return (port.name, port.bits.index(bit)), stages
    return None, stages


def _output_facts(
    sub: Module,
    name: str,
    ctx: _RuleContext,
    domains: dict[str, str | None],
    flop_by_name: dict[str, Flop],
    clock_pins: Iterable[str] = (),
) -> PortFacts:
    """Launch domain + synchroniser proof for one output port."""
    port = sub.ports[name]
    undriven: set[Bit] = set()
    launchers = _backward_flop_fanin(sub, port.bits, ctx.bit_drivers, undriven=undriven)
    clock, ambiguous = _single_domain({domains.get(f) for f in launchers})
    user = _prove_output_synchroniser(sub, name, ctx, domains, flop_by_name)
    clock_out = tuple(
        sorted(
            c
            for c in clock_pins
            if c in sub.ports and any(b in undriven for b in sub.ports[c].bits)
        )
    )
    pin = PinFacts(clock_out=clock_out) if clock_out else None
    hops: int | None = None
    latch: tuple[str, str] | None = None
    if len(port.bits) == 1 and isinstance(port.bits[0], int):
        hops = _launch_comb_hops(sub, port.bits[0], ctx)
        latch = _driving_latch(sub, port.bits[0], ctx, domains)
    return PortFacts(
        driver=_driver_facts(sub, port.bits, ctx, flop_by_name),
        port=name,
        direction=port.direction,
        width=len(port.bits),
        clock=clock,
        ambiguous=ambiguous,
        sync_depth=2 if user else None,
        user_synchronised=user,
        comb_hops=hops,
        latch=latch,
        launchers=(
            tuple(sorted(n for n in launchers if n in flop_by_name)) if hops else ()
        ),
        feedthrough=tuple(
            sorted(
                p.name
                for p in sub.ports.values()
                if p.direction in ("input", "inout")
                and p.name not in clock_pins
                and any(b in undriven for b in p.bits)
            )
        ),
        pin=pin,
    )


def _driver_facts(
    sub: Module,
    bits: tuple[Bit, ...],
    ctx: _RuleContext,
    flop_by_name: dict[str, Flop],
) -> DriverFacts | None:
    """:class:`DriverFacts` when one register's ``Q`` drives ``bits``
    directly, bit for bit and whole."""
    drvs = {ctx.bit_drivers.get(b) for b in bits}
    names = {d[0] for d in drvs if d is not None and d[1] == "Q"}
    if len(names) != 1 or None in drvs:
        return None
    (name,) = names
    flop = flop_by_name.get(name)
    if flop is None or tuple(flop.q) != tuple(bits):
        return None
    marks = frozenset(
        mark
        for mark, tagged in (
            ("gray", ctx.user_grays),
            ("static", ctx.user_statics),
            ("handshake", ctx.user_handshakes),
            ("sync", ctx.user_syncs),
        )
        if name in tagged
    )
    if len(bits) != 1:
        hold_domains: frozenset[str] = frozenset()
        hold_pins: tuple[str, ...] = ()
        control = _hold_control_bits(flop, sub, ctx.bit_drivers)
        if control:
            reached, undriven = _ack_walk(
                sub,
                control,
                ctx.domains.get(name),
                ctx.domains,
                ctx.bit_drivers,
                flop_by_name,
                seen={name},
                excluded=_ack_walk_excluded(ctx),
            )
            hold_domains = frozenset(reached)
            hold_pins = tuple(
                sorted(
                    p.name
                    for p in sub.ports.values()
                    if p.direction in ("input", "inout")
                    and any(b in undriven for b in p.bits)
                )
            )
        return DriverFacts(
            width=len(bits),
            marks=marks,
            gray=_is_gray_encoded_source(sub, flop, ctx.bit_drivers),
            hold_domains=hold_domains,
            hold_pins=hold_pins,
        )
    reset_value = _producer_async_reset_value(flop.cell)
    if reset_value is not None and name in ctx.reset_synchronizers:
        reset_value = None
    toggle = False
    d_bits = flop.cell.connections.get("D", ())
    if len(d_bits) == 1 and name not in ctx.user_handshakes:
        clock = ctx.domains.get(name)
        leg = _own_srst_data_leg(
            sub, flop, clock, ctx.domains, ctx.bit_drivers, ctx.reader_counts
        )
        toggle = (
            classify_toggle_d_pin(
                leg[0] if leg is not None else d_bits[0],
                bits[0],
                sub,
                ctx.bit_drivers,
                flop=flop.cell,
            )
            == "toggle"
        )
    pulse = len(d_bits) == 1 and (
        classify_d_pin_shape(
            d_bits[0], ctx.domains.get(name) or "", sub, ctx.bit_drivers, ctx.domains
        )
        == "pulse"
    )
    return DriverFacts(
        width=1,
        marks=marks,
        reset_value=reset_value,
        toggle=toggle,
        pulse=pulse,
        decoder=_decoder_group(sub, flop, ctx, {}),
    )


def _launch_comb_hops(sub: Module, bit: Bit, ctx: _RuleContext) -> int | None:
    """Fewest combinational cells between a register ``Q`` and ``bit``.

    Breadth-first backward over the drivers, one level per cell, as the
    crossing walk counts ``min_hops``. ``None`` when no register is
    reached, or when the nearest storage on some path is a transparent
    latch (the crossing walk stops there; CDC-017 owns that shape).
    """
    frontier: list[Bit] = [bit]
    seen: set[Bit] = set()
    for hops in range(_COMB_HOP_LIMIT):
        nxt: list[Bit] = []
        for b in frontier:
            if b in seen:
                continue
            seen.add(b)
            drv = ctx.bit_drivers.get(b)
            if drv is None:
                continue
            cell = sub.cells[drv[0]]
            if is_latch_cell(cell.type):
                return None
            if is_ff_cell(cell.type):
                return hops
            for pin, bits in cell.connections.items():
                if pin == "CLK" or cell.is_output_pin(pin):
                    continue
                nxt.extend(x for x in bits if isinstance(x, int))
        if not nxt:
            return None
        frontier = nxt
    return None


# Comb levels :func:`_launch_comb_hops` looks through before giving up —
# the crossing walk's own default depth budget.
_COMB_HOP_LIMIT = 64


def _driving_latch(
    sub: Module, bit: Bit, ctx: _RuleContext, domains: dict[str, str | None]
) -> tuple[str, str] | None:
    """``(latch, clock)`` when a transparent latch's ``Q`` drives ``bit``
    and the registers behind its ``D`` all launch in that one clock."""
    drv = ctx.bit_drivers.get(bit)
    if drv is None or not is_latch_cell(sub.cells[drv[0]].type):
        return None
    latch = sub.cells[drv[0]]
    d_bits = tuple(b for b in latch.connections.get("D", ()) if isinstance(b, int))
    clocks = {
        domains.get(f) for f in _backward_flop_fanin(sub, d_bits, ctx.bit_drivers)
    }
    if len(clocks) != 1 or None in clocks:
        return None
    (clock,) = clocks
    assert clock is not None
    return latch.name, clock


def _prove_output_synchroniser(
    sub: Module,
    name: str,
    ctx: _RuleContext,
    domains: dict[str, str | None],
    flop_by_name: dict[str, Flop],
) -> bool:
    """True iff **every** register→port path on ``name`` passes a chain.

    §4.9's documented meaning of an output-side ``synchronised``. The
    proof requires each port bit to be driven *directly* by a flop ``Q``
    (a comb cell between the last register and the port defeats it) and
    that flop to be either the tail of a ≥2-stage same-domain chain whose
    inter-stage net has exactly one reader, or explicitly tagged with one
    of :data:`~rtl_buddy_cdc.rules.USER_SYNC_ATTRS`.
    """
    port = sub.ports[name]
    if not port.bits:
        return False
    for bit in port.bits:
        drv = ctx.bit_drivers.get(bit)
        if drv is None or not is_ff_cell(sub.cells[drv[0]].type):
            return False
        tail = flop_by_name[drv[0]]
        if drv[0] in ctx.user_syncs:
            continue
        if len(tail.d) != 1 or not isinstance(tail.d[0], int):
            return False
        if ctx.reader_counts.get(tail.d[0], 0) != 1:
            return False
        prev = ctx.bit_drivers.get(tail.d[0])
        if prev is None or not is_ff_cell(sub.cells[prev[0]].type):
            return False
        if domains.get(prev[0]) != domains.get(drv[0]):
            return False
    return True


def _reconvergent_input_ports(
    sub: Module,
    ports: dict[str, PortFacts],
    ctx: _RuleContext,
    domains: dict[str, str | None],
    flop_by_name: dict[str, Flop],
) -> frozenset[frozenset[str]]:
    """Input-port pairs whose captured values recombine inside ``sub``.

    The boundary analogue of CDC-005's phase-2 reconvergence filter: walk
    forward from each input port's capture chain **terminal** and record
    the pairs whose downstream cones intersect. Chain-internal cells are
    excluded so a chain never reconverges on itself.

    The parent re-raises CDC-005 for a recorded pair only when both of
    its parent-side sources are the *same* flop crossing into the block
    (the shape the flat rule fires on) — this set answers "would these
    two ports reconverge if they did", which is precisely the fact the
    star-collapse severs and #259 had to decline over.
    """
    cones: dict[str, set[str]] = {}
    for name, facts in ports.items():
        if facts.direction not in ("input", "inout") or facts.clock is None:
            continue
        heads = _capture_flops(sub, sub.ports[name].bits, ctx)
        internal: set[str] = set()
        starts: list[Bit] = []
        for head_name in sorted(heads):
            # ``facts.clock`` is set, so every capture flop resolved to
            # that one domain (see :func:`_single_domain`).
            chain = _sync_chain_flops(
                sub,
                flop_by_name[head_name],
                facts.clock,
                domains,
                ctx.reader_counts,
                ctx.d_bit_to_single_bit_flop,
                ctx.srst_mux_data_bit_to_single_bit_flop,
            )
            internal.update(f.cell.name for f in chain)
            starts.extend(chain[-1].q)
        cones[name] = (
            _forward_reachable_cells(sub, tuple(starts), ctx.bit_consumers) - internal
        )

    out: set[frozenset[str]] = set()
    names = sorted(cones)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            if cones[a] & cones[b]:
                out.add(frozenset({a, b}))
    return frozenset(out)
