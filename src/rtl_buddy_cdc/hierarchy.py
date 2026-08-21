"""Compositional boundary walk (CDC scaling phase 3, #257).

Phases 1/2 made a blackboxed subtree a first-class, loadable boundary
cell (#255) and taught the summariser to abstract a single-clock
subtree to its port boundary (#256). What was still implicit is the
*compositional* contract the epic (#253) is actually about: a block
instantiated N times must be **analysed once**, its boundary summary
cached and re-applied to every instance, so the full flattened graph
is never materialised.

:func:`compose_boundaries` makes that explicit. It walks the parent's
boundary instances and summarises each distinct ``(module type, clock
context)`` exactly once — caching by the pair so identical instances
hit the cache (the analyse-once / perf win), while an instance in a
*different* clock domain gets its own correct summary. The returned
boundary map is keyed **per instance** so
:func:`~rtl_buddy_cdc.domain.find_crossings` re-seeds each instance's
boundary crossings against the domain its parent actually drives. A
:class:`CompositionStats` record *proves* the sharing (``cache_hits``
> 0 whenever an identical-context instance was reused).

Pure orchestration: the per-subtree detection / summarisation lives in
:mod:`rtl_buddy_cdc.abstract`; this module only walks instances and
caches results. The file I/O that loads the blackbox siblings stays in
``cli.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from rtl_buddy_cdc.abstract import (
    _bit_consumers,
    _clock_sink_bits,
    _instance_clocks,
    blackbox_clock_pins_by_module,
    clock_driving_output_ports,
    summarise_subtree,
    summarise_sync_primitive,
)
from rtl_buddy_cdc.compositional import ModuleAnalysis, analyse_module
from rtl_buddy_cdc.domain import Crossing
from rtl_buddy_cdc.netlist import BoundarySummary, Module
from rtl_buddy_cdc.primitives import is_sync_primitive, normalise_type
from rtl_buddy_cdc.rules import Violation
from rtl_buddy_cdc.sdc import ClockSpec

# How many instance names a lifted internal finding names before it
# elides the rest. The point of "analyse once" is one finding per module
# type, not one per instance; naming a handful of instances keeps the
# message actionable on a 50-instance mesh without becoming the wall of
# text the per-instance form would be.
_LIFT_INSTANCE_SAMPLE = 4


@dataclass(frozen=True)
class LiftedAnalysis:
    """One boundary module's internal findings, ready to lift (#261).

    Produced once per ``(module type, clock context)`` — the same key the
    summary cache uses — so a block instantiated N times is analysed and
    reported **once**. ``instances`` names the parent cells the analysis
    covers, which is what the lifted message carries in place of the
    per-instance repetition.
    """

    module: str
    instances: tuple[str, ...]
    analysis: ModuleAnalysis

    def lifted_violations(self) -> list[Violation]:
        """The internal findings re-stamped for the parent's report.

        Each keeps its own rule id, severity and crossing; the message
        gains a prefix naming the module and the instances it stands for,
        and ``cell_name`` is re-anchored on the first instance so the
        finding resolves to a source location in the *parent* and is
        waivable by boundary instance (``waive CDC-001 u_afifo``) — the
        internal cell name is not addressable from up here.
        """
        if not self.instances:
            return []
        shown = list(self.instances[:_LIFT_INSTANCE_SAMPLE])
        if len(self.instances) > _LIFT_INSTANCE_SAMPLE:
            shown.append(f"+{len(self.instances) - _LIFT_INSTANCE_SAMPLE} more")
        where = ", ".join(shown)
        noun = "instance" if len(self.instances) == 1 else "instances"
        return [
            replace(
                v,
                message=(
                    f"[inside `{self.module}` — analysed once, "
                    f"{len(self.instances)} {noun}: {where}] {v.message}"
                ),
                cell_name=self.instances[0],
                instance_path=(),
            )
            for v in self.analysis.violations
        ]


@dataclass(frozen=True)
class CompositionStats:
    """Bookkeeping for one compositional boundary walk.

    ``instances`` is the number of blackbox *instances* the parent
    carries; ``summarised`` is the number of instances that received a
    :class:`BoundarySummary` (one entry per abstracted instance in the
    returned per-instance map). ``cache_hits`` counts instances whose
    ``(module type, clock context)`` had already been summarised — the
    direct evidence that an identical subtree was analysed once and
    reused rather than recomputed. ``declined`` counts distinct module
    types the summariser refused to abstract for at least one context
    (not provably single-clock); those instances fall through to the
    normal flat walk. ``boundary_modules`` is the set of distinct module
    *types* that were summarised for at least one instance — the set
    CDC-008 exempts from its clock-as-data check (the boundary cell's
    clock pin is distribution into the opaque subtree, not data).
    ``primitive_modules`` (#275) is the subset of ``boundary_modules``
    recognised as sanctioned CDC synchroniser macros (the ``xpm_cdc_*``
    family plus any ``--sync-primitive`` registrations) — summarised as
    synchronisers rather than as single-clock subtrees.
    ``clock_output_ports`` (#273) records the *why* for the clock-output
    decline flavour: ``(module type, output port)`` pairs for candidates
    refused because an output of theirs drives a clock. Every module type
    named here is also in ``declined_modules``; the CLI reads the pair set
    to name the offending port in the ``CDC-BBX`` message.
    """

    instances: int = 0
    summarised: int = 0
    cache_hits: int = 0
    declined: int = 0
    declined_modules: frozenset[str] = field(default_factory=frozenset)
    boundary_modules: frozenset[str] = field(default_factory=frozenset)
    primitive_modules: frozenset[str] = field(default_factory=frozenset)
    clock_output_ports: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    # (#261) Module types whose own internals were analysed compositionally
    # (a *greybox*: blackbox-attributed but still carrying its cells), and
    # the per-``(type, clock context)`` records the CLI lifts into the
    # report. ``ambiguous_input_modules`` records the third decline
    # flavour: a multi-clock module with an input captured in two internal
    # domains, which one virtual sink cannot represent.
    analysed_modules: frozenset[str] = field(default_factory=frozenset)
    lifted: tuple[LiftedAnalysis, ...] = ()
    ambiguous_input_modules: frozenset[str] = field(default_factory=frozenset)
    # (#261) Module types declined because an internal flop could not be
    # placed on any of the parent's clock roots — a clock arriving on a
    # pin no classifier recognises. Pin inspection alone reads such a
    # module as single-clock and abstracts its internal crossing away
    # silently; the compositional pass is the only thing that can see it.
    unresolved_internal_modules: frozenset[str] = field(default_factory=frozenset)

    @property
    def shared_subtree_reused(self) -> bool:
        """True iff at least one ``(module, clock context)`` was
        instantiated more than once and its summary served from cache
        rather than being recomputed."""
        return self.cache_hits > 0

    def clock_output_ports_of(self, module_type: str) -> tuple[str, ...]:
        """The offending output ports of ``module_type``, sorted.

        Empty when the type was not declined for driving a clock output.
        """
        return tuple(
            sorted(p for (m, p) in self.clock_output_ports if m == module_type)
        )

    def lifted_violations(self) -> list[Violation]:
        """Every module-internal finding, re-stamped for the parent (#261)."""
        out: list[Violation] = []
        for entry in self.lifted:
            out.extend(entry.lifted_violations())
        return out

    def internal_crossings(self) -> int:
        """Async crossings found *inside* analysed boundary modules."""
        return sum(e.analysis.crossings for e in self.lifted)


def compose_boundaries(
    top: Module,
    blackboxes: dict[str, Module] | None,
    spec: ClockSpec,
    *,
    max_depth: int = 16,
    required_depth: int = 2,
    sync_primitives: frozenset[str] = frozenset(),
) -> tuple[dict[str, BoundarySummary], CompositionStats]:
    """Summarise every distinct ``(module, clock context)`` once and compose.

    Walks each boundary instance in ``top`` (an ordinary cell whose
    ``type`` is a blackbox module name). Summarisation is cached by the
    ``(module type, clock context)`` pair: the first instance of a given
    type-in-a-domain costs one
    :func:`~rtl_buddy_cdc.abstract.summarise_subtree` call and every
    later instance with the *same* context is served from cache (the
    analyse-once / perf win). An instance of the same type in a
    *different* clock domain is summarised separately, so its boundary
    crossings are seeded against the domain its parent actually drives —
    the per-instance correctness the type-only keying could not express.

    Returns ``(boundaries, stats)``. ``boundaries`` is keyed **per
    instance** (the cell name) and only contains instances whose
    ``(module, context)`` was *provably* single-clock — a declined
    instance is absent (analysed by the normal flat walk, which sees no
    internals through the opaque boundary). ``find_crossings`` resolves a
    cell's summary instance-first, falling back to a module-type key for
    legacy hand-built callers. ``stats.boundary_modules`` carries the
    distinct module types abstracted (for CDC-008's boundary exemption).

    Decline flavours feed ``stats.declined_modules``: a candidate that is
    **not provably single-clock** (and whose internals are unavailable),
    (#273) a candidate whose **output drives a clock** — clock generation
    / forwarding that blackboxing would silently elide — and (#261) a
    multi-clock candidate with an **ambiguous input port** even though its
    internals were analysed. The clock-output flavour is decided per
    module type in a pre-pass (any qualifying instance declines the type)
    and recorded in ``stats.clock_output_ports`` so the CLI can name the
    offending port; all flavours land on the same ``CDC-BBX`` report path
    and are waived identically.

    **Compositional analysis (#261).** When a boundary module carries its
    own cells — a *greybox*, produced by marking it ``blackbox`` while
    keeping its body (``lint --greybox``) — it is run through the ordinary
    pipeline **once per ``(module type, clock context)``**, on its own
    internals, by :func:`~rtl_buddy_cdc.compositional.analyse_module`. The
    result is folded into the boundary summary (per-port synchroniser
    proofs, per-port domains, internal reconvergence) and its internal
    findings are collected in ``stats.lifted`` for the CLI to report once,
    attributed to the instances they cover. That is what retires the #259
    reconvergence gate and the multi-clock decline for such a module.

    The cache key is the ``(module type, clock-PIN -> root mapping)`` pair
    rather than the bare root set. Two instances of one dual-clock module
    wired src/dest in opposite directions share a root *set* but need
    opposite per-port domains; keying on the mapping is a strict
    refinement, so identical instances still hit the cache.

    Pure: no I/O, no mutation of ``top`` or ``blackboxes``.

    ``max_depth`` is the clock-trace hop budget threaded to
    :func:`~rtl_buddy_cdc.abstract._instance_clocks` /
    :func:`~rtl_buddy_cdc.abstract.summarise_subtree` (default 16,
    surfaced as ``--clock-trace-depth``). It MUST equal the budget the
    crossing walk uses on the same run so the abstraction decision and
    the crossing walk resolve the same clock roots — a mismatch could
    let a deep clock pin be missed here and collapse a multi-clock
    boundary to a false single-clock summary, dropping its internal
    crossing. See issue #263.
    """
    out: dict[str, BoundarySummary] = {}
    if not blackboxes:
        return out, CompositionStats()

    # Summary cache keyed by ``(module type, clock context)``. ``None``
    # records a context we summarised and *declined* so a later instance
    # of the same context is also a cache hit (analysed once), not a
    # re-summarise. Keying on the resolved clock context (not just the
    # type) is what makes one module instantiated under two domains
    # correct while still hitting the cache for identical instances.
    Key = tuple[str, tuple[tuple[str, str | None], ...]]
    seen: dict[Key, BoundarySummary | None] = {}
    analyses: dict[Key, ModuleAnalysis | None] = {}
    lifted_instances: dict[Key, list[str]] = {}
    instances = 0
    cache_hits = 0
    declined_modules: set[str] = set()
    boundary_modules: set[str] = set()
    primitive_modules: set[str] = set()
    analysed_modules: set[str] = set()
    ambiguous_input_modules: set[str] = set()
    unresolved_internal_modules: set[str] = set()

    # Clock-output decline (#273), decided per MODULE before anything is
    # summarised: a candidate whose output drives a clock generates or
    # forwards that clock, and blackboxing it would elide the clock
    # network. The verdict is module-level because *any* instance
    # qualifying is enough — a clock-forwarding tile whose top instance
    # leaves ``clk_out`` unconnected is the same module, and abstracting
    # it there would be just as unsound. The per-parent maps are built
    # once and shared across instances.
    clock_out_pairs: set[tuple[str, str]] = set()
    clock_out_modules: set[str] = set()
    clock_pins_by_module = blackbox_clock_pins_by_module(
        top, blackboxes, spec=spec, pin_clocks=spec.pin_clocks, max_depth=max_depth
    )
    sink_bits = _clock_sink_bits(
        top, clock_pins_by_module, spec=spec, pin_clocks=spec.pin_clocks
    )
    consumers = _bit_consumers(top)
    for cell in top.cells.values():
        sub = blackboxes.get(cell.type)
        if sub is None:
            continue
        offenders = clock_driving_output_ports(
            top,
            cell,
            sub,
            blackboxes=blackboxes,
            spec=spec,
            pin_clocks=spec.pin_clocks,
            max_depth=max_depth,
            clock_pins_by_module=clock_pins_by_module,
            sink_bits=sink_bits,
            consumers=consumers,
        )
        if offenders:
            clock_out_modules.add(cell.type)
            clock_out_pairs.update((cell.type, p) for p in offenders)

    for inst_name, cell in top.cells.items():
        sub = blackboxes.get(cell.type)
        if sub is None:
            continue
        instances += 1
        if cell.type in clock_out_modules:
            # Declined ahead of the primitive path too: a sanctioned
            # synchroniser that also forwards a clock would elide the
            # clock network exactly the same way, and the user's
            # ``--sync-primitive`` promise is about the data crossing.
            declined_modules.add(cell.type)
            continue
        if is_sync_primitive(cell.type, sync_primitives):
            # Recognised CDC macro (#275): summarised as a synchroniser,
            # never declined as "not provably single-clock". Deliberately
            # NOT cached — the primitive summary depends on WHICH root is
            # the destination, and the cache key's clock context is a
            # frozenset, so two instances wired src/dest in opposite
            # directions would collide. Tracing two clock pins is cheap.
            prim = summarise_sync_primitive(
                top, cell, sub, spec, pin_clocks=spec.pin_clocks, max_depth=max_depth
            )
            if prim is not None:
                out[inst_name] = prim
                boundary_modules.add(cell.type)
                primitive_modules.add(normalise_type(cell.type))
                continue
            # Destination clock unidentifiable — fall through to the
            # generic path so the instance is declined (and reported)
            # rather than silently vouched for.
        # Clock CONTEXT is the instance's clock-PIN -> root MAPPING (FIX 1
        # + #261) — not a single root, so a dual-clock IP keys distinctly
        # from a single-clock one, two instances of one module wired in
        # opposite directions key distinctly from each other, and
        # identical instances still hit the cache.
        ic = _instance_clocks(
            top, cell, sub, spec=spec, pin_clocks=spec.pin_clocks, max_depth=max_depth
        )
        cache_key: Key = (cell.type, ic.pin_roots)
        if cache_key in seen:
            # Identical subtree in an identical clock context: reuse the
            # already-computed (or already-declined) summary AND its
            # already-computed internal analysis — the analyse-once win.
            cache_hits += 1
            summary = seen[cache_key]
        else:
            # Compositional per-module pass (#261). Returns ``None`` for a
            # stub blackbox (zero cells), in which case ``summarise_subtree``
            # behaves exactly as it did before this change.
            analyses[cache_key] = analyse_module(
                sub,
                ic.pin_root_map(),
                spec,
                max_depth=max_depth,
                required_depth=required_depth,
                sync_primitives=sync_primitives,
            )
            summary = summarise_subtree(
                top,
                cell,
                sub,
                spec,
                pin_clocks=spec.pin_clocks,
                max_depth=max_depth,
                analysis=analyses[cache_key],
            )
            seen[cache_key] = summary
        analysis = analyses.get(cache_key)
        if summary is None:
            declined_modules.add(cell.type)
            if analysis is None:
                pass  # stub blackbox: the pre-#261 decline flavours apply
            elif analysis.resolved:
                # Internals were analysable but a port could not be placed
                # in ONE domain — the #261 ambiguous-capture decline.
                ambiguous_input_modules.add(cell.type)
            else:
                # An internal flop is clocked from something the parent
                # never handed in. Recorded separately so the CDC-BBX
                # message can point at the real cause.
                unresolved_internal_modules.add(cell.type)
        else:
            out[inst_name] = summary
            boundary_modules.add(cell.type)
            if analysis is not None:
                analysed_modules.add(cell.type)
                lifted_instances.setdefault(cache_key, []).append(inst_name)

    # Lifted internal findings, one record per ``(module type, clock
    # context)`` — the analyse-once contract made visible in the report.
    lifted: list[LiftedAnalysis] = []
    for key in sorted(lifted_instances):
        analysis = analyses.get(key)
        if analysis is None or not analysis.violations:
            continue
        lifted.append(
            LiftedAnalysis(
                module=key[0],
                instances=tuple(sorted(lifted_instances[key])),
                analysis=analysis,
            )
        )

    stats = CompositionStats(
        instances=instances,
        summarised=len(out),
        cache_hits=cache_hits,
        declined=len(declined_modules),
        declined_modules=frozenset(declined_modules),
        boundary_modules=frozenset(boundary_modules),
        primitive_modules=frozenset(primitive_modules),
        clock_output_ports=frozenset(clock_out_pairs),
        analysed_modules=frozenset(analysed_modules),
        lifted=tuple(lifted),
        ambiguous_input_modules=frozenset(ambiguous_input_modules),
        unresolved_internal_modules=frozenset(unresolved_internal_modules),
    )
    return out, stats


def reconvergence_unsafe_instances(
    crossings: list[Crossing],
    boundaries: dict[str, BoundarySummary] | None = None,
) -> set[str]:
    """Instances with foreign-domain crossings into >=2 DISTINCT input ports.

    FIX 3 (soundness audit of #259). A single-clock block that *is*
    abstracted but has two or more distinct foreign-domain crossings
    entering DISTINCT input ports can hide an internal reconvergence
    (CDC-005) the flat design would flag — the boundary collapse severs
    the internal graph, so the reconvergence among those ports cannot be
    checked at the boundary. Conservative policy: refuse to abstract such
    a block.

    Groups the boundary-*sink* crossings (those carrying ``dst_boundary``)
    by instance and counts DISTINCT ``dst_boundary`` ports per instance.
    An instance with >=2 distinct incoming ports is reconvergence-unsafe.
    A single multi-bit bus on ONE port counts as 1 port (safe — the
    multi-bit rules cover it).

    **Retired per instance by compositional analysis (#261).** When
    ``boundaries`` is supplied, an instance whose summary carries
    ``internal_analysed`` is exempt: its module's internals HAVE been
    analysed, the internal reconvergence is recorded in the summary, and
    CDC-005 re-raises it at the boundary
    (:func:`~rtl_buddy_cdc.rules._boundary_reconvergence`). The gate exists
    because the collapse severed a fact; once the fact is back, declining
    would only destroy coverage. An instance with no analysis (a stub
    blackbox) keeps the conservative #259 behaviour.

    Pure: reads only the crossing list and the boundary map.
    """
    analysed = {
        inst
        for inst, summary in (boundaries or {}).items()
        if summary.internal_analysed
    }
    ports_by_inst: dict[str, set[str]] = {}
    for c in crossings:
        if c.dst_boundary is None:
            continue
        inst, port = c.dst_boundary
        if inst in analysed:
            continue
        ports_by_inst.setdefault(inst, set()).add(port)
    return {inst for inst, ports in ports_by_inst.items() if len(ports) >= 2}
