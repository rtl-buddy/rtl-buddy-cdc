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

from dataclasses import dataclass, field

from rtl_buddy_cdc.domain import filter_async, find_crossings
from rtl_buddy_cdc.flops import Flop, is_ff_cell
from rtl_buddy_cdc.netlist import Bit, Module
from rtl_buddy_cdc.rules import (
    Violation,
    _backward_flop_fanin,
    _build_context,
    _forward_reachable_cells,
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
#
# None of the three goes silent overall: what they are really about — data
# (or a reset) entering the block from a foreign domain — is reported by
# the PARENT as the boundary-sink crossing at that very port, where
# CDC-001 / CDC-003 / CDC-004 apply. And comb logic between the port and
# the first internal flop *defeats* the synchroniser proof
# (:func:`_prove_input_synchroniser`), so the boundary falls back to
# firing CDC-001 rather than staying quiet. The residual is a rule-id
# difference against a flat run, never a dropped hazard.
#
# Everything else in the pack runs. The derived sub-spec (see
# :func:`derive_sub_spec`) declares the module's traced clock pins as real
# clocks, so the rules that key off declared-clock identity (CDC-008 /
# CDC-010 / CDC-021 / CDC-023) see a coherent SDC rather than an
# undeclared-everything one.
INTERNAL_RULE_EXCLUSIONS: frozenset[str] = frozenset({"CDC-006", "CDC-011", "RDC-008"})


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
    """

    module: str
    clock_roots: frozenset[str] = frozenset()
    unresolved_flops: tuple[str, ...] = ()
    ports: dict[str, PortFacts] = field(default_factory=dict)
    reconvergent_inputs: frozenset[frozenset[str]] = frozenset()
    violations: tuple[Violation, ...] = ()
    crossings: int = 0

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
    return sub


def analyse_module(
    sub: Module,
    clock_pin_roots: dict[str, str | None],
    spec: ClockSpec,
    *,
    max_depth: int = 16,
    required_depth: int = 2,
    sync_primitives: frozenset[str] = frozenset(),
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
    if not sub.cells:
        return None

    sub_spec = derive_sub_spec(spec, clock_pin_roots)
    clock_for_port = sub_spec.clock_for_port
    # ``clock_for_port`` is threaded into every domain view: inside the
    # subtree a flop's clock traces back to the module's *pin* (``clk``),
    # and only the derived spec knows that pin carries the parent's
    # ``clk_d``. Without it the rule context and the crossing list would
    # disagree about every domain name at module scope.
    ctx = _build_context(
        sub, sub_spec, max_depth=max_depth, clock_for_port=clock_for_port
    )
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

    crossings = find_crossings(sub, clock_for_port=clock_for_port, max_depth=max_depth)
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
            ports[port.name] = _output_facts(sub, port.name, ctx, domains, flop_by_name)

    reconvergent = _reconvergent_input_ports(sub, ports, ctx, domains, flop_by_name)

    return ModuleAnalysis(
        module=sub.name,
        clock_roots=clock_roots,
        unresolved_flops=unresolved,
        ports=ports,
        reconvergent_inputs=reconvergent,
        violations=tuple(violations),
        crossings=len(async_crossings),
    )


# --- port facts -------------------------------------------------------------


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
    depth, user = _prove_input_synchroniser(sub, name, ctx, domains, flop_by_name)
    return PortFacts(
        port=name,
        direction=port.direction,
        width=len(port.bits),
        clock=clock,
        ambiguous=ambiguous,
        sync_depth=depth,
        user_synchronised=user,
    )


def _prove_input_synchroniser(
    sub: Module,
    name: str,
    ctx: _RuleContext,
    domains: dict[str, str | None],
    flop_by_name: dict[str, Flop],
) -> tuple[int | None, bool]:
    """Prove (or refuse to prove) that ``name`` lands on a synchroniser.

    Returns ``(chain_depth, user_tagged)``; ``(None, False)`` whenever any
    step of the proof fails. The refusals are the ones §4.9 demands —
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
    - **not a flop ``D`` pin** — landing on an enable / reset pin is not
      a synchroniser first stage.
    - **feed-through** — the port bit is also an output-port bit, so the
      unsynchronised value leaves the block directly.
    - **domain-unknown first stage** — nothing is proven about a flop we
      could not place in a domain.
    """
    port = sub.ports[name]
    if len(port.bits) != 1:
        return None, False
    bit = port.bits[0]
    if not isinstance(bit, int):
        return None, False
    for other in sub.ports.values():
        if other.direction in ("output", "inout") and bit in other.bits:
            return None, False
    readers = ctx.bit_consumers.get(bit, ())
    if len(readers) != 1:
        return None, False
    cell_name, pin, _idx = readers[0]
    if not is_ff_cell(sub.cells[cell_name].type) or pin != "D":
        return None, False
    head = flop_by_name[cell_name]
    if len(head.d) != 1:
        return None, False
    clock = domains.get(cell_name)
    if clock is None:
        return None, False
    depth = _sync_chain_depth(
        sub,
        head,
        clock,
        domains,
        ctx.reader_counts,
        d_bit_to_single_bit_flop=ctx.d_bit_to_single_bit_flop,
    )
    return depth, cell_name in ctx.user_syncs


def _output_facts(
    sub: Module,
    name: str,
    ctx: _RuleContext,
    domains: dict[str, str | None],
    flop_by_name: dict[str, Flop],
) -> PortFacts:
    """Launch domain + synchroniser proof for one output port."""
    port = sub.ports[name]
    launchers = _backward_flop_fanin(sub, port.bits, ctx.bit_drivers)
    clock, ambiguous = _single_domain({domains.get(f) for f in launchers})
    user = _prove_output_synchroniser(sub, name, ctx, domains, flop_by_name)
    return PortFacts(
        port=name,
        direction=port.direction,
        width=len(port.bits),
        clock=clock,
        ambiguous=ambiguous,
        sync_depth=2 if user else None,
        user_synchronised=user,
    )


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
